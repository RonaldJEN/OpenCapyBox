"""AskUserQuestionTool + Interrupt-Resume 测试"""
import pytest
import json

from src.agent.agent import Agent
from src.agent.tools.ask_user_tool import AskUserQuestionTool, ASK_USER_TOOL_NAME
from src.agent.schema import Message, LLMResponse, ToolCall, FunctionCall
from src.agent.schema.agui_events import EventType, InterruptDetails
from tests.helpers import MockLLMClient, MockTool, make_agent, collect_agui_events


# ============== AskUserQuestionTool 基础测试 ==============


class TestAskUserQuestionTool:
    """AskUserQuestionTool 自身的测试"""

    def test_tool_name(self):
        tool = AskUserQuestionTool()
        assert tool.name == "ask_user"
        assert tool.name == ASK_USER_TOOL_NAME

    def test_schema_has_questions(self):
        tool = AskUserQuestionTool()
        schema = tool.parameters
        assert "questions" in schema["properties"]
        assert schema["required"] == ["questions"]

    def test_schema_questions_structure(self):
        tool = AskUserQuestionTool()
        q_schema = tool.parameters["properties"]["questions"]
        assert q_schema["type"] == "array"
        assert q_schema["minItems"] == 1
        assert q_schema["maxItems"] == 4
        item_props = q_schema["items"]["properties"]
        assert "question" in item_props
        assert "header" in item_props
        assert "options" in item_props
        assert "multiSelect" in item_props

    def test_openai_schema(self):
        tool = AskUserQuestionTool()
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "ask_user"

    @pytest.mark.asyncio
    async def test_execute_returns_error(self):
        """execute() 不应被正常调用，返回错误"""
        tool = AskUserQuestionTool()
        result = await tool.execute(questions=[])
        assert not result.success
        assert "intercepted" in result.error


# ============== Agent Interrupt 测试 ==============


def _make_ask_user_response():
    """创建一个 LLM 回复，其中包含 ask_user 工具调用"""
    return LLMResponse(
        content="",
        thinking=None,
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id="tc_ask_1",
                type="function",
                function=FunctionCall(
                    name="ask_user",
                    arguments={
                        "questions": [
                            {
                                "question": "Which database should we use?",
                                "header": "Database",
                                "options": [
                                    {"label": "PostgreSQL", "description": "Full SQL"},
                                    {"label": "SQLite", "description": "Lightweight"},
                                ],
                            }
                        ]
                    },
                ),
            )
        ],
    )


