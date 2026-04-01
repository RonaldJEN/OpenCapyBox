// 会话状态
export enum SessionStatus {
  ACTIVE = "active",
  PAUSED = "paused",
  COMPLETED = "completed"
}

// =============================================================================
// AG-UI 协议类型定义 (v2)
// =============================================================================

// AG-UI 事件类型枚举
export enum AGUIEventType {
  // 生命周期事件
  RUN_STARTED = "RUN_STARTED",
  RUN_FINISHED = "RUN_FINISHED",
  RUN_ERROR = "RUN_ERROR",
  STEP_STARTED = "STEP_STARTED",
  STEP_FINISHED = "STEP_FINISHED",

  // 文本消息事件
  TEXT_MESSAGE_START = "TEXT_MESSAGE_START",
  TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT",
  TEXT_MESSAGE_END = "TEXT_MESSAGE_END",
  TEXT_MESSAGE_CHUNK = "TEXT_MESSAGE_CHUNK",

  // 思考过程事件（扩展）
  THINKING_TEXT_MESSAGE_START = "THINKING_TEXT_MESSAGE_START",
  THINKING_TEXT_MESSAGE_CONTENT = "THINKING_TEXT_MESSAGE_CONTENT",
  THINKING_TEXT_MESSAGE_END = "THINKING_TEXT_MESSAGE_END",

  // 工具调用事件
  TOOL_CALL_START = "TOOL_CALL_START",
  TOOL_CALL_ARGS = "TOOL_CALL_ARGS",
  TOOL_CALL_END = "TOOL_CALL_END",
  TOOL_CALL_RESULT = "TOOL_CALL_RESULT",
  TOOL_CALL_CHUNK = "TOOL_CALL_CHUNK",

  // 状态管理事件
  STATE_SNAPSHOT = "STATE_SNAPSHOT",
  STATE_DELTA = "STATE_DELTA",
  MESSAGES_SNAPSHOT = "MESSAGES_SNAPSHOT",

  // 活动事件
  ACTIVITY_SNAPSHOT = "ACTIVITY_SNAPSHOT",
  ACTIVITY_DELTA = "ACTIVITY_DELTA",

  // 特殊事件
  RAW = "RAW",
  CUSTOM = "CUSTOM",
}

// JSON Patch 操作 (RFC 6902)
export interface JsonPatchOperation {
  op: "add" | "remove" | "replace" | "move" | "copy" | "test";
  path: string;
  value?: any;
  from?: string;
}

// AG-UI 基础事件接口
export interface AGUIBaseEvent {
  type: AGUIEventType;
  timestamp?: number;
  rawEvent?: any;
}

// 生命周期事件
export interface RunStartedEvent extends AGUIBaseEvent {
  type: AGUIEventType.RUN_STARTED;
  threadId: string;
  runId: string;
  parentRunId?: string;
}

export interface RunFinishedEvent extends AGUIBaseEvent {
  type: AGUIEventType.RUN_FINISHED;
  threadId: string;
  runId: string;
  result?: any;
  outcome?: "success" | "interrupt";
  interrupt?: InterruptDetails;
}

export interface RunErrorEvent extends AGUIBaseEvent {
  type: AGUIEventType.RUN_ERROR;
  message: string;
  code?: string;
}

export interface StepStartedEvent extends AGUIBaseEvent {
  type: AGUIEventType.STEP_STARTED;
  stepName: string;
}

export interface StepFinishedEvent extends AGUIBaseEvent {
  type: AGUIEventType.STEP_FINISHED;
  stepName: string;
}

// 文本消息事件
export interface TextMessageStartEvent extends AGUIBaseEvent {
  type: AGUIEventType.TEXT_MESSAGE_START;
  messageId: string;
  role: string;
}

export interface TextMessageContentEvent extends AGUIBaseEvent {
  type: AGUIEventType.TEXT_MESSAGE_CONTENT;
  messageId: string;
  delta: string;
}

