"""Persistent subagent run graph service."""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from src.api.model_registry import get_model_registry
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.subagent_run import SubagentRun
from src.api.schemas.subagent_graph import SubagentGraphEdge, SubagentGraphNode, SubagentRunGraph
from src.api.services.auth_service import get_enabled_user
from src.api.utils.timezone import now_naive


class SubagentGraphService:
    """Create and query subagent run graph edges."""

    def create_edge(
        self,
        db: DBSession,
        *,
        user_id: str,
        session_id: str,
        parent_run_id: str,
        prompt: str,
        child_run_id: str | None = None,
        tool_call_id: str | None = None,
        agent_name: str | None = None,
        agent_type: str | None = None,
        model_id: str | None = None,
        description: str | None = None,
        isolation: str | None = None,
        worktree_path: str | None = None,
        status: str | None = None,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SubagentRun:
        self._validate_user_session(db, user_id=user_id, session_id=session_id)
        parent = self._get_round(db, session_id=session_id, run_id=parent_run_id)
        child = self._get_round(db, session_id=session_id, run_id=child_run_id) if child_run_id else None
        root_run_id = self._root_run_id(db, parent_run_id)
        final_status = status or (self._status_from_child_round(child) if child is not None else SubagentRun.REQUESTED)
        self._validate_status(final_status)
        resolved_model_id = self._resolve_model_id(model_id)

        edge = SubagentRun(
            user_id=user_id,
            session_id=session_id,
            root_run_id=root_run_id or parent.id,
            parent_run_id=parent.id,
            child_run_id=child.id if child else None,
            tool_call_id=tool_call_id,
            agent_name=agent_name,
            agent_type=agent_type,
            model_id=resolved_model_id,
            description=description,
            prompt=prompt,
            isolation=isolation,
            worktree_path=worktree_path,
            status=final_status,
            output=output,
            error=error,
            metadata_json=self._dump_metadata(metadata),
            started_at=now_naive() if final_status == SubagentRun.RUNNING else None,
            completed_at=now_naive() if final_status in self._terminal_statuses() else None,
        )
        db.add(edge)
        db.commit()
        db.refresh(edge)
        return edge

    def attach_child_run(
        self,
        db: DBSession,
        *,
        edge_id: str,
        user_id: str,
        session_id: str,
        child_run_id: str,
        status: str | None = None,
    ) -> SubagentRun:
        edge = self._get_edge(db, edge_id=edge_id, user_id=user_id, session_id=session_id)
        child = self._get_round(db, session_id=session_id, run_id=child_run_id)
        final_status = status or self._status_from_child_round(child)
        self._validate_status(final_status)
        edge.child_run_id = child.id
        edge.status = final_status
        edge.updated_at = now_naive()
        if final_status == SubagentRun.RUNNING and edge.started_at is None:
            edge.started_at = now_naive()
        if final_status in self._terminal_statuses():
            edge.completed_at = now_naive()
        db.commit()
        db.refresh(edge)
        return edge

    def mark_status(
        self,
        db: DBSession,
        *,
        edge_id: str,
        user_id: str,
        session_id: str,
        status: str,
        output: str | None = None,
        error: str | None = None,
    ) -> SubagentRun:
        self._validate_status(status)
        edge = self._get_edge(db, edge_id=edge_id, user_id=user_id, session_id=session_id)
        edge.status = status
        edge.updated_at = now_naive()
        if status == SubagentRun.RUNNING and edge.started_at is None:
            edge.started_at = now_naive()
        if status in self._terminal_statuses():
            edge.completed_at = now_naive()
        if output is not None:
            edge.output = output
        if error is not None:
            edge.error = error
        db.commit()
        db.refresh(edge)
        return edge

    def get_graph(
        self,
        db: DBSession,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> SubagentRunGraph:
        self._validate_user_session(db, user_id=user_id, session_id=session_id)
        requested = self._get_round(db, session_id=session_id, run_id=run_id)
        root_run_id = self._root_run_id(db, requested.id) or requested.id
        root = self._get_round(db, session_id=session_id, run_id=root_run_id)

        edges = (
            db.query(SubagentRun)
            .filter(
                SubagentRun.user_id == user_id,
                SubagentRun.session_id == session_id,
                SubagentRun.root_run_id == root_run_id,
            )
            .order_by(SubagentRun.created_at, SubagentRun.id)
            .all()
        )
        run_ids = {root.id, requested.id}
        for edge in edges:
            run_ids.add(edge.parent_run_id)
            if edge.child_run_id:
                run_ids.add(edge.child_run_id)
        rounds = {
            round_obj.id: round_obj
            for round_obj in db.query(Round).filter(Round.id.in_(run_ids)).all()
        }
        nodes = [
            self._node_from_round(round_obj, kind="root" if round_obj.id == root.id else "subagent")
            for round_obj in sorted(rounds.values(), key=lambda r: (r.created_at or now_naive(), r.id))
        ]
        return SubagentRunGraph(
            session_id=session_id,
            root_run_id=root.id,
            requested_run_id=requested.id,
            nodes=nodes,
            edges=[self._edge_schema(edge) for edge in edges],
        )

    def _validate_user_session(self, db: DBSession, *, user_id: str, session_id: str) -> Session:
        get_enabled_user(db, user_id)
        session = db.query(Session).filter(Session.id == session_id, Session.user_id == user_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        return session

    @staticmethod
    def _get_round(db: DBSession, *, session_id: str, run_id: str | None) -> Round:
        if not run_id:
            raise HTTPException(status_code=404, detail="运行不存在")
        round_obj = db.query(Round).filter(Round.id == run_id, Round.session_id == session_id).first()
        if not round_obj:
            raise HTTPException(status_code=404, detail="运行不存在")
        return round_obj

    @staticmethod
    def _get_edge(db: DBSession, *, edge_id: str, user_id: str, session_id: str) -> SubagentRun:
        edge = (
            db.query(SubagentRun)
            .filter(
                SubagentRun.id == edge_id,
                SubagentRun.user_id == user_id,
                SubagentRun.session_id == session_id,
            )
            .first()
        )
        if not edge:
            raise HTTPException(status_code=404, detail="Subagent run 不存在")
        return edge

    @staticmethod
    def _root_run_id(db: DBSession, run_id: str) -> str | None:
        parent_edge = db.query(SubagentRun).filter(SubagentRun.child_run_id == run_id).first()
        return parent_edge.root_run_id if parent_edge else run_id

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in SubagentRun.STATUSES:
            raise ValueError(f"Unsupported subagent status: {status}")

    @staticmethod
    def _status_from_child_round(round_obj: Round) -> str:
        status = round_obj.status or SubagentRun.RUNNING
        if status in SubagentRun.STATUSES:
            return status
        if status == "failed":
            return SubagentRun.FAILED
        if status == "cancelled":
            return SubagentRun.CANCELLED
        if status == "completed":
            return SubagentRun.COMPLETED
        return SubagentRun.RUNNING

    @staticmethod
    def _terminal_statuses() -> frozenset[str]:
        return frozenset({SubagentRun.COMPLETED, SubagentRun.FAILED, SubagentRun.CANCELLED})

    @staticmethod
    def _resolve_model_id(model_id: str | None) -> str | None:
        if model_id:
            return model_id
        return get_model_registry().get_subagent_default().id

    @staticmethod
    def _dump_metadata(metadata: dict[str, Any] | None) -> str | None:
        if not metadata:
            return None
        return json.dumps(metadata, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _load_metadata(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _node_from_round(round_obj: Round, *, kind: str) -> SubagentGraphNode:
        return SubagentGraphNode(
            run_id=round_obj.id,
            session_id=round_obj.session_id,
            parent_run_id=round_obj.parent_run_id,
            status=round_obj.status,
            kind=kind,
            created_at=round_obj.created_at,
            completed_at=round_obj.completed_at,
        )

    def _edge_schema(self, edge: SubagentRun) -> SubagentGraphEdge:
        return SubagentGraphEdge(
            edge_id=edge.id,
            root_run_id=edge.root_run_id,
            parent_run_id=edge.parent_run_id,
            child_run_id=edge.child_run_id,
            tool_call_id=edge.tool_call_id,
            agent_name=edge.agent_name,
            agent_type=edge.agent_type,
            model_id=edge.model_id,
            description=edge.description,
            prompt=edge.prompt,
            isolation=edge.isolation,
            worktree_path=edge.worktree_path,
            status=edge.status,
            output=edge.output,
            error=edge.error,
            metadata=self._load_metadata(edge.metadata_json),
            created_at=edge.created_at,
            started_at=edge.started_at,
            completed_at=edge.completed_at,
            updated_at=edge.updated_at,
        )


_GLOBAL_SUBAGENT_GRAPH_SERVICE = SubagentGraphService()


def get_subagent_graph_service() -> SubagentGraphService:
    return _GLOBAL_SUBAGENT_GRAPH_SERVICE
