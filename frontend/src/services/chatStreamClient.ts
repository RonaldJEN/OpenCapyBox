import { apiService } from './api';
import { eventMessageId, eventSequence, eventToolCallId, flushSSEBuffer, parseSSELines } from './sseParser';
import { formatHttpErrorMessage } from '../utils/errorMessages';
import type {
  ChatContentBlock,
  RoundData,
  StreamDeltaMeta,
} from '../types';
import type { StreamEnvelope, StreamSource } from '../runtime/chatRuntimeTypes';

const MAX_RETRIES = 3;
const RETRY_BASE_MS = 1000;
const STALE_TIMEOUT_MS = 45_000;

const ROUND_TERMINAL_STATUSES = new Set([
  'completed',
  'failed',
  'interrupted',
  'resumed',
  'cancelled',
  'max_steps_reached',
]);

const TERMINAL_EVENT_TYPES = new Set(['RUN_FINISHED', 'RUN_ERROR']);
const DELTA_EVENT_TYPES = new Set([
  'TEXT_MESSAGE_CONTENT',
  'THINKING_TEXT_MESSAGE_CONTENT',
  'TOOL_CALL_ARGS',
]);

class HttpError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = 'HttpError';
  }
}

class RoundExistsError extends Error {
  constructor(public readonly roundId: string) {
    super('SSE_ROUND_EXISTS');
    this.name = 'RoundExistsError';
  }
}

class TerminalComplete extends Error {
  constructor() {
    super('STREAM_TERMINAL_COMPLETE');
    this.name = 'TerminalComplete';
  }
}

class UserAbort extends Error {
  constructor() {
    super('STREAM_USER_ABORT');
    this.name = 'UserAbort';
  }
}

interface AbortState {
  userAborted: boolean;
  controllers: Set<AbortController>;
}

interface StreamIdentity {
  ownerSessionId: string;
  clientRunKey: string;
  transportEpoch: number;
  connectionId: string;
  source: StreamSource;
}

interface StreamHandlers {
  onEnvelope: (envelope: StreamEnvelope) => void;
  onError?: (message: string, code?: string) => void;
}

interface StartSendArgs extends StreamIdentity, StreamHandlers {
  content: ChatContentBlock[];
  idempotencyKey?: string;
  preferredSkillKeys?: string[];
  onRejectedBeforeAccept?: () => void;
}

interface SubscribeArgs extends StreamIdentity, StreamHandlers {
  serverRunId: string;
  lastSequence?: number;
}

interface ResumeArgs extends StreamIdentity, StreamHandlers {
  interruptId: string;
  answers: Record<string, string>;
}

export interface RuntimeSubscription {
  abort: () => void;
  promise: Promise<void>;
  getLatestSequence?: () => number;
}

function createAbortState(): AbortState {
  return {
    userAborted: false,
    controllers: new Set(),
  };
}

function abortState(state: AbortState) {
  state.userAborted = true;
  for (const controller of state.controllers) {
    controller.abort();
  }
}

async function getSessionHistoryWithAbort(
  abort: AbortState,
  ownerSessionId: string,
): ReturnType<typeof apiService.getSessionHistoryV2> {
  if (abort.userAborted) throw new UserAbort();
  const controller = new AbortController();
  abort.controllers.add(controller);
  try {
    const history = await apiService.getSessionHistoryV2(
      ownerSessionId,
      controller.signal,
    );
    if (abort.userAborted) throw new UserAbort();
    return history;
  } catch (error) {
    if (abort.userAborted || controller.signal.aborted) throw new UserAbort();
    throw error;
  } finally {
    abort.controllers.delete(controller);
  }
}

function makeEnvelope(identity: StreamIdentity, event: any, meta?: StreamDeltaMeta): StreamEnvelope {
  return {
    ownerSessionId: identity.ownerSessionId,
    clientRunKey: identity.clientRunKey,
    serverRunId: event?.runId,
    transportEpoch: identity.transportEpoch,
    connectionId: identity.connectionId,
    event,
    source: identity.source,
    sequence: meta?.sequence ?? eventSequence(event),
    isAggregate: meta?.isAggregate ?? event?.isAggregate,
    eventId: event?.id,
    messageId: eventMessageId(event),
    toolCallId: eventToolCallId(event),
    receivedAt: Date.now(),
  };
}

