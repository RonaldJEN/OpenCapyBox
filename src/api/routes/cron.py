"""Cron 定时任务 API

提供 Cron 任务管理和执行历史查询：
- GET /api/cron/jobs: 获取 CronJob 任务列表
- GET /api/cron/runs: 获取执行历史（分页）
- GET /api/cron/runs/unread-count: 未读运行记录数
- POST /api/cron/runs/mark-read: 批量标记已读
- GET /api/cron/runs/{run_id}: 查询单条执行状态
- GET /api/cron/runs/{run_id}/files: 列出产物文件
- GET /api/cron/runs/{run_id}/files/{path}: 下载产物文件
- POST /api/cron/jobs/{name}/run: 手动触发任务
"""

import asyncio
import json
import logging
import posixpath
import shlex
import uuid
from typing import Set

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session as DBSession

from src.api.models.database import get_db
from src.api.deps import get_current_user
from src.api.services.cron_service import CronService, run_cron_job
from src.api.utils.sandbox_helpers import extract_command_stdout
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
    offset: int = Query(0, ge=0),
    db: DBSession = Depends(get_db),
):
    """获取 Cron 执行历史（分页）"""
    svc = CronService(db)
    runs, total = svc.get_run_history(user_id, job_name=job_name, limit=limit, offset=offset)
    return {"runs": runs, "total": total, "offset": offset, "limit": limit}


# ⚠️ unread-count 和 mark-read 必须在 {run_id} 之前注册，否则被路径参数吞掉

@router.get("/runs/unread-count")
async def get_unread_count(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """获取未读运行记录数（仅统计 success 且未读）。"""
    count = (
        db.query(CronJobRun)
        .filter(
            CronJobRun.user_id == user_id,
            CronJobRun.status == "success",
            CronJobRun.is_read == False,  # noqa: E712
        )
        .count()
    )
    return {"count": count}


@router.post("/runs/mark-read")
async def mark_runs_read(
    run_id: str | None = Query(None, description="仅标记指定 run_id 为已读"),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """标记已读。

    - 未传 run_id: 标记当前用户全部未读记录
    - 传 run_id: 仅标记该条记录（若属于当前用户且未读）
    """
    query = db.query(CronJobRun).filter(
        CronJobRun.user_id == user_id,
        CronJobRun.status == "success",
        CronJobRun.is_read == False,  # noqa: E712
    )
    if run_id:
        query = query.filter(CronJobRun.id == run_id)
    marked = query.update({"is_read": True})
    db.commit()
    return {"marked": marked}


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

    return run.to_dict()


@router.get("/runs/{run_id}/files")
async def list_run_files(
    run_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """列出某次执行的产物文件"""
    run = (
        db.query(CronJobRun)
        .filter(CronJobRun.id == run_id, CronJobRun.user_id == user_id)
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="执行记录不存在")

    # 优先从 DB artifacts 字段读取（通过 to_dict 统一解析）
    parsed = run.to_dict()
    if parsed["artifacts"]:
        return {"files": parsed["artifacts"]}

    # 兼容老数据：实时扫描沙箱目录
    if not run.run_workspace:
        return {"files": []}

    try:
        from src.api.services.sandbox_service import get_sandbox_service
        from src.api.models.user_sandbox import UserSandbox

        user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
        if not user_sandbox:
            return {"files": []}

        sandbox_service = get_sandbox_service()
        sandbox = await sandbox_service.get_or_resume(user_id, user_sandbox.sandbox_id)

        from src.api.services.cron_service import _scan_run_artifacts
        artifacts_json = await _scan_run_artifacts(sandbox, run.run_workspace)
        if artifacts_json:
            files = json.loads(artifacts_json)
            # 顺便回填 DB
            run.artifacts = artifacts_json
            db.commit()
            return {"files": files}
    except Exception as e:
        logger.warning("实时扫描产物失败 (run_id=%s): %s", run_id, e)

    return {"files": []}


@router.get("/runs/{run_id}/files/{file_path:path}")
async def download_run_file(
    run_id: str,
    file_path: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """下载/预览某次执行的产物文件"""
    run = (
        db.query(CronJobRun)
        .filter(CronJobRun.id == run_id, CronJobRun.user_id == user_id)
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="执行记录不存在")

    if not run.run_workspace:
        raise HTTPException(status_code=404, detail="无工作目录信息")

    # 路径安全校验：resolve 后必须在 run_workspace 内
    sandbox_path = posixpath.normpath(posixpath.join(run.run_workspace, file_path))
    if not sandbox_path.startswith(run.run_workspace + "/") and sandbox_path != run.run_workspace:
        raise HTTPException(status_code=403, detail="路径越界")

    # 获取沙箱
    from src.api.models.user_sandbox import UserSandbox
    from src.api.services.sandbox_service import get_sandbox_service

    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    if not user_sandbox:
        raise HTTPException(status_code=404, detail="无沙箱信息")

    sandbox_service = get_sandbox_service()
    try:
        sandbox = await sandbox_service.get_or_resume(user_id, user_sandbox.sandbox_id)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"沙箱连接失败: {e}")

    # 读取文件内容（SDK → base64 命令回退）
    file_bytes = None
    try:
        file_bytes = await sandbox.files.read_bytes(sandbox_path)
    except Exception:
        pass

    if file_bytes is None:
        try:
            cmd_result = await sandbox.commands.run(
                f"base64 -w0 {shlex.quote(sandbox_path)}"
            )
            import base64 as b64_mod
            stdout = extract_command_stdout(cmd_result)
            if stdout.strip():
                file_bytes = b64_mod.b64decode(stdout.strip())
        except Exception:
            pass

    if file_bytes is None:
        raise HTTPException(status_code=404, detail="文件读取失败")

    # 推断 MIME 类型
    import mimetypes
    filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "application/octet-stream"

    return Response(
        content=file_bytes,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
        is_read=False,
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
