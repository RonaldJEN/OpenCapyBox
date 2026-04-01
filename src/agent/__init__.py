"""Mini Agent - Minimal single agent with basic tools and MCP support."""

from .agent import Agent
from .llm import LLMClient
from .schema import FunctionCall, LLMProvider, LLMResponse, Message, ToolCall
from .event_emitter import AGUIEventEmitter
from .schema.agui_events import (
    EventType,
    AGUIEvent,
    AgentState,
    RunStartedEvent,
    RunFinishedEvent,
    RunErrorEvent,
    StepStartedEvent,
    StepFinishedEvent,
    TextMessageStartEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    ThinkingTextMessageStartEvent,
    ThinkingTextMessageContentEvent,
    ThinkingTextMessageEndEvent,
    ToolCallStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    StateSnapshotEvent,
    StateDeltaEvent,
)

__version__ = "0.1.0"

__all__ = [
    # Agent 核心
    "Agent",
    "LLMClient",
    "LLMProvider",
    "Message",
    "LLMResponse",
    "ToolCall",
    "FunctionCall",
    # AG-UI 事件發射器
    "AGUIEventEmitter",
    # AG-UI 事件類型
    "EventType",
    "AGUIEvent",
    "AgentState",
    # 生命週期事件
    "RunStartedEvent",
    "RunFinishedEvent",
    "RunErrorEvent",
    "StepStartedEvent",
    "StepFinishedEvent",
    # 消息事件
    "TextMessageStartEvent",
    "TextMessageContentEvent",
    "TextMessageEndEvent",
    "ThinkingTextMessageStartEvent",
    "ThinkingTextMessageContentEvent",
    "ThinkingTextMessageEndEvent",
    # 工具調用事件
    "ToolCallStartEvent",
    "ToolCallArgsEvent",
    "ToolCallEndEvent",
    "ToolCallResultEvent",
    # 狀態事件
    "StateSnapshotEvent",
    "StateDeltaEvent",
]
