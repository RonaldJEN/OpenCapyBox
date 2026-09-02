"""OpenAI Client 擴展測試

提高 openai_client.py 的覆蓋率
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from src.agent.schema import Message, ToolCall, FunctionCall, LLMResponse
from src.agent.retry import RetryConfig
from tests.helpers import FakeAsyncStream


# ── 模块级 fixtures（消除 5 处重复）─────────────────────────


def _make_openai_client(**kwargs):
    """OpenAI 客户端工厂，减少 import + 构造样板"""
    from src.agent.llm.openai_client import OpenAIClient
    defaults = {"api_key": "test-key", "model": "TestModel"}
    defaults.update(kwargs)
    return OpenAIClient(**defaults)


@pytest.fixture
def openai_client():
    """基础 OpenAI 客户端"""
    return _make_openai_client()


@pytest.fixture
def openai_client_with_reasoning():
    """启用 reasoning_split 的 OpenAI 客户端"""
    return _make_openai_client(enable_reasoning_split=True)


class TestOpenAIClientInitialization:
    """測試客戶端初始化"""

    def test_default_initialization(self):
        """測試默認初始化"""
        from src.agent.llm.openai_client import OpenAIClient

        with patch('src.agent.llm.openai_client.AsyncOpenAI') as mock_openai:
            client = OpenAIClient(
                api_key="test-key",
                api_base="https://api.example.com/v1",
                model="TestModel",
            )

        kwargs = mock_openai.call_args.kwargs
        assert kwargs["max_retries"] == 0
        assert kwargs["timeout"].connect == 10.0
        assert kwargs["timeout"].read == 120.0
        assert kwargs["timeout"].write == 60.0
        assert kwargs["timeout"].pool == 10.0
        
        assert client.api_key == "test-key"
        assert client.model == "TestModel"
        assert client.enable_reasoning_split is True

    def test_glm_model_reasoning_format(self):
        """測試 GLM 模型的推理格式（須顯式傳入 reasoning_format）"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="glm-4-plus",
            reasoning_format="reasoning_content",
        )
        
        assert client.reasoning_format == "reasoning_content"

    def test_qwen_model_reasoning_format(self):
        """測試 Qwen 模型的推理格式（須顯式傳入 reasoning_format）"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="qwen-plus",
            reasoning_format="reasoning_content",
        )
        
        assert client.reasoning_format == "reasoning_content"

    def test_deepseek_model_reasoning_format(self):
        """測試 DeepSeek 模型的推理格式（須顯式傳入 reasoning_format）"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="deepseek-v3",
            reasoning_format="reasoning_content",
        )
        
        assert client.reasoning_format == "reasoning_content"

    def test_minimax_model_reasoning_format(self):
        """測試 MiniMax 模型的推理格式（須顯式傳入 reasoning_format）"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="MiniMax-M2",
            reasoning_format="minimax",
        )
        
        assert client.reasoning_format == "minimax"

    def test_deepseek_chat_max_tokens(self):
        """測試 DeepSeek-Chat 模型的最大 token 數（須顯式傳入 max_tokens）"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="deepseek-chat",
            max_tokens=8192,
        )
        
        assert client.max_tokens == 8192

    def test_qwen_plus_max_tokens(self):
        """測試 Qwen-Plus 模型的最大 token 數（須顯式傳入 max_tokens）"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="qwen-plus-latest",
            max_tokens=32768,
        )
        
        assert client.max_tokens == 32768

    def test_disable_reasoning_split(self):
        """測試禁用 reasoning_split"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="TestModel",
            enable_reasoning_split=False,
        )
        
        assert client.enable_reasoning_split is False

    def test_with_retry_config(self):
        """測試帶重試配置"""
        from src.agent.llm.openai_client import OpenAIClient
        
        retry_config = RetryConfig(max_retries=5)
        client = OpenAIClient(
            api_key="test-key",
            model="TestModel",
            retry_config=retry_config,
        )
        
        assert client.retry_config.max_retries == 5

    def test_reasoning_params_keep_thinking_independent_from_split(self):
        client = _make_openai_client(
            enable_reasoning_split=False,
            enable_thinking=True,
            reasoning_effort="high",
        )

        assert client._reasoning_request_params() == {
            "extra_body": {"enable_thinking": True},
            "reasoning_effort": "high",
        }

    def test_reasoning_params_can_explicitly_disable_thinking(self):
        client = _make_openai_client(
            enable_reasoning_split=False,
            thinking_mode="disabled",
        )

        assert client._reasoning_request_params() == {
            "extra_body": {"enable_thinking": False},
        }

    def test_provider_default_omits_thinking_switch(self):
        client = _make_openai_client(
            enable_reasoning_split=False,
            enable_thinking=False,
            thinking_mode="provider_default",
        )

        assert client._reasoning_request_params() == {}

    def test_deepseek_thinking_object_matches_harness_wire_contract(self):
        client = _make_openai_client(
            enable_reasoning_split=False,
            thinking_mode="enabled",
            thinking_wire_format="thinking_object",
            reasoning_effort="max",
        )

        assert client._reasoning_request_params() == {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "max",
        }

    def test_deepseek_off_uses_disabled_thinking_object_without_effort(self):
        client = _make_openai_client(
            enable_reasoning_split=False,
            thinking_mode="enabled",
            thinking_wire_format="thinking_object",
            reasoning_effort="max",
        )
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        token = current_run_context.set(AgentRunContext(
            reasoning=ResolvedReasoningContext(mode="disabled", effort=None)
        ))
        try:
            assert client._reasoning_request_params() == {
                "extra_body": {"thinking": {"type": "disabled"}},
            }
        finally:
            current_run_context.reset(token)

    def test_none_wire_format_only_sends_effort(self):
        client = _make_openai_client(
            enable_reasoning_split=False,
            thinking_mode="enabled",
            thinking_wire_format="none",
            reasoning_effort="high",
        )

        assert client._reasoning_request_params() == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("reserved", ["off", "on"])
    def test_request_boundary_rejects_switch_alias_as_reasoning_effort(self, reserved):
        client = _make_openai_client(reasoning_effort=reserved)

        with pytest.raises(ValueError, match="reasoning_effort cannot be off/on"):
            client._reasoning_request_params()

    def test_turn_reasoning_context_overrides_catalog_for_entire_run(self):
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        client = _make_openai_client(
            enable_reasoning_split=True,
            thinking_mode="enabled",
            reasoning_effort="high",
        )
        token = current_run_context.set(AgentRunContext(
            reasoning=ResolvedReasoningContext(mode="enabled", effort="max")
        ))
        try:
            assert client._reasoning_request_params() == {
                "extra_body": {"reasoning_split": True, "enable_thinking": True},
                "reasoning_effort": "max",
            }
        finally:
            current_run_context.reset(token)

    def test_turn_off_removes_catalog_effort_and_sends_false(self):
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        client = _make_openai_client(thinking_mode="enabled", reasoning_effort="high")
        token = current_run_context.set(AgentRunContext(
            reasoning=ResolvedReasoningContext(mode="disabled", effort=None)
        ))
        try:
            assert client._reasoning_request_params() == {
                "extra_body": {"reasoning_split": True, "enable_thinking": False},
            }
        finally:
            current_run_context.reset(token)

    def test_fallback_client_passes_run_snapshot_through_unchanged(self):
        """推理等级是网关级透传参数：备用模型不过滤、不降级主模型的冻结快照。"""
        from src.agent.schema.run_context import (
            AgentRunContext,
            ResolvedReasoningContext,
            current_run_context,
        )

        fallback_client = _make_openai_client(
            enable_reasoning_split=False,
            thinking_mode="provider_default",
            thinking_wire_format="thinking_object",
            reasoning_effort="low",
        )
        token = current_run_context.set(AgentRunContext(
            reasoning=ResolvedReasoningContext(mode="enabled", effort="max")
        ))
        try:
            assert fallback_client._reasoning_request_params() == {
                "extra_body": {"thinking": {"type": "enabled"}},
                "reasoning_effort": "max",
            }
        finally:
            current_run_context.reset(token)


