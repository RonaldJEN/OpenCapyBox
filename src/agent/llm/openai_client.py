"""OpenAI LLM client implementation."""

import json
import logging
import uuid
from typing import Any

from openai import AsyncOpenAI

from ..retry import RetryConfig, async_retry
from ..schema import FunctionCall, LLMResponse, Message, ToolCall
from .base import LLMClientBase
from .json_parser import robust_json_parse

logger = logging.getLogger(__name__)


class OpenAIClient(LLMClientBase):
    """LLM client using OpenAI's protocol.

    This client uses the official OpenAI SDK and supports:
    - Reasoning content (via reasoning_split=True)
    - Tool calling
    - Retry logic
    """

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.minimaxi.com/v1",
        model: str = "MiniMax-M2",
        retry_config: RetryConfig | None = None,
        enable_reasoning_split: bool = True,
        max_tokens: int = 16384,
        reasoning_format: str = "none",
        enable_thinking: bool = False,
    ):
        """Initialize OpenAI client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API (complete URL, passed directly to SDK)
            model: Model name to use
            retry_config: Optional retry configuration
            enable_reasoning_split: Send extra_body.reasoning_split = true
            max_tokens: Maximum output tokens (from ModelConfig)
            reasoning_format: Thinking format: "none" | "reasoning_content" | "reasoning_details"
            enable_thinking: Send extra_body.enable_thinking = true
        """
        super().__init__(api_key, api_base, model, retry_config)

        # Initialize OpenAI client
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
        )

        # All model-specific params are now passed in from ModelConfig
        # No more model.startswith() branching!
        self.enable_reasoning_split = enable_reasoning_split
        self.reasoning_format = reasoning_format
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        logger.info(
            "OpenAIClient initialized: model=%s, reasoning_format=%s, "
            "reasoning_split=%s, enable_thinking=%s, max_tokens=%d",
            model, reasoning_format, enable_reasoning_split, enable_thinking, max_tokens,
        )

    async def _make_api_request(
        self,
        api_messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
    ) -> Any:
        """Execute API request (core method that can be retried).

        Args:
            api_messages: List of messages in OpenAI format
            tools: Optional list of tools

        Returns:
            OpenAI ChatCompletion message

        Raises:
            Exception: API call failed
        """
        params = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": self.max_tokens,
        }

        # Add reasoning params from ModelConfig (no more model.startswith() branching)
        if self.enable_reasoning_split:
            params["extra_body"] = {"reasoning_split": True}
            if self.enable_thinking:
                params["extra_body"]["enable_thinking"] = True

        if tools:
            params["tools"] = self._convert_tools(tools)

        # Use OpenAI SDK's chat.completions.create
        response = await self.client.chat.completions.create(**params)

        # Add null checks and better error handling
        if response is None:
            logger.error("API returned None response")
            raise ValueError("API returned None response")

        if not hasattr(response, 'choices') or response.choices is None:
            logger.error(f"API response missing choices field. Response: {response}")
            raise ValueError(f"API response missing choices field. Response type: {type(response)}")

        if len(response.choices) == 0:
            logger.error(f"API response has empty choices array. Response: {response}")
            raise ValueError("API response has empty choices array")

        return response.choices[0].message

    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        """Convert tools to OpenAI format.

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in OpenAI dict format
        """
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                # If already a dict, check if it's in OpenAI format
                if "type" in tool and tool["type"] == "function":
                    result.append(tool)
                else:
                    # Assume it's in Anthropic format, convert to OpenAI
                    result.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool["name"],
                                "description": tool["description"],
                                "parameters": tool["input_schema"],
                            },
                        }
                    )
            elif hasattr(tool, "to_openai_schema"):
                # Tool object with to_openai_schema method
                result.append(tool.to_openai_schema())
            else:
                raise TypeError(f"Unsupported tool type: {type(tool)}")
        return result

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal messages to OpenAI format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
            Note: OpenAI includes system message in the messages array
        """
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                # OpenAI includes system message in messages array
                api_messages.append({"role": "system", "content": msg.content})
                continue

            # For user messages
            if msg.role == "user":
                api_messages.append({"role": "user", "content": msg.content})

            # For assistant messages
            elif msg.role == "assistant":
                assistant_msg = {"role": "assistant"}

                # Add content if present
                if msg.content:
                    assistant_msg["content"] = msg.content

                # Add tool calls if present
                if msg.tool_calls:
                    tool_calls_list = []
                    for tool_call in msg.tool_calls:
                        tool_calls_list.append(
                            {
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function.name,
                                    "arguments": json.dumps(tool_call.function.arguments),
                                },
                            }
                        )
                    assistant_msg["tool_calls"] = tool_calls_list

                # IMPORTANT: Add reasoning content if thinking is present
                # This is CRITICAL for Interleaved Thinking to work properly!
                # The complete response_message (including reasoning content) must be
                # preserved in Message History and passed back to the model in the next turn.
                # This ensures the model's chain of thought is not interrupted.
                if msg.thinking:
                    if self.reasoning_format == "reasoning_content":
                        # GLM/Qwen/deepseek format: reasoning_content (string)
                        assistant_msg["reasoning_content"] = msg.thinking
                    elif self.reasoning_format == "reasoning_details":
                        # MiniMax format: reasoning_details (list)
                        assistant_msg["reasoning_details"] = [{"text": msg.thinking}]

                api_messages.append(assistant_msg)

            # For tool result messages
            elif msg.role == "tool":
                api_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )

        return None, api_messages

    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request for OpenAI API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing request parameters
        """
        _, api_messages = self._convert_messages(messages)

        return {
            "api_messages": api_messages,
            "tools": tools,
        }

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse OpenAI response into LLMResponse.

        Args:
            response: OpenAI ChatCompletionMessage response

        Returns:
            LLMResponse object
        """
        # Extract text content
        text_content = response.content or ""

        # Extract thinking content - support both MiniMax and GLM formats
        thinking_content = ""

        # Method 1: GLM format - reasoning_content (string)
        if hasattr(response, "reasoning_content") and response.reasoning_content:
            thinking_content = response.reasoning_content
            logger.debug("Extracted reasoning from reasoning_content (GLM format)")

        # Method 2: MiniMax format - reasoning_details (list)
        elif hasattr(response, "reasoning_details") and response.reasoning_details:
            for detail in response.reasoning_details:
                if hasattr(detail, "text"):
                    thinking_content += detail.text
            logger.debug("Extracted reasoning from reasoning_details (MiniMax format)")

        # Extract tool calls
        tool_calls = []
        if response.tool_calls:
            for i, tool_call in enumerate(response.tool_calls):
                # Parse arguments from JSON string
                arguments = json.loads(tool_call.function.arguments)

                # 🔧 确保 ID 不为空：如果 API 没提供有效 ID，生成一个
                tc_id = tool_call.id if tool_call.id and tool_call.id.strip() else f"tc_{uuid.uuid4().hex[:16]}_{i}"

                tool_calls.append(
                    ToolCall(
                        id=tc_id,
                        type="function",
                        function=FunctionCall(
                            name=tool_call.function.name,
                            arguments=arguments,
                        ),
                    )
                )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason="stop",  # OpenAI doesn't provide finish_reason in the message
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """Generate response from OpenAI LLM.

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
                request_params["api_messages"],
                request_params["tools"],
            )
        else:
            # Don't use retry
            response = await self._make_api_request(
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
            params: Request parameters for OpenAI API
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
        tool_calls_dict = {}  # Map tool call index to accumulated data
        finish_reason = "stop"

        # Use OpenAI SDK's streaming API
        # Note: create() returns a coroutine that needs to be awaited to get the stream
        stream = await self.client.chat.completions.create(**params)

        async for chunk in stream:
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            # Update finish_reason if present
            if choice.finish_reason:
                finish_reason = choice.finish_reason

            # Handle text content delta
            if hasattr(delta, "content") and delta.content and isinstance(delta.content, str):
                text_content += delta.content
                # Call streaming callback if provided
                if on_content:
                    await on_content(delta.content)

            # Handle reasoning content delta (MiniMax format)
            if hasattr(delta, "reasoning_details") and delta.reasoning_details:
                for detail in delta.reasoning_details:
                    if hasattr(detail, "text") and detail.text and isinstance(detail.text, str):
                        thinking_content += detail.text
                        # Call thinking callback if provided
                        if on_thinking:
                            await on_thinking(detail.text)

            # Handle reasoning content delta (GLM format)
            if hasattr(delta, "reasoning_content") and delta.reasoning_content and isinstance(delta.reasoning_content, str):
                thinking_content += delta.reasoning_content
                # Call thinking callback if provided
                if on_thinking:
                    await on_thinking(delta.reasoning_content)

            # Handle tool calls delta
            if delta.tool_calls:
                for tool_call_delta in delta.tool_calls:
                    idx = tool_call_delta.index

                    # Initialize tool call entry if not exists
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {
                                "name": "",
                                "arguments": "",
                            },
                        }

                    # Accumulate tool call data
                    if tool_call_delta.id:
                        tool_calls_dict[idx]["id"] = tool_call_delta.id

                    if tool_call_delta.function:
                        if tool_call_delta.function.name:
                            tool_calls_dict[idx]["function"]["name"] = tool_call_delta.function.name
                        if tool_call_delta.function.arguments:
                            # 🔍 调试日志：记录每个原始参数块
                            logger.debug(
                                f"Tool call [{idx}] '{tool_calls_dict[idx]['function']['name']}' "
                                f"arguments chunk: {repr(tool_call_delta.function.arguments)}"
                            )
                            tool_calls_dict[idx]["function"]["arguments"] += tool_call_delta.function.arguments

                            # 🔥 调用流式回调，实时推送 tool call 增量更新
                            if on_tool_call:
                                await on_tool_call(
                                    idx,
                                    tool_calls_dict[idx]["function"]["name"],
                                    tool_calls_dict[idx]["function"]["arguments"],
                                    tool_calls_dict[idx]["id"]  # 🔥 添加 tool_call_id 参数
                                )

        # Convert accumulated tool calls to ToolCall objects
        tool_calls = []
        for idx in sorted(tool_calls_dict.keys()):
            tc = tool_calls_dict[idx]

            # 🔥 使用健壮的 JSON 解析器，尝试修复格式问题
            tool_name_raw = tc["function"]["name"]
            tool_name = tool_name_raw.strip() if isinstance(tool_name_raw, str) else ""
            if not tool_name:
                logger.warning(f"Skipping tool call at index {idx}: empty or invalid tool name")
                continue

            arguments_str = tc["function"]["arguments"]

            # 🔍 调试日志：记录完整的原始参数字符串
            logger.debug(
                f"[RAW_JSON] Tool '{tool_name}': {repr(arguments_str[:500])}..."
                if len(arguments_str) > 500 else
                f"[RAW_JSON] Tool '{tool_name}': {repr(arguments_str)}"
            )

            if not arguments_str or not arguments_str.strip():
                arguments = {}
            else:
                # 尝试健壮解析
                arguments = robust_json_parse(arguments_str, tool_name)

                # 如果所有修复尝试都失败，跳过此工具调用
                if arguments is None:
                    logger.error(
                        f"Tool call '{tool_name}' will be skipped due to unparseable arguments."
                    )
                    continue

            if not isinstance(arguments, dict):
                logger.warning(
                    f"Tool call '{tool_name}' will be skipped because parsed arguments are not a dict: "
                    f"{type(arguments).__name__}"
                )
                continue

            # 🔧 确保 ID 不为空：如果流式响应中没有收到有效 ID，生成一个
            tc_id = tc["id"] if tc["id"] and tc["id"].strip() else f"tc_{uuid.uuid4().hex[:16]}_{idx}"

            tool_calls.append(
                ToolCall(
                    id=tc_id,
                    type="function",
                    function=FunctionCall(
                        name=tool_name,
                        arguments=arguments,
                    ),
                )
            )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=finish_reason,
        )

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        on_content: Any = None,
        on_thinking: Any = None,
        on_tool_call: Any = None,
    ) -> LLMResponse:
        """Generate response from OpenAI LLM with streaming support.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools
            on_content: Optional async callback for streaming text content: async def(delta: str) -> None
            on_thinking: Optional async callback for streaming thinking content: async def(delta: str) -> None
            on_tool_call: Optional async callback for streaming tool call updates: async def(tool_index: int, tool_name: str, partial_arguments: str, tool_call_id: str) -> None

        Returns:
            LLMResponse containing the complete generated content
        """
        # Prepare request
        request_params = self._prepare_request(messages, tools)
        params = {
            "model": self.model,
            "messages": request_params["api_messages"],
            "max_tokens": self.max_tokens,
            "stream": True,  # Enable streaming
        }

        # Add reasoning params from ModelConfig (no more model.startswith() branching)
        if self.enable_reasoning_split:
            params["extra_body"] = {"reasoning_split": True}
            if self.enable_thinking:
                params["extra_body"]["enable_thinking"] = True

        if request_params["tools"]:
            # params["tools"] = request_params["tools"]
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
