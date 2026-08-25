"""Periodic reconciliation for abandoned approved-tool executions."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from src.api.config import get_settings
from src.api.models.database import SessionLocal
from src.api.services.agent_interaction_service import AgentInteractionService
from src.api.services.run_completion_service import RunCompletionService
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
                irrecoverable_run_ids = (
                    AgentInteractionService.load_irrecoverable_continuation_round_ids(
                        db
                    )
                )
                failed_run_ids: list[str] = []
                for run_id in irrecoverable_run_ids:
                    interaction_kind = (
                        AgentInteractionService.lock_irrecoverable_continuation_round_for_failure(
                            db,
                            round_id=run_id,
                        )
                    )
                    if interaction_kind is None:
                        db.rollback()
                        continue
                    await RunCompletionService(db).complete(
                        run_id=run_id,
                        status="failed",
                        final_response=(
                            "[工具审批续跑进程中断；为避免重复副作用，本轮不会自动重试]"
                            if interaction_kind == "tool_approval"
                            else "[交互续跑在持久化启动后中断；本轮不会重新提交已接受的回答]"
                        ),
                    )
                    failed_run_ids.append(run_id)
            if count:
                logger.warning(
                    "reconciled %s expired tool approval execution lease(s) as unknown; "
                    "no tool call was retried",
                    count,
                )
            if failed_run_ids:
                logger.warning(
                    "failed %s irrecoverable started continuation round(s)",
                    len(failed_run_ids),
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
