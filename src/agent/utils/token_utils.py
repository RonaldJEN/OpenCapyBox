"""Token-based text utilities for context management."""

import tiktoken

# 模塊級緩存 encoding 對象，避免每次調用重複查找
_encoding = tiktoken.get_encoding("cl100k_base")


def truncate_text_by_tokens(text: str, max_tokens: int) -> str:
    """Truncate text by token count if it exceeds the limit.

    When text exceeds the specified token limit, performs intelligent truncation
    by keeping the front and back parts while truncating the middle.
    """
    token_count = len(_encoding.encode(text))

    if token_count <= max_tokens:
        return text

    char_count = len(text)
    if char_count == 0:
        return text

    ratio = token_count / char_count
    chars_per_half = int((max_tokens / 2) / ratio * 0.95)

    head_part = text[:chars_per_half]
    last_newline_head = head_part.rfind("\n")
    if last_newline_head > 0:
        head_part = head_part[:last_newline_head]

    tail_part = text[-chars_per_half:]
    first_newline_tail = tail_part.find("\n")
    if first_newline_tail > 0:
        tail_part = tail_part[first_newline_tail + 1:]

    truncation_note = f"\n\n... [Content truncated: {token_count} tokens -> ~{max_tokens} tokens limit] ...\n\n"
    return head_part + truncation_note + tail_part
