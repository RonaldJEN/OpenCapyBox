"""Periodic content-object retention and crash-recoverable history GC."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import or_

from src.api.config import get_settings
from src.api.models.database import SessionLocal
from src.api.models.workspace import (
    UserWorkspace,
    WorkspaceChangeSet,
    WorkspaceContentObject,
    WorkspaceFileVersion,
    WorkspaceMutation,
)
from src.api.services.workspace_service import WorkspaceService
from src.api.utils.timezone import now_naive


logger = logging.getLogger(__name__)


def _claim_due_workspace_ids(*, limit: int = 20) -> list[str]:
    settings = get_settings()
    claimed_at = now_naive()
    cutoff = claimed_at - timedelta(seconds=int(settings.workspace_history_gc_interval_seconds))
    claimed: list[str] = []
    with SessionLocal() as db:
        candidates = (
            db.query(UserWorkspace.user_id)
            .filter(
                or_(
                    UserWorkspace.history_used_bytes > 0,
                    db.query(WorkspaceContentObject.blob_id).filter(
                        WorkspaceContentObject.user_id == UserWorkspace.user_id,
                        WorkspaceContentObject.state.in_(("materialized", "pruning")),
                    ).exists(),
                    db.query(WorkspaceChangeSet.change_set_id).filter(
                        WorkspaceChangeSet.user_id == UserWorkspace.user_id,
                        or_(
                            WorkspaceChangeSet.status.in_(("preparing", "proposed", "conflict", "needs_review", "applying")),
                            WorkspaceChangeSet.proposal_blob_id.is_(None),
                            WorkspaceChangeSet.details_json.like('%"proposal_path":".opencapybox/tmp/%'),
                            WorkspaceChangeSet.details_json.like('%"proposal_path": ".opencapybox/tmp/%'),
                            WorkspaceChangeSet.details_json.like('%"proposal_temp_path":".opencapybox/tmp/%'),
                            WorkspaceChangeSet.details_json.like('%"proposal_temp_path": ".opencapybox/tmp/%'),
                        ),
                    ).exists(),
                    db.query(WorkspaceFileVersion.version_id).filter(
                        WorkspaceFileVersion.user_id == UserWorkspace.user_id,
                        WorkspaceFileVersion.state == "materialized",
                        WorkspaceFileVersion.blob_id.is_(None),
                        WorkspaceFileVersion.content_path.isnot(None),
                    ).exists(),
                ),
                or_(
                    UserWorkspace.last_history_gc_at.is_(None),
                    UserWorkspace.last_history_gc_at <= cutoff,
                ),
            )
            .order_by(UserWorkspace.last_history_gc_at.asc().nullsfirst(), UserWorkspace.user_id.asc())
            .limit(max(int(limit), 1))
            .all()
        )
        for (user_id,) in candidates:
            updated = (
                db.query(UserWorkspace)
                .filter(
                    UserWorkspace.user_id == user_id,
                    or_(
                        UserWorkspace.last_history_gc_at.is_(None),
                        UserWorkspace.last_history_gc_at <= cutoff,
                    ),
                )
                .update(
                    {UserWorkspace.last_history_gc_at: claimed_at},
                    synchronize_session=False,
                )
            )
            if updated == 1:
                claimed.append(str(user_id))
        db.commit()
    return claimed


def _prepared_workspace_ids(*, limit: int = 100) -> list[str]:
    with SessionLocal() as db:
        return [
            str(item[0])
            for item in (
                db.query(WorkspaceMutation.user_id)
                .filter(
                    WorkspaceMutation.state == "prepared",
                    WorkspaceMutation.lease_expires_at <= now_naive(),
                )
                .distinct()
                .limit(max(1, int(limit)))
                .all()
            )
            if item[0]
        ]


async def _workspace_maintenance_loop(interval_seconds: float) -> None:
    while True:
        try:
            due_user_ids, prepared_user_ids = await asyncio.gather(
                asyncio.to_thread(_claim_due_workspace_ids),
                asyncio.to_thread(_prepared_workspace_ids),
            )
            user_ids = sorted(set(due_user_ids) | set(prepared_user_ids))
            for user_id in user_ids:
                with SessionLocal() as db:
                    try:
                        service = WorkspaceService(db)
                        await service.reconcile_prepared_mutations(user_id)
                        migrated = await service.migrate_legacy_versions(user_id)
                        if migrated:
                            logger.info(
                                "Workspace legacy versions migrated user=%s count=%s",
                                user_id,
                                migrated,
                            )
                        backfilled = service.backfill_legacy_round_file_references(user_id)
                        if backfilled:
                            logger.info(
                                "Workspace legacy assistant references protected user=%s count=%s",
                                user_id,
                                backfilled,
                            )
                        converged = await service.reconcile_change_sets(user_id)
                        if converged:
                            logger.info(
                                "Workspace change sets reconciled user=%s count=%s",
                                user_id,
                                converged,
                            )
                        result = await service.run_history_gc(user_id)
                        if result.versions_pruned or result.objects_pruned:
                            logger.info(
                                "Workspace history GC completed user=%s versions=%s objects=%s bytes=%s",
                                user_id,
                                result.versions_pruned,
                                result.objects_pruned,
                                result.bytes_reclaimed,
                            )
                    except Exception:
                        db.rollback()
                        logger.warning("Workspace history GC failed user=%s", user_id, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Workspace maintenance iteration failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def start_workspace_maintenance(app) -> None:
    existing = getattr(app.state, "workspace_maintenance_task", None)
    if existing is not None and not existing.done():
        return
    interval = float(get_settings().workspace_history_gc_interval_seconds)
    app.state.workspace_maintenance_task = asyncio.create_task(
        _workspace_maintenance_loop(interval),
        name="workspace-history-maintenance",
    )


async def stop_workspace_maintenance(app) -> None:
    task = getattr(app.state, "workspace_maintenance_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    app.state.workspace_maintenance_task = None


__all__ = [
    "start_workspace_maintenance",
    "stop_workspace_maintenance",
]
