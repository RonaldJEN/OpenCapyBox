"""Run coordination for user/session execution slots."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable

from sqlalchemy import insert, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.database import SessionLocal
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.user_run_lock import UserRunLock
from src.api.services.run_completion_service import RunCompletionService
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)


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
        settings = self.settings_provider()
        stale_threshold_seconds = max(settings.sse_subscribe_timeout, 1)
        concurrency_limit = max(int(getattr(settings, "agent_user_concurrency_limit", 1) or 1), 1)
        max_busy_retries = 5
        retry_interval_seconds = 0.1

        with self.session_factory() as lock_db:
            def _lock_age_seconds(lock: UserRunLock) -> float:
                heartbeat_at = lock.updated_at or lock.created_at
                return (now_naive() - heartbeat_at).total_seconds()

            def _delete_stale_locks() -> list[str]:
                locks = (
                    lock_db.query(UserRunLock)
                    .filter(UserRunLock.user_id == user_id)
                    .all()
                )
                stale_session_ids: list[str] = []
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
                    stale_session_ids.append(lock.session_id)
                    lock_db.delete(lock)
                if stale_session_ids:
                    lock_db.commit()
                return stale_session_ids

            for attempt in range(max_busy_retries):
                try:
                    stale_session_ids = _delete_stale_locks()
                    for stale_session_id in stale_session_ids:
                        try:
                            self.cleanup_orphaned_rounds(
                                lock_db,
                                user_id=user_id,
                                session_id=stale_session_id,
                            )
                        except Exception:
                            logger.warning(
                                "清理孤儿 round 失败: user=%s session=%s",
                                user_id,
                                stale_session_id,
                                exc_info=True,
                            )

                    active_locks = (
                        lock_db.query(UserRunLock)
                        .filter(UserRunLock.user_id == user_id)
                        .all()
                    )
                    if any(lock.session_id == session_id for lock in active_locks):
                        return None
                    if len(active_locks) >= concurrency_limit:
                        return None

                    used_slots = {lock.slot for lock in active_locks}
                    slot = next((i for i in range(concurrency_limit) if i not in used_slots), None)
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

    def cleanup_orphaned_rounds(
        self,
        db: DBSession,
        *,
        user_id: str,
        session_id: str | None = None,
    ) -> int:
        if session_id is not None:
            session_filter = or_(Round.session_id == session_id, Round.thread_id == session_id)
        else:
            user_session_ids_stmt = select(Session.id).where(Session.user_id == user_id)
            session_filter = or_(
                Round.session_id.in_(user_session_ids_stmt),
                Round.thread_id.in_(user_session_ids_stmt),
            )
        orphaned_rounds = (
            db.query(Round)
            .filter(
                Round.status == "running",
                session_filter,
            )
            .all()
        )
        if not orphaned_rounds:
            return 0
        for r in orphaned_rounds:
            RunCompletionService(db).complete_sync(
                run_id=r.id,
                status="cancelled",
                final_response=r.final_response or "Worker crashed, round orphaned",
                step_count=r.step_count or 0,
            )
        logger.warning(
            "已回收 %d 个孤儿 round: user=%s round_ids=%s",
            len(orphaned_rounds),
            user_id,
            [r.id for r in orphaned_rounds],
        )
        return len(orphaned_rounds)

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
