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
  SendMessageInput,
  StreamEnvelope,
  emptySessionState,
  initialChatRuntimeState,
} from './chatRuntimeTypes';
import { chatRuntimeReducer } from './chatRuntimeReducer';

const INIT_WINDOW_POLL_INTERVAL_MS = 1500;

interface StreamRegistryEntry {
  currentEpoch: number;
  connectionId: string;
  startedEpochs: Set<number>;
  subscription?: RuntimeSubscription;
  abort?: () => void;
  retryCount?: number;
  retryTimer?: ReturnType<typeof setTimeout>;
}

interface LoadHistoryOptions {
  hasActiveSlot?: boolean;
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
  const historyRequestSeqRef = useRef<Record<string, number>>({});
  const initPollTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const terminalRunKeysRef = useRef<Set<string>>(new Set());

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

  const beginTransport = useCallback((clientRunKey: string) => {
    const existing = streamRegistryRef.current[clientRunKey];
    const nextEpoch = (existing?.currentEpoch || 0) + 1;
    const connectionId = randomId('conn');
    const entry: StreamRegistryEntry = {
      currentEpoch: nextEpoch,
      connectionId,
      startedEpochs: existing?.startedEpochs || new Set<number>(),
      subscription: existing?.subscription,
      abort: existing?.abort,
      retryCount: existing?.retryCount,
      retryTimer: existing?.retryTimer,
    };
    streamRegistryRef.current[clientRunKey] = entry;
    return { transportEpoch: nextEpoch, connectionId };
  }, []);

  const guardAndDispatch = useCallback((envelope: StreamEnvelope) => {
    const entry = streamRegistryRef.current[envelope.clientRunKey];
    if (
      !entry
      || entry.currentEpoch !== envelope.transportEpoch
      || entry.connectionId !== envelope.connectionId
    ) {
      console.debug('Dropping stale stream event', envelope.clientRunKey, envelope.event?.type);
      return;
    }

    if (envelope.event?.type === 'CUSTOM' && envelope.event.name === 'title_updated') {
      onTitleUpdated?.();
    }
    if (
      (envelope.event?.type === 'CUSTOM' && envelope.event.name === 'stream_accepted')
      || envelope.event?.type === 'RUN_STARTED'
    ) {
      notifyStart(envelope.ownerSessionId);
    }

    dispatch({ type: 'STREAM_EVENT', envelope });

    if (envelope.event?.type === 'RUN_FINISHED' || envelope.event?.type === 'RUN_ERROR') {
      terminalRunKeysRef.current.add(envelope.clientRunKey);
      notifyEnd(envelope.ownerSessionId);
    }
  }, [notifyEnd, notifyStart, onTitleUpdated]);

