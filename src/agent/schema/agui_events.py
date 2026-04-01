"""AG-UI 协议事件类型定义

基于 Agent User Interaction Protocol SDK v2 规范
参考文档：AG-UI_事件文档_v2.md, AG-UI_类型文档_v2.md
"""
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field
import time


# =============================================================================
# 事件类型枚举
# =============================================================================

class EventType(str, Enum):
    """AG-UI 事件类型枚举"""
    
    # 生命周期事件
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    STEP_STARTED = "STEP_STARTED"
    STEP_FINISHED = "STEP_FINISHED"
    
    # 文本消息事件
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    TEXT_MESSAGE_CHUNK = "TEXT_MESSAGE_CHUNK"  # 便捷事件
    
    # 思考过程事件（扩展）
    THINKING_TEXT_MESSAGE_START = "THINKING_TEXT_MESSAGE_START"
    THINKING_TEXT_MESSAGE_CONTENT = "THINKING_TEXT_MESSAGE_CONTENT"
    THINKING_TEXT_MESSAGE_END = "THINKING_TEXT_MESSAGE_END"
    
    # 工具调用事件
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"
    TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
    TOOL_CALL_CHUNK = "TOOL_CALL_CHUNK"  # 便捷事件
    
    # 状态管理事件
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    STATE_DELTA = "STATE_DELTA"
    MESSAGES_SNAPSHOT = "MESSAGES_SNAPSHOT"
    
    # 活动事件
    ACTIVITY_SNAPSHOT = "ACTIVITY_SNAPSHOT"
    ACTIVITY_DELTA = "ACTIVITY_DELTA"
    
    # 特殊事件
    RAW = "RAW"
    CUSTOM = "CUSTOM"


# =============================================================================
# 角色类型
# =============================================================================

Role = Literal["developer", "system", "assistant", "user", "tool", "activity"]


# =============================================================================
# Human-in-the-Loop 类型（预留）
# =============================================================================

InterruptReason = Literal[
    "human_approval",    # 需要人工审批
    "input_required",    # 需要用户补充信息
    "confirmation",      # 需要用户确认
    "policy_hold",       # 组织策略暂停
    "error_recovery",    # 错误恢复指导
]


class InterruptDetails(BaseModel):
    """中断详情 - Human-in-the-Loop"""
    id: Optional[str] = None
    reason: Optional[str] = None  # InterruptReason 或自定义
    payload: Optional[Any] = None


class ResumePayload(BaseModel):
    """恢复执行负载"""
    interrupt_id: Optional[str] = Field(None, alias="interruptId")
    payload: Optional[Any] = None
    
    class Config:
        populate_by_name = True


RunFinishedOutcome = Literal["success", "interrupt"]


# =============================================================================
# 基础事件
# =============================================================================

class BaseEvent(BaseModel):
    """所有事件的基类"""
    type: EventType
    timestamp: Optional[int] = Field(default_factory=lambda: int(time.time() * 1000))
    raw_event: Optional[Any] = Field(None, alias="rawEvent")
    
    class Config:
        populate_by_name = True
        use_enum_values = True


# =============================================================================
# 生命周期事件
# =============================================================================

class RunStartedEvent(BaseEvent):
    """运行开始事件"""
    type: Literal[EventType.RUN_STARTED] = EventType.RUN_STARTED
    thread_id: str = Field(..., alias="threadId")
    run_id: str = Field(..., alias="runId")
    parent_run_id: Optional[str] = Field(None, alias="parentRunId")


class RunFinishedEvent(BaseEvent):
    """运行结束事件"""
    type: Literal[EventType.RUN_FINISHED] = EventType.RUN_FINISHED
    thread_id: str = Field(..., alias="threadId")
    run_id: str = Field(..., alias="runId")
    result: Optional[Any] = None
    outcome: Optional[RunFinishedOutcome] = None
    interrupt: Optional[InterruptDetails] = None


class RunErrorEvent(BaseEvent):
    """运行错误事件"""
    type: Literal[EventType.RUN_ERROR] = EventType.RUN_ERROR
    message: str
    code: Optional[str] = None


