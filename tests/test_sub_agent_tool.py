import asyncio
from time import perf_counter

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from src.agent.agent import Agent
from src.agent.schema import FunctionCall, LLMResponse, Message, ToolCall
from src.agent.schema.agui_events import (
    RunFinishedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from src.agent.tools.base import ToolResult, ToolRuntimeContext
from src.agent.tools.sub_agent_tool import SubAgentTool
from src.agent.subagent_profiles import resolve_profile
from src.api.models.auth_user import AuthUser
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.subagent_run import SubagentRun
from src.api.services.agent_service import AgentService
from src.api.services.history_service import HistoryService
from tests.helpers import make_mock_sandbox


@pytest.mark.asyncio
async def test_sub_agent_tool_delegates_to_configured_runner():
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs)
        return ToolResult(success=True, content="child result")

    tool = SubAgentTool(runner=runner)
    context = ToolRuntimeContext(
        thread_id="session-1",
        run_id="parent-run",
        tool_call_id="tc-sub",
        tool_name="sub_agent",
    )
    tool.set_runtime_context(context)

    result = await tool.execute(
        prompt="帮我规划 vlog 节奏",
        subagent_type="plan",
        description="vlog 剪辑",
    )

    assert result.success is True
    assert result.content == "child result"
    assert calls[0]["prompt"] == "帮我规划 vlog 节奏"
    assert calls[0]["subagent_type"] == "plan"
    assert calls[0]["description"] == "vlog 剪辑"
    assert calls[0]["context"] is context


@pytest.mark.asyncio
async def test_sub_agent_tool_requires_prompt():
    result = await SubAgentTool().execute(prompt="")

    assert result.success is False
    assert result.error == "prompt is required"


@pytest.mark.asyncio
async def test_sub_agent_tool_requires_runtime_runner_and_context():
    no_runner = SubAgentTool()
    no_runner.set_runtime_context(
        ToolRuntimeContext(
            thread_id="session-1",
            run_id="parent-run",
            tool_call_id="tc-sub",
            tool_name="sub_agent",
        )
    )

    result = await no_runner.execute(prompt="task")
    assert result.success is False
    assert result.error == "sub_agent runner is not configured"

    async def runner(**kwargs):
        return ToolResult(success=True, content="unused")

    no_context = SubAgentTool(runner=runner)
    result = await no_context.execute(prompt="task")
    assert result.success is False
    assert result.error == "sub_agent runtime context is unavailable"


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_stream(self, messages, tools, on_content=None, on_thinking=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="tc-sub",
                        type="function",
                        function=FunctionCall(
                            name="sub_agent",
                            arguments={
                                "prompt": "review this",
                                "subagent_type": "review",
                                "description": "code review",
                            },
                        ),
                    )
                ],
            )
        return LLMResponse(content="done", finish_reason="stop", tool_calls=[])


class _ParallelFakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_stream(self, messages, tools, on_content=None, on_thinking=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="tc-sub-1",
                        type="function",
                        function=FunctionCall(
                            name="sub_agent",
                            arguments={
                                "prompt": "research part one",
                                "subagent_type": "research",
                                "description": "part one",
                            },
                        ),
                    ),
                    ToolCall(
                        id="tc-sub-2",
                        type="function",
                        function=FunctionCall(
                            name="sub_agent",
                            arguments={
                                "prompt": "research part two",
                                "subagent_type": "research",
                                "description": "part two",
                            },
                        ),
                    ),
                ],
            )
        return LLMResponse(content="done", finish_reason="stop", tool_calls=[])


