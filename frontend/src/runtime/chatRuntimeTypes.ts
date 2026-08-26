import type {
  AgentState,
  AGUIEvent,
  ChatContentBlock,
  FileInfo,
  InterruptDetails,
  PreferredMcpConnectionSnapshot,
  RoundData,
  TurnReasoningSelection,
} from '../types';

export type StreamSource = 'direct' | 'subscribe' | 'resume';

/** Internal event used to project a complete history snapshot before advancing a run cursor. */
export const RUNTIME_HISTORY_SNAPSHOT = 'RUNTIME_HISTORY_SNAPSHOT' as const;

export interface RuntimeHistorySnapshotEvent {
  type: typeof RUNTIME_HISTORY_SNAPSHOT;
  rounds: RoundData[];
  sequence?: number;
}

export type RunStatus =
  | 'starting'
  | 'streaming'
  | 'waiting'
  | 'cancelled'
  | 'finished'
  | 'error'
  | 'stale';

export interface StreamEnvelope {
  ownerSessionId: string;
  clientRunKey: string;
  serverRunId?: string;
  transportEpoch: number;
  connectionId: string;
  event: AGUIEvent | any;
  source: StreamSource;
  /** Event synthesized from an authoritative history snapshot after transport loss. */
  authoritativeRecovery?: boolean;
  sequence?: number;
  isAggregate?: boolean;
  eventId?: string;
  messageId?: string;
  toolCallId?: string;
  receivedAt: number;
}

export interface HistoryLoadedAction {
  type: 'HISTORY_LOADED';
  sessionId: string;
  rounds: RoundData[];
  loadedAt: number;
  source: 'history';
}

export interface StreamSegmentState {
  open: boolean;
  dirty: boolean;
}

export interface StreamBuffers {
  textByMessageId: Record<string, string>;
  thinkingByMessageId: Record<string, string>;
  toolArgsByToolCallId: Record<string, string>;
  textSegmentStateByMessageId: Record<string, StreamSegmentState>;
  thinkingSegmentStateByMessageId: Record<string, StreamSegmentState>;
  toolArgsSegmentStateByToolCallId: Record<string, StreamSegmentState>;
  currentTextMessageId?: string | null;
  currentThinkingMessageId?: string | null;
}

export interface ChatRunRuntimeState {
  clientRunKey: string;
  ownerSessionId: string;
  tempRoundId: string;
  serverRunId?: string;
  idempotencyKey?: string;
  source: StreamSource | 'history' | 'init';
  status: RunStatus;
  lastSequence: number;
  /** Latest durable interaction_requested / interaction_resolved boundary. */
  lastInteractionSequence?: number;
  buffers: StreamBuffers;
  backendTerminal?: any;
  debugMetadata?: Record<string, any>;
  createdAt: number;
  updatedAt: number;
}

export interface ChatSessionRuntimeState {
  rounds: RoundData[];
  pendingInterrupt: InterruptDetails | null;
  error: string;
  loading: boolean;
  activeRunKeys: string[];
  agentStateByRunKey: Record<string, AgentState>;
  visibleAgentStateRunKey?: string | null;
  lastHistoryLoadedAt?: number;
  debugMetadata?: Record<string, any>;
}

export interface ChatRuntimeState {
  sessions: Record<string, ChatSessionRuntimeState>;
  runs: Record<string, ChatRunRuntimeState>;
  serverRunIdToClientRunKey: Record<string, string>;
  tempRoundIdToServerRoundId: Record<string, string>;
  serverRoundIdToLocalRoundId: Record<string, string>;
  idempotencyKeyToClientRunKey: Record<string, string>;
}

export type ChatRuntimeAction =
  | { type: 'SESSION_LOADING'; sessionId: string; loading: boolean }
  | { type: 'SESSION_ERROR'; sessionId: string; error: string }
  | { type: 'CLEAR_SESSION_VIEW'; sessionId: string }
  | { type: 'CLEAR_ERROR'; sessionId: string }
  | HistoryLoadedAction
  | {
      type: 'LOCAL_RUN_STARTED';
      sessionId: string;
      clientRunKey: string;
      tempRoundId: string;
      idempotencyKey?: string;
      source: StreamSource | 'init';
      round?: RoundData;
    }
  | { type: 'STREAM_EVENT'; envelope: StreamEnvelope }
  | {
      type: 'LOCAL_CONTROL_CONFLICT';
      sessionId: string;
      clientRunKey: string;
      serverRunId?: string;
    }
  | { type: 'LOCAL_CANCELLED'; sessionId: string; clientRunKey?: string }
  | { type: 'LOCAL_INIT_SLOT_CLEARED'; sessionId: string }
  | {
      type: 'RESTORE_PENDING_INTERACTION';
      sessionId: string;
      clientRunKey: string;
      round: RoundData;
      interrupt: InterruptDetails;
    }
  | {
      type: 'RUNNING_SESSIONS_SNAPSHOT';
      runningSessions: Array<{ session_id: string; round_id: string | null }>;
      receivedAt: number;
    };

export interface ChatSessionProjection extends ChatSessionRuntimeState {
  sending: boolean;
  resuming: boolean;
}

export interface SendMessageInput {
  sessionId: string;
  displayMessage: string;
  content: ChatContentBlock[];
  attachments?: FileInfo[];
  preferredSkillKeys?: string[];
  preferredMcpConnections?: PreferredMcpConnectionSnapshot[];
  reasoning?: TurnReasoningSelection;
  onStreamAccepted?: () => void;
  onRejectedBeforeAccept?: () => void;
}

export const emptyAgentState = (): AgentState => ({
  currentStep: 0,
  status: 'idle',
  toolLogs: [],
  lastUpdated: Date.now(),
});

export const emptyBuffers = (): StreamBuffers => ({
  textByMessageId: {},
  thinkingByMessageId: {},
  toolArgsByToolCallId: {},
  textSegmentStateByMessageId: {},
  thinkingSegmentStateByMessageId: {},
  toolArgsSegmentStateByToolCallId: {},
  currentTextMessageId: null,
  currentThinkingMessageId: null,
});

export const emptySessionState = (): ChatSessionRuntimeState => ({
  rounds: [],
  pendingInterrupt: null,
  error: '',
  loading: false,
  activeRunKeys: [],
  agentStateByRunKey: {},
  visibleAgentStateRunKey: null,
});

export const initialChatRuntimeState: ChatRuntimeState = {
  sessions: {},
  runs: {},
  serverRunIdToClientRunKey: {},
  tempRoundIdToServerRoundId: {},
  serverRoundIdToLocalRoundId: {},
  idempotencyKeyToClientRunKey: {},
};
