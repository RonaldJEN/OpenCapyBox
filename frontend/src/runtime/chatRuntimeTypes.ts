import type {
  AgentState,
  AGUIEvent,
  ChatContentBlock,
  FileInfo,
  InterruptDetails,
  RoundData,
} from '../types';

export type StreamSource = 'direct' | 'subscribe' | 'resume';

export type RunStatus =
  | 'starting'
  | 'streaming'
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

export interface StreamBuffers {
  textByMessageId: Record<string, string>;
  thinkingByMessageId: Record<string, string>;
  toolArgsByToolCallId: Record<string, string>;
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
  | { type: 'LOCAL_CANCELLED'; sessionId: string; clientRunKey?: string }
  | { type: 'SET_PENDING_INTERRUPT'; sessionId: string; interrupt: InterruptDetails | null }
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