@pytest.mark.asyncio
async def test_agent_supplies_runtime_context_to_sub_agent_tool():
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs)
        return ToolResult(success=True, content="child review")

    agent = Agent(
        llm_client=_FakeLLM(),
        system_prompt="system",
        tools=[SubAgentTool(runner=runner)],
        max_steps=3,
    )
    agent.add_user_message("start")

    events = [
        event
        async for event in agent.run_agui(
            thread_id="session-1",
            run_id="parent-run",
        )
    ]

    assert calls[0]["prompt"] == "review this"
    assert calls[0]["subagent_type"] == "review"
    assert calls[0]["context"].thread_id == "session-1"
    assert calls[0]["context"].run_id == "parent-run"
    assert calls[0]["context"].tool_call_id == "tc-sub"
    assert calls[0]["context"].tool_name == "sub_agent"
    assert events[-1].type.value == "RUN_FINISHED"


@pytest.mark.asyncio
async def test_agent_executes_same_step_sub_agents_in_parallel():
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.2)
        tool_call_id = kwargs["context"].tool_call_id
        return ToolResult(success=True, content=f"child result for {tool_call_id}")

    agent = Agent(
        llm_client=_ParallelFakeLLM(),
        system_prompt="system",
        tools=[SubAgentTool(runner=runner)],
        max_steps=3,
        subagent_max_parallel=2,
    )
    agent.add_user_message("start")

    started_at = perf_counter()
    events = [
        event
        async for event in agent.run_agui(
            thread_id="session-1",
            run_id="parent-run",
        )
    ]
    elapsed = perf_counter() - started_at

    assert elapsed < 0.35
    assert [call["context"].tool_call_id for call in calls] == ["tc-sub-1", "tc-sub-2"]
    tool_messages = [msg for msg in agent.messages if msg.role == "tool" and msg.name == "sub_agent"]
    assert [msg.tool_call_id for msg in tool_messages] == ["tc-sub-1", "tc-sub-2"]
    assert "child result for tc-sub-1" in tool_messages[0].content
    assert "child result for tc-sub-2" in tool_messages[1].content
    assert events[-1].type.value == "RUN_FINISHED"


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine, testing_session_local


class _ChildAgent:
    def __init__(self) -> None:
        self.messages = [Message(role="system", content="child system")]
        self.last_llm_usage = None
        self._llm_call_hook = None

    def set_llm_call_hook(self, hook):
        self._llm_call_hook = hook

    def add_user_message(self, content):
        self.messages.append(Message(role="user", content=content))

    async def run_agui(self, *, thread_id, run_id, cancel_token=None):
        yield TextMessageStartEvent(messageId="child-msg", role="assistant")
        yield TextMessageContentEvent(messageId="child-msg", delta="child answer")
        yield TextMessageEndEvent(messageId="child-msg")
        yield RunFinishedEvent(threadId=thread_id, runId=run_id, outcome="success")


class _MaxStepsChildAgent(_ChildAgent):
    async def run_agui(self, *, thread_id, run_id, cancel_token=None):
        yield RunFinishedEvent(
            threadId=thread_id,
            runId=run_id,
            outcome="interrupt",
            result={
                "reason": "max_steps_reached",
                "finalResponse": "child hit max steps",
            },
        )