  const dispatchRunError = useCallback((
    sessionId: string,
    clientRunKey: string,
    transportEpoch: number,
    connectionId: string,
    source: StreamEnvelope['source'],
    message: string,
    code?: string,
  ) => {
    dispatch({
      type: 'STREAM_EVENT',
      envelope: {
        ownerSessionId: sessionId,
        clientRunKey,
        transportEpoch,
        connectionId,
        source,
        event: { type: 'RUN_ERROR', message, code },
        receivedAt: Date.now(),
      },
    });
  }, []);

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
    };
  }, []);

  const startSubscribeForRound = useCallback((
    sessionId: string,
    roundId: string,
    lastSequence: number = 0,
  ) => {
    const clientRunKey = `run:${roundId}`;
    const existing = streamRegistryRef.current[clientRunKey];
    if (existing?.subscription || existing?.retryTimer) {
      return;
    }

    const hasRuntimeRun = !!stateRef.current.runs[clientRunKey];
    if (!hasRuntimeRun) {
      dispatch({
        type: 'LOCAL_RUN_STARTED',
        sessionId,
        clientRunKey,
        tempRoundId: roundId,
        source: 'subscribe',
      });
      notifyStart(sessionId);
    }

    const { transportEpoch, connectionId } = beginTransport(clientRunKey);
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
      onEnvelope: guardAndDispatch,
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
        current.subscription = undefined;
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
        const current = streamRegistryRef.current[clientRunKey];
        if (current?.connectionId === connectionId) {
          current.subscription = undefined;
        }
      });
  }, [beginTransport, guardAndDispatch, notifyStart]);

  const loadSessionHistory = useCallback(async (sessionId: string, options?: LoadHistoryOptions) => {
    if (!sessionId) return;
    const requestId = (historyRequestSeqRef.current[sessionId] || 0) + 1;
    historyRequestSeqRef.current[sessionId] = requestId;
    dispatch({ type: 'SESSION_LOADING', sessionId, loading: true });
    try {
      const response = await apiService.getSessionHistoryV2(sessionId);
      if (historyRequestSeqRef.current[sessionId] !== requestId) {
        return;
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

      const runningRound = response.rounds.find((round) => round.status === 'running');
      if (runningRound) {
        clearInitPoll(sessionId);
        startSubscribeForRound(sessionId, runningRound.round_id, runningRound.last_event_sequence || 0);
        return;
      }

      if (options?.hasActiveSlot) {
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

      clearInitPoll(sessionId);
      notifyEnd(sessionId);
    } catch (error) {
      if (historyRequestSeqRef.current[sessionId] !== requestId) {
        return;
      }
      console.error('Failed to load history:', error);
      dispatch({ type: 'SESSION_ERROR', sessionId, error: '加载历史记录失败' });
    }
  }, [clearInitPoll, notifyEnd, notifyStart, startSubscribeForRound]);

  const sendMessage = useCallback(async ({
    sessionId,
    displayMessage,
    content,
    attachments = [],
  }: SendMessageInput) => {
    const clientRunKey = randomId('run');
    const tempRoundId = `temp-${Date.now()}`;
    const idempotencyKey = randomId('idem');
    const round: RoundData = {
      round_id: tempRoundId,
      idempotency_key: idempotencyKey,
      user_message: displayMessage,
      user_attachments: [...attachments],
      final_response: '',
      steps: [],
      step_count: 0,
      status: 'running',
      created_at: new Date().toISOString(),
    };

    dispatch({
      type: 'LOCAL_RUN_STARTED',
      sessionId,
      clientRunKey,
      tempRoundId,
      idempotencyKey,
      source: 'direct',
      round,
    });

    const { transportEpoch, connectionId } = beginTransport(clientRunKey);
    const entry = streamRegistryRef.current[clientRunKey];
    if (entry.startedEpochs.has(transportEpoch)) {
      return;
    }
    entry.startedEpochs.add(transportEpoch);

    try {
      const subscription = startSendStream({
        ownerSessionId: sessionId,
        clientRunKey,
        transportEpoch,
        connectionId,
        source: 'direct',
        content,
        idempotencyKey,
        onEnvelope: guardAndDispatch,
        onError: (message) => {
          dispatch({ type: 'SESSION_ERROR', sessionId, error: message });
        },
      });
      entry.subscription = subscription;
      entry.abort = subscription.abort;
      await subscription.promise;
    } catch (error: any) {
      console.error('Failed to send message:', error);
      dispatchRunError(
        sessionId,
        clientRunKey,
        transportEpoch,
        connectionId,
        'direct',
        error?.message || '发送失败',
      );
      notifyEnd(sessionId);
    }
  }, [beginTransport, dispatchRunError, guardAndDispatch, notifyEnd]);

  const resumeRun = useCallback(async (
    sessionId: string,
    interrupt: InterruptDetails,
    answers: Record<string, string>,
  ) => {
    if (!interrupt.id) return;
    const clientRunKey = randomId('resume');
    const tempRoundId = `resume-temp-${Date.now()}`;
    const resumeEntries = Object.entries(answers);
    // A tool-approval resolution is a control decision, not user chat input, so
    // it must not render as a user bubble. ask_user answers remain genuine user
    // messages and are still shown as Q/A text.
    const isToolApproval = interrupt.reason === 'human_approval'
      || interrupt.payload?.kind === 'tool_approval';
    const userMessage = isToolApproval
      ? `Tool approval: ${answers.approval}`
      : resumeEntries.length > 0
        ? resumeEntries.map(([question, answer], index) => {
            const safeQuestion = question?.trim() || '(Untitled question)';
            const safeAnswer = answer?.trim() || '[No preference]';
            return `${index > 0 ? '\n\n' : ''}Q: ${safeQuestion}\nA: ${safeAnswer}`;
          }).join('')
        : 'Q: (No question)\nA: [No preference]';
    const round: RoundData = {
      round_id: tempRoundId,
      control_kind: isToolApproval ? 'tool_approval' : undefined,
      user_message: userMessage,
      user_attachments: [],
      final_response: '',
      steps: [],
      step_count: 0,
      status: 'running',
      created_at: new Date().toISOString(),
    };

    dispatch({
      type: 'LOCAL_RUN_STARTED',
      sessionId,
      clientRunKey,
      tempRoundId,
      source: 'resume',
      round,
    });
    notifyStart(sessionId);

    const { transportEpoch, connectionId } = beginTransport(clientRunKey);
    const entry = streamRegistryRef.current[clientRunKey];
    if (entry.startedEpochs.has(transportEpoch)) {
      return;
    }
    entry.startedEpochs.add(transportEpoch);

    try {
      const subscription = startResumeStream({
        ownerSessionId: sessionId,
        clientRunKey,
        transportEpoch,
        connectionId,
        source: 'resume',
        interruptId: interrupt.id,
        answers,
        onEnvelope: guardAndDispatch,
        onError: (message) => {
          dispatch({ type: 'SESSION_ERROR', sessionId, error: message });
        },
      });
      entry.subscription = subscription;
      entry.abort = subscription.abort;
      await subscription.promise;
    } catch (error: any) {
      console.error('Failed to resume:', error);
      dispatchRunError(
        sessionId,
        clientRunKey,
        transportEpoch,
        connectionId,
        'resume',
        error?.message || '恢复执行失败',
      );
      dispatch({ type: 'SET_PENDING_INTERRUPT', sessionId, interrupt });
      notifyEnd(sessionId);
    }
  }, [beginTransport, dispatchRunError, guardAndDispatch, notifyEnd, notifyStart]);

  const stopSessionRun = useCallback(async (sessionId: string) => {
    const session = stateRef.current.sessions[sessionId];
    if (!session || session.activeRunKeys.length === 0) return;
    const stoppedRunKeys = [...session.activeRunKeys];
    for (const runKey of session.activeRunKeys) {
      const entry = streamRegistryRef.current[runKey];
      entry?.abort?.();
      if (entry) {
        entry.subscription = undefined;
        entry.abort = undefined;
      }
    }
    dispatch({ type: 'LOCAL_CANCELLED', sessionId });
    notifyEnd(sessionId);
    try {
      await apiService.abortChat(sessionId);
    } catch (error) {
      const statusCode = (error as { response?: { status?: number } })?.response?.status;
      if (statusCode === 409) {
        return;
      }
      if (stoppedRunKeys.some((runKey) => terminalRunKeysRef.current.has(runKey))) {
        return;
      }
      console.warn('Abort request failed, reloading running state:', error);
      await loadSessionHistory(sessionId);
      dispatch({ type: 'SESSION_ERROR', sessionId, error: '停止请求失败，后端任务可能仍在运行' });
    }
  }, [loadSessionHistory, notifyEnd]);

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
        return run && (run.status === 'starting' || run.status === 'streaming');
      })) {
        ids.add(sessionId);
      }
    }
    return ids;
  }, []);

  const getActiveSlotSessionIds = getExecutingSessionIds;

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
