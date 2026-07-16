import { applyPatch, Operation } from 'fast-json-patch';

import type { AgentState, InterruptDetails, RoundData, StepData, ToolResult } from '../types';
import {
  ChatRuntimeAction,
  ChatRuntimeState,
  ChatRunRuntimeState,
  ChatSessionRuntimeState,
  StreamEnvelope,
  emptyAgentState,
  emptyBuffers,
  emptySessionState,
  initialChatRuntimeState,
} from './chatRuntimeTypes';

const TERMINAL_ROUND_STATUSES = new Set([
  'completed',
  'failed',
  'interrupted',
  'resumed',
  'cancelled',
  'max_steps_reached',
]);

const terminalRunStatuses = new Set(['finished', 'error', 'cancelled']);

export function chatRuntimeReducer(
  state: ChatRuntimeState = initialChatRuntimeState,
  action: ChatRuntimeAction,
): ChatRuntimeState {
  switch (action.type) {
    case 'SESSION_LOADING': {
      const session = ensureSession(state, action.sessionId);
      return putSession(state, action.sessionId, { ...session, loading: action.loading });
    }

    case 'SESSION_ERROR': {
      const session = ensureSession(state, action.sessionId);
      return putSession(state, action.sessionId, {
        ...session,
        error: action.error,
        loading: false,
      });
    }

    case 'CLEAR_ERROR': {
      const session = ensureSession(state, action.sessionId);
      return putSession(state, action.sessionId, { ...session, error: '' });
    }

    case 'CLEAR_SESSION_VIEW': {
      return putSession(state, action.sessionId, emptySessionState());
    }

    case 'LOCAL_RUN_STARTED':
      return applyLocalRunStarted(state, action);

    case 'HISTORY_LOADED':
      return applyHistoryLoaded(state, action.sessionId, action.rounds, action.loadedAt);

    case 'STREAM_EVENT':
      return applyStreamEvent(state, action.envelope);

    case 'LOCAL_CANCELLED':
      return applyLocalCancelled(state, action.sessionId, action.clientRunKey);

    case 'SET_PENDING_INTERRUPT': {
      const session = ensureSession(state, action.sessionId);
      return putSession(state, action.sessionId, {
        ...session,
        pendingInterrupt: action.interrupt,
      });
    }

    case 'RUNNING_SESSIONS_SNAPSHOT':
      return applyRunningSessionsSnapshot(state, action.runningSessions, action.receivedAt);

    default:
      return state;
  }
}

function ensureSession(state: ChatRuntimeState, sessionId: string): ChatSessionRuntimeState {
  return state.sessions[sessionId] || emptySessionState();
}

function putSession(
  state: ChatRuntimeState,
  sessionId: string,
  session: ChatSessionRuntimeState,
): ChatRuntimeState {
  return {
    ...state,
    sessions: {
      ...state.sessions,
      [sessionId]: session,
    },
  };
}

function putRun(
  state: ChatRuntimeState,
  clientRunKey: string,
  run: ChatRunRuntimeState,
): ChatRuntimeState {
  return {
    ...state,
    runs: {
      ...state.runs,
      [clientRunKey]: run,
    },
  };
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)));
}

function createRun(args: {
  ownerSessionId: string;
  clientRunKey: string;
  tempRoundId: string;
  idempotencyKey?: string;
  source: ChatRunRuntimeState['source'];
  status?: ChatRunRuntimeState['status'];
}): ChatRunRuntimeState {
  const now = Date.now();
  return {
    clientRunKey: args.clientRunKey,
    ownerSessionId: args.ownerSessionId,
    tempRoundId: args.tempRoundId,
    idempotencyKey: args.idempotencyKey,
    source: args.source,
    status: args.status || 'starting',
    lastSequence: 0,
    buffers: emptyBuffers(),
    createdAt: now,
    updatedAt: now,
  };
}

function applyLocalRunStarted(
  state: ChatRuntimeState,
  action: Extract<ChatRuntimeAction, { type: 'LOCAL_RUN_STARTED' }>,
): ChatRuntimeState {
  const session = ensureSession(state, action.sessionId);
  const existingRun = state.runs[action.clientRunKey];
  const baseRun = createRun({
    ownerSessionId: action.sessionId,
    clientRunKey: action.clientRunKey,
    tempRoundId: action.tempRoundId,
    idempotencyKey: action.idempotencyKey,
    source: action.source,
  });
  const run: ChatRunRuntimeState = existingRun
    ? {
        ...baseRun,
        serverRunId: existingRun.serverRunId ?? baseRun.serverRunId,
        lastSequence: Math.max(existingRun.lastSequence, baseRun.lastSequence),
        buffers: existingRun.buffers,
      }
    : baseRun;
  const round = action.round;
  const nextSession: ChatSessionRuntimeState = {
    ...session,
    rounds: round && !session.rounds.some((item) => item.round_id === round.round_id)
      ? [...session.rounds, round]
      : session.rounds,
    pendingInterrupt: null,
    error: '',
    loading: false,
    activeRunKeys: unique([...session.activeRunKeys, action.clientRunKey]),
    agentStateByRunKey: {
      ...session.agentStateByRunKey,
      [action.clientRunKey]: {
        currentStep: 0,
        status: 'running',
        toolLogs: [],
        lastUpdated: Date.now(),
      },
    },
    visibleAgentStateRunKey: action.clientRunKey,
  };
  const nextState = putSession(state, action.sessionId, nextSession);
  const withRun = putRun(nextState, action.clientRunKey, run);
  return action.idempotencyKey
    ? {
        ...withRun,
        idempotencyKeyToClientRunKey: {
          ...withRun.idempotencyKeyToClientRunKey,
          [action.idempotencyKey]: action.clientRunKey,
        },
      }
    : withRun;
}

