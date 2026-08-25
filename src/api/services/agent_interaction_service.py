"""Durable same-Round Human-in-the-Loop transitions."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.api.models.agent_interaction import AgentInteraction
from src.api.models.round import Round
from src.api.utils.timezone import now_naive


DEFAULT_CONTINUATION_LEASE_SECONDS = 60.0


class InteractionConflictError(RuntimeError):
    """The requested interaction transition conflicts with durable state."""


ContinuationFenceTransition = Literal["validate", "start", "complete"]


@dataclass(frozen=True)
class ContinuationWriteFence:
    """Opaque continuation ownership required for a shared-Round write.

    ``transition`` describes the state change that must commit in the same
    transaction as the durable write. Ephemeral stream deltas always downgrade
    to ``validate`` because they are not a recoverable completion boundary.
    """

    session_id: str
    interaction_id: str
    claim_token: str
    transition: ContinuationFenceTransition = "validate"


class AgentInteractionService:
    """Own durable interactions and their recoverable continuation claims.

    Every mutating transition locks rows in the same order:
    ``Round -> AgentInteraction``. Callers that also mutate a
    ``ToolApprovalRequest`` must call :meth:`lock_pending_for_update` first and
    then acquire the approval row, preserving the global
    ``Round -> AgentInteraction -> ToolApprovalRequest`` order.

    An accepted answer intentionally remains ``pending`` until the continuation
    reaches a durable start boundary. A worker first claims a lease, then the
    ``interaction_resolved`` event atomically marks both the Round running and
    ``continuation_started_at``. Only a pre-start claim can be reclaimed. Once
    that boundary is durable, a crashed worker must converge the Round to a
    durable failure instead of replaying the accepted answer.
    """

    @staticmethod
    def create_pending(
        db: DBSession,
        *,
        interaction_id: str,
        session_id: str,
        round_id: str,
        kind: str,
        tool_call_id: str | None,
        request_payload: dict[str, Any],
        step_count: int | None = None,
        commit: bool = True,
    ) -> AgentInteraction:
        round_obj = (
            db.query(Round)
            .filter(Round.id == round_id, Round.session_id == session_id)
            .with_for_update()
            .first()
        )
        if round_obj is None:
            AgentInteractionService._rollback_if_owned(db, commit)
            raise ValueError(f"Round not found: {round_id}")
        if round_obj.status != "running":
            AgentInteractionService._rollback_if_owned(db, commit)
            raise InteractionConflictError(
                f"Round cannot request interaction: {round_id} status={round_obj.status}"
            )

        interaction = AgentInteraction(
            id=interaction_id,
            session_id=session_id,
            round_id=round_id,
            kind=kind,
            tool_call_id=tool_call_id,
            status="pending",
            request_payload=json.dumps(request_payload, ensure_ascii=False),
        )
        round_obj.status = "waiting_interaction"
        if step_count is not None:
            round_obj.step_count = max(
                int(round_obj.step_count or 0),
                max(int(step_count), 0),
            )
        round_obj.completed_at = None
        db.add(interaction)
        try:
            if commit:
                db.commit()
            else:
                db.flush()
        except IntegrityError as exc:
            db.rollback()
            existing = (
                db.query(AgentInteraction)
                .filter(
                    AgentInteraction.session_id == session_id,
                    AgentInteraction.round_id == round_id,
                    AgentInteraction.status == "pending",
                )
                .first()
            )
            if existing is not None and existing.id == interaction_id:
                db.expunge(existing)
                db.rollback()
                return existing
            raise InteractionConflictError(
                f"Round already has a pending interaction: {round_id}"
            ) from exc
        if not commit:
            return interaction
        db.refresh(interaction)
        db.expunge(interaction)
        db.rollback()
        return interaction

    @staticmethod
    def load_pending(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str | None = None,
    ) -> AgentInteraction | None:
        query = db.query(AgentInteraction).filter(
            AgentInteraction.session_id == session_id,
            AgentInteraction.status == "pending",
        )
        if interaction_id is not None:
            query = query.filter(AgentInteraction.id == interaction_id)
        interaction = query.order_by(AgentInteraction.created_at.desc()).first()
        # Small unit-test DB doubles may return an object configured for a
        # different query regardless of the requested model.
        if interaction is not None and not isinstance(interaction, AgentInteraction):
            return None
        if interaction is not None:
            db.expunge(interaction)
        return interaction

    @staticmethod
    def lock_pending_for_update(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str,
    ) -> tuple[Round, AgentInteraction]:
        """Lock a pending interaction using the canonical Round-first order.

        This public lock primitive is for compound transactions such as tool
        approval resolution. It never commits or rolls back; the caller owns
        the surrounding transaction.
        """

        round_obj, interaction = AgentInteractionService._lock_round_then_interaction(
            db,
            session_id=session_id,
            interaction_id=interaction_id,
        )
        if interaction.status != "pending":
            raise InteractionConflictError(
                f"Interaction is not pending: {interaction_id} status={interaction.status}"
            )
        return round_obj, interaction

    @staticmethod
    def answer_pending(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str,
        answers: dict[str, str],
        tool_result_content: str | None = None,
        commit: bool = True,
    ) -> AgentInteraction:
        round_obj, interaction = AgentInteractionService._lock_round_then_interaction(
            db,
            session_id=session_id,
            interaction_id=interaction_id,
            rollback_on_error=commit,
        )
        if interaction.status != "pending":
            AgentInteractionService._rollback_if_owned(db, commit)
            raise InteractionConflictError(
                f"Interaction is not answerable: {interaction_id} status={interaction.status}"
            )
        if round_obj.status != "waiting_interaction":
            AgentInteractionService._rollback_if_owned(db, commit)
            raise InteractionConflictError(
                f"Round is not waiting for interaction: {round_obj.id} status={round_obj.status}"
            )

        ordered_answers = AgentInteractionService.order_answers_for_request(
            answers,
            AgentInteractionService.request_payload(interaction),
        )
        answer_json = json.dumps(ordered_answers, ensure_ascii=False)
        if interaction.kind == "user_input":
            tool_result_content = AgentInteractionService.format_user_input_tool_result(
                ordered_answers
            )
        elif tool_result_content is None:
            AgentInteractionService._rollback_if_owned(db, commit)
            raise ValueError("tool approval answer requires tool_result_content")

        if interaction.answer_payload is not None:
            existing_answers = AgentInteractionService.answer_payload(interaction)
            if existing_answers != ordered_answers:
                AgentInteractionService._rollback_if_owned(db, commit)
                raise InteractionConflictError(
                    f"Interaction already has a different answer: {interaction_id}"
                )
            # Reuse the first durable rendering for old rows as well as new
            # canonical rows. This preserves retry idempotence across deploys.
            return AgentInteractionService._finish(db, interaction, commit=commit)

        interaction.answer_payload = answer_json
        interaction.tool_result_content = tool_result_content
        interaction.updated_at = now_naive()
        return AgentInteractionService._finish(db, interaction, commit=commit)

    @staticmethod
    def fence_continuation_write(
        db: DBSession,
        *,
        run_id: str,
        fence: ContinuationWriteFence,
        durable: bool = True,
        occurred_at: datetime | None = None,
        commit: bool = False,
    ) -> AgentInteraction:
        """Validate ownership and optionally cross a boundary with one write.

        Callers must invoke this inside the transaction that persists the
        associated AG-UI event or terminal state. The canonical lock order is
        preserved by :meth:`_lock_round_then_interaction`.
        """

        transition: ContinuationFenceTransition = (
            fence.transition if durable else "validate"
        )
        if transition not in {"validate", "start", "complete"}:
            AgentInteractionService._rollback_if_owned(db, commit)
            raise ValueError(f"Unsupported continuation fence transition: {transition}")

        current_time = occurred_at or now_naive()
        try:
            round_obj, interaction = AgentInteractionService._lock_round_then_interaction(
                db,
                session_id=fence.session_id,
                interaction_id=fence.interaction_id,
                rollback_on_error=commit,
            )
        except ValueError as exc:
            AgentInteractionService._rollback_if_owned(db, commit)
            raise InteractionConflictError(
                f"Continuation ownership no longer exists: {fence.interaction_id}"
            ) from exc

        if round_obj.id != run_id:
            AgentInteractionService._rollback_if_owned(db, commit)
            raise InteractionConflictError(
                f"Continuation Round changed: {fence.interaction_id}"
            )
        AgentInteractionService._require_owned_claim(
            db,
            interaction,
            claim_token=fence.claim_token,
            current_time=current_time,
            require_live_lease=transition == "start",
            rollback_on_error=commit,
        )

        if transition == "start":
            if round_obj.status != "waiting_interaction":
                AgentInteractionService._rollback_if_owned(db, commit)
                raise InteractionConflictError(
                    f"Round cannot start continuation: {round_obj.id} "
                    f"status={round_obj.status}"
                )
            if interaction.continuation_started_at is not None:
                AgentInteractionService._rollback_if_owned(db, commit)
                raise InteractionConflictError(
                    f"Interaction continuation already started: {interaction.id}"
                )
            round_obj.status = "running"
            round_obj.completed_at = None
            interaction.continuation_started_at = current_time
            interaction.updated_at = current_time
        elif transition == "complete":
            if round_obj.status != "running":
                AgentInteractionService._rollback_if_owned(db, commit)
                raise InteractionConflictError(
                    f"Round continuation is not running: {round_obj.id} "
                    f"status={round_obj.status}"
                )
            interaction.status = "answered"
            interaction.resolved_at = current_time
            interaction.claim_token = None
            interaction.claim_lease_expires_at = None
            interaction.updated_at = current_time
        elif round_obj.status not in {"waiting_interaction", "running"}:
            AgentInteractionService._rollback_if_owned(db, commit)
            raise InteractionConflictError(
                f"Round no longer accepts continuation writes: {round_obj.id} "
                f"status={round_obj.status}"
            )

        return AgentInteractionService._finish(db, interaction, commit=commit)

    @staticmethod
    def claim_answered_continuation(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str,
        lease_seconds: float = DEFAULT_CONTINUATION_LEASE_SECONDS,
        claimed_at: datetime | None = None,
        commit: bool = True,
    ) -> AgentInteraction:
        """Lease an answered interaction without consuming it.

        The Round remains ``waiting_interaction``. Only a claim whose durable
        start boundary has not been crossed may be acquired or reclaimed.
        """

        seconds = AgentInteractionService._positive_lease_seconds(lease_seconds)
        current_time = claimed_at or now_naive()
        round_obj, interaction = AgentInteractionService._lock_round_then_interaction(
            db,
            session_id=session_id,
            interaction_id=interaction_id,
            rollback_on_error=commit,
        )
        AgentInteractionService._require_claimable_answer(
            db,
            interaction,
            rollback_on_error=commit,
        )
        if (
            round_obj.status != "waiting_interaction"
            or interaction.continuation_started_at is not None
        ):
            AgentInteractionService._rollback_if_owned(db, commit)
            raise InteractionConflictError(
                f"Interaction continuation already started or closed: "
                f"{interaction.id} round_status={round_obj.status}"
            )

        lease_is_active = bool(
            interaction.claim_token
            and interaction.claim_lease_expires_at is not None
            and interaction.claim_lease_expires_at > current_time
        )
        if lease_is_active:
            AgentInteractionService._rollback_if_owned(db, commit)
            raise InteractionConflictError(
                f"Interaction continuation is already claimed: {interaction_id}"
            )

        interaction.claim_token = uuid.uuid4().hex
        interaction.claim_lease_expires_at = current_time + timedelta(seconds=seconds)
        interaction.updated_at = current_time
        round_obj.completed_at = None
        return AgentInteractionService._finish(db, interaction, commit=commit)

    @staticmethod
    def mark_continuation_started(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str,
        claim_token: str,
        started_at: datetime | None = None,
        commit: bool = True,
    ) -> AgentInteraction:
        """Mark the Round running after the leased worker is ready to start."""

        round_id = AgentInteractionService._round_id_for_interaction(
            db,
            session_id=session_id,
            interaction_id=interaction_id,
            rollback_on_error=commit,
        )
        return AgentInteractionService.fence_continuation_write(
            db,
            run_id=round_id,
            fence=ContinuationWriteFence(
                session_id=session_id,
                interaction_id=interaction_id,
                claim_token=claim_token,
                transition="start",
            ),
            occurred_at=started_at,
            commit=commit,
        )

    @staticmethod
    def renew_continuation_claim(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str,
        claim_token: str,
        lease_seconds: float = DEFAULT_CONTINUATION_LEASE_SECONDS,
        renewed_at: datetime | None = None,
        commit: bool = True,
    ) -> AgentInteraction:
        """Renew ownership; the token fences a worker after any reclaim."""

        seconds = AgentInteractionService._positive_lease_seconds(lease_seconds)
        current_time = renewed_at or now_naive()
        _round_obj, interaction = AgentInteractionService._lock_round_then_interaction(
            db,
            session_id=session_id,
            interaction_id=interaction_id,
            rollback_on_error=commit,
        )
        # Renewal may win just after the timestamp elapsed, as long as a
        # reclaimer has not replaced the token first.
        AgentInteractionService._require_owned_claim(
            db,
            interaction,
            claim_token=claim_token,
            current_time=current_time,
            require_live_lease=False,
            rollback_on_error=commit,
        )
        interaction.claim_lease_expires_at = current_time + timedelta(seconds=seconds)
        interaction.updated_at = current_time
        return AgentInteractionService._finish(db, interaction, commit=commit)

    @staticmethod
    def complete_continuation(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str,
        claim_token: str,
        completed_at: datetime | None = None,
        commit: bool = True,
    ) -> AgentInteraction:
        """Consume the answer only after it has crossed a durable start point."""

        round_id = AgentInteractionService._round_id_for_interaction(
            db,
            session_id=session_id,
            interaction_id=interaction_id,
            rollback_on_error=commit,
        )
        return AgentInteractionService.fence_continuation_write(
            db,
            run_id=round_id,
            fence=ContinuationWriteFence(
                session_id=session_id,
                interaction_id=interaction_id,
                claim_token=claim_token,
                transition="complete",
            ),
            occurred_at=completed_at,
            commit=commit,
        )

    @staticmethod
    def release_continuation_claim(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str,
        claim_token: str,
        released_at: datetime | None = None,
        commit: bool = True,
    ) -> AgentInteraction:
        """Release a failed startup and restore the durable waiting state."""

        current_time = released_at or now_naive()
        round_obj, interaction = AgentInteractionService._lock_round_then_interaction(
            db,
            session_id=session_id,
            interaction_id=interaction_id,
            rollback_on_error=commit,
        )
        AgentInteractionService._require_owned_claim(
            db,
            interaction,
            claim_token=claim_token,
            current_time=current_time,
            require_live_lease=False,
            rollback_on_error=commit,
        )
        if (
            round_obj.status != "waiting_interaction"
            or interaction.continuation_started_at is not None
        ):
            AgentInteractionService._rollback_if_owned(db, commit)
            raise InteractionConflictError(
                f"Round cannot release started continuation: {round_obj.id} "
                f"status={round_obj.status}"
            )
        round_obj.completed_at = None
        interaction.claim_token = None
        interaction.claim_lease_expires_at = None
        interaction.updated_at = current_time
        return AgentInteractionService._finish(db, interaction, commit=commit)

    @staticmethod
    def repark_expired_continuation_claims(
        db: DBSession,
        *,
        session_id: str,
        now: datetime | None = None,
    ) -> int:
        """Recover expired continuations that cannot have external side effects."""
        from src.api.models.tool_permission import ToolApprovalRequest
        from src.api.services.tool_permission_service import (
            APPROVAL_CONTINUATION_RESUMABLE_STATUSES,
        )

        current_time = now or now_naive()
        candidate_ids = [
            str(row[0])
            for row in (
                db.query(AgentInteraction.id)
                .filter(
                    AgentInteraction.session_id == session_id,
                    AgentInteraction.status == "pending",
                    AgentInteraction.answer_payload.isnot(None),
                    AgentInteraction.continuation_started_at.is_(None),
                    or_(
                        AgentInteraction.claim_token.is_(None),
                        AgentInteraction.claim_lease_expires_at.is_(None),
                        AgentInteraction.claim_lease_expires_at <= current_time,
                    ),
                )
                .order_by(AgentInteraction.id.asc())
                .all()
            )
            if isinstance(row[0], str) and row[0]
        ]
        recovered = 0
        for interaction_id in candidate_ids:
            round_obj, interaction = AgentInteractionService._lock_round_then_interaction(
                db,
                session_id=session_id,
                interaction_id=interaction_id,
            )
            if (
                interaction.status != "pending"
                or interaction.answer_payload is None
                or (
                    round_obj.status == "waiting_interaction"
                    and interaction.claim_token is None
                    and interaction.claim_lease_expires_at is None
                )
                or (
                    interaction.claim_token is not None
                    and interaction.claim_lease_expires_at is not None
                    and interaction.claim_lease_expires_at > current_time
                )
                or round_obj.status != "waiting_interaction"
                or interaction.continuation_started_at is not None
            ):
                continue
            if interaction.kind == "tool_approval":
                approval = (
                    db.query(ToolApprovalRequest)
                    .filter(
                        ToolApprovalRequest.id == interaction.id,
                        ToolApprovalRequest.session_id == session_id,
                        ToolApprovalRequest.run_id == round_obj.id,
                    )
                    .with_for_update()
                    .first()
                )
                if (
                    approval is None
                    or approval.status
                    not in APPROVAL_CONTINUATION_RESUMABLE_STATUSES
                ):
                    continue
            elif interaction.kind != "user_input":
                continue
            round_obj.completed_at = None
            interaction.claim_token = None
            interaction.claim_lease_expires_at = None
            interaction.updated_at = current_time
            recovered += 1
        if recovered:
            db.commit()
        else:
            db.rollback()
        return recovered

    @staticmethod
    def lock_running_round_for_terminal_cleanup(
        db: DBSession,
        *,
        session_id: str,
        round_id: str,
    ) -> Round | None:
        """Lock one running Round before terminal orphan cleanup."""
        return (
            db.query(Round)
            .filter(
                Round.id == round_id,
                Round.status == "running",
                or_(Round.session_id == session_id, Round.thread_id == session_id),
            )
            .with_for_update()
            .first()
        )

    @staticmethod
    def load_irrecoverable_continuation_round_ids(
        db: DBSession,
        *,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """Find expired continuations beyond their durable start boundary."""
        from src.api.models.tool_permission import ToolApprovalRequest

        current_time = now or now_naive()
        query = (
            db.query(AgentInteraction.round_id)
            .join(Round, Round.id == AgentInteraction.round_id)
            .outerjoin(
                ToolApprovalRequest,
                ToolApprovalRequest.id == AgentInteraction.id,
            )
            .filter(
                AgentInteraction.status == "pending",
                AgentInteraction.answer_payload.isnot(None),
                Round.status.in_(("running", "waiting_interaction")),
                or_(
                    AgentInteraction.continuation_started_at.isnot(None),
                    Round.status == "running",
                    and_(
                        AgentInteraction.kind == "tool_approval",
                        ToolApprovalRequest.status.in_(
                            ("executing", "executed", "failed", "unknown")
                        ),
                    ),
                ),
                or_(
                    AgentInteraction.claim_token.is_(None),
                    AgentInteraction.claim_lease_expires_at.is_(None),
                    AgentInteraction.claim_lease_expires_at <= current_time,
                ),
            )
            .order_by(AgentInteraction.round_id.asc())
        )
        if session_id is not None:
            query = query.filter(AgentInteraction.session_id == session_id)
        rows = query.all()
        return sorted({
            str(row[0])
            for row in rows
            if isinstance(row[0], str) and row[0]
        })

    @staticmethod
    def has_active_continuation_work(
        db: DBSession,
        *,
        round_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Protect live orchestration and dispatched-tool ownership leases.

        This helper owns the canonical ``Round -> AgentInteraction ->
        ToolApprovalRequest`` lock order so stale-lock and startup recovery
        cannot disagree about whether a running continuation is still live.
        """
        from src.api.models.tool_permission import ToolApprovalRequest

        current_time = now or now_naive()
        round_obj = (
            db.query(Round)
            .filter(
                Round.id == round_id,
                Round.status.in_(("running", "waiting_interaction")),
            )
            .with_for_update()
            .first()
        )
        if round_obj is None:
            return False
        interaction = (
            db.query(AgentInteraction)
            .filter(
                AgentInteraction.round_id == round_id,
                AgentInteraction.status == "pending",
                AgentInteraction.answer_payload.isnot(None),
            )
            .order_by(AgentInteraction.id.asc())
            .with_for_update()
            .first()
        )
        if interaction is None:
            return False
        if (
            interaction.claim_token is not None
            and interaction.claim_lease_expires_at is not None
            and interaction.claim_lease_expires_at > current_time
        ):
            return True
        if interaction.kind != "tool_approval":
            return False
        approval = (
            db.query(ToolApprovalRequest)
            .filter(
                ToolApprovalRequest.id == interaction.id,
                ToolApprovalRequest.run_id == round_id,
            )
            .with_for_update()
            .first()
        )
        return bool(
            approval is not None
            and approval.status == "executing"
            and approval.execution_claim_token is not None
            and approval.execution_lease_expires_at is not None
            and approval.execution_lease_expires_at > current_time
        )

    @staticmethod
    def lock_expired_prestart_continuation_for_recovery(
        db: DBSession,
        *,
        round_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Fence an expired claim that never crossed ``interaction_resolved``.

        The caller owns commit/rollback. Returning ``True`` means the Round is
        a safe pre-start waiting continuation and no old claim token can renew
        after the caller commits. Started or dispatch-crossed work deliberately
        returns ``False`` so the irrecoverable failure path can handle it.
        """
        from src.api.models.tool_permission import ToolApprovalRequest
        from src.api.services.tool_permission_service import (
            APPROVAL_CONTINUATION_RESUMABLE_STATUSES,
        )

        current_time = now or now_naive()
        round_obj = (
            db.query(Round)
            .filter(
                Round.id == round_id,
                Round.status == "waiting_interaction",
            )
            .with_for_update()
            .first()
        )
        if round_obj is None:
            return False
        interaction = (
            db.query(AgentInteraction)
            .filter(
                AgentInteraction.round_id == round_id,
                AgentInteraction.status == "pending",
                AgentInteraction.answer_payload.isnot(None),
            )
            .order_by(AgentInteraction.id.asc())
            .with_for_update()
            .first()
        )
        if interaction is None or interaction.continuation_started_at is not None:
            return False
        if (
            interaction.claim_token is not None
            and interaction.claim_lease_expires_at is not None
            and interaction.claim_lease_expires_at > current_time
        ):
            return False
        if interaction.kind == "tool_approval":
            approval = (
                db.query(ToolApprovalRequest)
                .filter(
                    ToolApprovalRequest.id == interaction.id,
                    ToolApprovalRequest.run_id == round_id,
                )
                .with_for_update()
                .first()
            )
            if (
                approval is None
                or approval.status not in APPROVAL_CONTINUATION_RESUMABLE_STATUSES
            ):
                return False
        elif interaction.kind != "user_input":
            return False

        round_obj.completed_at = None
        interaction.claim_token = None
        interaction.claim_lease_expires_at = None
        interaction.updated_at = current_time
        return True

    @staticmethod
    def lock_irrecoverable_continuation_round_for_failure(
        db: DBSession,
        *,
        round_id: str,
        now: datetime | None = None,
    ) -> str | None:
        """Revalidate one started crash under canonical row locks.

        The returned interaction kind lets the caller choose a user-facing
        failure message. An active tool execution lease remains authoritative;
        an expired one becomes ``unknown`` before the Round is failed, so no
        recovery path can replay its external side effect.
        """
        from src.api.models.tool_permission import ToolApprovalRequest
        from src.api.services.tool_permission_service import (
            APPROVAL_OUTCOME_UNKNOWN_ERROR,
        )

        current_time = now or now_naive()
        round_obj = (
            db.query(Round)
            .filter(
                Round.id == round_id,
                Round.status.in_(("running", "waiting_interaction")),
            )
            .with_for_update()
            .first()
        )
        if round_obj is None:
            return None
        interaction = (
            db.query(AgentInteraction)
            .filter(
                AgentInteraction.round_id == round_id,
                AgentInteraction.status == "pending",
                AgentInteraction.answer_payload.isnot(None),
            )
            .order_by(AgentInteraction.id.asc())
            .with_for_update()
            .first()
        )
        if interaction is None:
            return None
        claim_is_active = bool(
            interaction.claim_token
            and interaction.claim_lease_expires_at is not None
            and interaction.claim_lease_expires_at > current_time
        )
        if claim_is_active:
            return None

        approval = None
        if interaction.kind == "tool_approval":
            approval = (
                db.query(ToolApprovalRequest)
                .filter(
                    ToolApprovalRequest.id == interaction.id,
                    ToolApprovalRequest.run_id == round_id,
                )
                .with_for_update()
                .first()
            )
        crossed_start = bool(
            interaction.continuation_started_at is not None
            or round_obj.status == "running"
            or (
                approval is not None
                and approval.status in {"executing", "executed", "failed", "unknown"}
            )
        )
        if not crossed_start:
            return None

        if approval is not None and approval.status == "executing":
            execution_lease_is_active = bool(
                approval.execution_claim_token
                and approval.execution_lease_expires_at is not None
                and approval.execution_lease_expires_at > current_time
            )
            if execution_lease_is_active:
                return None
            approval.status = "unknown"
            approval.error = APPROVAL_OUTCOME_UNKNOWN_ERROR
            approval.completed_at = current_time
            approval.execution_claim_token = None
            approval.execution_lease_expires_at = None

        return str(interaction.kind)

    @staticmethod
    def request_payload(interaction: AgentInteraction) -> dict[str, Any]:
        try:
            value = json.loads(interaction.request_payload)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def answer_payload(interaction: AgentInteraction) -> dict[str, str]:
        try:
            value = json.loads(getattr(interaction, "answer_payload", None) or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(question): str(answer)
            for question, answer in value.items()
        }

    @staticmethod
    def order_answers_for_request(
        answers: dict[str, str],
        request_payload: dict[str, Any],
    ) -> dict[str, str]:
        """Order answers by the immutable persisted question definition.

        Unknown keys are retained in lexical order for compatibility with old
        interactions whose request payload omitted questions. Their ordering
        is still deterministic and never depends on JSON object insertion.
        """

        nested = request_payload.get("payload")
        payload = nested if isinstance(nested, dict) else request_payload
        questions = payload.get("questions")
        definitions = questions if isinstance(questions, list) else []

        ordered: dict[str, str] = {}
        for item in definitions:
            if not isinstance(item, dict):
                continue
            question = item.get("question")
            if (
                isinstance(question, str)
                and question in answers
                and question not in ordered
            ):
                ordered[question] = answers[question]
        for question in sorted(
            (key for key in answers if key not in ordered),
            key=str,
        ):
            ordered[question] = answers[question]
        return ordered

    @staticmethod
    def format_user_input_tool_result(answers: dict[str, str]) -> str:
        if not answers:
            return "User provided no answers."
        return "User answered:\n" + "\n".join(
            f"- {question}: {answer}"
            for question, answer in answers.items()
        )

    @staticmethod
    def _round_id_for_interaction(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str,
        rollback_on_error: bool,
    ) -> str:
        row = (
            db.query(AgentInteraction.round_id)
            .filter(
                AgentInteraction.id == interaction_id,
                AgentInteraction.session_id == session_id,
            )
            .first()
        )
        round_id = (
            row[0]
            if isinstance(row, tuple)
            else getattr(row, "round_id", None)
        )
        if not isinstance(round_id, str) or not round_id:
            if rollback_on_error:
                db.rollback()
            raise ValueError(f"Pending interaction not found: {interaction_id}")
        return round_id

    @staticmethod
    def _lock_round_then_interaction(
        db: DBSession,
        *,
        session_id: str,
        interaction_id: str,
        rollback_on_error: bool = False,
    ) -> tuple[Round, AgentInteraction]:
        # This first lookup is intentionally non-locking. round_id is immutable;
        # it lets every mutation acquire the shared Round mutex before the
        # interaction row and avoids the resume/cancel PostgreSQL deadlock.
        round_id_row = (
            db.query(AgentInteraction.round_id)
            .filter(
                AgentInteraction.id == interaction_id,
                AgentInteraction.session_id == session_id,
            )
            .first()
        )
        if round_id_row is None:
            if rollback_on_error:
                db.rollback()
            raise ValueError(f"Pending interaction not found: {interaction_id}")
        round_id = (
            round_id_row[0]
            if isinstance(round_id_row, tuple)
            else getattr(round_id_row, "round_id", None)
        )
        if not isinstance(round_id, str) or not round_id:
            if rollback_on_error:
                db.rollback()
            raise ValueError(f"Pending interaction has no Round: {interaction_id}")

        round_obj = (
            db.query(Round)
            .filter(Round.id == round_id, Round.session_id == session_id)
            .with_for_update()
            .first()
        )
        if round_obj is None:
            if rollback_on_error:
                db.rollback()
            raise ValueError(f"Round not found: {round_id}")
        interaction = (
            db.query(AgentInteraction)
            .filter(
                AgentInteraction.id == interaction_id,
                AgentInteraction.session_id == session_id,
                AgentInteraction.round_id == round_id,
            )
            .with_for_update()
            .first()
        )
        if interaction is None:
            if rollback_on_error:
                db.rollback()
            raise ValueError(f"Pending interaction not found: {interaction_id}")
        return round_obj, interaction

    @staticmethod
    def _require_claimable_answer(
        db: DBSession,
        interaction: AgentInteraction,
        *,
        rollback_on_error: bool,
    ) -> None:
        if interaction.status != "pending" or interaction.answer_payload is None:
            if rollback_on_error:
                db.rollback()
            raise InteractionConflictError(
                f"Interaction has no claimable answer: {interaction.id} "
                f"status={interaction.status}"
            )

    @staticmethod
    def _require_owned_claim(
        db: DBSession,
        interaction: AgentInteraction,
        *,
        claim_token: str,
        current_time: datetime,
        require_live_lease: bool,
        rollback_on_error: bool,
    ) -> None:
        AgentInteractionService._require_claimable_answer(
            db,
            interaction,
            rollback_on_error=rollback_on_error,
        )
        if not claim_token or interaction.claim_token != claim_token:
            if rollback_on_error:
                db.rollback()
            raise InteractionConflictError(
                f"Interaction continuation claim token does not match: {interaction.id}"
            )
        if (
            require_live_lease
            and (
                interaction.claim_lease_expires_at is None
                or interaction.claim_lease_expires_at <= current_time
            )
        ):
            if rollback_on_error:
                db.rollback()
            raise InteractionConflictError(
                f"Interaction continuation lease expired: {interaction.id}"
            )

    @staticmethod
    def _positive_lease_seconds(value: float) -> float:
        seconds = float(value)
        if seconds <= 0:
            raise ValueError("continuation lease_seconds must be positive")
        return seconds

    @staticmethod
    def _finish(
        db: DBSession,
        interaction: AgentInteraction,
        *,
        commit: bool,
    ) -> AgentInteraction:
        if not commit:
            db.flush()
            return interaction
        db.commit()
        db.refresh(interaction)
        db.expunge(interaction)
        # Release the read transaction opened by refresh on PostgreSQL.
        db.rollback()
        return interaction

    @staticmethod
    def _rollback_if_owned(db: DBSession, commit: bool) -> None:
        if commit:
            db.rollback()
