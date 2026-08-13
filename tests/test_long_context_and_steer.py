import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.agent.context_compaction import SUMMARY_PREFIX
from src.agent.schema import Message
from src.api.models.agui_event import AGUIEventLog
from src.api.models.context_checkpoint import ContextCheckpoint
from src.api.models.conversation_message import ConversationMessage
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.services.agent_service import AgentService
from src.api.services.context_checkpoint_service import (
    CHECKPOINT_SCHEMA_VERSION,
    ContextCheckpointService,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def add_session_round(db, *, round_id="run-1", status="running"):
    if db.query(Session).filter(Session.id == "session-1").first() is None:
        db.add(Session(id="session-1", user_id="user-1", status="active"))
    db.add(Round(
        id=round_id,
        session_id="session-1",
        thread_id="session-1",
        user_message="user",
        status=status,
    ))
    db.commit()


def replacement(run_id="run-1"):
    return [
        Message(role="user", run_id=run_id, content="latest exact user"),
        Message(
            role="user",
            content=f"{SUMMARY_PREFIX}\nhandoff",
            is_synthetic=True,
        ),
    ]


def add_authoritative_exchange(db, *, round_id="run-1"):
    add_session_round(db, round_id=round_id, status="completed")
    db.add(ConversationMessage(
        session_id="session-1",
        round_id=round_id,
        sequence=1,
        role="user",
        content="authoritative user",
        is_synthetic=False,
    ))
    for sequence, payload in [
        (1, {"type": "TEXT_MESSAGE_CONTENT", "delta": "authoritative answer"}),
        (2, {"type": "STEP_FINISHED"}),
    ]:
        db.add(AGUIEventLog(
            run_id=round_id,
            event_type=payload["type"],
            payload=json.dumps(payload),
            sequence=sequence,
        ))
    db.commit()


def make_restore_service(db):
    history = SimpleNamespace(db=db, reset_session=lambda: db.rollback())
    service = AgentService.__new__(AgentService)
    service.session_id = "session-1"
    service.history_service = history
    service._active_checkpoint_id = None
    service._active_checkpoint_sha256 = None
    return service


def use_checkpoint_history_strategy(monkeypatch):
    monkeypatch.setattr(
        "src.api.config.get_settings",
        lambda: SimpleNamespace(
            agent_history_strategy="checkpoint_v1",
            agent_max_history_messages=120,
        ),
    )


def test_v4_checkpoint_accepts_running_source_and_preserves_exact_replacement(db):
    add_session_round(db, status="running")
    saved = ContextCheckpointService(db).save(
        session_id="session-1",
        source_round_id="run-1",
        source_message_sequence=7,
        source_event_sequence=11,
        trigger_phase="mid_turn",
        summary="handoff",
        messages=replacement(),
        source_token_count=100,
        replacement_token_count=20,
    )
    loaded = ContextCheckpointService(db).load_latest("session-1")

    assert loaded == saved
    assert loaded.source_message_sequence == 7
    assert loaded.source_event_sequence == 11
    assert loaded.trigger_phase == "mid_turn"
    assert loaded.messages == replacement()
    row = db.query(ContextCheckpoint).one()
    assert row.schema_version == CHECKPOINT_SCHEMA_VERSION == 4
    assert row.replacement_sha256 is None


def test_checkpoints_are_append_only(db):
    add_session_round(db)
    service = ContextCheckpointService(db)
    for index in range(4):
        service.save(
            session_id="session-1",
            source_round_id="run-1",
            trigger_phase="pre_turn",
            summary=f"summary-{index}",
            messages=replacement(),
            source_token_count=100,
            replacement_token_count=20,
        )

    rows = db.query(ContextCheckpoint).order_by(ContextCheckpoint.generation).all()
    assert [row.generation for row in rows] == [1, 2, 3, 4]
    assert ContextCheckpointService(db).load_latest("session-1").summary == "summary-3"


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_midturn_checkpoint_survives_round_failure_or_cancellation(db, terminal_status):
    add_session_round(db, status="running")
    saved = ContextCheckpointService(db).save(
        session_id="session-1",
        source_round_id="run-1",
        source_message_sequence=1,
        source_event_sequence=4,
        trigger_phase="mid_turn",
        summary="completed tool output is covered",
        messages=replacement(),
        source_token_count=100,
        replacement_token_count=20,
    )
    row = db.query(Round).filter(Round.id == "run-1").one()
    row.status = terminal_status
    db.commit()

    loaded = ContextCheckpointService(db).load_latest("session-1")
    assert loaded.checkpoint_id == saved.checkpoint_id
    assert loaded.source_event_sequence == 4


def test_legacy_checkpoint_is_ignored_and_authoritative_history_must_rebuild(db):
    add_session_round(db)
    ContextCheckpointService(db).save(
        session_id="session-1",
        source_round_id="run-1",
        messages=replacement(),
        source_token_count=100,
        replacement_token_count=20,
    )
    row = db.query(ContextCheckpoint).one()
    row.schema_version = 3
    db.commit()

    assert ContextCheckpointService(db).load_latest("session-1") is None


def test_invalid_latest_v4_does_not_fall_back_to_older_generation(db):
    add_session_round(db)
    service = ContextCheckpointService(db)
    service.save(
        session_id="session-1",
        source_round_id="run-1",
        messages=replacement(),
        source_token_count=100,
        replacement_token_count=20,
    )
    latest = service.save(
        session_id="session-1",
        source_round_id="run-1",
        messages=replacement(),
        source_token_count=100,
        replacement_token_count=20,
    )
    row = db.query(ContextCheckpoint).filter(
        ContextCheckpoint.checkpoint_id == latest.checkpoint_id
    ).one()
    row.replacement_messages_json = "not-json"
    db.commit()

    assert service.load_latest("session-1") is None


def test_checkpoint_without_source_round_falls_back_to_full_authoritative_history(
    db,
    monkeypatch,
):
    add_authoritative_exchange(db)
    ContextCheckpointService(db).save(
        session_id="session-1",
        source_round_id=None,
        messages=replacement(),
        source_token_count=100,
        replacement_token_count=20,
    )
    use_checkpoint_history_strategy(monkeypatch)
    service = make_restore_service(db)
    service._active_checkpoint_id = "stale-checkpoint"

    restored = service._build_restored_history_messages()

    assert [(message.role, message.content) for message in restored] == [
        ("user", "authoritative user"),
        ("assistant", "authoritative answer"),
    ]
    assert all(message.is_synthetic is False for message in restored)
    assert service._active_checkpoint_id is None


def test_checkpoint_with_missing_source_round_falls_back_to_full_authoritative_history(
    db,
    monkeypatch,
):
    add_authoritative_exchange(db)
    ContextCheckpointService(db).save(
        session_id="session-1",
        source_round_id="missing-run",
        messages=replacement("missing-run"),
        source_token_count=100,
        replacement_token_count=20,
    )
    use_checkpoint_history_strategy(monkeypatch)
    service = make_restore_service(db)

    restored = service._build_restored_history_messages()

    assert [(message.role, message.content) for message in restored] == [
        ("user", "authoritative user"),
        ("assistant", "authoritative answer"),
    ]
    assert all(message.run_id != "missing-run" for message in restored)
    assert service._active_checkpoint_id is None


def test_valid_checkpoint_still_restores_replacement_plus_exact_suffix(db, monkeypatch):
    add_authoritative_exchange(db)
    saved = ContextCheckpointService(db).save(
        session_id="session-1",
        source_round_id="run-1",
        source_message_sequence=1,
        source_event_sequence=0,
        trigger_phase="mid_turn",
        summary="handoff",
        messages=replacement(),
        source_token_count=100,
        replacement_token_count=20,
    )
    use_checkpoint_history_strategy(monkeypatch)
    service = make_restore_service(db)

    restored = service._build_restored_history_messages()

    assert [(message.role, message.content) for message in restored] == [
        ("user", "latest exact user"),
        ("user", f"{SUMMARY_PREFIX}\nhandoff"),
        ("assistant", "authoritative answer"),
    ]
    assert "authoritative user" not in [message.content for message in restored]
    assert service._active_checkpoint_id == saved.checkpoint_id


def test_restore_replays_only_source_round_event_suffix(db):
    add_session_round(db)
    db.add(ConversationMessage(
        session_id="session-1",
        round_id="run-1",
        sequence=1,
        role="user",
        content="user",
        is_synthetic=False,
    ))
    for sequence, payload in [
        (1, {"type": "TEXT_MESSAGE_CONTENT", "delta": "already compacted"}),
        (2, {"type": "STEP_FINISHED"}),
        (3, {"type": "TEXT_MESSAGE_CONTENT", "delta": "after checkpoint"}),
        (4, {"type": "STEP_FINISHED"}),
    ]:
        db.add(AGUIEventLog(
            run_id="run-1",
            event_type=payload["type"],
            payload=json.dumps(payload),
            sequence=sequence,
        ))
    db.commit()
    checkpoint = ContextCheckpointService(db).save(
        session_id="session-1",
        source_round_id="run-1",
        source_message_sequence=1,
        source_event_sequence=2,
        trigger_phase="mid_turn",
        summary="handoff",
        messages=replacement(),
        source_token_count=100,
        replacement_token_count=20,
    )
    history = SimpleNamespace(db=db, reset_session=lambda: db.rollback())
    service = AgentService.__new__(AgentService)
    service.session_id = "session-1"
    service.history_service = history

    suffix = service._rebuild_messages_after_checkpoint(checkpoint)
    assert [(message.role, message.content) for message in suffix] == [
        ("assistant", "after checkpoint")
    ]
    assert suffix[0].run_id == "run-1"


def test_restore_replays_later_round_after_preturn_checkpoint(db):
    add_session_round(db, round_id="run-1", status="completed")
    add_session_round(db, round_id="run-2", status="completed")
    db.add(ConversationMessage(
        session_id="session-1",
        round_id="run-2",
        sequence=2,
        role="user",
        content="new incoming user",
        is_synthetic=False,
    ))
    db.add(AGUIEventLog(
        run_id="run-2",
        event_type="TEXT_MESSAGE_CONTENT",
        payload=json.dumps({"type": "TEXT_MESSAGE_CONTENT", "delta": "new answer"}),
        sequence=1,
    ))
    db.add(AGUIEventLog(
        run_id="run-2",
        event_type="STEP_FINISHED",
        payload=json.dumps({"type": "STEP_FINISHED"}),
        sequence=2,
    ))
    db.commit()
    checkpoint = ContextCheckpointService(db).save(
        session_id="session-1",
        source_round_id="run-1",
        source_message_sequence=0,
        source_event_sequence=0,
        trigger_phase="pre_turn",
        summary="handoff",
        messages=replacement("run-1"),
        source_token_count=100,
        replacement_token_count=20,
    )
    history = SimpleNamespace(db=db, reset_session=lambda: db.rollback())
    service = AgentService.__new__(AgentService)
    service.session_id = "session-1"
    service.history_service = history

    tail = service._rebuild_messages_after_checkpoint(checkpoint)
    assert [(message.role, message.content) for message in tail] == [
        ("user", "new incoming user"),
        ("assistant", "new answer"),
    ]