function applyHistoryLoaded(
  state: ChatRuntimeState,
  sessionId: string,
  serverRounds: RoundData[],
  loadedAt: number,
): ChatRuntimeState {
  const session = ensureSession(state, sessionId);
  let nextState = state;
  let rounds = [...serverRounds];
  const activeRunKeys = new Set(session.activeRunKeys);
  const nextRuns = { ...state.runs };
  const nextServerRunMap = { ...state.serverRunIdToClientRunKey };
  const nextTempMap = { ...state.tempRoundIdToServerRoundId };
  const nextServerRoundMap = { ...state.serverRoundIdToLocalRoundId };
  const nextIdempotencyMap = { ...state.idempotencyKeyToClientRunKey };

  for (const runKey of session.activeRunKeys) {
    const run = nextRuns[runKey];
    if (!run) {
      activeRunKeys.delete(runKey);
      continue;
    }
    const localRound = session.rounds.find((round) => (
      round.round_id === run.tempRoundId || round.round_id === run.serverRunId
    ));
    const serverRound = serverRounds.find((round) => (
      round.round_id === run.serverRunId
      || round.round_id === run.tempRoundId
      || (!!run.idempotencyKey && round.idempotency_key === run.idempotencyKey)
    ));
    if (!serverRound) {
      if (
        localRound
        && !TERMINAL_ROUND_STATUSES.has(localRound.status)
        && (run.source === 'direct' || run.source === 'resume')
        && !rounds.some((round) => round.round_id === localRound.round_id)
      ) {
        rounds = [...rounds, localRound];
      }
      continue;
    }

    const serverIsTerminal = TERMINAL_ROUND_STATUSES.has(serverRound.status);
    const useServerTerminal = serverIsTerminal && (
      !localRound || isServerRoundNewer(localRound, serverRound, run)
    );

    nextRuns[runKey] = {
      ...run,
      serverRunId: serverRound.round_id,
      lastSequence: Math.max(run.lastSequence, serverRound.last_event_sequence || 0),
      status: useServerTerminal ? runStatusFromRoundStatus(serverRound.status) : run.status,
      buffers: useServerTerminal ? emptyBuffers() : run.buffers,
      debugMetadata: serverIsTerminal && !useServerTerminal
        ? {
            ...run.debugMetadata,
            historyConflicts: (run.debugMetadata?.historyConflicts || 0) + 1,
          }
        : run.debugMetadata,
      updatedAt: loadedAt,
    };
    if (useServerTerminal) {
      activeRunKeys.delete(runKey);
    }
    nextServerRunMap[serverRound.round_id] = runKey;
    nextTempMap[run.tempRoundId] = serverRound.round_id;
    nextServerRoundMap[serverRound.round_id] = run.tempRoundId;
    if (run.idempotencyKey) {
      nextIdempotencyMap[run.idempotencyKey] = runKey;
    }

    if (localRound && !useServerTerminal && !TERMINAL_ROUND_STATUSES.has(localRound.status)) {
      rounds = rounds.map((round) => (
        round.round_id === serverRound.round_id
          ? mergeActiveRound(localRound, serverRound)
          : round
      ));
    }
  }

  for (const round of serverRounds) {
    if (round.status === 'running') {
      const runKey = nextServerRunMap[round.round_id] || `run:${round.round_id}`;
      activeRunKeys.add(runKey);
      if (!nextRuns[runKey]) {
        nextRuns[runKey] = createRun({
          ownerSessionId: sessionId,
          clientRunKey: runKey,
          tempRoundId: round.round_id,
          source: 'history',
          status: 'streaming',
        });
      }
      nextRuns[runKey] = {
        ...nextRuns[runKey],
        serverRunId: round.round_id,
        lastSequence: Math.max(nextRuns[runKey].lastSequence, round.last_event_sequence || 0),
        status: 'streaming',
        updatedAt: loadedAt,
      };
      nextServerRunMap[round.round_id] = runKey;
      nextServerRoundMap[round.round_id] = round.round_id;
    }
  }

  const interruptedRound = [...rounds].reverse().find((round) => (
    round.status === 'interrupted' && round.interrupt
  ));
  const hasRunningRound = rounds.some((round) => round.status === 'running');
  const nextSession: ChatSessionRuntimeState = {
    ...session,
    rounds,
    loading: false,
    lastHistoryLoadedAt: loadedAt,
    activeRunKeys: unique(Array.from(activeRunKeys).filter((runKey) => {
      const run = nextRuns[runKey];
      return run && !terminalRunStatuses.has(run.status);
    })),
    pendingInterrupt: hasRunningRound ? null : (interruptedRound?.interrupt || session.pendingInterrupt),
  };

  nextState = {
    ...nextState,
    runs: nextRuns,
    serverRunIdToClientRunKey: nextServerRunMap,
    tempRoundIdToServerRoundId: nextTempMap,
    serverRoundIdToLocalRoundId: nextServerRoundMap,
    idempotencyKeyToClientRunKey: nextIdempotencyMap,
  };
  return putSession(nextState, sessionId, nextSession);
}

