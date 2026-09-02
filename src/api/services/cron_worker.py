"""去中心化 Cron worker。

每个 worker 独立运行：
- 到点唤醒扫描 enabled 任务
- 通过 cron_fires 的 UNIQUE 约束去重抢占执行权
- 抢到执行权后立即并发执行，不按用户串行排队
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from contextlib import suppress
from datetime import date, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.exc import OperationalError

from src.api.models.auth_user import AuthUser
from src.api.models.cron_fire import CronFire
from src.api.models.cron_job import CronJob
from src.api.models.database import SessionLocal
from src.api.models.user_memory import CronJobRun
from src.api.models.user_sandbox import UserSandbox
from src.api.services.cron_engine import CronEngine
from src.api.services.cron_service import (
    build_cron_definition_snapshot,
    build_cron_workspace_change_set_summaries,
    run_cron_job,
)
from src.api.utils.timezone import get_timezone, now_naive
from src.api.config import get_settings

logger = logging.getLogger(__name__)

# 防止 create_task 返回的 Task 被 GC 回收导致后台任务中途丢失
_background_tasks: set[asyncio.Task] = set()
_local_run_ids: set[str] = set()

# Worker 与 API 共用事件循环；当同进程出现短暂阻塞时，醒来后补扫最近的
# 分钟，避免只看“当前分钟”导致整点任务静默漏发。
DEFAULT_DISPATCH_CATCH_UP_MINUTES = 60


def _spawn(coro) -> asyncio.Task:
    """受控创建后台任务：保留强引用并自动清理。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _settled(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.error(
                "cron background task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(_settled)
    return task


def _spawn_queued_run(run_id: str, worker_id: str) -> asyncio.Task | None:
    """Avoid duplicate local claim tasks while DB fencing handles other workers."""
    if run_id in _local_run_ids:
        return None
    _local_run_ids.add(run_id)

    async def _guarded() -> None:
        try:
            await _run_queued(run_id, worker_id)
        finally:
            _local_run_ids.discard(run_id)

    return _spawn(_guarded())


async def start_cron_worker(app) -> None:
    """启动当前进程的 cron worker。"""
    worker_id = uuid.uuid4().hex
    app.state.cron_worker_id = worker_id
    await asyncio.to_thread(reconcile_expired_cron_runs)
    await _spawn_queued_runs(worker_id)
    app.state.cron_worker_task = asyncio.create_task(
        _cron_worker_loop(worker_id)
    )
    app.state.cron_reconciler_task = asyncio.create_task(
        _cron_reconciler_loop(worker_id)
    )
    logger.info("cron_worker started worker_id=%s", worker_id)


async def stop_cron_worker(app) -> None:
    """停止当前进程的 cron worker 与其后台任务。

    优雅停机：先等待 in-flight 任务最多 30s，超时再强制 cancel。
    """
    for task_name in ("cron_worker_task", "cron_reconciler_task"):
        task = getattr(app.state, task_name, None)
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
    _local_run_ids.clear()


async def _cron_reconciler_loop(worker_id: str) -> None:
    """Recover expired claims and dispatch durable queued runs."""
    interval = max(
        1.0,
        float(get_settings().cron_reconcile_interval_seconds),
    )
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(reconcile_expired_cron_runs)
            await _spawn_queued_runs(worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cron reconciler loop error worker=%s", worker_id)


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


def _load_enabled_job_snapshots() -> list[tuple[int, str, str, str, int, int]]:
    """同步读取所有 enabled job 的轻量快照，供 dispatch 使用。"""
    with SessionLocal() as db:
        jobs = (
            db.query(CronJob)
            .join(AuthUser, AuthUser.user_id == CronJob.user_id)
            .filter(CronJob.enabled == True, AuthUser.enabled == True)  # noqa: E712
            .all()
        )
        return [
            (
                j.id,
                j.user_id,
                j.name,
                j.cron_expr,
                int(j.rule_version or 1),
                int(j.definition_version or 1),
            )
            for j in jobs
        ]


async def _dispatch_and_run(
    worker_id: str,
    minute: datetime,
) -> None:
    # 同步 DB 调用一律放线程，避免阻塞事件循环（影响同进程的 SSE / 长连接 jitter）
    job_snapshots = await asyncio.to_thread(_load_enabled_job_snapshots)

    for job_id, user_id, name, cron_expr, rule_version, definition_version in job_snapshots:
        # per-job 隔离：单个 job 的解析/匹配/抢占异常不能影响其他 job 的本分钟调度
        try:
            if not _cron_matches_minute(cron_expr, minute):
                continue
            run_id = await asyncio.to_thread(
                _enqueue_scheduled_run,
                job_id,
                minute,
                rule_version,
                definition_version,
            )
        except Exception:
            logger.exception(
                "cron dispatch error job_id=%s user=%s name=%s expr=%s",
                job_id, user_id, name, cron_expr,
            )
            continue
        if run_id:
            _spawn_queued_run(run_id, worker_id)


def _cron_matches_minute(expr: str, minute: datetime) -> bool:
    """使用本地时区匹配 cron 表达式。

    `minute` 为本地时间的 naive datetime。
    """
    return CronEngine.matches(expr, minute)


def _enqueue_scheduled_run(
    job_id: int,
    minute: datetime,
    rule_version: int = 1,
    definition_version: int | None = None,
) -> str | None:
    """Atomically acquire a Fire and create its durable queued run.

    Fire without a run is not an execution intent. Keeping both writes in one
    transaction closes the crash window between minute dedupe and task spawn.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    try:
        with SessionLocal() as db:
            job = (
                db.query(CronJob)
                .join(AuthUser, AuthUser.user_id == CronJob.user_id)
                .filter(
                    CronJob.id == job_id,
                    CronJob.enabled == True,  # noqa: E712
                    AuthUser.enabled == True,  # noqa: E712
                    CronJob.rule_version == rule_version,
                )
                .first()
            )
            if job is None:
                return None
            current_definition_version = int(job.definition_version or 1)
            if (
                definition_version is not None
                and current_definition_version != definition_version
            ):
                logger.info(
                    "cron definition advanced before durable enqueue; using latest "
                    "job_id=%s expected=%s actual=%s",
                    job_id,
                    definition_version,
                    current_definition_version,
                )
            snapshot = build_cron_definition_snapshot(job)
            run_id = str(uuid.uuid4())
            fire_id = str(uuid.uuid4())
            values = dict(
                id=fire_id,
                job_id=job_id,
                scheduled_at=minute,
                rule_version=rule_version,
                definition_version=current_definition_version,
                run_id=run_id,
            )
            stmt = pg_insert(CronFire).values(**values).on_conflict_do_nothing(
                constraint="uq_cronfire_job_time"
            )
            result = db.execute(stmt)
            if result.rowcount != 1:
                db.rollback()
                return None
            db.add(CronJobRun(
                id=run_id,
                user_id=job.user_id,
                job_id=job.id,
                fire_id=fire_id,
                job_name=job.name,
                cron_expr=job.cron_expr,
                rule_version=rule_version,
                definition_version=current_definition_version,
                definition_snapshot=json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                scheduled_at=minute,
                trigger_source="scheduled",
                status="queued",
                phase="queued",
                is_read=False,
            ))
            db.commit()
            return run_id
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
        return None


def _try_insert_fire(
    job_id: int,
    minute: datetime,
    rule_version: int = 1,
    definition_version: int | None = None,
) -> bool:
    """Compatibility wrapper around the durable enqueue operation."""
    return _enqueue_scheduled_run(
        job_id,
        minute,
        rule_version,
        definition_version,
    ) is not None


def _claim_queued_run(run_id: str, worker_id: str) -> dict | None:
    """Claim one durable run with a renewable fencing token."""
    lease_seconds = max(
        1.0,
        float(get_settings().cron_claim_lease_seconds),
    )
    claimed_at = now_naive()
    with SessionLocal() as db:
        record = (
            db.query(CronJobRun)
            .filter(CronJobRun.id == run_id)
            .with_for_update()
            .first()
        )
        if record is None or record.status != "queued":
            return None
        sandbox_row = (
            db.query(UserSandbox.sandbox_id)
            .filter(UserSandbox.user_id == record.user_id)
            .first()
        )
        frozen_sandbox_id = sandbox_row[0] if sandbox_row else None
        record.sandbox_id = (
            frozen_sandbox_id
            if isinstance(frozen_sandbox_id, str) and frozen_sandbox_id
            else None
        )
        claim_token = str(uuid.uuid4())
        record.status = "running"
        record.phase = "preparing"
        record.claim_token = claim_token
        record.claim_worker_id = worker_id
        record.claim_lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        record.heartbeat_at = claimed_at
        record.started_at = record.started_at or claimed_at
        record.attempt_count = int(record.attempt_count or 0) + 1
        payload = {
            "run_id": record.id,
            "user_id": record.user_id,
            "job_name": record.job_name,
            "job_id": record.job_id,
            "rule_version": record.rule_version,
            "scheduled_at": record.scheduled_at,
            "trigger_source": record.trigger_source,
            "claim_token": claim_token,
            "sandbox_id": record.sandbox_id,
        }
        db.commit()
        return payload


def _renew_run_claim(run_id: str, claim_token: str) -> bool:
    renewed_at = now_naive()
    lease_seconds = max(
        1.0,
        float(get_settings().cron_claim_lease_seconds),
    )
    with SessionLocal() as db:
        updated = (
            db.query(CronJobRun)
            .filter(
                CronJobRun.id == run_id,
                CronJobRun.status == "running",
                CronJobRun.claim_token == claim_token,
                CronJobRun.claim_lease_expires_at > renewed_at,
            )
            .update(
                {
                    "heartbeat_at": renewed_at,
                    "claim_lease_expires_at": renewed_at + timedelta(seconds=lease_seconds),
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return updated == 1


def _run_claim_state(run_id: str) -> str:
    with SessionLocal() as db:
        row = db.query(CronJobRun.status).filter(CronJobRun.id == run_id).first()
        if row is None:
            return "missing"
        return str(row[0])


async def _claim_heartbeat_loop(
    run_id: str,
    claim_token: str,
    stop: asyncio.Event,
    execution_task: asyncio.Task,
) -> None:
    interval = max(
        1.0,
        float(get_settings().cron_claim_heartbeat_seconds),
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            renewed = await asyncio.to_thread(_renew_run_claim, run_id, claim_token)
        except Exception:
            logger.exception(
                "cron heartbeat failed; cancelling execution conservatively run=%s",
                run_id,
            )
            execution_task.cancel()
            return
        if not renewed:
            state = await asyncio.to_thread(_run_claim_state, run_id)
            if state in {"success", "failed", "conflict"}:
                return
            logger.error(
                "cron claim lost; cancelling execution run=%s state=%s",
                run_id,
                state,
            )
            execution_task.cancel()
            return


def reconcile_expired_cron_runs(
    *,
    at: datetime | None = None,
    db=None,
    commit: bool = True,
) -> tuple[int, int]:
    """Recover pre-execution claims and conservatively close started work.

    A preparing run has not crossed the Agent dispatch boundary and can be
    queued again with the same run id. Executing/publishing work may already
    have caused side effects, so it converges to ``unknown`` and is never
    automatically replayed.
    """
    now = at or now_naive()
    requeued = 0
    unknown = 0
    owns_session = db is None
    session = db or SessionLocal()
    try:
        expired = (
            session.query(CronJobRun)
            .filter(
                CronJobRun.status == "running",
                or_(
                    CronJobRun.claim_lease_expires_at.is_(None),
                    CronJobRun.claim_lease_expires_at <= now,
                ),
            )
            .with_for_update()
            .all()
        )
        for record in expired:
            can_requeue = record.phase == "preparing" and bool(record.claim_token)
            if can_requeue:
                record.status = "queued"
                record.phase = "queued"
                record.started_at = None
                record.sandbox_id = None
                record.output = "[执行 worker 在 Agent 启动前失联，任务已重新排队]"
                record.error_code = None
                requeued += 1
            else:
                record.status = "unknown"
                record.phase = "terminal"
                record.completed_at = now
                record.output = "[执行 lease 已过期；任务可能已产生副作用，不会自动重试]"
                record.error_code = "worker_lease_expired_after_start"
                unknown += 1
            record.claim_token = None
            record.claim_worker_id = None
            record.claim_lease_expires_at = None
        _reconcile_workspace_change_summaries(session)
        if commit:
            session.commit()
        else:
            session.flush()
    finally:
        if owns_session:
            session.close()
    return requeued, unknown


def _reconcile_workspace_change_summaries(db, *, limit: int = 1000) -> int:
    """Repair CronJobRun summaries from the authoritative mutation journal."""
    from src.api.models.workspace import WorkspaceChangeSet, WorkspaceEntry, WorkspaceMutation

    def _mutation_details(mutation) -> dict:
        try:
            details = json.loads(mutation.details_json or "{}")
        except (TypeError, ValueError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        return details

    def _mutation_path(details, journal, entry) -> str | None:
        for candidate in (
            journal.get("target_path"),
            next((item.get("relative_path") for item in journal.get("root_projections", []) if isinstance(item, dict)), None),
            details.get("to"),
            details.get("original_path"),
            getattr(entry, "relative_path", None),
        ):
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    rows = (
        db.query(WorkspaceMutation, WorkspaceEntry)
        .outerjoin(WorkspaceEntry, WorkspaceEntry.entry_id == WorkspaceMutation.entry_id)
        .filter(
            WorkspaceMutation.actor == "cron",
            WorkspaceMutation.state == "completed",
            WorkspaceMutation.cron_run_id.isnot(None),
        )
        .order_by(WorkspaceMutation.created_at.desc())
        .limit(limit)
        .all()
    )
    by_run: dict[str, list[tuple[object, object]]] = {}
    for mutation, entry in reversed(rows):
        by_run.setdefault(str(mutation.cron_run_id), []).append((mutation, entry))

    runs_by_id = {
        str(run.id): run
        for run in (
            db.query(CronJobRun)
            .filter(CronJobRun.id.in_(tuple(by_run)))
            .all()
            if by_run
            else []
        )
    }
    repaired = 0
    for run_id, mutations in by_run.items():
        run = runs_by_id.get(run_id)
        if run is None:
            continue
        try:
            changes = json.loads(run.workspace_changes or "[]")
        except (TypeError, ValueError):
            changes = []
        if not isinstance(changes, list):
            changes = []
        known_ids = {
            item.get("mutation_id")
            for item in changes
            if isinstance(item, dict) and item.get("mutation_id")
        }
        changed = False
        for mutation, entry in mutations:
            if mutation.mutation_id in known_ids:
                continue
            details = _mutation_details(mutation)
            journal = details.get("journal")
            if not isinstance(journal, dict):
                journal = {}
            roots = journal.get("root_projections") or []
            deleted_root = roots[0] if roots else {}
            change = {
                "affected_entry_ids": journal.get("delete_entry_ids") or [],
                "mutation_id": mutation.mutation_id,
                "idempotency_key": mutation.idempotency_key,
                "entry_id": mutation.entry_id,
                "action": mutation.operation,
                "operation": mutation.result_status or mutation.operation,
                "before_revision": mutation.before_revision,
                "revision": (int(deleted_root["revision"]) + 1) if deleted_root else mutation.after_revision,
                "path": _mutation_path(details, journal, entry),
                "name": getattr(entry, "name", None) or deleted_root.get("name"),
                "kind": getattr(entry, "kind", None) or deleted_root.get("kind"),
                "size_bytes": getattr(entry, "size_bytes", None),
                "mime_type": getattr(entry, "mime_type", None),
                "sha256": mutation.after_sha256,
                "status": getattr(entry, "status", None),
            }
            candidate = changes + [change]
            encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            if len(candidate) > 100 or len(encoded.encode("utf-8")) > 64 * 1024:
                break
            changes = candidate
            known_ids.add(mutation.mutation_id)
            changed = True
        if changed:
            run.workspace_changes = json.dumps(
                changes,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            repaired += 1
    change_set_run_ids = [
        str(item[0])
        for item in (
            db.query(WorkspaceChangeSet.cron_run_id)
            .filter(WorkspaceChangeSet.cron_run_id.isnot(None))
            .distinct()
            .limit(limit)
            .all()
        )
        if item[0]
    ]
    if change_set_run_ids:
        for run in db.query(CronJobRun).filter(
            CronJobRun.id.in_(tuple(change_set_run_ids))
        ).all():
            summaries = build_cron_workspace_change_set_summaries(
                db,
                run_id=str(run.id),
            )
            encoded = json.dumps(summaries, ensure_ascii=False, separators=(",", ":"))
            if (run.workspace_change_sets or "[]") != encoded:
                run.workspace_change_sets = encoded
                repaired += 1
    return repaired


def _load_queued_run_ids(limit: int = 200) -> list[str]:
    with SessionLocal() as db:
        return [
            str(row[0])
            for row in (
                db.query(CronJobRun.id)
                .filter(CronJobRun.status == "queued")
                .order_by(CronJobRun.queued_at.asc(), CronJobRun.id.asc())
                .limit(limit)
                .all()
            )
        ]


async def _spawn_queued_runs(worker_id: str) -> None:
    run_ids = await asyncio.to_thread(_load_queued_run_ids)
    for run_id in run_ids:
        _spawn_queued_run(run_id, worker_id)


async def _run_queued(run_id: str, worker_id: str) -> None:
    claim = await asyncio.to_thread(_claim_queued_run, run_id, worker_id)
    if claim is None:
        return

    claim_token = str(claim["claim_token"])
    execution_task = asyncio.create_task(
        run_cron_job(
            str(claim["user_id"]),
            str(claim["job_name"]),
            run_id,
            expected_job_id=(
                int(claim["job_id"])
                if claim.get("job_id") is not None
                else None
            ),
            expected_rule_version=(
                int(claim["rule_version"])
                if claim.get("rule_version") is not None
                else None
            ),
            scheduled_at=claim.get("scheduled_at"),
            trigger_source=str(claim.get("trigger_source") or "scheduled"),
            claim_token=claim_token,
        )
    )
    stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _claim_heartbeat_loop(
            run_id,
            claim_token,
            stop,
            execution_task,
        )
    )
    try:
        await execution_task
    finally:
        stop.set()
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


def _is_durable_queued_run(run_id: str) -> bool:
    with SessionLocal() as db:
        return (
            db.query(CronJobRun.id)
            .filter(CronJobRun.id == run_id, CronJobRun.status == "queued")
            .first()
            is not None
        )


async def _run(
    user_id: str,
    job_name: str,
    worker_id: str,
    run_id: str | None = None,
    scheduled_job_id: int | None = None,
    expected_job_id: int | None = None,
    expected_rule_version: int | None = None,
    scheduled_at: datetime | None = None,
) -> None:
    """执行单个任务，cron 与手动触发共用。

    当 scheduled_job_id 不为空（自动调度路径）时，在真正执行前按 job_id
    做一次 enabled 校验，避免“快照已启用 -> 执行前被禁用/删除”导致空跑。
    """
    actual_run_id = run_id or str(uuid.uuid4())
    source = "manual" if run_id else "scheduled"

    if run_id is not None and await asyncio.to_thread(_is_durable_queued_run, run_id):
        await _run_queued(run_id, worker_id)
        return

    if scheduled_job_id is not None:
        # 自动调度路径：在真正执行前做二次校验，收敛竞态窗口。
        latest = await asyncio.to_thread(
            _load_job_snapshot_if_enabled,
            scheduled_job_id,
            expected_rule_version,
        )
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
        await run_cron_job(
            user_id,
            job_name,
            actual_run_id,
            expected_job_id=expected_job_id or scheduled_job_id,
            expected_rule_version=expected_rule_version,
            scheduled_at=scheduled_at,
            trigger_source=source,
        )
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
    expected_rule_version: int | None = None,
    scheduled_at: datetime | None = None,
) -> None:
    """按 job_id 重新从 DB 拉取 job 并执行。

    防止快照到 _run 之间 job 已被删除或禁用导致空跑。
    """
    snapshot = await asyncio.to_thread(
        _load_job_snapshot_if_enabled,
        job_id,
        expected_rule_version,
    )
    if snapshot is None:
        logger.info(
            "cron skip stale job job_id=%s worker=%s (deleted or disabled)",
            job_id,
            worker_id,
        )
        return

    user_id, name, rule_version = snapshot
    await _run(
        user_id,
        name,
        worker_id,
        scheduled_job_id=job_id,
        expected_rule_version=rule_version,
        scheduled_at=scheduled_at,
    )


def _load_job_snapshot_if_enabled(
    job_id: int,
    expected_rule_version: int | None = None,
) -> tuple[str, str, int] | None:
    """读取启用任务；传入版本时只接受完全相同的调度规则。"""
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
        rule_version = int(job.rule_version or 1)
        if expected_rule_version is not None and rule_version != expected_rule_version:
            return None
        return job.user_id, job.name, rule_version


# ============== 公共 API ==============


async def trigger_manual_run(
    app,
    user_id: str,
    job_name: str,
    run_id: str,
    job_id: int,
    rule_version: int,
) -> asyncio.Task:
    """手动触发指定任务的后台执行。

    路由层应仅通过此函数与 worker 交互，避免直接 import 私有符号。

    Args:
        app: FastAPI app 实例（用于读取 worker_id）
        user_id: 任务所属用户
        job_name: 任务名称
        run_id: 调用方预先生成并已写入 CronJobRun 的执行 id
        job_id: 触发时任务 id，用于防止同名任务删除重建后误执行
        rule_version: 触发时调度规则版本

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

    return _spawn(
        _run(
            user_id,
            job_name,
            worker_id,
            run_id=run_id,
            expected_job_id=job_id,
            expected_rule_version=rule_version,
        )
    )
