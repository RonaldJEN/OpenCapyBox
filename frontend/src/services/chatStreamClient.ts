import { apiService } from './api';
import { eventMessageId, eventSequence, eventToolCallId, flushSSEBuffer, parseSSELines } from './sseParser';
import { formatHttpErrorMessage } from '../utils/errorMessages';
import type {
  ChatContentBlock,
  RoundData,
  StreamDeltaMeta,
  TurnReasoningSelection,
} from '../types';
import {
  RUNTIME_HISTORY_SNAPSHOT,
  type StreamEnvelope,
  type StreamSource,
} from '../runtime/chatRuntimeTypes';

const MAX_RETRIES = 3;
const RETRY_BASE_MS = 1000;
const STALE_TIMEOUT_MS = 45_000;

const ROUND_TERMINAL_STATUSES = new Set([
  'completed',
  'failed',
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

class InteractionPendingError extends Error {
  readonly code = 'INTERACTION_PENDING';

  constructor(message?: string) {
    super(message || '当前 Round 正在等待用户回答');
    this.name = 'InteractionPendingError';
  }
}

class DurableInteractionRunError extends Error {
  constructor(public readonly event: any) {
    super(event?.message || event?.code || 'Interaction continuation failed');
    this.name = 'DurableInteractionRunError';
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

export type NonTerminalStreamReason =
  | 'eof'
  | 'network_error'
  | 'subscribe_control_error'
  | 'history_running'
  | 'history_waiting';

/** A subscribe transport ended while the durable Round was still non-terminal. */
export class NonTerminalStreamError extends Error {
  readonly code = 'SSE_NON_TERMINAL_END';

  constructor(
    public readonly reason: NonTerminalStreamReason,
    message: string,
  ) {
    super(message);
    this.name = 'NonTerminalStreamError';
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
  reasoning?: TurnReasoningSelection;
  onRejectedBeforeAccept?: () => void;
  onControlConflict?: (message: string, code: string, serverRunId?: string) => void;
}

interface SubscribeArgs extends StreamIdentity, StreamHandlers {
  serverRunId: string;
  lastSequence?: number;
  durableInteractionObserved?: boolean;
}

interface ResumeArgs extends StreamIdentity, StreamHandlers {
  interruptId: string;
  answers: Record<string, string>;
  serverRunId: string;
  lastSequence?: number;
}

export interface RuntimeSubscription {
  abort: () => void;
  promise: Promise<void>;
  getLatestSequence?: () => number;
  getHandoff?: () => RuntimeHandoff | undefined;
}

export type RuntimeHandoffStatus = 'running' | 'waiting_interaction' | 'unknown';

export interface RuntimeHandoff {
  serverRunId?: string;
  status: RuntimeHandoffStatus;
  lastSequence: number;
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

function makeEnvelope(
  identity: StreamIdentity,
  event: any,
  meta?: StreamDeltaMeta,
  authoritativeRecovery: boolean = false,
): StreamEnvelope {
  return {
    ownerSessionId: identity.ownerSessionId,
    clientRunKey: identity.clientRunKey,
    serverRunId: event?.runId,
    transportEpoch: identity.transportEpoch,
    connectionId: identity.connectionId,
    event,
    source: identity.source,
    authoritativeRecovery: authoritativeRecovery || undefined,
    sequence: meta?.sequence ?? eventSequence(event),
    isAggregate: meta?.isAggregate ?? event?.isAggregate,
    eventId: event?.id,
    messageId: eventMessageId(event),
    toolCallId: eventToolCallId(event),
    receivedAt: Date.now(),
  };
}

function emit(
  handlers: StreamHandlers,
  identity: StreamIdentity,
  event: any,
  meta?: StreamDeltaMeta,
  authoritativeRecovery: boolean = false,
) {
  handlers.onEnvelope(makeEnvelope(identity, event, meta, authoritativeRecovery));
}

function emitHistorySnapshot(
  handlers: StreamHandlers,
  identity: StreamIdentity,
  rounds: RoundData[],
  runId: string,
) {
  const round = rounds.find((item) => item.round_id === runId);
  const sequence = round?.last_event_sequence;
  emit(
    handlers,
    identity,
    {
      type: RUNTIME_HISTORY_SNAPSHOT,
      rounds,
      sequence,
    },
    sequence === undefined ? undefined : { sequence, isAggregate: true },
    true,
  );
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
        round.status === 'cancelled'
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

function interactionRequestedFromRound(round: RoundData, runId: string) {
  if (round.status !== 'waiting_interaction' || !round.interrupt?.id) return null;
  return {
    type: 'CUSTOM',
    name: 'interaction_requested',
    value: {
      interactionId: round.interrupt.id,
      runId,
      kind: round.interrupt.payload?.kind || 'user_input',
      toolCallId: round.interrupt.payload?.tool_call_id,
      payload: round.interrupt.payload || {},
    },
    sequence: round.last_event_sequence,
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
  emit(handlers, identity, terminal, metaForEvent(terminal, identity.source), true);
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
  if (event?.type === 'RUN_ERROR' && event.code === 'INTERACTION_PENDING') {
    throw new InteractionPendingError(event.message);
  }
  if (event?.type === 'RUN_STARTED') {
    onRunStarted?.(event.threadId, event.runId);
  }
  if (TERMINAL_EVENT_TYPES.has(event?.type)) {
    onTerminal?.();
  }
  emit(handlers, identity, event, metaForEvent(event, identity.source));
}

async function recoverRoundState(
  identity: StreamIdentity,
  handlers: StreamHandlers,
  runId: string,
  abort: AbortState,
): Promise<{ status: RuntimeHandoffStatus | 'terminal'; lastSequence: number }> {
  const history = await getSessionHistoryWithAbort(abort, identity.ownerSessionId);
  if (abort.userAborted) throw new UserAbort();
  const round = history.rounds.find((item: any) => item.round_id === runId);
  if (round) {
    emitHistorySnapshot(handlers, identity, history.rounds, runId);
  }
  const lastSequence = round?.last_event_sequence || 0;
  if (emitRecoveredTerminal(handlers, identity, round, runId)) {
    return { status: 'terminal', lastSequence };
  }
  const interactionEvent = round
    ? interactionRequestedFromRound(round, runId)
    : null;
  if (interactionEvent) {
    emit(handlers, identity, interactionEvent, undefined, true);
    return { status: 'waiting_interaction', lastSequence };
  }
  if (round?.status === 'running') {
    return { status: 'running', lastSequence };
  }
  return { status: 'unknown', lastSequence };
}

function isSubscribeControlError(event: any): boolean {
  return event?.type === 'RUN_ERROR'
    && event.code === 'SUBSCRIBE_FAILED'
    && eventSequence(event) === undefined;
}

function isUnsequencedRunError(event: any): boolean {
  return event?.type === 'RUN_ERROR' && eventSequence(event) === undefined;
}

function isDurableInteractionBoundary(event: any): boolean {
  return event?.type === 'CUSTOM'
    && (event.name === 'interaction_requested' || event.name === 'interaction_resolved')
    && eventSequence(event) !== undefined;
}

function subscribeOnce(args: SubscribeArgs, abort: AbortState): RuntimeSubscription {
  let latestSequence = args.lastSequence || 0;
  let terminalReceived = false;
  let durableInteractionObserved = Boolean(args.durableInteractionObserved);
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
          if (isSubscribeControlError(event)) {
            throw new NonTerminalStreamError(
              'subscribe_control_error',
              event.message || '订阅连接异常',
            );
          }
          if (durableInteractionObserved && isUnsequencedRunError(event)) {
            throw new DurableInteractionRunError(event);
          }
          const sequence = eventSequence(event);
          if (sequence !== undefined) {
            latestSequence = Math.max(latestSequence, sequence);
          }
          if (isDurableInteractionBoundary(event)) {
            durableInteractionObserved = true;
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
        throw new NonTerminalStreamError('eof', 'SSE_STREAM_CLOSED');
      }
    } catch (error: any) {
      if (error instanceof UserAbort || error instanceof TerminalComplete) {
        return;
      }
      if (error instanceof DurableInteractionRunError) {
        args.onError?.(error.message, error.event?.code);
      }
      let recoveredReason: NonTerminalStreamReason | null = null;
      try {
        const history = await getSessionHistoryWithAbort(abort, identity.ownerSessionId);
        const round = history.rounds.find((item: any) => item.round_id === args.serverRunId);
        if (round) {
          emitHistorySnapshot(args, identity, history.rounds, args.serverRunId);
          latestSequence = Math.max(latestSequence, round.last_event_sequence || 0);
        }
        if (emitRecoveredTerminal(args, identity, round, args.serverRunId)) {
          return;
        }
        const interactionEvent = round
          ? interactionRequestedFromRound(round, args.serverRunId)
          : null;
        if (interactionEvent) {
          emit(args, identity, interactionEvent, undefined, true);
          recoveredReason = 'history_waiting';
        } else if (round?.status === 'running') {
          recoveredReason = 'history_running';
        }
      } catch (recoverError) {
        if (recoverError instanceof UserAbort || abort.userAborted) return;
        console.error('检查轮次状态失败:', recoverError);
      }
      if (error instanceof NonTerminalStreamError && !recoveredReason) {
        throw error;
      }
      throw new NonTerminalStreamError(
        recoveredReason || 'network_error',
        error?.message || '订阅连接已断开',
      );
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
  let interactionRequested = false;
  let streamAccepted = false;
  let preAcceptRejectionNotified = false;
  let retryCount = 0;
  let successfulHistoryChecks = 0;
  let failedHistoryChecks = 0;
  let latestSequence = 0;
  let handoff: RuntimeHandoff | undefined;
  const seenSequences = new Set<number>();

  const handoffKnownRound = (
    status: RuntimeHandoffStatus,
    runId: string | null = currentRunId,
  ) => {
    handoff = {
      serverRunId: runId || undefined,
      status,
      lastSequence: latestSequence,
    };
  };

  const handoffFromSubscribeFailure = (error: unknown, runId: string): boolean => {
    if (!(error instanceof NonTerminalStreamError)) return false;
    if (error.reason === 'history_waiting') {
      interactionRequested = true;
      handoffKnownRound('waiting_interaction', runId);
      return true;
    }
    if (error.reason === 'history_running') {
      handoffKnownRound('running', runId);
      return true;
    }
    return false;
  };

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
      durableInteractionObserved: interactionRequested,
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
          ...(args.reasoning
            ? {
                thinking_mode: args.reasoning.mode,
                reasoning_effort: args.reasoning.effort,
              }
            : {}),
        }),
      },
      abort,
      markStreamAccepted,
      (event) => {
        if (!markSequence(event)) return;
        if (interactionRequested && isUnsequencedRunError(event)) {
          throw new DurableInteractionRunError(event);
        }
        if (event?.type === 'CUSTOM' && event.name === 'interaction_requested') {
          interactionRequested = true;
          currentRunId = currentRunId || event.value?.runId || null;
          currentThreadId = currentThreadId || args.ownerSessionId;
        }
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

    if (!runCompleted && !interactionRequested) {
      throw new Error('SSE_STREAM_CLOSED');
    }
  };

  const promise = (async () => {
    try {
      await doPost();
      return;
    } catch (error: any) {
      if (error instanceof UserAbort) return;

      if (error instanceof InteractionPendingError) {
        let pendingRound: RoundData | undefined;
        let history: Awaited<ReturnType<typeof apiService.getSessionHistoryV2>> | undefined;
        try {
          history = await getSessionHistoryWithAbort(abort, args.ownerSessionId);
          pendingRound = newestRound(
            history.rounds,
            (round) => round.status === 'waiting_interaction' && Boolean(round.interrupt?.id),
          );
        } catch (recoveryError) {
          if (recoveryError instanceof UserAbort || abort.userAborted) return;
          console.error('恢复待处理交互失败:', recoveryError);
        }

        args.onControlConflict?.(error.message, error.code, pendingRound?.round_id);
        args.onError?.(error.message, error.code);
        if (pendingRound && history) {
          currentRunId = pendingRound.round_id;
          currentThreadId = args.ownerSessionId;
          latestSequence = Math.max(latestSequence, pendingRound.last_event_sequence || 0);
          const recoveryIdentity: StreamIdentity = { ...identity, source: 'subscribe' };
          emitHistorySnapshot(
            args,
            recoveryIdentity,
            history.rounds,
            pendingRound.round_id,
          );
          const interactionEvent = interactionRequestedFromRound(
            pendingRound,
            pendingRound.round_id,
          );
          if (interactionEvent) {
            emit(args, recoveryIdentity, interactionEvent, undefined, true);
            interactionRequested = true;
          }
          handoffKnownRound('waiting_interaction', pendingRound.round_id);
        }
        return;
      }

      if (error instanceof DurableInteractionRunError) {
        args.onError?.(error.message, error.event?.code);
        if (currentRunId) {
          try {
            const recovered = await recoverRoundState(
              { ...identity, source: 'subscribe' },
              args,
              currentRunId,
              abort,
            );
            latestSequence = Math.max(latestSequence, recovered.lastSequence);
            if (recovered.status === 'terminal') return;
            if (recovered.status === 'waiting_interaction') {
              interactionRequested = true;
            }
            handoffKnownRound(recovered.status, currentRunId);
            return;
          } catch (recoveryError) {
            if (recoveryError instanceof UserAbort || abort.userAborted) return;
            console.error('恢复交互边界后的 Round 状态失败:', recoveryError);
          }
          if (interactionRequested) {
            handoffKnownRound('waiting_interaction', currentRunId);
            return;
          }
        }
      }

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
            if (handoffFromSubscribeFailure(retryError, currentRunId)) return;
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
          && (
            round.status === 'running'
            || round.status === 'waiting_interaction'
            || ROUND_TERMINAL_STATUSES.has(round.status)
          )
        ));

        if (acceptedRound) {
          markStreamAccepted();
          const recoveryIdentity: StreamIdentity = { ...identity, source: 'subscribe' };
          emitHistorySnapshot(
            args,
            recoveryIdentity,
            history.rounds,
            acceptedRound.round_id,
          );
          latestSequence = Math.max(
            latestSequence,
            acceptedRound.last_event_sequence || 0,
          );
        }

        if (acceptedRound && acceptedRound.status !== 'running') {
          const interactionEvent = interactionRequestedFromRound(
            acceptedRound,
            acceptedRound.round_id,
          );
          if (interactionEvent) {
            const recoveryIdentity: StreamIdentity = { ...identity, source: 'subscribe' };
            emit(args, recoveryIdentity, interactionEvent, undefined, true);
            interactionRequested = true;
            handoffKnownRound('waiting_interaction', acceptedRound.round_id);
            return;
          }
          if (emitRecoveredTerminal(
            args,
            { ...identity, source: 'subscribe' },
            acceptedRound,
            acceptedRound.round_id,
          )) {
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
            if (handoffFromSubscribeFailure(retryError, acceptedRound.round_id)) return;
            console.error(`重连/重试失败 (${retryCount}/${MAX_RETRIES}):`, retryError);
            continue;
          }
        }
      }

      if (abort.userAborted || runCompleted) return;

      if (currentRunId) {
        try {
          const recovered = await recoverRoundState(
            { ...identity, source: 'subscribe' },
            args,
            currentRunId,
            abort,
          );
          latestSequence = Math.max(latestSequence, recovered.lastSequence);
          if (recovered.status === 'terminal') return;
          if (recovered.status === 'waiting_interaction') {
            interactionRequested = true;
          }
          handoffKnownRound(recovered.status, currentRunId);
          if (recovered.status === 'unknown') {
            args.onError?.(
              '连接已断开，Agent 可能仍在运行。请刷新页面查看结果',
              'SSE_DISCONNECTED',
            );
          }
          return;
        } catch (recoverError) {
          if (recoverError instanceof UserAbort || abort.userAborted) return;
          console.error('检查轮次状态失败:', recoverError);
        }
        if (abort.userAborted) return;
        handoffKnownRound('unknown', currentRunId);
        args.onError?.(
          '连接已断开，Agent 可能仍在运行。请刷新页面查看结果',
          'SSE_DISCONNECTED',
        );
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
          handoffKnownRound('unknown', null);
          args.onError?.(
            streamAccepted
              ? '连接已断开，Agent 可能仍在运行。请刷新页面查看结果'
              : '连接已断开，暂时无法确认请求是否已受理。请刷新页面查看结果',
            'REQUEST_STATUS_UNKNOWN',
          );
        }
      }
    }
  })();

  return {
    abort: () => abortState(abort),
    promise,
    getLatestSequence: () => latestSequence,
    getHandoff: () => handoff,
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
  let interactionRequested = false;
  let streamAccepted = false;
  let continuationStarted = false;
  let preludeError: any | null = null;
  let boundaryControlError: any | null = null;
  let currentRunId: string | null = args.serverRunId;
  let latestSequence = args.lastSequence || 0;

  const recoverAcceptedResume = async (allowOriginalInteraction = false): Promise<boolean> => {
    if (!currentRunId || abort.userAborted) return false;

    const history = await getSessionHistoryWithAbort(abort, args.ownerSessionId);
    const round = history.rounds.find((item: any) => item.round_id === currentRunId);
    if (!round) return false;

    const recoveryIdentity: StreamIdentity = { ...identity, source: 'subscribe' };
    emitHistorySnapshot(args, recoveryIdentity, history.rounds, currentRunId);
    latestSequence = Math.max(latestSequence, round.last_event_sequence || 0);
    if (emitRecoveredTerminal(args, recoveryIdentity, round, currentRunId)) {
      terminalReceived = true;
      return true;
    }

    const interactionEvent = interactionRequestedFromRound(round, currentRunId);
    if (
      interactionEvent
      && (allowOriginalInteraction || round.interrupt?.id !== args.interruptId)
    ) {
      emit(args, recoveryIdentity, interactionEvent, undefined, true);
      interactionRequested = true;
      return true;
    }

    if (round.status !== 'running') return false;

    const subscription = subscribeOnce({
      ownerSessionId: args.ownerSessionId,
      clientRunKey: args.clientRunKey,
      transportEpoch: args.transportEpoch,
      connectionId: args.connectionId,
      source: 'subscribe',
      serverRunId: currentRunId,
      lastSequence: latestSequence,
      durableInteractionObserved: true,
      onEnvelope: (envelope) => {
        if (TERMINAL_EVENT_TYPES.has(envelope.event?.type)) {
          terminalReceived = true;
        } else if (
          envelope.event?.type === 'CUSTOM'
          && envelope.event.name === 'interaction_requested'
        ) {
          interactionRequested = true;
        }
        args.onEnvelope(envelope);
      },
      onError: args.onError,
    }, abort);
    try {
      await subscription.promise;
    } finally {
      latestSequence = Math.max(
        latestSequence,
        subscription.getLatestSequence?.() ?? latestSequence,
      );
    }
    return terminalReceived || interactionRequested;
  };

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
          streamAccepted = true;
          emit(args, identity, { type: 'CUSTOM', name: 'stream_accepted', value: {} });
        },
        (event) => {
          const sequence = eventSequence(event);
          if (sequence !== undefined) {
            latestSequence = Math.max(latestSequence, sequence);
          }
          if (typeof event?.runId === 'string' && event.runId) {
            currentRunId = event.runId;
          } else if (
            event?.type === 'CUSTOM'
            && typeof event.value?.runId === 'string'
            && event.value.runId
          ) {
            currentRunId = event.value.runId;
          }
          if (event?.type === 'CUSTOM' && event.name === 'interaction_requested') {
            interactionRequested = true;
          }
          if (event?.type === 'CUSTOM' && event.name === 'interaction_resolved') {
            continuationStarted = true;
          }
          if (
            (continuationStarted || interactionRequested)
            && isUnsequencedRunError(event)
          ) {
            boundaryControlError = event;
            throw new DurableInteractionRunError(event);
          }
          if (
            event?.type === 'RUN_ERROR'
            && !continuationStarted
            && sequence === undefined
          ) {
            preludeError = event;
            throw new Error(event.message || event.code || 'Resume rejected before continuation');
          }
          handleStreamEvent(event, identity, args, undefined, () => {
            terminalReceived = true;
          });
        },
      );

      if (!terminalReceived && !interactionRequested) {
        throw new Error('Resume stream ended without terminal event');
      }
    } catch (error: any) {
      if (error instanceof UserAbort) return;
      // The durable terminal was already delivered to the reducer. A reader
      // failure after that boundary is only transport noise and must settle.
      if (terminalReceived) return;
      // interaction_requested is itself a durable boundary. A later transport
      // failure must not overwrite the new card with a synthetic RUN_ERROR.
      if (interactionRequested && !boundaryControlError) return;
      let recoveryError: unknown = error;
      const recoverableControlError = preludeError || boundaryControlError;
      if (streamAccepted && currentRunId && !terminalReceived) {
        for (let attempt = 0; attempt < MAX_RETRIES && !abort.userAborted; attempt += 1) {
          try {
            if (await recoverAcceptedResume(Boolean(preludeError))) {
              if (preludeError || (boundaryControlError && !terminalReceived)) {
                args.onError?.(
                  recoverableControlError?.message || '恢复执行失败',
                  recoverableControlError?.code,
                );
              }
              return;
            }
          } catch (candidateError) {
            if (candidateError instanceof UserAbort || abort.userAborted) return;
            recoveryError = candidateError;
          }
          if (attempt < MAX_RETRIES - 1) {
            await delayWithAbort(abort, RETRY_BASE_MS * (attempt + 1));
          }
        }
      }
      if (abort.userAborted) return;
      if (!terminalReceived && !interactionRequested) {
        const message = streamAccepted
          ? '恢复请求已受理，但连接中断后暂时无法确认执行状态。请刷新页面查看结果'
          : (error?.message || '恢复执行失败');
        args.onError?.(
          recoverableControlError?.message || message,
          recoverableControlError?.code
            || (streamAccepted ? 'RESUME_STATUS_UNKNOWN' : 'RESUME_FAILED'),
        );
      }
      throw recoveryError;
    }
  })();

  return {
    abort: () => abortState(abort),
    promise,
    getLatestSequence: () => latestSequence,
  };
}