function emit(handlers: StreamHandlers, identity: StreamIdentity, event: any, meta?: StreamDeltaMeta) {
  handlers.onEnvelope(makeEnvelope(identity, event, meta));
}

function metaForEvent(event: any, source: StreamSource): StreamDeltaMeta | undefined {
  const sequence = eventSequence(event);
  const explicitAggregate = event?.isAggregate ?? event?.aggregate ?? event?.replay;
  const replayAggregate = source === 'subscribe'
    && sequence !== undefined
    && DELTA_EVENT_TYPES.has(event?.type);

  if (sequence === undefined && explicitAggregate === undefined && !replayAggregate) {
    return undefined;
  }

  return {
    sequence,
    isAggregate: Boolean(explicitAggregate) || replayAggregate || undefined,
  };
}

function terminalFromRound(round: any, threadId: string, runId: string): any | null {
  if (!round || !ROUND_TERMINAL_STATUSES.has(round.status)) return null;
  if (round.status === 'failed') {
    return {
      type: 'RUN_ERROR',
      threadId,
      runId,
      message: round.final_response || 'Run failed',
      code: 'RUN_FAILED',
      sequence: round.last_event_sequence,
    };
  }

  const outcome = round.status === 'completed'
    ? 'success'
    : (
        round.status === 'interrupted'
        || round.status === 'cancelled'
        || round.status === 'resumed'
        || round.status === 'max_steps_reached'
      )
      ? 'interrupt'
      : 'error';

  return {
    type: 'RUN_FINISHED',
    threadId,
    runId,
    result: {
      finalResponse: round.final_response || '',
      stepCount: round.step_count || 0,
      ...(round.status === 'cancelled' ? { reason: 'user_cancelled' } : {}),
      ...(round.status === 'max_steps_reached' ? { reason: 'max_steps_reached' } : {}),
    },
    outcome,
    interrupt: round.interrupt,
    sequence: round.last_event_sequence,
  };
}

function runStartedFromRound(round: RoundData | undefined, threadId: string, runId: string) {
  return {
    type: 'RUN_STARTED',
    threadId,
    runId,
    preferredSkills: Array.isArray(round?.preferred_skills)
      ? round.preferred_skills
      : [],
  };
}

function emitRecoveredTerminal(
  handlers: StreamHandlers,
  identity: StreamIdentity,
  round: RoundData | undefined,
  runId: string,
): boolean {
  const terminal = terminalFromRound(round, identity.ownerSessionId, runId);
  if (!terminal) return false;
  const started = runStartedFromRound(round, identity.ownerSessionId, runId);
  emit(handlers, identity, started, metaForEvent(started, identity.source));
  emit(handlers, identity, terminal, metaForEvent(terminal, identity.source));
  return true;
}

function roundCreatedAtMs(round: any): number {
  const value = new Date(round?.created_at || 0).getTime();
  return Number.isFinite(value) ? value : 0;
}

function newestRound(rounds: any[] | undefined, predicate: (round: any) => boolean): any | undefined {
  return [...(rounds || [])]
    .filter(predicate)
    .sort((a, b) => roundCreatedAtMs(b) - roundCreatedAtMs(a))[0];
}

function matchesAcceptedRequest(round: any, idempotencyKey?: string): boolean {
  return Boolean(idempotencyKey && round?.idempotency_key === idempotencyKey);
}

function is4xx(error: unknown): error is HttpError {
  return error instanceof HttpError && error.status >= 400 && error.status < 500;
}

function is5xx(error: unknown): error is HttpError {
  return error instanceof HttpError && error.status >= 500;
}

function delayWithAbort(abort: AbortState, ms: number): Promise<void> {
  if (abort.userAborted) return Promise.resolve();

  const controller = new AbortController();
  abort.controllers.add(controller);

  return new Promise<void>((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      controller.signal.removeEventListener('abort', finish);
      abort.controllers.delete(controller);
      resolve();
    };

    controller.signal.addEventListener('abort', finish, { once: true });
    const timer = setTimeout(finish, ms);
  });
}

