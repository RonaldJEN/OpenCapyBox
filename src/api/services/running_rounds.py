"""Helpers for selecting user-visible running rounds."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from src.api.models.round import Round
from src.api.models.subagent_run import SubagentRun


def subagent_child_run_ids_select():
    """Return child round ids that should not drive user-facing run state."""
    return select(SubagentRun.child_run_id).where(SubagentRun.child_run_id.isnot(None))


def main_running_round_join_condition(session_id_column):
    """SQLAlchemy ON condition for the main running round of a session."""
    return (
        (Round.session_id == session_id_column)
        & (Round.status == "running")
        & (~Round.id.in_(subagent_child_run_ids_select()))
    )


def get_main_running_round(db: DBSession, *, session_id: str) -> Round | None:
    """Fetch the user-visible running round, excluding internal subagent children."""
    return (
        db.query(Round)
        .filter(
            Round.session_id == session_id,
            Round.status == "running",
            ~Round.id.in_(subagent_child_run_ids_select()),
        )
        .first()
    )