class StepStartedEvent(BaseEvent):
    """步骤开始事件"""
    type: Literal[EventType.STEP_STARTED] = EventType.STEP_STARTED
    step_name: str = Field(..., alias="stepName")


class StepFinishedEvent(BaseEvent):
    """步骤结束事件"""
    type: Literal[EventType.STEP_FINISHED] = EventType.STEP_FINISHED
    step_name: str = Field(..., alias="stepName")


# =============================================================================
# 文本消息事件
# =============================================================================

class TextMessageStartEvent(BaseEvent):
    """文本消息开始事件"""
    type: Literal[EventType.TEXT_MESSAGE_START] = EventType.TEXT_MESSAGE_START
    message_id: str = Field(..., alias="messageId")
    role: Role


class TextMessageContentEvent(BaseEvent):
    """文本消息内容事件"""
    type: Literal[EventType.TEXT_MESSAGE_CONTENT] = EventType.TEXT_MESSAGE_CONTENT
    message_id: str = Field(..., alias="messageId")
    delta: str  # 非空字符串


class TextMessageEndEvent(BaseEvent):
    """文本消息结束事件"""
    type: Literal[EventType.TEXT_MESSAGE_END] = EventType.TEXT_MESSAGE_END
    message_id: str = Field(..., alias="messageId")


class TextMessageChunkEvent(BaseEvent):
    """文本消息块事件（便捷事件）- 自动展开为 Start → Content → End"""
    type: Literal[EventType.TEXT_MESSAGE_CHUNK] = EventType.TEXT_MESSAGE_CHUNK
    message_id: Optional[str] = Field(None, alias="messageId")
    role: Optional[Role] = None
    delta: Optional[str] = None


# =============================================================================
# 思考过程事件（扩展）
# =============================================================================

class ThinkingTextMessageStartEvent(BaseEvent):
    """思考过程开始事件"""
    type: Literal[EventType.THINKING_TEXT_MESSAGE_START] = EventType.THINKING_TEXT_MESSAGE_START
    message_id: str = Field(..., alias="messageId")


class ThinkingTextMessageContentEvent(BaseEvent):
    """思考过程内容事件"""
    type: Literal[EventType.THINKING_TEXT_MESSAGE_CONTENT] = EventType.THINKING_TEXT_MESSAGE_CONTENT
    message_id: str = Field(..., alias="messageId")
    delta: str  # 非空字符串


class ThinkingTextMessageEndEvent(BaseEvent):
    """思考过程结束事件"""
    type: Literal[EventType.THINKING_TEXT_MESSAGE_END] = EventType.THINKING_TEXT_MESSAGE_END
    message_id: str = Field(..., alias="messageId")


# =============================================================================
# 工具调用事件
# =============================================================================

class ToolCallStartEvent(BaseEvent):
    """工具调用开始事件"""
    type: Literal[EventType.TOOL_CALL_START] = EventType.TOOL_CALL_START
    tool_call_id: str = Field(..., alias="toolCallId")
    tool_call_name: str = Field(..., alias="toolCallName")
    parent_message_id: Optional[str] = Field(None, alias="parentMessageId")


class ToolCallArgsEvent(BaseEvent):
    """工具调用参数事件"""
    type: Literal[EventType.TOOL_CALL_ARGS] = EventType.TOOL_CALL_ARGS
    tool_call_id: str = Field(..., alias="toolCallId")
    delta: str  # 参数数据块（JSON 片段）


class ToolCallEndEvent(BaseEvent):
    """工具调用结束事件"""
    type: Literal[EventType.TOOL_CALL_END] = EventType.TOOL_CALL_END
    tool_call_id: str = Field(..., alias="toolCallId")


class ToolCallResultEvent(BaseEvent):
    """工具调用结果事件"""
    type: Literal[EventType.TOOL_CALL_RESULT] = EventType.TOOL_CALL_RESULT
    message_id: str = Field(..., alias="messageId")
    tool_call_id: str = Field(..., alias="toolCallId")
    content: str
    role: Optional[Literal["tool"]] = "tool"
    execution_time_ms: Optional[int] = Field(None, alias="executionTimeMs")


