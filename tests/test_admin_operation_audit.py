"""Administrator operation audit lifecycle and read API tests."""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.api.services.admin_operation_audit as audit_service
from src.api.deps import get_current_admin_user, get_current_user
from src.api.models.admin_operation_log import AdminOperationLog
from src.api.models.auth_user import AuthUser
from src.api.models.database import Base, get_db
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.routes import admin_operation_logs
from src.api.routes import admin, admin_mcp, admin_permissions
from src.api.services.admin_operation_audit import (
    AdminAuditRoute,
    HIGH_RISK_ACTIONS,
    L0_ACTIONS,
    L1_ACTIONS,
    L2_ACTIONS,
    L3_ACTIONS,
    admin_audit_action,
    begin_admin_audit,
    enrich_admin_audit,
    finalize_admin_audit,
)
from src.api.utils.timezone import now_naive


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'admin-audit.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(audit_service, "SessionLocal", Session)
    with Session() as db:
        db.add(
            AuthUser(
                user_id="admin-id",
                username="admin",
                auth_type="simple",
                password_hash="hash",
                enabled=True,
                is_admin=True,
                created_by="test",
            )
        )
        db.commit()
    try:
        yield Session
    finally:
        engine.dispose()


def _client_for_router(router: APIRouter, Session, *, prefix: str = "/admin") -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix=prefix)

    def override_db():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: "admin-id"
    return TestClient(app, raise_server_exceptions=False)


def _lifecycle_router(executed: dict[str, int]) -> APIRouter:
    router = APIRouter(route_class=AdminAuditRoute)

    @router.get("/ok")
    @admin_audit_action("example.read", target_type="example")
    async def ok(
        request: Request,
        _admin: str = Depends(get_current_admin_user),
    ):
        executed["ok"] = executed.get("ok", 0) + 1
        enrich_admin_audit(
            request,
            target_id="example-1",
            changed_fields={
                "manual_review_status": {"before": "待复核", "after": "没问题"},
                "password": "must-not-appear",
                "prompt": "must-not-appear",
                "credential": "must-not-appear",
                "deleted": True,
            },
            details={
                "returned_count": 1,
                "password_changed": True,
                "api_key": "must-not-appear",
                "prompt": "must-not-appear",
            },
        )
        return {"ok": True}

    @router.get("/sessions/{session_id}/steps/{step_id}")
    @admin_audit_action(
        "step.view",
        target_type="step",
        target_param="step_id",
        session_param="session_id",
        step_param="step_id",
    )
    async def step(
        session_id: str,
        step_id: int,
        _admin: str = Depends(get_current_admin_user),
    ):
        return {"session_id": session_id, "step_id": step_id}

    @router.get("/missing")
    @admin_audit_action("example.missing")
    async def missing(_admin: str = Depends(get_current_admin_user)):
        raise HTTPException(status_code=404, detail="missing")

    @router.get("/boom")
    @admin_audit_action("example.boom")
    async def boom(_admin: str = Depends(get_current_admin_user)):
        raise RuntimeError("boom")

    @router.get("/validated")
    @admin_audit_action("example.validated")
    async def validated(
        amount: int = Query(..., ge=1),
        _admin: str = Depends(get_current_admin_user),
    ):
        return {"amount": amount}

    return router


def _classification_router(executed: dict[str, int]) -> APIRouter:
    router = APIRouter(route_class=AdminAuditRoute)

    @router.get("/sessions")
    @admin_audit_action(
        "session.list",
        query_action_param="search",
        query_action="session.search",
    )
    async def sessions(
        search: str | None = None,
        amount: int = Query(1, ge=1),
        _admin: str = Depends(get_current_admin_user),
    ):
        executed["sessions"] = executed.get("sessions", 0) + 1
        return {"search": search, "amount": amount}

    @router.get("/system")
    @admin_audit_action("system.read")
    async def system(_admin: str = Depends(get_current_admin_user)):
        raise HTTPException(status_code=503, detail="unavailable")

    return router


