import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.models.agent_interaction import AgentInteraction
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.tool_permission import ToolApprovalRequest
from src.api.services.agent_interaction_service import (
    AgentInteractionService,
    InteractionConflictError,
)


@pytest.fixture()
def interaction_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Session.__table__,
            Round.__table__,
            AgentInteraction.__table__,
            ToolApprovalRequest.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Session(id="session-1", user_id="user-1"))
    db.add(Round(id="round-1", session_id="session-1", user_message="hello", status="running"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_pending_interaction_parks_round(interaction_db):
    interaction = AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-1",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-1",
        request_payload={"payload": {"questions": [{"question": "Continue?"}]}},
    )

    assert interaction.status == "pending"
    round_obj = interaction_db.query(Round).filter(Round.id == "round-1").one()
    assert round_obj.status == "waiting_interaction"
    assert round_obj.completed_at is None


def test_pending_interaction_can_join_caller_owned_event_transaction(interaction_db):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-atomic",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-atomic",
        request_payload={"payload": {"questions": []}},
        commit=False,
    )

    assert interaction_db.get(AgentInteraction, "interaction-atomic") is not None
    assert interaction_db.get(Round, "round-1").status == "waiting_interaction"

    interaction_db.rollback()

    assert interaction_db.get(AgentInteraction, "interaction-atomic") is None
    assert interaction_db.get(Round, "round-1").status == "running"


def test_answer_claim_start_and_complete_resume_same_round(interaction_db):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-1",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-1",
        request_payload={"payload": {"questions": []}},
    )
    answers = {"Continue?": "Yes"}
    answered = AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        answers=answers,
        tool_result_content="Continue?: Yes",
    )

    assert answered.status == "pending"
    assert json.loads(answered.answer_payload) == answers
    assert answered.round_id == "round-1"
    round_obj = interaction_db.query(Round).filter(Round.id == "round-1").one()
    assert round_obj.status == "waiting_interaction"

    repeated = AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        answers=answers,
        tool_result_content="Continue?: Yes",
    )
    assert repeated.status == "pending"

    with pytest.raises(InteractionConflictError):
        AgentInteractionService.answer_pending(
            interaction_db,
            session_id="session-1",
            interaction_id="interaction-1",
            answers={"Continue?": "No"},
            tool_result_content="Continue?: No",
        )

    claimed_at = datetime(2026, 8, 24, 12, 0, 0)
    claimed = AgentInteractionService.claim_answered_continuation(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        claimed_at=claimed_at,
        lease_seconds=30,
    )
    assert claimed.status == "pending"
    assert claimed.claim_token
    assert claimed.claim_lease_expires_at == claimed_at + timedelta(seconds=30)
    round_obj = interaction_db.query(Round).filter(Round.id == "round-1").one()
    assert round_obj.status == "waiting_interaction"

    started = AgentInteractionService.mark_continuation_started(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        claim_token=claimed.claim_token,
        started_at=claimed_at + timedelta(seconds=1),
    )
    assert started.status == "pending"
    assert started.continuation_started_at == claimed_at + timedelta(seconds=1)
    round_obj = interaction_db.query(Round).filter(Round.id == "round-1").one()
    assert round_obj.status == "running"

    renewed = AgentInteractionService.renew_continuation_claim(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        claim_token=claimed.claim_token,
        renewed_at=claimed_at + timedelta(seconds=2),
        lease_seconds=30,
    )
    assert renewed.claim_lease_expires_at == claimed_at + timedelta(seconds=32)

    completed = AgentInteractionService.complete_continuation(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        claim_token=claimed.claim_token,
        completed_at=claimed_at + timedelta(seconds=3),
    )
    assert completed.status == "answered"
    assert completed.claim_token is None
    assert completed.claim_lease_expires_at is None


def test_repeated_answer_is_idempotent_when_json_key_order_changes(interaction_db):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-key-order",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-key-order",
        request_payload={
            "payload": {
                "questions": [
                    {"question": "Z question?"},
                    {"question": "A question?"},
                ]
            }
        },
    )

    first = AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-key-order",
        answers={"A question?": "No", "Z question?": "Yes"},
        tool_result_content="ignored caller rendering",
    )
    repeated = AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-key-order",
        answers={"Z question?": "Yes", "A question?": "No"},
        tool_result_content="also ignored",
    )

    assert repeated.answer_payload == first.answer_payload
    assert repeated.answer_payload == (
        '{"Z question?": "Yes", "A question?": "No"}'
    )
    assert repeated.tool_result_content == (
        "User answered:\n- Z question?: Yes\n- A question?: No"
    )


