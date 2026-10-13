"""Parse complete tool arguments without repairing or rewriting their contents."""

import json
import math
from typing import Any


class ToolArgumentsError(ValueError):
    """Model-facing failure naming only a category, never argument contents."""

    def __init__(self, category: str):
        super().__init__(
            f"tool_arguments_invalid: {category}. The tool was not executed. "
            "Regenerate the complete call with arguments as one valid JSON object."
        )


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ToolArgumentsError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ToolArgumentsError("non_finite_number")


def validate_tool_arguments(value: Any) -> dict[str, Any]:
    """Validate an SDK-decoded object; duplicate keys are no longer observable."""
    if not isinstance(value, dict):
        raise ToolArgumentsError("expected_object")

    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ToolArgumentsError("invalid_key_type")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ToolArgumentsError("non_finite_number")
        elif item is not None and not isinstance(item, (str, int, bool)):
            raise ToolArgumentsError("invalid_value_type")
    return value


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    """Parse exactly one complete JSON object.

    Empty input denotes an empty argument object. Required fields are checked
    against the tool schema by the execution layer. No partial data is returned,
    and nothing is unwrapped, extracted or repaired on the model's behalf.
    """
    if not isinstance(raw, str):
        raise ToolArgumentsError("expected_json_string")
    if not raw.strip():
        return {}

    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError:
        raise ToolArgumentsError("invalid_json") from None
    return validate_tool_arguments(value)