def test_sensitive_list_requests_are_persisted_and_query_action_is_resolved_before_handler(
    audit_db,
):
    executed: dict[str, int] = {}
    client = _client_for_router(_classification_router(executed), audit_db)

    assert client.get("/admin/sessions").status_code == 200
    assert client.get("/admin/sessions?amount=0").status_code == 422
    assert client.get("/admin/system").status_code == 503
    promoted_success = client.get("/admin/sessions?search=needle")
    promoted_failure = client.get("/admin/sessions?search=needle&amount=0")

    assert promoted_success.status_code == 200
    assert promoted_failure.status_code == 422
    assert executed == {"sessions": 2}
    with audit_db() as db:
        rows = db.query(AdminOperationLog).order_by(AdminOperationLog.id).all()
    assert [(row.action, row.outcome, row.status_code) for row in rows] == [
        ("session.list", "succeeded", 200),
        ("session.list", "failed", 422),
        ("session.search", "succeeded", 200),
        ("session.search", "failed", 422),
    ]


def test_l0_request_does_not_depend_on_operation_audit_database(
    audit_db,
    monkeypatch,
):
    executed: dict[str, int] = {}

    def unavailable_factory():
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(audit_service, "SessionLocal", unavailable_factory)
    response = _client_for_router(
        _classification_router(executed),
        audit_db,
    ).get("/admin/system")

    assert response.status_code == 503
    assert response.json()["detail"] == "unavailable"
    assert executed == {}


def test_route_lifecycle_persists_success_failure_targets_and_redaction(audit_db):
    executed: dict[str, int] = {}
    client = _client_for_router(_lifecycle_router(executed), audit_db)

    success = client.get("/admin/ok")
    not_found = client.get("/admin/missing")
    crashed = client.get("/admin/boom")
    step = client.get("/admin/sessions/session-1/steps/42")

    assert success.status_code == 200
    assert success.headers["x-request-id"]
    assert not_found.status_code == 404
    assert crashed.status_code == 500
    assert step.status_code == 200
    assert executed == {"ok": 1}

    with audit_db() as db:
        rows = {
            row.action: row
            for row in db.query(AdminOperationLog).order_by(AdminOperationLog.id).all()
        }
    assert rows["example.read"].outcome == "succeeded"
    assert rows["example.read"].status_code == 200
    assert rows["example.missing"].outcome == "failed"
    assert rows["example.missing"].status_code == 404
    assert rows["example.boom"].outcome == "failed"
    assert rows["example.boom"].status_code == 500
    assert rows["step.view"].session_id == "session-1"
    assert rows["step.view"].step_record_id == 42
    assert rows["step.view"].target_id == "42"
    assert rows["step.view"].route_template == "/admin/sessions/{session_id}/steps/{step_id}"

    changes = json.loads(rows["example.read"].changed_fields)
    assert changes["fields"] == ["credential", "deleted", "manual_review_status"]
    assert changes["values"]["manual_review_status"] == {
        "after": "没问题",
        "before": "待复核",
    }
    combined_metadata = (rows["example.read"].changed_fields or "") + (
        rows["example.read"].details_json or ""
    )
    assert "must-not-appear" not in combined_metadata
    assert "password_changed" in combined_metadata


def test_validation_error_is_audited_as_422(audit_db):
    client = _client_for_router(_lifecycle_router({}), audit_db)
    response = client.get("/admin/validated?amount=0")
    assert response.status_code == 422
    with audit_db() as db:
        row = db.query(AdminOperationLog).filter_by(action="example.validated").one()
        assert row.outcome == "failed"
        assert row.status_code == 422


def test_user_agent_that_contains_credentials_is_redacted(audit_db):
    response = _client_for_router(_lifecycle_router({}), audit_db).get(
        "/admin/ok",
        headers={"User-Agent": "Authorization: Bearer top-secret-token"},
    )

    assert response.status_code == 200
    with audit_db() as db:
        row = db.query(AdminOperationLog).one()
    assert row.user_agent == "[redacted]"
    assert "top-secret-token" not in row.user_agent