class TestAgentInterrupt:
    """测试 Agent 在遇到 ask_user 时正确中断"""

    @pytest.mark.asyncio
    async def test_interrupt_saves_pending_state(self, tmp_path):
        """中断后 agent._pending_interrupt 应有正确状态"""
        llm = MockLLMClient()
        llm.responses = [_make_ask_user_response()]

        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help me choose")

        await collect_agui_events(agent)

        assert agent._pending_interrupt is not None
        assert "interrupt_id" in agent._pending_interrupt
        assert agent._pending_interrupt["tool_call_id"] == "tc_ask_1"
        assert len(agent._pending_interrupt["questions"]) == 1

    @pytest.mark.asyncio
    async def test_interrupt_has_placeholder_tool_result(self, tmp_path):
        """中断后消息历史应有占位的 tool_result"""
        llm = MockLLMClient()
        llm.responses = [_make_ask_user_response()]

        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help me choose")

        await collect_agui_events(agent)

        tool_msgs = [m for m in agent.messages if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == "[Awaiting user response]"
        assert tool_msgs[0].tool_call_id == "tc_ask_1"

    @pytest.mark.asyncio
    async def test_tool_call_events_emitted_before_interaction(self, tmp_path):
        """等待交互前发出调用边界，但不把内部占位投影到 wire。"""
        llm = MockLLMClient()
        llm.responses = [_make_ask_user_response()]

        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help")

        events, event_types = await collect_agui_events(agent)

        assert "TOOL_CALL_START" in event_types
        assert "TOOL_CALL_END" in event_types
        assert "TOOL_CALL_RESULT" not in event_types

    @pytest.mark.asyncio
    async def test_interaction_parks_without_run_finished(self, tmp_path):
        llm = MockLLMClient()
        llm.responses = [_make_ask_user_response()]
        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help")

        events, event_types = await collect_agui_events(agent)

        assert "RUN_FINISHED" not in event_types
        assert not any(
            event.type.value == "TOOL_CALL_RESULT"
            and event.content == "[Awaiting user response]"
            for event in events
        )
        requested = [
            event
            for event in events
            if event.type.value == "CUSTOM" and event.name == "interaction_requested"
        ]
        assert len(requested) == 1
        assert requested[0].value["runId"] == "test_run"
        assert requested[0].value["toolCallId"] == "tc_ask_1"

    @pytest.mark.asyncio
    async def test_continuation_suppresses_second_run_started(self, tmp_path):
        llm = MockLLMClient()
        llm.responses = [
            _make_ask_user_response(),
            LLMResponse(
                content="Use PostgreSQL",
                thinking=None,
                finish_reason="stop",
                tool_calls=[],
            ),
        ]
        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help")

        first_events, _ = await collect_agui_events(agent)
        interaction_id = next(
            event.value["interactionId"]
            for event in first_events
            if event.type.value == "CUSTOM" and event.name == "interaction_requested"
        )
        agent.resume_from_interrupt(
            interaction_id,
            {"Which database should we use?": "PostgreSQL"},
        )

        continuation = []
        async for event in agent.run_agui(
            thread_id="test_thread",
            run_id="test_run",
            emit_run_started=False,
            initial_step=1,
        ):
            continuation.append(event)

        assert not any(event.type.value == "RUN_STARTED" for event in continuation)
        assert next(
            event.step_name
            for event in continuation
            if event.type.value == "STEP_STARTED"
        ) == "step_2"
        assert any(
            event.type.value == "RUN_FINISHED" and event.outcome == "success"
            for event in continuation
        )

    @pytest.mark.asyncio
    async def test_remaining_tool_calls_skipped(self, tmp_path):
        """ask_user 之后的其他 tool_call 应被标记为 skipped"""
        response = LLMResponse(
            content="",
            thinking=None,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="tc_mock_1",
                    type="function",
                    function=FunctionCall(name="mock_tool", arguments={"input": "test"}),
                ),
                ToolCall(
                    id="tc_ask_1",
                    type="function",
                    function=FunctionCall(
                        name="ask_user",
                        arguments={"questions": [{"question": "Q?", "header": "Q", "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]}]},
                    ),
                ),
                ToolCall(
                    id="tc_mock_2",
                    type="function",
                    function=FunctionCall(name="mock_tool", arguments={"input": "test2"}),
                ),
            ],
        )

        llm = MockLLMClient()
        llm.responses = [response]

        agent = make_agent(
            tmp_path, llm=llm,
            tools=[MockTool(), AskUserQuestionTool()],
        )
        agent.add_user_message("Do stuff")

        await collect_agui_events(agent)

        tool_msgs = [m for m in agent.messages if m.role == "tool"]
        # mock_tool executed, ask_user placeholder, mock_tool skipped
        assert len(tool_msgs) == 3
        assert tool_msgs[0].name == "mock_tool"  # executed normally
        assert tool_msgs[1].content == "[Awaiting user response]"  # ask_user placeholder
        assert tool_msgs[2].content == "[Skipped: user question pending]"  # skipped


# ============== Agent Resume 测试 ==============


class TestAgentResume:
    """测试从中断中恢复"""

    @pytest.mark.asyncio
    async def test_resume_replaces_placeholder(self, tmp_path):
        """resume 应将占位内容替换为用户答案"""
        llm = MockLLMClient()
        llm.responses = [_make_ask_user_response()]

        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help")

        await collect_agui_events(agent)
        interrupt_id = agent._pending_interrupt["interrupt_id"]

        # Resume
        agent.resume_from_interrupt(interrupt_id, {
            "Which database should we use?": "PostgreSQL",
        })

        # 验证占位消息已被替换
        tool_msgs = [m for m in agent.messages if m.role == "tool" and m.tool_call_id == "tc_ask_1"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content.startswith("User answered:\n")
        assert "PostgreSQL" in tool_msgs[0].content
        assert "Which database" in tool_msgs[0].content

        # 验证中断状态已清除
        assert agent._pending_interrupt is None

    def test_replace_interrupt_tool_result_accepts_cold_placeholder(self, tmp_path):
        """冷恢复历史重建后的 resolved 占位也可被统一 helper 替换。"""
        agent = make_agent(tmp_path)
        agent.messages.append(
            Message(
                role="tool",
                content="[Interrupt resolved in subsequent round]",
                tool_call_id="tc_ask_1",
                name="ask_user",
            )
        )

        replaced = agent.replace_interrupt_tool_result("tc_ask_1", "User answered:\n- Q?: A")

        assert replaced is True
        tool_msg = next(m for m in agent.messages if m.role == "tool" and m.tool_call_id == "tc_ask_1")
        assert tool_msg.content == "User answered:\n- Q?: A"

    def test_format_interrupt_tool_result_is_shared_resume_format(self):
        """结构化 resolution 持久化应复用热 resume 的 tool result 格式。"""
        definitions = [{"question": "Z?"}, {"question": "A?"}]
        content = Agent.format_interrupt_tool_result(
            {"A?": "A2", "Z?": "A1"},
            definitions,
        )
        pre_normalized = Agent.format_interrupt_tool_result(
            {"Z?": "A1", "A?": "A2"}
        )

        assert content == "User answered:\n- Z?: A1\n- A?: A2"
        assert pre_normalized == content

    @pytest.mark.asyncio
    async def test_resume_wrong_id_raises(self, tmp_path):
        """使用错误的 interrupt_id 应抛出 ValueError"""
        llm = MockLLMClient()
        llm.responses = [_make_ask_user_response()]

        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help")

        await collect_agui_events(agent)

        with pytest.raises(ValueError, match="mismatch"):
            agent.resume_from_interrupt("wrong-id", {"Q": "A"})

    @pytest.mark.asyncio
    async def test_resume_no_pending_raises(self, tmp_path):
        """没有待处理中断时 resume 应抛出 ValueError"""
        agent = make_agent(tmp_path)
        with pytest.raises(ValueError, match="No pending interrupt"):
            agent.resume_from_interrupt("any-id", {})

    @pytest.mark.asyncio
    async def test_resume_then_continue(self, tmp_path):
        """resume 后 agent 应能继续 run_agui"""
        llm = MockLLMClient()
        # 第一次调用：ask_user
        llm.responses = [_make_ask_user_response()]

        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help")

        await collect_agui_events(agent)
        interrupt_id = agent._pending_interrupt["interrupt_id"]

        # Resume
        agent.resume_from_interrupt(interrupt_id, {"Which database should we use?": "PostgreSQL"})

        # 第二次调用：正常完成
        llm.responses = [LLMResponse(content="Great, using PostgreSQL!", finish_reason="stop")]

        events, event_types = await collect_agui_events(agent, run_id="resume_run")
        assert "RUN_STARTED" in event_types
        assert "RUN_FINISHED" in event_types

        run_finished = [e for e in events if e.type.value == "RUN_FINISHED"][0]
        assert run_finished.outcome == "success"


# ============== Clear Pending Interrupt 测试 ==============


class TestClearPendingInterrupt:
    """测试用户发送新消息时清除中断状态"""

    @pytest.mark.asyncio
    async def test_clear_replaces_placeholder(self, tmp_path):
        """clear_pending_interrupt 应替换占位内容"""
        llm = MockLLMClient()
        llm.responses = [_make_ask_user_response()]

        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help")

        await collect_agui_events(agent)
        assert agent._pending_interrupt is not None

        agent.clear_pending_interrupt()

        assert agent._pending_interrupt is None
        tool_msgs = [m for m in agent.messages if m.role == "tool" and m.tool_call_id == "tc_ask_1"]
        assert "chose not to answer" in tool_msgs[0].content

    def test_clear_when_no_interrupt_is_noop(self, tmp_path):
        """没有中断时 clear 是无操作"""
        agent = make_agent(tmp_path)
        agent.clear_pending_interrupt()  # 不应抛出异常
        assert agent._pending_interrupt is None


# ============== Schema 测试 ==============


class TestResumeRequestSchema:
    """测试 ResumeRequest schema"""

    def test_valid_resume_request(self):
        from src.api.schemas.chat import ResumeRequest
        req = ResumeRequest(
            interrupt_id="test-id",
            answers={"Q1?": "A1", "Q2?": "A2"},
        )
        assert req.interrupt_id == "test-id"
        assert len(req.answers) == 2

    def test_missing_interrupt_id_fails(self):
        from src.api.schemas.chat import ResumeRequest
        with pytest.raises(Exception):
            ResumeRequest(answers={"Q": "A"})

    def test_missing_answers_fails(self):
        from src.api.schemas.chat import ResumeRequest
        with pytest.raises(Exception):
            ResumeRequest(interrupt_id="test")


# ============== Bug-fix 回归测试 ==============


class TestAnswerKeyIsQuestionText:
    """回归：resume answers 的 key 应使用问题全文（非 header 短标签）"""

    @pytest.mark.asyncio
    async def test_resume_answer_keyed_by_question(self, tmp_path):
        """确保 resume_from_interrupt 按 question text 索引答案"""
        llm = MockLLMClient()
        llm.responses = [_make_ask_user_response()]

        agent = make_agent(tmp_path, llm=llm, tools=[AskUserQuestionTool()])
        agent.add_user_message("Help")
        await collect_agui_events(agent)

        iid = agent._pending_interrupt["interrupt_id"]
        # 用问题全文作 key（和前端 QuestionCard 保持一致）
        agent.resume_from_interrupt(iid, {
            "Which database should we use?": "SQLite",
        })

        tool_msg = next(m for m in agent.messages if m.role == "tool" and m.tool_call_id == "tc_ask_1")
        assert "SQLite" in tool_msg.content
        assert "Which database should we use?" in tool_msg.content
