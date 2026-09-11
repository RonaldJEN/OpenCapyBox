"""Child identity reaches parent direct/replay UI without changing run ownership."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.agent.agent import Agent
from src.agent.schema import FunctionCall, LLMResponse, ToolCall
from src.agent.schema.agui_events import (
    RunFinishedEvent, TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent,
)
from src.agent.tools.base import ToolRuntimeContext
from src.agent.tools.sub_agent_tool import SubAgentTool
from src.api.models.auth_user import AuthUser
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.subagent_run import SubagentRun
from src.api.services.agent_service import AgentService
from src.api.services.agui_event_bus import AguiEventBus
from src.api.services.history_service import HistoryService
from tests.helpers import make_mock_sandbox, make_test_client
from tests.test_sub_agent_tool import _ChildAgent, _make_db


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(AguiEventBus, "_terminal_runs", set())
    monkeypatch.setattr(AguiEventBus, "_stream_buffers", {})
    monkeypatch.setattr(AguiEventBus, "_subscribers", {})
    engine, factory = _make_db()
    db = factory()
    db.add(AuthUser(user_id="u1", username="u1", auth_type="simple", password_hash="hash", enabled=True))
    db.add(Session(id="s1", user_id="u1", status="active"))
    db.add(Round(id="parent", session_id="s1", user_message="parent", status="running"))
    db.commit()
    service = AgentService(make_mock_sandbox(), HistoryService(factory), "s1", "u1")
    service._post_round_tasks = AsyncMock()
    started, finish = asyncio.Event(), asyncio.Event()

    class Child(_ChildAgent):
        async def run_agui(self, *, thread_id, run_id, cancel_token=None):
            yield TextMessageStartEvent(messageId="child-message", role="assistant")
            yield TextMessageContentEvent(messageId="child-message", delta="child answer")
            started.set()
            await finish.wait()
            yield TextMessageEndEvent(messageId="child-message")
            yield RunFinishedEvent(threadId=thread_id, runId=run_id, outcome="success")

    class Parent(_ChildAgent):
        result = None

        async def run_agui(self, *, thread_id, run_id, cancel_token=None):
            self.result = await service._run_subagent_invocation(
                prompt="review the deck", subagent_type="review", description="内容审核",
                context=ToolRuntimeContext(thread_id, run_id, "tc-child", "sub_agent", cancel_token),
            )
            yield TextMessageStartEvent(messageId="parent-message", role="assistant")
            yield TextMessageContentEvent(messageId="parent-message", delta="parent summary")
            yield TextMessageEndEvent(messageId="parent-message")
            yield RunFinishedEvent(threadId=thread_id, runId=run_id, outcome="success")

    async def initialize(child_service):
        child_service.agent = Child()
        child_service._post_round_tasks = AsyncMock()

    service.agent = Parent()
    registry = SimpleNamespace(get_subagent_default=lambda: SimpleNamespace(id="child-model"))
    with patch.object(AgentService, "initialize_agent", initialize), patch(
        "src.api.services.agent_service.get_model_registry", return_value=registry,
    ):
        yield service, factory, started, finish
    service.close()
    db.close()
    engine.dispose()


def payload(event):
    return event.model_dump(by_alias=True, exclude_none=True) if hasattr(event, "model_dump") else event


@pytest.mark.asyncio
async def test_parent_stream_live_subscribe_and_snapshots_share_child_identity(runtime):
    service, factory, started, finish = runtime
    direct = []

    async def consume_direct():
        async for event in service._run_round_stream("parent", "parent"):
            data = payload(event)
            direct.append(data)
            if data.get("name") == "subagent_run_updated" and data["value"]["status"] == "running":
                assert data["value"]["child_run_id"]
                assert not finish.is_set()
                await asyncio.wait_for(started.wait(), 2)
                finish.set()

    async def consume_subscribe():
        return [event async for event in AguiEventBus(factory).subscribe("parent")]

    subscribed, _ = await asyncio.wait_for(asyncio.gather(consume_subscribe(), consume_direct()), 5)
    updates = [event for event in direct if event.get("name") == "subagent_run_updated"]
    assert [event["value"]["status"] for event in updates] == ["requested", "running", "completed"]
    assert [event for event in subscribed if event.get("name") == "subagent_run_updated"] == updates
    assert all(event.get("sequence") for event in updates)
    assert service.agent.result.success is True
    assert service._subagent_activities == {}

    from src.api.routes import chat, sessions
    db = factory()
    try:
        history_client = make_test_client(sessions.router, "/sessions", user="u1", db=db)
        history = history_client.get("/sessions/s1/history/v2").json()
        assert [item["round_id"] for item in history["rounds"]] == ["parent"]
        parent = history["rounds"][0]
        assert parent["subagent_tasks"] == [updates[-1]["value"]]
        assert parent["final_response"] == "parent summary"
        child_id = parent["subagent_tasks"][0]["child_run_id"]
        client = make_test_client(chat.router, "/chat", user="u1", db=db)
        assert client.get("/chat/s1/round/parent/snapshot").json() == parent
        child = client.get(f"/chat/s1/round/{child_id}/snapshot").json()
        assert child["final_response"] == "child answer"
        assert child["parent_run_id"] == "parent"
        assert child["subagent_tasks"] == []
        assert child["assistant_messages"][0]["content"] == "child answer"
        assert child["last_event_sequence"] > 0
        other_user = make_test_client(chat.router, "/chat", user="other", db=db)
        assert other_user.get(f"/chat/s1/round/{child_id}/snapshot").status_code == 404
        db.add(Session(id="other-session", user_id="u1", status="active"))
        db.commit()
        assert client.get(f"/chat/other-session/round/{child_id}/snapshot").status_code == 404
    finally:
        db.close()


class _RetrySubagentLLM:
    """One malformed call must stay failed before the model retries correctly."""

    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools, on_content=None, on_thinking=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            arguments = {"prompt": "review the deck", "sub_agent_type": "review"}
            call_id = "tc-invalid"
        elif self.calls == 2:
            arguments = {"prompt": "review the deck", "subagent_type": "review"}
            call_id = "tc-valid"
        else:
            return LLMResponse(content="parent summary", finish_reason="stop", tool_calls=[])
        return LLMResponse(content="", finish_reason="tool_calls", tool_calls=[ToolCall(
            id=call_id, type="function", function=FunctionCall(name="sub_agent", arguments=arguments),
        )])


@pytest.mark.asyncio
async def test_failed_subagent_call_has_no_child_then_retry_uses_graph_truth(runtime):
    service, factory, _started, finish = runtime
    finish.set()
    service.agent = Agent(
        llm_client=_RetrySubagentLLM(), system_prompt="parent",
        tools=[SubAgentTool(runner=service._run_subagent_invocation)], max_steps=4,
    )

    direct = [payload(event) async for event in service._run_round_stream("parent", "parent")]
    result_by_call = {
        event["toolCallId"]: event for event in direct if event["type"] == "TOOL_CALL_RESULT"
    }
    assert result_by_call["tc-invalid"]["success"] is False
    assert "sub_agent_type" in result_by_call["tc-invalid"]["content"]
    assert "Traceback" not in result_by_call["tc-invalid"]["content"]
    assert result_by_call["tc-valid"]["success"] is True

    direct_statuses = [event["value"]["status"] for event in direct
                       if event.get("name") == "subagent_run_updated"]
    assert direct_statuses == ["requested", "running", "completed"]

    replay = await AguiEventBus(factory).replay("parent")
    replay_results = {event["toolCallId"]: event for event in replay if event["type"] == "TOOL_CALL_RESULT"}
    assert replay_results["tc-invalid"]["success"] is False
    assert replay_results["tc-valid"]["success"] is True
    assert [event["value"]["status"] for event in replay
            if event.get("name") == "subagent_run_updated"] == direct_statuses

    history = HistoryService(factory)
    try:
        snapshot = history.get_round_snapshot("s1", "parent")
        history_results = {
            result["tool_call_id"]
            for step in snapshot["steps"]
            for result in step["tool_results"]
            if result["success"] is False
        }
        assert history_results == {"tc-invalid"}
        assert [task["status"] for task in snapshot["subagent_tasks"]] == direct_statuses[-1:]
        db = factory()
        try:
            edges = db.query(SubagentRun).order_by(SubagentRun.created_at).all()
            assert [(edge.tool_call_id, edge.status, bool(edge.child_run_id)) for edge in edges] == [
                ("tc-valid", "completed", True),
            ]
        finally:
            db.close()
    finally:
        history.close()


@pytest.mark.asyncio
async def test_identity_publication_failure_keeps_child_success_and_graph_recovery(runtime):
    service, factory, started, finish = runtime
    finish.set()
    save = service.history_service.save_agui_event

    async def fail_identity(run_id, event, **kwargs):
        if getattr(event, "name", None) == "subagent_run_updated":
            raise RuntimeError("UI publication unavailable")
        return await save(run_id, event, **kwargs)

    with patch.object(
        service.history_service, "save_agui_event", side_effect=fail_identity,
    ):
        events = [payload(event) async for event in service._run_round_stream("parent", "parent")]
    assert events[-1]["type"] == "RUN_FINISHED"
    assert service.agent.result.success is True
    history = HistoryService(factory)
    try:
        parent = history.get_round_snapshot("s1", "parent")
        assert parent["subagent_tasks"][0]["status"] == "completed"
        assert parent["final_response"] == "parent summary"
    finally:
        history.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_cancel", [True, False])
async def test_parent_cancel_closes_child_wait_and_preserves_cancel_cause(runtime, user_cancel):
    service, factory, started, finish = runtime
    service.cancel_token = asyncio.Event()

    async def consume():
        return [event async for event in service._run_round_stream("parent", "parent")]

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    if user_cancel:
        service.cancel_token.set()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer, 2)
    assert service._subagent_activities == {}
    db = factory()
    try:
        edge = db.query(SubagentRun).one()
        expected = "cancelled" if user_cancel else "failed"
        assert edge.status == expected
        assert db.query(Round).filter(Round.id == edge.child_run_id).one().status == expected
        assert db.query(Round).filter(Round.id == "parent").one().status == expected
    finally:
        db.close()


@pytest.mark.asyncio
async def test_identity_update_cannot_write_after_parent_terminal(runtime):
    from src.api.services.run_completion_service import RunCompletionService

    service, factory, started, finish = runtime
    save = service.history_service.save_agui_event

    async def close_before_identity_commit(run_id, event, **kwargs):
        if getattr(event, "name", None) == "subagent_run_updated" and event.value["status"] == "running":
            with factory() as db:
                RunCompletionService(db).complete_sync(run_id="parent", status="cancelled", final_response="Cancelled")
        return await save(run_id, event, **kwargs)

    with patch.object(service.history_service, "save_agui_event", side_effect=close_before_identity_commit):
        await asyncio.wait_for(_consume(service._run_round_stream("parent", "parent")), 3)
    replay = await AguiEventBus(factory).replay("parent")
    assert [item["value"]["status"] for item in replay if item.get("name") == "subagent_run_updated"] == ["requested"]
    assert replay[-1]["type"] == "RUN_FINISHED"
    assert service._subagent_activities == {}


async def _consume(iterator):
    return [event async for event in iterator]


@pytest.mark.asyncio
async def test_identity_updates_validate_but_do_not_complete_continuation(runtime):
    from src.api.services.agent_interaction_service import AgentInteractionService

    service, factory, started, finish = runtime
    finish.set()
    with factory() as db:
        AgentInteractionService.create_pending(
            db, interaction_id="ask", session_id="s1", round_id="parent", kind="user_input",
            tool_call_id="tc-ask", request_payload={"payload": {"questions": [{"question": "Continue?"}]}},
        )
        AgentInteractionService.answer_pending(db, session_id="s1", interaction_id="ask", answers={"Continue?": "Yes"})
    with patch.object(service.history_service, "save_agui_event", wraps=service.history_service.save_agui_event) as save:
        await asyncio.wait_for(_consume(service._run_round_stream(
            "parent", "parent", is_continuation=True, interaction_id="ask",
            interaction_tool_call_id="tc-ask", interaction_tool_result_content="Yes", interaction_kind="user_input",
        )), 3)
    task_writes = [call for call in save.await_args_list if getattr(call.args[1], "name", None) == "subagent_run_updated"]
    assert len(task_writes) == 3
    assert all(call.kwargs["continuation_fence"].transition == "validate" for call in task_writes)
    assert service.agent.result.success is True
