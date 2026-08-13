"""Token-based text utilities for context management."""

import tiktoken

# 模塊級緩存 encoding 對象，避免每次調用重複查找
_encoding = tiktoken.get_encoding("cl100k_base")


def truncate_text_by_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to at most max_tokens, keeping the head and tail around a note.

    Slices the encoded token list directly, so the result is exact regardless of token
    density; character-ratio estimates overshoot badly when density is uneven. Returns
    "" when the truncation note alone cannot fit.
    """
    tokens = _encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    if max_tokens <= 0:
        return ""

    note = f"\n\n... [Content truncated: {len(tokens)} tokens -> ~{max_tokens} tokens limit] ...\n\n"
    body_budget = max_tokens - len(_encoding.encode(note))
    while body_budget > 0:
        head = body_budget // 2
        tail = body_budget - head
        result = _decode_whole_chars(tokens[:head]) + note + _decode_whole_chars(tokens[len(tokens) - tail:])
        # Re-encoding can merge or split tokens at the join points.
        overflow = len(_encoding.encode(result)) - max_tokens
        if overflow <= 0:
            return result
        body_budget -= overflow
    return ""


def _decode_whole_chars(tokens: list[int]) -> str:
    """Decode a token slice, dropping the partial UTF-8 sequence at either cut edge.

    Slicing mid-character (emoji, CJK) would otherwise decode to U+FFFD.
    """
    return _encoding.decode_bytes(tokens).decode("utf-8", errors="ignore")


def count_text_tokens(text: str) -> int:
    """Count tokens of a plain text fragment."""
    return len(_encoding.encode(text))
