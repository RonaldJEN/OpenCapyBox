import { applyPatch, Operation } from 'fast-json-patch';

import type {
  AgentState,
  AssistantFileReference,
  InterruptDetails,
  RoundData,
  StepData,
  ToolResult,
} from '../types';
import {
  ChatRuntimeAction,
  ChatRuntimeState,
  ChatRunRuntimeState,
  ChatSessionRuntimeState,
  RUNTIME_HISTORY_SNAPSHOT,
  StreamEnvelope,
  emptyAgentState,
  emptyBuffers,
  emptySessionState,
  initialChatRuntimeState,
} from './chatRuntimeTypes';

const TERMINAL_ROUND_STATUSES = new Set([
  'completed',
  'failed',
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

    case 'WORKSPACE_ENTRIES_DELETED':
      return applyWorkspaceEntryTombstones(state, action.entryIds);

    case 'LOCAL_RUN_STARTED':
      return applyLocalRunStarted(state, action);

    case 'HISTORY_LOADED':
      return applyHistoryLoaded(
        state,
        action.sessionId,
        action.rounds.map((round) => removeDeletedWorkspaceFiles(
          round,
          state.workspaceDeletedEntryIds,
        )),
        action.loadedAt,
      );

    case 'STREAM_EVENT':
      return applyStreamEvent(state, action.envelope);

    case 'LOCAL_CONTROL_CONFLICT':
      return applyLocalControlConflict(
        state,
        action.sessionId,
        action.clientRunKey,
        action.serverRunId,
      );

    case 'LOCAL_CANCELLED':
      return applyLocalCancelled(state, action.sessionId, action.clientRunKey);

    case 'LOCAL_INIT_SLOT_CLEARED':
      return clearLocalInitSlot(state, action.sessionId);

    case 'RESTORE_PENDING_INTERACTION':
      return restorePendingInteraction(state, action);

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

function removeDeletedWorkspaceFiles(
  round: RoundData,
  deletedEntryIds: Readonly<Record<string, true>>,
): RoundData {
  const userAttachments = (round.user_attachments || []).filter((file) => (
    file.source !== 'workspace'
    || !file.entry_id
    || !deletedEntryIds[file.entry_id]
  ));
  const assistantReferences = (round.assistant_file_references || []).filter((file) => (
    file.source !== 'workspace'
    || !file.entry_id
    || !deletedEntryIds[file.entry_id]
  ));
  if (
    userAttachments.length === (round.user_attachments || []).length
    && assistantReferences.length === (round.assistant_file_references || []).length
  ) return round;
  return {
    ...round,
    user_attachments: userAttachments,
    assistant_file_references: assistantReferences,
  };
}

function applyWorkspaceEntryTombstones(
  state: ChatRuntimeState,
  entryIds: string[],
): ChatRuntimeState {
  const normalized = [...new Set(entryIds.filter(Boolean))];
  if (normalized.length === 0) return state;
  const workspaceDeletedEntryIds = { ...state.workspaceDeletedEntryIds };
  normalized.forEach((entryId) => { workspaceDeletedEntryIds[entryId] = true; });
  let changed = false;
  const sessions = Object.fromEntries(Object.entries(state.sessions).map(([sessionId, session]) => {
    const rounds = session.rounds.map((round) => {
      const next = removeDeletedWorkspaceFiles(round, workspaceDeletedEntryIds);
      if (next !== round) changed = true;
      return next;
    });
    return [sessionId, changed ? { ...session, rounds } : session];
  }));
  return {
    ...state,
    sessions: changed ? sessions : state.sessions,
    workspaceDeletedEntryIds,
  };
}

function normalizeAssistantFileReference(value: unknown): AssistantFileReference | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const source = item.source;
  const refId = item.ref_id;
  const name = item.name;
  const path = item.path;
  const revision = item.revision;
  if (source !== 'session' && source !== 'workspace') return null;
  if (
    typeof refId !== 'string' || !refId
    || typeof name !== 'string' || !name
    || typeof path !== 'string' || !path
    || typeof revision !== 'string' || !revision
  ) return null;
  const common = {
    ref_id: refId,
    name,
    path,
    size: typeof item.size === 'number' ? item.size : 0,
    modified: typeof item.modified === 'string' ? item.modified : '',
    type: typeof item.type === 'string' ? item.type : '',
    revision,
    operation: typeof item.operation === 'string' ? item.operation : null,
    tool_call_id: typeof item.toolCallId === 'string' ? item.toolCallId : null,
    sha256: typeof item.sha256 === 'string' ? item.sha256 : null,
  };
  if (source === 'session') {
    if (typeof item.session_id !== 'string' || !item.session_id) return null;
    if (typeof item.snapshot_path !== 'string' || !item.snapshot_path) return null;
    return {
      ...common,
      source,
      session_id: item.session_id,
      snapshot_path: item.snapshot_path,
    };
  }
  if (typeof item.entry_id !== 'string' || !item.entry_id) return null;
  if (typeof item.version_id !== 'string' || !item.version_id) return null;
  return {
    ...common,
    source,
    entry_id: item.entry_id,
    workspace_path: typeof item.workspace_path === 'string' && item.workspace_path
      ? item.workspace_path
      : path,
    version_id: item.version_id,
  };
}

function appendAssistantFileReference(
  round: RoundData,
  reference: AssistantFileReference,
): RoundData {
  const identity = reference.source === 'workspace'
    ? `workspace:${reference.entry_id}`
    : `session:${reference.session_id}:${reference.path}`;
  const retained = (round.assistant_file_references || []).filter((item) => (
    item.source === 'workspace'
      ? `workspace:${item.entry_id}` !== identity
      : `session:${item.session_id}:${item.path}` !== identity
  ));
  return { ...round, assistant_file_references: [...retained, reference] };
}

function removeAssistantWorkspaceReference(round: RoundData, entryId: string): RoundData {
  return {
    ...round,
    assistant_file_references: (round.assistant_file_references || []).filter((item) => (
      item.source !== 'workspace' || item.entry_id !== entryId
    )),
  };
}

function removeAssistantSessionReference(
  round: RoundData,
  sessionId: string,
  path: string,
): RoundData {
  return {
    ...round,
    assistant_file_references: (round.assistant_file_references || []).filter((item) => (
      item.source !== 'session' || item.session_id !== sessionId || item.path !== path
    )),
  };
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
        lastInteractionSequence: existingRun.lastInteractionSequence,
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

function applyLocalControlConflict(
  state: ChatRuntimeState,
  sessionId: string,
  clientRunKey: string,
  serverRunId?: string,
): ChatRuntimeState {
  const session = ensureSession(state, sessionId);
  const run = state.runs[clientRunKey];
  if (!run) return state;

  const rounds = session.rounds.filter((round) => (
    round.round_id !== run.tempRoundId || round.round_id === serverRunId
  ));
  const nextSession: ChatSessionRuntimeState = {
    ...session,
    rounds,
    activeRunKeys: session.activeRunKeys.filter((key) => key !== clientRunKey),
    agentStateByRunKey: {
      ...session.agentStateByRunKey,
      [clientRunKey]: {
        ...(session.agentStateByRunKey[clientRunKey] || emptyAgentState()),
        status: serverRunId ? 'waiting' : 'idle',
        lastUpdated: Date.now(),
      },
    },
  };
  const idempotencyKeyToClientRunKey = { ...state.idempotencyKeyToClientRunKey };
  if (run.idempotencyKey) {
    delete idempotencyKeyToClientRunKey[run.idempotencyKey];
  }
  const nextState = putSession({
    ...state,
    idempotencyKeyToClientRunKey,
  }, sessionId, nextSession);

  if (!serverRunId) {
    const runs = { ...nextState.runs };
    delete runs[clientRunKey];
    return { ...nextState, runs };
  }

  return {
    ...putRun(nextState, clientRunKey, {
      ...run,
      serverRunId,
      status: 'waiting',
      buffers: emptyBuffers(),
      updatedAt: Date.now(),
    }),
    serverRunIdToClientRunKey: {
      ...nextState.serverRunIdToClientRunKey,
      [serverRunId]: clientRunKey,
    },
    tempRoundIdToServerRoundId: {
      ...nextState.tempRoundIdToServerRoundId,
      [run.tempRoundId]: serverRunId,
    },
    serverRoundIdToLocalRoundId: {
      ...nextState.serverRoundIdToLocalRoundId,
      [serverRunId]: serverRunId,
    },
  };
}

function restorePendingInteraction(
  state: ChatRuntimeState,
  action: Extract<ChatRuntimeAction, { type: 'RESTORE_PENDING_INTERACTION' }>,
): ChatRuntimeState {
  const session = ensureSession(state, action.sessionId);
  const run = state.runs[action.clientRunKey];
  const restoredRound: RoundData = {
    ...action.round,
    status: 'waiting_interaction',
    completed_at: undefined,
    interrupt: action.interrupt,
  };
  let matched = false;
  const rounds = session.rounds.map((round) => {
    if (
      round.round_id !== restoredRound.round_id
      && (!run || !roundMatchesRun(round, run, restoredRound.round_id))
    ) {
      return round;
    }
    matched = true;
    return restoredRound;
  });
  const nextSession: ChatSessionRuntimeState = {
    ...session,
    rounds: matched ? rounds : [...rounds, restoredRound],
    pendingInterrupt: action.interrupt,
    activeRunKeys: session.activeRunKeys.filter((key) => key !== action.clientRunKey),
    agentStateByRunKey: {
      ...session.agentStateByRunKey,
      [action.clientRunKey]: {
        ...(session.agentStateByRunKey[action.clientRunKey] || emptyAgentState()),
        status: 'waiting',
        lastUpdated: Date.now(),
      },
    },
  };
  const nextState = putSession(state, action.sessionId, nextSession);
  if (!run) return nextState;
  return putRun(nextState, action.clientRunKey, {
    ...run,
    serverRunId: restoredRound.round_id,
    status: 'waiting',
    backendTerminal: undefined,
    buffers: emptyBuffers(),
    updatedAt: Date.now(),
  });
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
  const reconciledRunKeys = new Set<string>();

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
    reconciledRunKeys.add(runKey);

    const serverIsTerminal = TERMINAL_ROUND_STATUSES.has(serverRound.status);
    const useServerTerminal = serverIsTerminal && (
      !localRound || isServerRoundNewer(localRound, serverRound, run)
    );
    const serverSequence = serverRound.last_event_sequence || 0;
    const historyHasMaterializedLocalSegments = !hasDirtyStreamSegments(run.buffers)
      || historyMaterializesDirtySegments(serverRound, run.buffers);
    const serverLiveStateIsCurrent = !serverIsTerminal && (
      !localRound || serverSequence >= run.lastSequence
    );
    const useServerLiveProjection = serverLiveStateIsCurrent
      && (!localRound || historyHasMaterializedLocalSegments);
    const liveRunStatus = serverRound.status === 'waiting_interaction'
      ? 'waiting'
      : 'streaming';

    nextRuns[runKey] = {
      ...run,
      serverRunId: serverRound.round_id,
      lastSequence: useServerTerminal || useServerLiveProjection
        ? Math.max(run.lastSequence, serverSequence)
        : run.lastSequence,
      lastInteractionSequence: serverRound.status === 'waiting_interaction'
        && typeof serverRound.last_event_sequence === 'number'
        ? Math.max(run.lastInteractionSequence || 0, serverRound.last_event_sequence)
        : run.lastInteractionSequence,
      status: useServerTerminal
        ? runStatusFromRoundStatus(serverRound.status)
        : serverLiveStateIsCurrent
          ? liveRunStatus
          : run.status,
      // A higher global cursor can belong to another interleaved segment whose
      // durable START reached history before this segment's aggregate END.
      // Replace dirty buffers only when this snapshot proves their prefixes
      // were materialized, not merely because some event advanced the cursor.
      buffers: useServerTerminal || useServerLiveProjection
        ? emptyBuffers()
        : run.buffers,
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
    } else if (serverLiveStateIsCurrent) {
      if (serverRound.status === 'running') {
        activeRunKeys.add(runKey);
      } else {
        activeRunKeys.delete(runKey);
      }
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
          ? mergeActiveRound(
              localRound,
              serverRound,
              serverLiveStateIsCurrent ? serverRound.status : localRound.status,
              useServerLiveProjection,
            )
          : round
      ));
    }
  }

  for (const round of serverRounds) {
    if (round.status === 'running' || round.status === 'waiting_interaction') {
      const runKey = nextServerRunMap[round.round_id] || `run:${round.round_id}`;
      if (reconciledRunKeys.has(runKey)) {
        continue;
      }
      const effectiveRound = rounds.find((candidate) => candidate.round_id === round.round_id);
      const effectiveStatus = effectiveRound?.status === 'running'
        || effectiveRound?.status === 'waiting_interaction'
        ? effectiveRound.status
        : round.status;
      if (effectiveStatus === 'running') {
        activeRunKeys.add(runKey);
      } else {
        activeRunKeys.delete(runKey);
      }
      if (!nextRuns[runKey]) {
        nextRuns[runKey] = createRun({
          ownerSessionId: sessionId,
          clientRunKey: runKey,
          tempRoundId: round.round_id,
          source: 'history',
          status: effectiveStatus === 'running' ? 'streaming' : 'waiting',
        });
      }
      nextRuns[runKey] = {
        ...nextRuns[runKey],
        serverRunId: round.round_id,
        lastSequence: Math.max(nextRuns[runKey].lastSequence, round.last_event_sequence || 0),
        lastInteractionSequence: effectiveStatus === 'waiting_interaction'
          && typeof round.last_event_sequence === 'number'
          ? Math.max(nextRuns[runKey].lastInteractionSequence || 0, round.last_event_sequence)
          : nextRuns[runKey].lastInteractionSequence,
        status: effectiveStatus === 'running' ? 'streaming' : 'waiting',
        updatedAt: loadedAt,
      };
      nextServerRunMap[round.round_id] = runKey;
      nextServerRoundMap[round.round_id] = round.round_id;
      continue;
    }

    if (TERMINAL_ROUND_STATUSES.has(round.status)) {
      const runKey = nextServerRunMap[round.round_id];
      const existingRun = runKey ? nextRuns[runKey] : undefined;
      if (runKey && existingRun && !activeRunKeys.has(runKey)) {
        nextRuns[runKey] = {
          ...existingRun,
          serverRunId: round.round_id,
          lastSequence: Math.max(existingRun.lastSequence, round.last_event_sequence || 0),
          status: runStatusFromRoundStatus(round.status),
          buffers: emptyBuffers(),
          updatedAt: loadedAt,
        };
        activeRunKeys.delete(runKey);
      }
    }
  }

  const waitingRound = [...rounds].reverse().find((round) => (
    round.status === 'waiting_interaction' && round.interrupt
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
    pendingInterrupt: hasRunningRound ? null : (waitingRound?.interrupt || null),
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

function mergeActiveRound(
  localRound: RoundData,
  serverRound: RoundData,
  status: RoundData['status'],
  useServerProjection: boolean = false,
): RoundData {
  return {
    ...serverRound,
    round_id: serverRound.round_id,
    user_message: localRound.user_message || serverRound.user_message,
    user_attachments: (serverRound.user_attachments?.length || 0) > 0
      ? serverRound.user_attachments
      : localRound.user_attachments,
    preferred_skills: serverRound.preferred_skills ?? localRound.preferred_skills,
    preferred_mcp_connections: serverRound.preferred_mcp_connections
      ?? localRound.preferred_mcp_connections,
    final_response: useServerProjection
      ? serverRound.final_response
      : (localRound.final_response || serverRound.final_response),
    steps: useServerProjection
      ? serverRound.steps
      : (localRound.steps.length > 0 ? localRound.steps : serverRound.steps),
    step_count: useServerProjection
      ? serverRound.step_count
      : Math.max(localRound.step_count || 0, serverRound.step_count || 0),
    status,
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
  // Equal sequence means history has materialized the same durable terminal
  // boundary the stream cursor already observed; it is not a stale snapshot.
  if ((serverRound.last_event_sequence || 0) >= run.lastSequence) {
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
  if (eventType === RUNTIME_HISTORY_SNAPSHOT && Array.isArray(event.rounds)) {
    return applyHistoryLoaded(
      state,
      envelope.ownerSessionId,
      event.rounds,
      envelope.receivedAt,
    );
  }
  const run = state.runs[envelope.clientRunKey];
  if (!run) {
    return state;
  }

  const sequence = envelope.sequence;
  const reconcilesCurrentAggregate = typeof sequence === 'number'
    && sequence === run.lastSequence
    && aggregateMatchesBufferedSegment(run, envelope);
  if (
    typeof sequence === 'number'
    && sequence <= run.lastSequence
    && !envelope.authoritativeRecovery
    && !reconcilesCurrentAggregate
  ) {
    return state;
  }

  if (
    eventType === 'RUN_ERROR'
    && typeof sequence !== 'number'
    && !envelope.authoritativeRecovery
    && typeof run.lastInteractionSequence === 'number'
  ) {
    return putRun(state, run.clientRunKey, {
      ...run,
      debugMetadata: {
        ...run.debugMetadata,
        droppedUnsequencedRunErrors:
          (run.debugMetadata?.droppedUnsequencedRunErrors || 0) + 1,
      },
      updatedAt: envelope.receivedAt,
    });
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
    lastSequence: typeof sequence === 'number'
      ? Math.max(run.lastSequence, sequence)
      : run.lastSequence,
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
          textSegmentStateByMessageId: {
            ...nextRun.buffers.textSegmentStateByMessageId,
            [event.messageId]: {
              open: true,
              dirty: nextRun.buffers.textSegmentStateByMessageId[event.messageId]?.dirty || false,
            },
          },
        },
      };
      break;
    case 'TEXT_MESSAGE_CONTENT':
      nextRun = applyTextDelta(nextRun, envelope);
      nextState = updateRound(nextState, nextRun, (round) => updateRoundTextContent(round, latestText(nextRun)));
      break;
    case 'TEXT_MESSAGE_END':
      nextRun = {
        ...nextRun,
        buffers: {
          ...nextRun.buffers,
          currentTextMessageId: null,
          textSegmentStateByMessageId: closeSegment(
            nextRun.buffers.textSegmentStateByMessageId,
            event.messageId || nextRun.buffers.currentTextMessageId,
          ),
        },
      };
      break;
    case 'THINKING_TEXT_MESSAGE_START':
      nextRun = {
        ...nextRun,
        buffers: {
          ...nextRun.buffers,
          currentThinkingMessageId: event.messageId,
          thinkingByMessageId: { ...nextRun.buffers.thinkingByMessageId, [event.messageId]: '' },
          thinkingSegmentStateByMessageId: {
            ...nextRun.buffers.thinkingSegmentStateByMessageId,
            [event.messageId]: {
              open: true,
              dirty: nextRun.buffers.thinkingSegmentStateByMessageId[event.messageId]?.dirty || false,
            },
          },
        },
      };
      nextState = updateLastStep(nextState, nextRun, (step) => ({ ...step, thinking_start_ts: event.timestamp }));
      break;
    case 'THINKING_TEXT_MESSAGE_CONTENT':
      nextRun = applyThinkingDelta(nextRun, envelope);
      nextState = updateLastStep(nextState, nextRun, (step) => ({ ...step, thinking: latestThinking(nextRun) }));
      break;
    case 'THINKING_TEXT_MESSAGE_END':
      nextRun = {
        ...nextRun,
        buffers: {
          ...nextRun.buffers,
          currentThinkingMessageId: null,
          thinkingSegmentStateByMessageId: closeSegment(
            nextRun.buffers.thinkingSegmentStateByMessageId,
            event.messageId || nextRun.buffers.currentThinkingMessageId,
          ),
        },
      };
      nextState = updateLastStep(nextState, nextRun, (step) => ({ ...step, thinking_end_ts: event.timestamp }));
      break;
    case 'TOOL_CALL_START':
      nextRun = {
        ...nextRun,
        buffers: {
          ...nextRun.buffers,
          toolArgsByToolCallId: {
            ...nextRun.buffers.toolArgsByToolCallId,
            [event.toolCallId]: Object.prototype.hasOwnProperty.call(
              nextRun.buffers.toolArgsByToolCallId,
              event.toolCallId,
            )
              ? nextRun.buffers.toolArgsByToolCallId[event.toolCallId]
              : '',
          },
          toolArgsSegmentStateByToolCallId: {
            ...nextRun.buffers.toolArgsSegmentStateByToolCallId,
            [event.toolCallId]: {
              open: true,
              dirty: nextRun.buffers.toolArgsSegmentStateByToolCallId[event.toolCallId]?.dirty || false,
            },
          },
        },
      };
      nextState = upsertToolCallStart(
        nextState,
        nextRun,
        event.toolCallId,
        event.toolCallName,
        event.timestamp,
      );
      nextState = updateAgentToolLog(nextState, nextRun, {
        toolCallId: event.toolCallId,
        toolName: event.toolCallName,
        status: 'running',
        startedAt: event.timestamp ?? envelope.receivedAt,
      });
      break;
    case 'TOOL_CALL_ARGS':
      nextRun = applyToolArgsDelta(nextRun, envelope);
      nextState = updateToolArgs(nextState, nextRun, event.toolCallId);
      break;
    case 'TOOL_CALL_END':
      nextRun = {
        ...nextRun,
        buffers: {
          ...nextRun.buffers,
          toolArgsSegmentStateByToolCallId: closeSegment(
            nextRun.buffers.toolArgsSegmentStateByToolCallId,
            event.toolCallId,
          ),
        },
      };
      nextState = updateToolEnd(nextState, nextRun, event.toolCallId, event.timestamp);
      break;
    case 'TOOL_CALL_RESULT':
      nextRun = {
        ...nextRun,
        buffers: {
          ...nextRun.buffers,
          toolArgsSegmentStateByToolCallId: closeSegment(
            nextRun.buffers.toolArgsSegmentStateByToolCallId,
            event.toolCallId,
          ),
        },
      };
      nextState = updateToolResult(nextState, nextRun, event.toolCallId, event.content, event.timestamp, event.executionTimeMs);
      break;
    case 'CUSTOM':
      if (event.name === 'interaction_requested') {
        return applyInteractionRequested(nextState, envelope, nextRun);
      }
      if (event.name === 'interaction_resolved') {
        return applyInteractionResolved(nextState, envelope, nextRun);
      }
      if (event.name === 'assistant_file_referenced') {
        const value = event.value && typeof event.value === 'object'
          ? event.value as Record<string, unknown>
          : null;
        if (
          value?.source === 'session'
          && typeof value.operation === 'string'
          && value.operation.toUpperCase() === 'DELETED'
          && typeof value.session_id === 'string'
          && typeof value.path === 'string'
        ) {
          nextState = updateRound(nextState, nextRun, (round) => (
            removeAssistantSessionReference(round, value.session_id as string, value.path as string)
          ));
          break;
        }
        const reference = normalizeAssistantFileReference(event.value);
        if (reference) {
          nextState = updateRound(nextState, nextRun, (round) => (
            appendAssistantFileReference(round, reference)
          ));
        }
      }
      if (event.name === 'workspace_resource_changed') {
        const value = event.value && typeof event.value === 'object'
          ? event.value as Record<string, unknown>
          : null;
        const operation = typeof value?.operation === 'string'
          ? value.operation.toUpperCase()
          : '';
        if (
          typeof value?.entry_id === 'string'
          && operation === 'DELETED'
        ) {
          const deletedIds = Array.isArray(value.affected_entry_ids)
            ? value.affected_entry_ids.filter((id): id is string => typeof id === 'string')
            : [value.entry_id];
          nextState = updateRound(nextState, nextRun, (round) => (
            deletedIds.reduce(removeAssistantWorkspaceReference, round)
          ));
          break;
        }
        const reference = normalizeAssistantFileReference(
          value?.assistant_file_reference,
        );
        if (reference) {
          nextState = updateRound(nextState, nextRun, (round) => (
            appendAssistantFileReference(round, reference)
          ));
        }
      }
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

  const ownershipTransfer = transferRunOwnership(state, serverRunId, run);
  state = ownershipTransfer.state;
  run = ownershipTransfer.run;

  const nextRun: ChatRunRuntimeState = {
    ...run,
    serverRunId,
    status: 'streaming',
  };
  const session = ensureSession(state, run.ownerSessionId);
  const rounds = reconcileRunStartedRounds(
    session.rounds,
    run,
    ownershipTransfer.previousRun,
    serverRunId,
    envelope.event.preferredSkills,
    envelope.event.preferredMcpConnections,
  );
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

function transferRunOwnership(
  state: ChatRuntimeState,
  serverRunId: string,
  incomingRun: ChatRunRuntimeState,
): {
  state: ChatRuntimeState;
  run: ChatRunRuntimeState;
  previousRun?: ChatRunRuntimeState;
} {
  const previousRunKey = state.serverRunIdToClientRunKey[serverRunId];
  if (!previousRunKey || previousRunKey === incomingRun.clientRunKey) {
    return { state, run: incomingRun };
  }

  const previousRun = state.runs[previousRunKey];
  if (!previousRun || previousRun.ownerSessionId !== incomingRun.ownerSessionId) {
    return { state, run: incomingRun };
  }

  const mergedRun: ChatRunRuntimeState = {
    ...incomingRun,
    serverRunId,
    lastSequence: Math.max(previousRun.lastSequence, incomingRun.lastSequence),
    buffers: {
      textByMessageId: {
        ...previousRun.buffers.textByMessageId,
        ...incomingRun.buffers.textByMessageId,
      },
      thinkingByMessageId: {
        ...previousRun.buffers.thinkingByMessageId,
        ...incomingRun.buffers.thinkingByMessageId,
      },
      toolArgsByToolCallId: {
        ...previousRun.buffers.toolArgsByToolCallId,
        ...incomingRun.buffers.toolArgsByToolCallId,
      },
      textSegmentStateByMessageId: {
        ...previousRun.buffers.textSegmentStateByMessageId,
        ...incomingRun.buffers.textSegmentStateByMessageId,
      },
      thinkingSegmentStateByMessageId: {
        ...previousRun.buffers.thinkingSegmentStateByMessageId,
        ...incomingRun.buffers.thinkingSegmentStateByMessageId,
      },
      toolArgsSegmentStateByToolCallId: {
        ...previousRun.buffers.toolArgsSegmentStateByToolCallId,
        ...incomingRun.buffers.toolArgsSegmentStateByToolCallId,
      },
      currentTextMessageId:
        incomingRun.buffers.currentTextMessageId
        ?? previousRun.buffers.currentTextMessageId,
      currentThinkingMessageId:
        incomingRun.buffers.currentThinkingMessageId
        ?? previousRun.buffers.currentThinkingMessageId,
    },
    createdAt: Math.min(previousRun.createdAt, incomingRun.createdAt),
    updatedAt: Math.max(previousRun.updatedAt, incomingRun.updatedAt),
  };

  const session = ensureSession(state, incomingRun.ownerSessionId);
  const agentStateByRunKey = { ...session.agentStateByRunKey };
  const previousAgentState = agentStateByRunKey[previousRunKey];
  const incomingAgentState = agentStateByRunKey[incomingRun.clientRunKey];
  if (previousRun.lastSequence > incomingRun.lastSequence && previousAgentState) {
    agentStateByRunKey[incomingRun.clientRunKey] = previousAgentState;
  } else if (!incomingAgentState && previousAgentState) {
    agentStateByRunKey[incomingRun.clientRunKey] = previousAgentState;
  }
  delete agentStateByRunKey[previousRunKey];

  const runs = { ...state.runs };
  delete runs[previousRunKey];
  runs[incomingRun.clientRunKey] = mergedRun;

  const serverRunIdToClientRunKey = { ...state.serverRunIdToClientRunKey };
  for (const [mappedServerRunId, clientRunKey] of Object.entries(serverRunIdToClientRunKey)) {
    if (clientRunKey === previousRunKey) {
      serverRunIdToClientRunKey[mappedServerRunId] = incomingRun.clientRunKey;
    }
  }
  serverRunIdToClientRunKey[serverRunId] = incomingRun.clientRunKey;

  const idempotencyKeyToClientRunKey = { ...state.idempotencyKeyToClientRunKey };
  for (const [idempotencyKey, clientRunKey] of Object.entries(idempotencyKeyToClientRunKey)) {
    if (clientRunKey === previousRunKey) {
      idempotencyKeyToClientRunKey[idempotencyKey] = incomingRun.clientRunKey;
    }
  }

  const nextSession: ChatSessionRuntimeState = {
    ...session,
    activeRunKeys: unique([
      ...session.activeRunKeys.map((runKey) => (
        runKey === previousRunKey ? incomingRun.clientRunKey : runKey
      )),
      incomingRun.clientRunKey,
    ]),
    agentStateByRunKey,
    visibleAgentStateRunKey:
      session.visibleAgentStateRunKey === previousRunKey
        ? incomingRun.clientRunKey
        : session.visibleAgentStateRunKey,
  };

  return {
    state: putSession({
      ...state,
      runs,
      serverRunIdToClientRunKey,
      idempotencyKeyToClientRunKey,
    }, incomingRun.ownerSessionId, nextSession),
    run: mergedRun,
    previousRun,
  };
}

function reconcileRunStartedRounds(
  rounds: RoundData[],
  run: ChatRunRuntimeState,
  previousRun: ChatRunRuntimeState | undefined,
  serverRunId: string,
  preferredSkills: unknown,
  preferredMcpConnections: unknown,
): RoundData[] {
  const matchingRoundIds = new Set([
    serverRunId,
    run.tempRoundId,
    run.serverRunId,
    previousRun?.tempRoundId,
    previousRun?.serverRunId,
  ].filter((roundId): roundId is string => Boolean(roundId)));
  const matchingIndexes = rounds
    .map((round, index) => (matchingRoundIds.has(round.round_id) ? index : -1))
    .filter((index) => index >= 0);

  if (matchingIndexes.length === 0) {
    return rounds;
  }

  const localRound = rounds.find((round) => round.round_id === run.tempRoundId);
  const serverRound = rounds.find((round) => round.round_id === serverRunId)
    || rounds[matchingIndexes[0]];
  const mergedRound = localRound && localRound !== serverRound
    ? mergeActiveRound(localRound, serverRound, localRound.status)
    : { ...(localRound || serverRound) };
  const reconciledRound: RoundData = {
    ...mergedRound,
    round_id: serverRunId,
    status: 'running',
    preferred_skills: Array.isArray(preferredSkills)
      ? preferredSkills
      : mergedRound.preferred_skills,
    preferred_mcp_connections: Array.isArray(preferredMcpConnections)
      ? preferredMcpConnections
      : mergedRound.preferred_mcp_connections,
  };
  const targetIndex = matchingIndexes[0];
  const matchedIndexSet = new Set(matchingIndexes);

  return rounds.flatMap((round, index) => {
    if (index === targetIndex) {
      return [reconciledRound];
    }
    return matchedIndexSet.has(index) ? [] : [round];
  });
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
      : null,
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

function applyInteractionRequested(
  state: ChatRuntimeState,
  envelope: StreamEnvelope,
  run: ChatRunRuntimeState,
): ChatRuntimeState {
  const value = envelope.event?.value && typeof envelope.event.value === 'object'
    ? envelope.event.value
    : {};
  const runId = value.runId || run.serverRunId || run.tempRoundId;
  const kind = value.kind || 'user_input';
  const payload = value.payload && typeof value.payload === 'object' ? value.payload : {};
  const interrupt: InterruptDetails = {
    id: value.interactionId,
    reason: kind === 'tool_approval' ? 'human_approval' : 'input_required',
    payload: {
      ...payload,
      kind,
      tool_call_id: value.toolCallId,
      run_id: runId,
    },
  };
  const session = ensureSession(state, run.ownerSessionId);
  const rounds = session.rounds.map((round) => (
    roundMatchesRun(round, run, runId)
      ? {
          ...round,
          round_id: runId,
          status: 'waiting_interaction',
          completed_at: undefined,
          interrupt,
        }
      : round
  ));
  const nextRun: ChatRunRuntimeState = {
    ...run,
    serverRunId: runId,
    status: 'waiting',
    lastInteractionSequence: typeof envelope.sequence === 'number'
      ? Math.max(run.lastInteractionSequence || 0, envelope.sequence)
      : run.lastInteractionSequence,
    updatedAt: envelope.receivedAt,
  };
  const nextSession: ChatSessionRuntimeState = {
    ...session,
    rounds,
    pendingInterrupt: interrupt,
    activeRunKeys: session.activeRunKeys.filter((key) => key !== run.clientRunKey),
    agentStateByRunKey: {
      ...session.agentStateByRunKey,
      [run.clientRunKey]: {
        ...(session.agentStateByRunKey[run.clientRunKey] || emptyAgentState()),
        status: 'waiting',
        lastUpdated: Date.now(),
      },
    },
  };
  const nextState = putSession(state, run.ownerSessionId, nextSession);
  return putRun(nextState, run.clientRunKey, nextRun);
}

function applyInteractionResolved(
  state: ChatRuntimeState,
  envelope: StreamEnvelope,
  run: ChatRunRuntimeState,
): ChatRuntimeState {
  const value = envelope.event?.value && typeof envelope.event.value === 'object'
    ? envelope.event.value
    : {};
  const runId = value.runId || run.serverRunId || run.tempRoundId;
  const session = ensureSession(state, run.ownerSessionId);
  const rounds = session.rounds.map((round) => (
    roundMatchesRun(round, run, runId)
      ? {
          ...round,
          round_id: runId,
          status: 'running',
          interrupt: undefined,
        }
      : round
  ));
  let nextState = putSession(state, run.ownerSessionId, {
    ...session,
    rounds,
    pendingInterrupt: null,
    activeRunKeys: unique([...session.activeRunKeys, run.clientRunKey]),
    agentStateByRunKey: {
      ...session.agentStateByRunKey,
      [run.clientRunKey]: {
        ...(session.agentStateByRunKey[run.clientRunKey] || emptyAgentState()),
        status: 'running',
        lastUpdated: Date.now(),
      },
    },
    visibleAgentStateRunKey: run.clientRunKey,
  });
  const nextRun: ChatRunRuntimeState = {
    ...run,
    serverRunId: runId,
    status: 'streaming',
    lastInteractionSequence: typeof envelope.sequence === 'number'
      ? Math.max(run.lastInteractionSequence || 0, envelope.sequence)
      : run.lastInteractionSequence,
    updatedAt: envelope.receivedAt,
  };
  nextState = putRun(nextState, run.clientRunKey, nextRun);
  if (typeof value.toolCallId === 'string' && typeof value.toolResultContent === 'string') {
    nextState = updateToolResult(
      nextState,
      nextRun,
      value.toolCallId,
      value.toolResultContent,
      envelope.event.timestamp,
      0,
    );
  }
  return nextState;
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
        pendingInterrupt: null,
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
    pendingInterrupt: null,
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

function clearLocalInitSlot(
  state: ChatRuntimeState,
  sessionId: string,
): ChatRuntimeState {
  const session = ensureSession(state, sessionId);
  const initRunKeys = session.activeRunKeys.filter(
    (key) => state.runs[key]?.source === 'init',
  );
  if (initRunKeys.length === 0) return state;

  const initRunKeySet = new Set(initRunKeys);
  const nextRuns = { ...state.runs };
  for (const key of initRunKeys) {
    nextRuns[key] = {
      ...nextRuns[key],
      status: 'stale',
      updatedAt: Date.now(),
    };
  }
  return putSession(
    { ...state, runs: nextRuns },
    sessionId,
    {
      ...session,
      activeRunKeys: session.activeRunKeys.filter((key) => !initRunKeySet.has(key)),
    },
  );
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
    const mappedRunKey = item.round_id
      ? nextState.serverRunIdToClientRunKey[item.round_id]
      : undefined;
    const matchingActiveRunKey = item.round_id
      ? session.activeRunKeys.find((key) => {
          const run = nextState.runs[key];
          return run?.ownerSessionId === item.session_id
            && (run.serverRunId === item.round_id || run.tempRoundId === item.round_id);
        })
      : undefined;
    const runKey = mappedRunKey
      || matchingActiveRunKey
      || (item.round_id ? `run:${item.round_id}` : `init:${item.session_id}`);
    if (!nextState.runs[runKey]) {
      nextState = putRun(nextState, runKey, createRun({
        ownerSessionId: item.session_id,
        clientRunKey: runKey,
        tempRoundId: item.round_id || runKey,
        source: item.round_id ? 'history' : 'init',
        status: item.round_id ? 'streaming' : 'starting',
      }));
    }
    if (item.round_id) {
      const run = nextState.runs[runKey];
      if (run.serverRunId !== item.round_id) {
        nextState = putRun(nextState, runKey, {
          ...run,
          serverRunId: item.round_id,
        });
      }
      if (nextState.serverRunIdToClientRunKey[item.round_id] !== runKey) {
        nextState = {
          ...nextState,
          serverRunIdToClientRunKey: {
            ...nextState.serverRunIdToClientRunKey,
            [item.round_id]: runKey,
          },
        };
      }
    }
    const currentSession = ensureSession(nextState, item.session_id);
    if (!currentSession.activeRunKeys.includes(runKey)) {
      nextState = putSession(nextState, item.session_id, {
        ...currentSession,
        activeRunKeys: unique([...currentSession.activeRunKeys, runKey]),
      });
    }
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
  return updateAgentStateDeltaValue(state, run, (prev) => {
    const index = prev.toolLogs.findIndex((item) => item.toolCallId === log.toolCallId);
    if (index < 0) {
      return {
        ...prev,
        toolLogs: [...prev.toolLogs, log],
        lastUpdated: Date.now(),
      };
    }

    const statusRank = {
      running: 0,
      pending: 1,
      completed: 2,
      failed: 2,
    } as const;
    const existing = prev.toolLogs[index];
    const toolLogs = [...prev.toolLogs];
    toolLogs[index] = {
      ...existing,
      ...log,
      status: statusRank[existing.status] >= statusRank[log.status]
        ? existing.status
        : log.status,
      startedAt: existing.startedAt ?? log.startedAt,
      completedAt: existing.completedAt ?? log.completedAt,
    };
    return {
      ...prev,
      toolLogs,
      lastUpdated: Date.now(),
    };
  });
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

function updateStepContainingToolCall(
  round: RoundData,
  toolCallId: string,
  updater: (step: StepData) => StepData,
): RoundData {
  let found = false;
  const steps = round.steps.map((step) => {
    if (found || !step.tool_calls.some((toolCall) => toolCall.id === toolCallId)) {
      return step;
    }
    found = true;
    return updater(step);
  });
  return found ? { ...round, steps } : round;
}

function updateToolCallById(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  toolCallId: string,
  updater: (toolCall: StepData['tool_calls'][number]) => StepData['tool_calls'][number],
): ChatRuntimeState {
  return updateRound(state, run, (round) => updateStepContainingToolCall(
    round,
    toolCallId,
    (step) => {
      const toolIndex = step.tool_calls.findIndex((toolCall) => toolCall.id === toolCallId);
      const toolCalls = [...step.tool_calls];
      toolCalls[toolIndex] = updater(toolCalls[toolIndex]);
      return { ...step, tool_calls: toolCalls };
    },
  ));
}

function upsertToolCallStart(
  state: ChatRuntimeState,
  run: ChatRunRuntimeState,
  toolCallId: string,
  toolCallName: string,
  timestamp?: number,
): ChatRuntimeState {
  return updateRound(state, run, (round) => {
    const ensured = ensureStep(round);
    let found = false;
    const steps = ensured.steps.map((step) => {
      if (found) {
        return step;
      }
      const toolIndex = step.tool_calls.findIndex((toolCall) => toolCall.id === toolCallId);
      if (toolIndex < 0) {
        return step;
      }
      found = true;
      const toolCalls = [...step.tool_calls];
      const existing = toolCalls[toolIndex];
      toolCalls[toolIndex] = {
        ...existing,
        name: toolCallName || existing.name,
        started_at_ts: existing.started_at_ts ?? timestamp,
      };
      return { ...step, tool_calls: toolCalls };
    });

    if (found) {
      return { ...ensured, steps };
    }

    const lastStepIndex = steps.length - 1;
    const lastStep = steps[lastStepIndex];
    steps[lastStepIndex] = {
      ...lastStep,
      tool_calls: [
        ...lastStep.tool_calls,
        {
          id: toolCallId,
          name: toolCallName,
          input: {},
          started_at_ts: timestamp,
        },
      ],
    };
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
  const previousSegment = run.buffers.textSegmentStateByMessageId[messageId];
  return {
    ...run,
    buffers: {
      ...run.buffers,
      currentTextMessageId: messageId,
      textByMessageId: { ...run.buffers.textByMessageId, [messageId]: next },
      textSegmentStateByMessageId: {
        ...run.buffers.textSegmentStateByMessageId,
        [messageId]: {
          open: envelope.isAggregate ? previousSegment?.open || false : true,
          dirty: envelope.isAggregate
            ? false
            : previousSegment?.dirty || typeof envelope.sequence !== 'number',
        },
      },
    },
  };
}

function applyThinkingDelta(run: ChatRunRuntimeState, envelope: StreamEnvelope): ChatRunRuntimeState {
  const messageId = envelope.messageId || envelope.event.messageId || run.buffers.currentThinkingMessageId || 'default';
  const prev = run.buffers.thinkingByMessageId[messageId] || '';
  const next = envelope.isAggregate ? envelope.event.delta : prev + (envelope.event.delta || '');
  const previousSegment = run.buffers.thinkingSegmentStateByMessageId[messageId];
  return {
    ...run,
    buffers: {
      ...run.buffers,
      currentThinkingMessageId: messageId,
      thinkingByMessageId: { ...run.buffers.thinkingByMessageId, [messageId]: next },
      thinkingSegmentStateByMessageId: {
        ...run.buffers.thinkingSegmentStateByMessageId,
        [messageId]: {
          open: envelope.isAggregate ? previousSegment?.open || false : true,
          dirty: envelope.isAggregate
            ? false
            : previousSegment?.dirty || typeof envelope.sequence !== 'number',
        },
      },
    },
  };
}

function applyToolArgsDelta(run: ChatRunRuntimeState, envelope: StreamEnvelope): ChatRunRuntimeState {
  const toolCallId = envelope.toolCallId || envelope.event.toolCallId;
  const prev = run.buffers.toolArgsByToolCallId[toolCallId] || '';
  const next = envelope.isAggregate ? envelope.event.delta : prev + (envelope.event.delta || '');
  const previousSegment = run.buffers.toolArgsSegmentStateByToolCallId[toolCallId];
  return {
    ...run,
    buffers: {
      ...run.buffers,
      toolArgsByToolCallId: {
        ...run.buffers.toolArgsByToolCallId,
        [toolCallId]: next,
      },
      toolArgsSegmentStateByToolCallId: {
        ...run.buffers.toolArgsSegmentStateByToolCallId,
        [toolCallId]: {
          open: envelope.isAggregate ? previousSegment?.open || false : true,
          dirty: envelope.isAggregate
            ? false
            : previousSegment?.dirty || typeof envelope.sequence !== 'number',
        },
      },
    },
  };
}

function hasDirtyStreamSegments(buffers: ChatRunRuntimeState['buffers']): boolean {
  return [
    ...Object.values(buffers.textSegmentStateByMessageId),
    ...Object.values(buffers.thinkingSegmentStateByMessageId),
    ...Object.values(buffers.toolArgsSegmentStateByToolCallId),
  ].some((segment) => segment.dirty);
}

function historyMaterializesDirtySegments(
  serverRound: RoundData,
  buffers: ChatRunRuntimeState['buffers'],
): boolean {
  const textCandidates = serverRound.steps.map((step) => step.assistant_content || '');
  const thinkingCandidates = serverRound.steps.map((step) => step.thinking || '');

  const prefixesAreMaterialized = (
    values: Record<string, string>,
    segments: Record<string, { open: boolean; dirty: boolean }>,
    candidates: string[],
    currentSegmentId?: string | null,
  ): boolean => {
    const remaining = candidates.filter(Boolean);
    for (const [segmentId, segment] of Object.entries(segments)) {
      if (!segment.dirty) continue;
      const prefix = values[segmentId] || '';
      if (!prefix) continue;
      if (segment.open && segmentId === currentSegmentId) {
        const currentCandidate = candidates[candidates.length - 1] || '';
        if (!currentCandidate.startsWith(prefix)) return false;
        const currentCandidateIndex = remaining.lastIndexOf(currentCandidate);
        if (currentCandidateIndex >= 0) {
          remaining.splice(currentCandidateIndex, 1);
        }
        continue;
      }
      const matchIndex = remaining.findIndex((candidate) => candidate.startsWith(prefix));
      if (matchIndex < 0) return false;
      remaining.splice(matchIndex, 1);
    }
    return true;
  };

  if (!prefixesAreMaterialized(
    buffers.textByMessageId,
    buffers.textSegmentStateByMessageId,
    textCandidates,
    buffers.currentTextMessageId,
  )) {
    return false;
  }
  if (!prefixesAreMaterialized(
    buffers.thinkingByMessageId,
    buffers.thinkingSegmentStateByMessageId,
    thinkingCandidates,
    buffers.currentThinkingMessageId,
  )) {
    return false;
  }

  const persistedToolCallIds = new Set(
    serverRound.steps.flatMap((step) => (
      step.tool_calls
        .map((toolCall) => toolCall.id)
        .filter((toolCallId): toolCallId is string => Boolean(toolCallId))
    )),
  );
  return Object.entries(buffers.toolArgsSegmentStateByToolCallId).every(
    ([toolCallId, segment]) => !segment.dirty
      || !(buffers.toolArgsByToolCallId[toolCallId] || '')
      || persistedToolCallIds.has(toolCallId),
  );
}

function aggregateMatchesBufferedSegment(
  run: ChatRunRuntimeState,
  envelope: StreamEnvelope,
): boolean {
  if (!envelope.isAggregate) return false;
  const aggregate = String(envelope.event?.delta || '');
  const eventType = envelope.event?.type;
  if (eventType === 'TEXT_MESSAGE_CONTENT') {
    const messageId = envelope.messageId
      || envelope.event.messageId
      || run.buffers.currentTextMessageId
      || 'default';
    const segment = run.buffers.textSegmentStateByMessageId[messageId];
    return Boolean(segment && aggregate.startsWith(run.buffers.textByMessageId[messageId] || ''));
  }
  if (eventType === 'THINKING_TEXT_MESSAGE_CONTENT') {
    const messageId = envelope.messageId
      || envelope.event.messageId
      || run.buffers.currentThinkingMessageId
      || 'default';
    const segment = run.buffers.thinkingSegmentStateByMessageId[messageId];
    return Boolean(segment && aggregate.startsWith(run.buffers.thinkingByMessageId[messageId] || ''));
  }
  if (eventType === 'TOOL_CALL_ARGS') {
    const toolCallId = envelope.toolCallId || envelope.event.toolCallId;
    const segment = run.buffers.toolArgsSegmentStateByToolCallId[toolCallId];
    return Boolean(segment && aggregate.startsWith(run.buffers.toolArgsByToolCallId[toolCallId] || ''));
  }
  return false;
}

function closeSegment(
  segments: Record<string, { open: boolean; dirty: boolean }>,
  segmentId?: string | null,
): Record<string, { open: boolean; dirty: boolean }> {
  if (!segmentId) return segments;
  return {
    ...segments,
    [segmentId]: {
      open: false,
      dirty: segments[segmentId]?.dirty || false,
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
    return updateToolCallById(state, run, toolCallId, (toolCall) => ({
      ...toolCall,
      input: parsedArgs,
    }));
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
  let nextState = updateToolCallById(state, run, toolCallId, (toolCall) => ({
    ...toolCall,
    ended_at_ts: toolCall.ended_at_ts ?? timestamp,
  }));
  nextState = updateAgentStateDeltaValue(nextState, run, (prev) => ({
    ...prev,
    toolLogs: prev.toolLogs.map((log) => (
      log.toolCallId === toolCallId
        ? {
            ...log,
            status: log.status === 'completed' || log.status === 'failed'
              ? log.status
              : 'pending',
            args: run.buffers.toolArgsByToolCallId[toolCallId],
          }
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
  const toolResult: ToolResult = {
    ...resultObj,
    tool_call_id: toolCallId,
    received_at_ts: timestamp,
    execution_time_ms: executionTimeMs,
  };
  let nextState = updateRound(state, run, (round) => updateStepContainingToolCall(
    round,
    toolCallId,
    (step) => {
      const resultIndex = step.tool_results.findIndex(
        (result) => result.tool_call_id === toolCallId,
      );
      if (resultIndex < 0) {
        return {
          ...step,
          tool_results: [...step.tool_results, toolResult],
        };
      }
      const toolResults = [...step.tool_results];
      toolResults[resultIndex] = toolResult;
      return { ...step, tool_results: toolResults };
    },
  ));
  nextState = updateAgentStateDeltaValue(nextState, run, (prev) => ({
    ...prev,
    toolLogs: prev.toolLogs.map((log) => (
      log.toolCallId === toolCallId
        ? {
            ...log,
            status: 'completed',
            result: content,
            completedAt: log.completedAt ?? timestamp ?? Date.now(),
          }
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
  if (outcome === 'interrupt') return 'failed';
  if (outcome === 'success') return 'completed';
  return outcome;
}

function getRunFinishedCompletedAt(_outcome: string, _isUserCancelled: boolean, _result?: any): string | undefined {
  return new Date().toISOString();
}

function finalizeStepsForTerminal(steps: StepData[], hasFinalResponse: boolean): StepData[] {
  return steps.map((step) => ({
    ...step,
    status: step.status === 'streaming' || step.status === 'running' ? 'completed' : step.status,
    ...(hasFinalResponse ? { assistant_content: '' } : {}),
  }));
}
