"""Model-facing gateway for deferred tool discovery."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from typing import Any

from .base import Tool, ToolExposure, ToolResult, ToolRuntimeContext


MCP_TOOL_SEARCH_NAME = "mcp_tool_search"
_MAX_DISCOVERY_RESULTS = 20
MAX_TOOL_SEARCH_DESCRIPTION_BYTES = 2 * 1024


def bound_tool_search_text(value: object, *, max_bytes: int) -> str:
    """Normalize and UTF-8 bound metadata before ranking or external embedding."""

    text = " ".join(str(value or "").split())[:max_bytes]
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class ToolSearchDocument:
    """Bounded, non-secret metadata for ranking one current Agent candidate."""

    model_name: str
    provider: str
    tool_name: str
    installation_id: str | None
    server_name: str
    server_description: str
    title: str
    description: str
    schema_hash: str
    connection_fingerprint: str


class DeferredToolRetriever(Protocol):
    """Non-authoritative async ranker for an already-authorized candidate set."""

    async def rank(
        self,
        query: str,
        candidates: list[ToolSearchDocument],
        *,
        limit: int,
    ) -> list[str]: ...


class DeferredToolCatalogStale(RuntimeError):
    """The Agent's MCP publication changed while discovery was in flight."""


class ToolDiscoveryTool(Tool):
    """Search deferred tools and expose ranked matches on the next model step."""

    exposure = ToolExposure.DIRECT_MODEL_ONLY

    def __init__(
        self,
        discover: Callable[..., Awaitable[list[dict[str, Any]]]],
    ) -> None:
        self._discover = discover
        self._runtime_context: ToolRuntimeContext | None = None

    @property
    def name(self) -> str:
        return MCP_TOOL_SEARCH_NAME

    @property
    def description(self) -> str:
        return (
            "Proactively match the user's request against enabled MCP "
            "connection names and capability descriptions. Matching deferred "
            "tools become available with full schemas on the next step."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural-language capability query ranked with hybrid "
                        "semantic and keyword retrieval across tool name, server, "
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
        try:
            matches = await self._discover(
                session_id=context.thread_id,
                query=query,
                names=exact_names,
                limit=bounded_limit,
            )
        except DeferredToolCatalogStale:
            return ToolResult(
                success=True,
                content=(
                    "The enabled MCP connections or tool schemas changed while "
                    "this step was running. The latest tools will be reloaded "
                    "automatically on the next user step; do not retry "
                    "mcp_tool_search in this step."
                ),
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


__all__ = [
    "MCP_TOOL_SEARCH_NAME",
    "MAX_TOOL_SEARCH_DESCRIPTION_BYTES",
    "DeferredToolCatalogStale",
    "DeferredToolRetriever",
    "ToolDiscoveryTool",
    "ToolSearchDocument",
    "bound_tool_search_text",
]
