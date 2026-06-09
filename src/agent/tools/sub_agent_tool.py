"""Sub-agent delegation tool.

The tool itself only owns the public schema and argument validation. Actual
sub-agent execution is a service-layer concern because it needs session/run
metadata, DB access, AG-UI event persistence, and graph-edge lifecycle updates.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from src.agent.tools.base import Tool, ToolResult, ToolRuntimeContext


SubAgentRunner = Callable[..., Awaitable[ToolResult]]


class SubAgentTool(Tool):
    """Delegate a focused task to a child Agent run."""

    max_result_tokens = 32000
    # 子 Round 自带步数上限，不受父 agent 单次工具超时拦截（research 抓取可能超 300s）。
    execute_timeout = 0

    def __init__(self, *, runner: SubAgentRunner | None = None) -> None:
        self._runner = runner
        self._runtime_context: ToolRuntimeContext | None = None

    @property
    def name(self) -> str:
        return "sub_agent"

    @property
    def description(self) -> str:
        return (
            "Delegate a self-contained subtask to a child agent that runs in its "
            "own isolated context and returns only a summary. Use this to keep "
            "high-volume or noisy output (web research/crawling, long-document "
            "lookups, bulk file generation) out of your main context. Choose "
            "subagent_type: 'research' (read + web + crawl, no workspace writes), "
            "'write' (create/update/edit workspace files and office-style "
            "deliverables), or 'general' (default, mixed task). Do NOT delegate "
            "work that needs frequent back-and-forth with the user or tight "
            "iteration with your current context — do that yourself. The child "
            "cannot ask the user questions and cannot spawn further sub-agents."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The complete task for the child agent. Include all relevant context.",
                },
                "subagent_type": {
                    "type": "string",
                    "description": (
                        "Sub-agent profile that controls the child's tools and "
                        "system prompt. One of: 'research' (read + web + crawl, "
                        "no workspace writes), 'write' (create/update/edit/annotate "
                        "workspace files), 'general' (default, broad task). Legacy "
                        "aliases map automatically (plan/review/explore->research, "
                        "code/debug->write)."
                    ),
                    "default": "general",
                },
                "description": {
                    "type": "string",
                    "description": "Short human-readable description of the delegated task.",
                    "default": "",
                },
            },
            "required": ["prompt"],
        }

    def set_runtime_context(self, context: ToolRuntimeContext) -> None:
        self._runtime_context = context

    def clear_runtime_context(self) -> None:
        self._runtime_context = None

    async def execute(
        self,
        prompt: str,
        subagent_type: str = "general",
        description: str = "",
    ) -> ToolResult:
        return await self.execute_with_context(
            self._runtime_context,
            prompt=prompt,
            subagent_type=subagent_type,
            description=description,
        )

    async def execute_with_context(
        self,
        context: ToolRuntimeContext | None,
        *,
        prompt: str,
        subagent_type: str = "general",
        description: str = "",
    ) -> ToolResult:
        if not isinstance(prompt, str) or not prompt.strip():
            return ToolResult(success=False, error="prompt is required")
        if self._runner is None:
            return ToolResult(success=False, error="sub_agent runner is not configured")
        if context is None:
            return ToolResult(success=False, error="sub_agent runtime context is unavailable")

        return await self._runner(
            prompt=prompt.strip(),
            subagent_type=(subagent_type or "general").strip() or "general",
            description=(description or "").strip(),
            context=context,
        )
