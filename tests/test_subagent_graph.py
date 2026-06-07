"""Subagent run graph service and route tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.auth_user import AuthUser
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.subagent_run import SubagentRun
from src.api.services.history_service import HistoryService
from src.api.services.subagent_graph_service import SubagentGraphService
from src.api.utils.timezone import now_naive


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine, TestingSessionLocal()


def _seed(db, *, user_id: str = "u1", session_id: str = "s1", include_grandchild: bool = True) -> None:
    db.add(
        AuthUser(
            user_id=user_id,
            username=user_id,
            auth_type="simple",
            password_hash="hash",
            enabled=True,
        )
    )
    db.add(Session(id=session_id, user_id=user_id, status="active"))
    db.add(Round(id="root-run", session_id=session_id, thread_id=session_id, user_message="root", status="running"))
    db.add(
        Round(
            id="child-run",
            session_id=session_id,
            thread_id=session_id,
            user_message="child",
            status="completed",
            final_response="child done",
            completed_at=now_naive(),
        )
    )
    if include_grandchild:
        db.add(
            Round(
                id="grandchild-run",
                session_id=session_id,
                thread_id=session_id,
                user_message="grandchild",
                status="running",
            )
        )
    db.commit()


def test_create_edge_and_query_graph():
    engine, db = _make_db()
    try:
        _seed(db)
        service = SubagentGraphService()

        edge = service.create_edge(
            db,
            user_id="u1",
            session_id="s1",
            parent_run_id="root-run",
            child_run_id="child-run",
            tool_call_id="tc-agent-1",
            agent_name="researcher",
            agent_type="research",
            model_id="sonnet",
            description="check docs",
            prompt="Read docs and summarize",
            metadata={"source": "AgentTool"},
        )
        graph = service.get_graph(db, user_id="u1", session_id="s1", run_id="root-run")

        assert edge.root_run_id == "root-run"
        assert edge.status == "completed"
        assert {node.run_id for node in graph.nodes} == {"root-run", "child-run"}
        assert graph.edges[0].tool_call_id == "tc-agent-1"
        assert graph.edges[0].metadata == {"source": "AgentTool"}
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_create_edge_uses_subagent_default_model_from_registry():
    engine, db = _make_db()
    try:
        _seed(db)
        service = SubagentGraphService()
        fake_registry = SimpleNamespace(
            get_subagent_default=lambda: SimpleNamespace(id="subagent-model")
        )

        with patch("src.api.services.subagent_graph_service.get_model_registry", return_value=fake_registry):
            edge = service.create_edge(
                db,
                user_id="u1",
                session_id="s1",
                parent_run_id="root-run",
                prompt="default model child",
            )

        assert edge.model_id == "subagent-model"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_nested_edge_inherits_root_run_id():
    engine, db = _make_db()
    try:
        _seed(db)
        service = SubagentGraphService()
        service.create_edge(
            db,
            user_id="u1",
            session_id="s1",
            parent_run_id="root-run",
            child_run_id="child-run",
            prompt="first child",
            model_id="sonnet",
        )
        nested = service.create_edge(
            db,
            user_id="u1",
            session_id="s1",
            parent_run_id="child-run",
            child_run_id="grandchild-run",
            prompt="nested child",
            model_id="sonnet",
            status=SubagentRun.RUNNING,
        )
        graph = service.get_graph(db, user_id="u1", session_id="s1", run_id="grandchild-run")

        assert nested.root_run_id == "root-run"
        assert graph.root_run_id == "root-run"
        assert len(graph.edges) == 2
        assert {edge.parent_run_id for edge in graph.edges} == {"root-run", "child-run"}
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_mark_status_sets_output_and_completed_at():
    engine, db = _make_db()
    try:
        _seed(db)
        service = SubagentGraphService()
        edge = service.create_edge(
            db,
            user_id="u1",
            session_id="s1",
            parent_run_id="root-run",
            prompt="pending child",
            model_id="sonnet",
        )

        updated = service.mark_status(
            db,
            edge_id=edge.id,
            user_id="u1",
            session_id="s1",
            status=SubagentRun.COMPLETED,
            output="done",
        )

        assert updated.status == SubagentRun.COMPLETED
        assert updated.output == "done"
        assert updated.completed_at is not None
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_subagent_graph_route_returns_graph():
    from tests.helpers import make_test_client
    from src.api.routes import chat as chat_routes

    engine, db = _make_db()
    try:
        _seed(db, user_id="testuser", session_id="session-1")
        SubagentGraphService().create_edge(
            db,
            user_id="testuser",
            session_id="session-1",
            parent_run_id="root-run",
            child_run_id="child-run",
            agent_type="verification",
            model_id="sonnet",
            prompt="verify this",
        )
        client = make_test_client(chat_routes.router, "/chat", user="testuser", db=db)

        response = client.get("/chat/session-1/round/root-run/subagent-graph")

        assert response.status_code == 200
        payload = response.json()
        assert payload["root_run_id"] == "root-run"
        assert payload["edges"][0]["agent_type"] == "verification"
        assert {node["run_id"] for node in payload["nodes"]} == {"root-run", "child-run"}
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_history_excludes_subagent_child_rounds():
    engine, db = _make_db()
    try:
        _seed(db, include_grandchild=False)
        SubagentGraphService().create_edge(
            db,
            user_id="u1",
            session_id="s1",
            parent_run_id="root-run",
            child_run_id="child-run",
            agent_type="general-purpose",
            model_id="sonnet",
            prompt="delegate this",
        )

        rounds = HistoryService(db).get_session_rounds("s1")

        assert [round_data["round_id"] for round_data in rounds] == ["root-run"]
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
