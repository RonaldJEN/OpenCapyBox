"""去中心化 Cron worker。

每个 worker 独立运行：
- 到点唤醒扫描 enabled 任务
- 通过 cron_fires 的 UNIQUE 约束去重抢占执行权
- 抢到执行权后立即并发执行，不按用户串行排队
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from contextlib import suppress
from datetime import date, datetime, timedelta

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.exc import OperationalError

from src.api.models.auth_user import AuthUser
from src.api.models.cron_fire import CronFire
from src.api.models.cron_job import CronJob
from src.api.models.database import SessionLocal
from src.api.services.cron_service import parse_cron_fields, run_cron_job
from src.api.utils.timezone import get_timezone, now_naive
from src.api.config import get_settings

logger = logging.getLogger(__name__)

# 防止 create_task 返回的 Task 被 GC 回收导致后台任务中途丢失
_background_tasks: set[asyncio.Task] = set()

# Worker 与 API 共用事件循环；当同进程出现短暂阻塞时，醒来后补扫最近的
# 分钟，避免只看“当前分钟”导致整点任务静默漏发。
DEFAULT_DISPATCH_CATCH_UP_MINUTES = 60


def _spawn(coro) -> asyncio.Task:
    """受控创建后台任务：保留强引用并自动清理。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def start_cron_worker(app) -> None:
    """启动当前进程的 cron worker。"""
    worker_id = uuid.uuid4().hex
    app.state.cron_worker_id = worker_id
    app.state.cron_worker_task = asyncio.create_task(
        _cron_worker_loop(worker_id)
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


async def _cron_worker_loop(worker_id: str) -> None:
    """主循环：到点唤醒后执行 dispatch + run。

    时区：以本地时区（`TIMEZONE_OFFSET`，默认 UTC+8）为基准匹配 cron 表达式，
    确保用户配置的 `"0 9 * * *"` 在本地 9 点而非 UTC 9 点触发。
    """
    await asyncio.sleep(random.uniform(0, 2.0))
    tz = get_timezone()
    catch_up_limit = max(
        1,
        int(
            getattr(
                get_settings(),
                "cron_dispatch_catch_up_max_minutes",
                DEFAULT_DISPATCH_CATCH_UP_MINUTES,
            )
        ),
    )
    last_dispatched_minute = _floor_to_minute(datetime.now(tz).replace(tzinfo=None))
    last_cleanup_date: date | None = None
    while True:
        try:
            now = datetime.now(tz).replace(tzinfo=None)
            next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
            await asyncio.sleep((next_minute - now).total_seconds())
            current_minute = _floor_to_minute(datetime.now(tz).replace(tzinfo=None))
            last_dispatched_minute, last_cleanup_date = await _dispatch_due_minutes(
                worker_id,
                last_dispatched_minute,
                current_minute,
                last_cleanup_date,
                catch_up_limit,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cron_worker loop error worker=%s", worker_id)
            await asyncio.sleep(5)


def _floor_to_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0, tzinfo=None)


def _due_minutes_after(
    last_dispatched_minute: datetime,
    current_minute: datetime,
    max_catch_up_minutes: int = DEFAULT_DISPATCH_CATCH_UP_MINUTES,
) -> tuple[list[datetime], int]:
    """Return missed minute ticks after ``last_dispatched_minute``.

    The worker intentionally does not backfill before process startup; this helper
    only fills gaps observed after the loop has already started. Very large gaps
    are capped so a suspended process cannot stampede the system on resume.
    """
    last_minute = _floor_to_minute(last_dispatched_minute)
    current = _floor_to_minute(current_minute)
    if current <= last_minute:
        return [], 0

    total_minutes = int((current - last_minute).total_seconds() // 60)
    catch_up_limit = max(1, int(max_catch_up_minutes))
    dropped = max(0, total_minutes - catch_up_limit)
    first_due = last_minute + timedelta(minutes=dropped + 1)
    due_count = total_minutes - dropped
    return [first_due + timedelta(minutes=i) for i in range(due_count)], dropped


async def _dispatch_due_minutes(
    worker_id: str,
    last_dispatched_minute: datetime,
    current_minute: datetime,
    last_cleanup_date: date | None,
    catch_up_limit: int,
) -> tuple[datetime, date | None]:
    due_minutes, dropped = _due_minutes_after(
        last_dispatched_minute,
        current_minute,
        catch_up_limit,
    )
    if dropped:
        logger.warning(
            "cron worker=%s skipped %d old missed minute(s); catch_up_limit=%d current=%s",
            worker_id,
            dropped,
            catch_up_limit,
            current_minute,
        )
    elif len(due_minutes) > 1:
        logger.warning(
            "cron worker=%s catching up %d missed minute(s): %s -> %s",
            worker_id,
            len(due_minutes) - 1,
            due_minutes[0],
            due_minutes[-1],
        )

    for minute in due_minutes:
        await _dispatch_and_run(worker_id, minute)
        last_dispatched_minute = minute

        # 每天凌晨第一次触发时清理过期 cron_fires。
        # 即使多 worker 同时清理也无害：DELETE 幂等，最终状态一致。
        if last_cleanup_date != minute.date() and minute.hour == 0:
            _cleanup_old_fires()
            last_cleanup_date = minute.date()

    return last_dispatched_minute, last_cleanup_date


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
        jobs = (
            db.query(CronJob)
            .join(AuthUser, AuthUser.user_id == CronJob.user_id)
            .filter(CronJob.enabled == True, AuthUser.enabled == True)  # noqa: E712
            .all()
        )
        return [(j.id, j.user_id, j.name, j.cron_expr) for j in jobs]


async def _dispatch_and_run(
    worker_id: str,
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
            _spawn(_run_by_id(job_id, worker_id))


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
    """尝试插入去重记录：成功插入返回 True，唯一约束冲突返回 False。

    PostgreSQL 使用 ON CONFLICT DO NOTHING。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    try:
        with SessionLocal() as db:
            values = dict(
                id=str(uuid.uuid4()),
                job_id=job_id,
                scheduled_at=minute,
            )
            stmt = pg_insert(CronFire).values(**values).on_conflict_do_nothing(
                constraint="uq_cronfire_job_time"
            )
            result = db.execute(stmt)
            db.commit()
            return result.rowcount == 1
    except OperationalError as e:
        # 只有死锁 / 序列化失败属于预期抢占冲突，其余（例如 no such table）
        # 都是严重配置错误，必须响亮报错，避免静默吞掉导致 cron 看起来"没运行"
        pgcode = getattr(getattr(e, "orig", None), "pgcode", None)
        if pgcode in ("40001", "40P01"):
            logger.debug(
                "dispatch db conflict job_id=%s minute=%s err=%s",
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
    worker_id: str,
    run_id: str | None = None,
    scheduled_job_id: int | None = None,
) -> None:
    """执行单个任务，cron 与手动触发共用。

    当 scheduled_job_id 不为空（自动调度路径）时，在真正执行前按 job_id
    做一次 enabled 校验，避免“快照已启用 -> 执行前被禁用/删除”导致空跑。
    """
    actual_run_id = run_id or str(uuid.uuid4())
    source = "manual" if run_id else "scheduled"

    if scheduled_job_id is not None:
        # 自动调度路径：在真正执行前做二次校验，收敛竞态窗口。
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
    await _run(user_id, name, worker_id, scheduled_job_id=job_id)


def _load_job_snapshot_if_enabled(job_id: int) -> tuple[str, str] | None:
    """同步读取单个 job 的 (user_id, name)，仅在 job 和用户账号均启用时返回。"""
    with SessionLocal() as db:
        job = (
            db.query(CronJob)
            .join(AuthUser, AuthUser.user_id == CronJob.user_id)
            .filter(
                CronJob.id == job_id,
                CronJob.enabled == True,  # noqa: E712
                AuthUser.enabled == True,  # noqa: E712
            )
            .first()
        )
        if job is None:
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
        app: FastAPI app 实例（用于读取 worker_id）
        user_id: 任务所属用户
        job_name: 任务名称
        run_id: 调用方预先生成并已写入 CronJobRun 的执行 id

    Returns:
        受控的后台 Task，调用方通常无需 await。

    Raises:
        HTTPException(503): cron worker 尚未启动（startup 失败或被跳过）。
            此时不允许悄悄回退到独立后台任务，避免 worker 生命周期与任务
            状态收敛语义分裂。
    """
    worker_id = getattr(app.state, "cron_worker_id", None)
    if worker_id is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail="cron worker 未启动，无法手动触发任务",
        )

    return _spawn(_run(user_id, job_name, worker_id, run_id=run_id))
