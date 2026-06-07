"""Operational repair for terminal AG-UI events."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from src.agent.schema.agui_events import EventType
from src.api.models.agui_event import AGUIEventLog
from src.api.models.round import Round
from src.api.services.run_completion_service import RunCompletionService
from src.api.utils.timezone import now_naive


@dataclass
class TerminalRepairReport:
    scanned: int = 0
    missing_terminal: int = 0
    repaired: int = 0
    skipped: int = 0
    repaired_run_ids: list[str] = field(default_factory=list)
    skipped_run_ids: list[str] = field(default_factory=list)


class TerminalRepairService:
    """Repair terminal rounds that are missing durable stream terminal events."""

    terminal_event_types = (EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value)

    def __init__(self, db: DBSession):
        self.db = db

    def repair_terminal_runs(self, *, since_hours: int, dry_run: bool = False) -> TerminalRepairReport:
        cutoff = now_naive() - timedelta(hours=max(int(since_hours), 1))
        terminal_run_ids = select(AGUIEventLog.run_id).where(
            AGUIEventLog.event_type.in_(self.terminal_event_types)
        )
        candidates = (
            self.db.query(Round)
            .filter(
                Round.status.in_(Round.SUBSCRIBE_TERMINAL_STATUSES),
                func.coalesce(Round.completed_at, Round.created_at) >= cutoff,
                ~Round.id.in_(terminal_run_ids),
            )
            .order_by(Round.created_at)
            .all()
        )
        report = TerminalRepairReport(
            scanned=len(candidates),
            missing_terminal=len(candidates),
        )
        if dry_run:
            report.skipped = len(candidates)
            report.skipped_run_ids = [round_obj.id for round_obj in candidates]
            return report

        completion = RunCompletionService(self.db)
        for round_obj in candidates:
            stored = completion.ensure_terminal_sync(round_obj.id)
            if stored is None:
                report.skipped += 1
                report.skipped_run_ids.append(round_obj.id)
                continue
            report.repaired += 1
            report.repaired_run_ids.append(round_obj.id)
        return report
