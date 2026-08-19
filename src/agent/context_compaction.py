"""Codex-compatible local context compaction primitives.

The reference behavior lives in ``docs/codex/codex-rs/core/src/compact.rs``.
This module deliberately keeps compaction policy independent from the Agent
loop so persistence and failover can reuse the exact same replacement rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .schema import Message


COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000
DEFAULT_TOOL_OUTPUT_TRUNCATION_BYTES = 42_667
TOOL_OUTPUT_SERIALIZATION_HEADROOM = 1.2

SUMMARIZATION_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""

SUMMARY_PREFIX = """Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"""


@dataclass(frozen=True)
class CompactionCandidate:
    replacement_messages: list[Message]
    summary: str
    source_token_count: int
    replacement_token_count: int
    dropped_oldest_items: int = 0


def approx_token_count(text: str) -> int:
    """Match Codex's ceil(UTF-8 bytes / 4) approximation."""
    size = len(text.encode("utf-8"))
    return (size + 3) // 4


def _split_utf8_by_budget(value: str, left_budget: int, right_budget: int) -> tuple[str, str, int]:
    encoded_len = len(value.encode("utf-8"))
    prefix_chars: list[str] = []
    prefix_bytes = 0
    for char in value:
        cost = len(char.encode("utf-8"))
        if prefix_bytes + cost > left_budget:
            break
        prefix_chars.append(char)
        prefix_bytes += cost

    suffix_chars: list[str] = []
    suffix_bytes = 0
    for char in reversed(value):
        cost = len(char.encode("utf-8"))
        if suffix_bytes + cost > right_budget:
            break
        suffix_chars.append(char)
        suffix_bytes += cost
    suffix_chars.reverse()

    prefix = "".join(prefix_chars)
    suffix = "".join(suffix_chars)
    # Avoid overlap for very small budgets and multi-byte input.
    prefix_count = len(prefix_chars)
    suffix_count = len(suffix_chars)
    if prefix_count + suffix_count > len(value):
        suffix_count = max(len(value) - prefix_count, 0)
        suffix = value[len(value) - suffix_count :] if suffix_count else ""
    removed_chars = max(len(value) - prefix_count - suffix_count, 0)
    return prefix, suffix, removed_chars


def truncate_middle_bytes(value: str, max_bytes: int) -> str:
    """Match Codex byte-policy truncation, preserving UTF-8 prefix and suffix."""
    max_bytes = max(int(max_bytes), 0)
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    left = max_bytes // 2
    right = max_bytes - left
    prefix, suffix, removed_chars = _split_utf8_by_budget(value, left, right)
    return f"{prefix}…{removed_chars} chars truncated…{suffix}"


