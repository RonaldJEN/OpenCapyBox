"""Canonical provider projections for model-visible tool schemas."""

from __future__ import annotations

from typing import Any, Iterable


def tools_to_anthropic_schema(tools: Iterable[Any]) -> list[dict[str, Any]]:
    """Project tools exactly as the Anthropic client sends them."""

    result: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, dict):
            result.append(tool)
        elif hasattr(tool, "to_schema"):
            result.append(tool.to_schema())
        else:
            raise TypeError(f"Unsupported tool type: {type(tool)}")
    return result


def tools_to_openai_schema(tools: Iterable[Any]) -> list[dict[str, Any]]:
    """Project tools exactly as the OpenAI-compatible client sends them."""

    result: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, dict):
            if tool.get("type") == "function":
                result.append(tool)
            else:
                result.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                })
        elif hasattr(tool, "to_openai_schema"):
            result.append(tool.to_openai_schema())
        else:
            raise TypeError(f"Unsupported tool type: {type(tool)}")
    return result


def tools_to_responses_schema(tools: Iterable[Any]) -> list[dict[str, Any]]:
    """Project tools exactly as the Responses API sends them."""

    result: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, dict):
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                function = tool["function"]
                result.append({
                    "type": "function",
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {"type": "object"}),
                })
            elif "input_schema" in tool:
                result.append({
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["input_schema"],
                })
            else:
                result.append(tool)
        elif hasattr(tool, "to_responses_schema"):
            result.append(tool.to_responses_schema())
        else:
            raise TypeError(f"Unsupported tool type: {type(tool)}")
    return result
