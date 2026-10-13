"""Provider contracts for lossless arguments and explicit failed tool calls."""

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from packaging.requirements import Requirement

from src.agent.llm.anthropic_client import AnthropicClient
from src.agent.llm.openai_client import OpenAIClient
from src.agent.llm.openai_responses_client import OpenAIResponsesClient
from tests.helpers import FakeAsyncStream


async def stream_anthropic(raw, *, stop_reason="tool_use", delta_usage=None):
    """Feed real SSE bytes through the real SDK so its stream decoding is exercised."""
    split = len(raw) // 2
    events = [
        {"type": "message_start", "message": {
            "id": "msg-1", "type": "message", "role": "assistant", "model": "test",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 1},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "call-write", "name": "write_file", "input": {},
        }},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": raw[:split]}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": raw[split:]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": delta_usage or {"output_tokens": 7}},
        {"type": "message_stop"},
    ]
    body = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    ).encode("utf-8")
    client = AnthropicClient(api_key="test", api_base="http://anthropic.test")
    client.client = anthropic.AsyncAnthropic(
        api_key="test", base_url="http://anthropic.test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body,
            ),
        )),
    )
    return await client._make_stream_request({"model": "test", "max_tokens": 16, "messages": []})


async def parse_provider(provider, raw, *, truncated=False):
    if provider.startswith("openai"):
        client = OpenAIClient(api_key="test")
        call = {"id": "call-write", "type": "function",
                "function": {"name": "write_file", "arguments": raw}}
        finish = "length" if truncated else "tool_calls"
        envelope = {"id": "response", "created": 0, "model": "test"}
        if provider.endswith("stream"):
            split = len(raw) // 2
            chunks = [
                ChatCompletionChunk.model_validate({
                    **envelope, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "finish_reason": None,
                                 "delta": {"tool_calls": [{**call, "index": 0, "function": {
                                     "name": "write_file", "arguments": raw[:split],
                                 }}]}}],
                }),
                ChatCompletionChunk.model_validate({
                    **envelope, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "finish_reason": finish,
                                 "delta": {"tool_calls": [{"index": 0, "function": {
                                     "arguments": raw[split:],
                                 }}]}}],
                }),
            ]
            client.client.chat.completions.create = AsyncMock(return_value=FakeAsyncStream(chunks))
            return await client._make_stream_request({})
        response = ChatCompletion.model_validate({
            **envelope, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": finish, "message": {
                "role": "assistant", "tool_calls": [call],
            }}],
        })
        return client._parse_response(response)

    if provider.startswith("responses"):
        client = OpenAIResponsesClient(api_key="test")
        response = NS(
            status="incomplete" if truncated else "completed", usage=None,
            incomplete_details=NS(reason="max_output_tokens") if truncated else None,
            output=[NS(type="function_call", call_id="call-write", name="write_file",
                       arguments=raw)],
        )
        if provider.endswith("stream"):
            event = NS(type="response.incomplete" if truncated else "response.completed",
                       response=response)
            client.client.responses.create = AsyncMock(return_value=FakeAsyncStream([event]))
            return await client._make_stream_request([], None, None, None, None)
        return client._parse_response(response)

    return await stream_anthropic(raw, stop_reason="max_tokens" if truncated else "tool_use")