def test_missing_audit_declaration_blocks_authenticated_handler(audit_db):
    executed = {"count": 0}
    router = APIRouter(route_class=AdminAuditRoute)

    @router.post("/undeclared")
    async def undeclared(_admin: str = Depends(get_current_admin_user)):
        executed["count"] += 1
        return {"ok": True}

    response = _client_for_router(router, audit_db).post("/admin/undeclared")
    assert response.status_code == 503
    assert executed["count"] == 0
    with audit_db() as db:
        assert db.query(AdminOperationLog).count() == 0


def test_unauthenticated_admin_request_does_not_create_operation_log(audit_db):
    app = FastAPI()
    app.include_router(admin.router, prefix="/admin")

    def override_db():
        with audit_db() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    response = TestClient(app, raise_server_exceptions=False).get("/admin/overview")

    assert response.status_code == 401
    with audit_db() as db:
        assert db.query(AdminOperationLog).count() == 0


def test_started_insert_failure_blocks_business_handler(audit_db, monkeypatch):
    executed: dict[str, int] = {}

    def unavailable_factory():
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(audit_service, "SessionLocal", unavailable_factory)
    response = _client_for_router(_lifecycle_router(executed), audit_db).get("/admin/ok")
    assert response.status_code == 503
    assert executed == {}


def test_finalize_failure_returns_503_and_leaves_started_row(audit_db, monkeypatch):
    executed: dict[str, int] = {}
    calls = 0

    def fail_second_audit_transaction():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("audit finalize unavailable")
        return audit_db()

    monkeypatch.setattr(audit_service, "SessionLocal", fail_second_audit_transaction)
    response = _client_for_router(_lifecycle_router(executed), audit_db).get("/admin/ok")
    assert response.status_code == 503
    assert executed == {"ok": 1}
    with audit_db() as db:
        row = db.query(AdminOperationLog).one()
        assert row.outcome == "started"
        assert row.status_code is None


def test_terminal_row_cannot_be_finalized_twice(audit_db):
    spec = {
        "action": "example.once",
        "target_type": None,
        "target_param": None,
        "session_param": None,
        "step_param": None,
        "target_user_param": None,
    }
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/once",
            "raw_path": b"/admin/once",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "scheme": "http",
            "route": SimpleNamespace(path="/admin/once", admin_audit_spec=spec),
        }
    )
    begin_admin_audit(request, "admin-id")
    finalize_admin_audit(request, status_code=200)
    with pytest.raises(HTTPException) as exc_info:
        finalize_admin_audit(request, status_code=500)
    assert exc_info.value.status_code == 503
    with audit_db() as db:
        row = db.query(AdminOperationLog).one()
        assert row.outcome == "succeeded"
        assert row.status_code == 200


def test_real_session_and_step_routes_capture_only_safe_audit_context(audit_db):
    secret_prompt = "prompt-secret-must-not-be-audited"
    secret_answer = "answer-secret-must-not-be-audited"
    secret_thinking = "thinking-secret-must-not-be-audited"
    round_id = "round-audit-1"
    with audit_db() as db:
        db.add(
            Session(
                id="session-audit-1",
                user_id="target-user",
                title="private-session-title",
                status="active",
            )
        )
        db.flush()
        db.add(
            Round(
                id=round_id,
                thread_id="session-audit-1",
                session_id="session-audit-1",
                user_message=secret_prompt,
                final_response=secret_answer,
                step_count=1,
                status="completed",
                created_at=now_naive(),
                completed_at=now_naive(),
            )
        )
        db.flush()
        step = LLMCallRecord(
            session_id="session-audit-1",
            round_id=round_id,
            step_index=1,
            request_messages=json.dumps([{"role": "user", "content": secret_prompt}]),
            request_tools=json.dumps([{"name": "private-tool"}]),
            response_content=secret_answer,
            response_thinking=secret_thinking,
            response_tool_calls=json.dumps([{"arguments": "private-arguments"}]),
        )
        db.add(step)
        db.commit()
        db.refresh(step)
        step_id = step.id

    client = _client_for_router(admin.router, audit_db)
    session_response = client.get("/admin/sessions/session-audit-1/rounds")
    step_response = client.get(f"/admin/llm-call-records/{step_id}")
    review_response = client.put(
        f"/admin/llm-call-records/{step_id}/review",
        json={"manual_review_status": "有问题"},
    )

    assert session_response.status_code == 200
    assert step_response.status_code == 200
    assert review_response.status_code == 200
    with audit_db() as db:
        rows = {
            row.action: row
            for row in db.query(AdminOperationLog)
            .filter(AdminOperationLog.action.in_((
                "session.view",
                "step.view",
                "step.review.update",
            )))
            .all()
        }

    assert rows["session.view"].session_id == "session-audit-1"
    assert rows["session.view"].target_user_id == "target-user"
    assert rows["session.view"].step_record_id is None
    for action in ("step.view", "step.review.update"):
        assert rows[action].session_id == "session-audit-1"
        assert rows[action].step_record_id == step_id
        assert rows[action].target_user_id == "target-user"
    assert json.loads(rows["step.review.update"].changed_fields)["values"] == {
        "manual_review_status": {"after": "有问题", "before": "没问题"}
    }

    serialized_logs = "\n".join(
        (row.changed_fields or "") + (row.details_json or "")
        for row in rows.values()
    )
    assert round_id not in serialized_logs
    assert "private-session-title" not in serialized_logs
    assert secret_prompt not in serialized_logs
    assert secret_answer not in serialized_logs
    assert secret_thinking not in serialized_logs
    assert "private-arguments" not in serialized_logs