def truncate_middle_tokens(value: str, max_tokens: int) -> str:
    """Match Codex token-policy truncation using its four-byte estimate."""
    max_tokens = max(int(max_tokens), 0)
    byte_budget = max_tokens * 4
    if max_tokens > 0 and len(value.encode("utf-8")) <= byte_budget:
        return value
    prefix, suffix, _ = _split_utf8_by_budget(value, byte_budget // 2, byte_budget - byte_budget // 2)
    removed_bytes = max(len(value.encode("utf-8")) - byte_budget, 0)
    removed_tokens = (removed_bytes + 3) // 4
    return f"{prefix}…{removed_tokens} tokens truncated…{suffix}"


def truncate_tool_output(value: str, configured_bytes: int = DEFAULT_TOOL_OUTPUT_TRUNCATION_BYTES) -> str:
    """Apply Codex's record-time 1.2x serialization allowance."""
    budget = int(max(configured_bytes, 0) * TOOL_OUTPUT_SERIALIZATION_HEADROOM)
    return truncate_middle_bytes(value, budget)


def message_text(message: Message) -> str:
    if isinstance(message.content, str):
        return message.content
    pieces: list[str] = []
    for block in message.content:
        # Codex retains only the textual portion of real user messages in a
        # compacted replacement. Media blocks must not be serialized into
        # plain text: doing so turns data URLs/base64 payloads into permanent
        # checkpoint text and consumes the recent-user token budget.
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            pieces.append(block["text"])
    return "\n".join(piece for piece in pieces if piece)


def is_summary_message(message: Message) -> bool:
    return (
        message.role == "user"
        and message.is_synthetic
        and isinstance(message.content, str)
        and message.content.startswith(f"{SUMMARY_PREFIX}\n")
    )


def collect_real_user_messages(messages: Iterable[Message]) -> list[Message]:
    return [
        message.model_copy(deep=True)
        for message in messages
        if message.role == "user"
        and not message.is_synthetic
        and not is_summary_message(message)
    ]


def select_recent_user_messages(
    messages: Iterable[Message],
    max_tokens: int = COMPACT_USER_MESSAGE_MAX_TOKENS,
) -> list[Message]:
    """Select newest real users first, then return them chronologically."""
    selected: list[Message] = []
    remaining = max(int(max_tokens), 0)
    if remaining <= 0:
        return selected
    for message in reversed(collect_real_user_messages(messages)):
        if remaining == 0:
            break
        text = message_text(message)
        cost = approx_token_count(text)
        copy = message.model_copy(deep=True)
        if cost <= remaining:
            copy.content = text
            selected.append(copy)
            remaining -= cost
            continue
        copy.content = truncate_middle_tokens(text, remaining)
        selected.append(copy)
        break
    selected.reverse()
    return selected


def build_compacted_history(messages: Iterable[Message], summary: str) -> list[Message]:
    selected = select_recent_user_messages(messages)
    summary_body = summary if summary else "(no summary available)"
    selected.append(Message(
        role="user",
        content=f"{SUMMARY_PREFIX}\n{summary_body}",
        is_synthetic=True,
    ))
    return selected


def _strip_unsupported_media(
    message: Message,
    *,
    supports_image: bool,
    supports_video: bool,
) -> Message:
    copy = message.model_copy(deep=True)
    if not isinstance(copy.content, list):
        return copy
    content: list[dict[str, Any]] = []
    for block in copy.content:
        if not isinstance(block, dict):
            content.append({"type": "text", "text": str(block)})
            continue
        block_type = block.get("type")
        if block_type == "image_url" and not supports_image:
            continue
        if block_type == "video_url" and not supports_video:
            continue
        if block_type in {"audio", "audio_url", "input_audio"}:
            continue
        content.append(dict(block))
    copy.content = content
    return copy


def normalize_history(
    messages: Iterable[Message],
    *,
    supports_image: bool = True,
    supports_video: bool = True,
) -> list[Message]:
    """Produce provider-valid history like Codex's context manager normalization."""
    source = [
        _strip_unsupported_media(
            message,
            supports_image=supports_image,
            supports_video=supports_video,
        )
        for message in messages
        if message.role != "system"
    ]
    normalized: list[Message] = []
    index = 0
    while index < len(source):
        message = source[index]
        if message.role == "tool":
            # Orphan output.
            index += 1
            continue
        normalized.append(message)
        if message.role != "assistant" or not message.tool_calls:
            index += 1
            continue

        if message.id is None:
            message.id = f"{message.tool_calls[0].id}:assistant"

        outputs: dict[str, Message] = {}
        cursor = index + 1
        while cursor < len(source) and source[cursor].role == "tool":
            output = source[cursor]
            if output.tool_call_id and output.tool_call_id not in outputs:
                outputs[output.tool_call_id] = output
            cursor += 1
        for call in message.tool_calls:
            output = outputs.get(call.id)
            if output is None:
                output = Message(
                    role="tool",
                    id=f"{call.id}:result",
                    content="aborted",
                    tool_call_id=call.id,
                    name=call.function.name,
                    is_synthetic=True,
                    run_id=message.run_id,
                )
            else:
                output.id = output.id or f"{call.id}:result"
                output.run_id = output.run_id or message.run_id
            normalized.append(output)
        index = cursor
    return normalized


def is_context_window_error(error: BaseException) -> bool:
    """Normalize common OpenAI/Anthropic/DashScope context errors."""
    current: BaseException | None = error
    fragments: list[str] = []
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        fragments.append(str(current))
        for attr in ("code", "type", "status_code"):
            value = getattr(current, attr, None)
            if value is not None:
                fragments.append(str(value))
        current = getattr(current, "last_exception", None) or getattr(current, "__cause__", None)
    text = " ".join(fragments).lower()
    return any(fragment in text for fragment in (
        "context_length_exceeded",
        "context_window_exceeded",
        "maximum context length",
        "context window",
        "context length",
        "input is too long",
        "too many tokens",
    ))


def is_compaction_model_fallback_error(error: BaseException) -> bool:
    """Whether Codex permits retrying compaction with the target fallback model."""
    if is_context_window_error(error):
        return True
    current: BaseException | None = error
    fragments: list[str] = []
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        fragments.extend((str(current), current.__class__.__name__))
        for attr in ("code", "type"):
            value = getattr(current, attr, None)
            if value is not None:
                fragments.append(str(value))
        current = getattr(current, "last_exception", None) or getattr(current, "__cause__", None)
    text = " ".join(fragments).lower()
    return any(fragment in text for fragment in (
        "invalid request",
        "invalidrequest",
        "badrequest",
        "usage limit",
        "insufficient_quota",
        "rate limit",
        "ratelimit",
        "server overload",
        "overloaded",
        "internal error",
        "internalserver",
        "retry exhausted",
        "retry failed",
    ))
