import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
} from 'react';

import { apiService } from '../services/api';
import {
  emitWorkspaceInvalidations,
  subscribeWorkspaceMutation,
} from '../services/workspaceEvents';
import {
  RuntimeSubscription,
  startResumeStream,
  startSendStream,
  startSubscribeStream,
} from '../services/chatStreamClient';
import type {
  InterruptDetails,
  RoundData,
} from '../types';
import {
  ChatRuntimeState,
  ChatSessionProjection,
  RUNTIME_HISTORY_SNAPSHOT,
  SendMessageInput,
  StreamEnvelope,
  emptySessionState,
  initialChatRuntimeState,
} from './chatRuntimeTypes';
import { chatRuntimeReducer } from './chatRuntimeReducer';

const INIT_WINDOW_POLL_INTERVAL_MS = 1500;
const TERMINAL_HISTORY_ROUND_STATUSES = new Set([
  'completed',
  'failed',
  'cancelled',
  'max_steps_reached',
]);

interface StreamRegistryEntry {
  currentEpoch: number;
  connectionId: string;
  startedEpochs: Set<number>;
  subscription?: RuntimeSubscription;
  abort?: () => void;
  retryCount?: number;
  retryTimer?: ReturnType<typeof setTimeout>;
}

interface RunOwnershipRegistry {
  serverRoundIdToClientRunKey: Record<string, string>;
  idempotencyKeyToClientRunKey: Record<string, string>;
}

interface LoadHistoryOptions {
  hasActiveSlot?: boolean;
  /** Reads the latest App/runtime slot snapshot; timers must not reuse a captured boolean. */
  isActiveSlotCurrent?: () => boolean;
  throwOnError?: boolean;
}

interface ChatRuntimeContextValue {
  state: ChatRuntimeState;
  getSessionProjection: (sessionId: string) => ChatSessionProjection;
  getExecutingSessionIds: () => Set<string>;
  getActiveSlotSessionIds: () => Set<string>;
  loadSessionHistory: (sessionId: string, options?: LoadHistoryOptions) => Promise<void>;
  sendMessage: (input: SendMessageInput) => Promise<void>;
  resumeRun: (
    sessionId: string,
    interrupt: InterruptDetails,
    answers: Record<string, string>,
    pendingFileDrafts?: import('../types').PendingFileDraftInfo[],
  ) => Promise<void>;
  stopSessionRun: (sessionId: string) => Promise<void>;
  clearSessionView: (sessionId: string) => void;
  clearError: (sessionId: string) => void;
  syncRunningSessions: (runningSessions: Array<{ session_id: string; round_id: string | null }>) => void;
}

const ChatRuntimeContext = createContext<ChatRuntimeContextValue | null>(null);

interface ChatRuntimeProviderProps {
  children: React.ReactNode;
  onTitleUpdated?: () => void;
  onExecutionStart?: (sessionId: string) => void;
  onExecutionEnd?: (sessionId?: string) => void;
}