def test_real_session_search_logs_filter_shape_not_search_text(audit_db):
    search_text = "search-secret-must-not-be-audited"
    with audit_db() as db:
        db.add(
            Session(
                id="session-search-1",
                user_id="target-user",
                title="private-search-title",
                status="active",
            )
        )
        db.flush()
        db.add(
            Round(
                id="round-search-1",
                thread_id="session-search-1",
                session_id="session-search-1",
                user_message=search_text,
                final_response="private-answer",
                step_count=0,
                status="completed",
                created_at=now_naive(),
                completed_at=now_naive(),
            )
        )
        db.commit()

    response = _client_for_router(admin.router, audit_db).get(
        "/admin/rounds-tree",
        params={"search": search_text, "limit": 30, "offset": 0},
    )

    assert response.status_code == 200
    with audit_db() as db:
        row = db.query(AdminOperationLog).one()
    assert row.action == "session.search"
    assert row.outcome == "succeeded"
    assert json.loads(row.details_json) == {
        "has_search": True,
        "limit": 30,
        "offset": 0,
        "returned_count": 1,
        "status": "all",
    }
    persisted = "|".join(
        str(value or "")
        for value in (
            row.route_template,
            row.target_id,
            row.changed_fields,
            row.details_json,
        )
    )
    assert search_text not in persisted
    assert "private-search-title" not in persisted
    assert "private-answer" not in persisted


def test_audit_evidence_survives_target_user_and_session_deletion(audit_db):
    with audit_db() as db:
        db.add(
            AuthUser(
                user_id="deleted-target-user",
                username="deleted-target-user",
                auth_type="simple",
                password_hash="hash",
                enabled=True,
                is_admin=False,
                created_by="test",
            )
        )
        db.add(
            Session(
                id="deleted-session-1",
                user_id="deleted-target-user",
                title="deleted private title",
                status="active",
            )
        )
        db.add(
            AdminOperationLog(
                request_id="evidence-after-delete",
                actor_user_id="admin-id",
                action="session.view",
                target_type="session",
                target_id="deleted-session-1",
                target_user_id="deleted-target-user",
                session_id="deleted-session-1",
                outcome="succeeded",
                http_method="GET",
                route_template="/api/admin/sessions/{session_id}/rounds",
                status_code=200,
                started_at=now_naive(),
                completed_at=now_naive(),
            )
        )
        db.commit()

        db.query(Session).filter_by(id="deleted-session-1").delete()
        db.query(AuthUser).filter_by(user_id="deleted-target-user").delete()
        db.commit()

        evidence = db.query(AdminOperationLog).filter_by(
            request_id="evidence-after-delete"
        ).one()
        assert evidence.session_id == "deleted-session-1"
        assert evidence.target_user_id == "deleted-target-user"