function mergeActiveRound(localRound: RoundData, serverRound: RoundData): RoundData {
  return {
    ...serverRound,
    round_id: serverRound.round_id,
    control_kind: localRound.control_kind ?? serverRound.control_kind,
    user_message: localRound.user_message || serverRound.user_message,
    user_attachments: localRound.user_attachments || serverRound.user_attachments,
    final_response: localRound.final_response || serverRound.final_response,
    steps: localRound.steps.length > 0 ? localRound.steps : serverRound.steps,
    step_count: Math.max(localRound.step_count || 0, serverRound.step_count || 0),
    status: localRound.status,
  };
}

function isServerRoundNewer(
  localRound: RoundData,
  serverRound: RoundData,
  run: ChatRunRuntimeState,
): boolean {
  const serverUpdatedAt = Date.parse(
    (serverRound as any).updatedAt
    || (serverRound as any).updated_at
    || serverRound.completed_at
    || '',
  );
  const localUpdatedAt = Date.parse(
    (localRound as any).updatedAt
    || (localRound as any).updated_at
    || localRound.completed_at
    || localRound.created_at
    || '',
  );
  if (!Number.isNaN(serverUpdatedAt) && !Number.isNaN(localUpdatedAt) && serverUpdatedAt > localUpdatedAt) {
    return true;
  }
  if ((serverRound.last_event_sequence || 0) > run.lastSequence) {
    return true;
  }
  if (serverRound.final_response && !localRound.final_response && !TERMINAL_ROUND_STATUSES.has(localRound.status)) {
    return true;
  }
  return false;
}

function runStatusFromRoundStatus(status: string): ChatRunRuntimeState['status'] {
  if (status === 'failed') return 'error';
  if (status === 'cancelled') return 'cancelled';
  return 'finished';
}

