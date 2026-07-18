"""Turn orchestration boundary.

The Web route normalizes requests and renders SSE.  This module owns the
prepared run lifecycle: starting the Agent event source, registering cancel
tokens, tracking the active task, and releasing the user run lock when the run
settles.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as DBSession

from src.agent.schema.agui_events import AGUIEvent, EventType, RunFinishedEvent
from src.api.config import get_settings
from src.api.models.database import SessionLocal
from src.api.models.user_run_lock import UserRunLock
from src.api.schemas.turn import CancelResult, NormalizedInboundTurn, NormalizedResumeTurn, RunHandle, TurnCancelTarget
from src.api.schemas.turn import WebReplyRoute
from src.api.services.agent_service import AgentService, PreparedAgentRun
from src.api.services.run_completion_service import RunCompletionService
from src.api.services.run_coordinator import RunCoordinator, get_run_coordinator
from src.api.services.run_cancel_service import RunCancelService, get_run_cancel_service
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)


_QUEUE_SENTINEL = object()


class _ManagedEventStream:
    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._consumer_active = True

    async def push(self, item: AGUIEvent | dict[str, Any] | Exception) -> None:
        if self._consumer_active:
            self._queue.put_nowait(item)

    async def close(self) -> None:
        if self._consumer_active:
            self._queue.put_nowait(_QUEUE_SENTINEL)

    def detach(self) -> None:
        self._consumer_active = False

    async def events(self) -> AsyncIterator[AGUIEvent | dict[str, Any]]:
        try:
            while True:
                item = await self._queue.get()
                if item is _QUEUE_SENTINEL:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.detach()


@dataclass(frozen=True)
class TurnExecution:
    """Prepared run handle plus an orchestrator-managed event stream."""

    handle: RunHandle
    event_source: AsyncIterator[AGUIEvent | dict[str, Any]]
    task: asyncio.Task | None = None
    cancel_token: asyncio.Event | None = None


SubmitTurnHandler = Callable[[NormalizedInboundTurn], Awaitable[TurnExecution]]
ResumeTurnHandler = Callable[[NormalizedResumeTurn], Awaitable[TurnExecution]]


class TurnOrchestrator:
    """Typed orchestration facade for Web now and channel adapters later."""

    def __init__(
        self,
        *,
        submit_handler: SubmitTurnHandler | None = None,
        resume_handler: ResumeTurnHandler | None = None,
        cancel_service: RunCancelService | None = None,
        run_coordinator: RunCoordinator | None = None,
    ):
        self._submit_handler = submit_handler
        self._resume_handler = resume_handler
        self._cancel_service = cancel_service or get_run_cancel_service()
        self._run_coordinator = run_coordinator or get_run_coordinator()
        self._active_runners: dict[str, asyncio.Task] = {}

    @property
    def active_runners(self) -> dict[str, asyncio.Task]:
        return self._active_runners

    async def submit_turn(
        self,
        turn: NormalizedInboundTurn,
        *,
        agent_service: AgentService | None = None,
        cancel_token: asyncio.Event | None = None,
        lock_id: str | None = None,
        run_started_at: datetime | None = None,
    ) -> TurnExecution:
        if self._submit_handler is not None:
            return await self._submit_handler(turn)
        if agent_service is None:
            raise RuntimeError("submit_turn requires agent_service")

        prepare_kwargs = {
            "user_content": turn.content,
            "idempotency_key": turn.idempotency_key,
        }
        if turn.context:
            prepare_kwargs["contexts"] = turn.context
        prepared = await agent_service.prepare_chat_round(**prepare_kwargs)
        return self._execution_from_prepared(
            turn=turn,
            prepared=prepared,
            agent_service=agent_service,
            cancel_token=cancel_token,
            lock_id=lock_id,
            run_started_at=run_started_at,
            error_label="Agent執行失敗",
        )

    async def resume_turn(
        self,
        turn: NormalizedResumeTurn,
        *,
        agent_service: AgentService | None = None,
        cancel_token: asyncio.Event | None = None,
        lock_id: str | None = None,
        run_started_at: datetime | None = None,
    ) -> TurnExecution:
        if self._resume_handler is not None:
            return await self._resume_handler(turn)
        if agent_service is None:
            raise RuntimeError("resume_turn requires agent_service")

        prepared = await agent_service.prepare_resume_round(
            interrupt_id=turn.interrupt_id,
            answers=turn.answers,
        )
        return self._execution_from_prepared(
            turn=turn,
            prepared=prepared,
            agent_service=agent_service,
            cancel_token=cancel_token,
            lock_id=lock_id,
            run_started_at=run_started_at,
            error_label="Resume 执行失败",
        )

    async def cancel_turn(self, target: TurnCancelTarget, *, db: DBSession) -> CancelResult:
        audit = self._cancel_service.request_cancel(
            db,
            user_id=target.user_id,
            session_id=target.session_id,
            target_run_id=target.round_id,
            root_run_id=target.root_run_id,
            requested_after=target.requested_after,
            reason=target.reason,
        )
        return CancelResult(
            request_id=audit.request_id,
            state="acked" if audit.local_hit else "requested",
            target_run_id=audit.target_run_id,
        )

    def _execution_from_prepared(
        self,
        *,
        turn: NormalizedInboundTurn | NormalizedResumeTurn,
        prepared: PreparedAgentRun,
        agent_service: AgentService,
        cancel_token: asyncio.Event | None,
        lock_id: str | None,
        run_started_at: datetime | None,
        error_label: str,
    ) -> TurnExecution:
        session_id = self._session_id_from_turn(turn)
        started_at = run_started_at or now_naive()
        cancel_token = cancel_token or asyncio.Event()
        agent_service.cancel_token = cancel_token
        handle = RunHandle(
            session_id=session_id,
            round_id=prepared.run_id,
            run_id=prepared.run_id,
            root_run_id=prepared.run_id,
            parent_run_id=prepared.parent_run_id,
            reply_route=turn.reply_route,
            started_at=started_at,
        )
        stream = _ManagedEventStream()
        source = agent_service.run_prepared_round(
            prepared,
            error_label=error_label,
        )
        task = asyncio.create_task(
            self._drive_run(
                handle=handle,
                event_source=source,
                stream=stream,
                user_id=turn.user_id,
                lock_id=lock_id,
                cancel_token=cancel_token,
            )
        )
        self._active_runners[session_id] = task
        self._cancel_service.register(
            session_id=session_id,
            run_id=prepared.run_id,
            cancel_token=cancel_token,
            root_run_id=handle.root_run_id,
            started_at=started_at,
            task=task,
        )
        return TurnExecution(
            handle=handle,
            event_source=stream.events(),
            task=task,
            cancel_token=cancel_token,
        )

    @staticmethod
    def _session_id_from_turn(turn: NormalizedInboundTurn | NormalizedResumeTurn) -> str:
        if isinstance(turn.reply_route, WebReplyRoute):
            return turn.reply_route.session_id
        value = getattr(turn, "session_id", None) or turn.metadata.get("session_id")
        if isinstance(value, str) and value:
            return value
        raise RuntimeError("turn is missing internal session_id")

    async def _drive_run(
        self,
        *,
        handle: RunHandle,
        event_source: AsyncIterator[AGUIEvent],
        stream: _ManagedEventStream,
        user_id: str,
        lock_id: str | None,
        cancel_token: asyncio.Event,
    ) -> None:
        run_completed = False
        heartbeat_task: asyncio.Task | None = None
        if lock_id:
            heartbeat_task = asyncio.create_task(
                self._lock_heartbeat_guard(
                    user_id=user_id,
                    session_id=handle.session_id,
                    lock_id=lock_id,
                    cancel_token=cancel_token,
                )
            )
        try:
            async for event in event_source:
                event_type = self._event_type(event)
                if event_type in {EventType.RUN_FINISHED, EventType.RUN_FINISHED.value, EventType.RUN_ERROR, EventType.RUN_ERROR.value}:
                    run_completed = True
                await stream.push(event)
        except asyncio.CancelledError:
            if not run_completed:
                finished_event = RunFinishedEvent(
                    threadId=handle.session_id,
                    runId=handle.run_id,
                    outcome="interrupt",
                    result={"reason": "user_cancelled"},
                )
                try:
                    stored = await RunCompletionService(SessionLocal).complete(
                        run_id=handle.run_id,
                        status="cancelled",
                        final_response="Cancelled",
                        step_count=0,
                        terminal_event=finished_event,
                    )
                    if stored is not None:
                        await stream.push(stored.event)
                except Exception:
                    logger.warning(
                        "orchestrated producer cancelled terminal completion failed: session=%s run=%s",
                        handle.session_id,
                        handle.run_id,
                        exc_info=True,
                    )
            raise
        except Exception as exc:
            await stream.push(exc)
        finally:
            await stream.close()
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            if self._active_runners.get(handle.session_id) is asyncio.current_task():
                self._active_runners.pop(handle.session_id, None)
            self._cancel_service.unregister(session_id=handle.session_id, run_id=handle.run_id)
            if lock_id:
                await self._complete_cancel_requests(user_id=user_id, session_id=handle.session_id)
                await self._release_lock(user_id=user_id, session_id=handle.session_id, lock_id=lock_id)

    @staticmethod
    def _event_type(event: AGUIEvent | dict[str, Any]) -> Any:
        if isinstance(event, dict):
            return event.get("type")
        return getattr(event, "type", None)

    async def _complete_cancel_requests(self, *, user_id: str, session_id: str) -> None:
        try:
            with SessionLocal() as db:
                self._cancel_service.mark_completed(db, user_id=user_id, session_id=session_id)
        except Exception:
            logger.warning(
                "complete cancel requests failed: user=%s session=%s",
                user_id,
                session_id,
                exc_info=True,
            )

    async def _release_lock(self, *, user_id: str, session_id: str, lock_id: str) -> None:
        try:
            with SessionLocal() as db:
                await self._run_coordinator.release_user_run_lock(
                    db,
                    user_id=user_id,
                    lock_id=lock_id,
                    session_id=session_id,
                )
        except Exception:
            logger.warning(
                "release run lock failed: user=%s session=%s lock=%s",
                user_id,
                session_id,
                lock_id,
                exc_info=True,
            )

    async def _lock_heartbeat_guard(
        self,
        *,
        user_id: str,
        session_id: str,
        lock_id: str,
        cancel_token: asyncio.Event,
    ) -> None:
        settings = get_settings()
        check_interval = max(settings.cancel_watcher_interval_seconds, 0.5)
        heartbeat_interval = max(settings.sse_heartbeat_interval, check_interval)
        last_heartbeat = time.monotonic()
        fail_count = 0
        max_failures = 3
        try:
            while not cancel_token.is_set():
                if time.monotonic() - last_heartbeat >= heartbeat_interval:
                    try:
                        with SessionLocal() as db:
                            updated = db.query(UserRunLock).filter(
                                UserRunLock.user_id == user_id,
                                UserRunLock.lock_id == lock_id,
                            ).update({UserRunLock.updated_at: now_naive()}, synchronize_session=False)
                            db.commit()
                        if updated:
                            fail_count = 0
                            last_heartbeat = time.monotonic()
                        else:
                            logger.warning(
                                "heartbeat found missing lock, cancelling run: user=%s session=%s lock=%s",
                                user_id,
                                session_id,
                                lock_id,
                            )
                            cancel_token.set()
                            return
                    except OperationalError:
                        fail_count += 1
                        logger.warning(
                            "heartbeat write conflict: user=%s session=%s fail_count=%d/%d",
                            user_id,
                            session_id,
                            fail_count,
                            max_failures,
                            exc_info=True,
                        )
                    except Exception:
                        fail_count += 1
                        logger.warning(
                            "heartbeat failed: user=%s session=%s fail_count=%d/%d",
                            user_id,
                            session_id,
                            fail_count,
                            max_failures,
                            exc_info=True,
                        )
                    if fail_count >= max_failures:
                        cancel_token.set()
                        return
                await asyncio.sleep(check_interval)
        except asyncio.CancelledError:
            raise


_GLOBAL_TURN_ORCHESTRATOR = TurnOrchestrator()


def get_turn_orchestrator() -> TurnOrchestrator:
    return _GLOBAL_TURN_ORCHESTRATOR