JSON_PROVIDERS = ["openai", "openai_stream", "responses", "responses_stream", "anthropic_stream"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", JSON_PROVIDERS)
async def test_provider_keeps_legal_content_exactly(provider):
    payload = {"path": "report.md", "content": '测试“甲”“乙”。末尾校验\n‘甲’「乙」『丙』'}
    response = await parse_provider(provider, json.dumps(payload, ensure_ascii=False))
    call = response.tool_calls[0]
    assert call.id == "call-write"
    assert call.function.arguments == payload
    assert call.arguments_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", JSON_PROVIDERS)
@pytest.mark.parametrize("raw", [
    '{"path":"report.md","content":"private-unfinished-body',
    '{"content":"first","content":"second"}',
    '{"content":NaN}',
    '[]',
    # The Anthropic SDK's stream helper raised inside its iterator on these.
    '{"content":"ok",}',
    '```json\n{"content":"ok"}\n```',
])
async def test_provider_retains_failed_call_id_and_safe_error(provider, raw):
    response = await parse_provider(provider, raw)
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id == "call-write"
    assert call.function.name == "write_file"
    assert call.function.arguments == {}
    assert call.arguments_error
    assert "private-unfinished-body" not in call.arguments_error


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", JSON_PROVIDERS)
async def test_truncation_status_wins_over_syntactically_complete_tool_call(provider):
    response = await parse_provider(provider, '{"path":"report.md","content":"looks complete"}',
                                    truncated=True)
    assert response.finish_reason == "length"
    assert response.tool_calls[0].id == "call-write"


@pytest.mark.asyncio
@pytest.mark.parametrize(("delta_usage", "prompt_tokens"), [
    ({"output_tokens": 7}, 11),
    ({"output_tokens": 7, "input_tokens": 13}, 13),
])
async def test_anthropic_raw_stream_reports_usage_like_final_message(delta_usage, prompt_tokens):
    response = await stream_anthropic('{"content":"ok"}', delta_usage=delta_usage)
    assert response.finish_reason == "tool_use"
    assert response.usage.prompt_tokens == prompt_tokens
    assert response.usage.completion_tokens == 7
    assert response.usage.total_tokens == prompt_tokens + 7


def test_declared_anthropic_sdk_floor_exposes_delta_input_tokens():
    # The raw stream reads MessageDeltaUsage.input_tokens, added in SDK 0.51.0;
    # 0.50.0 raises AttributeError on every message_delta.
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
    declared = [
        requirement.specifier
        for requirement in map(Requirement, [
            *pyproject["project"]["dependencies"],
            *(line for line in requirements if line.startswith("anthropic")),
        ])
        if requirement.name == "anthropic"
    ]
    assert len(declared) == 2
    for specifier in declared:
        assert "0.50.0" not in specifier
        assert "0.51.0" in specifier


@pytest.mark.parametrize("value", [[], {"nested": [float("inf")]}])
def test_anthropic_decoded_invalid_arguments_are_retained_as_failure(value):
    client = AnthropicClient(api_key="test")
    response = client._parse_response(NS(
        content=[NS(type="tool_use", id="call-write", name="write_file", input=value)],
        stop_reason="tool_use", usage=None,
    ))
    call = response.tool_calls[0]
    assert call.id == "call-write"
    assert call.function.arguments == {}
    assert call.arguments_error


def test_anthropic_decoded_valid_arguments_and_truncation_survive():
    payload = {"content": "测试“甲”“乙”。末尾校验"}
    client = AnthropicClient(api_key="test")
    response = client._parse_response(NS(
        content=[NS(type="tool_use", id="call-write", name="write_file", input=payload)],
        stop_reason="max_tokens", usage=None,
    ))
    assert response.finish_reason == "length"
    assert response.tool_calls[0].function.arguments == payload
    assert response.tool_calls[0].arguments_error is None


@pytest.mark.parametrize("reason, expected", [
    ("max_output_tokens", "length"), ("content_filter", "content_filter"),
    ("unknown", "incomplete"), (None, "incomplete"),
])
def test_responses_preserves_incomplete_reason_even_for_custom_calls(reason, expected):
    response = OpenAIResponsesClient(api_key="test")._parse_response(NS(
        status="incomplete", incomplete_details=NS(reason=reason), usage=None,
        output=[NS(type="custom_tool_call", call_id="call-patch", name="apply_patch",
                   input="*** Begin Patch\n*** End Patch")],
    ))
    assert response.finish_reason == expected
    assert response.tool_calls[0].id == "call-patch"