async function fetchSSE(
  url: string,
  init: RequestInit,
  abort: AbortState,
  onAccepted: (() => void) | undefined,
  onEvent: (event: any) => void,
): Promise<void> {
  if (abort.userAborted) throw new UserAbort();
  const controller = new AbortController();
  abort.controllers.add(controller);
  let lastDataTime = Date.now();
  let staleAbort = false;
  const staleTimer = setInterval(() => {
    if (Date.now() - lastDataTime > STALE_TIMEOUT_MS) {
      staleAbort = true;
      controller.abort();
    }
  }, 10_000);

  try {
    const response = await fetch(url, {
      ...init,
      signal: controller.signal,
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new HttpError(response.status, formatHttpErrorMessage(response.status, errorText));
    }

    onAccepted?.();

    const reader = response.body?.getReader();
    if (!reader) {
      throw new Error('Response body is null');
    }

    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      lastDataTime = Date.now();

      if (done) {
        for (const event of flushSSEBuffer(buffer)) {
          onEvent(event);
        }
        return;
      }

      const parsed = parseSSELines(buffer, decoder.decode(value, { stream: true }));
      buffer = parsed.buffer;
      for (const event of parsed.events) {
        onEvent(event);
      }
    }
  } catch (error: any) {
    if (abort.userAborted && (error?.name === 'AbortError' || controller.signal.aborted)) {
      throw new UserAbort();
    }
    if (staleAbort && (error?.name === 'AbortError' || controller.signal.aborted)) {
      throw new Error('SSE_STALE_TIMEOUT');
    }
    throw error;
  } finally {
    clearInterval(staleTimer);
    abort.controllers.delete(controller);
  }
}

function handleStreamEvent(
  event: any,
  identity: StreamIdentity,
  handlers: StreamHandlers,
  onRunStarted?: (threadId: string, runId: string) => void,
  onTerminal?: () => void,
) {
  if (event?.type === 'RUN_ERROR' && event.code === 'ROUND_IN_PROGRESS') {
    throw new RoundExistsError(event.message);
  }
  if (event?.type === 'RUN_STARTED') {
    onRunStarted?.(event.threadId, event.runId);
  }
  if (TERMINAL_EVENT_TYPES.has(event?.type)) {
    onTerminal?.();
  }
  emit(handlers, identity, event, metaForEvent(event, identity.source));
}

async function recoverTerminal(
  identity: StreamIdentity,
  handlers: StreamHandlers,
  runId: string,
  abort: AbortState,
): Promise<boolean> {
  const history = await getSessionHistoryWithAbort(abort, identity.ownerSessionId);
  if (abort.userAborted) throw new UserAbort();
  const round = history.rounds.find((item: any) => item.round_id === runId);
  return emitRecoveredTerminal(handlers, identity, round, runId);
}

function subscribeOnce(args: SubscribeArgs, abort: AbortState): RuntimeSubscription {
  let latestSequence = args.lastSequence || 0;
  let terminalReceived = false;
  const identity: StreamIdentity = {
    ownerSessionId: args.ownerSessionId,
    clientRunKey: args.clientRunKey,
    transportEpoch: args.transportEpoch,
    connectionId: args.connectionId,
    source: args.source,
  };

  const promise = (async () => {
    try {
      await fetchSSE(
        `/api/chat/${args.ownerSessionId}/round/${args.serverRunId}/subscribe?last_sequence=${latestSequence}`,
        {
          method: 'GET',
          headers: {
            Accept: 'text/event-stream',
            ...apiService.getAuthHeaders(),
          },
        },
        abort,
        undefined,
        (event) => {
          const sequence = eventSequence(event);
          if (sequence !== undefined) {
            latestSequence = Math.max(latestSequence, sequence);
          }
          handleStreamEvent(event, identity, args, undefined, () => {
            terminalReceived = true;
          });
          if (terminalReceived) {
            throw new TerminalComplete();
          }
        },
      );

      if (!terminalReceived) {
        throw new Error('SSE_STREAM_CLOSED');
      }
    } catch (error: any) {
      if (error instanceof UserAbort || error instanceof TerminalComplete) {
        return;
      }
      try {
        if (await recoverTerminal(identity, args, args.serverRunId, abort)) {
          return;
        }
      } catch (recoverError) {
        if (recoverError instanceof UserAbort || abort.userAborted) return;
        console.error('检查轮次状态失败:', recoverError);
      }
      args.onError?.(error?.message || '订阅连接已断开');
      throw error;
    }
  })();

  return {
    abort: () => abortState(abort),
    promise,
    getLatestSequence: () => latestSequence,
  };
}

export function startSubscribeStream(args: SubscribeArgs): RuntimeSubscription {
  return subscribeOnce(args, createAbortState());
}

