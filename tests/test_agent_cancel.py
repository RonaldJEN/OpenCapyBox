"""Agent cancel_token 取消機制測試"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.agent.schema import ToolCall, FunctionCall, LLMResponse
from src.agent.schema.agui_events import EventType
from src.agent.tools.base import ToolResult
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


# ── tool timeout tests ──────────────────────────────────────


class TimeoutTool(SlowTool):
    """模拟一个会超时的工具"""

    @property
    def name(self) -> str:
        return "timeout_tool"

    async def execute(self, **kwargs):
        await asyncio.sleep(999)  # 永远不会完成
        return ToolResult(success=True, content="done")


class TimeoutWriteTool(TimeoutTool):
    """模拟已发出但未确认结果的文件写入。"""

    repeat_policy = "mutating"

    @property
    def name(self) -> str:
        return "write_file"


class TimeoutEditTool(TimeoutWriteTool):
    @property
    def name(self) -> str:
        return "edit_file"


class NoTimeoutTool(SlowTool):
    """模拟显式关闭 Agent 单工具超时的工具"""

    execute_timeout = 0

    @property
    def name(self) -> str:
        return "no_timeout_tool"

    async def execute(self, **kwargs):
        return ToolResult(success=True, content="no-timeout-done")


@pytest.mark.asyncio
async def test_tool_execution_timeout():
    """工具执行超时后返回错误结果，Agent 继续运行"""
    timeout_tool = TimeoutTool()
    agent, llm = _make_agent(tools=[timeout_tool])
    agent.tool_timeout = 1  # 1 秒超时

    tool_call = ToolCall(
        id="tc-timeout",
        type="function",
        function=FunctionCall(name="timeout_tool", arguments={"msg": "hi"}),
    )
    # 第一次 LLM 调用返回工具调用，第二次返回最终回复
    response1 = LLMResponse(content="", tool_calls=[tool_call], finish_reason="tool_calls")
    response2 = LLMResponse(content="Done", tool_calls=[], finish_reason="stop")

    call_count = 0

    async def _fake_stream(messages, tools, on_content=None, on_thinking=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return response1
        if on_content:
            await on_content("Done")
        return response2

    llm.generate_stream = _fake_stream

    events = []
    with patch.object(agent, "_record_permission_audit") as audit:
        async for event in agent.run_agui("thread-1", "run-timeout"):
            events.append(event)

    types = [e.type for e in events]
    # 应该正常完成（工具超时不中断 Agent，只是返回错误）
    assert EventType.RUN_FINISHED in types
    run_finished = [e for e in events if e.type == EventType.RUN_FINISHED][0]
    assert run_finished.outcome == "success"

    # 应该有超时错误的 TOOL_CALL_RESULT
    tool_results = [e for e in events if e.type == EventType.TOOL_CALL_RESULT]
    assert len(tool_results) >= 1
    assert "timed out" in tool_results[0].content.lower()
    assert "read_file" not in tool_results[0].content
    audit.assert_called_once()
    assert audit.call_args.kwargs["outcome"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_class", "tool_name"),
    [
        (TimeoutWriteTool, "write_file"),
        (TimeoutEditTool, "edit_file"),
    ],
)
async def test_file_write_timeout_requires_read_verification(tool_class, tool_name):
    """文件写入超时时应明确要求读取验证后再重试。"""
    timeout_tool = tool_class()
    agent, _ = _make_agent(tools=[timeout_tool])
    agent.tool_timeout = 0.01

    record = await agent._execute_tool_call_for_record(
        index=0,
        thread_id="thread-1",
        run_id="run-timeout-write",
        tool_call_id="tc-timeout-write",
        function_name=tool_name,
        arguments={"path": "/home/user/out.txt", "content": "new"},
        cancel_token=None,
    )

    assert record.result.success is False
    assert record.result.outcome_uncertain is True
    assert "timed out" in record.result_content.lower()
    assert "read_file" in record.result_content
    assert "/home/user/out.txt" in record.result_content
    assert "before retrying" in record.result_content


@pytest.mark.asyncio
async def test_tool_with_custom_execute_timeout():
    """工具级 execute_timeout 覆盖全局 tool_timeout"""
    timeout_tool = TimeoutTool()
    timeout_tool.execute_timeout = 1  # 工具级覆盖：1 秒

    agent, llm = _make_agent(tools=[timeout_tool])
    agent.tool_timeout = 600  # 全局 10 分钟（不应生效）

    tool_call = ToolCall(
        id="tc-custom",
        type="function",
        function=FunctionCall(name="timeout_tool", arguments={"msg": "hi"}),
    )
    response1 = LLMResponse(content="", tool_calls=[tool_call], finish_reason="tool_calls")
    response2 = LLMResponse(content="OK", tool_calls=[], finish_reason="stop")

    call_count = 0

    async def _fake_stream(messages, tools, on_content=None, on_thinking=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return response1
        if on_content:
            await on_content("OK")
        return response2

    llm.generate_stream = _fake_stream

    events = []
    async for event in agent.run_agui("thread-1", "run-custom-timeout"):
        events.append(event)

    # 验证 1 秒就超时了（而非等待 600 秒）
    tool_results = [e for e in events if e.type == EventType.TOOL_CALL_RESULT]
    assert len(tool_results) >= 1
    assert "timed out" in tool_results[0].content.lower()


@pytest.mark.asyncio
async def test_tool_execute_timeout_zero_disables_agent_tool_timeout(monkeypatch):
    """execute_timeout=0 表示不套 Agent 单工具超时"""
    no_timeout_tool = NoTimeoutTool()
    agent, llm = _make_agent(tools=[no_timeout_tool])
    agent.tool_timeout = 1

    tool_call = ToolCall(
        id="tc-no-timeout",
        type="function",
        function=FunctionCall(name="no_timeout_tool", arguments={"msg": "hi"}),
    )
    response1 = LLMResponse(content="", tool_calls=[tool_call], finish_reason="tool_calls")
    response2 = LLMResponse(content="OK", tool_calls=[], finish_reason="stop")

    call_count = 0

    async def _fake_stream(messages, tools, on_content=None, on_thinking=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return response1
        if on_content:
            await on_content("OK")
        return response2

    async def _fail_wait_for(awaitable, timeout):
        awaitable.close()
        raise AssertionError("execute_timeout=0 should not call asyncio.wait_for")

    llm.generate_stream = _fake_stream
    monkeypatch.setattr(asyncio, "wait_for", _fail_wait_for)

    events = []
    async for event in agent.run_agui("thread-1", "run-no-timeout"):
        events.append(event)

    tool_results = [e for e in events if e.type == EventType.TOOL_CALL_RESULT]
    assert len(tool_results) >= 1
    assert tool_results[0].content == "no-timeout-done"