function applyStreamEvent(state: ChatRuntimeState, envelope: StreamEnvelope): ChatRuntimeState {
  const event = envelope.event;
  const eventType = event?.type;
  const run = state.runs[envelope.clientRunKey];
  if (!run) {
    return state;
  }

  const sequence = envelope.sequence;
  if (typeof sequence === 'number' && sequence <= run.lastSequence) {
    return state;
  }

  if (
    run.status === 'cancelled'
    && isVisibleDeltaEvent(eventType)
  ) {
    return putRun(state, run.clientRunKey, {
      ...run,
      debugMetadata: {
        ...run.debugMetadata,
        droppedAfterCancel: (run.debugMetadata?.droppedAfterCancel || 0) + 1,
      },
      updatedAt: Date.now(),
    });
  }

  if (terminalRunStatuses.has(run.status)) {
    return putRun(state, run.clientRunKey, {
      ...run,
      backendTerminal: eventType === 'RUN_FINISHED' || eventType === 'RUN_ERROR'
        ? event
        : run.backendTerminal,
      debugMetadata: {
        ...run.debugMetadata,
        droppedAfterTerminal: (run.debugMetadata?.droppedAfterTerminal || 0) + 1,
      },
      updatedAt: envelope.receivedAt,
    });
  }

  let nextState = state;
  let nextRun: ChatRunRuntimeState = {
    ...run,
    lastSequence: typeof sequence === 'number' ? sequence : run.lastSequence,
    updatedAt: envelope.receivedAt,
  };

  switch (eventType) {
    case 'RUN_STARTED':
      nextState = applyRunStarted(nextState, envelope, nextRun);
      return nextState;
    case 'STATE_SNAPSHOT':
      nextState = updateAgentState(nextState, nextRun, event.snapshot || emptyAgentState());
      nextRun = { ...nextRun, status: nextRun.status === 'starting' ? 'streaming' : nextRun.status };
      break;
    case 'STATE_DELTA':
      nextState = updateAgentStateDelta(nextState, nextRun, event.delta || []);
      break;
    case 'STEP_STARTED':
      nextState = updateRound(nextState, nextRun, (round) => addStepStarted(round, event.stepName, event.timestamp));
      nextState = updateAgentStateDeltaValue(nextState, nextRun, (prev) => ({
        ...prev,
        currentStep: prev.currentStep + 1,
        status: 'running',
        lastUpdated: Date.now(),
      }));
      break;
    case 'STEP_FINISHED':
      nextState = updateRound(nextState, nextRun, (round) => markStepFinished(round, event.stepName, event.timestamp));
      break;
    case 'TEXT_MESSAGE_START':
      nextRun = {
        ...nextRun,
        buffers: {
          ...nextRun.buffers,
          currentTextMessageId: event.messageId,
          textByMessageId: { ...nextRun.buffers.textByMessageId, [event.messageId]: '' },
        },
      };
      break;
    case 'TEXT_MESSAGE_CONTENT':
      nextRun = applyTextDelta(nextRun, envelope);
      nextState = updateRound(nextState, nextRun, (round) => updateRoundTextContent(round, latestText(nextRun)));
      break;
    case 'TEXT_MESSAGE_END':
      nextRun = { ...nextRun, buffers: { ...nextRun.buffers, currentTextMessageId: null } };
      break;
    case 'THINKING_TEXT_MESSAGE_START':
      nextRun = {
        ...nextRun,
        buffers: {
          ...nextRun.buffers,
          currentThinkingMessageId: event.messageId,
          thinkingByMessageId: { ...nextRun.buffers.thinkingByMessageId, [event.messageId]: '' },
        },
      };
      nextState = updateLastStep(nextState, nextRun, (step) => ({ ...step, thinking_start_ts: event.timestamp }));
      break;
    case 'THINKING_TEXT_MESSAGE_CONTENT':
      nextRun = applyThinkingDelta(nextRun, envelope);
      nextState = updateLastStep(nextState, nextRun, (step) => ({ ...step, thinking: latestThinking(nextRun) }));
      break;
    case 'THINKING_TEXT_MESSAGE_END':
      nextRun = { ...nextRun, buffers: { ...nextRun.buffers, currentThinkingMessageId: null } };
      nextState = updateLastStep(nextState, nextRun, (step) => ({ ...step, thinking_end_ts: event.timestamp }));
      break;
    case 'TOOL_CALL_START':
      nextRun = {
        ...nextRun,
        buffers: {
          ...nextRun.buffers,
          toolArgsByToolCallId: {
            ...nextRun.buffers.toolArgsByToolCallId,
            [event.toolCallId]: '',
          },
        },
      };
      nextState = updateLastStep(nextState, nextRun, (step) => ({
        ...step,
        tool_calls: [
          ...step.tool_calls,
          { id: event.toolCallId, name: event.toolCallName, input: {}, started_at_ts: event.timestamp },
        ],
      }));
      nextState = updateAgentToolLog(nextState, nextRun, {
        toolCallId: event.toolCallId,
        toolName: event.toolCallName,
        status: 'running',
        startedAt: Date.now(),
      });
      break;
    case 'TOOL_CALL_ARGS':
      nextRun = applyToolArgsDelta(nextRun, envelope);
      nextState = updateToolArgs(nextState, nextRun, event.toolCallId);
      break;
    case 'TOOL_CALL_END':
      nextState = updateToolEnd(nextState, nextRun, event.toolCallId, event.timestamp);
      break;
    case 'TOOL_CALL_RESULT':
      nextState = updateToolResult(nextState, nextRun, event.toolCallId, event.content, event.timestamp, event.executionTimeMs);
      break;
    case 'RUN_FINISHED':
      nextState = applyRunFinished(nextState, envelope, nextRun);
      return nextState;
    case 'RUN_ERROR':
      nextState = applyRunError(nextState, envelope, nextRun);
      return nextState;
    default:
      break;
  }

  return putRun(nextState, nextRun.clientRunKey, nextRun);
}

function applyRunStarted(
  state: ChatRuntimeState,
  envelope: StreamEnvelope,
  run: ChatRunRuntimeState,
): ChatRuntimeState {
  const serverRunId = envelope.event.runId || envelope.serverRunId;
  if (!serverRunId) {
    return putRun(state, run.clientRunKey, { ...run, status: 'streaming' });
  }
  const nextRun: ChatRunRuntimeState = {
    ...run,
    serverRunId,
    status: 'streaming',
  };
  const session = ensureSession(state, run.ownerSessionId);
  const rounds = session.rounds.map((round) => (
    round.round_id === run.tempRoundId
      ? { ...round, round_id: serverRunId, status: 'running' }
      : round
  ));
  const nextState = putSession(state, run.ownerSessionId, {
    ...session,
    rounds,
    activeRunKeys: unique([...session.activeRunKeys, run.clientRunKey]),
    visibleAgentStateRunKey: run.clientRunKey,
  });
  return {
    ...putRun(nextState, run.clientRunKey, nextRun),
    serverRunIdToClientRunKey: {
      ...nextState.serverRunIdToClientRunKey,
      [serverRunId]: run.clientRunKey,
    },
    tempRoundIdToServerRoundId: {
      ...nextState.tempRoundIdToServerRoundId,
      [run.tempRoundId]: serverRunId,
    },
    serverRoundIdToLocalRoundId: {
      ...nextState.serverRoundIdToLocalRoundId,
      [serverRunId]: run.tempRoundId,
    },
  };
}

