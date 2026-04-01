"""Cron 定时任务 API

提供 Cron 任务管理和执行历史查询：
- GET /api/cron/jobs: 获取 CronJob 任务列表
- GET /api/cron/heartbeat: 读取 HEARTBEAT.md（纯轮询清单）
- GET /api/cron/runs: 获取执行历史
- POST /api/cron/jobs/{name}/run: 手动触发任务
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DBSession

from src.api.models.database import get_db
from src.api.deps import get_current_user
from src.api.services.cron_service import CronService, run_cron_job

logger = logging.getLogger(__name__)
router = APIRouter()


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


@router.get("/heartbeat")
async def get_heartbeat(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """获取 HEARTBEAT.md 内容（纯轮询清单，不含 Cron 任务）"""
    svc = CronService(db)
    content = svc.get_heartbeat_content(user_id)
    # 同时返回 CronJob 任务列表以保持向下兼容
    tasks = svc.get_jobs(user_id)
    return {
        "content": content,
        "tasks": [t.to_dict() for t in tasks],
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


@router.post("/jobs/{job_name}/run")
async def trigger_job(
    job_name: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """手动触发指定的 Cron 任务"""
    from src.api.models.cron_job import CronJob

    job = (
        db.query(CronJob)
        .filter(CronJob.user_id == user_id, CronJob.name == job_name)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 '{job_name}' 不存在")

    result = await run_cron_job(user_id, job_name)
    return {
        "job_name": job_name,
        "status": "success" if result else "failed",
        "output": result,
    }
