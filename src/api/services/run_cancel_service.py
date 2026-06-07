"""Single-worker run cancel registry and audit service."""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session as DBSession

from src.api.models.run_cancel_request import RunCancelRequest
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)


_CANCEL_STATE_REQUESTED = "requested"
_CANCEL_STATE_ACKED = "acked"
_CANCEL_STATE_COMPLETED = "completed"


@dataclass(frozen=True)
class CancelAuditResult:
    request_id: str
    session_id: str
    target_run_id: str | None
    local_hit: bool


@dataclass
class RunRegistryEntry:
    session_id: str
    run_id: str
    cancel_token: asyncio.Event
    root_run_id: str | None = None
    started_at: datetime | None = None
    task: asyncio.Task | None = None
    metadata: dict[str, Any] | None = None


class RunCancelService:
    """In-process registry plus append-only cancel audit rows.

    This is intentionally single-worker.  Database rows record what happened,
    but they do not deliver cancellation commands to other processes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_current_run: dict[str, str] = {}
        self._runs: dict[str, RunRegistryEntry] = {}

    def register(
        self,
        *,
        session_id: str,
        run_id: str,
        cancel_token: asyncio.Event,
        root_run_id: str | None = None,
        started_at: datetime | None = None,
        task: asyncio.Task | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._session_current_run[session_id] = run_id
            self._runs[run_id] = RunRegistryEntry(
                session_id=session_id,
                run_id=run_id,
                cancel_token=cancel_token,
                root_run_id=root_run_id or run_id,
                started_at=started_at,
                task=task,
                metadata=metadata or {},
            )

    def unregister(self, *, session_id: str | None = None, run_id: str | None = None) -> None:
        with self._lock:
            if run_id is None and session_id is not None:
                run_id = self._session_current_run.get(session_id)
            if run_id is None:
                return
            entry = self._runs.pop(run_id, None)
            entry_session_id = entry.session_id if entry else session_id
            if entry_session_id and self._session_current_run.get(entry_session_id) == run_id:
                self._session_current_run.pop(entry_session_id, None)

    def current_run_id(self, session_id: str) -> str | None:
        with self._lock:
            return self._session_current_run.get(session_id)

    def get_entry(self, run_id: str) -> RunRegistryEntry | None:
        with self._lock:
            return self._runs.get(run_id)

    def request_cancel(
        self,
        db: DBSession,
        *,
        user_id: str,
        session_id: str,
        target_run_id: str | None = None,
        root_run_id: str | None = None,
        requested_after: datetime | None = None,
        reason: str = "user_cancelled",
    ) -> CancelAuditResult:
        now = now_naive()
        request_id = str(uuid.uuid4())
        current_run_id: str | None
        entry: RunRegistryEntry | None
        with self._lock:
            current_run_id = self._session_current_run.get(session_id)
            if target_run_id is None:
                target_run_id = current_run_id
            entry = self._runs.get(target_run_id) if target_run_id else None

        if entry and requested_after and entry.started_at and entry.started_at > requested_after:
            local_hit = False
        else:
            local_hit = bool(entry and target_run_id == current_run_id)
        row = RunCancelRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            target_run_id=target_run_id,
            root_run_id=root_run_id or (entry.root_run_id if entry else target_run_id),
            requested_after=requested_after,
            state=_CANCEL_STATE_ACKED if local_hit else _CANCEL_STATE_REQUESTED,
            requested_at=now,
            acked_at=now if local_hit else None,
            updated_at=now,
        )
        db.add(row)
        db.commit()

        if local_hit and entry is not None:
            entry.cancel_token.set()
            logger.info(
                "cancel token set: user=%s session=%s run=%s request=%s reason=%s",
                user_id,
                session_id,
                target_run_id,
                request_id,
                reason,
            )
        else:
            logger.info(
                "cancel request audited without local registry hit: user=%s session=%s target=%s request=%s",
                user_id,
                session_id,
                target_run_id,
                request_id,
            )

        return CancelAuditResult(
            request_id=request_id,
            session_id=session_id,
            target_run_id=target_run_id,
            local_hit=local_hit,
        )

    def mark_completed(
        self,
        db: DBSession,
        *,
        request_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        target_run_id: str | None = None,
    ) -> int:
        query = db.query(RunCancelRequest)
        if request_id:
            query = query.filter(RunCancelRequest.request_id == request_id)
        else:
            if user_id:
                query = query.filter(RunCancelRequest.user_id == user_id)
            if session_id:
                query = query.filter(RunCancelRequest.session_id == session_id)
            if target_run_id:
                query = query.filter(RunCancelRequest.target_run_id == target_run_id)
            query = query.filter(RunCancelRequest.state != _CANCEL_STATE_COMPLETED)
        now = now_naive()
        count = query.update(
            {
                "state": _CANCEL_STATE_COMPLETED,
                "completed_at": now,
                "updated_at": now,
            },
            synchronize_session=False,
        )
        if count:
            db.commit()
        else:
            db.rollback()
        return int(count or 0)

    def clear(self) -> None:
        with self._lock:
            self._session_current_run.clear()
            self._runs.clear()


_GLOBAL_RUN_CANCEL_SERVICE = RunCancelService()


def get_run_cancel_service() -> RunCancelService:
    return _GLOBAL_RUN_CANCEL_SERVICE
