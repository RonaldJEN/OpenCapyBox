"""Shared validation for stable Skill keys."""

from __future__ import annotations

import unicodedata


MAX_SKILL_KEY_LENGTH = 128

# Skill keys are transported in JSON, URL path segments, XML-like prompt
# metadata, and database columns. Keep legacy human-readable official keys
# (spaces and parentheses are valid), while rejecting path/query delimiters,
# percent-encoding ambiguity, and invisible/control characters.
_FORBIDDEN_SKILL_KEY_CHARACTERS = frozenset("/\\?#%")

SKILL_KEY_EMPTY_ERROR = "Skill key cannot be empty"
SKILL_KEY_TOO_LONG_ERROR = (
    f"Skill key cannot exceed {MAX_SKILL_KEY_LENGTH} characters"
)
SKILL_KEY_UNSAFE_CHARACTERS_ERROR = "Skill key contains unsupported characters"
PUBLIC_SKILL_KEY_VALIDATION_ERRORS = frozenset({
    SKILL_KEY_EMPTY_ERROR,
    SKILL_KEY_TOO_LONG_ERROR,
    SKILL_KEY_UNSAFE_CHARACTERS_ERROR,
})


class SkillKeyValidationError(ValueError):
    """A validation failure whose message is safe to expose to clients."""


def normalize_skill_key(value: str, *, allow_empty: bool = False) -> str:
    """Trim and validate one stable Skill key.

    Unicode letters and legacy display-like official keys remain compatible.
    Unicode ``C*`` categories are rejected because they include control,
    formatting/bidirectional, surrogate, private-use, and unassigned code
    points that are unsafe or ambiguous in identifiers.
    """

    if not isinstance(value, str):
        raise SkillKeyValidationError(SKILL_KEY_UNSAFE_CHARACTERS_ERROR)
    key = value.strip()
    if not key:
        if allow_empty:
            return ""
        raise SkillKeyValidationError(SKILL_KEY_EMPTY_ERROR)
    if len(key) > MAX_SKILL_KEY_LENGTH:
        raise SkillKeyValidationError(SKILL_KEY_TOO_LONG_ERROR)
    if any(
        char in _FORBIDDEN_SKILL_KEY_CHARACTERS
        or unicodedata.category(char).startswith("C")
        for char in key
    ):
        raise SkillKeyValidationError(SKILL_KEY_UNSAFE_CHARACTERS_ERROR)
    return key


def normalize_optional_skill_key(value: str) -> str:
    """Pydantic adapter: normalize one preferred key while allowing blanks."""

    return normalize_skill_key(value, allow_empty=True)