function applyRunFinished(
  state: ChatRuntimeState,
  envelope: StreamEnvelope,
  run: ChatRunRuntimeState,
): ChatRuntimeState {
  const event = envelope.event;
  const session = ensureSession(state, run.ownerSessionId);
  const targetRunId = event.runId || run.serverRunId || run.tempRoundId;
  const isCancelled = run.status === 'cancelled' || isUserCancelledOutcome(event.outcome || 'success', event.interrupt, event.result);
  const nextStatus = isCancelled ? 'cancelled' : 'finished';
  const roundStatus = isCancelled
    ? 'cancelled'
    : getRunFinishedRoundStatus(event.outcome || 'success', false, event.result);
  const finalContent = event.result?.finalResponse || event.result?.final_response || latestText(run);
  const rounds = session.rounds.map((round) => {
    if (!roundMatchesRun(round, run, targetRunId)) {
      return round;
    }
    if (run.status === 'cancelled') {
      return {
        ...round,
        status: 'cancelled',
        completed_at: round.completed_at || new Date().toISOString(),
        steps: finalizeStepsForTerminal(round.steps, false),
      };
    }
    return {
      ...round,
      round_id: targetRunId,
      steps: finalizeStepsForTerminal(round.steps, !!finalContent),
      final_response: finalContent || round.final_response,
      status: roundStatus,
      completed_at: getRunFinishedCompletedAt(event.outcome || 'success', isCancelled, event.result),
      interrupt: event.interrupt,
    };
  });
  const nextRun: ChatRunRuntimeState = {
    ...run,
    serverRunId: targetRunId,
    status: nextStatus,
    backendTerminal: event,
    buffers: emptyBuffers(),
    updatedAt: envelope.receivedAt,
  };
  const nextSession: ChatSessionRuntimeState = {
    ...session,
    rounds,
    pendingInterrupt: event.outcome === 'interrupt' && event.interrupt && !isCancelled
      ? event.interrupt
      : (isCancelled ? null : session.pendingInterrupt),
    activeRunKeys: session.activeRunKeys.filter((key) => key !== run.clientRunKey),
    agentStateByRunKey: {
      ...session.agentStateByRunKey,
      [run.clientRunKey]: {
        ...(session.agentStateByRunKey[run.clientRunKey] || emptyAgentState()),
        status: event.outcome === 'interrupt' && event.interrupt && !isCancelled ? 'waiting' : 'completed',
        lastUpdated: Date.now(),
      },
    },
  };
  const nextState = putSession(state, run.ownerSessionId, nextSession);
  return putRun(nextState, run.clientRunKey, nextRun);
}

function applyRunError(
  state: ChatRuntimeState,
  envelope: StreamEnvelope,
  run: ChatRunRuntimeState,
): ChatRuntimeState {
  const session = ensureSession(state, run.ownerSessionId);
  const message = envelope.event.message || 'Run failed';
  if (envelope.event.code === 'USER_BUSY') {
    const rounds = session.rounds.filter((round) => !roundMatchesRun(round, run, envelope.event.runId));
    return putRun(
      putSession(state, run.ownerSessionId, {
        ...session,
        rounds,
        error: message,
        activeRunKeys: session.activeRunKeys.filter((key) => key !== run.clientRunKey),
      }),
      run.clientRunKey,
      {
        ...run,
        status: 'error',
        backendTerminal: envelope.event,
        buffers: emptyBuffers(),
        updatedAt: envelope.receivedAt,
      },
    );
  }
  const nextRun: ChatRunRuntimeState = {
    ...run,
    status: run.status === 'cancelled' ? 'cancelled' : 'error',
    backendTerminal: envelope.event,
    buffers: emptyBuffers(),
    updatedAt: envelope.receivedAt,
  };
  const rounds = session.rounds.map((round) => (
    roundMatchesRun(round, run, envelope.event.runId)
      ? {
          ...round,
          status: nextRun.status === 'cancelled' ? 'cancelled' : 'failed',
          completed_at: round.completed_at || new Date().toISOString(),
          final_response: round.final_response || message,
        }
      : round
  ));
  const nextSession: ChatSessionRuntimeState = {
    ...session,
    rounds,
    error: nextRun.status === 'cancelled' ? session.error : message,
    activeRunKeys: session.activeRunKeys.filter((key) => key !== run.clientRunKey),
    agentStateByRunKey: {
      ...session.agentStateByRunKey,
      [run.clientRunKey]: {
        ...(session.agentStateByRunKey[run.clientRunKey] || emptyAgentState()),
        status: nextRun.status === 'cancelled' ? 'completed' : 'error',
        lastUpdated: Date.now(),
      },
    },
  };
  return putRun(putSession(state, run.ownerSessionId, nextSession), run.clientRunKey, nextRun);
}

