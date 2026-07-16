"""Base tool classes."""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """Tool execution result."""

    success: bool
    content: str = ""
    error: str | None = None
    content_blocks: list[dict[str, Any]] | None = None
    # Internal execution-state signal.  In particular, a remote write may
    # have reached the MCP server even though no response was observed.
    outcome_uncertain: bool = Field(default=False, exclude=True)


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Per-tool-call runtime context supplied by Agent.run_agui."""

    thread_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    cancel_token: Any = None


@dataclass(frozen=True)
class ToolRef:
    """Stable execution identity independent from the model-visible name."""

    provider: str
    name: str
    server_id: str | None = None
    installation_id: str | None = None


class ToolExposure(str, Enum):
    """How a registered tool is projected onto model-facing tool surfaces.

    ``DIRECT_MODEL_ONLY`` currently has the same projection as ``DIRECT``.
    Keeping the distinction in the contract lets a future Code Mode runtime
    omit model-only tools from its nested executor without changing tools.
    """

    DIRECT = "direct"
    DEFERRED = "deferred"
    HIDDEN = "hidden"
    DIRECT_MODEL_ONLY = "direct_model_only"


class Tool:
    """Base class for all tools."""

    # 工具結果的最大 token 數，超出部分會被 head+tail 截斷。
    # 子類可覆蓋，例如 SandboxReadTool 設為 32000。
    max_result_tokens: int = 8000

    # 单次 execute() 超时（秒）。None = 使用 Agent 全局默认值，0 = 不限时，>0 = 工具级覆盖。
    execute_timeout: int | None = None

    # Most built-in tools remain directly visible for backwards compatibility.
    # Remote catalogs can opt into DEFERRED to keep their schemas out of the
    # initial model request.
    exposure: ToolExposure = ToolExposure.DIRECT

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

    @property
    def tool_ref(self) -> ToolRef:
        """Stable reference used by runtime policy, audit and remote routing."""
        return ToolRef(provider="builtin", name=self.name)

    def validate_arguments(self, arguments: dict[str, Any]) -> str | None:
        """Run provider-specific validation before approval or execution."""
        return None

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
