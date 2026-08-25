"""History projection must not re-offer post-dispatch tool approvals."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.agent_interaction import AgentInteraction
from src.api.models.agui_event import AGUIEventLog
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.tool_permission import ToolApprovalRequest
from src.api.services.history_service import HistoryService
from src.api.services.agui_event_bus import get_agui_event_bus
from src.api.utils.timezone import now_naive


def test_expired_started_user_input_converges_to_durable_failed_history():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    started_at = now_naive() - timedelta(minutes=5)
    try:
        with factory() as db:
            db.add(Session(id="session-started-recovery", user_id="alice"))
            db.add(Round(
                id="round-started-recovery",
                session_id="session-started-recovery",
                user_message="original",
                status="running",
                step_count=1,
            ))
            db.add(AgentInteraction(
                id="interaction-started-recovery",
                session_id="session-started-recovery",
                round_id="round-started-recovery",
                kind="user_input",
                tool_call_id="call-started-recovery",
                status="pending",
                request_payload='{"payload":{"questions":[]}}',
                answer_payload='{"Continue?":"Yes"}',
                tool_result_content="User answered:\n- Continue?: Yes",
                claim_token="dead-worker",
                claim_lease_expires_at=started_at + timedelta(seconds=10),
                continuation_started_at=started_at,
            ))
            db.commit()

            rounds = HistoryService(db).get_session_rounds(
                "session-started-recovery"
            )

            assert len(rounds) == 1
            assert rounds[0]["status"] == "failed"
            assert rounds[0]["interrupt"] is None
            interaction = db.get(
                AgentInteraction,
                "interaction-started-recovery",
            )
            assert interaction.status == "failed"
            assert interaction.claim_token is None
            terminal = (
                db.query(AGUIEventLog)
                .filter(AGUIEventLog.run_id == "round-started-recovery")
                .one()
            )
            assert terminal.event_type == "RUN_ERROR"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("approval_status", ["executed", "unknown"])
def test_terminal_approval_pending_interaction_becomes_failed_history(
    approval_status: str,
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
            db.add(Session(id="session-history-recovery", user_id="alice"))
            db.commit()
            db.add(Round(
                id="round-history-recovery",
                session_id="session-history-recovery",
                user_message="original",
                status="waiting_interaction",
            ))
            db.commit()
            db.add(AgentInteraction(
                id="approval-history-recovery",
                session_id="session-history-recovery",
                round_id="round-history-recovery",
                kind="tool_approval",
                tool_call_id="call-history-recovery",
                status="pending",
                request_payload='{"payload":{"kind":"tool_approval"}}',
                answer_payload='{"approval":"allow_once"}',
                tool_result_content="[Tool approval execution pending]",
            ))
            db.add(ToolApprovalRequest(
                id="approval-history-recovery",
                user_id="alice",
                session_id="session-history-recovery",
                run_id="round-history-recovery",
                tool_call_id="call-history-recovery",
                provider="builtin",
                tool_name="shell_exec",
                model_tool_name="shell_exec",
                arguments_encrypted="encrypted",
                arguments_hash="hash",
                status=approval_status,
                resolution="allow_once",
            ))
            db.commit()

            bus = get_agui_event_bus()
            queue: asyncio.Queue = asyncio.Queue()
            with bus.subscribers_lock:
                bus.subscribers["round-history-recovery"] = [queue]
            try:
                rounds = HistoryService(db).get_session_rounds(
                    "session-history-recovery"
                )
                live_terminal = queue.get_nowait()
            finally:
                with bus.subscribers_lock:
                    bus.subscribers.pop("round-history-recovery", None)

            assert len(rounds) == 1
            assert live_terminal["type"] == "RUN_ERROR"
            assert rounds[0]["status"] == "failed"
            assert rounds[0]["interrupt"] is None
            assert db.get(
                AgentInteraction,
                "approval-history-recovery",
            ).status == "failed"
            assert db.get(
                ToolApprovalRequest,
                "approval-history-recovery",
            ).status == approval_status
            terminal = (
                db.query(AGUIEventLog)
                .filter(AGUIEventLog.run_id == "round-history-recovery")
                .one()
            )
            assert terminal.event_type == "RUN_ERROR"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("approval_status", ["approved", "denied"])
def test_predispatch_approval_remains_visible_as_waiting_interaction(
    approval_status: str,
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
            db.add(Session(id="session-history-waiting", user_id="alice"))
            db.commit()
            db.add(Round(
                id="round-history-waiting",
                session_id="session-history-waiting",
                user_message="original",
                status="waiting_interaction",
            ))
            db.commit()
            resolution = "deny" if approval_status == "denied" else "allow_once"
            db.add(AgentInteraction(
                id="approval-history-waiting",
                session_id="session-history-waiting",
                round_id="round-history-waiting",
                kind="tool_approval",
                tool_call_id="call-history-waiting",
                status="pending",
                request_payload='{"payload":{"kind":"tool_approval"}}',
                answer_payload=f'{{"approval":"{resolution}"}}',
                tool_result_content=(
                    "Tool execution denied by user."
                    if approval_status == "denied"
                    else "[Tool approval execution pending]"
                ),
            ))
            db.add(ToolApprovalRequest(
                id="approval-history-waiting",
                user_id="alice",
                session_id="session-history-waiting",
                run_id="round-history-waiting",
                tool_call_id="call-history-waiting",
                provider="builtin",
                tool_name="shell_exec",
                model_tool_name="shell_exec",
                arguments_encrypted="encrypted",
                arguments_hash="hash",
                status=approval_status,
                resolution=resolution,
            ))
            db.commit()

            rounds = HistoryService(db).get_session_rounds(
                "session-history-waiting"
            )

            assert len(rounds) == 1
            assert rounds[0]["status"] == "waiting_interaction"
            assert rounds[0]["interrupt"]["id"] == "approval-history-waiting"
            assert rounds[0]["interrupt"]["reason"] == "human_approval"
            assert db.get(
                AgentInteraction,
                "approval-history-waiting",
            ).status == "pending"
            assert (
                db.query(AGUIEventLog)
                .filter(AGUIEventLog.run_id == "round-history-waiting")
                .count()
                == 0
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
