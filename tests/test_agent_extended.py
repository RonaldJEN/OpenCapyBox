"""Agent 核心類擴展測試 - 提升覆蓋率"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path
import json

from src.agent.agent import Agent, Colors
from src.agent.tools.base import ToolResult
from src.agent.schema import Message, LLMResponse, ToolCall, FunctionCall
from tests.helpers import MockLLMClient, MockTool, make_agent, collect_agui_events


class TestAgentTokenEstimation:
    """Token 計算測試"""

    @pytest.fixture
    def agent(self, tmp_path):
        """創建 Agent 實例"""
        return make_agent(tmp_path, system_prompt="You are a helpful assistant.", max_steps=10, token_limit=5000)

    def test_estimate_tokens_basic(self, agent):
        """測試基本 Token 估算"""
        tokens = agent._estimate_tokens()
        assert tokens > 0

    def test_estimate_tokens_with_messages(self, agent):
        """測試帶消息的 Token 估算"""
        agent.add_user_message("Hello, this is a test message")
        tokens = agent._estimate_tokens()
        assert tokens > 0

    def test_estimate_tokens_cache_invalidation(self, agent):
        """測試 Token 緩存失效"""
        initial_tokens = agent._estimate_tokens()
        
        # 添加消息
        agent.add_user_message("New message to invalidate cache")
        
        # 應該重新計算
        new_tokens = agent._estimate_tokens()
        assert new_tokens > initial_tokens

    @patch('src.agent.agent.tiktoken.get_encoding')
    def test_estimate_tokens_fallback(self, mock_tiktoken, agent):
        """測試 tiktoken 失敗時的 fallback"""
        mock_tiktoken.side_effect = Exception("tiktoken failed")
        
        # 強制重新計算
        agent._cached_token_count = 0
        agent._cached_message_count = 0
        
        tokens = agent._estimate_tokens_fallback()
        assert tokens >= 0

    def test_estimate_tokens_fallback_calculation(self, agent):
        """測試 fallback 計算方法"""
        agent.add_user_message("Test message for fallback")
        tokens = agent._estimate_tokens_fallback()
        assert tokens > 0


class TestAgentMessageSummarization:
    """消息摘要測試"""

    @pytest.fixture
    def agent_with_many_messages(self, tmp_path):
        """創建有很多消息的 Agent"""
        llm = MockLLMClient()
        llm.responses = [LLMResponse(content="Summary of execution", finish_reason="stop")]
        return make_agent(tmp_path, llm=llm, system_prompt="You are a helpful assistant.", max_steps=10, token_limit=100)

    @pytest.mark.asyncio
    async def test_summarize_messages_no_action_under_limit(self, tmp_path):
        """測試 Token 未超限時不摘要"""
        agent = make_agent(tmp_path, system_prompt="Short prompt", token_limit=100000)
        
        original_len = len(agent.messages)
        await agent._summarize_messages()
        
        # 消息數量不應改變
        assert len(agent.messages) == original_len

    @pytest.mark.asyncio
    async def test_summarize_messages_insufficient_messages(self, agent_with_many_messages):
        """測試消息不足時不摘要"""
        agent = agent_with_many_messages
        
        # 只有 system 消息，不足以摘要
        original_len = len(agent.messages)
        await agent._summarize_messages()
        
        # 消息數量不應改變（沒有 user 消息）
        assert len(agent.messages) == original_len

    @pytest.mark.asyncio
    async def test_create_summary_empty_messages(self, tmp_path):
        """測試空消息列表的摘要"""
        agent = make_agent(tmp_path)
        
        result = await agent._create_summary([], 1)
        assert result == ""

    @pytest.mark.asyncio
    async def test_create_summary_with_messages(self, tmp_path):
        """測試帶消息的摘要創建"""
        llm = MockLLMClient()
        llm.responses = [LLMResponse(content="Summary of the execution", finish_reason="stop")]
        agent = make_agent(tmp_path, llm=llm)
        
        messages = [
            Message(role="assistant", content="I will help you"),
            Message(role="tool", content="Tool result here", tool_call_id="123"),
        ]
        
        result = await agent._create_summary(messages, 1)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_create_summary_with_tool_calls(self, tmp_path):
        """測試帶工具調用的摘要創建"""
        llm = MockLLMClient()
        llm.responses = [LLMResponse(content="Summary with tool calls", finish_reason="stop")]
        agent = make_agent(tmp_path, llm=llm)
        
        messages = [
            Message(
                role="assistant",
                content="Using tool",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        type="function",
                        function=FunctionCall(name="read_file", arguments={"path": "test.txt"})
                    )
                ]
            ),
        ]
        
        result = await agent._create_summary(messages, 1)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_create_summary_llm_failure(self, tmp_path):
        """測試 LLM 摘要失敗時的處理"""
        llm = MockLLMClient()
        
        async def fail_generate(*args, **kwargs):
            raise Exception("LLM failed")
        
        llm.generate = fail_generate
        agent = make_agent(tmp_path, llm=llm)
        
        messages = [
            Message(role="assistant", content="Some content"),
        ]
        
        # 應該返回簡單文本摘要而不是拋出異常
        result = await agent._create_summary(messages, 1)
        assert "Round 1" in result

    @pytest.mark.asyncio
    async def test_summarize_messages_uses_assistant_role_for_summary(self, tmp_path):
        """摘要回寫應使用 assistant 角色，避免權限升級"""
        agent = make_agent(tmp_path, token_limit=10)
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="u1"),
            Message(role="assistant", content="a1"),
            Message(role="user", content="u2"),
        ]

        with patch.object(agent, "_estimate_tokens", return_value=999):
            with patch.object(agent, "_create_summary", new=AsyncMock(return_value="summary text")):
                await agent._summarize_messages()

        summary_messages = [
            msg for msg in agent.messages
            if isinstance(msg.content, str) and msg.content.startswith("[Assistant Execution Summary")
        ]
        assert summary_messages
        assert all(msg.role == "assistant" for msg in summary_messages)


class TestAgentRun:
    """Agent 運行測試"""

    @pytest.fixture
    def simple_agent(self, tmp_path):
        """創建簡單 Agent"""
        llm = MockLLMClient()
        llm.responses = [LLMResponse(content="Task completed", finish_reason="stop")]
        return make_agent(tmp_path, llm=llm, system_prompt="You are a helpful assistant.", max_steps=5)

    @pytest.mark.asyncio
    async def test_run_agui_simple(self, simple_agent):
        """測試 AG-UI 運行模式"""
        simple_agent.add_user_message("Hello")

        events, event_types = await collect_agui_events(simple_agent)

        # 檢查基本事件流
        assert "RUN_STARTED" in event_types
        assert "RUN_FINISHED" in event_types or "RUN_ERROR" in event_types

    @pytest.mark.asyncio
    async def test_run_agui_with_tool_calls(self, tmp_path):
        """測試帶工具調用的運行"""
        llm = MockLLMClient()
        
        # 第一次返回工具調用，第二次返回最終響應
        llm.responses = [
            LLMResponse(
                content="Using tool",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        type="function",
                        function=FunctionCall(name="mock_tool", arguments={"param1": "test"})
                    )
                ]
            ),
            LLMResponse(content="Task completed after tool", finish_reason="stop"),
        ]
        
        agent = make_agent(tmp_path, llm=llm)
        agent.add_user_message("Use the tool")

        events, event_types = await collect_agui_events(agent)

        # 檢查工具調用事件
        assert "TOOL_CALL_START" in event_types
        assert "TOOL_CALL_RESULT" in event_types

    @pytest.mark.asyncio
    async def test_run_agui_unknown_tool(self, tmp_path):
        """測試調用未知工具"""
        llm = MockLLMClient()
        llm.responses = [
            LLMResponse(
                content="Trying unknown tool",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        type="function",
                        function=FunctionCall(name="unknown_tool", arguments={"param": "value"})
                    )
                ]
            ),
            LLMResponse(content="Tool not found", finish_reason="stop"),
        ]
        
        agent = make_agent(tmp_path, llm=llm)
        agent.add_user_message("Call unknown")

        events, event_types = await collect_agui_events(agent)

        # 應該處理未知工具錯誤
        assert "RUN_STARTED" in event_types

    @pytest.mark.asyncio
    async def test_run_agui_tool_execution_error(self, tmp_path):
        """測試工具執行錯誤"""
        llm = MockLLMClient()
        llm.responses = [
            LLMResponse(
                content="Calling failing tool",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        type="function",
                        function=FunctionCall(name="failing_tool", arguments={"param1": "test"})
                    )
                ]
            ),
            LLMResponse(content="Handled error", finish_reason="stop"),
        ]
        
        agent = make_agent(tmp_path, llm=llm, tools=[MockTool(name="failing_tool", raise_on_execute=True)])
        agent.add_user_message("Call failing tool")

        events, event_types = await collect_agui_events(agent)

        # 應該捕獲異常並繼續
        assert "RUN_STARTED" in event_types

    @pytest.mark.asyncio
    async def test_run_agui_max_steps_reached(self, tmp_path):
        """測試達到最大步驟"""
        llm = MockLLMClient()
        
        # 持續返回工具調用，永不結束
        for _ in range(10):
            llm.responses.append(
                LLMResponse(
                    content="Using tool again",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCall(
                            id=f"call_{_}",
                            type="function",
                            function=FunctionCall(name="mock_tool", arguments={"param1": "test"})
                        )
                    ]
                )
            )
        
        agent = make_agent(tmp_path, llm=llm, max_steps=3)
        agent.add_user_message("Loop forever")

        events, event_types = await collect_agui_events(agent)

        # 檢查是否發出 interrupt 結束事件
        assert "RUN_FINISHED" in event_types

    @pytest.mark.asyncio
    async def test_run_agui_llm_error(self, tmp_path):
        """測試 LLM 調用錯誤"""
        llm = MockLLMClient()
        
        async def fail_generate_stream(*args, **kwargs):
            raise Exception("LLM API error")
        
        llm.generate_stream = fail_generate_stream
        agent = make_agent(tmp_path, llm=llm)
        agent.add_user_message("Hello")

        events, event_types = await collect_agui_events(agent)

        # 檢查是否發出錯誤事件
        assert "RUN_ERROR" in event_types

    @pytest.mark.asyncio
    async def test_run_agui_with_invalid_tool_arguments(self, tmp_path):
        """測試無效工具參數（缺少必要參數）"""
        llm = MockLLMClient()
        llm.responses = [
            LLMResponse(
                content="Calling with incomplete args",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        type="function",
                        function=FunctionCall(name="nonexistent_tool", arguments={})  # 不存在的工具
                    )
                ]
            ),
            LLMResponse(content="Handled invalid args", finish_reason="stop"),
        ]
        
        agent = make_agent(tmp_path, llm=llm)
        agent.add_user_message("Bad call")

        events, event_types = await collect_agui_events(agent)

        # 應該正常完成（未知工具返回錯誤結果但不崩潰）
        assert "RUN_STARTED" in event_types
        assert "RUN_FINISHED" in event_types or "RUN_ERROR" in event_types

    @pytest.mark.asyncio
    async def test_run_agui_missing_required_arguments_can_recover(self, tmp_path):
        """缺少必要參數應回填錯誤並允許下一步自我修復"""
        llm = MockLLMClient()
        llm.responses = [
            LLMResponse(
                content="Calling tool without required args",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        type="function",
                        function=FunctionCall(name="mock_tool", arguments={}),
                    )
                ],
            ),
            LLMResponse(content="Recovered after tool error", finish_reason="stop"),
        ]

        agent = make_agent(tmp_path, llm=llm, max_steps=5)
        agent.add_user_message("run tool")

        events, event_types = await collect_agui_events(agent)

        run_finished = [e for e in events if e.type.value == "RUN_FINISHED"]
        assert run_finished
        assert run_finished[-1].outcome == "success"
        assert llm.call_count == 2


class TestAgentGetHistory:
    """消息歷史測試"""

    def test_get_history_returns_copy(self, tmp_path):
        """測試獲取歷史返回副本"""
        agent = make_agent(tmp_path)
        
        history1 = agent.get_history()
        history2 = agent.get_history()
        
        # 應該是不同的列表對象
        assert history1 is not history2

    def test_get_history_content(self, tmp_path):
        """測試獲取歷史內容"""
        agent = make_agent(tmp_path)
        
        agent.add_user_message("User message 1")
        agent.add_user_message("User message 2")
        
        history = agent.get_history()
        
        # 應該包含 system + 2 個 user 消息
        assert len(history) == 3
        assert history[0].role == "system"
        assert history[1].content == "User message 1"
        assert history[2].content == "User message 2"
