"""Terminal convergence and PostgreSQL lock-order regression tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import Barrier, Event

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.agent_interaction import AgentInteraction
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.tool_permission import ToolApprovalRequest
from src.api.services.agent_interaction_service import (
    AgentInteractionService,
    InteractionConflictError,
)
from src.api.services.run_completion_service import RunCompletionService
from src.api.services.tool_permission_service import (
    ToolRef,
    create_approval_request,
    dispatch_approval_request,
    prepare_approval_request,
)
from tests.db_safety import (
    build_pytest_pg_engine,
    create_all_for_test_engine,
    reset_all_tables,
)


_PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture()
def completion_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _add_waiting_round_with_approval(db, *, run_id: str = "round-1") -> None:
    db.add(Session(id="session-1", user_id="user-1"))
    db.add(
        Round(
            id=run_id,
            session_id="session-1",
            user_message="hello",
            status="waiting_interaction",
        )
    )
    db.add(
        AgentInteraction(
            id="interaction-1",
            session_id="session-1",
            round_id=run_id,
            kind="tool_approval",
            tool_call_id="tool-call-1",
            status="pending",
            request_payload="{}",
            answer_payload='{"approval":"allow_once"}',
            tool_result_content="[Tool approval execution pending]",
            claim_token="continuation-claim",
        )
    )
    db.add(
        ToolApprovalRequest(
            id="interaction-1",
            user_id="user-1",
            session_id="session-1",
            run_id=run_id,
            tool_call_id="tool-call-1",
            provider="builtin",
            tool_name="shell_exec",
            model_tool_name="shell_exec",
            arguments_encrypted="encrypted",
            arguments_hash="hash",
            status="requested",
        )
    )
    db.commit()


def test_terminal_cancel_closes_pending_interaction_and_orphan_approval(completion_db):
    _add_waiting_round_with_approval(completion_db)

    stored = RunCompletionService(completion_db).complete_sync(
        run_id="round-1",
        status="cancelled",
        step_count=1,
    )

    assert stored is not None
    interaction = completion_db.get(AgentInteraction, "interaction-1")
    approval = completion_db.get(ToolApprovalRequest, "interaction-1")
    assert interaction.status == "cancelled"
    assert interaction.resolved_at is not None
    assert interaction.claim_token is None
    assert interaction.claim_lease_expires_at is None
    assert approval.status == "cancelled"
    assert approval.resolved_at is not None
    assert approval.completed_at is not None
    assert "cancelled" in approval.error.lower()

    # Repeated terminal convergence also repairs a requested approval orphaned
    # by an older process after the terminal event was already committed.
    completion_db.add(
        ToolApprovalRequest(
            id="approval-late",
            user_id="user-1",
            session_id="session-1",
            run_id="round-1",
            tool_call_id="tool-call-late",
            provider="builtin",
            tool_name="shell_exec",
            model_tool_name="shell_exec",
            arguments_encrypted="encrypted",
            arguments_hash="hash-late",
            status="requested",
        )
    )
    completion_db.commit()

    repeated = RunCompletionService(completion_db).complete_sync(
        run_id="round-1",
        status="cancelled",
    )

    assert repeated is not None
    assert repeated.sequence == stored.sequence
    assert completion_db.get(ToolApprovalRequest, "approval-late").status == "cancelled"


def test_terminal_cancel_preserves_approved_resolution_timestamp(completion_db):
    _add_waiting_round_with_approval(completion_db)
    approval = completion_db.get(ToolApprovalRequest, "interaction-1")
    approval.status = "approved"
    approval.resolution = "allow_once"
    approval.resolved_at = approval.requested_at
    original_resolved_at = approval.resolved_at
    completion_db.commit()

    RunCompletionService(completion_db).complete_sync(
        run_id="round-1",
        status="cancelled",
    )

    completion_db.expire_all()
    cancelled = completion_db.get(ToolApprovalRequest, "interaction-1")
    assert cancelled.status == "cancelled"
    assert cancelled.resolved_at == original_resolved_at
    assert "before approved tool dispatch" in cancelled.error


@pytest.mark.parametrize(
    ("round_status", "approval_status", "outcome_uncertain"),
    [
        ("waiting_interaction", None, False),
        ("waiting_interaction", "requested", False),
        ("running", None, True),
        ("running", "approved", False),
        ("running", "denied", True),
        ("running", "executing", True),
        ("running", "unknown", True),
    ],
)
def test_user_cancel_classifies_dispatch_and_commits_matching_terminal(
    completion_db,
    round_status,
    approval_status,
    outcome_uncertain,
):
    _add_waiting_round_with_approval(completion_db)
    round_obj = completion_db.get(Round, "round-1")
    interaction = completion_db.get(AgentInteraction, "interaction-1")
    approval = completion_db.get(ToolApprovalRequest, "interaction-1")
    round_obj.status = round_status
    if approval_status is None:
        completion_db.delete(approval)
        interaction.kind = "user_input"
    else:
        approval.status = approval_status
        if approval_status != "requested":
            approval.resolution = "allow_once"
    completion_db.commit()

    result = RunCompletionService(completion_db).cancel_user_run_sync(
        run_id="round-1",
        outcome_warning="dispatch outcome uncertain",
    )

    assert result.stored_event is not None
    assert result.outcome_uncertain is outcome_uncertain
    terminal_result = result.stored_event.event["result"]
    assert terminal_result["outcomeUncertain"] is outcome_uncertain
    assert terminal_result["finalResponse"] == result.final_response
    if outcome_uncertain:
        assert terminal_result["warning"] == "dispatch outcome uncertain"
    else:
        assert "warning" not in terminal_result
    completion_db.expire_all()
    assert completion_db.get(Round, "round-1").status == "cancelled"
    persisted_approval = completion_db.get(ToolApprovalRequest, "interaction-1")
    if approval_status is None:
        assert persisted_approval is None
    else:
        expected_approval_status = (
            "cancelled" if approval_status in {"requested", "approved"}
            else approval_status
        )
        assert persisted_approval.status == expected_approval_status


@pytest.mark.parametrize("round_status", ["failed", "max_steps_reached"])
def test_non_cancel_terminal_cancels_all_predispatch_approvals(
    completion_db,
    round_status,
):
    _add_waiting_round_with_approval(completion_db)
    approved = completion_db.get(ToolApprovalRequest, "interaction-1")
    approved.status = "approved"
    approved.resolution = "allow_once"
    approved.resolved_at = approved.requested_at
    completion_db.add(
        ToolApprovalRequest(
            id="approval-requested",
            user_id="user-1",
            session_id="session-1",
            run_id="round-1",
            tool_call_id="tool-call-requested",
            provider="builtin",
            tool_name="shell_exec",
            model_tool_name="shell_exec",
            arguments_encrypted="encrypted",
            arguments_hash="hash-requested",
            status="requested",
        )
    )
    completion_db.commit()

    RunCompletionService(completion_db).complete_sync(
        run_id="round-1",
        status=round_status,
    )

    completion_db.expire_all()
    assert completion_db.get(AgentInteraction, "interaction-1").status == "failed"
    assert completion_db.get(ToolApprovalRequest, "interaction-1").status == "cancelled"
    assert completion_db.get(ToolApprovalRequest, "approval-requested").status == "cancelled"


def test_answer_locks_round_before_interaction_on_postgresql():
    """A Round lock must block answer before it can lock Interaction.

    This reproduces the first half of the old cancel/resume deadlock. While the
    answer worker is blocked on Round, the Round owner can still lock the
    Interaction row with a short PostgreSQL lock timeout. If answer regresses
    to Interaction-first, this assertion times out (or deadlocks) deterministically.
    """

    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    create_all_for_test_engine(engine, Base.metadata)
    reset_all_tables(engine, Base.metadata)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with factory() as seed:
        seed.add(Session(id="session-lock-order", user_id="user-lock-order"))
        seed.commit()
        seed.add(
            Round(
                id="round-lock-order",
                session_id="session-lock-order",
                user_message="hello",
                status="waiting_interaction",
            )
        )
        seed.commit()
        seed.add(
            AgentInteraction(
                id="interaction-lock-order",
                session_id="session-lock-order",
                round_id="round-lock-order",
                kind="user_input",
                status="pending",
                request_payload="{}",
            )
        )
        seed.commit()

    contender_started = Event()

    def answer_in_contender() -> None:
        with factory() as contender:
            contender_started.set()
            AgentInteractionService.answer_pending(
                contender,
                session_id="session-lock-order",
                interaction_id="interaction-lock-order",
                answers={"Continue?": "Yes"},
                tool_result_content="Continue?: Yes",
            )

    try:
        with factory() as owner:
            owner.execute(text("SET LOCAL lock_timeout = '500ms'"))
            owner.query(Round).filter(Round.id == "round-lock-order").with_for_update().one()
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(answer_in_contender)
                assert contender_started.wait(timeout=2)
                with pytest.raises(FutureTimeoutError):
                    future.result(timeout=0.2)

                # The answer worker is waiting for Round and therefore cannot
                # already own this Interaction row.
                owner.query(AgentInteraction).filter(
                    AgentInteraction.id == "interaction-lock-order"
                ).with_for_update().one()
                owner.commit()
                future.result(timeout=5)
    finally:
        reset_all_tables(engine, Base.metadata)
        engine.dispose()


def test_concurrent_answer_and_cancel_do_not_deadlock_on_postgresql():
    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    create_all_for_test_engine(engine, Base.metadata)
    reset_all_tables(engine, Base.metadata)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        with factory() as seed:
            seed.add(Session(id="session-race", user_id="user-race"))
            seed.commit()
            seed.add(
                Round(
                    id="round-race",
                    session_id="session-race",
                    user_message="hello",
                    status="waiting_interaction",
                )
            )
            seed.commit()
            seed.add(
                AgentInteraction(
                    id="interaction-race",
                    session_id="session-race",
                    round_id="round-race",
                    kind="user_input",
                    status="pending",
                    request_payload="{}",
                )
            )
            seed.commit()

        barrier = Barrier(2)

        def answer() -> str:
            with factory() as db:
                barrier.wait(timeout=2)
                try:
                    AgentInteractionService.answer_pending(
                        db,
                        session_id="session-race",
                        interaction_id="interaction-race",
                        answers={"Continue?": "Yes"},
                        tool_result_content="Continue?: Yes",
                    )
                except InteractionConflictError:
                    return "cancel-won"
                return "answer-won"

        def cancel():
            barrier.wait(timeout=2)
            return RunCompletionService(factory).complete_sync(
                run_id="round-race",
                status="cancelled",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            answer_future = executor.submit(answer)
            cancel_future = executor.submit(cancel)
            answer_result = answer_future.result(timeout=5)
            cancel_result = cancel_future.result(timeout=5)

        assert answer_result in {"answer-won", "cancel-won"}
        assert cancel_result is not None
        with factory() as verify:
            assert verify.get(Round, "round-race").status == "cancelled"
            assert verify.get(AgentInteraction, "interaction-race").status == "cancelled"
    finally:
        reset_all_tables(engine, Base.metadata)
        engine.dispose()


def test_concurrent_approval_prepare_and_cancel_do_not_deadlock_on_postgresql():
    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    create_all_for_test_engine(engine, Base.metadata)
    reset_all_tables(engine, Base.metadata)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        with factory() as seed:
            seed.add(Session(id="session-approval-race", user_id="user-race"))
            seed.commit()
            seed.add(Round(
                id="round-approval-race",
                session_id="session-approval-race",
                user_message="hello",
                status="waiting_interaction",
            ))
            seed.commit()
            seed.add(AgentInteraction(
                id="interaction-approval-race",
                session_id="session-approval-race",
                round_id="round-approval-race",
                kind="tool_approval",
                tool_call_id="tool-call-race",
                status="pending",
                request_payload="{}",
            ))
            seed.commit()
            create_approval_request(
                seed,
                request_id="interaction-approval-race",
                user_id="user-race",
                session_id="session-approval-race",
                run_id="round-approval-race",
                tool_call_id="tool-call-race",
                ref=ToolRef(provider="builtin", tool_name="shell_exec"),
                model_tool_name="shell_exec",
                arguments={"command": "pwd"},
            )

        barrier = Barrier(2)

        def approve() -> str:
            with factory() as db:
                barrier.wait(timeout=2)
                try:
                    AgentInteractionService.lock_pending_for_update(
                        db,
                        session_id="session-approval-race",
                        interaction_id="interaction-approval-race",
                    )
                    AgentInteractionService.answer_pending(
                        db,
                        session_id="session-approval-race",
                        interaction_id="interaction-approval-race",
                        answers={"approval": "allow_once"},
                        tool_result_content="[Tool approval execution pending]",
                        commit=False,
                    )
                    prepare_approval_request(
                        db,
                        request_id="interaction-approval-race",
                        user_id="user-race",
                        resolution="allow_once",
                        commit=False,
                    )
                    db.commit()
                except InteractionConflictError:
                    db.rollback()
                    return "cancel-won"
                return "approval-won"

        def cancel():
            barrier.wait(timeout=2)
            return RunCompletionService(factory).complete_sync(
                run_id="round-approval-race",
                status="cancelled",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            approve_future = executor.submit(approve)
            cancel_future = executor.submit(cancel)
            approve_result = approve_future.result(timeout=5)
            cancel_result = cancel_future.result(timeout=5)

        assert approve_result in {"approval-won", "cancel-won"}
        assert cancel_result is not None
        with factory() as verify:
            assert verify.get(Round, "round-approval-race").status == "cancelled"
            assert (
                verify.get(AgentInteraction, "interaction-approval-race").status
                == "cancelled"
            )
            assert (
                verify.get(ToolApprovalRequest, "interaction-approval-race").status
                == "cancelled"
            )
    finally:
        reset_all_tables(engine, Base.metadata)
        engine.dispose()


def test_dispatch_and_user_cancel_commit_one_consistent_boundary_on_postgresql():
    """Two DB sessions must agree whether approved dispatch crossed its boundary."""

    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    create_all_for_test_engine(engine, Base.metadata)
    reset_all_tables(engine, Base.metadata)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        with factory() as seed:
            seed.add(Session(id="session-dispatch-race", user_id="user-dispatch-race"))
            seed.commit()
            seed.add(Round(
                id="round-dispatch-race",
                session_id="session-dispatch-race",
                user_message="hello",
                status="running",
            ))
            seed.commit()
            seed.add(AgentInteraction(
                id="interaction-dispatch-race",
                session_id="session-dispatch-race",
                round_id="round-dispatch-race",
                kind="tool_approval",
                tool_call_id="tool-call-dispatch-race",
                status="pending",
                request_payload="{}",
                answer_payload='{"approval":"allow_once"}',
                tool_result_content="[Tool approval execution pending]",
                claim_token="continuation-dispatch-race",
            ))
            seed.commit()
            create_approval_request(
                seed,
                request_id="interaction-dispatch-race",
                user_id="user-dispatch-race",
                session_id="session-dispatch-race",
                run_id="round-dispatch-race",
                tool_call_id="tool-call-dispatch-race",
                ref=ToolRef(provider="builtin", tool_name="shell_exec"),
                model_tool_name="shell_exec",
                arguments={"command": "pwd"},
            )
            prepare_approval_request(
                seed,
                request_id="interaction-dispatch-race",
                user_id="user-dispatch-race",
                resolution="allow_once",
            )

        barrier = Barrier(2)

        def dispatch() -> str:
            with factory() as db:
                barrier.wait(timeout=2)
                round_obj = (
                    db.query(Round)
                    .filter(Round.id == "round-dispatch-race")
                    .with_for_update()
                    .one()
                )
                if round_obj.status != "running":
                    db.rollback()
                    return "cancel-won"
                db.query(AgentInteraction).filter(
                    AgentInteraction.id == "interaction-dispatch-race"
                ).with_for_update().one()
                dispatch_approval_request(
                    db,
                    request_id="interaction-dispatch-race",
                    user_id="user-dispatch-race",
                    commit=False,
                )
                db.commit()
                return "dispatch-won"

        def cancel():
            barrier.wait(timeout=2)
            return RunCompletionService(factory).cancel_user_run_sync(
                run_id="round-dispatch-race",
                outcome_warning="dispatch outcome uncertain",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            dispatch_future = executor.submit(dispatch)
            cancel_future = executor.submit(cancel)
            dispatch_result = dispatch_future.result(timeout=5)
            cancel_result = cancel_future.result(timeout=5)

        assert cancel_result.stored_event is not None
        with factory() as verify:
            approval = verify.get(
                ToolApprovalRequest,
                "interaction-dispatch-race",
            )
            if dispatch_result == "dispatch-won":
                assert approval.status == "executing"
                assert cancel_result.outcome_uncertain is True
            else:
                assert approval.status == "cancelled"
                assert cancel_result.outcome_uncertain is False
            assert verify.get(Round, "round-dispatch-race").status == "cancelled"
            assert (
                cancel_result.stored_event.event["result"]["outcomeUncertain"]
                is cancel_result.outcome_uncertain
            )
    finally:
        reset_all_tables(engine, Base.metadata)
        engine.dispose()