function applyLocalCancelled(
  state: ChatRuntimeState,
  sessionId: string,
  clientRunKey?: string,
): ChatRuntimeState {
  const session = ensureSession(state, sessionId);
  const targetKeys = clientRunKey
    ? [clientRunKey]
    : session.activeRunKeys;
  let nextState = state;
  let rounds = session.rounds;
  const nextRuns = { ...state.runs };
  for (const key of targetKeys) {
    const run = nextRuns[key];
    if (!run) continue;
    nextRuns[key] = {
      ...run,
      status: 'cancelled',
      buffers: emptyBuffers(),
      updatedAt: Date.now(),
    };
    rounds = rounds.map((round) => (
      roundMatchesRun(round, run)
        ? {
            ...round,
            status: 'cancelled',
            completed_at: round.completed_at || new Date().toISOString(),
            steps: finalizeStepsForTerminal(round.steps, false),
          }
        : round
    ));
  }
  nextState = { ...nextState, runs: nextRuns };
  return putSession(nextState, sessionId, {
    ...session,
    rounds,
    pendingInterrupt: null,
    activeRunKeys: session.activeRunKeys.filter((key) => !targetKeys.includes(key)),
  });
}

function applyRunningSessionsSnapshot(
  state: ChatRuntimeState,
  runningSessions: Array<{ session_id: string; round_id: string | null }>,
  receivedAt: number,
): ChatRuntimeState {
  let nextState = state;
  const runningSessionIds = new Set(runningSessions.map((item) => item.session_id));

  for (const item of runningSessions) {
    const session = ensureSession(nextState, item.session_id);
    const runKey = item.round_id ? `run:${item.round_id}` : `init:${item.session_id}`;
    if (!nextState.runs[runKey]) {
      nextState = putRun(nextState, runKey, createRun({
        ownerSessionId: item.session_id,
        clientRunKey: runKey,
        tempRoundId: item.round_id || runKey,
        source: item.round_id ? 'history' : 'init',
        status: item.round_id ? 'streaming' : 'starting',
      }));
    }
    nextState = putSession(nextState, item.session_id, {
      ...session,
      activeRunKeys: unique([...session.activeRunKeys, runKey]),
    });
  }

  for (const [sessionId, session] of Object.entries(nextState.sessions)) {
    if (runningSessionIds.has(sessionId)) continue;
    if (session.activeRunKeys.length === 0) continue;
    const retainedRunKeys = session.activeRunKeys.filter((key) => {
      const run = nextState.runs[key];
      return !!run
        && !terminalRunStatuses.has(run.status)
        && (run.source === 'direct' || run.source === 'resume');
    });
    if (retainedRunKeys.length === session.activeRunKeys.length) continue;
    nextState = putSession(nextState, sessionId, {
      ...session,
      activeRunKeys: retainedRunKeys,
      lastHistoryLoadedAt: session.lastHistoryLoadedAt || receivedAt,
    });
  }

  return nextState;
}

function isVisibleDeltaEvent(eventType: string): boolean {
  return eventType === 'TEXT_MESSAGE_CONTENT'
    || eventType === 'THINKING_TEXT_MESSAGE_CONTENT'
    || eventType === 'TOOL_CALL_ARGS';
}

function updateAgentState(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  agentState: AgentState,
): ChatRuntimeState {
  const session = ensureSession(state, run.ownerSessionId);
  return putSession(state, run.ownerSessionId, {
    ...session,
    agentStateByRunKey: {
      ...session.agentStateByRunKey,
      [run.clientRunKey]: agentState,
    },
    visibleAgentStateRunKey: run.clientRunKey,
  });
}

function updateAgentStateDelta(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  delta: Operation[],
): ChatRuntimeState {
  return updateAgentStateDeltaValue(state, run, (prev) => {
    try {
      const result = applyPatch(prev, delta, true, false);
      return result.newDocument;
    } catch (error) {
      console.error('Failed to apply state patch:', error);
      return prev;
    }
  });
}

function updateAgentStateDeltaValue(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  updater: (prev: AgentState) => AgentState,
): ChatRuntimeState {
  const session = ensureSession(state, run.ownerSessionId);
  const prev = session.agentStateByRunKey[run.clientRunKey] || emptyAgentState();
  return updateAgentState(state, run, updater(prev));
}

