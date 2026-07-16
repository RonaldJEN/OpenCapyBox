"""Model-facing gateway for deferred tool discovery."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from .base import Tool, ToolExposure, ToolResult, ToolRuntimeContext


TOOL_SEARCH_NAME = "tool_search"
_MAX_DISCOVERY_RESULTS = 20


class ToolDiscoveryTool(Tool):
    """Search deferred tools and expose exact matches on the next model step."""

    exposure = ToolExposure.DIRECT_MODEL_ONLY

    def __init__(
        self,
        discover: Callable[..., Awaitable[list[dict[str, Any]]]],
    ) -> None:
        self._discover = discover
        self._runtime_context: ToolRuntimeContext | None = None

    @property
    def name(self) -> str:
        return TOOL_SEARCH_NAME

    @property
    def description(self) -> str:
        return (
            "Search tools that are not included in the initial tool list. "
            "Matching tools returned by this call become available with their "
            "full schemas starting on the next step of this conversation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Case-insensitive words to match against tool name, server, "
                        "title, and description."
                    ),
                    "maxLength": 200,
                },
                "names": {
                    "type": "array",
                    "description": "Exact model tool names to enable.",
                    "items": {"type": "string", "maxLength": 128},
                    "maxItems": _MAX_DISCOVERY_RESULTS,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of matches to enable and return.",
                    "minimum": 1,
                    "maximum": _MAX_DISCOVERY_RESULTS,
                    "default": 8,
                },
            },
            "anyOf": [
                {"required": ["query"]},
                {"required": ["names"]},
            ],
            "additionalProperties": False,
        }

    def set_runtime_context(self, context: ToolRuntimeContext) -> None:
        self._runtime_context = context

    def clear_runtime_context(self) -> None:
        self._runtime_context = None

    async def execute(
        self,
        query: str | None = None,
        names: list[str] | None = None,
        limit: int = 8,
    ) -> ToolResult:
        context = self._runtime_context
        if context is None:
            return ToolResult(success=False, error="Tool discovery requires a runtime session")

        if query is not None and not isinstance(query, str):
            return ToolResult(success=False, error="query must be a string")
        if names is not None and not isinstance(names, list):
            return ToolResult(success=False, error="names must be an array of exact tool names")
        query = (query or "").strip()
        if len(query) > 200:
            return ToolResult(success=False, error="query exceeds the 200 character limit")
        exact_names = [
            name.strip()
            for name in (names or [])[:_MAX_DISCOVERY_RESULTS]
            if isinstance(name, str) and name.strip()
        ]
        if not query and not exact_names:
            return ToolResult(success=False, error="Provide query or at least one exact tool name")

        if not isinstance(limit, int) or isinstance(limit, bool):
            return ToolResult(success=False, error="limit must be an integer")
        bounded_limit = max(1, min(limit, _MAX_DISCOVERY_RESULTS))
        matches = await self._discover(
            session_id=context.thread_id,
            query=query,
            names=exact_names,
            limit=bounded_limit,
        )
        if not matches:
            return ToolResult(
                success=True,
                content="No available deferred tools matched. Try a more specific capability or server name.",
            )

        payload = {
            "enabled_starting_next_step": matches,
            "count": len(matches),
            "note": "Use the returned model_name exactly; its full schema will be available next step.",
        }
        return ToolResult(
            success=True,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )


__all__ = ["TOOL_SEARCH_NAME", "ToolDiscoveryTool"]