def test_round_can_request_another_interaction_after_answer(interaction_db):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-1",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-1",
        request_payload={"payload": {"questions": []}},
    )
    AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        answers={"Q1": "A1"},
        tool_result_content="Q1: A1",
    )
    claimed = AgentInteractionService.claim_answered_continuation(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
    )
    AgentInteractionService.mark_continuation_started(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        claim_token=claimed.claim_token,
    )
    AgentInteractionService.complete_continuation(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        claim_token=claimed.claim_token,
    )

    second = AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-2",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-2",
        request_payload={"payload": {"questions": []}},
    )
    assert second.status == "pending"


def test_expired_prestart_claim_can_be_reclaimed(interaction_db):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-1",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-1",
        request_payload={"payload": {"questions": []}},
    )
    AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        answers={"Continue?": "Yes"},
        tool_result_content="Continue?: Yes",
    )
    first_time = datetime(2026, 8, 24, 12, 0, 0)
    first = AgentInteractionService.claim_answered_continuation(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        claimed_at=first_time,
        lease_seconds=10,
    )
    recovered = AgentInteractionService.claim_answered_continuation(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        claimed_at=first_time + timedelta(seconds=11),
        lease_seconds=20,
    )
    assert recovered.status == "pending"
    assert recovered.claim_token != first.claim_token
    assert interaction_db.get(Round, "round-1").status == "waiting_interaction"

    with pytest.raises(InteractionConflictError, match="claim token"):
        AgentInteractionService.complete_continuation(
            interaction_db,
            session_id="session-1",
            interaction_id="interaction-1",
            claim_token=first.claim_token,
        )



def test_expired_started_claim_cannot_be_reclaimed_or_released(interaction_db):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-started",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-started",
        request_payload={"payload": {"questions": []}},
    )
    AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-started",
        answers={"Continue?": "Yes"},
    )
    first_time = datetime(2026, 8, 24, 12, 0, 0)
    claim = AgentInteractionService.claim_answered_continuation(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-started",
        claimed_at=first_time,
        lease_seconds=10,
    )
    AgentInteractionService.mark_continuation_started(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-started",
        claim_token=claim.claim_token,
        started_at=first_time + timedelta(seconds=1),
    )

    with pytest.raises(InteractionConflictError, match="already started"):
        AgentInteractionService.claim_answered_continuation(
            interaction_db,
            session_id="session-1",
            interaction_id="interaction-started",
            claimed_at=first_time + timedelta(seconds=11),
        )
    with pytest.raises(InteractionConflictError, match="cannot release started"):
        AgentInteractionService.release_continuation_claim(
            interaction_db,
            session_id="session-1",
            interaction_id="interaction-started",
            claim_token=claim.claim_token,
            released_at=first_time + timedelta(seconds=11),
        )

    assert AgentInteractionService.load_irrecoverable_continuation_round_ids(
        interaction_db,
        session_id="session-1",
        now=first_time + timedelta(seconds=11),
    ) == ["round-1"]
    assert interaction_db.get(Round, "round-1").status == "running"


def test_release_claim_restores_waiting_round(interaction_db):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-1",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-1",
        request_payload={"payload": {"questions": []}},
    )
    AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        answers={"Continue?": "Yes"},
        tool_result_content="Continue?: Yes",
    )
    claim = AgentInteractionService.claim_answered_continuation(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
    )
    released = AgentInteractionService.release_continuation_claim(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        claim_token=claim.claim_token,
    )

    assert released.status == "pending"
    assert released.claim_token is None
    assert interaction_db.get(Round, "round-1").status == "waiting_interaction"


def test_repark_is_noop_for_unclaimed_answer_already_waiting(interaction_db):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-1",
        session_id="session-1",
        round_id="round-1",
        kind="user_input",
        tool_call_id="tool-1",
        request_payload={"payload": {"questions": []}},
    )
    answered = AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        answers={"Continue?": "Yes"},
        tool_result_content="Continue?: Yes",
    )
    original_updated_at = answered.updated_at

    recovered = AgentInteractionService.repark_expired_continuation_claims(
        interaction_db,
        session_id="session-1",
        now=original_updated_at + timedelta(minutes=1),
    )

    persisted = interaction_db.get(AgentInteraction, "interaction-1")
    assert recovered == 0
    assert persisted.updated_at == original_updated_at
    assert interaction_db.get(Round, "round-1").status == "waiting_interaction"


