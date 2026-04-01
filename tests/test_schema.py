"""Schema 模型測試"""
import pytest
from pydantic import ValidationError

from src.agent.schema import (
    LLMProvider,
    FunctionCall,
    ToolCall,
    Message,
    LLMResponse,
)


class TestLLMProvider:
    """LLMProvider 枚舉測試"""

    def test_anthropic_value(self):
        """測試 Anthropic 值"""
        assert LLMProvider.ANTHROPIC.value == "anthropic"

    def test_openai_value(self):
        """測試 OpenAI 值"""
        assert LLMProvider.OPENAI.value == "openai"

    def test_from_string(self):
        """測試從字符串創建"""
        provider = LLMProvider("anthropic")
        assert provider == LLMProvider.ANTHROPIC

    def test_invalid_provider(self):
        """測試無效提供者"""
        with pytest.raises(ValueError):
            LLMProvider("invalid")

    def test_is_string(self):
        """測試是否為字符串子類"""
        assert isinstance(LLMProvider.ANTHROPIC, str)


class TestFunctionCall:
    """FunctionCall 模型測試"""

    def test_basic_creation(self):
        """測試基本創建"""
        func = FunctionCall(
            name="read_file",
            arguments={"path": "test.txt"}
        )
        
        assert func.name == "read_file"
        assert func.arguments["path"] == "test.txt"

    def test_empty_arguments(self):
        """測試空參數"""
        func = FunctionCall(
            name="list_dir",
            arguments={}
        )
        
        assert func.arguments == {}

    def test_complex_arguments(self):
        """測試複雜參數"""
        func = FunctionCall(
            name="search",
            arguments={
                "query": "test",
                "limit": 10,
                "filters": ["a", "b"],
                "options": {"deep": True}
            }
        )
        
        assert func.arguments["limit"] == 10
        assert func.arguments["filters"] == ["a", "b"]


class TestToolCall:
    """ToolCall 模型測試"""

    def test_basic_creation(self):
        """測試基本創建"""
        tool_call = ToolCall(
            id="call_123",
            type="function",
            function=FunctionCall(
                name="bash",
                arguments={"command": "ls"}
            )
        )
        
        assert tool_call.id == "call_123"
        assert tool_call.type == "function"
        assert tool_call.function.name == "bash"

    def test_from_dict(self):
        """測試從字典創建"""
        data = {
            "id": "call_456",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": {"path": "test.txt", "content": "hello"}
            }
        }
        
        tool_call = ToolCall(**data)
        
        assert tool_call.id == "call_456"
        assert tool_call.function.arguments["content"] == "hello"


class TestMessage:
    """Message 模型測試"""

    def test_user_message(self):
        """測試用戶消息"""
        msg = Message(role="user", content="Hello")
        
        assert msg.role == "user"
        assert msg.content == "Hello"

    def test_system_message(self):
        """測試系統消息"""
        msg = Message(
            role="system",
            content="You are a helpful assistant."
        )
        
        assert msg.role == "system"

    def test_assistant_message_with_thinking(self):
        """測試帶思考的助手消息"""
        msg = Message(
            role="assistant",
            content="The answer is 42.",
            thinking="Let me analyze this question..."
        )
        
        assert msg.thinking == "Let me analyze this question..."

    def test_assistant_message_with_tool_calls(self):
        """測試帶工具調用的助手消息"""
        msg = Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    type="function",
                    function=FunctionCall(name="read_file", arguments={"path": "a.txt"})
                )
            ]
        )
        
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].function.name == "read_file"

    def test_tool_result_message(self):
        """測試工具結果消息"""
        msg = Message(
            role="tool",
            content="File content here",
            tool_call_id="call_1",
            name="read_file"
        )
        
        assert msg.role == "tool"
        assert msg.tool_call_id == "call_1"
        assert msg.name == "read_file"

    def test_content_as_list(self):
        """測試列表類型內容"""
        msg = Message(
            role="user",
            content=[
                {"type": "text", "text": "Describe this image"},
                {"type": "image_url", "image_url": {"url": "http://..."}}
            ]
        )
        
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2

    def test_optional_fields(self):
        """測試可選字段"""
        msg = Message(role="user", content="test")
        
        assert msg.thinking is None
        assert msg.tool_calls is None
        assert msg.tool_call_id is None
        assert msg.name is None


class TestLLMResponse:
    """LLMResponse 模型測試"""

    def test_basic_response(self):
        """測試基本響應"""
        response = LLMResponse(
            content="Hello, I'm here to help.",
            finish_reason="stop"
        )
        
        assert response.content == "Hello, I'm here to help."
        assert response.finish_reason == "stop"

    def test_response_with_thinking(self):
        """測試帶思考的響應"""
        response = LLMResponse(
            content="The result is...",
            thinking="Analyzing the problem...",
            finish_reason="stop"
        )
        
        assert response.thinking == "Analyzing the problem..."

    def test_response_with_tool_calls(self):
        """測試帶工具調用的響應"""
        response = LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="call_abc",
                    type="function",
                    function=FunctionCall(name="bash", arguments={"command": "pwd"})
                )
            ],
            finish_reason="tool_calls"
        )
        
        assert response.finish_reason == "tool_calls"
        assert len(response.tool_calls) == 1

    def test_finish_reasons(self):
        """測試不同的結束原因"""
        # 正常結束
        r1 = LLMResponse(content="done", finish_reason="stop")
        assert r1.finish_reason == "stop"
        
        # 工具調用
        r2 = LLMResponse(content="", finish_reason="tool_calls")
        assert r2.finish_reason == "tool_calls"
        
        # 長度限制
        r3 = LLMResponse(content="...", finish_reason="length")
        assert r3.finish_reason == "length"
