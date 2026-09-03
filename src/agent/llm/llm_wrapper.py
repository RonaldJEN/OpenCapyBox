"""LLM client wrapper that supports multiple providers.

This module provides a unified interface for different LLM providers
(Anthropic and OpenAI) through a single LLMClient class.

Supports two initialization modes:
  1. from_model_config(config) — 推薦：從 ModelConfig 創建，消除所有硬編碼
  2. __init__(api_key, provider, ...) — 向後兼容：原有 CLI 等場景
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..retry import RetryConfig, RetryExhaustedError
from ..schema import LLMProvider, LLMResponse, Message
from ..schema.run_context import current_run_context
from .anthropic_client import AnthropicClient
from .base import LLMClientBase
from .openai_client import OpenAIClient
from .openai_responses_client import OpenAIResponsesClient

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
        enable_reasoning_split: bool = False,
        enable_thinking: bool = False,
        thinking_mode: str = "provider_default",
        thinking_wire_format: str = "enable_thinking",
        reasoning_effort: str | None = None,
        openai_protocol: str = "chat_completions",
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
            enable_reasoning_split: Send Chat Completions extra_body.reasoning_split
            enable_thinking: Send extra_body.enable_thinking
            thinking_mode: provider_default/enabled/disabled
            thinking_wire_format: Request encoding for the thinking switch
            reasoning_effort: Send the OpenAI-compatible reasoning_effort value
            openai_protocol: "responses" or "chat_completions"
            _api_base_is_full: Internal flag — True when called from from_model_config()
        """
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.retry_config = retry_config or RetryConfig()
        self.openai_protocol = openai_protocol if provider == LLMProvider.OPENAI else None

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
            if openai_protocol not in {"responses", "chat_completions"}:
                raise ValueError(f"Unsupported openai_protocol: {openai_protocol}")
            client_class = (
                OpenAIResponsesClient
                if openai_protocol == "responses"
                else OpenAIClient
            )
            client_kwargs = dict(
                api_key=api_key,
                api_base=full_api_base,
                model=model,
                retry_config=retry_config,
                max_tokens=max_tokens,
                reasoning_format=reasoning_format,
                enable_thinking=enable_thinking,
                thinking_mode=thinking_mode,
                thinking_wire_format=thinking_wire_format,
                reasoning_effort=reasoning_effort,
            )
            if openai_protocol == "chat_completions":
                client_kwargs["enable_reasoning_split"] = enable_reasoning_split
            self._client = client_class(**client_kwargs)
            logger.info(
                "OpenAI client: protocol=%s, reasoning_format=%s, "
                "thinking_mode=%s, thinking_wire_format=%s, max_tokens=%d (model: %s)",
                openai_protocol, reasoning_format, thinking_mode,
                thinking_wire_format, max_tokens, model,
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        logger.info("Initialized LLM client: provider=%s, api_base=%s", provider, full_api_base)

        # 最近一次实际发送给 provider 的请求快照
        self._last_request_snapshot: dict[str, Any] | None = None

        # Failover: 備用模型配置列表（僅 from_model_config 路徑使用）
        self._fallback_configs: list[ModelConfig] = []
        # 緩存已構建的 fallback 客戶端，避免每次 failover 都重建
        self._fallback_clients: dict[str, LLMClientBase] = {}
        # Failover preparation callback. It may reset streaming state and
        # return rewritten kwargs after compacting for the target model.
        self._failover_notify = None

    @classmethod
    def from_model_config(
        cls,
        config: "ModelConfig",
        retry_config: RetryConfig | None = None,
        fallback_configs: list["ModelConfig"] | None = None,
    ) -> "LLMClient":
        """從 ModelConfig 創建 LLMClient（推薦方式）

        所有模型特定行為由 ModelConfig 驅動，不再有任何 model.startswith() 分支。

        Args:
            config: 模型配置（來自 ModelRegistry）
            retry_config: 可選重試配置
            fallback_configs: 備用模型配置列表，當主模型重試耗盡後依序嘗試

        Returns:
            配置完成的 LLMClient 實例
        """
        provider = (
            LLMProvider.ANTHROPIC
            if config.provider == "anthropic"
            else LLMProvider.OPENAI
        )

        instance = cls(
            api_key=config.resolve_api_key(),
            provider=provider,
            api_base=config.api_base,
            model=config.model_name,
            retry_config=retry_config,
            max_tokens=config.max_tokens,
            reasoning_format=config.reasoning_format,
            enable_reasoning_split=config.reasoning_split,
            enable_thinking=config.enable_thinking,
            thinking_mode=config.thinking_mode,
            thinking_wire_format=config.thinking_wire_format,
            reasoning_effort=config.reasoning_effort,
            openai_protocol=config.openai_protocol or "chat_completions",
            _api_base_is_full=True,
        )
        instance._fallback_configs = list(fallback_configs or [])
        if instance._fallback_configs:
            logger.info(
                "Failover enabled: %d fallback model(s) — %s",
                len(instance._fallback_configs),
                [c.id for c in instance._fallback_configs],
            )
        return instance

    @property
    def retry_callback(self):
        """Get retry callback."""
        return self._client.retry_callback

    @retry_callback.setter
    def retry_callback(self, value):
        """Set retry callback."""
        self._client.retry_callback = value

    @property
    def last_request_snapshot(self) -> dict[str, Any] | None:
        """Get last provider request snapshot for auditing."""
        return self._last_request_snapshot

    @last_request_snapshot.setter
    def last_request_snapshot(self, value: dict[str, Any] | None):
        """Set last provider request snapshot."""
        self._last_request_snapshot = value

    @property
    def failover_notify(self):
        """Get failover notify callback (async callable or None)."""
        return self._failover_notify

    @failover_notify.setter
    def failover_notify(self, value):
        """Set failover notify callback.

        Preferred signature::

            async def callback(config, client, call_method, kwargs) -> dict | None

        """
        self._failover_notify = value

    # ---- Failover helpers ----

    def _sync_last_request_snapshot(self, client: LLMClientBase) -> None:
        snapshot = getattr(client, "last_request_snapshot", None)
        self._last_request_snapshot = snapshot if isinstance(snapshot, dict) else None

    @staticmethod
    def _fallback_can_encode_run_reasoning(config: "ModelConfig") -> bool:
        run_context = current_run_context.get()
        reasoning = run_context.reasoning if run_context is not None else None
        if reasoning is None or (
            reasoning.mode == "provider_default" and reasoning.effort is None
        ):
            return True
        if config.provider != "openai":
            return False
        if reasoning.effort is not None:
            return True
        return config.thinking_wire_format != "none"

    @classmethod
    def _build_client(cls, config: "ModelConfig", retry_config: RetryConfig) -> LLMClientBase:
        """從 ModelConfig 構建底層 LLMClientBase（複用 from_model_config 保證路徑一致）

        注意: 此處 **不傳** fallback_configs，確保不會遞歸構建 fallback 鏈。
        """
        tmp = cls.from_model_config(config, retry_config=retry_config, fallback_configs=None)
        return tmp._client

    async def _failover_generate(
        self,
        call_method: str,
        **kwargs,
    ) -> LLMResponse:
        """帶 failover 的通用調用包裝

        先調用當前 _client，若拋出 RetryExhaustedError 則依序嘗試 fallback 模型。

        Args:
            call_method: 要調用的方法名（"generate" 或 "generate_stream"）
            **kwargs: 傳給方法的命名參數
        """
        # 1) 嘗試主模型
        last_error: Exception | None = None
        try:
            result = await getattr(self._client, call_method)(**kwargs)
            self._sync_last_request_snapshot(self._client)
            return result
        except RetryExhaustedError as primary_err:
            self._sync_last_request_snapshot(self._client)
            last_error = primary_err
            if not self._fallback_configs:
                raise
            logger.warning(
                "Primary model '%s' exhausted retries: %s — starting failover",
                self.model, primary_err.last_exception,
            )

        # 2) 依序嘗試 fallback 模型
        attempted_fallbacks = 0
        for i, fb_config in enumerate(self._fallback_configs):
            if not self._fallback_can_encode_run_reasoning(fb_config):
                logger.warning(
                    "Failover [%d/%d]: skipping model '%s'; provider=%s "
                    "thinking_wire_format=%s cannot encode the frozen reasoning selection",
                    i + 1,
                    len(self._fallback_configs),
                    fb_config.id,
                    fb_config.provider,
                    fb_config.thinking_wire_format,
                )
                continue
            attempted_fallbacks += 1
            logger.warning(
                "Failover [%d/%d]: switching to model '%s' (%s)",
                i + 1, len(self._fallback_configs),
                fb_config.id, fb_config.model_name,
            )
            try:
                # 使用緩存的 fallback 客戶端，避免重複構建 HTTP 連接
                if fb_config.id not in self._fallback_clients:
                    self._fallback_clients[fb_config.id] = self._build_client(fb_config, self.retry_config)
                fb_client = self._fallback_clients[fb_config.id]
                fb_client.retry_callback = self._client.retry_callback
                fallback_kwargs = dict(kwargs)
                if self._failover_notify:
                    prepared = await self._failover_notify(
                        fb_config,
                        fb_client,
                        call_method,
                        fallback_kwargs,
                    )
                    if isinstance(prepared, dict):
                        fallback_kwargs = prepared
                result = await getattr(fb_client, call_method)(**fallback_kwargs)
                self._sync_last_request_snapshot(fb_client)
                # 僅本次調用使用 fallback，不修改 self._client，
                # 下次調用仍優先嘗試主模型（主模型可能已恢復）。
                logger.info("Failover succeeded (one-shot): model '%s'", fb_config.id)
                return result
            except RetryExhaustedError as fb_err:
                self._sync_last_request_snapshot(fb_client)
                logger.warning(
                    "Failover model '%s' also failed: %s",
                    fb_config.id, fb_err,
                )
                last_error = fb_err

        # 3) 所有兼容模型均失敗。若沒有任何 fallback 能編碼本輪快照，
        # 保留主模型的原始錯誤，不把「協議不兼容而跳過」包裝成一次模型失敗。
        if attempted_fallbacks == 0 and isinstance(last_error, RetryExhaustedError):
            raise last_error
        total_models = 1 + attempted_fallbacks
        raise RetryExhaustedError(
            last_error if isinstance(last_error, Exception) else Exception(str(last_error)),
            total_models,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list | None = None,
    ) -> LLMResponse:
        """Generate response from LLM (with failover support).

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Returns:
            LLMResponse containing the generated content
        """
        return await self._failover_generate("generate", messages=messages, tools=tools)

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list | None = None,
        on_content=None,
        on_thinking=None,
        on_tool_call=None,
    ) -> LLMResponse:
        """Generate response from LLM with streaming support (with failover).

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts
            on_content: Optional callback for streaming text content (async callable)
            on_thinking: Optional callback for streaming thinking content (async callable)
            on_tool_call: Optional callback for streaming tool call updates (async callable)

        Returns:
            LLMResponse containing the complete generated content
        """
        return await self._failover_generate(
            "generate_stream",
            messages=messages, tools=tools,
            on_content=on_content, on_thinking=on_thinking, on_tool_call=on_tool_call,
        )