function updateAgentToolLog(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  log: AgentState['toolLogs'][number],
): ChatRuntimeState {
  return updateAgentStateDeltaValue(state, run, (prev) => ({
    ...prev,
    toolLogs: [...prev.toolLogs, log],
    lastUpdated: Date.now(),
  }));
}

function updateRound(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  updater: (round: RoundData) => RoundData,
): ChatRuntimeState {
  const session = ensureSession(state, run.ownerSessionId);
  const rounds = session.rounds.map((round) => (
    roundMatchesRun(round, run) ? updater(round) : round
  ));
  return putSession(state, run.ownerSessionId, { ...session, rounds });
}

function updateLastStep(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  updater: (step: StepData) => StepData,
): ChatRuntimeState {
  return updateRound(state, run, (round) => {
    const ensured = ensureStep(round);
    const steps = [...ensured.steps];
    steps[steps.length - 1] = updater(steps[steps.length - 1]);
    return { ...ensured, steps };
  });
}

function addStepStarted(round: RoundData, stepName: string, timestamp?: number): RoundData {
  const stepNumber = parseInt(String(stepName || '').replace('step_', ''), 10) || round.steps.length + 1;
  if (round.steps.some((step) => step.step_number === stepNumber)) {
    return round;
  }
  return {
    ...round,
    steps: [
      ...round.steps,
      {
        step_number: stepNumber,
        thinking: '',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'streaming',
        started_at_ts: timestamp,
      },
    ],
    step_count: Math.max(round.step_count || 0, stepNumber),
  };
}

function markStepFinished(round: RoundData, stepName: string, timestamp?: number): RoundData {
  const stepNumber = parseInt(String(stepName || '').replace('step_', ''), 10) || round.steps.length;
  return {
    ...round,
    steps: round.steps.map((step) => (
      step.step_number === stepNumber
        ? { ...step, status: 'completed', finished_at_ts: timestamp }
        : step
    )),
  };
}

function ensureStep(round: RoundData): RoundData {
  if (round.steps.length > 0) {
    return round;
  }
  return {
    ...round,
    steps: [{
      step_number: 1,
      thinking: '',
      assistant_content: '',
      tool_calls: [],
      tool_results: [],
      status: 'streaming',
    }],
    step_count: Math.max(round.step_count || 0, 1),
  };
}

function updateRoundTextContent(round: RoundData, content: string): RoundData {
  const ensured = ensureStep(round);
  const steps = [...ensured.steps];
  const lastStep = steps[steps.length - 1];
  if (lastStep.tool_calls.length > 0 && lastStep.status !== 'streaming') {
    const newStepNumber = ensured.steps.length + 1;
    return {
      ...ensured,
      steps: [
        ...steps,
        {
          step_number: newStepNumber,
          thinking: '',
          assistant_content: content,
          tool_calls: [],
          tool_results: [],
          status: 'streaming',
        },
      ],
      step_count: newStepNumber,
    };
  }
  steps[steps.length - 1] = { ...lastStep, assistant_content: content };
  return { ...ensured, steps };
}

function applyTextDelta(run: ChatRunRuntimeState, envelope: StreamEnvelope): ChatRunRuntimeState {
  const messageId = envelope.messageId || envelope.event.messageId || run.buffers.currentTextMessageId || 'default';
  const prev = run.buffers.textByMessageId[messageId] || '';
  const next = envelope.isAggregate ? envelope.event.delta : prev + (envelope.event.delta || '');
  return {
    ...run,
    buffers: {
      ...run.buffers,
      currentTextMessageId: messageId,
      textByMessageId: { ...run.buffers.textByMessageId, [messageId]: next },
    },
  };
}

function applyThinkingDelta(run: ChatRunRuntimeState, envelope: StreamEnvelope): ChatRunRuntimeState {
  const messageId = envelope.messageId || envelope.event.messageId || run.buffers.currentThinkingMessageId || 'default';
  const prev = run.buffers.thinkingByMessageId[messageId] || '';
  const next = envelope.isAggregate ? envelope.event.delta : prev + (envelope.event.delta || '');
  return {
    ...run,
    buffers: {
      ...run.buffers,
      currentThinkingMessageId: messageId,
      thinkingByMessageId: { ...run.buffers.thinkingByMessageId, [messageId]: next },
    },
  };
}

function applyToolArgsDelta(run: ChatRunRuntimeState, envelope: StreamEnvelope): ChatRunRuntimeState {
  const toolCallId = envelope.toolCallId || envelope.event.toolCallId;
  const prev = run.buffers.toolArgsByToolCallId[toolCallId] || '';
  const next = envelope.isAggregate ? envelope.event.delta : prev + (envelope.event.delta || '');
  return {
    ...run,
    buffers: {
      ...run.buffers,
      toolArgsByToolCallId: {
        ...run.buffers.toolArgsByToolCallId,
        [toolCallId]: next,
      },
    },
  };
}

