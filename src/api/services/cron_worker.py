"""去中心化 Cron worker。

每个 worker 独立运行：
- 到点唤醒扫描 enabled 任务
- 通过 cron_fires 的 UNIQUE 约束去重抢占执行权
- 同用户执行串行化
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from contextlib import suppress
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import insert
from sqlalchemy.exc import OperationalError

from src.api.models.cron_fire import CronFire
from src.api.models.cron_job import CronJob
from src.api.models.database import SessionLocal
from src.api.services.cron_service import parse_cron_fields, run_cron_job
from src.api.utils.timezone import get_timezone, now_naive
from src.api.config import get_settings

logger = logging.getLogger(__name__)

# 防止 create_task 返回的 Task 被 GC 回收导致后台任务中途丢失
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """受控创建后台任务：保留强引用并自动清理。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _get_or_create_user_lock(user_locks: dict[str, asyncio.Lock], user_id: str) -> asyncio.Lock:
    lock = user_locks.get(user_id)
    if lock is None:
        lock = user_locks.setdefault(user_id, asyncio.Lock())
    return lock


async def start_cron_worker(app) -> None:
    """启动当前进程的 cron worker。"""
    worker_id = uuid.uuid4().hex
    user_locks: dict[str, asyncio.Lock] = {}
    app.state.cron_worker_id = worker_id
    app.state.cron_user_locks = user_locks
    app.state.cron_worker_task = asyncio.create_task(
        _cron_worker_loop(worker_id, user_locks)
    )
    logger.info("cron_worker started worker_id=%s", worker_id)