export interface TextMessageEndEvent extends AGUIBaseEvent {
  type: AGUIEventType.TEXT_MESSAGE_END;
  messageId: string;
}

// 思考过程事件
export interface ThinkingTextMessageStartEvent extends AGUIBaseEvent {
  type: AGUIEventType.THINKING_TEXT_MESSAGE_START;
  messageId: string;
}

export interface ThinkingTextMessageContentEvent extends AGUIBaseEvent {
  type: AGUIEventType.THINKING_TEXT_MESSAGE_CONTENT;
  messageId: string;
  delta: string;
}

export interface ThinkingTextMessageEndEvent extends AGUIBaseEvent {
  type: AGUIEventType.THINKING_TEXT_MESSAGE_END;
  messageId: string;
}

// 工具调用事件
export interface ToolCallStartEvent extends AGUIBaseEvent {
  type: AGUIEventType.TOOL_CALL_START;
  toolCallId: string;
  toolCallName: string;
  parentMessageId?: string;
}

export interface ToolCallArgsEvent extends AGUIBaseEvent {
  type: AGUIEventType.TOOL_CALL_ARGS;
  toolCallId: string;
  delta: string;
}

export interface ToolCallEndEvent extends AGUIBaseEvent {
  type: AGUIEventType.TOOL_CALL_END;
  toolCallId: string;
}

export interface ToolCallResultEvent extends AGUIBaseEvent {
  type: AGUIEventType.TOOL_CALL_RESULT;
  messageId: string;
  toolCallId: string;
  content: string;
  role?: "tool";
}

// 状态管理事件
export interface StateSnapshotEvent extends AGUIBaseEvent {
  type: AGUIEventType.STATE_SNAPSHOT;
  snapshot: AgentState;
}

export interface StateDeltaEvent extends AGUIBaseEvent {
  type: AGUIEventType.STATE_DELTA;
  delta: JsonPatchOperation[];
}

export interface MessagesSnapshotEvent extends AGUIBaseEvent {
  type: AGUIEventType.MESSAGES_SNAPSHOT;
  messages: AGUIMessage[];
}

// 活动事件
export interface ActivitySnapshotEvent extends AGUIBaseEvent {
  type: AGUIEventType.ACTIVITY_SNAPSHOT;
  messageId: string;
  activityType: string;
  content: Record<string, any>;
  replace?: boolean;
}

export interface ActivityDeltaEvent extends AGUIBaseEvent {
  type: AGUIEventType.ACTIVITY_DELTA;
  messageId: string;
  activityType: string;
  patch: JsonPatchOperation[];
}

// 自定义事件
export interface CustomEvent extends AGUIBaseEvent {
  type: AGUIEventType.CUSTOM;
  name: string;
  value: any;
}

// AG-UI 事件联合类型
export type AGUIEvent =
  | RunStartedEvent
  | RunFinishedEvent
  | RunErrorEvent
  | StepStartedEvent
  | StepFinishedEvent
  | TextMessageStartEvent
  | TextMessageContentEvent
  | TextMessageEndEvent
  | ThinkingTextMessageStartEvent
  | ThinkingTextMessageContentEvent
  | ThinkingTextMessageEndEvent
  | ToolCallStartEvent
  | ToolCallArgsEvent
  | ToolCallEndEvent
  | ToolCallResultEvent
  | StateSnapshotEvent
  | StateDeltaEvent
  | MessagesSnapshotEvent
  | ActivitySnapshotEvent
  | ActivityDeltaEvent
  | CustomEvent;

// Agent 状态
export interface AgentState {
  currentStep: number;
  totalSteps?: number;
  status: "idle" | "running" | "waiting" | "completed" | "error";
  toolLogs: ToolLogEntry[];
  lastUpdated: number;
}

// 工具日志条目
export interface ToolLogEntry {
  toolCallId: string;
  toolName: string;
  args?: string;
  status: "pending" | "running" | "completed" | "failed";
  result?: string;
  startedAt?: number;
  completedAt?: number;
  error?: string;
}

