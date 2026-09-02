"""Run coordination for user/session execution slots."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import uuid
import weakref
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from sqlalchemy import insert, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.agent_interaction import AgentInteraction
from src.api.models.database import SessionLocal
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.user_run_lock import UserRunLock
from src.api.models.user_memory import CronJobRun
from src.api.services.agent_interaction_service import AgentInteractionService
from src.api.services.agui_event_bus import get_agui_event_bus
from src.api.services.run_completion_service import RunCompletionService
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)

_LOCAL_ACQUIRE_LOCKS: weakref.WeakValueDictionary[
    tuple[int, str],
    asyncio.Lock,
] = weakref.WeakValueDictionary()
_LOCAL_ACQUIRE_LOCKS_GUARD = threading.Lock()


def _get_local_acquire_lock(user_id: str) -> asyncio.Lock:
    """Serialize same-process acquisitions without sharing locks across loops."""

    key = (id(asyncio.get_running_loop()), user_id)
    with _LOCAL_ACQUIRE_LOCKS_GUARD:
        lock = _LOCAL_ACQUIRE_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCAL_ACQUIRE_LOCKS[key] = lock
        return lock


def _postgres_advisory_lock_key(user_id: str) -> int:
    digest = hashlib.blake2b(
        user_id.encode("utf-8"),
        digest_size=8,
        person=b"run-slot",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _is_retryable_db_error(exc: OperationalError) -> bool:
    pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
    return pgcode in ("40001", "40P01")


async def _with_db_retry(
    fn: Callable[[], object],
    *,
    max_retries: int = 5,
    retry_interval: float = 0.1,
    rollback: Callable[[], None] | None = None,
) -> object:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except OperationalError as exc:
            last_exc = exc
            if rollback:
                rollback()
            if _is_retryable_db_error(exc) and attempt + 1 < max_retries:
                await asyncio.sleep(retry_interval)
                continue
            raise
        except Exception:
            if rollback:
                rollback()
            raise
    raise last_exc  # type: ignore[misc]


class RunCoordinator:
    """Coordinates per-user slots and per-session serialization."""

    def __init__(self, session_factory=SessionLocal, settings_provider: Callable[[], object] = get_settings):
        self.session_factory = session_factory
        self.settings_provider = settings_provider

    async def acquire_user_run_lock(self, *, user_id: str, session_id: str) -> str | None:
        async with _get_local_acquire_lock(user_id):
            return await self._acquire_user_run_lock_serialized(
                user_id=user_id,
                session_id=session_id,
            )

    async def _acquire_user_run_lock_serialized(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> str | None:
        settings = self.settings_provider()
        stale_threshold_seconds = max(settings.sse_subscribe_timeout, 1)
        concurrency_limit = max(int(getattr(settings, "agent_user_concurrency_limit", 1) or 1), 1)
        max_busy_retries = 5
        retry_interval_seconds = 0.1

        with self._coordination_session() as lock_db:
            advisory_key = self._acquire_postgres_user_mutex(lock_db, user_id)

            def _lock_age_seconds(lock: UserRunLock) -> float:
                heartbeat_at = lock.updated_at or lock.created_at
                return (now_naive() - heartbeat_at).total_seconds()

            def _delete_stale_locks() -> list[dict[str, Any]]:
                locks = (
                    lock_db.query(UserRunLock)
                    .filter(UserRunLock.user_id == user_id)
                    .order_by(UserRunLock.slot.asc())
                    .with_for_update()
                    .all()
                )
                stale_locks: list[dict[str, Any]] = []
                for lock in locks:
                    age = _lock_age_seconds(lock)
                    if age < stale_threshold_seconds:
                        continue
                    logger.warning(
                        "检测到陈旧用户运行锁（心跳 %.1fs 前），回收: user=%s session=%s lock=%s slot=%s",
                        age,
                        user_id,
                        lock.session_id,
                        lock.lock_id,
                        lock.slot,
                    )
                    stale_locks.append({
                        "lock_id": lock.lock_id,
                        "user_id": lock.user_id,
                        "session_id": lock.session_id,
                        "slot": lock.slot,
                        "created_at": lock.created_at,
                        "updated_at": lock.updated_at,
                    })
                    lock_db.delete(lock)
                if stale_locks:
                    lock_db.commit()
                return stale_locks

            try:
                for attempt in range(max_busy_retries):
                    try:
                        stale_locks = _delete_stale_locks()
                        for stale_lock in stale_locks:
                            stale_session_id = str(stale_lock["session_id"])
                            restore_stale_lock = False
                            try:
                                _, active_claim_found = (
                                    self._cleanup_orphaned_rounds_detailed(
                                        lock_db,
                                        user_id=user_id,
                                        session_id=stale_session_id,
                                    )
                                )
                                restore_stale_lock = bool(active_claim_found)
                            except Exception:
                                lock_db.rollback()
                                restore_stale_lock = True
                                logger.warning(
                                    "清理孤儿 round 失败，恢复原运行锁: user=%s session=%s",
                                    user_id,
                                    stale_session_id,
                                    exc_info=True,
                                )
                            if restore_stale_lock:
                                self._restore_stale_lock(lock_db, stale_lock)

                        # A prior cleaner may have committed the stale-lock
                        # delete and crashed before classifying its Round. Run
                        # the same state machine for every user-owned Round
                        # that still lacks a lock before allocating capacity.
                        _, active_unlocked_sessions = (
                            self._cleanup_orphaned_rounds_detailed(
                                lock_db,
                                user_id=user_id,
                            )
                        )

                        active_locks = (
                            lock_db.query(UserRunLock)
                            .filter(UserRunLock.user_id == user_id)
                            .all()
                        )
                        if (
                            any(lock.session_id == session_id for lock in active_locks)
                            or session_id in active_unlocked_sessions
                        ):
                            return None
                        if (
                            len(active_locks) + len(active_unlocked_sessions)
                            >= concurrency_limit
                        ):
                            return None

                        used_slots = {lock.slot for lock in active_locks}
                        slot = next(
                            (i for i in range(concurrency_limit) if i not in used_slots),
                            None,
                        )
                        if slot is None:
                            return None

                        lock_id = str(uuid.uuid4())
                        lock_db.execute(
                            insert(UserRunLock).values(
                                lock_id=lock_id,
                                user_id=user_id,
                                session_id=session_id,
                                slot=slot,
                                created_at=now_naive(),
                                updated_at=now_naive(),
                            )
                        )
                        lock_db.commit()
                        return lock_id
                    except OperationalError as exc:
                        lock_db.rollback()
                        if not _is_retryable_db_error(exc):
                            raise
                        if attempt + 1 < max_busy_retries:
                            await asyncio.sleep(retry_interval_seconds)
                            continue
                        logger.warning(
                            "获取用户运行锁遇到 DB 写冲突: user=%s session=%s",
                            user_id,
                            session_id,
                            exc_info=True,
                        )
                        return None
                    except IntegrityError:
                        lock_db.rollback()
                        if attempt + 1 < max_busy_retries:
                            await asyncio.sleep(retry_interval_seconds)
                            continue
                        logger.warning(
                            "获取用户运行锁遇到唯一约束竞争: user=%s session=%s",
                            user_id,
                            session_id,
                            exc_info=True,
                        )
                        return None
                    except Exception:
                        lock_db.rollback()
                        raise
                return None
            finally:
                if advisory_key is not None:
                    self._release_postgres_user_mutex(lock_db, advisory_key)

    @contextmanager
    def _coordination_session(self):
        """Pin PostgreSQL coordination to one physical connection.

        ``pg_advisory_lock`` is session-scoped. A normal ORM Session releases
        its checked-out connection on every commit, while this critical
        section intentionally commits stale-lock cleanup and slot allocation
        in several steps. Binding a fresh ORM Session to an externally owned
        Connection keeps acquire, every commit, and unlock on one backend.
        """
        db = self.session_factory()
        try:
            try:
                bind = db.get_bind()
            except (AttributeError, TypeError):
                bind = None
            is_engine_bound_postgres = bool(
                isinstance(bind, Engine)
                and getattr(getattr(bind, "dialect", None), "name", None)
                == "postgresql"
            )
            if not is_engine_bound_postgres:
                with db as managed_db:
                    yield managed_db
                return

            # The factory-created Session has not issued SQL yet, so closing
            # it is side-effect free. The replacement Session never owns the
            # external Connection and therefore cannot return it to the pool
            # between commits.
            db.close()
            with bind.connect() as connection:
                with DBSession(bind=connection, autoflush=False) as pinned_db:
                    yield pinned_db
        finally:
            # ``Session.close`` is idempotent, and FakeDB test doubles manage
            # themselves in their context-manager branch above.
            if hasattr(db, "close"):
                db.close()

    @staticmethod
    def _acquire_postgres_user_mutex(
        db: DBSession,
        user_id: str,
    ) -> int | None:
        try:
            bind = db.get_bind()
        except (AttributeError, TypeError):
            return None
        if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
            return None
        advisory_key = _postgres_advisory_lock_key(user_id)
        db.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": advisory_key},
        )
        return advisory_key

    @staticmethod
    def _release_postgres_user_mutex(db: DBSession, advisory_key: int) -> None:
        connection = None
        try:
            connection = db.connection()
            unlocked = connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": advisory_key},
            ).scalar_one()
            if unlocked is not True:
                raise RuntimeError(
                    f"PostgreSQL advisory lock was not owned: {advisory_key}"
                )
            db.commit()
        except Exception as exc:
            # A failed session-level unlock cannot be repaired with rollback:
            # the physical PostgreSQL connection would return to the pool while
            # still owning the advisory lock. Invalidating it forces a socket
            # close, and PostgreSQL releases every session lock with it.
            invalidated = False
            if connection is not None:
                try:
                    connection.invalidate(exc)
                    invalidated = True
                except Exception:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if not invalidated:
                try:
                    db.invalidate()
                    invalidated = True
                except Exception:
                    pass
            if not invalidated:
                try:
                    db.close()
                except Exception:
                    pass
            logger.critical(
                "释放 PostgreSQL 用户运行互斥锁失败，连接已失效: key=%s",
                advisory_key,
                exc_info=True,
            )
            raise

    @staticmethod
    def _restore_stale_lock(db: DBSession, stale_lock: dict[str, Any]) -> None:
        db.execute(insert(UserRunLock).values(**stale_lock))
        db.commit()

    def cleanup_orphaned_rounds(
        self,
        db: DBSession,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> int:
        failed_count, _ = self._cleanup_orphaned_rounds_detailed(
            db,
            user_id=user_id,
            session_id=session_id,
        )
        return failed_count

    def _cleanup_orphaned_rounds_detailed(
        self,
        db: DBSession,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> tuple[int, set[str]]:
        if session_id is not None:
            session_filter = or_(Round.session_id == session_id, Round.thread_id == session_id)
        else:
            user_session_ids_stmt = select(Session.id).where(Session.user_id == user_id)
            session_filter = or_(
                Round.session_id.in_(user_session_ids_stmt),
                Round.thread_id.in_(user_session_ids_stmt),
            )
        surviving_lock_sessions = {
            str(row[0])
            for row in (
                db.query(UserRunLock.session_id)
                .filter(UserRunLock.user_id == user_id)
                .all()
            )
            if isinstance(row[0], str) and row[0]
        }
        surviving_lock_sessions.update(
            str(row[0])
            for row in (
                db.query(CronJobRun.id)
                .filter(
                    CronJobRun.user_id == user_id,
                    CronJobRun.status == "running",
                    CronJobRun.claim_token.isnot(None),
                    CronJobRun.claim_lease_expires_at.isnot(None),
                    CronJobRun.claim_lease_expires_at > now_naive(),
                )
                .all()
            )
            if isinstance(row[0], str) and row[0]
        )
        active_unlocked_sessions: set[str] = set()
        failed_round_ids: list[str] = []

        def _owner_session_id(row) -> str | None:
            return (
                session_id
                or (row[1] if isinstance(row[1], str) and row[1] else None)
                or (row[2] if isinstance(row[2], str) and row[2] else None)
            )

        def _has_surviving_lock(row) -> bool:
            return any(
                isinstance(value, str) and value in surviving_lock_sessions
                for value in (row[1], row[2])
            )

        def _continuation_failure_response(interaction_kind: str) -> str:
            if interaction_kind == "tool_approval":
                return "[工具审批续跑进程中断；为避免重复副作用，本轮不会自动重试]"
            return "[交互续跑在持久化启动后中断；本轮不会重新提交已接受的回答]"

        # First classify the entire unlocked session set without mutating any
        # Round. Parent continuation ownership protects sibling subagent child
        # Rounds in the same session regardless of UUID ordering.
        unlocked_rounds = [
            row
            for row in (
                db.query(Round.id, Round.session_id, Round.thread_id)
                .filter(
                    Round.status.in_(("running", "waiting_interaction")),
                    session_filter,
                )
                .order_by(Round.id.asc())
                .all()
            )
            if isinstance(row[0], str) and row[0]
        ]
        for unlocked_round in unlocked_rounds:
            if _has_surviving_lock(unlocked_round):
                continue
            round_id = str(unlocked_round[0])
            owner_session_id = _owner_session_id(unlocked_round)
            active_work = AgentInteractionService.has_active_continuation_work(
                db,
                round_id=round_id,
            )
            db.rollback()
            if active_work and owner_session_id is not None:
                active_unlocked_sessions.add(owner_session_id)

        waiting_rounds = [
            row
            for row in (
                db.query(Round.id, Round.session_id, Round.thread_id)
                .filter(
                    Round.status == "waiting_interaction",
                    session_filter,
                )
                .order_by(Round.id.asc())
                .all()
            )
            if isinstance(row[0], str) and row[0]
        ]
        for waiting_round in waiting_rounds:
            if _has_surviving_lock(waiting_round):
                continue
            waiting_round_id = str(waiting_round[0])
            owner_session_id = _owner_session_id(waiting_round)
            if owner_session_id in active_unlocked_sessions:
                continue
            active_waiting_claim = (
                AgentInteractionService.has_active_continuation_work(
                    db,
                    round_id=waiting_round_id,
                )
            )
            if active_waiting_claim:
                if owner_session_id is not None:
                    active_unlocked_sessions.add(owner_session_id)
                db.rollback()
                logger.info(
                    "孤儿清理保留 waiting Round 的有效 continuation claim: run=%s",
                    waiting_round_id,
                )
                continue
            if AgentInteractionService.lock_expired_prestart_continuation_for_recovery(
                db,
                round_id=waiting_round_id,
            ):
                # Commit the cleared token before any new slot can be
                # allocated. A late renew/start then observes the fence.
                db.commit()
                continue
            interaction_kind = (
                AgentInteractionService.lock_irrecoverable_continuation_round_for_failure(
                    db,
                    round_id=waiting_round_id,
                )
            )
            if interaction_kind is None:
                db.rollback()
                continue
            stored_terminal = RunCompletionService(db).complete_sync(
                run_id=waiting_round_id,
                status="failed",
                final_response=_continuation_failure_response(interaction_kind),
            )
            if stored_terminal is not None:
                get_agui_event_bus().publish_committed_nowait(
                    waiting_round_id,
                    stored_terminal.event,
                )
                failed_round_ids.append(waiting_round_id)

        orphaned_rounds = [
            row
            for row in (
                db.query(Round.id, Round.session_id, Round.thread_id)
                .filter(
                    Round.status == "running",
                    session_filter,
                )
                .order_by(Round.id.asc())
                .all()
            )
            if isinstance(row[0], str) and row[0]
        ]
        running_round_ids = [str(row[0]) for row in orphaned_rounds]
        continuation_round_ids = (
            {
                str(row[0])
                for row in (
                    db.query(AgentInteraction.round_id)
                    .filter(
                        AgentInteraction.round_id.in_(running_round_ids),
                        AgentInteraction.status == "pending",
                        AgentInteraction.answer_payload.isnot(None),
                    )
                    .all()
                )
                if isinstance(row[0], str) and row[0]
            }
            if running_round_ids
            else set()
        )
        orphaned_rounds.sort(
            key=lambda row: (
                str(row[0]) not in continuation_round_ids,
                str(row[0]),
            )
        )
        for orphaned_round in orphaned_rounds:
            if _has_surviving_lock(orphaned_round):
                continue
            round_id = str(orphaned_round[0])
            owner_session_id = _owner_session_id(orphaned_round)
            if owner_session_id in active_unlocked_sessions:
                continue
            round_obj = (
                AgentInteractionService.lock_running_round_for_terminal_cleanup(
                    db,
                    session_id=owner_session_id,
                    round_id=round_id,
                )
                if owner_session_id is not None
                else (
                    db.query(Round)
                    .filter(Round.id == round_id, Round.status == "running")
                    .with_for_update()
                    .first()
                )
            )
            if round_obj is None or round_obj.status != "running":
                db.rollback()
                continue
            if AgentInteractionService.has_active_continuation_work(
                db,
                round_id=round_id,
            ):
                if owner_session_id is not None:
                    active_unlocked_sessions.add(owner_session_id)
                db.rollback()
                logger.info(
                    "孤儿清理跳过仍有有效 continuation 或工具执行 lease 的 Round: run=%s",
                    round_id,
                )
                continue
            interaction_kind = (
                AgentInteractionService.lock_irrecoverable_continuation_round_for_failure(
                    db,
                    round_id=round_id,
                )
            )
            if interaction_kind == "tool_approval":
                final_response = _continuation_failure_response(interaction_kind)
            elif interaction_kind == "user_input":
                final_response = _continuation_failure_response(interaction_kind)
            else:
                final_response = (
                    round_obj.final_response or "Worker crashed, round orphaned"
                )
            stored_terminal = RunCompletionService(db).complete_sync(
                run_id=round_id,
                status="failed",
                final_response=final_response,
                step_count=round_obj.step_count or 0,
            )
            if stored_terminal is not None:
                get_agui_event_bus().publish_committed_nowait(
                    round_id,
                    stored_terminal.event,
                )
                failed_round_ids.append(round_id)
        logger.warning(
            "已回收 %d 个孤儿 round: user=%s round_ids=%s",
            len(failed_round_ids),
            user_id,
            failed_round_ids,
        )
        return len(failed_round_ids), active_unlocked_sessions

    async def release_user_run_lock(
        self,
        db: DBSession,
        *,
        user_id: str,
        lock_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        def _do():
            query = db.query(UserRunLock).filter(UserRunLock.user_id == user_id)
            if lock_id is not None:
                query = query.filter(UserRunLock.lock_id == lock_id)
            elif session_id is not None:
                query = query.filter(UserRunLock.session_id == session_id)
            else:
                query.delete(synchronize_session=False)
                db.commit()
                return True

            lock_row = query.first()
            if not lock_row:
                if session_id is not None:
                    same_session_lock = (
                        db.query(UserRunLock)
                        .filter(
                            UserRunLock.user_id == user_id,
                            UserRunLock.session_id == session_id,
                        )
                        .first()
                    )
                    db.rollback()
                    return same_session_lock is None
                if lock_id is not None:
                    any_lock = db.query(UserRunLock).filter(UserRunLock.user_id == user_id).first()
                    db.rollback()
                    if any_lock:
                        return False
                db.rollback()
                return True

            db.delete(lock_row)
            db.commit()
            return True

        try:
            return bool(await _with_db_retry(_do, rollback=db.rollback))
        except OperationalError:
            logger.warning(
                "释放用户运行锁失败: user=%s lock_id=%s session=%s",
                user_id,
                lock_id,
                session_id,
                exc_info=True,
            )
            return False
        except Exception:
            logger.warning(
                "释放用户运行锁异常: user=%s lock_id=%s session=%s",
                user_id,
                lock_id,
                session_id,
                exc_info=True,
            )
            return False

    async def release_user_run_lock_in_new_session(
        self,
        *,
        user_id: str,
        lock_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        try:
            with self.session_factory() as db:
                return await self.release_user_run_lock(
                    db,
                    user_id=user_id,
                    lock_id=lock_id,
                    session_id=session_id,
                )
        except Exception:
            logger.warning(
                "释放用户运行锁失败: user=%s lock_id=%s session=%s",
                user_id,
                lock_id,
                session_id,
                exc_info=True,
            )
            return False


_GLOBAL_RUN_COORDINATOR = RunCoordinator()


def get_run_coordinator() -> RunCoordinator:
    return _GLOBAL_RUN_COORDINATOR
