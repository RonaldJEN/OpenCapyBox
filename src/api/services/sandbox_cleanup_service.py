"""Lease-fenced cleanup of platform-owned Session/Cron paths."""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.api.models.database import SessionLocal
from src.api.models.sandbox_cleanup import SandboxCleanupJob
from src.api.services.sandbox_service import get_sandbox_service
from src.api.utils.sandbox_helpers import extract_command_stdout
from src.api.utils.timezone import now_naive


_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
logger = logging.getLogger(__name__)


def _validate_target(owner_kind: str, relative_path: str) -> str:
    normalized = posixpath.normpath(relative_path.replace("\\", "/"))
    parts = normalized.split("/")
    valid = (
        owner_kind == "session"
        and len(parts) == 2
        and parts[0] == "sessions"
        and _UUID.fullmatch(parts[1])
    ) or (
        owner_kind == "attachment_capture"
        and len(parts) == 5
        and parts[0] == "sessions"
        and _UUID.fullmatch(parts[1])
        and parts[2] == ".workspace-snapshots"
        and _UUID.fullmatch(parts[3])
        and re.fullmatch(r"[0-9a-f]{32}", parts[4])
    )
    if not valid:
        raise ValueError("Sandbox cleanup target is outside a platform-owned scope")
    return normalized


def enqueue_cleanup(
    db: DBSession,
    *,
    user_id: str,
    owner_kind: str,
    owner_id: str,
    sandbox_id: str,
    mount_path: str,
    relative_path: str,
    profile_id: str | None = None,
    profile_version: int | None = None,
) -> str:
    safe_path = _validate_target(owner_kind, relative_path)
    existing = db.query(SandboxCleanupJob.cleanup_id).filter(
        SandboxCleanupJob.owner_kind == owner_kind,
        SandboxCleanupJob.owner_id == owner_id,
        SandboxCleanupJob.relative_path == safe_path,
    ).scalar()
    if existing:
        return str(existing)
    cleanup_id = str(uuid.uuid4())
    db.add(SandboxCleanupJob(
        cleanup_id=cleanup_id,
        user_id=user_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        sandbox_id=sandbox_id,
        profile_id=profile_id,
        profile_version=profile_version,
        mount_path=posixpath.normpath(mount_path),
        relative_path=safe_path,
        state="queued",
        next_attempt_at=now_naive(),
    ))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = db.query(SandboxCleanupJob.cleanup_id).filter(
            SandboxCleanupJob.owner_kind == owner_kind,
            SandboxCleanupJob.owner_id == owner_id,
            SandboxCleanupJob.relative_path == safe_path,
        ).scalar()
        if not winner:
            raise
        return str(winner)
    return cleanup_id


@dataclass(frozen=True)
class _ClaimedCleanup:
    cleanup_id: str
    user_id: str
    sandbox_id: str
    mount_path: str
    relative_path: str
    owner_token: str
    generation: int


def _claim(cleanup_id: str) -> _ClaimedCleanup | None:
    with SessionLocal() as db:
        row = db.query(SandboxCleanupJob).filter(
            SandboxCleanupJob.cleanup_id == cleanup_id,
        ).with_for_update().one_or_none()
        if row is None or row.state == "completed":
            db.rollback()
            return None
        now = now_naive()
        if row.state == "running" and row.lease_expires_at and row.lease_expires_at > now:
            db.rollback()
            return None
        token = uuid.uuid4().hex
        row.state = "running"
        row.owner_token = token
        row.generation = int(row.generation or 0) + 1
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.lease_expires_at = now + timedelta(minutes=5)
        row.next_attempt_at = None
        db.commit()
        return _ClaimedCleanup(
            cleanup_id=str(row.cleanup_id),
            user_id=str(row.user_id),
            sandbox_id=str(row.sandbox_id),
            mount_path=str(row.mount_path),
            relative_path=str(row.relative_path),
            owner_token=token,
            generation=int(row.generation),
        )


def _finish(claim: _ClaimedCleanup, *, error: str | None) -> None:
    with SessionLocal() as db:
        row = db.query(SandboxCleanupJob).filter(
            SandboxCleanupJob.cleanup_id == claim.cleanup_id,
            SandboxCleanupJob.owner_token == claim.owner_token,
            SandboxCleanupJob.generation == claim.generation,
            SandboxCleanupJob.state == "running",
        ).with_for_update().one_or_none()
        if row is None:
            db.rollback()
            return
        if error is None:
            row.state = "completed"
            row.completed_at = now_naive()
            row.next_attempt_at = None
            row.error_message = None
        else:
            row.state = "retry"
            row.next_attempt_at = now_naive() + timedelta(seconds=30)
            row.error_message = error[:2000]
        row.lease_expires_at = None
        db.commit()


