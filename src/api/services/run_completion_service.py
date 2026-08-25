"""Single entry point for AG-UI stream terminal completion."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.agent.schema.agui_events import (
    AGUIEvent,
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
)
from src.api.models.agui_event import AGUIEventLog
from src.api.models.agent_interaction import AgentInteraction
from src.api.models.round import Round
from src.api.models.tool_permission import ToolApprovalRequest
from src.api.services.agent_interaction_service import (
    AgentInteractionService,
    ContinuationWriteFence,
    InteractionConflictError,
)
from src.api.services.agui_event_bus import AguiEventBus, StoredEvent, get_agui_event_bus
from src.api.services.tool_permission_service import APPROVAL_CANCELLABLE_STATUSES
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunCancellationResult:
    """Authoritative result of the user-cancellation transaction."""

    stored_event: StoredEvent | None
    outcome_uncertain: bool
    final_response: str


class RunCompletionService:
    """Persist Round terminal state and terminal event atomically."""

    _TERMINAL_EVENT_TYPES = (EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value)

    def __init__(self, db: DBSession | Callable[[], DBSession]):
        if callable(db) and not hasattr(db, "query"):
            self._session_factory: Callable[[], DBSession] | None = db
            self._db: DBSession | None = None
        else:
            self._session_factory = None
            self._db = db  # type: ignore[assignment]

    @contextmanager
    def _session_scope(self):
        if self._session_factory is None:
            yield self._db
            return
        db = self._session_factory()
        try:
            yield db
        finally:
            db.close()

    async def complete(
        self,
        run_id: str,
        status: str,
        final_response: str | None = None,
        step_count: int | None = None,
        terminal_event: AGUIEvent | dict[str, Any] | None = None,
        continuation_fence: ContinuationWriteFence | None = None,
    ) -> StoredEvent | None:
        """Async facade that also fans out the committed terminal event."""
        stored = self.complete_sync(
            run_id=run_id,
            status=status,
            final_response=final_response,
            step_count=step_count,
            terminal_event=terminal_event,
            continuation_fence=continuation_fence,
        )
        if stored is not None:
            await get_agui_event_bus().publish_committed(run_id, stored.event)
        return stored

    async def cancel_user_run(
        self,
        *,
        run_id: str,
        outcome_warning: str,
        safe_final_response: str = "Cancelled",
    ) -> RunCancellationResult:
        """Cancel one Round and classify dispatch risk in the same transaction.

        Lock order is always ``Round -> AgentInteraction -> ToolApprovalRequest``.
        The approval dispatch boundary uses the same order, so either dispatch
        commits ``executing`` first and this transaction reports an uncertain
        outcome, or cancellation commits ``cancelled`` first and dispatch is
        rejected by its conditional ``approved -> executing`` update.
        """

        result = self.cancel_user_run_sync(
            run_id=run_id,
            outcome_warning=outcome_warning,
            safe_final_response=safe_final_response,
        )
        if result.stored_event is not None:
            await get_agui_event_bus().publish_committed(
                run_id,
                result.stored_event.event,
            )
        return result

    def cancel_user_run_sync(
        self,
        *,
        run_id: str,
        outcome_warning: str,
        safe_final_response: str = "Cancelled",
    ) -> RunCancellationResult:
        with self._session_scope() as db:
            if db is None:
                raise RuntimeError("RunCompletionService has no DB session")
            return self._cancel_user_run_in_session(
                db,
                run_id=run_id,
                outcome_warning=outcome_warning,
                safe_final_response=safe_final_response,
            )

    def complete_sync(
        self,
        *,
        run_id: str,
        status: str,
        final_response: str | None = None,
        step_count: int | None = None,
        terminal_event: AGUIEvent | dict[str, Any] | None = None,
        continuation_fence: ContinuationWriteFence | None = None,
    ) -> StoredEvent | None:
        if status not in {
            "completed",
            "failed",
            "cancelled",
            "max_steps_reached",
        }:
            raise ValueError(f"Unsupported terminal status: {status}")

        with self._session_scope() as db:
            if db is None:
                raise RuntimeError("RunCompletionService has no DB session")
            return self._complete_in_session(
                db,
                run_id=run_id,
                status=status,
                final_response=final_response,
                step_count=step_count,
                terminal_event=terminal_event,
                continuation_fence=continuation_fence,
            )

    def ensure_terminal_sync(self, run_id: str) -> StoredEvent | None:
        """Create a missing terminal event for an already terminal round."""
        with self._session_scope() as db:
            if db is None:
                raise RuntimeError("RunCompletionService has no DB session")
            round_obj = db.query(Round).filter(Round.id == run_id).first()
            if not round_obj or round_obj.status not in Round.SUBSCRIBE_TERMINAL_STATUSES:
                db.rollback()
                return None
            self._lock_round_row_if_possible(db, round_obj)
            existing = self._existing_terminal_event(db, run_id)
            if existing is not None:
                changed = self._converge_pending_human_work(
                    db,
                    run_id=run_id,
                    status=round_obj.status,
                )
                if changed:
                    db.commit()
                else:
                    db.rollback()
                return existing
            status = round_obj.status
            self._converge_pending_human_work(db, run_id=run_id, status=status)
            event_payload = self._build_terminal_payload(
                round_obj,
                status=status,
                final_response=round_obj.final_response,
                step_count=round_obj.step_count,
                terminal_event=None,
            )
            sequence = self._current_high_water(db, run_id) + 1
            event_payload["sequence"] = sequence
            event_log = AGUIEventLog(
                run_id=run_id,
                event_type=str(event_payload.get("type") or ""),
                payload=json.dumps(event_payload, ensure_ascii=False),
                sequence=sequence,
                timestamp=event_payload.get("timestamp"),
            )
            db.add(event_log)
            db.commit()
            AguiEventBus._terminal_runs.add(run_id)
            return StoredEvent(run_id=run_id, sequence=sequence, event=event_payload)

    def _complete_in_session(
        self,
        db: DBSession,
        *,
        run_id: str,
        status: str,
        final_response: str | None,
        step_count: int | None,
        terminal_event: AGUIEvent | dict[str, Any] | None = None,
        continuation_fence: ContinuationWriteFence | None = None,
    ) -> StoredEvent | None:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                round_obj = db.query(Round).filter(Round.id == run_id).first()
                if not round_obj:
                    db.rollback()
                    return None
                self._lock_round_row_if_possible(db, round_obj)
                if continuation_fence is not None:
                    AgentInteractionService.fence_continuation_write(
                        db,
                        run_id=run_id,
                        fence=continuation_fence,
                        durable=True,
                        commit=False,
                    )

                existing = self._existing_terminal_event(db, run_id)
                if existing is not None and round_obj.status in Round.SUBSCRIBE_TERMINAL_STATUSES:
                    changed = self._converge_pending_human_work(
                        db,
                        run_id=run_id,
                        status=round_obj.status,
                    )
                    if changed:
                        db.commit()
                    else:
                        db.rollback()
                    return existing

                if round_obj.status in Round.COMPLETE_TERMINAL_STATUSES:
                    human_work_changed = self._converge_pending_human_work(
                        db,
                        run_id=run_id,
                        status=round_obj.status,
                    )
                    logger.info(
                        "Round %s 已处于终态 %s，跳过 complete(status=%s)",
                        run_id,
                        round_obj.status,
                        status,
                    )
                    if human_work_changed:
                        db.commit()
                    else:
                        db.rollback()
                    return existing

                self._apply_round_status(
                    round_obj,
                    status=status,
                    final_response=final_response,
                    step_count=step_count,
                )
                if status in Round.COMPLETE_TERMINAL_STATUSES:
                    self._converge_pending_human_work(
                        db,
                        run_id=run_id,
                        status=status,
                    )
                event_payload = self._build_terminal_payload(
                    round_obj,
                    status=status,
                    final_response=final_response,
                    step_count=step_count,
                    terminal_event=terminal_event,
                )
                event_type = str(event_payload.get("type") or "")
                if status == "failed" and event_type != EventType.RUN_ERROR.value:
                    raise ValueError("failed rounds must use RUN_ERROR terminal event")
                if status != "failed" and event_type != EventType.RUN_FINISHED.value:
                    raise ValueError(f"{status} rounds must use RUN_FINISHED terminal event")

                sequence = self._current_high_water(db, run_id) + 1
                event_payload["sequence"] = sequence
                event_log = AGUIEventLog(
                    run_id=run_id,
                    event_type=event_type,
                    payload=json.dumps(event_payload, ensure_ascii=False),
                    sequence=sequence,
                    timestamp=event_payload.get("timestamp"),
                )
                db.add(event_log)
                db.commit()
                AguiEventBus._terminal_runs.add(run_id)
                return StoredEvent(run_id=run_id, sequence=sequence, event=event_payload)
            except InteractionConflictError:
                db.rollback()
                raise
            except IntegrityError as exc:
                last_exc = exc
                db.rollback()
                if attempt == 2:
                    raise
                logger.warning(
                    "terminal sequence unique conflict, retrying: run=%s attempt=%d",
                    run_id,
                    attempt + 1,
                )
        raise last_exc  # type: ignore[misc]

    def _cancel_user_run_in_session(
        self,
        db: DBSession,
        *,
        run_id: str,
        outcome_warning: str,
        safe_final_response: str,
    ) -> RunCancellationResult:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                round_obj = db.query(Round).filter(Round.id == run_id).first()
                if round_obj is None:
                    db.rollback()
                    return RunCancellationResult(
                        stored_event=None,
                        outcome_uncertain=True,
                        final_response=outcome_warning,
                    )
                self._lock_round_row_if_possible(db, round_obj)

                existing = self._existing_terminal_event(db, run_id)
                if round_obj.status in Round.SUBSCRIBE_TERMINAL_STATUSES:
                    changed = self._converge_pending_human_work(
                        db,
                        run_id=run_id,
                        status=round_obj.status,
                    )
                    if changed:
                        db.commit()
                    else:
                        db.rollback()
                    return self._cancellation_result_from_existing(
                        existing,
                        outcome_warning=outcome_warning,
                        safe_final_response=(
                            round_obj.final_response or safe_final_response
                        ),
                    )

                interactions, approvals = self._lock_human_work_rows(
                    db,
                    run_id=run_id,
                )
                outcome_uncertain = self._round_has_uncertain_dispatch(
                    round_obj,
                    approvals,
                )
                final_response = (
                    outcome_warning if outcome_uncertain else safe_final_response
                )
                step_count = round_obj.step_count or 0
                self._apply_round_status(
                    round_obj,
                    status="cancelled",
                    final_response=final_response,
                    step_count=step_count,
                )
                self._converge_locked_human_work(
                    status="cancelled",
                    interactions=interactions,
                    approvals=approvals,
                )

                result: dict[str, Any] = {
                    "reason": "user_cancelled",
                    "finalResponse": final_response,
                    "stepCount": step_count,
                    "outcomeUncertain": outcome_uncertain,
                }
                if outcome_uncertain:
                    result["warning"] = outcome_warning
                event_payload = RunFinishedEvent(
                    threadId=(
                        self._string_or_none(
                            getattr(round_obj, "effective_thread_id", None)
                        )
                        or self._string_or_none(getattr(round_obj, "session_id", None))
                        or self._string_or_none(getattr(round_obj, "thread_id", None))
                        or ""
                    ),
                    runId=run_id,
                    outcome="interrupt",
                    result=result,
                ).model_dump(by_alias=True, exclude_none=True, mode="json")
                sequence = self._current_high_water(db, run_id) + 1
                event_payload["sequence"] = sequence
                event_log = AGUIEventLog(
                    run_id=run_id,
                    event_type=EventType.RUN_FINISHED.value,
                    payload=json.dumps(event_payload, ensure_ascii=False),
                    sequence=sequence,
                    timestamp=event_payload.get("timestamp"),
                )
                db.add(event_log)
                db.commit()
                AguiEventBus._terminal_runs.add(run_id)
                stored = StoredEvent(
                    run_id=run_id,
                    sequence=sequence,
                    event=event_payload,
                )
                return RunCancellationResult(
                    stored_event=stored,
                    outcome_uncertain=outcome_uncertain,
                    final_response=final_response,
                )
            except IntegrityError as exc:
                last_exc = exc
                db.rollback()
                if attempt == 2:
                    raise
                logger.warning(
                    "cancel terminal sequence unique conflict, retrying: run=%s attempt=%d",
                    run_id,
                    attempt + 1,
                )
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _cancellation_result_from_existing(
        existing: StoredEvent | None,
        *,
        outcome_warning: str,
        safe_final_response: str,
    ) -> RunCancellationResult:
        event = existing.event if existing is not None else {}
        result = event.get("result") if isinstance(event, dict) else None
        result = result if isinstance(result, dict) else {}
        outcome_uncertain = result.get("outcomeUncertain") is True
        final_response = result.get("finalResponse")
        if not isinstance(final_response, str) or not final_response:
            final_response = (
                outcome_warning if outcome_uncertain else safe_final_response
            )
        return RunCancellationResult(
            stored_event=existing,
            outcome_uncertain=outcome_uncertain,
            final_response=final_response,
        )

    @staticmethod
    def _apply_round_status(
        round_obj: Round,
        *,
        status: str,
        final_response: str | None,
        step_count: int | None,
    ) -> None:
        round_obj.final_response = final_response
        round_obj.step_count = step_count
        round_obj.status = status
        round_obj.completed_at = now_naive()

    @staticmethod
    def _converge_pending_human_work(
        db: DBSession,
        *,
        run_id: str,
        status: str,
    ) -> bool:
        """Close orphan interactions and unclaimed approvals in lock order.

        The caller already owns the Round row lock. Rows are then locked in a
        deterministic ``AgentInteraction -> ToolApprovalRequest`` order so
        resume and terminal convergence cannot form a PostgreSQL lock cycle.
        Executing approvals are deliberately untouched because their external
        side effect may already have happened; their execution lease owns that
        outcome-unknown reconciliation.
        """

        if status not in Round.COMPLETE_TERMINAL_STATUSES:
            return False

        interactions, approvals = RunCompletionService._lock_human_work_rows(
            db,
            run_id=run_id,
        )
        return RunCompletionService._converge_locked_human_work(
            status=status,
            interactions=interactions,
            approvals=approvals,
        )

    @staticmethod
    def _lock_human_work_rows(
        db: DBSession,
        *,
        run_id: str,
    ) -> tuple[list[AgentInteraction], list[ToolApprovalRequest]]:
        interactions = (
            db.query(AgentInteraction)
            .filter(
                AgentInteraction.round_id == run_id,
                AgentInteraction.status == "pending",
            )
            .order_by(AgentInteraction.id.asc())
            .with_for_update()
            .all()
        )
        approvals = (
            db.query(ToolApprovalRequest)
            .filter(ToolApprovalRequest.run_id == run_id)
            .order_by(ToolApprovalRequest.id.asc())
            .with_for_update()
            .all()
        )
        return interactions, approvals

    @staticmethod
    def _converge_locked_human_work(
        *,
        status: str,
        interactions: list[AgentInteraction],
        approvals: list[ToolApprovalRequest],
    ) -> bool:
        resolved_at = now_naive()
        interaction_terminal_status = (
            "cancelled" if status == "cancelled" else "failed"
        )
        changed = False
        for interaction in interactions:
            interaction.status = interaction_terminal_status
            interaction.resolved_at = resolved_at
            interaction.claim_token = None
            interaction.claim_lease_expires_at = None
            interaction.updated_at = resolved_at
            changed = True

        for approval in approvals:
            if approval.status not in APPROVAL_CANCELLABLE_STATUSES:
                continue
            was_approved = approval.status == "approved"
            # requested/approved means dispatch never crossed into executing;
            # a Round terminal therefore cancels the decision regardless of
            # whether the Round itself completed, failed, or hit max steps.
            approval.status = "cancelled"
            if not was_approved:
                approval.resolved_at = resolved_at
            approval.completed_at = resolved_at
            approval.execution_claim_token = None
            approval.execution_lease_expires_at = None
            if was_approved:
                approval.error = (
                    "Round cancelled before approved tool dispatch."
                    if status == "cancelled"
                    else f"Round reached terminal status {status} before approved tool dispatch."
                )
            else:
                approval.error = (
                    "Round cancelled before tool approval was resolved."
                    if status == "cancelled"
                    else f"Round reached terminal status {status} before tool approval was resolved."
                )
            changed = True
        return changed

    @staticmethod
    def _round_has_uncertain_dispatch(
        round_obj: Round,
        approvals: list[ToolApprovalRequest],
    ) -> bool:
        statuses = {
            approval.status
            for approval in approvals
            if isinstance(getattr(approval, "status", None), str)
        }
        if statuses.intersection({"executing", "unknown"}):
            return True
        if round_obj.status == "waiting_interaction":
            return False
        if statuses.intersection(APPROVAL_CANCELLABLE_STATUSES):
            return False
        # Direct MCP calls have no ToolApprovalRequest row proving that they
        # have not crossed their dispatch boundary, so a generic running Round
        # remains conservative.
        return True

    def _build_terminal_payload(
        self,
        round_obj: Round,
        *,
        status: str,
        final_response: str | None,
        step_count: int | None,
        terminal_event: AGUIEvent | dict[str, Any] | None,
    ) -> dict[str, Any]:
        if terminal_event is not None:
            return self._event_to_dict(terminal_event)

        thread_id = (
            self._string_or_none(getattr(round_obj, "effective_thread_id", None))
            or self._string_or_none(getattr(round_obj, "session_id", None))
            or self._string_or_none(getattr(round_obj, "thread_id", None))
            or ""
        )
        if status == "failed":
            return RunErrorEvent(
                message=final_response or "Run failed",
                code="RUN_FAILED",
            ).model_dump(by_alias=True, exclude_none=True, mode="json")

        result: dict[str, Any] = {
            "finalResponse": final_response or "",
            "stepCount": step_count or 0,
        }
        if status == "cancelled":
            result["reason"] = "user_cancelled"
        elif status == "max_steps_reached":
            result["reason"] = "max_steps_reached"
        return RunFinishedEvent(
            threadId=thread_id,
            runId=run_id_from_round(round_obj),
            result=result,
            outcome="success" if status == "completed" else "interrupt",
        ).model_dump(by_alias=True, exclude_none=True, mode="json")

    @staticmethod
    def _event_to_dict(event: AGUIEvent | dict[str, Any]) -> dict[str, Any]:
        if isinstance(event, dict):
            return dict(event)
        return event.model_dump(by_alias=True, exclude_none=True, mode="json")

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        return value if isinstance(value, str) else None

    def _existing_terminal_event(self, db: DBSession, run_id: str) -> StoredEvent | None:
        row = (
            db.query(AGUIEventLog)
            .filter(
                AGUIEventLog.run_id == run_id,
                AGUIEventLog.event_type.in_(self._TERMINAL_EVENT_TYPES),
            )
            .order_by(AGUIEventLog.sequence.desc())
            .first()
        )
        if not row:
            return None
        if not isinstance(getattr(row, "payload", None), (str, bytes, bytearray)):
            return None
        try:
            payload = json.loads(row.payload)
        except json.JSONDecodeError:
            payload = {"type": row.event_type}
        payload["sequence"] = row.sequence
        return StoredEvent(run_id=run_id, sequence=row.sequence, event=payload, log_id=row.id)

    @staticmethod
    def _current_high_water(db: DBSession, run_id: str) -> int:
        value = (
            db.query(func.coalesce(func.max(AGUIEventLog.sequence), 0))
            .filter(AGUIEventLog.run_id == run_id)
            .scalar()
        )
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _lock_round_row_if_possible(db: DBSession, round_obj: Round) -> None:
        if not RunCompletionService._is_mapped_instance(round_obj):
            return
        try:
            db.refresh(round_obj, with_for_update=True)
        except TypeError:
            db.refresh(round_obj)

    @staticmethod
    def _is_mapped_instance(obj: Any) -> bool:
        try:
            from sqlalchemy import inspect as sa_inspect

            sa_inspect(obj)
            return True
        except Exception:
            return False


def run_id_from_round(round_obj: Round) -> str:
    value = getattr(round_obj, "id", None) or getattr(round_obj, "run_id", "")
    return value if isinstance(value, str) else ""
