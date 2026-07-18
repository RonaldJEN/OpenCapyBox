"""Agent Tool adapter for a snapshotted remote MCP tool."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools.base import Tool, ToolExposure, ToolRef, ToolResult, ToolRuntimeContext
from src.api.services.mcp_runtime import (
    McpCallCancelled,
    McpCallOutcomeUnknown,
    McpRuntime,
    McpToolArgumentsInvalid,
    McpToolSnapshot,
    validate_mcp_tool_arguments,
)


_MAX_RESULT_TEXT_BYTES = 2 * 1024 * 1024
_MAX_STRUCTURED_CONTENT_BYTES = 2 * 1024 * 1024
_MAX_MEDIA_BASE64_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_MEDIA_BASE64_BYTES = 16 * 1024 * 1024
_MAX_MIME_TYPE_BYTES = 256
_MAX_CONTENT_ITEMS = 256


class McpToolResultTooLarge(ValueError):
    """Raised when untrusted MCP result content exceeds a model-safe boundary."""


class McpRemoteTool(Tool):
    """Expose one remote MCP tool through the existing Agent Tool contract."""

    exposure = ToolExposure.DEFERRED

    def __init__(
        self,
        *,
        user_id: str,
        snapshot: McpToolSnapshot,
        runtime: McpRuntime,
    ):
        self._user_id = user_id
        self._snapshot = snapshot
        self._runtime = runtime
        self._runtime_context: ToolRuntimeContext | None = None

    @property
    def name(self) -> str:
        return self._snapshot.model_name

    @property
    def description(self) -> str:
        return self._snapshot.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._snapshot.input_schema

    @property
    def tool_ref(self) -> ToolRef:
        return ToolRef(
            provider="mcp",
            name=self._snapshot.raw_name,
            server_id=self._snapshot.server_id,
            installation_id=self._snapshot.installation_id,
        )

    @property
    def annotations(self) -> dict[str, Any]:
        return dict(self._snapshot.annotations)

    @property
    def source(self) -> str:
        return self._snapshot.source

    @property
    def server_name(self) -> str:
        return self._snapshot.server_name

    @property
    def server_description(self) -> str | None:
        return self._snapshot.server_description

    @property
    def title(self) -> str | None:
        return self._snapshot.title

    @property
    def schema_hash(self) -> str:
        return self._snapshot.schema_hash

    @property
    def connection_fingerprint(self) -> str:
        """Endpoint/credential binding captured with the tool schema."""

        return self._snapshot.connection_fingerprint

    def current_connection_fingerprint(self) -> str | None:
        """Read the live target binding before policy or approved execution."""

        return self._runtime.current_execution_fingerprint(
            user_id=self._user_id,
            installation_id=self._snapshot.installation_id,
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> str | None:
        try:
            validate_mcp_tool_arguments(self._snapshot.input_schema, arguments)
        except McpToolArgumentsInvalid as exc:
            return str(exc)
        return None

    def set_runtime_context(self, context: ToolRuntimeContext) -> None:
        self._runtime_context = context

    def clear_runtime_context(self) -> None:
        self._runtime_context = None

    async def execute(self, **arguments: Any) -> ToolResult:
        context = self._runtime_context
        try:
            result = await self._runtime.call_tool(
                user_id=self._user_id,
                tool=self._snapshot,
                arguments=arguments,
                cancel_token=context.cancel_token if context else None,
            )
        except McpCallOutcomeUnknown as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                content="MCP 调用结果未知；远端工具可能已经执行，请勿自动重试。",
                outcome_uncertain=True,
            )
        except McpCallCancelled as exc:
            return ToolResult(success=False, error=str(exc), content="Cancelled by user")
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"MCP tool execution failed: {type(exc).__name__}: {exc}",
            )

        try:
            return _tool_result_from_mcp(result)
        except McpToolResultTooLarge as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                content="MCP tool result exceeded the configured byte limit",
            )


def _model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(by_alias=True, exclude_none=True, mode="json")
        return result if isinstance(result, dict) else {}
    return {}


def _json_text(value: Any, *, limit: int, label: str) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text.encode("utf-8")) > limit:
        raise McpToolResultTooLarge(f"MCP {label} exceeded the byte limit")
    return text


def _strip_meta(value: Any) -> Any:
    """Return JSON-like data with hidden MCP ``_meta`` removed recursively."""

    if isinstance(value, dict):
        return {
            key: _strip_meta(item)
            for key, item in value.items()
            if key != "_meta"
        }
    if isinstance(value, list):
        return [_strip_meta(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_meta(item) for item in value]
    return value


def _append_text(parts: list[str], text: str, consumed: int) -> int:
    encoded_size = len(text.encode("utf-8"))
    # Account for the newline inserted by the final join as well.
    next_size = consumed + encoded_size + (1 if parts else 0)
    if next_size > _MAX_RESULT_TEXT_BYTES:
        raise McpToolResultTooLarge("MCP tool result text exceeded the byte limit")
    parts.append(text)
    return next_size


def _media_payload(block: dict[str, Any], block_type: Any) -> str | None:
    if block_type in {"image", "audio"}:
        data = block.get("data")
        return data if isinstance(data, str) else None
    if block_type == "resource":
        resource = block.get("resource")
        if isinstance(resource, dict):
            blob = resource.get("blob")
            return blob if isinstance(blob, str) else None
    return None


def _tool_result_from_mcp(result: Any) -> ToolResult:
    """Convert MCP CallToolResult content without leaking hidden ``_meta``."""

    payload = _model_dump(result)
    content_items = getattr(result, "content", None)
    if content_items is None:
        content_items = payload.get("content", [])
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = payload.get("structuredContent", payload.get("structured_content"))
    is_error = bool(getattr(result, "isError", payload.get("isError", payload.get("is_error", False))))

    text_parts: list[str] = []
    text_bytes = 0
    media_bytes = 0
    content_blocks: list[dict[str, Any]] = []
    for index, item in enumerate(content_items or []):
        if index >= _MAX_CONTENT_ITEMS:
            raise McpToolResultTooLarge("MCP tool result contains too many content items")
        block = _model_dump(item)
        block_type = block.get("type") or getattr(item, "type", None)
        media_payload = _media_payload(block, block_type)
        if media_payload is not None:
            item_media_bytes = len(media_payload.encode("utf-8"))
            if item_media_bytes > _MAX_MEDIA_BASE64_BYTES:
                raise McpToolResultTooLarge("MCP base64 media exceeded the per-item byte limit")
            media_bytes += item_media_bytes
            if media_bytes > _MAX_TOTAL_MEDIA_BASE64_BYTES:
                raise McpToolResultTooLarge("MCP base64 media exceeded the total byte limit")
        if block_type == "text":
            text = block.get("text", getattr(item, "text", ""))
            if text:
                text_bytes = _append_text(text_parts, str(text), text_bytes)
            continue
        if block_type == "image":
            data = block.get("data", getattr(item, "data", None))
            mime_type = block.get("mimeType", block.get("mime_type", getattr(item, "mimeType", None)))
            if isinstance(data, str) and data and isinstance(mime_type, str) and mime_type:
                if len(mime_type.encode("utf-8")) > _MAX_MIME_TYPE_BYTES:
                    raise McpToolResultTooLarge("MCP media MIME type exceeded the byte limit")
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{data}"},
                })
                text_bytes = _append_text(
                    text_parts,
                    f"[MCP image: {mime_type[:256]}]",
                    text_bytes,
                )
                continue
        # Audio, embedded resources and future content types remain visible as
        # bounded JSON text rather than being silently discarded.
        if block:
            block_text = _json_text(
                _strip_meta(block),
                limit=_MAX_RESULT_TEXT_BYTES,
                label="tool content block",
            )
            text_bytes = _append_text(text_parts, block_text, text_bytes)

    if structured is not None:
        structured_text = _json_text(
            _strip_meta(structured),
            limit=_MAX_STRUCTURED_CONTENT_BYTES,
            label="structured content",
        )
        if not text_parts:
            text_bytes = _append_text(text_parts, structured_text, text_bytes)
        else:
            text_bytes = _append_text(
                text_parts,
                f"Structured content:\n{structured_text}",
                text_bytes,
            )

    content = "\n".join(text_parts)
    if is_error:
        return ToolResult(
            success=False,
            content=content,
            error=content or "MCP server returned isError=true",
            content_blocks=content_blocks or None,
        )
    return ToolResult(
        success=True,
        content=content,
        content_blocks=content_blocks or None,
    )


__all__ = ["McpRemoteTool"]