async def run_cleanup_job(cleanup_id: str) -> bool:
    claim = await asyncio.to_thread(_claim, cleanup_id)
    if claim is None:
        return False
    error: str | None = None
    try:
        sandbox = await get_sandbox_service().get_existing(
            claim.user_id,
            claim.sandbox_id,
        )
        target_parent, target_name = posixpath.split(claim.relative_path)
        execution = await sandbox.commands.run(
            "python3 - <<'PY'\n"
            "import json, os, stat\n"
            f"root = {claim.mount_path!r}\n"
            f"parent = {target_parent!r}\n"
            f"name = {target_name!r}\n"
            "def open_dir(root, relative):\n"
            "    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)\n"
            "    try:\n"
            "        for part in [p for p in relative.split('/') if p]:\n"
            "            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)\n"
            "            os.close(fd); fd = nxt\n"
            "        return fd\n"
            "    except Exception:\n"
            "        os.close(fd); raise\n"
            "def remove_tree(parent_fd, child):\n"
            "    st = os.stat(child, dir_fd=parent_fd, follow_symlinks=False)\n"
            "    if stat.S_ISDIR(st.st_mode):\n"
            "        fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)\n"
            "        try:\n"
            "            for item in os.listdir(fd): remove_tree(fd, item)\n"
            "        finally: os.close(fd)\n"
            "        os.rmdir(child, dir_fd=parent_fd)\n"
            "    else: os.unlink(child, dir_fd=parent_fd)\n"
            "try:\n"
            "    parent_fd = open_dir(root, parent)\n"
            "    try:\n"
            "        try: remove_tree(parent_fd, name)\n"
            "        except FileNotFoundError: pass\n"
            "        try: os.stat(name, dir_fd=parent_fd, follow_symlinks=False); raise RuntimeError('target remains')\n"
            "        except FileNotFoundError: pass\n"
            "        os.fsync(parent_fd)\n"
            "    finally: os.close(parent_fd)\n"
            "    print(json.dumps({'ok': True}))\n"
            "except FileNotFoundError: print(json.dumps({'ok': True}))\n"
            "except Exception as exc: print(json.dumps({'ok': False, 'error': str(exc)}))\n"
            "PY"
        )
        payload = json.loads(extract_command_stdout(execution).strip())
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "cleanup failed"))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    await asyncio.to_thread(_finish, claim, error=error)
    return error is None


def _due_ids(limit: int = 20) -> list[str]:
    with SessionLocal() as db:
        now = now_naive()
        rows = db.query(SandboxCleanupJob.cleanup_id).filter(
            or_(
                and_(
                    SandboxCleanupJob.state.in_(("queued", "retry")),
                    or_(
                        SandboxCleanupJob.next_attempt_at.is_(None),
                        SandboxCleanupJob.next_attempt_at <= now,
                    ),
                ),
                and_(
                    SandboxCleanupJob.state == "running",
                    SandboxCleanupJob.lease_expires_at <= now,
                ),
            ),
        ).order_by(SandboxCleanupJob.created_at.asc()).limit(limit).all()
        db.rollback()
        return [str(row[0]) for row in rows]


async def _loop() -> None:
    while True:
        try:
            for cleanup_id in await asyncio.to_thread(_due_ids):
                await run_cleanup_job(cleanup_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Sandbox cleanup iteration failed", exc_info=True)
        await asyncio.sleep(30)


async def start_sandbox_cleanup_worker(app) -> None:
    task = getattr(app.state, "sandbox_cleanup_task", None)
    if task is None or task.done():
        app.state.sandbox_cleanup_task = asyncio.create_task(
            _loop(), name="sandbox-cleanup"
        )


async def stop_sandbox_cleanup_worker(app) -> None:
    task = getattr(app.state, "sandbox_cleanup_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    app.state.sandbox_cleanup_task = None


__all__ = [
    "enqueue_cleanup",
    "run_cleanup_job",
    "start_sandbox_cleanup_worker",
    "stop_sandbox_cleanup_worker",
]
