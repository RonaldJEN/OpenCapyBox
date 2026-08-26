"""Schema definitions for OpenCapyBox."""

from .schema import (
    FunctionCall,
    LLMProvider,
    LLMResponse,
    Message,
    ToolCall,
    TokenUsage,
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

from .run_context import (
    AgentRunContext,
    LLMRequestContext,
    RequestedTurnPreferencesContext,
    ResolvedMcpConnectionRef,
    ResolvedSkillRef,
    ResolvedTurnPreferencesContext,
)
from .skill_key import (
    MAX_SKILL_KEY_LENGTH,
    PUBLIC_SKILL_KEY_VALIDATION_ERRORS,
    SkillKeyValidationError,
    normalize_skill_key,
)

__all__ = [
    # 原有導出
    "FunctionCall",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "ToolCall",
    "TokenUsage",
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
    "AgentRunContext",
    "LLMRequestContext",
    "RequestedTurnPreferencesContext",
    "ResolvedMcpConnectionRef",
    "ResolvedSkillRef",
    "ResolvedTurnPreferencesContext",
    "MAX_SKILL_KEY_LENGTH",
    "PUBLIC_SKILL_KEY_VALIDATION_ERRORS",
    "SkillKeyValidationError",
    "normalize_skill_key",
]