@pytest.mark.parametrize(
    ("approval_status", "expected_lock_kind"),
    [
        pytest.param("requested", "tool_approval", id="started-before-decision"),
        pytest.param("approved", "tool_approval", id="started-before-dispatch"),
        pytest.param("denied", "tool_approval", id="started-denial-projection"),
        pytest.param("executing", None, id="active-execution-lease"),
        pytest.param("executed", "tool_approval", id="executed-result-not-replayed"),
        pytest.param("failed", "tool_approval", id="failed-result-not-replayed"),
        pytest.param("unknown", "tool_approval", id="already-reconciled-unknown"),
    ],
)
def test_started_tool_approval_claim_never_reparks(
    interaction_db,
    approval_status,
    expected_lock_kind,
):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="approval-1",
        session_id="session-1",
        round_id="round-1",
        kind="tool_approval",
        tool_call_id="tool-1",
        request_payload={"payload": {"kind": "tool_approval"}},
    )
    claimed_at = datetime(2026, 8, 24, 12, 0, 0)
    resolution = "deny" if approval_status == "denied" else (
        None if approval_status == "requested" else "allow_once"
    )
    interaction_db.add(
        ToolApprovalRequest(
            id="approval-1",
            user_id="user-1",
            session_id="session-1",
            run_id="round-1",
            tool_call_id="tool-1",
            provider="builtin",
            tool_name="shell_exec",
            model_tool_name="shell_exec",
            arguments_encrypted="encrypted",
            arguments_hash="hash",
            status=approval_status,
            resolution=resolution,
            resolved_at=(
                claimed_at if approval_status in {"approved", "denied", "executing", "unknown"}
                else None
            ),
            execution_started_at=(
                claimed_at if approval_status == "executing" else None
            ),
            execution_claim_token=(
                "execution-claim" if approval_status == "executing" else None
            ),
            execution_lease_expires_at=(
                claimed_at + timedelta(minutes=5)
                if approval_status == "executing"
                else None
            ),
            completed_at=(
                claimed_at
                if approval_status in {"denied", "executed", "failed", "unknown"}
                else None
            ),
        )
    )
    interaction_db.commit()
    AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="approval-1",
        answers={"approval": resolution or "allow_once"},
        tool_result_content=(
            "Tool execution denied by user."
            if approval_status == "denied"
            else "[Tool approval execution pending]"
        ),
    )
    claim = AgentInteractionService.claim_answered_continuation(
        interaction_db,
        session_id="session-1",
        interaction_id="approval-1",
        claimed_at=claimed_at,
        lease_seconds=10,
    )
    AgentInteractionService.mark_continuation_started(
        interaction_db,
        session_id="session-1",
        interaction_id="approval-1",
        claim_token=claim.claim_token,
        started_at=claimed_at + timedelta(seconds=1),
    )

    recovered = AgentInteractionService.repark_expired_continuation_claims(
        interaction_db,
        session_id="session-1",
        now=claimed_at + timedelta(seconds=11),
    )

    interaction_db.expire_all()
    persisted_interaction = interaction_db.get(AgentInteraction, "approval-1")
    persisted_round = interaction_db.get(Round, "round-1")
    persisted_approval = interaction_db.get(ToolApprovalRequest, "approval-1")
    irrecoverable = (
        AgentInteractionService.load_irrecoverable_continuation_round_ids(
            interaction_db,
            now=claimed_at + timedelta(seconds=11),
        )
    )
    assert recovered == 0
    assert irrecoverable == ["round-1"]
    assert persisted_approval.status == approval_status
    assert persisted_round.status == "running"
    assert persisted_interaction.claim_token == claim.claim_token
    assert persisted_interaction.claim_lease_expires_at == (
        claimed_at + timedelta(seconds=10)
    )
    if approval_status == "executed":
        # A worker may complete the Interaction after the reconciler's scan but
        # before its terminal write. The lock-time recheck must preserve it.
        AgentInteractionService.complete_continuation(
            interaction_db,
            session_id="session-1",
            interaction_id="approval-1",
            claim_token=claim.claim_token,
            completed_at=claimed_at + timedelta(seconds=11),
        )
        assert (
            AgentInteractionService.lock_irrecoverable_continuation_round_for_failure(
                interaction_db,
                round_id="round-1",
                now=claimed_at + timedelta(seconds=11),
            )
            is None
        )
        interaction_db.rollback()
        assert interaction_db.get(Round, "round-1").status == "running"
    else:
        assert (
            AgentInteractionService.lock_irrecoverable_continuation_round_for_failure(
                interaction_db,
                round_id="round-1",
                now=claimed_at + timedelta(seconds=11),
            )
            == expected_lock_kind
        )
        interaction_db.rollback()


def test_compound_tool_approval_transaction_can_own_commit(interaction_db):
    AgentInteractionService.create_pending(
        interaction_db,
        interaction_id="interaction-1",
        session_id="session-1",
        round_id="round-1",
        kind="tool_approval",
        tool_call_id="tool-1",
        request_payload={"payload": {"questions": []}},
    )

    round_obj, interaction = AgentInteractionService.lock_pending_for_update(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
    )
    assert round_obj.id == "round-1"
    assert interaction.id == "interaction-1"
    AgentInteractionService.answer_pending(
        interaction_db,
        session_id="session-1",
        interaction_id="interaction-1",
        answers={"approval": "allow_once"},
        tool_result_content="[Tool approval execution pending]",
        commit=False,
    )
    interaction_db.rollback()

    persisted = interaction_db.get(AgentInteraction, "interaction-1")
    assert persisted.answer_payload is None
    assert interaction_db.get(Round, "round-1").status == "waiting_interaction"
