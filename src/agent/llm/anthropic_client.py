"""Anthropic LLM client implementation."""

import json
import logging
from typing import Any

import anthropic

from ..retry import RetryConfig, async_retry
from ..schema import FunctionCall, LLMResponse, Message, ToolCall
from .base import LLMClientBase
from .json_parser import robust_json_parse

logger = logging.getLogger(__name__)


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

        # Initialize Anthropic client
        self.client = anthropic.Anthropic(
            base_url=api_base,
            api_key=api_key,
        )

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

        if tools:
            params["tools"] = self._convert_tools(tools)

        # Use Anthropic SDK's messages.create
        response = self.client.messages.create(**params)
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
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                result.append(tool)
            elif hasattr(tool, "to_schema"):
                # Tool object with to_schema method
                result.append(tool.to_schema())
            else:
                raise TypeError(f"Unsupported tool type: {type(tool)}")
        return result

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
                # Parse Anthropic tool_use block
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        type="function",
                        function=FunctionCall(
                            name=block.name,
                            arguments=block.input,
                        ),
                    )
                )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=response.stop_reason or "stop",
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

        # Use streaming API
        with self.client.messages.stream(**params) as stream:
            for event in stream:
                # Handle different event types
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "thinking":
                        # Thinking block started
                        pass
                    elif block.type == "tool_use":
                        # Tool use block started
                        tool_calls.append({
                            "id": block.id,
                            "name": block.name,
                            "input": {},
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

                elif event.type == "content_block_stop":
                    # Block finished
                    # Parse accumulated tool input JSON if needed
                    if tool_calls:
                        last_tool = tool_calls[-1]
                        if isinstance(last_tool["input"], str):
                            # 🔥 使用健壮的 JSON 解析器，尝试修复格式问题
                            parsed_input = robust_json_parse(last_tool["input"], last_tool["name"])

                            if parsed_input is None:
                                # 所有修复尝试都失败，标记为解析失败
                                logger.error(
                                    f"Tool call '{last_tool['name']}' will be skipped due to unparseable arguments."
                                )
                                last_tool["parse_failed"] = True
                            else:
                                # 解析成功（可能是修复后的）
                                last_tool["input"] = parsed_input

        # Get final message for finish_reason
        final_message = stream.get_final_message()

        # Convert tool_calls to ToolCall objects
        parsed_tool_calls = []
        for tc in tool_calls:
            # 🔥 跳过解析失败的工具调用
            if tc.get("parse_failed", False):
                continue

            # 确保 input 是字典类型
            if not isinstance(tc["input"], dict):
                logger.error(
                    f"Tool call '{tc['name']}' has invalid input type: {type(tc['input'])}. "
                    f"Expected dict, got {type(tc['input']).__name__}. Skipping."
                )
                continue

            parsed_tool_calls.append(
                ToolCall(
                    id=tc["id"],
                    type="function",
                    function=FunctionCall(
                        name=tc["name"],
                        arguments=tc["input"],
                    ),
                )
            )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=parsed_tool_calls if parsed_tool_calls else None,
            finish_reason=final_message.stop_reason or "stop",
        )

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        on_content: Any = None,
        on_thinking: Any = None,
        on_tool_call: Any = None,
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

        if request_params["tools"]:
            params["tools"] = self._convert_tools(request_params["tools"])

        # Make streaming API request with retry logic
        if self.retry_config.enabled:
            # Apply retry logic
            retry_decorator = async_retry(config=self.retry_config, on_retry=self.retry_callback)
            stream_call = retry_decorator(self._make_stream_request)
            return await stream_call(params, on_content, on_thinking, on_tool_call)
        else:
            # Don't use retry
            return await self._make_stream_request(params, on_content, on_thinking, on_tool_call)
