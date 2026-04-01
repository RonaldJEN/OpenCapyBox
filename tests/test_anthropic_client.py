"""Anthropic Client 測試"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import json

from src.agent.llm.anthropic_client import AnthropicClient
from src.agent.schema import Message, LLMResponse, ToolCall, FunctionCall
from src.agent.retry import RetryConfig
from tests.helpers import MockTool


class MockAnthropicResponse:
    """模擬 Anthropic API 響應"""

    def __init__(
        self,
        content=None,
        stop_reason="end_turn",
        usage=None
    ):
        self.content = content or []
        self.stop_reason = stop_reason
        self.usage = usage or MagicMock(input_tokens=100, output_tokens=50)


class MockContentBlock:
    """模擬內容塊"""

    def __init__(self, block_type, **kwargs):
        self.type = block_type
        for key, value in kwargs.items():
            setattr(self, key, value)


# ── 模块级 fixtures（消除 6 处重复）─────────────────────────


@pytest.fixture
def anthropic_client():
    """基础 AnthropicClient"""
    with patch('anthropic.Anthropic'):
        return AnthropicClient(api_key="test-key")


@pytest.fixture
def anthropic_client_with_mock():
    """AnthropicClient + client.client = MagicMock()（用于 API 请求测试）"""
    with patch('anthropic.Anthropic'):
        client = AnthropicClient(api_key="test-key")
        client.client = MagicMock()
        return client


class TestAnthropicClientInit:
    """AnthropicClient 初始化測試"""

    def test_client_initialization(self):
        """測試客戶端初始化"""
        with patch('anthropic.Anthropic') as mock_anthropic:
            client = AnthropicClient(
                api_key="test-key",
                api_base="https://api.example.com",
                model="test-model"
            )

            assert client.api_key == "test-key"
            assert client.api_base == "https://api.example.com"
            assert client.model == "test-model"

    def test_client_with_retry_config(self):
        """測試帶重試配置的客戶端初始化"""
        with patch('anthropic.Anthropic'):
            retry_config = RetryConfig(max_retries=5, initial_delay=2.0)
            client = AnthropicClient(
                api_key="test-key",
                retry_config=retry_config
            )

            assert client.retry_config.max_retries == 5
            assert client.retry_config.initial_delay == 2.0

    def test_client_default_values(self):
        """測試客戶端默認值"""
        with patch('anthropic.Anthropic'):
            client = AnthropicClient(api_key="test-key")

            assert "minimaxi" in client.api_base.lower() or "anthropic" in client.api_base.lower()
            assert client.model is not None


class TestAnthropicClientConvertTools:
    """工具轉換測試"""

    def test_convert_dict_tools(self, anthropic_client):
        """測試轉換字典格式的工具"""
        tools = [
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                }
            }
        ]

        result = anthropic_client._convert_tools(tools)

        assert len(result) == 1
        assert result[0]["name"] == "read_file"

    def test_convert_tool_objects(self, anthropic_client):
        """測試轉換 Tool 對象（使用 helpers.MockTool）"""
        tool = MockTool()
        result = anthropic_client._convert_tools([tool])

        assert len(result) == 1
        assert result[0]["name"] == "mock_tool"

    def test_convert_unsupported_tool_type(self, anthropic_client):
        """測試不支持的工具類型"""
        tools = ["invalid_tool"]

        with pytest.raises(TypeError):
            anthropic_client._convert_tools(tools)


class TestAnthropicClientConvertMessages:
    """消息轉換測試"""

    def test_convert_system_message(self, anthropic_client):
        """測試轉換系統消息"""
        messages = [
            Message(role="system", content="You are a helpful assistant.")
        ]

        system_msg, api_msgs = anthropic_client._convert_messages(messages)

        assert system_msg == "You are a helpful assistant."
        assert api_msgs == []

    def test_convert_user_message(self, anthropic_client):
        """測試轉換用戶消息"""
        messages = [
            Message(role="user", content="Hello")
        ]

        system_msg, api_msgs = anthropic_client._convert_messages(messages)

        assert system_msg is None
        assert len(api_msgs) == 1
        assert api_msgs[0]["role"] == "user"
        assert api_msgs[0]["content"] == "Hello"

    def test_convert_assistant_message(self, anthropic_client):
        """測試轉換助手消息"""
        messages = [
            Message(role="assistant", content="Hi there!")
        ]

        system_msg, api_msgs = anthropic_client._convert_messages(messages)

        assert len(api_msgs) == 1
        assert api_msgs[0]["role"] == "assistant"
        assert api_msgs[0]["content"] == "Hi there!"

    def test_convert_assistant_with_thinking(self, anthropic_client):
        """測試轉換帶思考的助手消息"""
        messages = [
            Message(
                role="assistant",
                content="The answer is 42.",
                thinking="Let me think about this..."
            )
        ]

        system_msg, api_msgs = anthropic_client._convert_messages(messages)

        assert len(api_msgs) == 1
        content_blocks = api_msgs[0]["content"]

        # 檢查是否有 thinking 塊
        thinking_blocks = [b for b in content_blocks if b.get("type") == "thinking"]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0]["thinking"] == "Let me think about this..."

    def test_convert_assistant_with_tool_calls(self, anthropic_client):
        """測試轉換帶工具調用的助手消息"""
        messages = [
            Message(
                role="assistant",
                content="I'll read the file.",
                tool_calls=[
                    ToolCall(
                        id="call_123",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "test.txt"}
                        )
                    )
                ]
            )
        ]

        system_msg, api_msgs = anthropic_client._convert_messages(messages)

        assert len(api_msgs) == 1
        content_blocks = api_msgs[0]["content"]

        # 檢查是否有 tool_use 塊
        tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]
        assert len(tool_use_blocks) == 1
        assert tool_use_blocks[0]["name"] == "read_file"
        assert tool_use_blocks[0]["id"] == "call_123"

    def test_convert_tool_result_message(self, anthropic_client):
        """測試轉換工具結果消息"""
        messages = [
            Message(
                role="tool",
                content="File content here",
                tool_call_id="call_123"
            )
        ]

        system_msg, api_msgs = anthropic_client._convert_messages(messages)

        assert len(api_msgs) == 1
        assert api_msgs[0]["role"] == "user"

        content_blocks = api_msgs[0]["content"]
        assert len(content_blocks) == 1
        assert content_blocks[0]["type"] == "tool_result"
        assert content_blocks[0]["tool_use_id"] == "call_123"
        assert content_blocks[0]["content"] == "File content here"

    def test_convert_full_conversation(self, anthropic_client):
        """測試轉換完整對話"""
        messages = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi!"),
            Message(role="user", content="How are you?"),
            Message(role="assistant", content="I'm fine, thanks!"),
        ]

        system_msg, api_msgs = anthropic_client._convert_messages(messages)

        assert system_msg == "You are helpful."
        assert len(api_msgs) == 4


class TestAnthropicClientPrepareRequest:
    """請求準備測試"""

    def test_prepare_request_basic(self, anthropic_client):
        """測試基本請求準備"""
        messages = [
            Message(role="system", content="System"),
            Message(role="user", content="Hello"),
        ]

        request = anthropic_client._prepare_request(messages)

        assert "system_message" in request
        assert "api_messages" in request
        assert request["system_message"] == "System"
        assert len(request["api_messages"]) == 1

    def test_prepare_request_with_tools(self, anthropic_client):
        """測試帶工具的請求準備"""
        messages = [Message(role="user", content="Hello")]
        tools = [{"name": "test_tool", "description": "Test"}]

        request = anthropic_client._prepare_request(messages, tools=tools)

        assert request["tools"] == tools


class TestAnthropicClientMakeRequest:
    """API 請求測試"""

    @pytest.mark.asyncio
    async def test_make_api_request_basic(self, anthropic_client_with_mock):
        """測試基本 API 請求"""
        client = anthropic_client_with_mock
        mock_response = MockAnthropicResponse(
            content=[MockContentBlock("text", text="Hello!")]
        )
        client.client.messages.create = MagicMock(return_value=mock_response)

        result = await client._make_api_request(
            system_message="System",
            api_messages=[{"role": "user", "content": "Hello"}]
        )

        assert result == mock_response
        client.client.messages.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_make_api_request_with_tools(self, anthropic_client_with_mock):
        """測試帶工具的 API 請求"""
        client = anthropic_client_with_mock
        mock_response = MockAnthropicResponse(
            content=[MockContentBlock("text", text="Using tool")]
        )
        client.client.messages.create = MagicMock(return_value=mock_response)

        tools = [{"name": "test", "description": "Test", "input_schema": {}}]

        result = await client._make_api_request(
            system_message=None,
            api_messages=[{"role": "user", "content": "Hello"}],
            tools=tools
        )

        assert result == mock_response


class TestAnthropicClientParseResponse:
    """響應解析測試"""

    def test_parse_text_response(self, anthropic_client):
        """測試解析文本響應"""
        response = MockAnthropicResponse(
            content=[MockContentBlock("text", text="Hello world!")]
        )

        result = anthropic_client._parse_response(response)

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello world!"

    def test_parse_response_with_thinking(self, anthropic_client):
        """測試解析帶思考的響應"""
        response = MockAnthropicResponse(
            content=[
                MockContentBlock("thinking", thinking="Let me think..."),
                MockContentBlock("text", text="The answer is 42."),
            ]
        )

        result = anthropic_client._parse_response(response)

        assert result.thinking == "Let me think..."
        assert result.content == "The answer is 42."

    def test_parse_response_with_tool_use(self, anthropic_client):
        """測試解析帶工具調用的響應"""
        response = MockAnthropicResponse(
            content=[
                MockContentBlock(
                    "tool_use",
                    id="call_123",
                    name="read_file",
                    input={"path": "test.txt"}
                )
            ],
            stop_reason="tool_use"
        )

        result = anthropic_client._parse_response(response)

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_123"
        assert result.tool_calls[0].function.name == "read_file"

    def test_parse_response_multiple_tool_calls(self, anthropic_client):
        """測試解析多個工具調用"""
        response = MockAnthropicResponse(
            content=[
                MockContentBlock("text", text="I'll use multiple tools"),
                MockContentBlock(
                    "tool_use",
                    id="call_1",
                    name="read_file",
                    input={"path": "a.txt"}
                ),
                MockContentBlock(
                    "tool_use",
                    id="call_2",
                    name="write_file",
                    input={"path": "b.txt", "content": "data"}
                ),
            ],
            stop_reason="tool_use"
        )

        result = anthropic_client._parse_response(response)

        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].function.name == "read_file"
        assert result.tool_calls[1].function.name == "write_file"


class TestAnthropicClientGenerate:
    """完整生成測試"""

    @pytest.mark.asyncio
    async def test_generate_simple(self, anthropic_client_with_mock):
        """測試簡單生成"""
        client = anthropic_client_with_mock
        mock_response = MockAnthropicResponse(
            content=[MockContentBlock("text", text="Generated text")]
        )
        client.client.messages.create = MagicMock(return_value=mock_response)

        messages = [
            Message(role="user", content="Hello")
        ]

        result = await client.generate(messages)

        assert isinstance(result, LLMResponse)
        assert result.content == "Generated text"

    @pytest.mark.asyncio
    async def test_generate_with_system(self, anthropic_client_with_mock):
        """測試帶系統消息的生成"""
        client = anthropic_client_with_mock
        mock_response = MockAnthropicResponse(
            content=[MockContentBlock("text", text="Response")]
        )
        client.client.messages.create = MagicMock(return_value=mock_response)

        messages = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hello"),
        ]

        result = await client.generate(messages)

        assert result.content == "Response"

        # 驗證 system 消息被正確傳遞
        call_args = client.client.messages.create.call_args
        assert "system" in call_args.kwargs or call_args.kwargs.get("system") is not None

    @pytest.mark.asyncio
    async def test_generate_with_tools(self, anthropic_client_with_mock):
        """測試帶工具的生成（使用 helpers.MockTool）"""
        client = anthropic_client_with_mock
        mock_response = MockAnthropicResponse(
            content=[
                MockContentBlock(
                    "tool_use",
                    id="call_1",
                    name="test_tool",
                    input={"param": "value"}
                )
            ],
            stop_reason="tool_use"
        )
        client.client.messages.create = MagicMock(return_value=mock_response)

        tool = MockTool(name="test_tool")

        messages = [Message(role="user", content="Use tool")]

        result = await client.generate(messages, tools=[tool])

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