def test_backend_user_export_is_audited_by_count_only(audit_db):
    response = _client_for_router(admin.router, audit_db).post(
        "/admin/users/export",
        json={"user_ids": ["admin-id"]},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    with audit_db() as db:
        row = db.query(AdminOperationLog).one()
    assert row.action == "user.export"
    assert row.outcome == "succeeded"
    assert json.loads(row.details_json) == {"exported_count": 1}
    assert row.changed_fields is None


def _seed_log(
    Session,
    *,
    request_id: str,
    action: str,
    started_at,
    outcome="succeeded",
    target_user_id="user-1",
    session_id="session-1",
):
    with Session() as db:
        db.add(
            AdminOperationLog(
                request_id=request_id,
                actor_user_id="admin-id",
                action=action,
                target_type="session",
                target_id="target-1",
                target_user_id=target_user_id,
                session_id=session_id,
                outcome=outcome,
                http_method="GET",
                route_template="/api/admin/example",
                status_code=200 if outcome == "succeeded" else None,
                started_at=started_at,
                completed_at=started_at if outcome != "started" else None,
            )
        )
        db.commit()


def test_operation_log_list_filters_paginates_and_records_sensitive_access(audit_db):
    now = now_naive()
    _seed_log(audit_db, request_id="seed-1", action="session.view", started_at=now)
    _seed_log(
        audit_db,
        request_id="seed-2",
        action="session.view",
        started_at=now - timedelta(minutes=1),
    )
    _seed_log(
        audit_db,
        request_id="old",
        action="session.view",
        started_at=now - timedelta(days=2),
    )
    client = _client_for_router(admin_operation_logs.router, audit_db)

    first = client.get("/admin/operation-logs?limit=1")
    assert first.status_code == 200
    payload = first.json()
    assert [item["request_id"] for item in payload["items"]] == ["seed-1"]
    assert payload["has_more"] is True
    assert payload["next_cursor"]

    second = client.get(
        "/admin/operation-logs",
        params={"action": "session.view", "limit": 1, "cursor": payload["next_cursor"]},
    )
    assert second.status_code == 200
    assert [item["request_id"] for item in second.json()["items"]] == ["seed-2"]
    assert second.json()["has_more"] is False
    refreshed = client.get("/admin/operation-logs?limit=10")
    refreshed_ids = {item["request_id"] for item in refreshed.json()["items"]}
    assert refreshed_ids == {
        "seed-1",
        "seed-2",
        first.headers["x-request-id"],
        second.headers["x-request-id"],
    }
    assert refreshed.headers["x-request-id"] not in refreshed_ids

    with audit_db() as db:
        rows = db.query(AdminOperationLog).filter_by(action="audit_log.list").all()
        assert len(rows) == 3
        assert {(row.outcome, row.status_code) for row in rows} == {("succeeded", 200)}


def test_operation_log_list_applies_time_target_session_and_outcome_filters(audit_db):
    now = now_naive()
    _seed_log(
        audit_db,
        request_id="filter-match",
        action="step.view",
        target_user_id="filter-user",
        session_id="filter-session",
        outcome="failed",
        started_at=now - timedelta(minutes=30),
    )
    _seed_log(
        audit_db,
        request_id="filter-wrong-outcome",
        action="step.view",
        target_user_id="filter-user",
        session_id="filter-session",
        outcome="succeeded",
        started_at=now - timedelta(minutes=20),
    )
    _seed_log(
        audit_db,
        request_id="filter-too-old",
        action="step.view",
        target_user_id="filter-user",
        session_id="filter-session",
        outcome="failed",
        started_at=now - timedelta(hours=2),
    )

    response = _client_for_router(admin_operation_logs.router, audit_db).get(
        "/admin/operation-logs",
        params={
            "from": (now - timedelta(hours=1)).isoformat(),
            "to": now.isoformat(),
            "action": "step.view",
            "target_user_id": "filter-user",
            "session_id": "filter-session",
            "outcome": "failed",
        },
    )

    assert response.status_code == 200
    assert [item["request_id"] for item in response.json()["items"]] == [
        "filter-match"
    ]


def test_operation_log_list_filters_derived_risk_level_and_supports_intersection(
    audit_db,
):
    now = now_naive()
    _seed_log(
        audit_db,
        request_id="risk-session",
        action="session.view",
        started_at=now,
    )
    _seed_log(
        audit_db,
        request_id="risk-step",
        action="step.view",
        started_at=now - timedelta(seconds=1),
    )
    _seed_log(
        audit_db,
        request_id="risk-normal",
        action="user.list",
        started_at=now - timedelta(seconds=2),
    )
    client = _client_for_router(admin_operation_logs.router, audit_db)

    high = client.get("/admin/operation-logs", params={"risk_level": "high"})
    assert high.status_code == 200
    assert [item["request_id"] for item in high.json()["items"]] == ["risk-step"]
    assert {item["risk_level"] for item in high.json()["items"]} == {"high"}

    normal = client.get(
        "/admin/operation-logs",
        params={"risk_level": "normal", "action": "session.view"},
    )
    assert normal.status_code == 200
    assert [item["request_id"] for item in normal.json()["items"]] == ["risk-session"]
    assert normal.json()["items"][0]["risk_level"] == "normal"

    conflicting = client.get(
        "/admin/operation-logs",
        params={"risk_level": "high", "action": "session.view"},
    )
    assert conflicting.status_code == 200
    assert conflicting.json()["items"] == []

    with audit_db() as db:
        rows = db.query(AdminOperationLog).filter_by(action="audit_log.list").all()
        assert len(rows) == 3
        assert {(row.outcome, row.status_code) for row in rows} == {("succeeded", 200)}


def test_operation_log_list_rejects_invalid_risk_level_and_audits_failure(audit_db):
    response = _client_for_router(admin_operation_logs.router, audit_db).get(
        "/admin/operation-logs",
        params={"risk_level": "critical"},
    )

    assert response.status_code == 400
    with audit_db() as db:
        row = db.query(AdminOperationLog).one()
        assert row.action == "audit_log.list"
        assert row.outcome == "failed"
        assert row.status_code == 400


def test_operation_log_export_is_csv_injection_safe_and_audited(audit_db):
    _seed_log(
        audit_db,
        request_id="seed-export",
        action="session.view",
        started_at=now_naive(),
    )
    with audit_db() as db:
        row = db.query(AdminOperationLog).filter_by(request_id="seed-export").one()
        row.target_id = "=HYPERLINK(\"https://invalid\")"
        db.commit()

    client = _client_for_router(admin_operation_logs.router, audit_db)
    response = client.get("/admin/operation-logs/export?action=session.view")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    parsed = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert len(parsed) == 1
    assert parsed[0]["request_id"] == "seed-export"
    assert parsed[0]["risk_level"] == "normal"
    assert parsed[0]["target_id"].startswith("'=HYPERLINK")

    with audit_db() as db:
        export_row = db.query(AdminOperationLog).filter_by(action="audit_log.export").one()
        assert export_row.outcome == "succeeded"
        assert json.loads(export_row.details_json) == {
            "exported_count": 1,
            "has_risk_filter": False,
        }


def test_operation_log_export_applies_high_risk_filter(audit_db):
    now = now_naive()
    _seed_log(
        audit_db,
        request_id="export-high",
        action="step.view",
        started_at=now,
    )
    _seed_log(
        audit_db,
        request_id="export-normal",
        action="user.list",
        started_at=now - timedelta(seconds=1),
    )

    response = _client_for_router(admin_operation_logs.router, audit_db).get(
        "/admin/operation-logs/export",
        params={"risk_level": "high"},
    )

    assert response.status_code == 200
    parsed = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert [row["request_id"] for row in parsed] == ["export-high"]
    assert parsed[0]["risk_level"] == "high"
    with audit_db() as db:
        export_row = db.query(AdminOperationLog).filter_by(
            request_id=response.headers["x-request-id"]
        ).one()
    assert json.loads(export_row.details_json) == {
        "exported_count": 1,
        "has_risk_filter": True,
        "risk_level": "high",
    }


def test_operation_log_export_rejects_results_above_limit_and_audits_failure(
    audit_db,
    monkeypatch,
):
    monkeypatch.setattr(admin_operation_logs, "_EXPORT_LIMIT", 1)
    now = now_naive()
    _seed_log(
        audit_db,
        request_id="export-limit-1",
        action="session.view",
        started_at=now,
    )
    _seed_log(
        audit_db,
        request_id="export-limit-2",
        action="session.view",
        started_at=now - timedelta(seconds=1),
    )

    response = _client_for_router(admin_operation_logs.router, audit_db).get(
        "/admin/operation-logs/export?action=session.view"
    )

    assert response.status_code == 400
    with audit_db() as db:
        export_row = db.query(AdminOperationLog).filter_by(
            action="audit_log.export"
        ).one()
        assert export_row.outcome == "failed"
        assert export_row.status_code == 400


def test_every_admin_route_declares_audit_action_and_admin_dependency():
    routers = (
        admin.router,
        admin_mcp.router,
        admin_permissions.router,
        admin_operation_logs.router,
    )
    checked = 0
    actions: set[str] = set()
    for router in routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            checked += 1
            assert isinstance(route, AdminAuditRoute), route.path
            assert route.admin_audit_spec is not None, route.path
            assert route.admin_audit_spec["action"], route.path
            actions.add(route.admin_audit_spec["action"])
            dependency_calls = {
                dependency.call for dependency in route.dependant.dependencies
            }
            assert get_current_admin_user in dependency_calls, route.path
    assert checked > 0
    expected_actions = {
        "overview.read",
        "system.read",
        "session.list",
        "session.view",
        "step.view",
        "step.review.update",
        "user.list",
        "user.login_history.view",
        "user.create",
        "user.enabled.update",
        "user.admin.update",
        "user.token_limits.update",
        "user.model_groups.update",
        "user.password.reset",
        "user.delete",
        "user.export",
        "sandbox.list",
        "sandbox.create",
        "sandbox.update",
        "sandbox.default.set",
        "sandbox.enabled.update",
        "user.sandbox.update",
        "model.list",
        "model.create",
        "model.update",
        "model.delete",
        "model.settings.update",
        "model_group.list",
        "model_group.create",
        "model_group.update",
        "model_group.models.update",
        "model_group.users.update",
        "mcp.list",
        "mcp.create",
        "mcp.update",
        "mcp.delete",
        "mcp.test",
        "tool_permission.list",
        "tool_permission.create",
        "tool_permission.update",
        "tool_permission.delete",
        "audit_log.list",
        "audit_log.export",
    }
    assert actions == expected_actions
    levels = (L0_ACTIONS, L1_ACTIONS, L2_ACTIONS, L3_ACTIONS)
    assert set().union(*levels) == expected_actions | {"session.search"}
    assert all(
        left.isdisjoint(right)
        for index, left in enumerate(levels)
        for right in levels[index + 1 :]
    )
    assert HIGH_RISK_ACTIONS == L3_ACTIONS == {"step.view"}
    assert L1_ACTIONS == {
        "audit_log.list",
        "session.list",
        "session.search",
        "session.view",
        "user.list",
        "user.login_history.view",
    }
    assert L0_ACTIONS == {
        "overview.read",
        "system.read",
        "sandbox.list",
        "model.list",
        "model_group.list",
        "mcp.list",
        "tool_permission.list",
    }
    assert "mcp.test" in L2_ACTIONS


def test_every_registered_api_admin_route_uses_strict_audit_route():
    from src.api.config import get_settings
    from src.api.main import app

    admin_prefix = f"{get_settings().api_prefix.rstrip('/')}/admin"
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and (route.path == admin_prefix or route.path.startswith(admin_prefix + "/"))
    ]
    assert routes
    for route in routes:
        assert isinstance(route, AdminAuditRoute), route.path
        assert route.admin_audit_spec and route.admin_audit_spec["action"], route.path