function latestText(run: ChatRunRuntimeState): string {
  const current = run.buffers.currentTextMessageId;
  if (current && run.buffers.textByMessageId[current] !== undefined) {
    return run.buffers.textByMessageId[current];
  }
  const values = Object.values(run.buffers.textByMessageId);
  return values[values.length - 1] || '';
}

function latestThinking(run: ChatRunRuntimeState): string {
  const current = run.buffers.currentThinkingMessageId;
  if (current && run.buffers.thinkingByMessageId[current] !== undefined) {
    return run.buffers.thinkingByMessageId[current];
  }
  const values = Object.values(run.buffers.thinkingByMessageId);
  return values[values.length - 1] || '';
}

function updateToolArgs(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  toolCallId: string,
): ChatRuntimeState {
  const argsString = run.buffers.toolArgsByToolCallId[toolCallId] || '';
  try {
    const parsedArgs = JSON.parse(argsString);
    return updateLastStep(state, run, (step) => {
      const toolIndex = step.tool_calls.findIndex((toolCall) => toolCall.id === toolCallId);
      if (toolIndex < 0) return step;
      const toolCalls = [...step.tool_calls];
      toolCalls[toolIndex] = { ...toolCalls[toolIndex], input: parsedArgs };
      return { ...step, tool_calls: toolCalls };
    });
  } catch {
    return state;
  }
}

function updateToolEnd(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  toolCallId: string,
  timestamp?: number,
): ChatRuntimeState {
  let nextState = updateLastStep(state, run, (step) => {
    const toolIndex = step.tool_calls.findIndex((toolCall) => toolCall.id === toolCallId);
    if (toolIndex < 0) return step;
    const toolCalls = [...step.tool_calls];
    toolCalls[toolIndex] = { ...toolCalls[toolIndex], ended_at_ts: timestamp };
    return { ...step, tool_calls: toolCalls };
  });
  nextState = updateAgentStateDeltaValue(nextState, run, (prev) => ({
    ...prev,
    toolLogs: prev.toolLogs.map((log) => (
      log.toolCallId === toolCallId
        ? { ...log, status: 'pending', args: run.buffers.toolArgsByToolCallId[toolCallId] }
        : log
    )),
    lastUpdated: Date.now(),
  }));
  return nextState;
}

function updateToolResult(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  toolCallId: string,
  content: string,
  timestamp?: number,
  executionTimeMs?: number,
): ChatRuntimeState {
  let resultObj: Pick<ToolResult, 'success' | 'content' | 'error'> = {
    success: true,
    content,
    error: undefined,
  };
  try {
    const parsed = JSON.parse(content);
    resultObj = {
      success: !parsed.error,
      content: parsed.output || content,
      error: parsed.error,
    };
  } catch {
    // keep raw content
  }
  let nextState = updateLastStep(state, run, (step) => ({
    ...step,
    tool_results: [
      ...step.tool_results,
      {
        ...resultObj,
        tool_call_id: toolCallId,
        received_at_ts: timestamp,
        execution_time_ms: executionTimeMs,
      },
    ],
  }));
  nextState = updateAgentStateDeltaValue(nextState, run, (prev) => ({
    ...prev,
    toolLogs: prev.toolLogs.map((log) => (
      log.toolCallId === toolCallId
        ? { ...log, status: 'completed', result: content, completedAt: Date.now() }
        : log
    )),
    lastUpdated: Date.now(),
  }));
  return nextState;
}

function roundMatchesRun(round: RoundData, run: ChatRunRuntimeState, explicitRunId?: string): boolean {
  return round.round_id === explicitRunId
    || round.round_id === run.serverRunId
    || round.round_id === run.tempRoundId;
}

function isUserCancelledOutcome(outcome: string, _interrupt: InterruptDetails | undefined, result?: any): boolean {
  if (outcome !== 'interrupt') return false;
  if (result?.reason === 'user_cancelled') return true;
  if (result?.reason === 'max_steps_reached') return false;
  return false;
}

function getRunFinishedRoundStatus(outcome: string, isUserCancelled: boolean, result?: any): string {
  if (isUserCancelled) return 'cancelled';
  if (outcome === 'interrupt' && result?.reason === 'max_steps_reached') return 'max_steps_reached';
  if (outcome === 'interrupt') return 'interrupted';
  if (outcome === 'success') return 'completed';
  return outcome;
}

function getRunFinishedCompletedAt(outcome: string, isUserCancelled: boolean, result?: any): string | undefined {
  if (outcome === 'interrupt' && !isUserCancelled && result?.reason !== 'max_steps_reached') {
    return undefined;
  }
  return new Date().toISOString();
}

function finalizeStepsForTerminal(steps: StepData[], hasFinalResponse: boolean): StepData[] {
  return steps.map((step) => ({
    ...step,
    status: step.status === 'streaming' || step.status === 'running' ? 'completed' : step.status,
    ...(hasFinalResponse ? { assistant_content: '' } : {}),
  }));
}
