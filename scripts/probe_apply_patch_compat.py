"""Probe model compatibility with Responses custom/freeform ``apply_patch``.

The probe never executes a returned tool call. It only asks configured models to
produce an ``apply_patch`` call, validates the wire shape, and parses the patch
with OpenCapyBox's production parser.

Examples (PowerShell, from the repository root):

    python scripts/probe_apply_patch_compat.py --list-models
    python scripts/probe_apply_patch_compat.py --model qwen3.8-flash
    python scripts/probe_apply_patch_compat.py --model qwen3.8-flash --runs 3
    python scripts/probe_apply_patch_compat.py --all-enabled --scenario responses-custom-auto
    python scripts/probe_apply_patch_compat.py --model qwen3.8-flash --llm-call-id 3389
    python scripts/probe_apply_patch_compat.py --model qwen3.8-flash --thinking enabled --reasoning-effort xhigh

By default, four scenarios are compared:

* Responses custom/freeform with natural tool choice.
* Responses custom/freeform with a forced custom tool choice.
* Responses function calling with a forced JSON ``patch`` argument.
* Chat Completions function calling with a forced JSON ``patch`` argument.

Use ``--tool-scope snapshot`` with ``--llm-call-id`` to replay the captured tool
catalog. The default ``patch`` scope exposes only ``apply_patch`` so the result
measures its wire compatibility rather than general tool selection quality.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from dotenv import load_dotenv
from openai import AsyncOpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from src.agent.tools.apply_patch_tool import (  # noqa: E402
    PatchError,
    SandboxApplyPatchTool,
    parse_patch,
)
from src.api.models.database import SessionLocal  # noqa: E402
from src.api.models.llm_call_record import LLMCallRecord  # noqa: E402
from src.api.models.llm_model import LLMModel  # noqa: E402
from src.api.services.model_access_service import db_model_to_config  # noqa: E402


SCENARIOS = (
    "responses-custom-auto",
    "responses-custom-forced",
    "responses-function-auto",
    "responses-function-forced",
    "chat-function-auto",
    "chat-function-forced",
)
DEFAULT_SCENARIOS = (
    "responses-custom-auto",
    "responses-custom-forced",
    "responses-function-forced",
    "chat-function-forced",
)

DEFAULT_INSTRUCTIONS = (
    "You are a code-editing agent. Use the available apply_patch tool for the "
    "requested edit. Do not claim to execute the patch yourself."
)
DEFAULT_PROMPT = """Update `probe/example.js` using apply_patch.

Current file:
```js
export const status = "draft";
export const retries = 1;
```

