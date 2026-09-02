"""Tests for turn contracts, binding, projection and repair boundaries."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.agui_event import AGUIEventLog
from src.api.models.auth_user import AuthUser
from src.api.models.channel_session_binding import ChannelSessionBinding
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.agent.schema.agui_events import RunStartedEvent, TextMessageContentEvent, TextMessageEndEvent
from src.api.schemas.chat import FileContentBlock, FileObject, SendMessageRequest, TextContentBlock
from src.api.schemas.turn import (
    ChannelMessageReplyRoute,
    ChannelReceiveResult,
    ChannelStateSnapshot,
    DeliveryIntent,
    DeliveryReceipt,
    LiveUpdate,
    MessageBody,
    MessageOrigin,
    MessageRelation,
    MessageTarget,
    NormalizedInboundTurn,
    NormalizedResumeTurn,
    RunHandle,
    WebReplyRoute,
)
from src.api.services.agent_service import PreparedAgentRun
from src.api.services.agui_event_bus import AguiEventBus
from src.api.services.channel_binding_service import ChannelBindingService
from src.api.services.channel_projection import ChannelProjection
from src.api.services.delivery_service import DeliveryService
from src.api.services.run_completion_service import RunCompletionService
from src.api.services.terminal_repair_service import TerminalRepairService
from src.api.services.turn_orchestrator import TurnOrchestrator
from src.api.services.web_chat_adapter import WebChatAdapter
from src.api.utils.timezone import now_naive


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _add_user_session(db, *, user_id: str = "u1", enabled: bool = True, session_id: str = "s1") -> None:
    db.add(
        AuthUser(
            user_id=user_id,
            username=user_id,
            auth_type="simple",
            password_hash="hash",
            enabled=enabled,
        )
    )
    db.add(Session(id=session_id, user_id=user_id, status="active"))
    db.commit()


def test_reply_route_discriminator_accepts_web_route():
    turn = NormalizedInboundTurn(
        channel="web",
        user_id="u1",
        peer_kind="web",
        peer_id="s1",
        content=[TextContentBlock(type="text", text="hello")],
        reply_route={"kind": "web_sse", "session_id": "s1"},
    )

    assert isinstance(turn.reply_route, WebReplyRoute)
    assert turn.reply_route.session_id == "s1"


def test_web_chat_adapter_normalizes_send_request_and_attachments():
    request = SendMessageRequest(
        content=[
            TextContentBlock(type="text", text="please read this"),
            FileContentBlock(
                type="file",
                file=FileObject(path="docs/a.txt", name="a.txt", mime_type="text/plain", size=12),
            ),
        ],
        idempotency_key="idem-1",
    )

    turn = WebChatAdapter().normalize_send(session_id="s1", user_id="u1", request=request)

    assert turn.channel == "web"
    assert turn.peer_kind == "web"
    assert turn.reply_route == WebReplyRoute(session_id="s1")
    assert turn.idempotency_key == "idem-1"
    assert [attachment.path for attachment in turn.attachments] == ["docs/a.txt"]


def test_web_chat_adapter_preserves_workspace_attachment_identity():
    request = SendMessageRequest(
        content=[
            FileContentBlock(
                type="file",
                file=FileObject(
                    source="workspace",
                    entry_id="entry-1",
                    name="report.xlsx",
                    mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    size=42,
                ),
            ),
        ],
    )

    turn = WebChatAdapter().normalize_send(session_id="s1", user_id="u1", request=request)

    assert turn.attachments[0].path is None
    assert turn.attachments[0].raw == {
        "source": "workspace",
        "entry_id": "entry-1",
        "name": "report.xlsx",
        "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "size": 42,
    }


def test_workspace_file_uses_current_head_when_version_is_omitted():
    file = FileObject(
        source="workspace",
        entry_id="entry-1",
    )

    assert file.version_id is None


def test_workspace_file_accepts_explicit_immutable_version():
    file = FileObject(
        source="workspace",
        entry_id="entry-1",
        version_id="version-1",
    )

    assert file.version_id == "version-1"


def test_workspace_directory_preserves_explicit_entry_kind():
    file = FileObject(
        source="workspace",
        entry_id="folder-1",
        kind="directory",
    )

    assert file.kind == "directory"


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "session"},
        {"source": "workspace"},
        {"source": "workspace", "entry_id": "entry-1", "revision": 1},
        {"source": "workspace", "entry_id": "entry-1", "tree_revision": 1},
        {
            "source": "workspace",
            "entry_id": "entry-1",
            "path": "forged.txt",
        },
    ],
)
def test_file_object_rejects_incomplete_or_forged_source_identity(payload):
    with pytest.raises(ValueError):
        FileObject.model_validate(payload)


def test_channel_receive_result_contract_carries_dispatch_turn():
    target = MessageTarget(peer_kind="direct", peer_id="peer-1", account_id="bot-1")
    inbound = ChannelReceiveResult(
        disposition="accepted",
        dedupe_key="telegram:evt-1",
        ack_required=True,
        message={
            "channel": "telegram",
            "account_id": "bot-1",
            "direction": "inbound",
            "target": target,
            "body": MessageBody(text="hello"),
            "origin": MessageOrigin(raw_event_id="evt-1"),
        },
        turn={
            "channel": "telegram",
            "user_id": "u1",
            "peer_kind": "direct",
            "peer_id": "peer-1",
            "content": [{"type": "text", "text": "hello"}],
            "reply_route": {
                "kind": "channel_message",
                "channel": "telegram",
                "account_id": "bot-1",
                "peer_kind": "direct",
                "peer_id": "peer-1",
            },
        },
    )

    assert inbound.message.direction == "inbound"
    assert isinstance(inbound.turn, NormalizedInboundTurn)
    assert inbound.turn.reply_route.kind == "channel_message"
    assert inbound.dedupe_key == "telegram:evt-1"


def test_live_update_and_state_snapshot_contracts_are_non_durable():
    target = MessageTarget(peer_kind="direct", peer_id="peer-1")
    live = LiveUpdate(
        channel="telegram",
        kind="typing",
        target=target,
        relation=MessageRelation(session_id="s1", run_id="r1"),
    )
    receipt = DeliveryReceipt(
        intent_id="intent-1",
        status="skipped",
        raw={"reason": "disabled"},
    )
    state = ChannelStateSnapshot(
        channel="telegram",
        target=target,
        binding_id="binding-1",
        session_id="s1",
        active_run_id="r1",
        last_inbound_dedupe_key="telegram:evt-1",
        last_delivery_intent_id="intent-1",
        last_receipt=receipt,
    )

    assert live.durability == "best_effort"
    assert live.kind == "typing"
    assert state.last_receipt.status == "skipped"
    assert state.active_run_id == "r1"


class _FakePreparedAgentService:
    def __init__(self):
        self.cancel_token: asyncio.Event | None = None
        self.prepare_calls: list[tuple[str, dict[str, Any]]] = []
        self.run_calls: list[tuple[PreparedAgentRun, str]] = []
        self.finish_event: asyncio.Event | None = None

    async def prepare_chat_round(
        self,
        *,
        user_content,
        idempotency_key: str | None = None,
    ) -> PreparedAgentRun:
        self.prepare_calls.append((
            "chat",
            {"user_content": user_content, "idempotency_key": idempotency_key},
        ))
        return PreparedAgentRun(run_id="r-submit", user_message="hello")

    async def prepare_resume_round(
        self,
        *,
        interrupt_id: str,
        answers: dict[str, str],
    ) -> PreparedAgentRun:
        self.prepare_calls.append((
            "resume",
            {"interrupt_id": interrupt_id, "answers": answers},
        ))
        return PreparedAgentRun(
            run_id="r-resume",
            user_message="resume answer",
            parent_run_id="r-parent",
        )

    async def run_prepared_round(
        self,
        prepared: PreparedAgentRun,
        *,
        error_label: str,
    ):
        self.run_calls.append((prepared, error_label))
        yield RunStartedEvent(threadId="s1", runId=prepared.run_id)
        if self.finish_event is not None:
            await self.finish_event.wait()


@pytest.mark.asyncio
async def test_turn_orchestrator_submit_turn_prepares_round_and_event_source():
    turn = NormalizedInboundTurn(
        channel="web",
        user_id="u1",
        peer_kind="web",
        peer_id="s1",
        content=[TextContentBlock(type="text", text="hello")],
        reply_route=WebReplyRoute(session_id="s1"),
        idempotency_key="idem-1",
    )
    cancel_token = asyncio.Event()
    agent_service = _FakePreparedAgentService()
    agent_service.finish_event = asyncio.Event()
    orchestrator = TurnOrchestrator()

    execution = await orchestrator.submit_turn(
        turn,
        agent_service=agent_service,
        cancel_token=cancel_token,
    )
    first_event = await execution.event_source.__anext__()

    assert execution.task is not None
    assert orchestrator.active_runners["s1"] is execution.task
    agent_service.finish_event.set()
    events = [first_event]
    events.extend([event async for event in execution.event_source])
    await execution.task

    assert agent_service.cancel_token is cancel_token
    assert agent_service.prepare_calls == [(
        "chat",
        {"user_content": turn.content, "idempotency_key": "idem-1"},
    )]
    assert agent_service.run_calls[0][0].run_id == "r-submit"
    assert agent_service.run_calls[0][1] == "Agent執行失敗"
    assert execution.handle.round_id == "r-submit"
    assert execution.handle.root_run_id == "r-submit"
    assert execution.handle.parent_run_id is None
    assert execution.handle.reply_route == WebReplyRoute(session_id="s1")
    assert events[0].run_id == "r-submit"
    assert "s1" not in orchestrator.active_runners


@pytest.mark.asyncio
async def test_turn_orchestrator_resume_turn_preserves_parent_run_relation():
    turn = NormalizedResumeTurn(
        channel="web",
        user_id="u1",
        session_id="s1",
        interrupt_id="interrupt-1",
        answers={"Continue?": "Yes"},
        reply_route=WebReplyRoute(session_id="s1"),
    )
    cancel_token = asyncio.Event()
    agent_service = _FakePreparedAgentService()

    execution = await TurnOrchestrator().resume_turn(
        turn,
        agent_service=agent_service,
        cancel_token=cancel_token,
    )
    events = [event async for event in execution.event_source]

    assert agent_service.cancel_token is cancel_token
    assert agent_service.prepare_calls == [(
        "resume",
        {"interrupt_id": "interrupt-1", "answers": {"Continue?": "Yes"}},
    )]
    assert agent_service.run_calls[0][0].parent_run_id == "r-parent"
    assert agent_service.run_calls[0][1] == "Resume 执行失败"
    assert execution.handle.round_id == "r-resume"
    assert execution.handle.root_run_id == "r-resume"
    assert execution.handle.parent_run_id == "r-parent"
    assert events[0].run_id == "r-resume"


def test_channel_binding_lazy_create_is_idempotent(db):
    _add_user_session(db)
    service = ChannelBindingService()

    first = service.get_or_create_binding(
        db,
        user_id="u1",
        session_id="s1",
        channel="web",
        peer_kind="web",
        peer_id="s1",
        reply_route=WebReplyRoute(session_id="s1"),
    )
    second = service.get_or_create_binding(
        db,
        user_id="u1",
        session_id="s1",
        channel="web",
        peer_kind="web",
        peer_id="s1",
    )

    assert first.id == second.id
    assert first.binding_key == second.binding_key
    assert db.query(ChannelSessionBinding).count() == 1


def test_visible_web_session_filter_excludes_cron_bound_sessions(db):
    from src.api.routes.sessions import _visible_web_session_filters

    _add_user_session(db, session_id="web-session")
    db.add(Session(id="cron-session", user_id="u1", title="Cron: daily", status="active"))
    db.add(
        ChannelSessionBinding(
            user_id="u1",
            session_id="cron-session",
            channel="cron",
            peer_kind="cron",
            peer_id="daily",
            external_thread_id="run-1",
            binding_key="cron-binding",
        )
    )
    db.commit()

    visible_ids = {
        row.id
        for row in db.query(Session).filter(*_visible_web_session_filters("u1")).all()
    }

    assert visible_ids == {"web-session"}


def test_channel_binding_lazy_create_recovers_from_concurrent_insert(db):
    _add_user_session(db)
    service = ChannelBindingService()
    binding_key = service.build_binding_key(
        channel="web",
        account_id=None,
        peer_kind="web",
        peer_id="s1",
    )
    existing = ChannelSessionBinding(
        user_id="u1",
        session_id="s1",
        channel="web",
        peer_kind="web",
        peer_id="s1",
        binding_key=binding_key,
    )
    db.add(existing)
    db.commit()
    db.refresh(existing)

    real_find = service._find
    calls = 0

    def race_find(db_arg, *, user_id: str, binding_key: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return real_find(db_arg, user_id=user_id, binding_key=binding_key)

    service._find = race_find  # type: ignore[method-assign]

    resolved = service.get_or_create_binding(
        db,
        user_id="u1",
        session_id="s1",
        channel="web",
        peer_kind="web",
        peer_id="s1",
    )

    assert resolved.id == existing.id
    assert db.query(ChannelSessionBinding).count() == 1


def test_channel_binding_rejects_disabled_user(db):
    _add_user_session(db, user_id="disabled", enabled=False, session_id="s-disabled")

    with pytest.raises(HTTPException) as exc:
        ChannelBindingService().get_or_create_binding(
            db,
            user_id="disabled",
            session_id="s-disabled",
            channel="web",
            peer_kind="web",
            peer_id="s-disabled",
        )

    assert exc.value.status_code == 401


def test_channel_binding_rejects_missing_user(db):
    with pytest.raises(HTTPException) as exc:
        ChannelBindingService().get_or_create_binding(
            db,
            user_id="deleted",
            session_id="s-deleted",
            channel="web",
            peer_kind="web",
            peer_id="s-deleted",
        )

    assert exc.value.status_code == 401


def test_channel_projection_renders_terminal_final_response():
    handle = RunHandle(
        session_id="s1",
        round_id="r1",
        run_id="r1",
        root_run_id="r1",
        reply_route=ChannelMessageReplyRoute(
            channel="telegram",
            account_id="bot-1",
            peer_kind="direct",
            peer_id="peer-1",
        ),
        started_at=now_naive(),
    )

    intents = ChannelProjection().project_event(
        handle=handle,
        event={"type": "RUN_FINISHED", "sequence": 4, "result": {"finalResponse": "done"}},
    )

    assert len(intents) == 1
    assert intents[0].durability == "best_effort"
    assert intents[0].message.body.text == "done"
    assert intents[0].message.relation.round_id == "r1"


@pytest.mark.asyncio
async def test_delivery_service_is_noop_boundary():
    handle = RunHandle(
        session_id="s1",
        round_id="r1",
        run_id="r1",
        root_run_id="r1",
        reply_route=ChannelMessageReplyRoute(channel="telegram", peer_kind="direct", peer_id="peer-1"),
        started_at=now_naive(),
    )
    intent = ChannelProjection().project_event(
        handle=handle,
        event={"type": "RUN_FINISHED", "result": {"finalResponse": "done"}},
    )[0]

    receipt = await DeliveryService().deliver(intent)

    assert receipt.intent_id == intent.id
    assert receipt.platform_message_ids == []
    assert receipt.status == "skipped"
    assert receipt.raw["status"] == "noop"


@pytest.mark.asyncio
async def test_delivery_service_disabled_intent_is_skipped():
    intent = DeliveryIntent(
        channel="telegram",
        durability="disabled",
        idempotency_key="disabled-1",
        message={
            "channel": "telegram",
            "direction": "outbound",
            "target": {"peer_kind": "direct", "peer_id": "peer-1"},
            "body": {"text": "done"},
        },
    )

    receipt = await DeliveryService().deliver(intent)

    assert receipt is None


def test_projection_delivery_failures_are_outside_round_terminal_commit(db, monkeypatch):
    _add_user_session(db)
    db.add(
        Round(
            id="r-projection-isolated",
            session_id="s1",
            thread_id="s1",
            user_message="hello",
            status="running",
            created_at=now_naive(),
        )
    )
    db.commit()

    def fail_projection(*args, **kwargs):
        raise AssertionError("projection must not run inside terminal commit")

    async def fail_delivery(*args, **kwargs):
        raise AssertionError("delivery must not run inside terminal commit")

    monkeypatch.setattr(ChannelProjection, "project_event", fail_projection)
    monkeypatch.setattr(DeliveryService, "deliver", fail_delivery)

    stored = RunCompletionService(db).complete_sync(
        run_id="r-projection-isolated",
        status="completed",
        final_response="done",
        step_count=1,
    )

    round_obj = db.query(Round).filter(Round.id == "r-projection-isolated").first()
    assert stored is not None
    assert round_obj.status == "completed"
    assert round_obj.final_response == "done"


def test_terminal_repair_service_backfills_missing_terminal_event(db):
    _add_user_session(db)
    created_at = now_naive() - timedelta(minutes=5)
    db.add(
        Round(
            id="r-missing-terminal",
            session_id="s1",
            thread_id="s1",
            user_message="hello",
            final_response="done",
            step_count=1,
            status="completed",
            created_at=created_at,
            completed_at=created_at,
        )
    )
    db.add(
        AGUIEventLog(
            run_id="r-missing-terminal",
            event_type="RUN_STARTED",
            payload=json.dumps({"type": "RUN_STARTED"}),
            sequence=1,
        )
    )
    db.commit()

    report = TerminalRepairService(db).repair_terminal_runs(since_hours=1)

    terminal = (
        db.query(AGUIEventLog)
        .filter(
            AGUIEventLog.run_id == "r-missing-terminal",
            AGUIEventLog.event_type == "RUN_FINISHED",
        )
        .one()
    )
    payload = json.loads(terminal.payload)
    assert report.repaired == 1
    assert report.repaired_run_ids == ["r-missing-terminal"]
    assert terminal.sequence == 2
    assert payload["sequence"] == 2
    assert payload["result"]["finalResponse"] == "done"


@pytest.mark.asyncio
async def test_run_completion_service_fans_out_committed_terminal(db):
    from src.api.services.agui_event_bus import get_agui_event_bus
    from src.api.services.run_completion_service import RunCompletionService

    _add_user_session(db)
    run_id = "r-terminal-fanout"
    db.add(
        Round(
            id=run_id,
            session_id="s1",
            thread_id="s1",
            user_message="hello",
            status="running",
        )
    )
    db.commit()

    bus = get_agui_event_bus()
    queue: asyncio.Queue = asyncio.Queue()
    with bus.subscribers_lock:
        bus.subscribers[run_id] = [queue]

    try:
        stored = await RunCompletionService(db).complete(
            run_id=run_id,
            status="completed",
            final_response="done",
            step_count=1,
        )

        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert stored is not None
        assert received["type"] == "RUN_FINISHED"
        assert received["sequence"] == stored.sequence
        assert received["result"]["finalResponse"] == "done"
    finally:
        with bus.subscribers_lock:
            bus.subscribers.pop(run_id, None)


@pytest.mark.asyncio
async def test_event_bus_fans_out_stream_aggregate_before_end_for_late_subscriber(db):
    _add_user_session(db)
    run_id = "r-stream-late-subscriber"
    db.add(
        Round(
            id=run_id,
            session_id="s1",
            thread_id="s1",
            user_message="hello",
            status="running",
        )
    )
    db.commit()

    bus = AguiEventBus(db)
    await bus.publish(run_id, TextMessageContentEvent(messageId="msg-1", delta="hel"))

    queue: asyncio.Queue = asyncio.Queue()
    with bus.subscribers_lock:
        bus.subscribers[run_id] = [queue]

    try:
        stored = await bus.publish(run_id, TextMessageEndEvent(messageId="msg-1"))
        aggregate = await asyncio.wait_for(queue.get(), timeout=1.0)
        end = await asyncio.wait_for(queue.get(), timeout=1.0)

        assert stored is not None
        assert aggregate["type"] == "TEXT_MESSAGE_CONTENT"
        assert aggregate["delta"] == "hel"
        assert aggregate["sequence"] == 1
        assert end["type"] == "TEXT_MESSAGE_END"
        assert end["sequence"] == 2
    finally:
        with bus.subscribers_lock:
            bus.subscribers.pop(run_id, None)


@pytest.mark.asyncio
async def test_event_bus_subscribe_returns_when_last_sequence_already_reached_terminal(db):
    _add_user_session(db)
    run_id = "r-terminal-already-seen"
    db.add(
        Round(
            id=run_id,
            session_id="s1",
            thread_id="s1",
            user_message="hello",
            status="completed",
            final_response="done",
            step_count=1,
            completed_at=now_naive(),
        )
    )
    db.add(
        AGUIEventLog(
            run_id=run_id,
            event_type="RUN_FINISHED",
            payload=json.dumps({"type": "RUN_FINISHED", "sequence": 1}),
            sequence=1,
        )
    )
    db.commit()

    iterator = AguiEventBus(db).subscribe(run_id, after_sequence=1).__aiter__()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