export function startSendStream(args: StartSendArgs): RuntimeSubscription {
  const abort = createAbortState();
  const identity: StreamIdentity = {
    ownerSessionId: args.ownerSessionId,
    clientRunKey: args.clientRunKey,
    transportEpoch: args.transportEpoch,
    connectionId: args.connectionId,
    source: args.source,
  };

  let currentThreadId: string | null = null;
  let currentRunId: string | null = null;
  let runCompleted = false;
  let streamAccepted = false;
  let preAcceptRejectionNotified = false;
  let retryCount = 0;
  let successfulHistoryChecks = 0;
  let failedHistoryChecks = 0;
  let latestSequence = 0;
  const seenSequences = new Set<number>();

  const notifyRejectedBeforeAccept = () => {
    if (streamAccepted || preAcceptRejectionNotified) return;
    preAcceptRejectionNotified = true;
    args.onRejectedBeforeAccept?.();
  };

  const markStreamAccepted = () => {
    if (streamAccepted) return;
    streamAccepted = true;
    emit(args, identity, { type: 'CUSTOM', name: 'stream_accepted', value: {} });
  };

  const markSequence = (event: any): boolean => {
    const sequence = eventSequence(event);
    if (sequence === undefined) {
      return true;
    }
    if (seenSequences.has(sequence)) {
      return false;
    }
    seenSequences.add(sequence);
    latestSequence = Math.max(latestSequence, sequence);
    return true;
  };

  const subscribeToKnownRound = async (runId: string) => {
    if (abort.userAborted) throw new UserAbort();
    currentRunId = runId;
    currentThreadId = args.ownerSessionId;
    const subscription = subscribeOnce({
      ownerSessionId: args.ownerSessionId,
      clientRunKey: args.clientRunKey,
      transportEpoch: args.transportEpoch,
      connectionId: args.connectionId,
      source: 'subscribe',
      serverRunId: runId,
      lastSequence: latestSequence,
      onEnvelope: args.onEnvelope,
      onError: args.onError,
    }, abort);

    try {
      await subscription.promise;
      runCompleted = true;
    } finally {
      latestSequence = Math.max(
        latestSequence,
        subscription.getLatestSequence?.() ?? latestSequence,
      );
    }
  };

  const doPost = async () => {
    await fetchSSE(
      `/api/chat/${args.ownerSessionId}/message/stream`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...apiService.getAuthHeaders(),
        },
        body: JSON.stringify({
          content: args.content,
          idempotency_key: args.idempotencyKey,
          ...(args.preferredSkillKeys?.length
            ? { preferred_skill_keys: args.preferredSkillKeys }
            : {}),
        }),
      },
      abort,
      markStreamAccepted,
      (event) => {
        if (!markSequence(event)) return;
        handleStreamEvent(
          event,
          identity,
          args,
          (threadId, runId) => {
            currentThreadId = threadId;
            currentRunId = runId;
          },
          () => {
            runCompleted = true;
          },
        );
      },
    );

    if (!runCompleted) {
      throw new Error('SSE_STREAM_CLOSED');
    }
  };

  const promise = (async () => {
    try {
      await doPost();
      return;
    } catch (error: any) {
      if (error instanceof UserAbort) return;

      if (is4xx(error)) {
        notifyRejectedBeforeAccept();
        const code = error.status === 429 ? 'USER_BUSY' : 'HTTP_CLIENT_ERROR';
        emit(args, identity, { type: 'RUN_ERROR', message: error.message, code });
        return;
      }

      if (is5xx(error)) {
        notifyRejectedBeforeAccept();
        emit(args, identity, { type: 'RUN_ERROR', message: error.message, code: 'SERVER_ERROR' });
        return;
      }

      if (error instanceof RoundExistsError) {
        currentRunId = error.roundId;
        currentThreadId = args.ownerSessionId;
      }

      while (!runCompleted && retryCount < MAX_RETRIES && !abort.userAborted) {
        retryCount += 1;
        await delayWithAbort(abort, RETRY_BASE_MS * retryCount);
        if (abort.userAborted) return;

        if (currentThreadId && currentRunId) {
          try {
            await subscribeToKnownRound(currentRunId);
            return;
          } catch (retryError) {
            if (retryError instanceof UserAbort || abort.userAborted) return;
            console.error(`重连/重试失败 (${retryCount}/${MAX_RETRIES}):`, retryError);
            continue;
          }
        }

        let history: Awaited<ReturnType<typeof apiService.getSessionHistoryV2>>;
        try {
          history = await getSessionHistoryWithAbort(abort, args.ownerSessionId);
        } catch (retryError) {
          if (abort.userAborted) return;
          failedHistoryChecks += 1;
          console.error(`请求状态确认失败 (${retryCount}/${MAX_RETRIES}):`, retryError);
          continue;
        }
        if (abort.userAborted) return;
        successfulHistoryChecks += 1;

        const acceptedRound = newestRound(history.rounds, (round) => (
          matchesAcceptedRequest(round, args.idempotencyKey)
          && (round.status === 'running' || ROUND_TERMINAL_STATUSES.has(round.status))
        ));

        if (acceptedRound) markStreamAccepted();

        if (acceptedRound && acceptedRound.status !== 'running') {
          if (emitRecoveredTerminal(args, identity, acceptedRound, acceptedRound.round_id)) {
            runCompleted = true;
            return;
          }
        }

        if (acceptedRound?.status === 'running') {
          if (abort.userAborted) return;
          try {
            await subscribeToKnownRound(acceptedRound.round_id);
            return;
          } catch (retryError) {
            if (retryError instanceof UserAbort || abort.userAborted) return;
            console.error(`重连/重试失败 (${retryCount}/${MAX_RETRIES}):`, retryError);
            continue;
          }
        }
      }

      if (abort.userAborted || runCompleted) return;

      if (currentRunId) {
        try {
          const recovered = await recoverTerminal(
            { ...identity, source: 'subscribe' },
            args,
            currentRunId,
            abort,
          );
          if (recovered) return;
        } catch (recoverError) {
          if (recoverError instanceof UserAbort || abort.userAborted) return;
          console.error('检查轮次状态失败:', recoverError);
        }
        if (abort.userAborted) return;
        emit(args, identity, {
          type: 'RUN_ERROR',
          message: '连接已断开，Agent 可能仍在运行。请刷新页面查看结果',
          code: 'SSE_DISCONNECTED',
        });
      } else {
        const confirmedNotAccepted = (
          !streamAccepted
          && Boolean(args.idempotencyKey)
          && successfulHistoryChecks === MAX_RETRIES
          && failedHistoryChecks === 0
        );
        if (confirmedNotAccepted) {
          emit(args, identity, {
            type: 'RUN_ERROR',
            message: '网络中断，请检查连接后重试',
            code: 'REQUEST_FAILED',
          });
          notifyRejectedBeforeAccept();
        } else {
          emit(args, identity, {
            type: 'RUN_ERROR',
            message: streamAccepted
              ? '连接已断开，Agent 可能仍在运行。请刷新页面查看结果'
              : '连接已断开，暂时无法确认请求是否已受理。请刷新页面查看结果',
            code: 'REQUEST_STATUS_UNKNOWN',
          });
        }
      }
    }
  })();

  return {
    abort: () => abortState(abort),
    promise,
    getLatestSequence: () => latestSequence,
  };
}