class ToolCallChunkEvent(BaseEvent):
    """工具调用块事件（便捷事件）- 自动展开为 Start → Args → End"""
    type: Literal[EventType.TOOL_CALL_CHUNK] = EventType.TOOL_CALL_CHUNK
    tool_call_id: Optional[str] = Field(None, alias="toolCallId")
    tool_call_name: Optional[str] = Field(None, alias="toolCallName")
    parent_message_id: Optional[str] = Field(None, alias="parentMessageId")
    delta: Optional[str] = None


# =============================================================================
# 状态管理事件
# =============================================================================

class StateSnapshotEvent(BaseEvent):
    """状态快照事件"""
    type: Literal[EventType.STATE_SNAPSHOT] = EventType.STATE_SNAPSHOT
    snapshot: Any  # 完整状态对象


class StateDeltaEvent(BaseEvent):
    """状态增量事件 - JSON Patch (RFC 6902)"""
    type: Literal[EventType.STATE_DELTA] = EventType.STATE_DELTA
    delta: List[Dict[str, Any]]  # JSON Patch 操作数组


class MessagesSnapshotEvent(BaseEvent):
    """消息快照事件"""
    type: Literal[EventType.MESSAGES_SNAPSHOT] = EventType.MESSAGES_SNAPSHOT
    messages: List[Dict[str, Any]]  # Message 对象数组


# =============================================================================
# 活动事件
# =============================================================================

class ActivitySnapshotEvent(BaseEvent):
    """活动快照事件"""
    type: Literal[EventType.ACTIVITY_SNAPSHOT] = EventType.ACTIVITY_SNAPSHOT
    message_id: str = Field(..., alias="messageId")
    activity_type: str = Field(..., alias="activityType")
    content: Dict[str, Any]
    replace: Optional[bool] = True


class ActivityDeltaEvent(BaseEvent):
    """活动增量事件 - JSON Patch (RFC 6902)"""
    type: Literal[EventType.ACTIVITY_DELTA] = EventType.ACTIVITY_DELTA
    message_id: str = Field(..., alias="messageId")
    activity_type: str = Field(..., alias="activityType")
    patch: List[Dict[str, Any]]  # RFC 6902 JSON Patch 操作


# =============================================================================
# 特殊事件
# =============================================================================

class RawEvent(BaseEvent):
    """原始事件 - 传递外部系统事件"""
    type: Literal[EventType.RAW] = EventType.RAW
    event: Any
    source: Optional[str] = None


class CustomEvent(BaseEvent):
    """自定义事件 - 应用特定扩展"""
    type: Literal[EventType.CUSTOM] = EventType.CUSTOM
    name: str
    value: Any


# =============================================================================
# 消息类型（用于 RunAgentInput 和 MessagesSnapshot）
# =============================================================================

class FunctionCall(BaseModel):
    """函数调用详情"""
    name: str
    arguments: str  # JSON 编码字符串


class ToolCall(BaseModel):
    """工具调用结构"""
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class TextInputContent(BaseModel):
    """文本输入内容"""
    type: Literal["text"] = "text"
    text: str


class BinaryInputContent(BaseModel):
    """二进制输入内容"""
    type: Literal["binary"] = "binary"
    mime_type: str = Field(..., alias="mimeType")
    id: Optional[str] = None
    url: Optional[str] = None
    data: Optional[str] = None  # Base64 编码
    filename: Optional[str] = None
    
    class Config:
        populate_by_name = True


InputContent = Union[TextInputContent, BinaryInputContent]


class DeveloperMessage(BaseModel):
    """开发者消息"""
    id: str
    role: Literal["developer"] = "developer"
    content: str
    name: Optional[str] = None


class SystemMessage(BaseModel):
    """系统消息"""
    id: str
    role: Literal["system"] = "system"
    content: str
    name: Optional[str] = None


class AssistantMessage(BaseModel):
    """助手消息"""
    id: str
    role: Literal["assistant"] = "assistant"
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = Field(None, alias="toolCalls")
    
    class Config:
        populate_by_name = True


class UserMessage(BaseModel):
    """用户消息"""
    id: str
    role: Literal["user"] = "user"
    content: Union[str, List[InputContent]]
    name: Optional[str] = None


class ToolMessage(BaseModel):
    """工具消息"""
    id: str
    role: Literal["tool"] = "tool"
    content: str
    tool_call_id: str = Field(..., alias="toolCallId")
    error: Optional[str] = None
    
    class Config:
        populate_by_name = True