// Human-in-the-Loop 类型
export interface InterruptDetails {
  id?: string;
  reason?: string;
  payload?: any;
}

// AG-UI 消息类型
export interface AGUIMessage {
  id: string;
  role: string;
  content?: string;
  toolCallId?: string;
}

export interface TextContentBlock {
  type: "text";
  text: string;
}

export interface ImageContentBlock {
  type: "image_url";
  image_url: {
    url: string;
  };
  file?: {
    path: string;
    name?: string;
    mime_type?: string;
    size?: number;
  };
}

export interface VideoContentBlock {
  type: "video_url";
  video_url: {
    url: string;
  };
}

export interface FileContentBlock {
  type: "file";
  file: {
    path: string;
    name?: string;
    mime_type?: string;
    size?: number;
  };
}

export type ChatContentBlock =
  | TextContentBlock
  | ImageContentBlock
  | VideoContentBlock
  | FileContentBlock;

// 模型信息（从后端 GET /api/models 获取）
export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  supports_thinking: boolean;
  supports_image: boolean;
  max_images: number;
  supports_video: boolean;
  max_videos: number;
  max_tokens: number;
  enabled: boolean;
  tags: string[];
}

export interface ModelsResponse {
  models: ModelInfo[];
  default_model: string;
}

// 会话类型
export interface Session {
  id: string;
  user_id: string;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
  title?: string;
  model_id?: string;
}

// 认证响应
export interface AuthResponse {
  user_id: string;
  access_token: string;
  token_type: string;
  expires_in: number;
  message: string;
}

// 创建会话响应
export interface CreateSessionResponse {
  session_id: string;
  model_id?: string;
  message: string;
}

// 会话列表响应
export interface SessionListResponse {
  sessions: Session[];
}


// 🆕 V2 API 类型定义

// 工具调用
export interface ToolCall {
  id?: string;  // 工具调用 ID（可选，用于流式更新）
  name: string;
  input: Record<string, any>;
  started_at_ts?: number;   // TOOL_CALL_START 事件 timestamp (ms)
  ended_at_ts?: number;     // TOOL_CALL_END 事件 timestamp (ms)
}

// 工具结果
export interface ToolResult {
  tool_call_id?: string;  // 关联的工具调用 ID（可选）
  success?: boolean;
  content: string;
  error?: string;
  received_at_ts?: number;      // TOOL_CALL_RESULT 事件 timestamp (ms)
  execution_time_ms?: number;   // 后端计算的工具执行耗时 (ms)
}

// 执行步骤
export interface StepData {
  step_number: number;
  thinking?: string;
  assistant_content?: string;
  tool_calls: ToolCall[];
  tool_results: ToolResult[];
  status: string;
  created_at?: string;
  // AG-UI timestamp 元数据
  thinking_start_ts?: number;   // THINKING_START 事件 timestamp (ms)
  thinking_end_ts?: number;     // THINKING_END 事件 timestamp (ms)
  started_at_ts?: number;       // STEP_STARTED 事件 timestamp (ms)
  finished_at_ts?: number;      // STEP_FINISHED 事件 timestamp (ms)
}

export interface AttachmentInfo {
  path: string;
  name: string;
  type: string;
  size?: number;
  data_url?: string;
  session_id?: string;
}

// 对话轮次
export interface RoundData {
  round_id: string;
  user_message: string;
  user_attachments?: AttachmentInfo[];
  final_response: string;
  steps: StepData[];
  step_count: number;
  status: string;
  created_at: string;
  completed_at?: string;
}


// 历史记录响应 V2
export interface HistoryResponseV2 {
  session_id: string;
  rounds: RoundData[];
  total: number;
}

// 🆕 文件管理类型定义

// 文件信息
export interface FileInfo {
  name: string;
  path: string;
  session_id?: string;
  size: number;
  modified: string;
  type: string;
  data_url?: string;
}