function randomId(prefix: string): string {
  const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

function findClientRunKeyForRound(
  state: ChatRuntimeState,
  sessionId: string,
  round: Pick<RoundData, 'round_id' | 'idempotency_key'>,
  ownership: RunOwnershipRegistry,
): string | undefined {
  const ownedRunKey = ownership.serverRoundIdToClientRunKey[round.round_id]
    || (
      round.idempotency_key
        ? ownership.idempotencyKeyToClientRunKey[round.idempotency_key]
        : undefined
    );
  if (ownedRunKey) {
    return ownedRunKey;
  }

  const mappedRunKey = state.serverRunIdToClientRunKey[round.round_id];
  if (mappedRunKey && state.runs[mappedRunKey]?.ownerSessionId === sessionId) {
    return mappedRunKey;
  }

  const session = state.sessions[sessionId];
  if (!session) {
    return undefined;
  }

  return session.activeRunKeys.find((runKey) => {
    const run = state.runs[runKey];
    if (!run || run.ownerSessionId !== sessionId) {
      return false;
    }
    return run.serverRunId === round.round_id
      || run.tempRoundId === round.round_id
      || (
        Boolean(round.idempotency_key)
        && run.idempotencyKey === round.idempotency_key
      );
  });
}

export function ChatRuntimeProvider({
  children,
  onTitleUpdated,
  onExecutionStart,
  onExecutionEnd,
}: ChatRuntimeProviderProps) {
  const [state, dispatch] = useReducer(chatRuntimeReducer, initialChatRuntimeState);
  const stateRef = useRef(state);
  stateRef.current = state;

  const streamRegistryRef = useRef<Record<string, StreamRegistryEntry>>({});
  const runOwnershipRef = useRef<RunOwnershipRegistry>({
    serverRoundIdToClientRunKey: {},
    idempotencyKeyToClientRunKey: {},
  });
  const historyRequestSeqRef = useRef<Record<string, number>>({});
  const streamWatermarkRef = useRef<Record<string, number>>({});
  const initPollTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const terminalRunKeysRef = useRef<Set<string>>(new Set());
  const stoppingRunKeysRef = useRef<Set<string>>(new Set());

  useEffect(() => subscribeWorkspaceMutation((detail) => {
    if (!detail.tombstone) return;
    const entryIds = detail.affectedEntryIds || (detail.entryId ? [detail.entryId] : []);
    if (entryIds.length > 0) dispatch({ type: 'WORKSPACE_ENTRIES_DELETED', entryIds });
  }), []);

  const notifyStart = useCallback((sessionId: string) => {
    onExecutionStart?.(sessionId);
  }, [onExecutionStart]);

  const notifyEnd = useCallback((sessionId?: string) => {
    onExecutionEnd?.(sessionId);
  }, [onExecutionEnd]);

  const clearInitPoll = useCallback((sessionId: string) => {
    const timer = initPollTimersRef.current[sessionId];
    if (timer) {
      clearTimeout(timer);
      delete initPollTimersRef.current[sessionId];
    }
  }, []);

  const beginTransport = useCallback((
    clientRunKey: string,
    sessionId: string,
    preserveRetryState: boolean = false,
  ) => {
    terminalRunKeysRef.current.delete(clientRunKey);
    streamWatermarkRef.current[sessionId] = (streamWatermarkRef.current[sessionId] || 0) + 1;
    const existing = streamRegistryRef.current[clientRunKey];
    if (existing?.retryTimer) {
      clearTimeout(existing.retryTimer);
    }
    const nextEpoch = (existing?.currentEpoch || 0) + 1;
    const connectionId = randomId('conn');
    const entry: StreamRegistryEntry = {
      currentEpoch: nextEpoch,
      connectionId,
      startedEpochs: existing?.startedEpochs || new Set<number>(),
      subscription: existing?.subscription,
      abort: existing?.abort,
      retryCount: preserveRetryState ? existing?.retryCount : 0,
      retryTimer: undefined,
    };
    streamRegistryRef.current[clientRunKey] = entry;
    return { transportEpoch: nextEpoch, connectionId };
  }, []);

  const isCurrentTransport = useCallback((
    clientRunKey: string,
    transportEpoch: number,
    connectionId: string,
  ) => {
    const entry = streamRegistryRef.current[clientRunKey];
    return Boolean(
      entry
      && entry.currentEpoch === transportEpoch
      && entry.connectionId === connectionId,
    );
  }, []);

  const invalidateRunTransport = useCallback((clientRunKey: string) => {
    const entry = streamRegistryRef.current[clientRunKey];
    if (!entry) return;
    if (entry.retryTimer) {
      clearTimeout(entry.retryTimer);
      entry.retryTimer = undefined;
    }
    const abort = entry.abort;
    entry.currentEpoch += 1;
    entry.connectionId = randomId('invalidated');
    entry.subscription = undefined;
    entry.abort = undefined;
    entry.retryCount = 0;
    abort?.();
  }, []);

  const releaseRunTransport = useCallback((
    clientRunKey: string,
    expectedEntry?: StreamRegistryEntry,
  ) => {
    const current = streamRegistryRef.current[clientRunKey];
    if (expectedEntry && current !== expectedEntry) return;
    if (current?.retryTimer) {
      clearTimeout(current.retryTimer);
    }
    delete streamRegistryRef.current[clientRunKey];
    stoppingRunKeysRef.current.delete(clientRunKey);
    for (const [roundId, ownedRunKey] of Object.entries(
      runOwnershipRef.current.serverRoundIdToClientRunKey,
    )) {
      if (ownedRunKey === clientRunKey) {
        delete runOwnershipRef.current.serverRoundIdToClientRunKey[roundId];
      }
    }
    for (const [idempotencyKey, ownedRunKey] of Object.entries(
      runOwnershipRef.current.idempotencyKeyToClientRunKey,
    )) {
      if (ownedRunKey === clientRunKey) {
        delete runOwnershipRef.current.idempotencyKeyToClientRunKey[idempotencyKey];
      }
    }
  }, []);

  const clearSettledSubscription = useCallback((
    clientRunKey: string,
    transportEpoch: number,
    connectionId: string,
    subscription: RuntimeSubscription,
    releaseTerminal: boolean = true,
  ) => {
    const entry = streamRegistryRef.current[clientRunKey];
    if (
      !entry
      || entry.currentEpoch !== transportEpoch
      || entry.connectionId !== connectionId
    ) {
      return;
    }

    if (entry.subscription === subscription) {
      entry.subscription = undefined;
    }
    if (entry.abort === subscription.abort) {
      entry.abort = undefined;
    }
    if (releaseTerminal && terminalRunKeysRef.current.has(clientRunKey)) {
      releaseRunTransport(clientRunKey, entry);
    }
  }, [releaseRunTransport]);

  const guardAndDispatch = useCallback((envelope: StreamEnvelope) => {
    const entry = streamRegistryRef.current[envelope.clientRunKey];
    const isTerminalEvent = envelope.event?.type === 'RUN_FINISHED'
      || envelope.event?.type === 'RUN_ERROR';
    const isStoppingTerminal = isTerminalEvent
      && (typeof envelope.sequence === 'number' || envelope.authoritativeRecovery)
      && stoppingRunKeysRef.current.has(envelope.clientRunKey);
    if (
      (
        !entry
        || entry.currentEpoch !== envelope.transportEpoch
        || entry.connectionId !== envelope.connectionId
      )
      && !isStoppingTerminal
    ) {
      console.debug('Dropping stale stream event', envelope.clientRunKey, envelope.event?.type);
      return;
    }

    streamWatermarkRef.current[envelope.ownerSessionId] =
      (streamWatermarkRef.current[envelope.ownerSessionId] || 0) + 1;

    if (envelope.event?.type === 'RUN_STARTED') {
      terminalRunKeysRef.current.delete(envelope.clientRunKey);
      const serverRoundId = envelope.event.runId || envelope.serverRunId;
      if (serverRoundId) {
        const previousRunKey = runOwnershipRef.current
          .serverRoundIdToClientRunKey[serverRoundId];
        if (previousRunKey && previousRunKey !== envelope.clientRunKey) {
          const previousTransport = streamRegistryRef.current[previousRunKey];
          previousTransport?.abort?.();
          releaseRunTransport(previousRunKey, previousTransport);
        }
        runOwnershipRef.current.serverRoundIdToClientRunKey[serverRoundId] =
          envelope.clientRunKey;
      }
    }

    if (envelope.event?.type === 'CUSTOM' && envelope.event.name === 'title_updated') {
      onTitleUpdated?.();
    }
    if (envelope.event?.type === 'CUSTOM' && envelope.event.name === 'workspace_resource_changed') {
      const value = envelope.event.value && typeof envelope.event.value === 'object'
        ? envelope.event.value as Record<string, unknown>
        : {};
      emitWorkspaceInvalidations([{
        operation: typeof value.operation === 'string' ? value.operation : 'updated',
        entryId: typeof value.entry_id === 'string' ? value.entry_id : undefined,
        tombstone: String(value.operation).toUpperCase() === 'DELETED',
        affectedEntryIds: Array.isArray(value.affected_entry_ids)
          ? value.affected_entry_ids.filter((id): id is string => typeof id === 'string')
          : undefined,
        path: typeof value.path === 'string' ? value.path : undefined,
        revision: typeof value.revision === 'number' ? value.revision : undefined,
        versionId: typeof value.current_version_id === 'string' ? value.current_version_id : null,
        origin: 'server',
      }]);
    }
    if (
      (envelope.event?.type === 'CUSTOM'
        && envelope.event.name === 'interaction_resolved')
      || envelope.event?.type === 'RUN_STARTED'
    ) {
      notifyStart(envelope.ownerSessionId);
    }

    dispatch({ type: 'STREAM_EVENT', envelope });

    if (isTerminalEvent) {
      terminalRunKeysRef.current.add(envelope.clientRunKey);
      notifyEnd(envelope.ownerSessionId);
    } else if (
      envelope.event?.type === 'CUSTOM'
      && envelope.event.name === 'interaction_requested'
    ) {
      notifyEnd(envelope.ownerSessionId);
    }
  }, [notifyEnd, notifyStart, onTitleUpdated, releaseRunTransport]);

  const dispatchRunError = useCallback((
    sessionId: string,
    clientRunKey: string,
    transportEpoch: number,
    connectionId: string,
    source: StreamEnvelope['source'],
    message: string,
    code?: string,
  ) => {
    guardAndDispatch({
      ownerSessionId: sessionId,
      clientRunKey,
      transportEpoch,
      connectionId,
      source,
      event: { type: 'RUN_ERROR', message, code },
      receivedAt: Date.now(),
    });
  }, [guardAndDispatch]);

  const dispatchSessionError = useCallback((
    sessionId: string,
    clientRunKey: string,
    transportEpoch: number,
    connectionId: string,
    error: string,
  ) => {
    if (!isCurrentTransport(clientRunKey, transportEpoch, connectionId)) return;
    dispatch({ type: 'SESSION_ERROR', sessionId, error });
  }, [isCurrentTransport]);

  useEffect(() => {
    return () => {
      for (const timer of Object.values(initPollTimersRef.current)) {
        clearTimeout(timer);
      }
      initPollTimersRef.current = {};
      for (const entry of Object.values(streamRegistryRef.current)) {
        if (entry.retryTimer) {
          clearTimeout(entry.retryTimer);
        }
        entry.abort?.();
      }
      streamRegistryRef.current = {};
      streamWatermarkRef.current = {};
      runOwnershipRef.current = {
        serverRoundIdToClientRunKey: {},
        idempotencyKeyToClientRunKey: {},
      };
    };
  }, []);

  const startSubscribeForRound = useCallback((
    sessionId: string,
    roundId: string,
    lastSequence: number = 0,
    existingClientRunKey?: string,
    knownHistoryStatus?: 'running' | 'waiting_interaction',
  ) => {
    const clientRunKey = existingClientRunKey
      || findClientRunKeyForRound(
        stateRef.current,
        sessionId,
        { round_id: roundId },
        runOwnershipRef.current,
      )
      || `run:${roundId}`;
    runOwnershipRef.current.serverRoundIdToClientRunKey[roundId] = clientRunKey;
    const existing = streamRegistryRef.current[clientRunKey];
    if (existing?.subscription || existing?.retryTimer) {
      return;
    }

    const hasRuntimeRun = !!stateRef.current.runs[clientRunKey];
    if (!hasRuntimeRun && !knownHistoryStatus) {
      dispatch({
        type: 'LOCAL_RUN_STARTED',
        sessionId,
        clientRunKey,
        tempRoundId: roundId,
        source: 'subscribe',
      });
      notifyStart(sessionId);
    }

    const { transportEpoch, connectionId } = beginTransport(
      clientRunKey,
      sessionId,
      true,
    );
    const entry = streamRegistryRef.current[clientRunKey];
    if (entry.retryTimer) {
      clearTimeout(entry.retryTimer);
      entry.retryTimer = undefined;
    }
    if (entry.startedEpochs.has(transportEpoch)) {
      return;
    }
    entry.startedEpochs.add(transportEpoch);

    const subscription = startSubscribeStream({
      ownerSessionId: sessionId,
      clientRunKey,
      serverRunId: roundId,
      transportEpoch,
      connectionId,
      source: 'subscribe',
      lastSequence,
      durableInteractionObserved: knownHistoryStatus === 'waiting_interaction'
        || typeof stateRef.current.runs[clientRunKey]?.lastInteractionSequence === 'number',
      onEnvelope: guardAndDispatch,
      onError: (message, code) => {
        if (!code) return;
        dispatchSessionError(
          sessionId,
          clientRunKey,
          transportEpoch,
          connectionId,
          message,
        );
      },
    });
    entry.subscription = subscription;
    entry.abort = subscription.abort;
    subscription.promise
      .then(() => {
        const current = streamRegistryRef.current[clientRunKey];
        if (current?.connectionId === connectionId) {
          current.retryCount = 0;
        }
      })
      .catch(() => {
        const current = streamRegistryRef.current[clientRunKey];
        if (!current || current.connectionId !== connectionId) return;
        const run = stateRef.current.runs[clientRunKey];
        if (!run || run.status === 'finished' || run.status === 'error' || run.status === 'cancelled') {
          return;
        }
        const retryCount = (current.retryCount || 0) + 1;
        if (retryCount > 3) {
          dispatch({
            type: 'SESSION_ERROR',
            sessionId,
            error: '订阅连接已断开，Agent 可能仍在运行。请刷新页面查看结果',
          });
          return;
        }
        current.retryCount = retryCount;
        current.retryTimer = setTimeout(() => {
          const latest = streamRegistryRef.current[clientRunKey];
          if (!latest || latest.connectionId !== connectionId) return;
          latest.retryTimer = undefined;
          const latestSequence = stateRef.current.runs[clientRunKey]?.lastSequence ?? lastSequence;
          startSubscribeForRound(sessionId, roundId, latestSequence);
        }, retryCount * 1000);
      })
      .finally(() => {
        clearSettledSubscription(
          clientRunKey,
          transportEpoch,
          connectionId,
          subscription,
        );
      });
  }, [
    beginTransport,
    clearSettledSubscription,
    dispatchSessionError,
    guardAndDispatch,
    notifyStart,
  ]);

  const loadSessionHistory = useCallback(async (sessionId: string, options?: LoadHistoryOptions) => {
    if (!sessionId) return;
    const isActiveSlotCurrent = () => (
      options?.isActiveSlotCurrent?.() ?? Boolean(options?.hasActiveSlot)
    );
    if (options && !isActiveSlotCurrent()) {
      clearInitPoll(sessionId);
    }
    const requestId = (historyRequestSeqRef.current[sessionId] || 0) + 1;
    historyRequestSeqRef.current[sessionId] = requestId;
    const streamWatermarkAtStart = streamWatermarkRef.current[sessionId] || 0;
    const currentSession = stateRef.current.sessions[sessionId];
    // 已有本地消息或运行槽时在后台校准历史，避免把乐观首轮替换成整页同步动画。
    if (!currentSession || (
      currentSession.rounds.length === 0
      && currentSession.activeRunKeys.length === 0
    )) {
      dispatch({ type: 'SESSION_LOADING', sessionId, loading: true });
    }
    try {
      const response = await apiService.getSessionHistoryV2(sessionId);
      if (historyRequestSeqRef.current[sessionId] !== requestId) {
        return;
      }
      if ((streamWatermarkRef.current[sessionId] || 0) !== streamWatermarkAtStart) {
        dispatch({ type: 'SESSION_LOADING', sessionId, loading: false });
        return;
      }
      for (const round of response.rounds) {
        const historyRunKey = findClientRunKeyForRound(
          stateRef.current,
          sessionId,
          round,
          runOwnershipRef.current,
        ) || `run:${round.round_id}`;
        if (round.status === 'running' || round.status === 'waiting_interaction') {
          terminalRunKeysRef.current.delete(historyRunKey);
          continue;
        }
        if (TERMINAL_HISTORY_ROUND_STATUSES.has(round.status)) {
          const terminalTransport = streamRegistryRef.current[historyRunKey];
          const currentRun = stateRef.current.runs[historyRunKey];
          const runtimeAlreadyTerminal = currentRun
            && (currentRun.status === 'finished'
              || currentRun.status === 'error'
              || currentRun.status === 'cancelled');
          const terminalSnapshotIsCurrent = !currentRun
            || runtimeAlreadyTerminal
            || (round.last_event_sequence || 0) >= currentRun.lastSequence;
          if (terminalTransport && terminalSnapshotIsCurrent) {
            terminalRunKeysRef.current.add(historyRunKey);
            terminalTransport.abort?.();
          }
          if (!terminalTransport || terminalSnapshotIsCurrent) {
            releaseRunTransport(historyRunKey);
          }
        }
      }
      dispatch({
        type: 'HISTORY_LOADED',
        sessionId,
        rounds: response.rounds.map((round) => ({
          ...round,
          user_attachments: (round.user_attachments || []).map((attachment) => ({
            ...attachment,
            session_id: attachment.session_id || sessionId,
          })),
        })),
        loadedAt: Date.now(),
        source: 'history',
      });

      const liveRound = response.rounds.find((round) => round.status === 'running')
        || response.rounds.find((round) => round.status === 'waiting_interaction');
      if (liveRound) {
        const liveStatus = liveRound.status === 'running'
          ? 'running'
          : 'waiting_interaction';
        const existingClientRunKey = findClientRunKeyForRound(
          stateRef.current,
          sessionId,
          liveRound,
          runOwnershipRef.current,
        );
        dispatch({ type: 'LOCAL_INIT_SLOT_CLEARED', sessionId });
        clearInitPoll(sessionId);
        startSubscribeForRound(
          sessionId,
          liveRound.round_id,
          liveRound.last_event_sequence || 0,
          existingClientRunKey,
          liveStatus,
        );
        if (liveStatus === 'running') {
          notifyStart(sessionId);
        } else {
          notifyEnd(sessionId);
        }
        return;
      }

      if (isActiveSlotCurrent()) {
        notifyStart(sessionId);
        const clientRunKey = `init:${sessionId}`;
        dispatch({
          type: 'LOCAL_RUN_STARTED',
          sessionId,
          clientRunKey,
          tempRoundId: clientRunKey,
          source: 'init',
        });
        if (!initPollTimersRef.current[sessionId]) {
          initPollTimersRef.current[sessionId] = setTimeout(() => {
            delete initPollTimersRef.current[sessionId];
            void loadSessionHistory(sessionId, options);
          }, INIT_WINDOW_POLL_INTERVAL_MS);
        }
        return;
      }

      dispatch({ type: 'LOCAL_INIT_SLOT_CLEARED', sessionId });
      clearInitPoll(sessionId);
      notifyEnd(sessionId);
    } catch (error) {
      if (historyRequestSeqRef.current[sessionId] !== requestId) {
        return;
      }
      console.error('Failed to load history:', error);
      dispatch({ type: 'SESSION_ERROR', sessionId, error: '加载历史记录失败' });
      if (options?.throwOnError) {
        throw error;
      }
    }
  }, [clearInitPoll, notifyEnd, notifyStart, releaseRunTransport, startSubscribeForRound]);

  const sendMessage = useCallback(async ({
    sessionId,
    displayMessage,
    content,
    attachments = [],
    preferredSkillKeys = [],
    preferredMcpConnections = [],
    reasoning,
    pendingFileDrafts = [],
    onStreamAccepted,
    onRejectedBeforeAccept,
  }: SendMessageInput) => {
    const clientRunKey = randomId('run');
    const tempRoundId = `temp-${Date.now()}`;
    const idempotencyKey = randomId('idem');
    const round: RoundData = {
      round_id: tempRoundId,
      idempotency_key: idempotencyKey,
      user_message: displayMessage,
      user_attachments: [...attachments],
      preferred_skills: preferredSkillKeys.map((key) => ({
        key,
        display_name: key,
      })),
      preferred_mcp_connections: preferredMcpConnections.map(
        (connection) => ({ ...connection }),
      ),
      thinking_mode: reasoning?.mode,
      reasoning_effort: reasoning?.effort,
      final_response: '',
      steps: [],
      step_count: 0,
      status: 'running',
      created_at: new Date().toISOString(),
    };

    runOwnershipRef.current.idempotencyKeyToClientRunKey[idempotencyKey] = clientRunKey;
    dispatch({
      type: 'LOCAL_RUN_STARTED',
      sessionId,
      clientRunKey,
      tempRoundId,
      idempotencyKey,
      source: 'direct',
      round,
    });

    const { transportEpoch, connectionId } = beginTransport(clientRunKey, sessionId);
    const entry = streamRegistryRef.current[clientRunKey];
    if (entry.startedEpochs.has(transportEpoch)) {
      return;
    }
    entry.startedEpochs.add(transportEpoch);

    let streamAccepted = false;
    let waitingRoundId: string | undefined;
    let controlConflictHandled = false;
    let latestSequence = 0;
    let subscription: RuntimeSubscription | undefined;
    try {
      subscription = startSendStream({
        ownerSessionId: sessionId,
        clientRunKey,
        transportEpoch,
        connectionId,
        source: 'direct',
        content,
        idempotencyKey,
        preferredSkillKeys,
        preferredMcpServerIds: preferredMcpConnections.map(
          (connection) => connection.server_id,
        ),
      reasoning,
        pendingFileDrafts,
        onRejectedBeforeAccept: () => {
          if (isCurrentTransport(clientRunKey, transportEpoch, connectionId)) {
            controlConflictHandled = true;
            dispatch({
              type: 'LOCAL_CONTROL_CONFLICT',
              sessionId,
              clientRunKey,
            });
            onRejectedBeforeAccept?.();
          }
        },
        onControlConflict: (_message, _code, serverRunId) => {
          if (!isCurrentTransport(clientRunKey, transportEpoch, connectionId)) return;
          controlConflictHandled = true;
          waitingRoundId = serverRunId;
          if (serverRunId) {
            runOwnershipRef.current.serverRoundIdToClientRunKey[serverRunId] = clientRunKey;
          }
          dispatch({
            type: 'LOCAL_CONTROL_CONFLICT',
            sessionId,
            clientRunKey,
            serverRunId,
          });
          onRejectedBeforeAccept?.();
          notifyEnd(sessionId);
        },
        onEnvelope: (envelope) => {
          if (typeof envelope.sequence === 'number') {
            latestSequence = Math.max(latestSequence, envelope.sequence);
          }
          if (
            !streamAccepted
            && envelope.event?.type === 'CUSTOM'
            && envelope.event.name === 'stream_accepted'
          ) {
            streamAccepted = true;
            onStreamAccepted?.();
          }
          if (
            envelope.event?.type === 'CUSTOM'
            && envelope.event.name === 'interaction_requested'
          ) {
            waitingRoundId = envelope.event.value?.runId
              || envelope.serverRunId;
          }
          guardAndDispatch(envelope);
        },
        onError: (message) => {
          dispatchSessionError(
            sessionId,
            clientRunKey,
            transportEpoch,
            connectionId,
            message,
          );
        },
      });
      entry.subscription = subscription;
      entry.abort = subscription.abort;
      await subscription.promise;
    } catch (error: any) {
      const isNonTerminalTransportError = error?.code === 'SSE_NON_TERMINAL_END';
      if (
        !streamAccepted
        && !isNonTerminalTransportError
        && isCurrentTransport(clientRunKey, transportEpoch, connectionId)
      ) {
        onRejectedBeforeAccept?.();
      }
      console.error('Failed to send message:', error);
      if (isNonTerminalTransportError) {
        dispatchSessionError(
          sessionId,
          clientRunKey,
          transportEpoch,
          connectionId,
          error?.message || '连接已断开，Agent 可能仍在运行',
        );
      } else {
        dispatchRunError(
          sessionId,
          clientRunKey,
          transportEpoch,
          connectionId,
          'direct',
          error?.message || '发送失败',
        );
      }
    } finally {
      const handoff = subscription?.getHandoff?.();
      if (subscription) {
        clearSettledSubscription(
          clientRunKey,
          transportEpoch,
          connectionId,
          subscription,
        );
      }
      const handoffRoundId = waitingRoundId || handoff?.serverRunId;
      if (
        handoffRoundId
        && isCurrentTransport(clientRunKey, transportEpoch, connectionId)
      ) {
        const handoffStatus = waitingRoundId
          ? 'waiting_interaction'
          : handoff?.status === 'running' || handoff?.status === 'waiting_interaction'
            ? handoff.status
            : undefined;
        startSubscribeForRound(
          sessionId,
          handoffRoundId,
          Math.max(latestSequence, handoff?.lastSequence || 0),
          clientRunKey,
          handoffStatus,
        );
      } else if (
        controlConflictHandled
        && isCurrentTransport(clientRunKey, transportEpoch, connectionId)
      ) {
        releaseRunTransport(clientRunKey);
      }
    }
  }, [
    beginTransport,
    clearSettledSubscription,
    dispatchRunError,
    dispatchSessionError,
    guardAndDispatch,
    isCurrentTransport,
    notifyEnd,
    releaseRunTransport,
    startSubscribeForRound,
  ]);

  const resumeRun = useCallback(async (
    sessionId: string,
    interrupt: InterruptDetails,
    answers: Record<string, string>,
    pendingFileDrafts: import('../types').PendingFileDraftInfo[] = [],
  ) => {
    if (!interrupt.id) return;
    const waitingRound = stateRef.current.sessions[sessionId]?.rounds.find(
      (round) => (
        round.status === 'waiting_interaction'
        && round.interrupt?.id === interrupt.id
      ),
    );
    if (!waitingRound) {
      dispatch({
        type: 'SESSION_ERROR',
        sessionId,
        error: '待处理交互已失效，请刷新会话后重试',
      });
      return;
    }
    const serverRunId = waitingRound.round_id;
    const existingRunKey = findClientRunKeyForRound(
      stateRef.current,
      sessionId,
      waitingRound,
      runOwnershipRef.current,
    );
    const clientRunKey = existingRunKey || `run:${serverRunId}`;
    const tempRoundId = serverRunId;

    const previousTransport = streamRegistryRef.current[clientRunKey];
    if (previousTransport?.retryTimer) {
      clearTimeout(previousTransport.retryTimer);
      previousTransport.retryTimer = undefined;
    }
    if (previousTransport) {
      previousTransport.retryCount = 0;
    }
    previousTransport?.abort?.();

    dispatch({
      type: 'LOCAL_RUN_STARTED',
      sessionId,
      clientRunKey,
      tempRoundId,
      source: 'resume',
    });

    const { transportEpoch, connectionId } = beginTransport(clientRunKey, sessionId);
    const entry = streamRegistryRef.current[clientRunKey];
    if (entry.startedEpochs.has(transportEpoch)) {
      return;
    }
    entry.startedEpochs.add(transportEpoch);

    let streamAccepted = false;
    let terminalEnvelopeReceived = false;
    let continuationStarted = false;
    let interactionRequestedAfterResume = false;
    let preludeError: { message?: string; code?: string } | null = null;
    let latestResumeSequence = existingRunKey
      ? stateRef.current.runs[existingRunKey]?.lastSequence || 0
      : 0;
    let subscription: RuntimeSubscription | undefined;
    try {
      subscription = startResumeStream({
        ownerSessionId: sessionId,
        clientRunKey,
        transportEpoch,
        connectionId,
        source: 'resume',
        interruptId: interrupt.id,
        answers,
        pendingFileDrafts,
        serverRunId,
        lastSequence: existingRunKey
          ? stateRef.current.runs[existingRunKey]?.lastSequence
          : undefined,
        onEnvelope: (envelope) => {
          if (typeof envelope.sequence === 'number') {
            latestResumeSequence = Math.max(latestResumeSequence, envelope.sequence);
          }
          if (
            envelope.event?.type === 'CUSTOM'
            && envelope.event.name === 'stream_accepted'
          ) {
            streamAccepted = true;
          }
          if (
            envelope.event?.type === 'CUSTOM'
            && envelope.event.name === 'interaction_resolved'
          ) {
            continuationStarted = true;
          }
          if (
            envelope.event?.type === RUNTIME_HISTORY_SNAPSHOT
            && Array.isArray(envelope.event.rounds)
            && envelope.event.rounds.some((round: RoundData) => (
              round.round_id === serverRunId && round.status === 'running'
            ))
          ) {
            // The resume stream may disconnect after the server commits
            // interaction_resolved but before that event reaches this tab.
            // A history projection of the same Round as running is the same
            // irreversible continuation boundary and must forbid restoring the
            // captured pre-resume question card.
            continuationStarted = true;
          }
          if (
            envelope.event?.type === 'CUSTOM'
            && envelope.event.name === 'interaction_requested'
          ) {
            interactionRequestedAfterResume = true;
          }
          if (envelope.event?.type === 'RUN_FINISHED') {
            terminalEnvelopeReceived = true;
          }
          if (envelope.event?.type === 'RUN_ERROR') {
            const isOriginalResumeControlError = !continuationStarted
              && envelope.source === 'resume'
              && !envelope.authoritativeRecovery
              && typeof envelope.sequence !== 'number';
            if (isOriginalResumeControlError) {
              preludeError = envelope.event;
              return;
            }
            terminalEnvelopeReceived = true;
          }
          guardAndDispatch(envelope);
        },
        onError: (message) => {
          dispatchSessionError(
            sessionId,
            clientRunKey,
            transportEpoch,
            connectionId,
            message,
          );
        },
      });
      entry.subscription = subscription;
      entry.abort = subscription.abort;
      await subscription.promise;
      const capturedPreludeError = preludeError as {
        message?: string;
        code?: string;
      } | null;
      if (
        capturedPreludeError
        && isCurrentTransport(clientRunKey, transportEpoch, connectionId)
      ) {
        dispatchSessionError(
          sessionId,
          clientRunKey,
          transportEpoch,
          connectionId,
          capturedPreludeError.message || '恢复执行失败',
        );
        clearSettledSubscription(
          clientRunKey,
          transportEpoch,
          connectionId,
          subscription,
          false,
        );
        try {
          await loadSessionHistory(sessionId, { throwOnError: true });
        } catch {
          if (
            !terminalEnvelopeReceived
            && isCurrentTransport(clientRunKey, transportEpoch, connectionId)
          ) {
            const continuationCursor = Math.max(
              latestResumeSequence,
              waitingRound.last_event_sequence || 0,
            );
            if (!continuationStarted) {
              dispatch({
                type: 'RESTORE_PENDING_INTERACTION',
                sessionId,
                clientRunKey,
                round: waitingRound,
                interrupt,
              });
            }
            startSubscribeForRound(
              sessionId,
              serverRunId,
              continuationCursor,
              clientRunKey,
              continuationStarted ? 'running' : 'waiting_interaction',
            );
          }
        }
      }
    } catch (error: any) {
      if (subscription) {
        clearSettledSubscription(
          clientRunKey,
          transportEpoch,
          connectionId,
          subscription,
          false,
        );
      }
      // A durable terminal already owns the Round. A later reader rejection is
      // transport noise and must not trigger history rollback or card restore.
      if (terminalEnvelopeReceived) {
        return;
      }
      console.error('Failed to resume:', error);
      if (!interactionRequestedAfterResume) {
        dispatchSessionError(
          sessionId,
          clientRunKey,
          transportEpoch,
          connectionId,
          error?.message || '恢复执行失败',
        );
      }
      if (isCurrentTransport(clientRunKey, transportEpoch, connectionId)) {
        if (interactionRequestedAfterResume) {
          dispatchSessionError(
            sessionId,
            clientRunKey,
            transportEpoch,
            connectionId,
            error?.message || '交互已更新，但订阅连接已断开',
          );
        } else if (!streamAccepted && !continuationStarted) {
          terminalRunKeysRef.current.delete(clientRunKey);
          dispatch({
            type: 'RESTORE_PENDING_INTERACTION',
            sessionId,
            clientRunKey,
            round: waitingRound,
            interrupt,
          });
          releaseRunTransport(clientRunKey);
          startSubscribeForRound(
            sessionId,
            serverRunId,
            Math.max(latestResumeSequence, waitingRound.last_event_sequence || 0),
            clientRunKey,
            'waiting_interaction',
          );
        } else {
          try {
            await loadSessionHistory(sessionId, { throwOnError: true });
          } catch {
            if (isCurrentTransport(clientRunKey, transportEpoch, connectionId)) {
              const continuationCursor = Math.max(
                latestResumeSequence,
                waitingRound.last_event_sequence || 0,
              );
              if (!continuationStarted) {
                dispatch({
                  type: 'RESTORE_PENDING_INTERACTION',
                  sessionId,
                  clientRunKey,
                  round: waitingRound,
                  interrupt,
                });
              }
              startSubscribeForRound(
                sessionId,
                serverRunId,
                continuationCursor,
                clientRunKey,
                continuationStarted ? 'running' : 'waiting_interaction',
              );
            }
          }
        }
      }
    } finally {
      if (subscription) {
        clearSettledSubscription(
          clientRunKey,
          transportEpoch,
          connectionId,
          subscription,
        );
      }
      if (
        interactionRequestedAfterResume
        && !terminalEnvelopeReceived
        && isCurrentTransport(clientRunKey, transportEpoch, connectionId)
      ) {
        startSubscribeForRound(
          sessionId,
          serverRunId,
          latestResumeSequence,
          clientRunKey,
          'waiting_interaction',
        );
      }
    }
  }, [
    beginTransport,
    clearSettledSubscription,
    dispatchSessionError,
    guardAndDispatch,
    isCurrentTransport,
    loadSessionHistory,
    releaseRunTransport,
    startSubscribeForRound,
  ]);

  const stopSessionRun = useCallback(async (sessionId: string) => {
    const session = stateRef.current.sessions[sessionId];
    if (!session) return;
    const waitingRound = session.rounds.find(
      (round) => round.status === 'waiting_interaction' && round.interrupt?.id,
    );
    const waitingRunKey = waitingRound
      ? stateRef.current.serverRunIdToClientRunKey[waitingRound.round_id]
      : undefined;
    const stoppedRunKeys = session.activeRunKeys.length > 0
      ? [...session.activeRunKeys]
      : waitingRunKey
        ? [waitingRunKey]
        : [];
    if (stoppedRunKeys.length === 0) return;
    for (const runKey of stoppedRunKeys) {
      stoppingRunKeysRef.current.add(runKey);
      invalidateRunTransport(runKey);
    }
    dispatch({
      type: 'LOCAL_CANCELLED',
      sessionId,
      clientRunKey: session.activeRunKeys.length === 0 ? waitingRunKey : undefined,
    });
    notifyEnd(sessionId);
    try {
      await apiService.abortChat(sessionId);
      for (const runKey of stoppedRunKeys) {
        releaseRunTransport(runKey);
      }
    } catch (error) {
      const statusCode = (error as { response?: { status?: number } })?.response?.status;
      if (statusCode === 409) {
        for (const runKey of stoppedRunKeys) {
          releaseRunTransport(runKey);
        }
        return;
      }
      if (stoppedRunKeys.some((runKey) => terminalRunKeysRef.current.has(runKey))) {
        for (const runKey of stoppedRunKeys) {
          releaseRunTransport(runKey);
        }
        return;
      }
      console.warn('Abort request failed, reloading running state:', error);
      await loadSessionHistory(sessionId);
      for (const runKey of stoppedRunKeys) {
        stoppingRunKeysRef.current.delete(runKey);
      }
      dispatch({ type: 'SESSION_ERROR', sessionId, error: '停止请求失败，后端任务可能仍在运行' });
    }
  }, [invalidateRunTransport, loadSessionHistory, notifyEnd, releaseRunTransport]);

  const clearSessionView = useCallback((sessionId: string) => {
    clearInitPoll(sessionId);
    dispatch({ type: 'CLEAR_SESSION_VIEW', sessionId });
  }, [clearInitPoll]);

  const clearError = useCallback((sessionId: string) => {
    dispatch({ type: 'CLEAR_ERROR', sessionId });
  }, []);

  const syncRunningSessions = useCallback((
    runningSessions: Array<{ session_id: string; round_id: string | null }>,
  ) => {
    dispatch({
      type: 'RUNNING_SESSIONS_SNAPSHOT',
      runningSessions,
      receivedAt: Date.now(),
    });
  }, []);

  const getSessionProjection = useCallback((sessionId: string): ChatSessionProjection => {
    const session = stateRef.current.sessions[sessionId] || emptySessionState();
    const activeRuns = session.activeRunKeys
      .map((key) => stateRef.current.runs[key])
      .filter(Boolean);
    return {
      ...session,
      sending: activeRuns.some((run) => run.status === 'starting' || run.status === 'streaming'),
      resuming: activeRuns.some((run) => run.source === 'resume' && (run.status === 'starting' || run.status === 'streaming')),
    };
  }, []);

  const getExecutingSessionIds = useCallback(() => {
    const ids = new Set<string>();
    for (const [sessionId, session] of Object.entries(stateRef.current.sessions)) {
      if (session.activeRunKeys.some((key) => {
        const run = stateRef.current.runs[key];
        return run?.status === 'streaming';
      })) {
        ids.add(sessionId);
      }
    }
    return ids;
  }, []);

  const getActiveSlotSessionIds = useCallback(() => {
    const ids = new Set<string>();
    for (const [sessionId, session] of Object.entries(stateRef.current.sessions)) {
      if (session.activeRunKeys.some((key) => {
        const run = stateRef.current.runs[key];
        return run && (run.status === 'starting' || run.status === 'streaming');
      })) {
        ids.add(sessionId);
      }
    }
    return ids;
  }, []);

  const value = useMemo<ChatRuntimeContextValue>(() => ({
    state,
    getSessionProjection,
    getExecutingSessionIds,
    getActiveSlotSessionIds,
    loadSessionHistory,
    sendMessage,
    resumeRun,
    stopSessionRun,
    clearSessionView,
    clearError,
    syncRunningSessions,
  }), [
    state,
    getSessionProjection,
    getExecutingSessionIds,
    getActiveSlotSessionIds,
    loadSessionHistory,
    sendMessage,
    resumeRun,
    stopSessionRun,
    clearSessionView,
    clearError,
    syncRunningSessions,
  ]);

  return (
    <ChatRuntimeContext.Provider value={value}>
      {children}
    </ChatRuntimeContext.Provider>
  );
}

export function useChatRuntime(): ChatRuntimeContextValue {
  const value = useContext(ChatRuntimeContext);
  if (!value) {
    throw new Error('useChatRuntime must be used within ChatRuntimeProvider');
  }
  return value;
}

export function useChatRuntimeOptional(): ChatRuntimeContextValue | null {
  return useContext(ChatRuntimeContext);
}
