"""Periodic reconciliation for abandoned approved-tool executions."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from src.api.config import get_settings
from src.api.models.database import SessionLocal
from src.api.services.tool_permission_service import (
    reconcile_expired_approval_leases,
)


logger = logging.getLogger(__name__)


async def _approval_reconciler_loop(interval_seconds: float) -> None:
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            with SessionLocal() as db:
                count = reconcile_expired_approval_leases(db)
            if count:
                logger.warning(
                    "reconciled %s expired tool approval execution lease(s) as unknown; "
                    "no tool call was retried",
                    count,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("tool approval lease reconciliation failed")


async def start_approval_reconciler(app) -> None:
    existing = getattr(app.state, "approval_reconciler_task", None)
    if existing is not None and not existing.done():
        return
    interval = float(get_settings().tool_approval_reconcile_interval_seconds)
    app.state.approval_reconciler_task = asyncio.create_task(
        _approval_reconciler_loop(interval),
        name="tool-approval-lease-reconciler",
    )


async def stop_approval_reconciler(app) -> None:
    task = getattr(app.state, "approval_reconciler_task", None)
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    app.state.approval_reconciler_task = None
