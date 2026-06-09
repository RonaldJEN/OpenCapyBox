"""Cron 定时任务 API

提供 Cron 任务管理和执行历史查询：
- GET /api/cron/jobs: 获取 CronJob 任务列表
- POST /api/cron/jobs: 新建任务（schedule 优先于 cron_expr）
- PUT /api/cron/jobs/{name}: 更新任务
- DELETE /api/cron/jobs/{name}: 删除任务（保留历史 run）
- POST /api/cron/jobs/preview: 预览未来 5 次执行时间
- GET /api/cron/runs: 获取执行历史（分页）
- GET /api/cron/runs/unread-count: 未读运行记录数
- POST /api/cron/runs/mark-read: 批量标记已读
- GET /api/cron/runs/{run_id}: 查询单条执行状态
- GET /api/cron/runs/{run_id}/files: 列出产物文件
- GET /api/cron/runs/{run_id}/files/{path}: 下载产物文件
- POST /api/cron/jobs/{name}/run: 手动触发任务
"""

import json
import logging
import posixpath
import shlex
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.api.models.database import get_db
from src.api.deps import get_current_user, verify_access_token
from src.api.services.cron_schedule import ScheduleError, next_fire_at
from src.api.services.cron_service import (
    CronJobBusyError,
    CronJobNotFoundError,
    CronJobValidationError,
    CronService,
)
from src.api.services.cron_worker import trigger_manual_run
from src.api.utils.sandbox_helpers import extract_command_stdout
from src.api.models.user_memory import CronJobRun
from src.api.utils.timezone import now_naive

_bearer_optional = HTTPBearer(auto_error=False)

logger = logging.getLogger(__name__)
router = APIRouter()


# ────────────────────────── Pydantic schemas ──────────────────────────


class CronJobCreate(BaseModel):
    """新建 Cron 任务请求体。schedule 与 cron_expr 二选一（schedule 优先）。"""

    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    content: str = Field("", max_length=8000)
    schedule: dict | None = None
    cron_expr: str | None = None
    enabled: bool = True


class CronJobUpdate(BaseModel):
    """更新 Cron 任务请求体。所有字段可选，省略则不变；name 不可改。"""

    description: str | None = Field(None, max_length=500)
    content: str | None = Field(None, max_length=8000)
    schedule: dict | None = None
    cron_expr: str | None = None
    enabled: bool | None = None


class SchedulePreviewRequest(BaseModel):
    """表单底部"未来 N 次执行预览"请求体。schedule / cron_expr 二选一。"""

    schedule: dict | None = None
    cron_expr: str | None = None
    n: int = Field(5, ge=1, le=20)


def _job_response(job: object) -> dict:
    raw_schedule = getattr(job, "schedule", None)
    if isinstance(raw_schedule, str):
        schedule = json.loads(raw_schedule) if raw_schedule else None
    else:
        schedule = raw_schedule
    return {
        "id": getattr(job, "id", None),
        "name": getattr(job, "name"),
        "cron_expr": getattr(job, "cron_expr"),
        "schedule": schedule,
        "description": getattr(job, "description", "") or "",
        "content": getattr(job, "content", "") or "",
        "enabled": bool(getattr(job, "enabled", False)),
    }


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


@router.post("/jobs", status_code=201)
async def create_cron_job(
    payload: CronJobCreate,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """新建 Cron 任务。schedule 与 cron_expr 二选一（schedule 优先）。"""
    svc = CronService(db)
    try:
        job = svc.create_job(
            user_id,
            name=payload.name,
            description=payload.description,
            content=payload.content,
            schedule=payload.schedule,
            cron_expr=payload.cron_expr,
            enabled=payload.enabled,
        )
    except CronJobValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except CronJobBusyError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"job": _job_response(job)}


@router.put("/jobs/{name}")
async def update_cron_job(
    name: str,
    payload: CronJobUpdate,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """更新 Cron 任务。任意字段可选；schedule/cron_expr 二选一。"""
    svc = CronService(db)
    try:
        job = svc.update_job(
            user_id,
            name,
            description=payload.description,
            content=payload.content,
            schedule=payload.schedule,
            cron_expr=payload.cron_expr,
            enabled=payload.enabled,
        )
    except CronJobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except CronJobValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except CronJobBusyError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"job": _job_response(job)}


