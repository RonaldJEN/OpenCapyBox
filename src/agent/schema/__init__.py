"""Schema definitions for OpenCapyBox."""

from .schema import (
    FunctionCall,
    LLMProvider,
    LLMResponse,
    Message,
    ToolCall,
)

from .agui_events import (
    # 事件類型枚舉
    EventType,
    # 聯合類型
    AGUIEvent,
    # 基礎事件
    BaseEvent,
    # 生命週期事件
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
    # 思考過程事件
    ThinkingTextMessageStartEvent,
    ThinkingTextMessageContentEvent,
    ThinkingTextMessageEndEvent,
    # 工具調用事件
    ToolCallStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallChunkEvent,
    # 狀態管理事件
    StateSnapshotEvent,
    StateDeltaEvent,
    MessagesSnapshotEvent,
    # 活動事件
    ActivitySnapshotEvent,
    ActivityDeltaEvent,
    # 特殊事件
    RawEvent,
    CustomEvent,
    # 狀態類型
    AgentState,
    ToolLogEntry,
    # 消息類型
    Role,
    InterruptDetails,
    ResumePayload,
    RunFinishedOutcome,
)

__all__ = [
    # 原有導出
    "FunctionCall",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "ToolCall",
    # AG-UI 事件類型
    "EventType",
    "AGUIEvent",
    "BaseEvent",
    # 生命週期事件
    "RunStartedEvent",
    "RunFinishedEvent",
    "RunErrorEvent",
    "StepStartedEvent",
    "StepFinishedEvent",
    # 文本消息事件
    "TextMessageStartEvent",
    "TextMessageContentEvent",
    "TextMessageEndEvent",
    "TextMessageChunkEvent",
    # 思考過程事件
    "ThinkingTextMessageStartEvent",
    "ThinkingTextMessageContentEvent",
    "ThinkingTextMessageEndEvent",
    # 工具調用事件
    "ToolCallStartEvent",
    "ToolCallArgsEvent",
    "ToolCallEndEvent",
    "ToolCallResultEvent",
    "ToolCallChunkEvent",
    # 狀態管理事件
    "StateSnapshotEvent",
    "StateDeltaEvent",
    "MessagesSnapshotEvent",
    # 活動事件
    "ActivitySnapshotEvent",
    "ActivityDeltaEvent",
    # 特殊事件
    "RawEvent",
    "CustomEvent",
    # 狀態類型
    "AgentState",
    "ToolLogEntry",
    # 消息類型
    "Role",
    "InterruptDetails",
    "ResumePayload",
    "RunFinishedOutcome",
]