class ActivityMessage(BaseModel):
    """活动消息"""
    id: str
    role: Literal["activity"] = "activity"
    activity_type: str = Field(..., alias="activityType")
    content: Dict[str, Any]
    
    class Config:
        populate_by_name = True


Message = Union[
    DeveloperMessage,
    SystemMessage,
    AssistantMessage,
    UserMessage,
    ToolMessage,
    ActivityMessage,
]


# =============================================================================
# 上下文和工具定义
# =============================================================================

class Context(BaseModel):
    """上下文信息"""
    description: str
    value: str


class Tool(BaseModel):
    """工具定义"""
    name: str
    description: str
    parameters: Any  # JSON Schema


# =============================================================================
# 运行 Agent 输入
# =============================================================================

class RunAgentInput(BaseModel):
    """运行 Agent 的输入参数"""
    thread_id: str = Field(..., alias="threadId")
    run_id: str = Field(..., alias="runId")
    parent_run_id: Optional[str] = Field(None, alias="parentRunId")
    state: Optional[Any] = None
    messages: List[Dict[str, Any]] = []  # Message 对象数组
    tools: List[Tool] = []
    context: List[Context] = []
    forwarded_props: Optional[Any] = Field(None, alias="forwardedProps")
    resume: Optional[ResumePayload] = None
    
    class Config:
        populate_by_name = True


# =============================================================================
# Agent 状态定义（用于 STATE_SNAPSHOT/STATE_DELTA）
# =============================================================================

class ToolLogEntry(BaseModel):
    """工具调用日志条目"""
    tool_call_id: str = Field(..., alias="toolCallId")
    tool_name: str = Field(..., alias="toolName")
    status: Literal["pending", "running", "completed", "failed"]
    started_at: Optional[int] = Field(None, alias="startedAt")
    completed_at: Optional[int] = Field(None, alias="completedAt")
    error: Optional[str] = None
    
    class Config:
        populate_by_name = True


class AgentState(BaseModel):
    """Agent 运行状态"""
    current_step: int = Field(0, alias="currentStep")
    total_steps: Optional[int] = Field(None, alias="totalSteps")
    status: Literal["idle", "running", "waiting", "completed", "error"] = "idle"
    tool_logs: List[ToolLogEntry] = Field(default_factory=list, alias="toolLogs")
    last_updated: int = Field(default_factory=lambda: int(time.time() * 1000), alias="lastUpdated")
    
    class Config:
        populate_by_name = True


# =============================================================================
# 活动内容定义（用于 ACTIVITY_SNAPSHOT/ACTIVITY_DELTA）
# =============================================================================

class PlanStep(BaseModel):
    """计划步骤"""
    name: str
    status: Literal["pending", "in_progress", "completed", "failed"]
    description: Optional[str] = None


class PlanActivityContent(BaseModel):
    """计划活动内容"""
    title: str
    steps: List[PlanStep]
    current_step: int = Field(0, alias="currentStep")
    
    class Config:
        populate_by_name = True


class SearchActivityContent(BaseModel):
    """搜索活动内容"""
    query: str
    results_count: int = Field(0, alias="resultsCount")
    status: Literal["searching", "completed", "failed"]
    
    class Config:
        populate_by_name = True


# =============================================================================
# 事件联合类型
# =============================================================================

AGUIEvent = Union[
    # 生命周期事件
    RunStartedEvent,
    RunFinishedEvent,
    RunErrorEvent,
    StepStartedEvent,
    StepFinishedEvent,
    # 文本消息事件
    TextMessageStartEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageChunkEvent,
    # 思考过程事件
    ThinkingTextMessageStartEvent,
    ThinkingTextMessageContentEvent,
    ThinkingTextMessageEndEvent,
    # 工具调用事件
    ToolCallStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallChunkEvent,
    # 状态管理事件
    StateSnapshotEvent,
    StateDeltaEvent,
    MessagesSnapshotEvent,
    # 活动事件
    ActivitySnapshotEvent,
    ActivityDeltaEvent,
    # 特殊事件
    RawEvent,
    CustomEvent,
]
