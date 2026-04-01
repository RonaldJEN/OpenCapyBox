"""Agent cancel_token 取消機制測試"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock

from src.agent.schema import ToolCall, FunctionCall, LLMResponse
from src.agent.schema.agui_events import EventType
from tests.helpers import SlowTool, fake_stream, make_agent


# ── helpers ──────────────────────────────────────────────────


def _make_agent(tools=None, max_steps=5):
    """創建一個帶 mock LLM 的 Agent（委托给 helpers.make_agent）"""
    from unittest.mock import MagicMock, AsyncMock
    llm = MagicMock()
    llm.generate_stream = AsyncMock()
    agent = make_agent(llm=llm, tools=tools or [], max_steps=max_steps, system_prompt="test")
    return agent, llm


# ── tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_before_first_step():
    """cancel_token 在第一個 step 之前就已設置 → 立即退出"""
    agent, llm = _make_agent()

    cancel_token = asyncio.Event()
    cancel_token.set()  # 預設取消

    events = []
    async for event in agent.run_agui("thread-1", "run-1", cancel_token=cancel_token):
        events.append(event)

    types = [e.type for e in events]
    assert EventType.RUN_STARTED in types
    assert EventType.RUN_FINISHED in types
    # 不應該有任何 STEP_STARTED
    assert EventType.STEP_STARTED not in types

    run_finished = [e for e in events if e.type == EventType.RUN_FINISHED][0]
    assert run_finished.outcome == "interrupt"
    assert run_finished.result.get("reason") == "user_cancelled"


@pytest.mark.asyncio
async def test_cancel_after_llm_response_before_tool():
    """LLM 回覆後、工具執行前取消 → 跳過工具調用"""
    slow_tool = SlowTool()
    agent, llm = _make_agent(tools=[slow_tool])

    tool_call = ToolCall(
        id="tc-1",
        type="function",
        function=FunctionCall(name="slow_tool", arguments={"msg": "hello"}),
    )
    # LLM 返回帶工具調用的回覆
    response = LLMResponse(content="", tool_calls=[tool_call], finish_reason="tool_calls")

    llm.generate_stream = fake_stream(response)

    cancel_token = asyncio.Event()

    events = []
    async for event in agent.run_agui("thread-1", "run-1", cancel_token=cancel_token):
        events.append(event)
        # 在 STEP_STARTED 之後設置取消（模擬用戶在 LLM 回覆後點停止）
        if event.type == EventType.STEP_STARTED:
            cancel_token.set()

    types = [e.type for e in events]
    # 應該有 RUN_FINISHED
    assert EventType.RUN_FINISHED in types
    run_finished = [e for e in events if e.type == EventType.RUN_FINISHED][0]
    assert run_finished.outcome == "interrupt"

    # 工具不應該被實際執行
    assert slow_tool.call_count == 0


@pytest.mark.asyncio
async def test_cancel_skips_remaining_tools():
    """有多個工具調用時，取消應跳過剩餘的工具並補充 cancelled result"""
    tool1 = SlowTool()
    tool1._name_override = "slow_tool"  # keep name as slow_tool
    agent, llm = _make_agent(tools=[tool1])

    tc1 = ToolCall(id="tc-1", type="function", function=FunctionCall(name="slow_tool", arguments={"msg": "a"}))
    tc2 = ToolCall(id="tc-2", type="function", function=FunctionCall(name="slow_tool", arguments={"msg": "b"}))
    response = LLMResponse(content="", tool_calls=[tc1, tc2], finish_reason="tool_calls")

    llm.generate_stream = fake_stream(response)

    cancel_token = asyncio.Event()

    events = []
    async for event in agent.run_agui("thread-1", "run-1", cancel_token=cancel_token):
        events.append(event)
        # 在第一個 tool_call 發射之後取消
        if event.type == EventType.TOOL_CALL_END and getattr(event, 'tool_call_id', '') == 'tc-1':
            cancel_token.set()

    types = [e.type for e in events]
    assert EventType.RUN_FINISHED in types

    # 收集所有 TOOL_CALL_RESULT
    results = [e for e in events if e.type == EventType.TOOL_CALL_RESULT]
    # tc-1 和 tc-2 都應該有 result（tc-1 cancelled，tc-2 also cancelled）
    result_ids = [e.tool_call_id for e in results]
    assert "tc-1" in result_ids
    assert "tc-2" in result_ids

    # 工具不應被實際執行
    assert tool1.call_count == 0


@pytest.mark.asyncio
async def test_no_cancel_runs_normally():
    """沒有 cancel_token 時正常完成"""
    agent, llm = _make_agent()

    response = LLMResponse(content="Final answer", tool_calls=[], finish_reason="stop")

    llm.generate_stream = fake_stream(response, on_content_text="Final answer")

    events = []
    async for event in agent.run_agui("thread-1", "run-1"):
        events.append(event)

    types = [e.type for e in events]
    assert EventType.RUN_STARTED in types
    assert EventType.RUN_FINISHED in types

    run_finished = [e for e in events if e.type == EventType.RUN_FINISHED][0]
    assert run_finished.outcome == "success"


@pytest.mark.asyncio
async def test_cancel_token_none_runs_normally():
    """cancel_token=None 時正常完成（向後兼容）"""
    agent, llm = _make_agent()

    response = LLMResponse(content="OK", tool_calls=[], finish_reason="stop")

    llm.generate_stream = fake_stream(response)

    events = []
    async for event in agent.run_agui("thread-1", "run-1", cancel_token=None):
        events.append(event)

    run_finished = [e for e in events if e.type == EventType.RUN_FINISHED][0]
    assert run_finished.outcome == "success"