Change only `status` from `draft` to `ready`.
"""


@dataclass(frozen=True)
class ProbeContext:
    source: str
    instructions: str
    responses_input: list[dict[str, Any]]
    chat_messages: list[dict[str, Any]]
    snapshot_tools: list[dict[str, Any]] | None = None
    notes: tuple[str, ...] = ()


@dataclass
class ToolObservation:
    type: str
    name: str
    wire_form: str
    wire_valid: bool
    patch_valid: bool
    length: int
    sha256: str
    prefix: str
    patch_actions: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ProbeResult:
    model_id: str
    model_name: str
    api_host: str
    scenario: str
    run: int
    ok: bool
    verdict: str
    latency_ms: int
    response_status: str | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_observations: list[ToolObservation] = field(default_factory=list)
    output_item_types: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["tool_observations"] = [asdict(item) for item in self.tool_observations]
        return value


def _safe_host(api_base: str) -> str:
    parsed = urlsplit(api_base)
    return parsed.hostname or "unknown"


def _short_error(exc: Exception, limit: int = 700) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "input_text", "output_text"}:
            parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part)


def _default_context(prompt_file: Path | None) -> ProbeContext:
    prompt = prompt_file.read_text(encoding="utf-8") if prompt_file else DEFAULT_PROMPT
    return ProbeContext(
        source=str(prompt_file) if prompt_file else "builtin:minimal-patch",
        instructions=DEFAULT_INSTRUCTIONS,
        responses_input=[{
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        }],
        chat_messages=[
            {"role": "system", "content": DEFAULT_INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ],
    )


def _snapshot_dict(call_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.query(LLMCallRecord).filter(LLMCallRecord.id == call_id).first()
        if row is None:
            raise ValueError(f"LLM call record does not exist: {call_id}")
        try:
            stored = json.loads(row.request_messages)
        finally:
            db.rollback()
    if (
        not isinstance(stored, list)
        or not stored
        or not isinstance(stored[0], dict)
        or "input" not in stored[0]
    ):
        raise ValueError(
            f"LLM call record {call_id} does not contain a Responses request snapshot"
        )
    return stored[0]


def _relevant_snapshot_tail(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    user_indices = [
        index
        for index, item in enumerate(items)
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    if not user_indices:
        raise ValueError("Responses snapshot contains no user input item")
    last_user = user_indices[-1]
    prior_assistant = next(
        (
            index
            for index in range(last_user - 1, -1, -1)
            if items[index].get("role") == "assistant"
        ),
        None,
    )
    start = prior_assistant if prior_assistant is not None else last_user
    selected: list[dict[str, Any]] = []
    dropped_reasoning = 0
    for item in items[start:]:
        if item.get("type") == "reasoning":
            dropped_reasoning += 1
            continue
        selected.append(dict(item))
    return selected, dropped_reasoning


def _responses_items_to_chat(
    instructions: str,
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    dropped_media = 0
    index = 0
    while index < len(items):
        item = items[index]
        role = item.get("role")
        item_type = item.get("type")
        if role in {"user", "assistant"}:
            text = _text_content(item.get("content"))
            if not text and isinstance(item.get("content"), list):
                dropped_media += sum(
                    1
                    for block in item["content"]
                    if isinstance(block, dict)
                    and block.get("type") not in {"text", "input_text", "output_text"}
                )
            if text:
                messages.append({"role": role, "content": text})
            index += 1
            continue

        if item_type in {"function_call", "custom_tool_call"}:
            calls: list[dict[str, Any]] = []
            while index < len(items) and items[index].get("type") in {
                "function_call",
                "custom_tool_call",
            }:
                current = items[index]
                arguments = (
                    str(current.get("arguments") or "{}")
                    if current.get("type") == "function_call"
                    else json.dumps(
                        {"patch": str(current.get("input") or "")},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                calls.append({
                    "id": str(current.get("call_id") or f"probe-call-{index}"),
                    "type": "function",
                    "function": {
                        "name": str(current.get("name") or ""),
                        "arguments": arguments,
                    },
                })
                index += 1
            messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            continue

        if item_type in {"function_call_output", "custom_tool_call_output"}:
            messages.append({
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or ""),
                "content": str(item.get("output") or ""),
            })
            index += 1
            continue

        index += 1
    return messages, dropped_media


def _snapshot_context(call_id: int) -> ProbeContext:
    snapshot = _snapshot_dict(call_id)
    raw_input = snapshot.get("input")
    if not isinstance(raw_input, list) or not all(isinstance(item, dict) for item in raw_input):
        raise ValueError(f"LLM call record {call_id} has invalid Responses input")
    selected, dropped_reasoning = _relevant_snapshot_tail(raw_input)
    instructions = str(snapshot.get("instructions") or "")
    chat_messages, dropped_media = _responses_items_to_chat(instructions, selected)
    notes: list[str] = []
    if dropped_reasoning:
        notes.append(
            f"dropped {dropped_reasoning} provider-native reasoning item(s) for cross-model replay"
        )
    if dropped_media:
        notes.append(f"dropped {dropped_media} non-text media block(s) from Chat replay")
    tools = snapshot.get("tools")
    return ProbeContext(
        source=f"llm_call_records.id={call_id}",
        instructions=instructions,
        responses_input=selected,
        chat_messages=chat_messages,
        snapshot_tools=(
            [dict(item) for item in tools if isinstance(item, dict)]
            if isinstance(tools, list)
            else None
        ),
        notes=tuple(notes),
    )


def _schemas() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    tool = SandboxApplyPatchTool(None)  # The probe never executes the tool.
    custom = tool.to_responses_schema()
    chat = tool.to_openai_schema()
    function = {
        "type": "function",
        "name": chat["function"]["name"],
        "description": chat["function"].get("description", ""),
        "parameters": chat["function"]["parameters"],
    }
    return custom, function, chat


def _to_chat_tool(tool: dict[str, Any], patch_chat: dict[str, Any]) -> dict[str, Any] | None:
    if tool.get("name") == "apply_patch" or (
        isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == "apply_patch"
    ):
        return patch_chat
    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        return dict(tool)
    if tool.get("type") == "function" and isinstance(tool.get("name"), str):
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object"}),
            },
        }
    if "input_schema" in tool and isinstance(tool.get("name"), str):
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
    return None


def _scenario_tools(
    context: ProbeContext,
    scenario: str,
    tool_scope: str,
) -> list[dict[str, Any]]:
    custom, function, chat = _schemas()
    if tool_scope == "patch":
        if scenario.startswith("responses-custom"):
            return [custom]
        if scenario.startswith("responses-function"):
            return [function]
        return [chat]

    if context.snapshot_tools is None:
        raise ValueError("--tool-scope snapshot requires --llm-call-id")
    if scenario.startswith("responses-custom"):
        return [dict(item) for item in context.snapshot_tools]
    if scenario.startswith("responses-function"):
        result: list[dict[str, Any]] = []
        for item in context.snapshot_tools:
            if item.get("name") == "apply_patch":
                result.append(function)
            else:
                result.append(dict(item))
        return result
    return [
        converted
        for item in context.snapshot_tools
        if (converted := _to_chat_tool(item, chat)) is not None
    ]


def _validate_patch(raw: str) -> tuple[bool, list[str], str | None]:
    try:
        parsed = parse_patch(raw)
    except PatchError as exc:
        return False, [], f"PatchError: {exc}"
    except Exception as exc:  # pragma: no cover - diagnostic boundary
        return False, [], _short_error(exc)
    return True, [f"{action.kind}:{action.path}" for action in parsed.actions], None


def _observation(
    *,
    item_type: str,
    name: str,
    raw: str,
) -> ToolObservation:
    prefix = raw[:80].replace("\r", "\\r").replace("\n", "\\n")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if name != "apply_patch":
        return ToolObservation(
            type=item_type,
            name=name,
            wire_form="unexpected_tool",
            wire_valid=False,
            patch_valid=False,
            length=len(raw),
            sha256=digest,
            prefix=prefix,
        )

    if item_type == "custom_tool_call":
        wire_form = "raw_text"
        candidate = raw
        wire_valid = True
        if raw.lstrip().startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                wire_form = "json_like_text"
                wire_valid = False
            else:
                if (
                    isinstance(parsed, dict)
                    and set(parsed) == {"data"}
                    and isinstance(parsed.get("data"), str)
                ):
                    wire_form = "json_data_wrapper"
                    candidate = parsed["data"]
                else:
                    wire_form = "json_wrapper"
                wire_valid = False
        patch_valid, actions, error = _validate_patch(candidate)
        return ToolObservation(
            type=item_type,
            name=name,
            wire_form=wire_form,
            wire_valid=wire_valid,
            patch_valid=patch_valid,
            length=len(raw),
            sha256=digest,
            prefix=prefix,
            patch_actions=actions,
            error=error,
        )

    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ToolObservation(
            type=item_type,
            name=name,
            wire_form="invalid_json_arguments",
            wire_valid=False,
            patch_valid=False,
            length=len(raw),
            sha256=digest,
            prefix=prefix,
            error=f"JSONDecodeError: {exc}",
        )
    if not isinstance(arguments, dict) or not isinstance(arguments.get("patch"), str):
        return ToolObservation(
            type=item_type,
            name=name,
            wire_form="missing_patch_argument",
            wire_valid=False,
            patch_valid=False,
            length=len(raw),
            sha256=digest,
            prefix=prefix,
            error="function arguments must contain a string patch field",
        )
    candidate = arguments["patch"]
    patch_valid, actions, error = _validate_patch(candidate)
    return ToolObservation(
        type=item_type,
        name=name,
        wire_form="json_patch_argument",
        wire_valid=True,
        patch_valid=patch_valid,
        length=len(raw),
        sha256=digest,
        prefix=candidate[:80].replace("\r", "\\r").replace("\n", "\\n"),
        patch_actions=actions,
        error=error,
    )


def _verdict(observations: list[ToolObservation]) -> str:
    patches = [item for item in observations if item.name == "apply_patch"]
    unexpected = [item for item in observations if item.name != "apply_patch"]
    if not patches:
        return "NO_PATCH_CALL" if not unexpected else "UNEXPECTED_TOOL_CALL"
    if any(not item.wire_valid for item in patches):
        return "INVALID_TOOL_WIRE"
    if any(not item.patch_valid for item in patches):
        return "INVALID_PATCH"
    return "PASS_WITH_UNEXPECTED_CALLS" if unexpected else "PASS"


def _response_observations(response: Any) -> tuple[list[ToolObservation], list[str]]:
    observations: list[ToolObservation] = []
    item_types: list[str] = []
    for item in getattr(response, "output", []) or []:
        item_type = str(getattr(item, "type", "") or "")
        item_types.append(item_type)
        if item_type == "custom_tool_call":
            observations.append(_observation(
                item_type=item_type,
                name=str(getattr(item, "name", "") or ""),
                raw=str(getattr(item, "input", "") or ""),
            ))
        elif item_type == "function_call":
            observations.append(_observation(
                item_type=item_type,
                name=str(getattr(item, "name", "") or ""),
                raw=str(getattr(item, "arguments", "") or ""),
            ))
    return observations, item_types


def _chat_observations(response: Any) -> tuple[list[ToolObservation], list[str]]:
    message = response.choices[0].message
    observations = [
        _observation(
            item_type="function_call",
            name=str(call.function.name or ""),
            raw=str(call.function.arguments or ""),
        )
        for call in (message.tool_calls or [])
    ]
    item_types = ["function_call" for _call in (message.tool_calls or [])]
    if message.content:
        item_types.append("message")
    return observations, item_types


def _reasoning_params(args: argparse.Namespace, *, responses: bool) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.reasoning_effort:
        key = "reasoning" if responses else "reasoning_effort"
        params[key] = (
            {"effort": args.reasoning_effort}
            if responses
            else args.reasoning_effort
        )
    if args.thinking != "omit":
        params["extra_body"] = {"enable_thinking": args.thinking == "enabled"}
    return params


async def _probe_once(
    *,
    config: Any,
    context: ProbeContext,
    scenario: str,
    run: int,
    args: argparse.Namespace,
    semaphore: asyncio.Semaphore,
) -> ProbeResult:
    started = time.perf_counter()
    host = _safe_host(config.api_base)
    try:
        api_key = config.resolve_api_key()
    except Exception as exc:
        return ProbeResult(
            model_id=config.id,
            model_name=config.model_name,
            api_host=host,
            scenario=scenario,
            run=run,
            ok=False,
            verdict="CONFIG_ERROR",
            latency_ms=0,
            error=_short_error(exc),
        )

    try:
        tools = _scenario_tools(context, scenario, args.tool_scope)
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.api_base,
            timeout=args.timeout,
            max_retries=0,
        )
        async with semaphore:
            if scenario.startswith("responses-"):
                params: dict[str, Any] = {
                    "model": config.model_name,
                    "input": context.responses_input,
                    "tools": tools,
                    "max_output_tokens": args.max_output_tokens,
                    "store": False,
                }
                if context.instructions:
                    params["instructions"] = context.instructions
                if scenario.endswith("-forced"):
                    params["tool_choice"] = {
                        "type": "custom" if "-custom-" in scenario else "function",
                        "name": "apply_patch",
                    }
                params.update(_reasoning_params(args, responses=True))
                response = await client.responses.create(**params)
                observations, item_types = _response_observations(response)
                usage = getattr(response, "usage", None)
                status = str(getattr(response, "status", "") or "")
                finish_reason = None
                input_tokens = getattr(usage, "input_tokens", None)
                output_tokens = getattr(usage, "output_tokens", None)
            else:
                params = {
                    "model": config.model_name,
                    "messages": context.chat_messages,
                    "tools": tools,
                    "max_tokens": args.max_output_tokens,
                }
                if scenario.endswith("-forced"):
                    params["tool_choice"] = {
                        "type": "function",
                        "function": {"name": "apply_patch"},
                    }
                params.update(_reasoning_params(args, responses=False))
                response = await client.chat.completions.create(**params)
                observations, item_types = _chat_observations(response)
                usage = response.usage
                status = None
                finish_reason = response.choices[0].finish_reason
                input_tokens = getattr(usage, "prompt_tokens", None)
                output_tokens = getattr(usage, "completion_tokens", None)
        latency_ms = round((time.perf_counter() - started) * 1000)
        return ProbeResult(
            model_id=config.id,
            model_name=config.model_name,
            api_host=host,
            scenario=scenario,
            run=run,
            ok=True,
            verdict=_verdict(observations),
            latency_ms=latency_ms,
            response_status=status,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_observations=observations,
            output_item_types=item_types,
        )
    except Exception as exc:
        return ProbeResult(
            model_id=config.id,
            model_name=config.model_name,
            api_host=host,
            scenario=scenario,
            run=run,
            ok=False,
            verdict="REQUEST_ERROR",
            latency_ms=round((time.perf_counter() - started) * 1000),
            error=_short_error(exc),
        )


def _load_configs(model_ids: list[str], all_enabled: bool) -> list[Any]:
    with SessionLocal() as db:
        query = db.query(LLMModel).filter(LLMModel.provider == "openai")
        if all_enabled:
            rows = query.filter(LLMModel.enabled.is_(True)).order_by(LLMModel.model_id).all()
        else:
            rows = query.filter(LLMModel.model_id.in_(model_ids)).all()
        configs = [db_model_to_config(row) for row in rows]
        db.rollback()
    by_id = {config.id: config for config in configs}
    missing = [model_id for model_id in model_ids if model_id not in by_id]
    if missing:
        raise ValueError(f"Unknown or non-OpenAI model id(s): {', '.join(missing)}")
    return sorted(configs, key=lambda item: item.id)


def _list_models() -> int:
    with SessionLocal() as db:
        rows = (
            db.query(LLMModel)
            .filter(LLMModel.provider == "openai")
            .order_by(LLMModel.model_id)
            .all()
        )
        for row in rows:
            print(json.dumps({
                "id": row.model_id,
                "model_name": row.model_name,
                "configured_protocol": row.openai_protocol or "chat_completions",
                "api_host": _safe_host(row.api_base),
                "enabled": bool(row.enabled),
                "api_key_configured": bool(row.api_key),
            }, ensure_ascii=False))
        db.rollback()
    return 0


def _summary(results: Iterable[ProbeResult]) -> None:
    grouped: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for result in results:
        grouped[(result.model_id, result.scenario)][result.verdict] += 1
    print("\n=== apply_patch compatibility summary ===")
    for (model_id, scenario), counts in sorted(grouped.items()):
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        print(f"{model_id:24} {scenario:30} {rendered}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="DB model id to probe. Repeat for multiple models.",
    )
    parser.add_argument(
        "--all-enabled",
        action="store_true",
        help="Probe every enabled DB model using provider=openai.",
    )
    parser.add_argument("--list-models", action="store_true", help="List safe model metadata and exit.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIOS,
        help="Scenario to run. Repeat as needed; defaults to the four-scenario suite.",
    )
    parser.add_argument("--runs", type=int, default=2, help="Samples per model/scenario (default: 2).")
    parser.add_argument("--concurrency", type=int, default=4, help="Maximum concurrent API requests.")
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-request timeout in seconds.")
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=4096,
        help="Maximum generated tokens per request.",
    )
    parser.add_argument(
        "--llm-call-id",
        type=int,
        help="Replay the latest semantic tail of a stored Responses request snapshot.",
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        help="UTF-8 text prompt for the built-in minimal context.",
    )
    parser.add_argument(
        "--tool-scope",
        choices=("patch", "snapshot"),
        default="patch",
        help="Expose only apply_patch or the captured tool catalog (default: patch).",
    )
    parser.add_argument(
        "--thinking",
        choices=("omit", "enabled", "disabled"),
        default="omit",
        help="Optional provider extension sent as extra_body.enable_thinking.",
    )
    parser.add_argument(
        "--reasoning-effort",
        help="Optional reasoning effort; encoded per endpoint.",
    )
    parser.add_argument("--json-output", type=Path, help="Also write the complete JSON report.")
    parser.add_argument("--summary-only", action="store_true", help="Do not print one JSON line per run.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless every run has verdict PASS.",
    )
    return parser


async def _run(args: argparse.Namespace) -> list[ProbeResult]:
    context = (
        _snapshot_context(args.llm_call_id)
        if args.llm_call_id is not None
        else _default_context(args.prompt_file)
    )
    configs = _load_configs(args.model, args.all_enabled)
    scenarios = tuple(args.scenario or DEFAULT_SCENARIOS)
    semaphore = asyncio.Semaphore(args.concurrency)
    print(json.dumps({
        "event": "probe_start",
        "context": context.source,
        "notes": list(context.notes),
        "models": [config.id for config in configs],
        "scenarios": list(scenarios),
        "runs": args.runs,
        "tool_scope": args.tool_scope,
        "thinking": args.thinking,
        "reasoning_effort": args.reasoning_effort,
        "executes_tools": False,
    }, ensure_ascii=False))
    tasks = [
        _probe_once(
            config=config,
            context=context,
            scenario=scenario,
            run=run,
            args=args,
            semaphore=semaphore,
        )
        for config in configs
        for scenario in scenarios
        for run in range(1, args.runs + 1)
    ]
    results = await asyncio.gather(*tasks)
    if not args.summary_only:
        for result in results:
            print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
    _summary(results)
    if args.json_output:
        report = {
            "context": context.source,
            "notes": list(context.notes),
            "executes_tools": False,
            "results": [result.to_dict() for result in results],
        }
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON report: {args.json_output}")
    return results


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list_models:
        return _list_models()
    if args.llm_call_id is not None and args.prompt_file is not None:
        raise SystemExit("--llm-call-id and --prompt-file are mutually exclusive")
    if not args.model and not args.all_enabled:
        raise SystemExit("Select at least one --model or pass --all-enabled")
    if args.runs < 1 or args.runs > 20:
        raise SystemExit("--runs must be between 1 and 20")
    if args.concurrency < 1 or args.concurrency > 32:
        raise SystemExit("--concurrency must be between 1 and 32")
    if args.max_output_tokens < 1:
        raise SystemExit("--max-output-tokens must be positive")
    results = asyncio.run(_run(args))
    if args.strict and any(result.verdict != "PASS" for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