async def stop_cron_worker(app) -> None:
    """停止当前进程的 cron worker 与其后台任务。

    优雅停机：先等待 in-flight 任务最多 30s，超时再强制 cancel。
    """
    task = getattr(app.state, "cron_worker_task", None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    pending = [t for t in list(_background_tasks) if not t.done()]
    if pending:
        done, still_pending = await asyncio.wait(pending, timeout=30)
        for t in still_pending:
            t.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await t
    _background_tasks.clear()


async def _cron_worker_loop(worker_id: str, user_locks: dict[str, asyncio.Lock]) -> None:
    """主循环：到点唤醒后执行 dispatch + run。

    时区：以本地时区（`TIMEZONE_OFFSET`，默认 UTC+8）为基准匹配 cron 表达式，
    确保用户配置的 `"0 9 * * *"` 在本地 9 点而非 UTC 9 点触发。
    """
    await asyncio.sleep(random.uniform(0, 2.0))
    tz = get_timezone()
    last_cleanup_date: datetime | None = None
    while True:
        try:
            now = datetime.now(tz).replace(tzinfo=None)
            next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
            await asyncio.sleep((next_minute - now).total_seconds())
            minute = datetime.now(tz).replace(second=0, microsecond=0, tzinfo=None)
            await _dispatch_and_run(worker_id, user_locks, minute)

            # 每天凌晨第一次触发时清理过期 cron_fires。
            # 即使多 worker 同时清理也无害：DELETE 幂等，且走 SQLite 写锁串行。
            if last_cleanup_date != minute.date() and minute.hour == 0:
                _cleanup_old_fires()
                last_cleanup_date = minute.date()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cron_worker loop error worker=%s", worker_id)
            await asyncio.sleep(5)


def _cleanup_old_fires() -> None:
    """删除超过 `cron_fire_max_age_days` 的 cron_fires 行，防止表单调膨胀。"""
    settings = get_settings()
    max_age_days = getattr(settings, "cron_fire_max_age_days", 7)
    if max_age_days <= 0:
        return
    threshold = now_naive() - timedelta(days=max_age_days)
    try:
        with SessionLocal() as db:
            deleted = (
                db.query(CronFire)
                .filter(CronFire.scheduled_at < threshold)
                .delete(synchronize_session=False)
            )
            db.commit()
            if deleted:
                logger.info("cron_fires 清理完成：删除 %d 行（早于 %s）", deleted, threshold)
    except Exception:
        logger.exception("cron_fires 清理失败")


def _load_enabled_job_snapshots() -> list[tuple[int, str, str, str]]:
    """同步读取所有 enabled job 的轻量快照，供 dispatch 使用。"""
    with SessionLocal() as db:
        jobs = db.query(CronJob).filter(CronJob.enabled == True).all()  # noqa: E712
        return [(j.id, j.user_id, j.name, j.cron_expr) for j in jobs]


async def _dispatch_and_run(
    worker_id: str,
    user_locks: dict[str, asyncio.Lock],
    minute: datetime,
) -> None:
    # 同步 DB 调用一律放线程，避免阻塞事件循环（影响同进程的 SSE / 长连接 jitter）
    job_snapshots = await asyncio.to_thread(_load_enabled_job_snapshots)

    for job_id, user_id, name, cron_expr in job_snapshots:
        # per-job 隔离：单个 job 的解析/匹配/抢占异常不能影响其他 job 的本分钟调度
        try:
            if not _cron_matches_minute(cron_expr, minute):
                continue
            won = await asyncio.to_thread(_try_insert_fire, job_id, minute)
        except Exception:
            logger.exception(
                "cron dispatch error job_id=%s user=%s name=%s expr=%s",
                job_id, user_id, name, cron_expr,
            )
            continue
        if won:
            _spawn(_run_by_id(job_id, user_locks, worker_id))


def _cron_matches_minute(expr: str, minute: datetime) -> bool:
    """使用本地时区匹配 cron 表达式。

    `minute` 为本地时间的 naive datetime。
    """
    fields = parse_cron_fields(expr)
    if fields is None:
        return False

    tz = get_timezone()
    trigger = CronTrigger(**fields, timezone=tz)
    # 把 naive 本地时间补上本地时区信息，供 APScheduler 匹配
    minute_local = minute.replace(tzinfo=tz)
    next_fire = trigger.get_next_fire_time(None, minute_local - timedelta(seconds=1))
    return next_fire is not None and next_fire == minute_local


def _try_insert_fire(job_id: int, minute: datetime) -> bool:
    """INSERT OR IGNORE：成功插入返回 True。"""
    try:
        with SessionLocal() as db:
            result = db.execute(
                insert(CronFire)
                .prefix_with("OR IGNORE")
                .values(
                    id=str(uuid.uuid4()),
                    job_id=job_id,
                    scheduled_at=minute,
                )
            )
            db.commit()
            return result.rowcount == 1
    except OperationalError as e:
        # 只有 "database is locked / busy" 属于预期抢占失败，其余（例如 no such table）
        # 都是严重配置错误，必须响亮报错，避免静默吞掉导致 cron 看起来"没运行"
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            logger.debug(
                "dispatch sqlite busy job_id=%s minute=%s err=%s",
                job_id,
                minute,
                e,
            )
        else:
            logger.error(
                "dispatch failed job_id=%s minute=%s err=%s (请检查 DB schema 是否已初始化)",
                job_id,
                minute,
                e,
            )
        return False


async def _run(
    user_id: str,
    job_name: str,
    user_locks: dict[str, asyncio.Lock],
    worker_id: str,
    run_id: str | None = None,
    scheduled_job_id: int | None = None,
) -> None:
    """执行单个任务，cron 与手动触发共用。

    当 scheduled_job_id 不为空（自动调度路径）时，拿到 per-user 锁后
    再按 job_id 做一次 enabled 校验，避免“快照已启用 -> 排队等待锁 ->
    用户在等待期间禁用任务”导致的空跑。
    """
    lock = _get_or_create_user_lock(user_locks, user_id)
    actual_run_id = run_id or str(uuid.uuid4())
    source = "manual" if run_id else "scheduled"

    async with lock:
        if scheduled_job_id is not None:
            # 自动调度路径：在真正执行前（已拿锁）做二次校验，收敛竞态窗口。
            latest = await asyncio.to_thread(_load_job_snapshot_if_enabled, scheduled_job_id)
            if latest is None:
                logger.info(
                    "cron skip stale job before execute job_id=%s worker=%s (deleted or disabled)",
                    scheduled_job_id,
                    worker_id,
                )
                return

        logger.info(
            "cron start worker=%s user=%s job=%s run=%s source=%s",
            worker_id,
            user_id,
            job_name,
            actual_run_id,
            source,
        )
        try:
            await run_cron_job(user_id, job_name, actual_run_id)
        except Exception:
            logger.exception(
                "cron run failed user=%s job=%s run=%s",
                user_id,
                job_name,
                actual_run_id,
            )


async def _run_by_id(
    job_id: int,
    user_locks: dict[str, asyncio.Lock],
    worker_id: str,
) -> None:
    """按 job_id 重新从 DB 拉取 job 并执行。

    防止快照到 _run 之间 job 已被删除或禁用导致空跑。
    """
    snapshot = await asyncio.to_thread(_load_job_snapshot_if_enabled, job_id)
    if snapshot is None:
        logger.info(
            "cron skip stale job job_id=%s worker=%s (deleted or disabled)",
            job_id,
            worker_id,
        )
        return

    user_id, name = snapshot
    await _run(user_id, name, user_locks, worker_id, scheduled_job_id=job_id)


def _load_job_snapshot_if_enabled(job_id: int) -> tuple[str, str] | None:
    """同步读取单个 job 的 (user_id, name)，仅在 enabled 时返回。"""
    with SessionLocal() as db:
        job = db.query(CronJob).filter(CronJob.id == job_id).first()
        if job is None or not job.enabled:
            return None
        return job.user_id, job.name


# ============== 公共 API ==============


async def trigger_manual_run(
    app,
    user_id: str,
    job_name: str,
    run_id: str,
) -> asyncio.Task:
    """手动触发指定任务的后台执行。

    路由层应仅通过此函数与 worker 交互，避免直接 import 私有符号。

    Args:
        app: FastAPI app 实例（用于读取共享的 user_locks / worker_id）
        user_id: 任务所属用户
        job_name: 任务名称
        run_id: 调用方预先生成并已写入 CronJobRun 的执行 id

    Returns:
        受控的后台 Task，调用方通常无需 await。

    Raises:
        HTTPException(503): cron worker 尚未启动（startup 失败或被跳过）。
            此时不允许悄悄回退到独立 dict 跑任务，否则会与未来上线的 worker
            分裂出两套 per-user 锁，破坏"同用户串行"的不变量。
    """
    user_locks = getattr(app.state, "cron_user_locks", None)
    worker_id = getattr(app.state, "cron_worker_id", None)
    if user_locks is None or worker_id is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail="cron worker 未启动，无法手动触发任务",
        )

    return _spawn(_run(user_id, job_name, user_locks, worker_id, run_id=run_id))
