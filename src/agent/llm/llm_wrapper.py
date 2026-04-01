"""LLM client wrapper that supports multiple providers.

This module provides a unified interface for different LLM providers
(Anthropic and OpenAI) through a single LLMClient class.

Supports two initialization modes:
  1. from_model_config(config) — 推薦：從 ModelConfig 創建，消除所有硬編碼
  2. __init__(api_key, provider, ...) — 向後兼容：原有 CLI 等場景
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..retry import RetryConfig
from ..schema import LLMProvider, LLMResponse, Message
from .anthropic_client import AnthropicClient
from .base import LLMClientBase
from .openai_client import OpenAIClient

if TYPE_CHECKING:
    from src.api.model_registry import ModelConfig

logger = logging.getLogger(__name__)


class LLMClient:
    """LLM Client wrapper supporting multiple providers.

    This class provides a unified interface for different LLM providers.

    Preferred usage (with Model Registry):
        config = registry.get_or_raise("deepseek-chat")
        client = LLMClient.from_model_config(config)

    Legacy usage (backward compatible):
        client = LLMClient(api_key=..., provider=..., api_base=..., model=...)
    """

    def __init__(
        self,
        api_key: str,
        provider: LLMProvider = LLMProvider.ANTHROPIC,
        api_base: str = "https://api.minimaxi.com",
        model: str = "MiniMax-M2",
        retry_config: RetryConfig | None = None,
        *,
        # === 新增：ModelConfig 驅動的參數（向後兼容：全部有默認值） ===
        max_tokens: int = 16384,
        reasoning_format: str = "none",
        enable_reasoning_split: bool | None = None,
        enable_thinking: bool = False,
        _api_base_is_full: bool = False,
    ):
        """Initialize LLM client with specified provider.

        Args:
            api_key: API key for authentication
            provider: LLM provider (anthropic or openai)
            api_base: Base URL for the API.
                     If _api_base_is_full=True, used as-is (from ModelConfig).
                     If _api_base_is_full=False, auto-suffixed for backward compat.
            model: Model name to use
            retry_config: Optional retry configuration
            max_tokens: Maximum output tokens
            reasoning_format: "none"|"reasoning_content"|"reasoning_details"|"anthropic_thinking"
            enable_reasoning_split: Send extra_body.reasoning_split (None=auto-detect for legacy)
            enable_thinking: Send extra_body.enable_thinking
            _api_base_is_full: Internal flag — True when called from from_model_config()
        """
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.retry_config = retry_config or RetryConfig()

        # Determine full API base URL
        if _api_base_is_full:
            # from_model_config() 路径：api_base 已經是完整 URL
            full_api_base = api_base
        else:
            # 向後兼容路径：自動拼接後綴（保持原有 CLI 行為）
            api_base = api_base.replace("/anthropic", "")
            if provider == LLMProvider.ANTHROPIC:
                full_api_base = f"{api_base.rstrip('/')}/anthropic"
            elif provider == LLMProvider.OPENAI and any(k in model.lower() for k in ('glm', 'qwen', 'deepseek')):
                full_api_base = f"{api_base.rstrip('/')}"
            elif provider == LLMProvider.OPENAI:
                full_api_base = f"{api_base.rstrip('/')}/v1"
            else:
                raise ValueError(f"Unsupported provider: {provider}")

        self.api_base = full_api_base

        # === 構建底層 Client ===
        self._client: LLMClientBase
        if provider == LLMProvider.ANTHROPIC:
            self._client = AnthropicClient(
                api_key=api_key,
                api_base=full_api_base,
                model=model,
                retry_config=retry_config,
                max_tokens=max_tokens,
            )
        elif provider == LLMProvider.OPENAI:
            # 向後兼容：如果未顯式傳 enable_reasoning_split，自動偵測
            if enable_reasoning_split is None:
                enable_reasoning_split = any(
                    model.lower().startswith(p)
                    for p in ("minimax", "glm", "qwen", "deepseek")
                )

            self._client = OpenAIClient(
                api_key=api_key,
                api_base=full_api_base,
                model=model,
                retry_config=retry_config,
                enable_reasoning_split=enable_reasoning_split,
                max_tokens=max_tokens,
                reasoning_format=reasoning_format,
                enable_thinking=enable_thinking,
            )
            logger.info(
                "OpenAI client: reasoning_split=%s, reasoning_format=%s, "
                "enable_thinking=%s, max_tokens=%d (model: %s)",
                enable_reasoning_split, reasoning_format, enable_thinking, max_tokens, model,
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        logger.info("Initialized LLM client: provider=%s, api_base=%s", provider, full_api_base)

    @classmethod
    def from_model_config(
        cls,
        config: "ModelConfig",
        retry_config: RetryConfig | None = None,
    ) -> "LLMClient":
        """從 ModelConfig 創建 LLMClient（推薦方式）

        所有模型特定行為由 ModelConfig 驅動，不再有任何 model.startswith() 分支。

        Args:
            config: 模型配置（來自 ModelRegistry）
            retry_config: 可選重試配置

        Returns:
            配置完成的 LLMClient 實例
        """
        provider = (
            LLMProvider.ANTHROPIC
            if config.provider == "anthropic"
            else LLMProvider.OPENAI
        )

        return cls(
            api_key=config.resolve_api_key(),
            provider=provider,
            api_base=config.api_base,
            model=config.model_name,
            retry_config=retry_config,
            max_tokens=config.max_tokens,
            reasoning_format=config.reasoning_format,
            enable_reasoning_split=config.reasoning_split,
            enable_thinking=config.enable_thinking,
            _api_base_is_full=True,
        )

    @property
    def retry_callback(self):
        """Get retry callback."""
        return self._client.retry_callback

    @retry_callback.setter
    def retry_callback(self, value):
        """Set retry callback."""
        self._client.retry_callback = value

    async def generate(
        self,
        messages: list[Message],
        tools: list | None = None,
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Returns:
            LLMResponse containing the generated content
        """
        return await self._client.generate(messages, tools)

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list | None = None,
        on_content=None,
        on_thinking=None,
        on_tool_call=None,
    ) -> LLMResponse:
        """Generate response from LLM with streaming support.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts
            on_content: Optional callback for streaming text content (async callable)
            on_thinking: Optional callback for streaming thinking content (async callable)
            on_tool_call: Optional callback for streaming tool call updates (async callable)

        Returns:
            LLMResponse containing the complete generated content
        """
        return await self._client.generate_stream(messages, tools, on_content, on_thinking, on_tool_call)