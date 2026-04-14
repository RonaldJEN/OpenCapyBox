"""Cron 定时任务 API

提供 Cron 任务管理和执行历史查询：
- GET /api/cron/jobs: 获取 CronJob 任务列表
- GET /api/cron/runs: 获取执行历史
- POST /api/cron/jobs/{name}/run: 手动触发任务
"""

import asyncio
import logging
import uuid
from typing import Set

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DBSession

from src.api.models.database import get_db
from src.api.deps import get_current_user
from src.api.services.cron_service import CronService, run_cron_job
from src.api.models.user_memory import CronJobRun

logger = logging.getLogger(__name__)
router = APIRouter()

# 防止 asyncio.create_task 返回值被 GC 回收导致后台任务静默丢失
_background_tasks: Set[asyncio.Task] = set()


@router.get("/jobs")
async def get_cron_jobs(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """获取 CronJob 任务列表（DB 驱动）"""
    svc = CronService(db)
    tasks = svc.get_jobs(user_id)
    return {
        "jobs": [t.to_dict() for t in tasks],
    }


@router.get("/runs")
async def get_run_history(
    user_id: str = Depends(get_current_user),
    job_name: str = Query(None, description="Filter by job name"),
    limit: int = Query(20, ge=1, le=100),
    db: DBSession = Depends(get_db),
):
    """获取 Cron 执行历史"""
    svc = CronService(db)
    runs = svc.get_run_history(user_id, job_name=job_name, limit=limit)
    return {"runs": runs}


@router.get("/runs/{run_id}")
async def get_run_status(
    run_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """查询单条执行记录状态（用于前端轮询）"""
    run = (
        db.query(CronJobRun)
        .filter(CronJobRun.id == run_id, CronJobRun.user_id == user_id)
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    return {
        "id": run.id,
        "job_name": run.job_name,
        "cron_expr": run.cron_expr,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "status": run.status,
        "output": run.output,
    }


@router.post("/jobs/{job_name}/run")
async def trigger_job(
    job_name: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """手动触发指定的 Cron 任务（后台执行，立即返回 run_id 供轮询）"""
    from src.api.models.cron_job import CronJob

    job = (
        db.query(CronJob)
        .filter(CronJob.user_id == user_id, CronJob.name == job_name)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 '{job_name}' 不存在")

    # 预创建执行记录，确保前端可立即开始轮询
    run_id = str(uuid.uuid4())
    run_record = CronJobRun(
        id=run_id,
        user_id=user_id,
        job_name=job_name,
        cron_expr=job.cron_expr,
        status="running",
    )
    db.add(run_record)
    db.commit()

    async def _run_in_background():
        try:
            await run_cron_job(user_id, job_name, run_id=run_id)
        except Exception:
            logger.exception("后台执行 Cron 任务失败 (user=%s, job=%s)", user_id, job_name)

    task = asyncio.create_task(_run_in_background())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    logger.info("Cron 手动触发已提交后台执行 (user=%s, job=%s, run_id=%s)", user_id, job_name, run_id)
    return {
        "job_name": job_name,
        "run_id": run_id,
        "status": "accepted",
        "message": "后台任务已执行",
    }
