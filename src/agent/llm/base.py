"""Base class for LLM clients."""

from abc import ABC, abstractmethod
from typing import Any

from ..retry import RetryConfig
from ..schema import LLMResponse, Message


class LLMClientBase(ABC):
    """Abstract base class for LLM clients.

    This class defines the interface that all LLM clients must implement,
    regardless of the underlying API protocol (Anthropic, OpenAI, etc.).
    """

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        retry_config: RetryConfig | None = None,
    ):
        """Initialize the LLM client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
        """
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.retry_config = retry_config or RetryConfig()

        # Callback for tracking retry count
        self.retry_callback = None

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Returns:
            LLMResponse containing the generated content, thinking, and tool calls
        """
        pass

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        on_content: Any = None,
        on_thinking: Any = None,
        on_tool_call: Any = None,
    ) -> LLMResponse:
        """Generate response from LLM with streaming support.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts
            on_content: Optional callback for streaming text content (async callable)
            on_thinking: Optional callback for streaming thinking content (async callable)
            on_tool_call: Optional callback for streaming tool call updates (async callable)
                         Signature: async def(tool_index: int, tool_name: str, partial_arguments: str) -> None

        Returns:
            LLMResponse containing the complete generated content
        """
        # Default implementation: fall back to non-streaming
        return await self.generate(messages, tools)

    @abstractmethod
    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request payload for the API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing the request payload
        """
        pass

    @abstractmethod
    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal message format to API-specific format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
        """
        pass
