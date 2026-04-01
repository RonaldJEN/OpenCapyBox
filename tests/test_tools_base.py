"""Tool 基礎類測試"""
import pytest
from src.agent.tools.base import ToolResult
from tests.helpers import MockTool


class TestToolResult:
    """ToolResult 測試"""

    def test_success_result(self):
        """測試成功結果"""
        result = ToolResult(success=True, content="操作成功")
        assert result.success is True
        assert result.content == "操作成功"
        assert result.error is None

    def test_error_result(self):
        """測試錯誤結果"""
        result = ToolResult(success=False, error="文件不存在")
        assert result.success is False
        assert result.error == "文件不存在"

    def test_result_with_empty_content(self):
        """測試空內容結果"""
        result = ToolResult(success=True)
        assert result.success is True
        assert result.content == ""

    def test_result_serialization(self):
        """測試結果序列化"""
        result = ToolResult(success=True, content="test", error=None)
        data = result.model_dump()
        assert data["success"] is True
        assert data["content"] == "test"


class TestToolBase:
    """Tool 基礎類測試"""

    def test_tool_schema(self):
        """測試 Anthropic schema 轉換"""
        tool = MockTool()
        schema = tool.to_schema()

        assert schema["name"] == "mock_tool"
        assert schema["description"] == "A mock tool for testing"
        assert "input_schema" in schema
        assert schema["input_schema"]["type"] == "object"

    def test_tool_openai_schema(self):
        """測試 OpenAI schema 轉換"""
        tool = MockTool()
        schema = tool.to_openai_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "mock_tool"
        assert "parameters" in schema["function"]

    @pytest.mark.asyncio
    async def test_tool_execute(self):
        """測試工具執行"""
        tool = MockTool()
        result = await tool.execute(param1="hello")

        assert result.success is True
        assert tool.execute_count == 1
