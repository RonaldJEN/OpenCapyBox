"""Single entry point for AG-UI stream terminal completion."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Callable

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.agent.schema.agui_events import (
    AGUIEvent,
    EventType,
    InterruptDetails,
    RunErrorEvent,
    RunFinishedEvent,
)
from src.api.models.agui_event import AGUIEventLog
from src.api.models.round import Round
from src.api.services.agui_event_bus import AguiEventBus, StoredEvent, get_agui_event_bus
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)


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
        interrupt_payload: str | None = None,
        terminal_event: AGUIEvent | dict[str, Any] | None = None,
    ) -> StoredEvent | None:
        """Async facade that also fans out the committed terminal event."""
        stored = self.complete_sync(
            run_id=run_id,
            status=status,
            final_response=final_response,
            step_count=step_count,
            interrupt_payload=interrupt_payload,
            terminal_event=terminal_event,
        )
        if stored is not None:
            await get_agui_event_bus().publish_committed(run_id, stored.event)
        return stored

    def complete_sync(
        self,
        *,
        run_id: str,
        status: str,
        final_response: str | None = None,
        step_count: int | None = None,
        interrupt_payload: str | None = None,
        terminal_event: AGUIEvent | dict[str, Any] | None = None,
    ) -> StoredEvent | None:
        if status not in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
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
                interrupt_payload=interrupt_payload,
                terminal_event=terminal_event,
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
            existing = self._existing_terminal_event(db, run_id)
            if existing is not None:
                db.rollback()
                return existing
            self._lock_round_row_if_possible(db, round_obj)
            status = "interrupted" if round_obj.status == "resumed" else round_obj.status
            event_payload = self._build_terminal_payload(
                round_obj,
                status=status,
                final_response=round_obj.final_response,
                step_count=round_obj.step_count,
                interrupt_payload=round_obj.interrupt_payload,
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
        interrupt_payload: str | None,
        terminal_event: AGUIEvent | dict[str, Any] | None = None,
    ) -> StoredEvent | None:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                round_obj = db.query(Round).filter(Round.id == run_id).first()
                if not round_obj:
                    db.rollback()
                    return None
                self._lock_round_row_if_possible(db, round_obj)

                existing = self._existing_terminal_event(db, run_id)
                if existing is not None and round_obj.status in Round.SUBSCRIBE_TERMINAL_STATUSES:
                    db.rollback()
                    return existing

                if round_obj.status in (Round.COMPLETE_TERMINAL_STATUSES | {"resumed"}):
                    if round_obj.status == "resumed" and status == "interrupted":
                        changed = False
                        if final_response and not round_obj.final_response:
                            round_obj.final_response = final_response
                            changed = True
                        if (
                            step_count is not None
                            and (round_obj.step_count is None or step_count > round_obj.step_count)
                        ):
                            round_obj.step_count = step_count
                            changed = True
                        if round_obj.completed_at is None:
                            round_obj.completed_at = now_naive()
                            changed = True
                        if changed:
                            db.commit()
                        else:
                            db.rollback()
                    else:
                        logger.info(
                            "Round %s 已处于终态 %s，跳过 complete(status=%s)",
                            run_id,
                            round_obj.status,
                            status,
                        )
                        db.rollback()
                    return existing

                self._apply_round_status(
                    round_obj,
                    status=status,
                    final_response=final_response,
                    step_count=step_count,
                    interrupt_payload=interrupt_payload,
                )
                event_payload = self._build_terminal_payload(
                    round_obj,
                    status=status,
                    final_response=final_response,
                    step_count=step_count,
                    interrupt_payload=interrupt_payload,
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

    @staticmethod
    def _apply_round_status(
        round_obj: Round,
        *,
        status: str,
        final_response: str | None,
        step_count: int | None,
        interrupt_payload: str | None,
    ) -> None:
        round_obj.final_response = final_response
        round_obj.step_count = step_count
        round_obj.status = status
        round_obj.interrupt_payload = interrupt_payload
        round_obj.completed_at = now_naive() if status != "interrupted" else None

    def _build_terminal_payload(
        self,
        round_obj: Round,
        *,
        status: str,
        final_response: str | None,
        step_count: int | None,
        interrupt_payload: str | None,
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
        elif status == "interrupted" and round_obj.status == "resumed":
            result["reason"] = "resumed_by_new_run"

        interrupt_details = None
        if status == "interrupted" and interrupt_payload:
            try:
                interrupt_details = InterruptDetails(**json.loads(interrupt_payload))
            except Exception:
                interrupt_details = None

        return RunFinishedEvent(
            threadId=thread_id,
            runId=run_id_from_round(round_obj),
            result=result,
            outcome="success" if status == "completed" else "interrupt",
            interrupt=interrupt_details,
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