class TestConvertTools:
    """測試工具轉換"""

    def test_convert_dict_openai_format(self, openai_client):
        """測試轉換 OpenAI 格式的字典"""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object"},
                },
            }
        ]
        
        result = openai_client._convert_tools(tools)

        assert len(result) == 1
        assert result[0]["type"] == "function"

    def test_convert_dict_anthropic_format(self, openai_client):
        """測試轉換 Anthropic 格式的字典"""
        tools = [
            {
                "name": "test_tool",
                "description": "A test tool",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]

        result = openai_client._convert_tools(tools)

        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "test_tool"

    def test_convert_tool_object_with_schema(self, openai_client):
        """測試轉換帶 to_openai_schema 方法的工具對象"""
        mock_tool = MagicMock()
        mock_tool.to_openai_schema.return_value = {
            "type": "function",
            "function": {"name": "mock_tool"},
        }

        result = openai_client._convert_tools([mock_tool])

        assert len(result) == 1
        assert result[0]["function"]["name"] == "mock_tool"

    def test_convert_unsupported_type_raises_error(self, openai_client):
        """測試不支持的類型拋出錯誤"""
        with pytest.raises(TypeError):
            openai_client._convert_tools(["invalid_tool"])


class TestConvertMessages:
    """測試消息轉換"""

    @pytest.fixture
    def glm_client(self):
        """創建 GLM 客戶端（顯式傳入 reasoning_format）"""
        return _make_openai_client(model="glm-4-plus", reasoning_format="reasoning_content")

    @pytest.fixture
    def minimax_client(self):
        """創建 MiniMax 客戶端（顯式傳入 reasoning_format）"""
        return _make_openai_client(model="MiniMax-M2", reasoning_format="reasoning_details")

    def test_convert_system_message(self, openai_client):
        """測試轉換系統消息"""
        messages = [Message(role="system", content="You are helpful")]

        _, api_messages = openai_client._convert_messages(messages)

        assert len(api_messages) == 1
        assert api_messages[0]["role"] == "system"
        assert api_messages[0]["content"] == "You are helpful"

    def test_convert_user_message(self, openai_client):
        """測試轉換用戶消息"""
        messages = [Message(role="user", content="Hello")]

        _, api_messages = openai_client._convert_messages(messages)

        assert len(api_messages) == 1
        assert api_messages[0]["role"] == "user"

    def test_convert_assistant_message(self, openai_client):
        """測試轉換助手消息"""
        messages = [Message(role="assistant", content="Hi there")]

        _, api_messages = openai_client._convert_messages(messages)

        assert len(api_messages) == 1
        assert api_messages[0]["role"] == "assistant"
        assert api_messages[0]["content"] == "Hi there"

    def test_convert_assistant_with_tool_calls(self, openai_client):
        """測試轉換帶工具調用的助手消息"""
        tool_call = ToolCall(
            id="call-123",
            type="function",
            function=FunctionCall(name="test_tool", arguments={"arg": "value"}),
        )
        messages = [
            Message(role="assistant", content="Let me help", tool_calls=[tool_call])
        ]

        _, api_messages = openai_client._convert_messages(messages)
        
        assert "tool_calls" in api_messages[0]
        assert api_messages[0]["tool_calls"][0]["id"] == "call-123"

    def test_convert_assistant_with_thinking_glm(self, glm_client):
        """測試 GLM 格式的推理內容"""
        messages = [
            Message(role="assistant", content="Result", thinking="Let me think...")
        ]
        
        _, api_messages = glm_client._convert_messages(messages)
        
        assert "reasoning_content" in api_messages[0]
        assert api_messages[0]["reasoning_content"] == "Let me think..."

    def test_convert_assistant_with_thinking_minimax(self, minimax_client):
        """測試 MiniMax 格式的推理內容"""
        messages = [
            Message(role="assistant", content="Result", thinking="Let me think...")
        ]
        
        _, api_messages = minimax_client._convert_messages(messages)
        
        assert "reasoning_details" in api_messages[0]
        assert api_messages[0]["reasoning_details"][0]["text"] == "Let me think..."

    def test_no_reasoning_key_when_format_none(self, openai_client):
        """測試 reasoning_format='none' 時不注入推理欄位"""
        messages = [
            Message(role="assistant", content="Result", thinking="Some thought")
        ]
        _, api_messages = openai_client._convert_messages(messages)
        assert "reasoning_content" not in api_messages[0]
        assert "reasoning_details" not in api_messages[0]

    def test_convert_tool_message(self, openai_client):
        """測試轉換工具結果消息"""
        messages = [
            Message(role="tool", content="Tool output", tool_call_id="call-123")
        ]

        _, api_messages = openai_client._convert_messages(messages)

        assert api_messages[0]["role"] == "tool"
        assert api_messages[0]["tool_call_id"] == "call-123"

    def test_convert_multiple_messages(self, openai_client):
        """測試轉換多條消息"""
        messages = [
            Message(role="system", content="System prompt"),
            Message(role="user", content="User question"),
            Message(role="assistant", content="Assistant response"),
        ]

        _, api_messages = openai_client._convert_messages(messages)

        assert len(api_messages) == 3


class TestPrepareRequest:
    """測試請求準備"""

    def test_prepare_basic_request(self, openai_client):
        """測試準備基本請求"""
        messages = [Message(role="user", content="Hello")]

        result = openai_client._prepare_request(messages)

        assert "api_messages" in result
        assert len(result["api_messages"]) == 1

    def test_prepare_request_with_tools(self, openai_client):
        """測試準備帶工具的請求"""
        messages = [Message(role="user", content="Hello")]
        tools = [{"type": "function", "function": {"name": "tool1"}}]

        result = openai_client._prepare_request(messages, tools=tools)

        assert result["tools"] == tools


class TestAPIRequest:
    """測試 API 請求"""

    @pytest.mark.asyncio
    async def test_api_request_none_response(self, openai_client_with_reasoning):
        """測試 API 返回 None"""
        openai_client_with_reasoning.client.chat.completions.create = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="None response"):
            await openai_client_with_reasoning._make_api_request([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_api_request_no_choices(self, openai_client_with_reasoning):
        """測試 API 響應無 choices"""
        mock_response = MagicMock()
        mock_response.choices = None
        openai_client_with_reasoning.client.chat.completions.create = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError, match="missing choices"):
            await openai_client_with_reasoning._make_api_request([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_api_request_empty_choices(self, openai_client_with_reasoning):
        """測試 API 響應 choices 為空"""
        mock_response = MagicMock()
        mock_response.choices = []
        openai_client_with_reasoning.client.chat.completions.create = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError, match="empty choices"):
            await openai_client_with_reasoning._make_api_request([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_api_request_success(self, openai_client_with_reasoning):
        """測試成功的 API 請求"""
        mock_message = MagicMock()
        mock_message.content = "Response"
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=mock_message)]
        openai_client_with_reasoning.client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await openai_client_with_reasoning._make_api_request([{"role": "user", "content": "test"}])

        assert result == mock_response

    @pytest.mark.asyncio
    async def test_api_request_with_tools(self, openai_client_with_reasoning):
        """測試帶工具的 API 請求"""
        mock_message = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=mock_message)]
        openai_client_with_reasoning.client.chat.completions.create = AsyncMock(return_value=mock_response)

        tools = [{"type": "function", "function": {"name": "tool1"}}]
        await openai_client_with_reasoning._make_api_request(
            [{"role": "user", "content": "test"}],
            tools=tools,
        )

        # 驗證調用參數包含 tools
        call_kwargs = openai_client_with_reasoning.client.chat.completions.create.call_args.kwargs
        assert "tools" in call_kwargs

    @pytest.mark.asyncio
    async def test_api_request_records_last_request_snapshot(self, openai_client_with_reasoning):
        """同步調用應記錄最終 provider 請求快照"""
        mock_message = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=mock_message)]
        openai_client_with_reasoning.client.chat.completions.create = AsyncMock(return_value=mock_response)

        api_messages = [{"role": "user", "content": "hello"}]
        tools = [{"type": "function", "function": {"name": "tool1"}}]
        await openai_client_with_reasoning._make_api_request(api_messages, tools=tools)

        snapshot = openai_client_with_reasoning.last_request_snapshot
        assert snapshot is not None
        assert snapshot["provider"] == "openai"
        assert snapshot["messages"] == api_messages
        assert snapshot["model"] == openai_client_with_reasoning.model
        assert snapshot["max_tokens"] == openai_client_with_reasoning.max_tokens
        assert snapshot["tools"][0]["function"]["name"] == "tool1"