// 文件列表响应
export interface FileListResponse {
  files: FileInfo[];
  total: number;
}

// 运行中会话响应
export interface RunningSessionResponse {
  running_session_id: string | null;
  round_id: string | null;
}

// =============================================================================
// SSE 回调类型 (AG-UI 协议)
// =============================================================================

// 流式消息回调 (sendMessageStreamV2)
export interface StreamCallbacks {
  // 生命周期事件
  onRunStarted?: (threadId: string, runId: string) => void;
  onRunFinished?: (threadId: string, runId: string, result: any, outcome: string) => void;
  onRunError?: (message: string, code?: string) => void;
  onStepStarted?: (stepName: string, timestamp?: number) => void;
  onStepFinished?: (stepName: string, timestamp?: number) => void;

  // 文本消息事件
  onTextMessageStart?: (messageId: string, role: string) => void;
  onTextMessageContent?: (messageId: string, delta: string) => void;
  onTextMessageEnd?: (messageId: string) => void;

  // 思考过程事件
  onThinkingStart?: (messageId: string, timestamp?: number) => void;
  onThinkingContent?: (messageId: string, delta: string) => void;
  onThinkingEnd?: (messageId: string, timestamp?: number) => void;

  // 工具调用事件
  onToolCallStart?: (toolCallId: string, toolName: string, parentMessageId?: string, timestamp?: number) => void;
  onToolCallArgs?: (toolCallId: string, delta: string) => void;
  onToolCallEnd?: (toolCallId: string, timestamp?: number) => void;
  onToolCallResult?: (messageId: string, toolCallId: string, content: string, timestamp?: number, executionTimeMs?: number) => void;

  // 状态管理事件
  onStateSnapshot?: (snapshot: AgentState) => void;
  onStateDelta?: (delta: JsonPatchOperation[]) => void;
  onMessagesSnapshot?: (messages: AGUIMessage[]) => void;

  // 活动事件
  onActivitySnapshot?: (messageId: string, activityType: string, content: Record<string, any>) => void;
  onActivityDelta?: (messageId: string, activityType: string, patch: JsonPatchOperation[]) => void;

  // 自定义事件
  onCustomEvent?: (name: string, value: any) => void;
}

// 订阅回调类型 (subscribeToRound)
export interface SubscribeCallbacks {
  onRunFinished?: (threadId: string, runId: string, result: any, outcome: string) => void;
  onRunError?: (message: string, code?: string) => void;
  onMessagesSnapshot?: (messages: AGUIMessage[]) => void;
  onStateSnapshot?: (snapshot: AgentState) => void;
  onStateDelta?: (delta: JsonPatchOperation[]) => void;
  onCustomEvent?: (name: string, value: any) => void;
  
  // 🆕 流式事件回调（用于刷新后恢复实时更新）
  onTextMessageStart?: (messageId: string, role: string) => void;
  onTextMessageContent?: (messageId: string, delta: string) => void;
  onTextMessageEnd?: (messageId: string) => void;
  onThinkingStart?: (messageId: string, timestamp?: number) => void;
  onThinkingContent?: (messageId: string, delta: string) => void;
  onThinkingEnd?: (messageId: string, timestamp?: number) => void;
  onToolCallStart?: (toolCallId: string, toolName: string, parentMessageId?: string, timestamp?: number) => void;
  onToolCallArgs?: (toolCallId: string, delta: string) => void;
  onToolCallEnd?: (toolCallId: string, timestamp?: number) => void;
  onToolCallResult?: (messageId: string, toolCallId: string, content: string, timestamp?: number, executionTimeMs?: number) => void;
  onStepStarted?: (stepName: string, timestamp?: number) => void;
  onStepFinished?: (stepName: string, timestamp?: number) => void;
}

// 订阅结果类型
export interface SubscriptionResult {
  promise: Promise<void>;
  abort: () => void;
  /** 🆕 獲取最新收到的事件序列號（用於重連） */
  getLatestSequence?: () => number;
}
