"""Anthropic LLM client implementation."""

import asyncio
import json
import logging
from typing import Any

import anthropic
import httpx

from ..retry import RetryConfig, async_retry
from ..schema import FunctionCall, LLMResponse, Message, ToolCall
from ..schema.schema import TokenUsage
from .base import LLMClientBase
from .json_parser import ToolArgumentsError, parse_tool_arguments, validate_tool_arguments
from .tool_schema import tools_to_anthropic_schema

logger = logging.getLogger(__name__)

# 流式响应中单个事件的最大等待时间（秒）
# 超过此时间无新事件到达，视为 LLM 无响应，触发重试
STREAM_EVENT_TIMEOUT = 30


class AnthropicClient(LLMClientBase):
    """LLM client using Anthropic's protocol.

    This client uses the official Anthropic SDK and supports:
    - Extended thinking content
    - Tool calling
    - Retry logic
    """

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.minimaxi.com/anthropic",
        model: str = "MiniMax-M2",
        retry_config: RetryConfig | None = None,
        max_tokens: int = 16384,
    ):
        """Initialize Anthropic client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API (complete URL, passed directly to SDK)
            model: Model name to use
            retry_config: Optional retry configuration
            max_tokens: Maximum output tokens (from ModelConfig)
        """
        super().__init__(api_key, api_base, model, retry_config)
        self.max_tokens = max_tokens

        # Initialize async Anthropic client (non-blocking for asyncio event loop)
        self.client = anthropic.AsyncAnthropic(
            base_url=api_base,
            api_key=api_key,
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0),
            max_retries=0,
        )

    @staticmethod
    def _extract_usage(response: Any):
        """Extract token usage from Anthropic response."""
        if hasattr(response, 'usage') and response.usage:
            input_tokens = getattr(response.usage, 'input_tokens', 0) or 0
            output_tokens = getattr(response.usage, 'output_tokens', 0) or 0
            return TokenUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )
        return None

    async def _make_api_request(
        self,
        system_message: str | None,
        api_messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
    ) -> anthropic.types.Message:
        """Execute API request (core method that can be retried).

        Args:
            system_message: Optional system message
            api_messages: List of messages in Anthropic format
            tools: Optional list of tools

        Returns:
            Anthropic Message response

        Raises:
            Exception: API call failed
        """
        params = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": api_messages,
        }

        if system_message:
            params["system"] = system_message

        converted_tools = self._convert_tools(tools) if tools else None
        if converted_tools:
            params["tools"] = converted_tools

        self.last_request_snapshot = {
            "provider": "anthropic",
            **params,
        }

        # Use Anthropic async SDK's messages.create
        response = await self.client.messages.create(**params)
        return response

    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        """Convert tools to Anthropic format.

        Anthropic tool format:
        {
            "name": "tool_name",
            "description": "Tool description",
            "input_schema": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in Anthropic dict format
        """
        return tools_to_anthropic_schema(tools)

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal messages to Anthropic format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
        """
        system_message = None
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                system_message = msg.content
                continue

            # For user and assistant messages
            if msg.role in ["user", "assistant"]:
                # Handle assistant messages with thinking or tool calls
                if msg.role == "assistant" and (msg.thinking or msg.tool_calls):
                    # Build content blocks for assistant with thinking and/or tool calls
                    content_blocks = []

                    # Add thinking block if present
                    if msg.thinking:
                        content_blocks.append({"type": "thinking", "thinking": msg.thinking})

                    # Add text content if present
                    if msg.content:
                        content_blocks.append({"type": "text", "text": msg.content})

                    # Add tool use blocks
                    if msg.tool_calls:
                        for tool_call in msg.tool_calls:
                            content_blocks.append(
                                {
                                    "type": "tool_use",
                                    "id": tool_call.id,
                                    "name": tool_call.function.name,
                                    "input": tool_call.function.arguments,
                                }
                            )

                    api_messages.append({"role": "assistant", "content": content_blocks})
                else:
                    api_messages.append({"role": msg.role, "content": msg.content})

            # For tool result messages
            elif msg.role == "tool":
                # Anthropic uses user role with tool_result content blocks
                api_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )

        return system_message, api_messages

    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request for Anthropic API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing request parameters
        """
        system_message, api_messages = self._convert_messages(messages)

        return {
            "system_message": system_message,
            "api_messages": api_messages,
            "tools": tools,
        }

    def _parse_response(self, response: anthropic.types.Message) -> LLMResponse:
        """Parse Anthropic response into LLMResponse.

        Args:
            response: Anthropic Message response

        Returns:
            LLMResponse object
        """
        # Extract text content, thinking, and tool calls
        text_content = ""
        thinking_content = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_content += block.text
            elif block.type == "thinking":
                thinking_content += block.thinking
            elif block.type == "tool_use":
                # The SDK has already decoded this input; validate the values
                # still observable here without rewriting the object.
                arguments_error = None
                try:
                    arguments = validate_tool_arguments(block.input)
                except ToolArgumentsError as error:
                    arguments = {}
                    arguments_error = str(error)
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        type="function",
                        arguments_error=arguments_error,
                        function=FunctionCall(
                            name=block.name,
                            arguments=arguments,
                        ),
                    )
                )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            # 統一 finish_reason：Anthropic 用 "max_tokens" 表示截斷，
            # OpenAI 用 "length"。Agent loop 按 "length" 判斷，此處做映射。
            finish_reason="length" if response.stop_reason == "max_tokens" else (response.stop_reason or "stop"),
            usage=self._extract_usage(response),
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """Generate response from Anthropic LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            LLMResponse containing the generated content
        """
        # Prepare request
        request_params = self._prepare_request(messages, tools)

        # Make API request with retry logic
        if self.retry_config.enabled:
            # Apply retry logic
            retry_decorator = async_retry(config=self.retry_config, on_retry=self.retry_callback)
            api_call = retry_decorator(self._make_api_request)
            response = await api_call(
                request_params["system_message"],
                request_params["api_messages"],
                request_params["tools"],
            )
        else:
            # Don't use retry
            response = await self._make_api_request(
                request_params["system_message"],
                request_params["api_messages"],
                request_params["tools"],
            )

        # Parse and return response
        return self._parse_response(response)

    async def _make_stream_request(
        self,
        params: dict[str, Any],
        on_content: Any = None,
        on_thinking: Any = None,
        on_tool_call: Any = None,
    ) -> LLMResponse:
        """Execute streaming API request (core method that can be retried).

        Args:
            params: Request parameters for Anthropic API
            on_content: Optional async callback for streaming text content
            on_thinking: Optional async callback for streaming thinking content
            on_tool_call: Optional async callback for streaming tool call updates

        Returns:
            LLMResponse containing the complete generated content

        Raises:
            Exception: API call failed
        """
        # Accumulators for streaming response
        text_content = ""
        thinking_content = ""
        tool_calls = []
        stop_reason = None
        input_tokens = 0
        output_tokens = 0

        # Consume raw events instead of the SDK's messages.stream() helper: the
        # helper re-parses every input_json_delta with jiter and raises inside
        # its iterator on invalid JSON, which would abort the whole response
        # instead of returning a tool error for that call.
        stream = await self.client.messages.create(**params, stream=True)
        async with stream:
            event_iter = stream.__aiter__()
            while True:
                try:
                    event = await asyncio.wait_for(event_iter.__anext__(), timeout=STREAM_EVENT_TIMEOUT)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning(
                        "Anthropic stream event timeout (%ds no data), will retry. model=%s",
                        STREAM_EVENT_TIMEOUT, self.model,
                    )
                    # 立即關閉底層 HTTP 連接，避免 __aexit__ 嘗試讀取殘餘數據而阻塞
                    try:
                        await stream.close()
                    except Exception:
                        pass
                    raise TimeoutError(
                        f"Anthropic stream stalled: no data received for {STREAM_EVENT_TIMEOUT}s"
                    )

                # Handle different event types
                if event.type == "message_start":
                    input_tokens = event.message.usage.input_tokens
                    output_tokens = event.message.usage.output_tokens

                elif event.type == "message_delta":
                    stop_reason = event.delta.stop_reason
                    output_tokens = event.usage.output_tokens
                    # Optional cumulative field; same rule as the SDK's final snapshot.
                    if event.usage.input_tokens is not None:
                        input_tokens = event.usage.input_tokens

                elif event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "thinking":
                        # Thinking block started
                        pass
                    elif block.type == "tool_use":
                        # Tool use block started
                        tool_calls.append({
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })

                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        # Accumulate text content
                        text_content += delta.text
                        # Call streaming callback if provided
                        if on_content:
                            await on_content(delta.text)

                    elif delta.type == "thinking_delta":
                        # Accumulate thinking content
                        thinking_content += delta.thinking
                        # Call thinking callback if provided
                        if on_thinking:
                            await on_thinking(delta.thinking)

                    elif delta.type == "input_json_delta":
                        # Accumulate tool input (partial JSON)
                        if tool_calls:
                            # Append to the last tool call's input
                            current_tool = tool_calls[-1]
                            if isinstance(current_tool["input"], dict):
                                # Convert to string for accumulation
                                current_tool["input"] = delta.partial_json
                            else:
                                current_tool["input"] += delta.partial_json

                            # 🔥 调用流式回调，实时推送 tool call 增量更新
                            if on_tool_call:
                                await on_tool_call(
                                    len(tool_calls) - 1,  # tool_index
                                    current_tool["name"],  # tool_name
                                    current_tool["input"]  # partial_arguments
                                )

        # Convert tool_calls to ToolCall objects
        parsed_tool_calls = []
        for tc in tool_calls:
            arguments_error = None
            try:
                if isinstance(tc["input"], str):
                    arguments = parse_tool_arguments(tc["input"])
                else:
                    arguments = validate_tool_arguments(tc["input"])
            except ToolArgumentsError as error:
                arguments = {}
                arguments_error = str(error)

            parsed_tool_calls.append(
                ToolCall(
                    id=tc["id"],
                    type="function",
                    arguments_error=arguments_error,
                    function=FunctionCall(
                        name=tc["name"],
                        arguments=arguments,
                    ),
                )
            )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=parsed_tool_calls if parsed_tool_calls else None,
            finish_reason="length" if stop_reason == "max_tokens" else (stop_reason or "stop"),
            usage=TokenUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        on_content: Any = None,
        on_thinking: Any = None,
        on_tool_call: Any = None,
        *,
        on_message: Any = None,
    ) -> LLMResponse:
        """Generate response from Anthropic LLM with streaming support.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools
            on_content: Optional async callback for streaming text content: async def(delta: str) -> None
            on_thinking: Optional async callback for streaming thinking content: async def(delta: str) -> None
            on_tool_call: Optional async callback for streaming tool call updates: async def(tool_index: int, tool_name: str, partial_arguments: str) -> None

        Returns:
            LLMResponse containing the complete generated content
        """
        # Prepare request
        request_params = self._prepare_request(messages, tools)

        params = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": request_params["api_messages"],
        }

        if request_params["system_message"]:
            params["system"] = request_params["system_message"]

        converted_tools = self._convert_tools(request_params["tools"]) if request_params["tools"] else None
        if converted_tools:
            params["tools"] = converted_tools

        self.last_request_snapshot = {
            "provider": "anthropic",
            **params,
        }

        # Make streaming API request with retry logic
        if self.retry_config.enabled:
            # Apply retry logic
            retry_decorator = async_retry(config=self.retry_config, on_retry=self.retry_callback)
            stream_call = retry_decorator(self._make_stream_request)
            return await stream_call(params, on_content, on_thinking, on_tool_call)
        else:
            # Don't use retry
            return await self._make_stream_request(params, on_content, on_thinking, on_tool_call)