class TestGenerateStream:
    @pytest.mark.asyncio
    async def test_generate_stream_records_last_request_snapshot(self, openai_client):
        """流式調用應記錄最終 provider 請求快照"""
        openai_client._make_stream_request = AsyncMock(
            return_value=LLMResponse(content="ok", finish_reason="stop")
        )
        tools = [{"type": "function", "function": {"name": "tool1"}}]

        await openai_client.generate_stream(
            messages=[Message(role="user", content="hello")],
            tools=tools,
        )

        snapshot = openai_client.last_request_snapshot
        assert snapshot is not None
        assert snapshot["provider"] == "openai"
        assert snapshot["stream"] is True
        assert snapshot["messages"][0]["role"] == "user"
        assert snapshot["messages"][0]["content"] == "hello"
        assert snapshot["tools"][0]["function"]["name"] == "tool1"


class TestEdgeCases:
    """測試邊界情況"""

    def test_deepseek_v3_2_max_tokens(self):
        """測試 DeepSeek-V3.2 模型的最大 token 數（須顯式傳入 max_tokens）"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="deepseek-v3.2-latest",
            max_tokens=65536,
        )
        
        assert client.max_tokens == 65536

    def test_qwen3_max_preview_max_tokens(self):
        """測試 Qwen3-Max-Preview 模型的最大 token 數（須顯式傳入 max_tokens）"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="qwen3-max-preview",
            max_tokens=32768,
        )
        
        assert client.max_tokens == 32768

    def test_unknown_model_default_tokens(self):
        """測試未知模型使用默認 token 數"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="unknown-model",
        )
        
        assert client.max_tokens == 16384

    def test_unknown_model_no_reasoning_format(self):
        """測試未知模型無推理格式"""
        from src.agent.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            api_key="test-key",
            model="unknown-model",
        )
        
        assert client.reasoning_format == "none"


class TestStreamingToolCallValidation:
    @pytest.mark.asyncio
    async def test_stream_skips_tool_call_with_empty_name(self, openai_client):
        tool_delta = MagicMock()
        tool_delta.index = 0
        tool_delta.id = "call_1"
        tool_delta.function = MagicMock(name="")
        tool_delta.function.name = ""
        tool_delta.function.arguments = '{"path": "a.txt"}'

        delta = MagicMock()
        delta.content = None
        delta.reasoning_details = None
        delta.reasoning_content = None
        delta.tool_calls = [tool_delta]

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = "tool_calls"

        chunk = MagicMock()
        chunk.choices = [choice]

        openai_client.client.chat.completions.create = AsyncMock(return_value=FakeAsyncStream([chunk]))

        result = await openai_client._make_stream_request(
            params={"model": "TestModel", "messages": [], "stream": True}
        )

        assert result.tool_calls is None

    @pytest.mark.asyncio
    async def test_stream_skips_tool_call_with_non_dict_arguments(self, openai_client):
        tool_delta = MagicMock()
        tool_delta.index = 0
        tool_delta.id = "call_1"
        tool_delta.function = MagicMock(name="read_file")
        tool_delta.function.name = "read_file"
        tool_delta.function.arguments = '123'

        delta = MagicMock()
        delta.content = None
        delta.reasoning_details = None
        delta.reasoning_content = None
        delta.tool_calls = [tool_delta]

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = "tool_calls"

        chunk = MagicMock()
        chunk.choices = [choice]

        openai_client.client.chat.completions.create = AsyncMock(return_value=FakeAsyncStream([chunk]))

        result = await openai_client._make_stream_request(
            params={"model": "TestModel", "messages": [], "stream": True}
        )

        assert result.tool_calls is None