@router.delete("/jobs/{name}", status_code=204)
async def delete_cron_job(
    name: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """删除 Cron 任务。保留 CronJobRun 历史；CronFire 去重记录按 job_id 清理。"""
    svc = CronService(db)
    try:
        svc.delete_job(user_id, name)
    except CronJobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except CronJobBusyError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return Response(status_code=204)


@router.post("/jobs/preview")
async def preview_schedule(
    payload: SchedulePreviewRequest,
    user_id: str = Depends(get_current_user),  # 仅鉴权，不读 user 数据
):
    """计算 schedule / cron_expr 接下来 N 次触发时间（本地时区 ISO 字符串）。"""
    from src.api.services.cron_schedule import schedule_to_cron

    try:
        has_schedule = payload.schedule is not None
        has_cron_expr = bool(payload.cron_expr and payload.cron_expr.strip())
        if has_schedule and has_cron_expr:
            raise HTTPException(status_code=400, detail="schedule 与 cron_expr 不能同时提供")
        if has_schedule:
            expr = schedule_to_cron(payload.schedule)
        elif has_cron_expr:
            expr = payload.cron_expr.strip()
        else:
            raise HTTPException(status_code=400, detail="必须提供 schedule 或 cron_expr")
        fires = next_fire_at(expr, n=payload.n)
    except ScheduleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "cron_expr": expr,
        "next_fires": [t.isoformat() for t in fires],
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
    """获取未读运行记录数"""
    count = (
        db.query(CronJobRun)
        .filter(CronJobRun.user_id == user_id, CronJobRun.is_read == False)  # noqa: E712
        .count()
    )
    return {"count": count}


@router.post("/runs/mark-read")
async def mark_runs_read(
    run_id: str | None = Query(None, description="指定后仅标记该条；省略则批量标记当前用户所有未读记录为已读"),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """标记运行记录为已读。

    - 不传 run_id：当前用户所有未读记录全部标记。
    - 传 run_id：仅标记归属于当前用户、且未读的该条记录。
    """
    query = db.query(CronJobRun).filter(
        CronJobRun.user_id == user_id,
        CronJobRun.is_read == False,  # noqa: E712
    )
    if run_id is not None:
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

    artifacts = None
    if run.artifacts:
        try:
            artifacts = json.loads(run.artifacts)
        except (ValueError, TypeError):
            artifacts = None

    return {
        "id": run.id,
        "job_name": run.job_name,
        "cron_expr": run.cron_expr,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "status": run.status,
        "output": run.output,
        "is_read": bool(getattr(run, 'is_read', True)),
        "artifacts": artifacts,
        "run_workspace": getattr(run, 'run_workspace', None),
    }


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

    # 优先从 DB artifacts 字段读取
    if run.artifacts:
        try:
            files = json.loads(run.artifacts)
            return {"files": files}
        except (ValueError, TypeError):
            pass

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
        latest_sandbox_id = sandbox_service.get_sandbox_id(user_id)
        if latest_sandbox_id and latest_sandbox_id != user_sandbox.sandbox_id:
            user_sandbox.sandbox_id = latest_sandbox_id
            user_sandbox.status = "active"
            db.commit()

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
    token: str | None = Query(None, description="Bearer token (用于浏览器直接下载)"),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
    db: DBSession = Depends(get_db),
):
    """下载/预览某次执行的产物文件

    支持两种鉴权方式（浏览器 <a> 链接无法带 header）：
    1. Authorization: Bearer <token>（标准方式）
    2. ?token=<token>（URL 直接下载）
    """
    # 鉴权：header 优先，query param 兜底
    raw_token = (credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else None) or token
    if not raw_token:
        raise HTTPException(status_code=401, detail="未提供访问令牌")
    user_id = verify_access_token(raw_token)
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
        latest_sandbox_id = sandbox_service.get_sandbox_id(user_id)
        if latest_sandbox_id and latest_sandbox_id != user_sandbox.sandbox_id:
            user_sandbox.sandbox_id = latest_sandbox_id
            user_sandbox.status = "active"
            db.commit()
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
    request: Request,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """手动触发指定的 Cron 任务。

    所有执行必须走 cron_worker.trigger_manual_run，以共享 worker 内部的
    per-user 串行锁。注意该串行锁是进程内语义：单 worker 严格串行，
    多 worker 部署下同一用户任务仍可能并行（与 cron spec 一致）。
    """
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

    try:
        await trigger_manual_run(request.app, user_id, job_name, run_id)
    except Exception as exc:
        # 任何异常（HTTPException / RuntimeError / 其他）都必须把预创建的 running
        # 记录立刻收拢为 failed，否则会一直挂到 startup 1 小时清理 → 前端永久转圈。
        rec = db.query(CronJobRun).filter(CronJobRun.id == run_id).first()
        if rec and rec.status == "running":
            if isinstance(exc, HTTPException):
                reason = str(exc.detail) or "cron worker 未启动，无法手动触发任务"
            else:
                reason = f"手动触发失败: {exc.__class__.__name__}: {exc}"
            rec.status = "failed"
            rec.output = reason
            rec.completed_at = now_naive()
            db.commit()
        raise

    logger.info("Cron 手动触发已提交后台执行 (user=%s, job=%s, run_id=%s)", user_id, job_name, run_id)
    return {
        "job_name": job_name,
        "run_id": run_id,
        "status": "accepted",
        "message": "后台任务已执行",
    }
