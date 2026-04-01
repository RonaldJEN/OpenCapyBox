"""LLM 客戶端測試"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.agent.llm.llm_wrapper import LLMClient
from src.agent.schema import LLMProvider, Message


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
