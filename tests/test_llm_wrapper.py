"""LLM 客戶端測試"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.agent.llm.llm_wrapper import LLMClient
from src.agent.retry import RetryConfig, RetryExhaustedError
from src.agent.schema import LLMProvider, LLMResponse, Message


class TestLLMClient:
    """LLMClient 測試"""

    def test_anthropic_provider_url(self):
        """測試 Anthropic 提供者 URL 後綴"""
        with patch("src.agent.llm.llm_wrapper.AnthropicClient"):
            client = LLMClient(
                api_key="test-key",
                provider=LLMProvider.ANTHROPIC,
                api_base="https://api.example.com",
                model="claude-3"
            )
            
            assert client.api_base == "https://api.example.com/anthropic"
            assert client.provider == LLMProvider.ANTHROPIC

    def test_openai_provider_url(self):
        """測試 OpenAI 提供者 URL 後綴"""
        with patch("src.agent.llm.llm_wrapper.OpenAIClient"):
            client = LLMClient(
                api_key="test-key",
                provider=LLMProvider.OPENAI,
                api_base="https://api.example.com",
                model="gpt-4"
            )
            
            assert client.api_base == "https://api.example.com/v1"
            assert client.provider == LLMProvider.OPENAI

    def test_glm_model_no_suffix(self):
        """測試 GLM 模型不添加後綴"""
        with patch("src.agent.llm.llm_wrapper.OpenAIClient"):
            client = LLMClient(
                api_key="test-key",
                provider=LLMProvider.OPENAI,
                api_base="https://api.example.com",
                model="glm-4"
            )
            
            # GLM 模型不添加 /v1 後綴
            assert client.api_base == "https://api.example.com"

    def test_qwen_model_no_suffix(self):
        """測試 Qwen 模型不添加後綴"""
        with patch("src.agent.llm.llm_wrapper.OpenAIClient"):
            client = LLMClient(
                api_key="test-key",
                provider=LLMProvider.OPENAI,
                api_base="https://api.example.com",
                model="qwen-max"
            )
            
            assert client.api_base == "https://api.example.com"

    def test_deepseek_model_no_suffix(self):
        """測試 DeepSeek 模型不添加後綴"""
        with patch("src.agent.llm.llm_wrapper.OpenAIClient"):
            client = LLMClient(
                api_key="test-key",
                provider=LLMProvider.OPENAI,
                api_base="https://api.example.com",
                model="deepseek-chat"
            )
            
            assert client.api_base == "https://api.example.com"

    def test_minimax_model_reasoning_split(self):
        """測試 MiniMax 模型啟用 reasoning_split"""
        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockClient:
            client = LLMClient(
                api_key="test-key",
                provider=LLMProvider.OPENAI,
                api_base="https://api.example.com",
                model="MiniMax-M2"
            )
            
            # 確認 OpenAIClient 被調用時 enable_reasoning_split=True
            MockClient.assert_called_once()
            call_kwargs = MockClient.call_args[1]
            assert call_kwargs["enable_reasoning_split"] is True

    def test_gpt_model_no_reasoning_split(self):
        """測試 GPT 模型不啟用 reasoning_split"""
        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockClient:
            client = LLMClient(
                api_key="test-key",
                provider=LLMProvider.OPENAI,
                api_base="https://api.example.com",
                model="gpt-4-turbo"
            )
            
            call_kwargs = MockClient.call_args[1]
            assert call_kwargs["enable_reasoning_split"] is False

    def test_reasoning_effort_reaches_openai_client(self):
        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as mock_client:
            config = _make_model_config("reasoning-model")
            config.reasoning_effort = "high"

            LLMClient.from_model_config(config)

            assert mock_client.call_args.kwargs["reasoning_effort"] == "high"

    def test_thinking_mode_reaches_openai_client(self):
        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as mock_client:
            config = _make_model_config("no-think-model")
            config.thinking_mode = "disabled"

            LLMClient.from_model_config(config)

            assert mock_client.call_args.kwargs["thinking_mode"] == "disabled"

    def test_url_cleanup_anthropic_suffix(self):
        """測試清理 URL 中已存在的 /anthropic 後綴"""
        with patch("src.agent.llm.llm_wrapper.AnthropicClient"):
            # 如果 URL 已經包含 /anthropic，應該只有一個
            client = LLMClient(
                api_key="test-key",
                provider=LLMProvider.ANTHROPIC,
                api_base="https://api.example.com/anthropic",
                model="claude-3"
            )
            
            assert client.api_base == "https://api.example.com/anthropic"
            assert "/anthropic/anthropic" not in client.api_base

    def test_unsupported_provider(self):
        """測試不支持的提供者"""
        with pytest.raises(ValueError, match="Unsupported provider"):
            # 創建一個無效的 provider
            client = LLMClient(
                api_key="test-key",
                provider="invalid_provider",  # type: ignore
                api_base="https://api.example.com",
                model="test-model"
            )


# NOTE: TestLLMProvider 和 TestMessageSchema 测试已统一到 test_schema.py，此处不再重复。


def _make_model_config(model_id: str, provider: str = "openai"):
    """構建一個 mock ModelConfig 用於 failover 測試"""
    cfg = MagicMock()
    cfg.id = model_id
    cfg.display_name = model_id
    cfg.provider = provider
    cfg.api_base = "https://api.example.com/v1"
    cfg.api_key = "test-key"
    cfg.model_name = model_id
    cfg.max_tokens = 4096
    cfg.reasoning_format = "none"
    cfg.reasoning_split = False
    cfg.enable_thinking = False
    cfg.thinking_mode = "provider_default"
    cfg.thinking_wire_format = "enable_thinking"
    cfg.reasoning_effort = None
    cfg.resolve_api_key.return_value = "test-key"
    return cfg


class TestLLMClientFailover:
    """LLMClient failover 測試"""

    @pytest.mark.asyncio
    async def test_primary_success_no_failover(self):
        """主模型成功時不觸發 failover"""
        expected = LLMResponse(content="hello", thinking=None, tool_calls=[], finish_reason="stop")
        expected_snapshot = {
            "provider": "openai",
            "model": "model-a",
            "messages": [{"role": "user", "content": "hi"}],
        }

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            mock_client = AsyncMock()
            mock_client.generate_stream = AsyncMock(return_value=expected)
            mock_client.retry_callback = None
            mock_client.last_request_snapshot = expected_snapshot
            MockOAI.return_value = mock_client

            primary = _make_model_config("model-a")
            fb = _make_model_config("model-b")

            client = LLMClient.from_model_config(primary, fallback_configs=[fb])
            result = await client.generate_stream(messages=[])

            assert result.content == "hello"
            assert mock_client.generate_stream.call_count == 1
            assert client.last_request_snapshot == expected_snapshot

    @pytest.mark.asyncio
    async def test_failover_to_second_model(self):
        """主模型失敗後切換到 fallback 模型"""
        primary_err = RetryExhaustedError(TimeoutError("stream stalled"), 2)
        expected = LLMResponse(content="from fallback", thinking=None, tool_calls=[], finish_reason="stop")
        primary_snapshot = {
            "provider": "openai",
            "model": "model-a",
            "messages": [{"role": "user", "content": "primary"}],
        }
        fallback_snapshot = {
            "provider": "openai",
            "model": "model-b",
            "messages": [{"role": "user", "content": "fallback"}],
        }

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            primary_client = AsyncMock()
            primary_client.generate_stream = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None
            primary_client.last_request_snapshot = primary_snapshot

            fb_client = AsyncMock()
            fb_client.generate_stream = AsyncMock(return_value=expected)
            fb_client.retry_callback = None
            fb_client.last_request_snapshot = fallback_snapshot

            MockOAI.side_effect = [primary_client, fb_client]

            primary = _make_model_config("model-a")
            fb = _make_model_config("model-b")

            client = LLMClient.from_model_config(primary, fallback_configs=[fb])
            result = await client.generate_stream(messages=[])

            assert result.content == "from fallback"
            # failover 是 one-shot 的，不會永久切換 client
            assert client.model == "model-a"
            assert client.last_request_snapshot == fallback_snapshot

    @pytest.mark.asyncio
    async def test_failover_preparation_can_rebuild_request_kwargs(self):
        primary_err = RetryExhaustedError(TimeoutError("stream stalled"), 2)
        expected = LLMResponse(content="fallback", finish_reason="stop")
        rewritten = [Message(role="user", content="compacted replacement")]

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            primary_client = AsyncMock()
            primary_client.generate_stream = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None

            fallback_client = AsyncMock()
            fallback_client.generate_stream = AsyncMock(return_value=expected)
            fallback_client.retry_callback = None
            MockOAI.side_effect = [primary_client, fallback_client]

            client = LLMClient.from_model_config(
                _make_model_config("model-a"),
                fallback_configs=[_make_model_config("model-b")],
            )

            async def prepare(config, target_client, call_method, kwargs):
                assert config.id == "model-b"
                assert target_client is fallback_client
                assert call_method == "generate_stream"
                return {**kwargs, "messages": rewritten}

            client.failover_notify = prepare
            result = await client.generate_stream(
                messages=[Message(role="user", content="oversized")]
            )

            assert result is expected
            assert fallback_client.generate_stream.await_args.kwargs["messages"] == rewritten

    @pytest.mark.asyncio
    async def test_all_models_fail(self):
        """所有模型都失敗時拋出 RetryExhaustedError"""
        err = RetryExhaustedError(TimeoutError("stalled"), 2)

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            failing_client = AsyncMock()
            failing_client.generate_stream = AsyncMock(side_effect=err)
            failing_client.retry_callback = None
            MockOAI.return_value = failing_client

            primary = _make_model_config("model-a")
            fb1 = _make_model_config("model-b")
            fb2 = _make_model_config("model-c")

            client = LLMClient.from_model_config(primary, fallback_configs=[fb1, fb2])

            with pytest.raises(RetryExhaustedError):
                await client.generate_stream(messages=[])

    @pytest.mark.asyncio
    async def test_no_fallback_raises_immediately(self):
        """無 fallback 配置時，主模型失敗直接拋出"""
        err = RetryExhaustedError(TimeoutError("stalled"), 2)

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            mock_client = AsyncMock()
            mock_client.generate_stream = AsyncMock(side_effect=err)
            mock_client.retry_callback = None
            MockOAI.return_value = mock_client

            primary = _make_model_config("model-a")
            client = LLMClient.from_model_config(primary)

            with pytest.raises(RetryExhaustedError):
                await client.generate_stream(messages=[])

    @pytest.mark.asyncio
    async def test_failover_skips_failing_uses_third(self):
        """第二個 fallback 也失敗，成功使用第三個"""
        primary_err = RetryExhaustedError(TimeoutError("stalled"), 2)
        expected = LLMResponse(content="third model", thinking=None, tool_calls=[], finish_reason="stop")

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            primary_client = AsyncMock()
            primary_client.generate_stream = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None

            fb1_client = AsyncMock()
            fb1_client.generate_stream = AsyncMock(side_effect=primary_err)
            fb1_client.retry_callback = None

            fb2_client = AsyncMock()
            fb2_client.generate_stream = AsyncMock(return_value=expected)
            fb2_client.retry_callback = None

            MockOAI.side_effect = [primary_client, fb1_client, fb2_client]

            primary = _make_model_config("model-a")
            fb1 = _make_model_config("model-b")
            fb2 = _make_model_config("model-c")

            client = LLMClient.from_model_config(primary, fallback_configs=[fb1, fb2])
            result = await client.generate_stream(messages=[])

            assert result.content == "third model"
            # failover 是 one-shot 的，不會永久切換 client
            assert client.model == "model-a"

    @pytest.mark.asyncio
    async def test_failover_generate_non_stream(self):
        """generate() 方法同樣支持 failover"""
        primary_err = RetryExhaustedError(TimeoutError("stalled"), 2)
        expected = LLMResponse(content="fallback ok", thinking=None, tool_calls=[], finish_reason="stop")

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            primary_client = AsyncMock()
            primary_client.generate = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None

            fb_client = AsyncMock()
            fb_client.generate = AsyncMock(return_value=expected)
            fb_client.retry_callback = None

            MockOAI.side_effect = [primary_client, fb_client]

            primary = _make_model_config("model-a")
            fb = _make_model_config("model-b")

            client = LLMClient.from_model_config(primary, fallback_configs=[fb])
            result = await client.generate(messages=[])

            assert result.content == "fallback ok"

    @pytest.mark.asyncio
    async def test_failover_keeps_run_reasoning_snapshot_untouched(self):
        """备用网关负责兼容未声明的等级，wrapper 不按白名单过滤或降级。"""
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        primary_err = RetryExhaustedError(TimeoutError("stalled"), 2)
        expected = LLMResponse(content="fallback ok", finish_reason="stop")
        snapshot = ResolvedReasoningContext(mode="enabled", effort="max")

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            primary_client = AsyncMock()
            primary_client.generate = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None

            fb_client = AsyncMock()
            fb_client.generate = AsyncMock(return_value=expected)
            fb_client.retry_callback = None

            MockOAI.side_effect = [primary_client, fb_client]

            primary = _make_model_config("model-a")
            fb = _make_model_config("model-b")
            fb.reasoning_effort = "low"
            fb.supported_reasoning_efforts = ["off", "low"]

            client = LLMClient.from_model_config(primary, fallback_configs=[fb])
            token = current_run_context.set(AgentRunContext(reasoning=snapshot))
            try:
                result = await client.generate(messages=[])
                assert current_run_context.get().reasoning == snapshot
            finally:
                current_run_context.reset(token)

            assert result.content == "fallback ok"
            # fallback client 只用自身目录配置构建，运行期覆盖仍由 ContextVar 提供
            assert MockOAI.call_args_list[-1].kwargs["reasoning_effort"] == "low"
            assert fb_client.generate.await_count == 1

    @pytest.mark.asyncio
    async def test_failover_skips_anthropic_for_explicit_reasoning_selection(self):
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        primary_err = RetryExhaustedError(TimeoutError("stalled"), 2)
        anthropic_result = LLMResponse(content="anthropic", finish_reason="stop")
        compatible_result = LLMResponse(content="compatible", finish_reason="stop")

        with (
            patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI,
            patch("src.agent.llm.llm_wrapper.AnthropicClient") as MockAnthropic,
        ):
            primary_client = AsyncMock()
            primary_client.generate = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None
            compatible_client = AsyncMock()
            compatible_client.generate = AsyncMock(return_value=compatible_result)
            compatible_client.retry_callback = None
            MockOAI.side_effect = [primary_client, compatible_client]

            anthropic_client = AsyncMock()
            anthropic_client.generate = AsyncMock(return_value=anthropic_result)
            anthropic_client.retry_callback = None
            MockAnthropic.return_value = anthropic_client

            client = LLMClient.from_model_config(
                _make_model_config("primary"),
                fallback_configs=[
                    _make_model_config("anthropic", provider="anthropic"),
                    _make_model_config("compatible"),
                ],
            )
            token = current_run_context.set(AgentRunContext(
                reasoning=ResolvedReasoningContext(mode="disabled", effort=None)
            ))
            try:
                result = await client.generate(messages=[])
            finally:
                current_run_context.reset(token)

            assert result is compatible_result
            MockAnthropic.assert_not_called()

    @pytest.mark.asyncio
    async def test_failover_skips_none_wire_for_pure_thinking_switch(self):
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        primary_err = RetryExhaustedError(TimeoutError("stalled"), 2)
        expected = LLMResponse(content="compatible", finish_reason="stop")

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            primary_client = AsyncMock()
            primary_client.generate = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None
            compatible_client = AsyncMock()
            compatible_client.generate = AsyncMock(return_value=expected)
            compatible_client.retry_callback = None
            MockOAI.side_effect = [primary_client, compatible_client]

            no_switch = _make_model_config("no-switch")
            no_switch.thinking_wire_format = "none"
            compatible = _make_model_config("switch-capable")
            client = LLMClient.from_model_config(
                _make_model_config("primary"),
                fallback_configs=[no_switch, compatible],
            )
            token = current_run_context.set(AgentRunContext(
                reasoning=ResolvedReasoningContext(mode="enabled", effort=None)
            ))
            try:
                result = await client.generate(messages=[])
            finally:
                current_run_context.reset(token)

            assert result is expected
            assert MockOAI.call_args_list[-1].kwargs["model"] == "switch-capable"

    @pytest.mark.asyncio
    async def test_failover_allows_none_wire_for_specific_effort(self):
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        primary_err = RetryExhaustedError(TimeoutError("stalled"), 2)
        expected = LLMResponse(content="fallback", finish_reason="stop")

        with patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI:
            primary_client = AsyncMock()
            primary_client.generate = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None
            fallback_client = AsyncMock()
            fallback_client.generate = AsyncMock(return_value=expected)
            fallback_client.retry_callback = None
            MockOAI.side_effect = [primary_client, fallback_client]

            fallback = _make_model_config("effort-only")
            fallback.thinking_wire_format = "none"
            client = LLMClient.from_model_config(
                _make_model_config("primary"),
                fallback_configs=[fallback],
            )
            token = current_run_context.set(AgentRunContext(
                reasoning=ResolvedReasoningContext(mode="enabled", effort="high")
            ))
            try:
                result = await client.generate(messages=[])
            finally:
                current_run_context.reset(token)

            assert result is expected
            assert MockOAI.call_args_list[-1].kwargs["model"] == "effort-only"

    @pytest.mark.asyncio
    async def test_failover_allows_anthropic_for_provider_default(self):
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        primary_err = RetryExhaustedError(TimeoutError("stalled"), 2)
        expected = LLMResponse(content="anthropic", finish_reason="stop")

        with (
            patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI,
            patch("src.agent.llm.llm_wrapper.AnthropicClient") as MockAnthropic,
        ):
            primary_client = AsyncMock()
            primary_client.generate = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None
            MockOAI.return_value = primary_client
            fallback_client = AsyncMock()
            fallback_client.generate = AsyncMock(return_value=expected)
            fallback_client.retry_callback = None
            MockAnthropic.return_value = fallback_client

            client = LLMClient.from_model_config(
                _make_model_config("primary"),
                fallback_configs=[_make_model_config("anthropic", provider="anthropic")],
            )
            token = current_run_context.set(AgentRunContext(
                reasoning=ResolvedReasoningContext(
                    mode="provider_default",
                    effort=None,
                )
            ))
            try:
                result = await client.generate(messages=[])
            finally:
                current_run_context.reset(token)

            assert result is expected
            MockAnthropic.assert_called_once()

    @pytest.mark.asyncio
    async def test_failover_raises_primary_error_when_all_fallbacks_are_incompatible(self):
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        primary_err = RetryExhaustedError(TimeoutError("stalled"), 2)

        with (
            patch("src.agent.llm.llm_wrapper.OpenAIClient") as MockOAI,
            patch("src.agent.llm.llm_wrapper.AnthropicClient") as MockAnthropic,
        ):
            primary_client = AsyncMock()
            primary_client.generate = AsyncMock(side_effect=primary_err)
            primary_client.retry_callback = None
            MockOAI.return_value = primary_client

            no_switch = _make_model_config("no-switch")
            no_switch.thinking_wire_format = "none"
            client = LLMClient.from_model_config(
                _make_model_config("primary"),
                fallback_configs=[
                    _make_model_config("anthropic", provider="anthropic"),
                    no_switch,
                ],
            )
            token = current_run_context.set(AgentRunContext(
                reasoning=ResolvedReasoningContext(mode="disabled", effort=None)
            ))
            try:
                with pytest.raises(RetryExhaustedError) as exc_info:
                    await client.generate(messages=[])
            finally:
                current_run_context.reset(token)

            assert exc_info.value is primary_err
            MockAnthropic.assert_not_called()
            assert MockOAI.call_count == 1
