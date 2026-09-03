"""OpenAI-compatible Responses API client."""

import asyncio
import json
import logging
import uuid
from typing import Any

from ..retry import async_retry
from ..schema import FunctionCall, LLMResponse, Message, ToolCall
from ..schema.schema import TokenUsage
from .json_parser import robust_json_parse
from .openai_client import OpenAIClient, STREAM_CHUNK_TIMEOUT
from .tool_schema import tools_to_responses_schema


logger = logging.getLogger(__name__)


class OpenAIResponsesClient(OpenAIClient):
    """Use the Responses API while preserving the existing Agent contract."""

    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        return tools_to_responses_schema(tools)

    @staticmethod
    def _responses_content(content: Any, *, assistant: bool) -> Any:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)

        converted: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"text", "input_text", "output_text"}:
                converted.append({
                    "type": "output_text" if assistant else "input_text",
                    "text": str(block.get("text") or ""),
                })
            elif block_type in {"image_url", "input_image"}:
                image = block.get("image_url")
                if isinstance(image, dict):
                    image_url = image.get("url")
                    detail = image.get("detail") or block.get("detail")
                else:
                    image_url = image
                    detail = block.get("detail")
                if isinstance(image_url, str) and image_url:
                    item = {"type": "input_image", "image_url": image_url}
                    if detail in {"low", "high", "auto"}:
                        item["detail"] = detail
                    converted.append(item)
            elif block_type in {"video_url", "input_video"}:
                video = block.get("video_url")
                video_url = video.get("url") if isinstance(video, dict) else video
                if isinstance(video_url, str) and video_url:
                    converted.append({"type": "input_video", "video_url": video_url})
        return converted

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                instructions.append(str(message.content))
                continue
            if message.role == "user":
                input_items.append({
                    "role": "user",
                    "content": self._responses_content(message.content, assistant=False),
                })
                continue
            if message.role == "assistant":
                if message.provider_items:
                    input_items.extend(message.provider_items)
                if message.content:
                    input_items.append({
                        "role": "assistant",
                        "content": self._responses_content(message.content, assistant=True),
                    })
                for tool_call in message.tool_calls or []:
                    if tool_call.type == "custom" or tool_call.function.name == "apply_patch":
                        input_items.append({
                            "type": "custom_tool_call",
                            "call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "input": str(tool_call.function.arguments.get("patch") or ""),
                        })
                    else:
                        input_items.append({
                            "type": "function_call",
                            "call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "arguments": json.dumps(
                                tool_call.function.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        })
                continue
            if message.role == "tool":
                if message.name == "apply_patch":
                    input_items.append({
                        "type": "custom_tool_call_output",
                        "call_id": message.tool_call_id,
                        "output": str(message.content),
                    })
                else:
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": str(message.content),
                    })

        return "\n\n".join(instructions) or None, input_items

    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        instructions, input_items = self._convert_messages(messages)
        return {
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
        }

    def _responses_reasoning_params(self) -> dict[str, Any]:
        thinking_mode, reasoning_effort = self._turn_reasoning_selection()
        params: dict[str, Any] = {}
        if reasoning_effort and reasoning_effort not in {"off", "on"}:
            params["reasoning"] = {"effort": reasoning_effort}
        extra_body: dict[str, Any] = {}
        if thinking_mode in {"enabled", "disabled"}:
            if self.thinking_wire_format == "enable_thinking":
                extra_body["enable_thinking"] = thinking_mode == "enabled"
            elif self.thinking_wire_format == "thinking_object":
                extra_body["thinking"] = {"type": thinking_mode}
        if extra_body:
            params["extra_body"] = extra_body
        return params

    def _request_params(
        self,
        messages: list[Message],
        tools: list[Any] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        prepared = self._prepare_request(messages, tools)
        params: dict[str, Any] = {
            "model": self.model,
            "input": prepared["input"],
            "max_output_tokens": self.max_tokens,
            "store": False,
            "stream": stream,
        }
        if prepared["instructions"]:
            params["instructions"] = prepared["instructions"]
        converted_tools = self._convert_tools(prepared["tools"]) if prepared["tools"] else None
        if converted_tools:
            params["tools"] = converted_tools
        params.update(self._responses_reasoning_params())
        self.last_request_snapshot = {
            "provider": "openai",
            "openai_protocol": "responses",
            **params,
        }
        return params

    @staticmethod
    def _usage(response: Any) -> TokenUsage | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        return TokenUsage(
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
        )

    def _parse_response(self, response: Any) -> LLMResponse:
        if getattr(response, "status", None) == "failed":
            error = getattr(response, "error", None)
            raise ValueError(f"Responses API failed: {error}")

        content = ""
        thinking = ""
        tool_calls: list[ToolCall] = []
        provider_items: list[dict[str, Any]] = []
        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", "")
            if item_type == "reasoning":
                provider_items.append(item.model_dump(exclude_none=True))
                for summary in getattr(item, "summary", []) or []:
                    thinking += str(getattr(summary, "text", "") or "")
            elif item_type == "message":
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", "") == "output_text":
                        content += str(getattr(part, "text", "") or "")
                    elif getattr(part, "type", "") == "refusal":
                        content += str(getattr(part, "refusal", "") or "")
            elif item_type == "function_call":
                raw_arguments = str(getattr(item, "arguments", "") or "")
                arguments = robust_json_parse(raw_arguments, getattr(item, "name", ""))
                if not isinstance(arguments, dict):
                    arguments = {"_raw": raw_arguments}
                tool_calls.append(ToolCall(
                    id=str(getattr(item, "call_id", "") or f"tc_{uuid.uuid4().hex}"),
                    type="function",
                    function=FunctionCall(
                        name=str(getattr(item, "name", "") or ""),
                        arguments=arguments,
                    ),
                ))
            elif item_type == "custom_tool_call":
                tool_calls.append(ToolCall(
                    id=str(getattr(item, "call_id", "") or f"tc_{uuid.uuid4().hex}"),
                    type="custom",
                    function=FunctionCall(
                        name=str(getattr(item, "name", "") or ""),
                        arguments={"patch": str(getattr(item, "input", "") or "")},
                    ),
                ))

        status = getattr(response, "status", "completed")
        finish_reason = "tool_calls" if tool_calls else "length" if status == "incomplete" else "stop"
        return LLMResponse(
            content=content,
            thinking=thinking or None,
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
            usage=self._usage(response),
            provider_items=provider_items or None,
        )

    async def _make_api_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        response = await self.client.responses.create(
            **self._request_params(messages, tools, stream=False)
        )
        return self._parse_response(response)

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        call = self._make_api_request
        if self.retry_config.enabled:
            call = async_retry(
                config=self.retry_config,
                on_retry=self.retry_callback,
            )(call)
        return await call(messages, tools)

    async def _make_stream_request(
        self,
        messages: list[Message],
        tools: list[Any] | None,
        on_content: Any,
        on_thinking: Any,
        on_tool_call: Any,
    ) -> LLMResponse:
        stream = await self.client.responses.create(
            **self._request_params(messages, tools, stream=True)
        )
        final_response = None
        tool_items: dict[str, tuple[int, str, str]] = {}
        iterator = stream.__aiter__()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=STREAM_CHUNK_TIMEOUT,
                    )
                except StopAsyncIteration:
                    break
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta" and on_content:
                    await on_content(str(getattr(event, "delta", "") or ""))
                elif event_type in {
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                } and on_thinking:
                    await on_thinking(str(getattr(event, "delta", "") or ""))
                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    item_type = str(getattr(item, "type", "") or "")
                    if item_type in {"function_call", "custom_tool_call"}:
                        tool_items[str(getattr(item, "id", "") or "")] = (
                            int(getattr(event, "output_index", 0) or 0),
                            str(getattr(item, "name", "") or ""),
                            str(getattr(item, "call_id", "") or ""),
                        )
                elif event_type in {
                    "response.function_call_arguments.delta",
                    "response.custom_tool_call_input.delta",
                } and on_tool_call:
                    item_id = str(getattr(event, "item_id", "") or "")
                    index, name, call_id = tool_items.get(item_id, (0, "", item_id))
                    await on_tool_call(
                        index,
                        name,
                        str(getattr(event, "delta", "") or ""),
                        call_id,
                    )
                elif event_type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                }:
                    final_response = getattr(event, "response", None)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                await close()
        if final_response is None:
            raise RuntimeError("Responses stream ended without a terminal response")
        return self._parse_response(final_response)

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        on_content: Any = None,
        on_thinking: Any = None,
        on_tool_call: Any = None,
    ) -> LLMResponse:
        call = self._make_stream_request
        if self.retry_config.enabled:
            call = async_retry(
                config=self.retry_config,
                on_retry=self.retry_callback,
            )(call)
        return await call(messages, tools, on_content, on_thinking, on_tool_call)