@pytest.mark.asyncio
async def test_agent_service_sub_agent_creates_child_round_and_graph_edge():
    engine, session_factory = _make_db()
    db = session_factory()
    try:
        child_tool_excludes = []
        child_prompt_overrides = []
        db.add(
            AuthUser(
                user_id="u1",
                username="u1",
                auth_type="simple",
                password_hash="hash",
                enabled=True,
            )
        )
        db.add(Session(id="s1", user_id="u1", status="active"))
        db.add(Round(id="parent-run", session_id="s1", user_message="parent", status="running"))
        db.commit()

        service = AgentService(
            sandbox=make_mock_sandbox(),
            history_service=HistoryService(session_factory),
            session_id="s1",
            user_id="u1",
        )
        fake_registry = type(
            "Registry",
            (),
            {"get_subagent_default": lambda self: type("Model", (), {"id": "sub-model"})()},
        )()

        async def fake_initialize_agent(self):
            child_tool_excludes.append(set(self.tool_exclude))
            child_prompt_overrides.append(self.system_prompt_override)
            self.agent = _ChildAgent()

        with (
            patch("src.api.services.agent_service.get_model_registry", return_value=fake_registry),
            patch.object(AgentService, "initialize_agent", fake_initialize_agent),
        ):
            result = await service._run_subagent_invocation(
                prompt="review this",
                subagent_type="review",
                description="code review",
                context=ToolRuntimeContext(
                    thread_id="s1",
                    run_id="parent-run",
                    tool_call_id="tc-sub",
                    tool_name="sub_agent",
                ),
            )

        assert result.success is True
        assert "child answer" in result.content
        assert "child_run_id:" in result.content

        child_round = (
            db.query(Round)
            .filter(Round.session_id == "s1", Round.parent_run_id == "parent-run")
            .one()
        )
        edge = db.query(SubagentRun).filter(SubagentRun.child_run_id == child_round.id).one()

        assert child_round.status == "completed"
        assert child_round.final_response == "child answer"
        assert edge.parent_run_id == "parent-run"
        assert edge.tool_call_id == "tc-sub"
        assert edge.agent_type == "review"
        assert edge.model_id == "sub-model"
        assert edge.status == SubagentRun.COMPLETED
        assert edge.output == "child answer"
        # subagent_type="review" 映射到 research profile，子 agent 工具被裁剪
        research_exclude = set(resolve_profile("review").tool_exclude)
        assert research_exclude in child_tool_excludes
        assert {"AskUserQuestionTool", "SubAgentTool"}.issubset(research_exclude)
        assert {"SandboxWriteTool", "SandboxEditTool", "ManageCronTool"}.issubset(research_exclude)
        # 子 agent 使用 profile 精简 system prompt，而非父记忆
        assert child_prompt_overrides == [resolve_profile("review").system_prompt]
    finally:
        service.close()
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.mark.asyncio
async def test_agent_service_sub_agent_maps_child_max_steps_to_failed_edge():
    engine, session_factory = _make_db()
    db = session_factory()
    try:
        db.add(
            AuthUser(
                user_id="u1",
                username="u1",
                auth_type="simple",
                password_hash="hash",
                enabled=True,
            )
        )
        db.add(Session(id="s1", user_id="u1", status="active"))
        db.add(Round(id="parent-run", session_id="s1", user_message="parent", status="running"))
        db.commit()

        service = AgentService(
            sandbox=make_mock_sandbox(),
            history_service=HistoryService(session_factory),
            session_id="s1",
            user_id="u1",
        )
        fake_registry = type(
            "Registry",
            (),
            {"get_subagent_default": lambda self: type("Model", (), {"id": "sub-model"})()},
        )()

        async def fake_initialize_agent(self):
            self.agent = _MaxStepsChildAgent()

        with (
            patch("src.api.services.agent_service.get_model_registry", return_value=fake_registry),
            patch.object(AgentService, "initialize_agent", fake_initialize_agent),
        ):
            result = await service._run_subagent_invocation(
                prompt="research until max steps",
                subagent_type="research",
                description="long research",
                context=ToolRuntimeContext(
                    thread_id="s1",
                    run_id="parent-run",
                    tool_call_id="tc-sub",
                    tool_name="sub_agent",
                ),
            )

        assert result.success is False
        assert result.error == "child hit max steps"

        child_round = (
            db.query(Round)
            .filter(Round.session_id == "s1", Round.parent_run_id == "parent-run")
            .one()
        )
        edge = db.query(SubagentRun).filter(SubagentRun.child_run_id == child_round.id).one()

        assert child_round.status == "max_steps_reached"
        assert child_round.final_response == "child hit max steps"
        assert edge.status == SubagentRun.FAILED
        assert edge.error == "child hit max steps"
        assert "unsupported status" not in (edge.error or "")
    finally:
        service.close()
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
