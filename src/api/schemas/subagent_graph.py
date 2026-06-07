"""Subagent run graph schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SubagentRunStatus = Literal["requested", "running", "completed", "failed", "cancelled"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
