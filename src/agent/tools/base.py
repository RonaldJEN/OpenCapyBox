"""Base tool classes."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class ToolResult(BaseModel):
    """Tool execution result."""

    success: bool
    content: str = ""
    error: str | None = None
    content_blocks: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Per-tool-call runtime context supplied by Agent.run_agui."""

    thread_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    cancel_token: Any = None


class Tool:
    """Base class for all tools."""

    # 工具結果的最大 token 數，超出部分會被 head+tail 截斷。
    # 子類可覆蓋，例如 SandboxReadTool 設為 32000。
    max_result_tokens: int = 8000

    # 单次 execute() 超时（秒）。None = 使用 Agent 全局默认值，0 = 不限时，>0 = 工具级覆盖。
    execute_timeout: int | None = None

    @property
    def name(self) -> str:
        """Tool name."""
        raise NotImplementedError

    @property
    def description(self) -> str:
        """Tool description."""
        raise NotImplementedError

    @property
    def parameters(self) -> dict[str, Any]:
        """Tool parameters schema (JSON Schema format)."""
        raise NotImplementedError

    async def execute(self, *args, **kwargs) -> ToolResult:  # type: ignore
        """Execute the tool with arbitrary arguments."""
        raise NotImplementedError

    def set_runtime_context(self, context: ToolRuntimeContext) -> None:
        """Receive runtime context for the next execute() call.

        Tools that do not need run/session metadata can ignore this hook.
        """

    def clear_runtime_context(self) -> None:
        """Clear runtime context after execute() returns."""

    def to_schema(self) -> dict[str, Any]:
        """Convert tool to Anthropic tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert tool to OpenAI tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