export function startResumeStream(args: ResumeArgs): RuntimeSubscription {
  const abort = createAbortState();
  const identity: StreamIdentity = {
    ownerSessionId: args.ownerSessionId,
    clientRunKey: args.clientRunKey,
    transportEpoch: args.transportEpoch,
    connectionId: args.connectionId,
    source: args.source,
  };
  let terminalReceived = false;

  const promise = (async () => {
    try {
      await fetchSSE(
        `/api/chat/${args.ownerSessionId}/resume`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...apiService.getAuthHeaders(),
          },
          body: JSON.stringify({
            interrupt_id: args.interruptId,
            answers: args.answers,
          }),
        },
        abort,
        () => {
          emit(args, identity, { type: 'CUSTOM', name: 'stream_accepted', value: {} });
        },
        (event) => {
          handleStreamEvent(event, identity, args, undefined, () => {
            terminalReceived = true;
          });
        },
      );

      if (!terminalReceived) {
        const error = new Error('Resume stream ended without terminal event');
        emit(args, identity, { type: 'RUN_ERROR', message: error.message });
        throw error;
      }
    } catch (error: any) {
      if (error instanceof UserAbort) return;
      if (!terminalReceived) {
        emit(args, identity, { type: 'RUN_ERROR', message: error?.message || '恢复执行失败' });
      }
      throw error;
    }
  })();

  return {
    abort: () => abortState(abort),
    promise,
  };
}
