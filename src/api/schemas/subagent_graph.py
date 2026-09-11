"""Subagent run graph schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SubagentRunStatus = Literal["requested", "running", "completed", "failed", "cancelled"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubagentTaskSnapshot(StrictModel):
    """Parent-facing task identity; excludes child prompts and answer content."""

    edge_id: str
    parent_run_id: str
    child_run_id: str | None = None
    tool_call_id: str | None = None
    agent_name: str | None = None
    description: str | None = None
    agent_type: str | None = None
    model_id: str | None = None
    status: SubagentRunStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_edge(cls, edge: Any, child: Any = None) -> "SubagentTaskSnapshot":
        snapshot = cls(edge_id=edge.id, **{
            name: getattr(edge, name) for name in cls.model_fields if name != "edge_id"
        })
        if child is not None and edge.child_run_id and child.id == edge.child_run_id:
            if child.status in {"completed", "failed", "cancelled", "max_steps_reached"}:
                snapshot.status = "failed" if child.status == "max_steps_reached" else child.status
                if snapshot.completed_at is None:
                    snapshot.completed_at = child.completed_at
        return snapshot


class SubagentGraphNode(StrictModel):
    run_id: str
    session_id: str | None = None
    parent_run_id: str | None = None
    status: str | None = None
    kind: Literal["root", "subagent"] = "subagent"
    created_at: datetime | None = None
    completed_at: datetime | None = None


class SubagentGraphEdge(StrictModel):
    edge_id: str
    root_run_id: str
    parent_run_id: str
    child_run_id: str | None = None
    tool_call_id: str | None = None
    agent_name: str | None = None
    agent_type: str | None = None
    model_id: str | None = None
    description: str | None = None
    prompt: str
    isolation: str | None = None
    worktree_path: str | None = None
    status: SubagentRunStatus
    output: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class SubagentRunGraph(StrictModel):
    session_id: str
    root_run_id: str
    requested_run_id: str
    nodes: list[SubagentGraphNode] = Field(default_factory=list)
    edges: list[SubagentGraphEdge] = Field(default_factory=list)
