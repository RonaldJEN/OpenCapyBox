"""Vertical crash-boundary tests for same-Round interaction requests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.agent.schema.agui_events import CustomEvent
from src.api.models.agui_event import AGUIEventLog
from src.api.models.agent_interaction import AgentInteraction
from src.api.models.conversation_message import ConversationMessage
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.services.agent_interaction_service import (
    AgentInteractionService,
    ContinuationWriteFence,
    InteractionConflictError,
)
from src.api.services.agui_event_bus import (
    AguiEventBus,
    RoundTerminalWriteSuppressed,
)
from src.api.services.run_completion_service import RunCompletionService
from src.api.services.agent_service import AgentService
from src.api.services.history_service import HistoryService


@pytest.mark.asyncio
async def test_waiting_interaction_and_requested_event_cross_restart_together(
    monkeypatch,
):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        with factory() as db:
            db.add(Session(id="session-atomic", user_id="alice"))
            db.commit()
            db.add(Round(
                id="round-atomic",
                session_id="session-atomic",
                user_message="continue",
                status="running",
            ))
            db.add(AGUIEventLog(
                run_id="round-atomic",
                event_type="STEP_STARTED",
                payload=json.dumps({
                    "type": "STEP_STARTED",
                    "stepName": "step-1",
                    "sequence": 1,
                }),
                sequence=1,
            ))
            db.commit()

            value = {
                "interactionId": "interaction-atomic",
                "runId": "round-atomic",
                "kind": "user_input",
                "toolCallId": "call-atomic",
                "payload": {"questions": [{"question": "Continue?"}]},
            }
            AgentInteractionService.create_pending(
                db,
                interaction_id="interaction-atomic",
                session_id="session-atomic",
                round_id="round-atomic",
                kind="user_input",
                tool_call_id="call-atomic",
                request_payload=value,
                step_count=1,
                commit=False,
            )
            bus = AguiEventBus(db)
            high_water = iter([0, 1])
            monkeypatch.setattr(
                bus,
                "_current_high_water",
                lambda *_args: next(high_water),
            )
            await bus.publish(
                "round-atomic",
                CustomEvent(name="interaction_requested", value=value),
            )

        # A fresh Session/EventBus represents a different worker after restart.
        with factory() as recovered_db:
            recovered_round = recovered_db.get(Round, "round-atomic")
            assert recovered_round.status == "waiting_interaction"
            assert recovered_round.step_count == 1
            assert recovered_db.get(AgentInteraction, "interaction-atomic") is not None
            projected = HistoryService(recovered_db).get_session_rounds(
                "session-atomic"
            )
            assert projected[0]["steps"][0]["step_number"] == 1
            assert projected[0]["steps"][0]["status"] == "completed"
            subscription = AguiEventBus(recovered_db).subscribe("round-atomic", 1)
            event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
            assert event["type"] == "CUSTOM"
            assert event["name"] == "interaction_requested"
            assert event["value"]["interactionId"] == "interaction-atomic"
            await subscription.aclose()
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_crash_before_requested_event_commit_leaves_no_waiting_half_state():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        with factory() as db:
            db.add(Session(id="session-rollback", user_id="alice"))
            db.commit()
            db.add(Round(
                id="round-rollback",
                session_id="session-rollback",
                user_message="continue",
                status="running",
            ))
            db.commit()
            AgentInteractionService.create_pending(
                db,
                interaction_id="interaction-rollback",
                session_id="session-rollback",
                round_id="round-rollback",
                kind="user_input",
                tool_call_id="call-rollback",
                request_payload={"payload": {"questions": []}},
                step_count=1,
                commit=False,
            )

            # Process loss closes the transaction before the event writer commits.
            db.rollback()

            rolled_back_round = db.get(Round, "round-rollback")
            assert rolled_back_round.status == "running"
            assert int(rolled_back_round.step_count or 0) == 0
            assert db.get(AgentInteraction, "interaction-rollback") is None
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.asyncio
async def test_continuation_boundaries_commit_event_and_state_atomically(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        with factory() as db:
            db.add(Session(id="session-fence", user_id="alice"))
            db.add(Round(
                id="round-fence",
                session_id="session-fence",
                user_message="continue",
                status="running",
            ))
            db.add(AGUIEventLog(
                run_id="round-fence",
                event_type="CUSTOM",
                payload=json.dumps({"type": "CUSTOM", "name": "seed", "sequence": 1}),
                sequence=1,
            ))
            db.commit()
            AgentInteractionService.create_pending(
                db,
                interaction_id="interaction-fence",
                session_id="session-fence",
                round_id="round-fence",
                kind="user_input",
                tool_call_id="call-fence",
                request_payload={"payload": {"questions": [{"question": "Continue?"}]}},
            )
            AgentInteractionService.answer_pending(
                db,
                session_id="session-fence",
                interaction_id="interaction-fence",
                answers={"Continue?": "Yes"},
            )
            claimed = AgentInteractionService.claim_answered_continuation(
                db,
                session_id="session-fence",
                interaction_id="interaction-fence",
            )
            fence = ContinuationWriteFence(
                session_id="session-fence",
                interaction_id="interaction-fence",
                claim_token=claimed.claim_token,
                transition="start",
            )
            bus = AguiEventBus(db)
            high_water = iter([0, 1, 1, 2])
            monkeypatch.setattr(
                bus,
                "_current_high_water",
                lambda *_args: next(high_water),
            )
            await bus.publish(
                "round-fence",
                CustomEvent(name="interaction_resolved", value={"interactionId": "interaction-fence"}),
                continuation_fence=fence,
            )
            assert db.get(Round, "round-fence").status == "running"
            started_interaction = db.get(AgentInteraction, "interaction-fence")
            assert started_interaction.status == "pending"
            assert started_interaction.continuation_started_at is not None

            await bus.publish(
                "round-fence",
                CustomEvent(name="durable_agent_boundary", value={}),
                continuation_fence=ContinuationWriteFence(
                    session_id="session-fence",
                    interaction_id="interaction-fence",
                    claim_token=claimed.claim_token,
                    transition="complete",
                ),
            )
            assert db.get(AgentInteraction, "interaction-fence").status == "answered"
            assert db.query(AGUIEventLog).filter(
                AGUIEventLog.run_id == "round-fence"
            ).count() == 3
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.asyncio
async def test_reclaimed_continuation_cannot_write_event_or_terminal():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    claimed_at = datetime(2026, 8, 25, 10, 0, 0)
    try:
        with factory() as db:
            db.add(Session(id="session-reclaimed", user_id="alice"))
            db.add(Round(
                id="round-reclaimed",
                session_id="session-reclaimed",
                user_message="continue",
                status="running",
            ))
            db.commit()
            AgentInteractionService.create_pending(
                db,
                interaction_id="interaction-reclaimed",
                session_id="session-reclaimed",
                round_id="round-reclaimed",
                kind="user_input",
                tool_call_id="call-reclaimed",
                request_payload={"payload": {"questions": [{"question": "Continue?"}]}},
            )
            AgentInteractionService.answer_pending(
                db,
                session_id="session-reclaimed",
                interaction_id="interaction-reclaimed",
                answers={"Continue?": "Yes"},
            )
            stale = AgentInteractionService.claim_answered_continuation(
                db,
                session_id="session-reclaimed",
                interaction_id="interaction-reclaimed",
                claimed_at=claimed_at,
                lease_seconds=1,
            )
            AgentInteractionService.claim_answered_continuation(
                db,
                session_id="session-reclaimed",
                interaction_id="interaction-reclaimed",
                claimed_at=claimed_at + timedelta(seconds=2),
            )
            stale_fence = ContinuationWriteFence(
                session_id="session-reclaimed",
                interaction_id="interaction-reclaimed",
                claim_token=stale.claim_token,
                transition="start",
            )

            with pytest.raises(InteractionConflictError):
                await AguiEventBus(db).publish(
                    "round-reclaimed",
                    CustomEvent(name="interaction_resolved", value={}),
                    continuation_fence=stale_fence,
                )
            with pytest.raises(InteractionConflictError):
                RunCompletionService(db).complete_sync(
                    run_id="round-reclaimed",
                    status="failed",
                    continuation_fence=ContinuationWriteFence(
                        session_id="session-reclaimed",
                        interaction_id="interaction-reclaimed",
                        claim_token=stale.claim_token,
                        transition="validate",
                    ),
                )

            assert db.get(Round, "round-reclaimed").status == "waiting_interaction"
            assert db.query(AGUIEventLog).filter(
                AGUIEventLog.run_id == "round-reclaimed"
            ).count() == 0
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.asyncio
async def test_synthetic_message_and_marker_share_commit_or_rollback():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        with factory() as db:
            db.add_all([
                Session(id="session-synthetic-ok", user_id="alice"),
                Session(id="session-synthetic-terminal", user_id="alice"),
                Round(
                    id="round-synthetic-ok",
                    session_id="session-synthetic-ok",
                    user_message="continue",
                    status="running",
                ),
                Round(
                    id="round-synthetic-terminal",
                    session_id="session-synthetic-terminal",
                    user_message="continue",
                    status="cancelled",
                ),
            ])
            db.commit()

            service = AgentService.__new__(AgentService)
            service.session_id = "session-synthetic-ok"
            service.history_service = HistoryService(db)
            service._save_conversation_message(
                "user",
                [{"type": "text", "text": "synthetic"}],
                round_id="round-synthetic-ok",
                is_synthetic=True,
                raise_on_error=True,
                commit=False,
            )
            await AguiEventBus(db).publish(
                "round-synthetic-ok",
                CustomEvent(
                    name="synthetic_user_message",
                    value={"contentRef": "conversation_messages"},
                ),
            )

            service.session_id = "session-synthetic-terminal"
            service._save_conversation_message(
                "user",
                [{"type": "text", "text": "must rollback"}],
                round_id="round-synthetic-terminal",
                is_synthetic=True,
                raise_on_error=True,
                commit=False,
            )
            with pytest.raises(RoundTerminalWriteSuppressed):
                await AguiEventBus(db).publish(
                    "round-synthetic-terminal",
                    CustomEvent(
                        name="synthetic_user_message",
                        value={"contentRef": "conversation_messages"},
                    ),
                )

        with factory() as verify:
            assert verify.query(ConversationMessage).filter(
                ConversationMessage.round_id == "round-synthetic-ok"
            ).count() == 1
            assert verify.query(AGUIEventLog).filter(
                AGUIEventLog.run_id == "round-synthetic-ok"
            ).count() == 1
            assert verify.query(ConversationMessage).filter(
                ConversationMessage.round_id == "round-synthetic-terminal"
            ).count() == 0
            assert verify.query(AGUIEventLog).filter(
                AGUIEventLog.run_id == "round-synthetic-terminal"
            ).count() == 0
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.asyncio
async def test_events_recheck_terminal_after_round_lock(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        with factory() as db:
            db.add(Session(id="session-terminal-race", user_id="alice"))
            db.add(Round(
                id="round-terminal-race",
                session_id="session-terminal-race",
                user_message="continue",
                status="cancelled",
            ))
            db.commit()
            bus = AguiEventBus(db)
            monkeypatch.setattr(bus, "_is_round_terminal", lambda *_args: False)
            ephemeral_fanout: list[dict] = []

            async def _record_ephemeral(_run_id, event):
                ephemeral_fanout.append(event)

            monkeypatch.setattr(bus, "publish_ephemeral", _record_ephemeral)

            with pytest.raises(RoundTerminalWriteSuppressed):
                await bus.publish(
                    "round-terminal-race",
                    CustomEvent(name="late_event", value={}),
                )

            assert db.query(AGUIEventLog).filter(
                AGUIEventLog.run_id == "round-terminal-race"
            ).count() == 0
            with pytest.raises(RoundTerminalWriteSuppressed):
                await bus.publish(
                    "round-terminal-race",
                    {
                        "type": "TEXT_MESSAGE_CONTENT",
                        "messageId": "late-message",
                        "delta": "late",
                    },
                )
            assert ephemeral_fanout == []
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
