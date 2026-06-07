"""Admin routes integration tests with real database queries."""

from datetime import timedelta
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_current_admin_user
from src.api.models.auth_login_event import AuthLoginEvent
from src.api.models.auth_user import AuthUser
from src.api.models.database import Base, get_db
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.subagent_run import SubagentRun
from src.api.models.user_run_lock import UserRunLock
from src.api.routes import admin as admin_routes
from src.api.utils.timezone import now_naive
from tests.db_safety import create_all_for_test_engine, ensure_safe_test_database_url, load_dotenv_database_url


@pytest.fixture
def admin_integration_client(tmp_path):
    test_db_url = os.environ.get("TEST_DATABASE_URL", "")
    ensure_safe_test_database_url(test_db_url, load_dotenv_database_url(Path(__file__).parent.parent))
    engine = create_engine(test_db_url)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Ensure required tables exist for real SQL query coverage.
    create_all_for_test_engine(engine, Base.metadata)

    # 清理可能遗留的脏数据（前次失败遗留）
    from sqlalchemy import text as _text
    with engine.begin() as conn:
        table_names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
        conn.execute(_text(f"TRUNCATE TABLE {table_names} CASCADE"))

    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/admin")

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_admin_user] = lambda: "admin"

    with TestClient(app) as client:
        yield client, TestingSessionLocal

    # 清理：PG 用 TRUNCATE CASCADE
    from sqlalchemy import text as _text
    with engine.begin() as conn:
        table_names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
        conn.execute(_text(f"TRUNCATE TABLE {table_names} CASCADE"))
    engine.dispose()


def _insert_round_with_step(
    db,
    *,
    session_id: str,
    user_id: str,
    title: str,
    round_id: str,
    status: str,
    created_at,
    user_message: str,
    final_response: str,
    request_messages: str,
    response_content: str,
):
    session = Session(
        id=session_id,
        user_id=user_id,
        title=title,
        status="active",
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(session)
    db.flush()

    round_row = Round(
        id=round_id,
        thread_id=session_id,
        session_id=session_id,
        user_message=user_message,
        final_response=final_response,
        step_count=1,
        status=status,
        created_at=created_at,
        completed_at=created_at + timedelta(seconds=5),
    )
    db.add(round_row)
    db.flush()

    llm_row = LLMCallRecord(
        session_id=session_id,
        round_id=round_id,
        step_index=1,
        request_message_count=3,
        request_messages=request_messages,
        request_tools=json.dumps(["read_file"]),
        response_content=response_content,
        response_thinking="",
        response_tool_calls=json.dumps([]),
        response_error=None,
        finish_reason="stop",
        usage_prompt_tokens=100,
        usage_completion_tokens=20,
        usage_total_tokens=120,
        first_token_latency_s=0.2,
        completion_latency_s=1.5,
        compaction_triggered=True,
        compaction_pre_tokens=9000,
        compaction_post_tokens=7000,
        compaction_tokens_saved=2000,
        compaction_microcompact_compacted_messages=5,
        compaction_summary_generated_count=1,
        compaction_summary_reused_count=0,
        compaction_summary_quality_repair_count=0,
        compaction_emergency_truncate_dropped_rounds=0,
    )
    db.add(llm_row)


def test_rounds_tree_real_sql_supports_limit_offset_status_search(admin_integration_client):
    client, SessionLocal = admin_integration_client

    now = now_naive()
    db = SessionLocal()
    _insert_round_with_step(
        db,
        session_id="s-new",
        user_id="admin",
        title="Newest",
        round_id="r-new",
        status="completed",
        created_at=now,
        user_message="find alpha keyword",
        final_response="done alpha",
        request_messages=json.dumps([{"role": "user", "content": "alpha"}]),
        response_content="alpha response",
    )
    _insert_round_with_step(
        db,
        session_id="s-mid",
        user_id="admin",
        title="Middle",
        round_id="r-mid",
        status="failed",
        created_at=now - timedelta(minutes=10),
        user_message="find beta keyword",
        final_response="error beta",
        request_messages=json.dumps([{"role": "user", "content": "beta"}]),
        response_content="beta response",
    )
    _insert_round_with_step(
        db,
        session_id="s-old",
        user_id="admin",
        title="Oldest",
        round_id="r-old",
        status="running",
        created_at=now - timedelta(minutes=20),
        user_message="find gamma keyword",
        final_response="",
        request_messages=json.dumps([{"role": "user", "content": "gamma"}]),
        response_content="gamma response",
    )
    db.commit()
    db.close()

    page1 = client.get("/admin/rounds-tree", params={"limit": 2, "offset": 0, "status": "all"})
    assert page1.status_code == 200
    page1_data = page1.json()
    assert page1_data["total_sessions"] == 3
    assert len(page1_data["sessions"]) == 2
    assert [item["session_id"] for item in page1_data["sessions"]] == ["s-new", "s-mid"]

    page2 = client.get("/admin/rounds-tree", params={"limit": 2, "offset": 2, "status": "all"})
    assert page2.status_code == 200
    page2_data = page2.json()
    assert page2_data["total_sessions"] == 3
    assert len(page2_data["sessions"]) == 1
    assert page2_data["sessions"][0]["session_id"] == "s-old"

    failed_only = client.get("/admin/rounds-tree", params={"status": "failed", "limit": 10, "offset": 0})
    assert failed_only.status_code == 200
    failed_data = failed_only.json()
    assert failed_data["total_sessions"] == 1
    assert len(failed_data["sessions"]) == 1
    assert failed_data["sessions"][0]["session_id"] == "s-mid"

    searched = client.get("/admin/rounds-tree", params={"search": "beta", "limit": 10, "offset": 0})
    assert searched.status_code == 200
    searched_data = searched.json()
    assert searched_data["total_sessions"] == 1
    assert len(searched_data["sessions"]) == 1
    assert searched_data["sessions"][0]["session_id"] == "s-mid"


def test_rounds_tree_step_list_is_lightweight_and_detail_is_full(admin_integration_client):
    client, SessionLocal = admin_integration_client

    now = now_naive()
    heavy_request = json.dumps(
        [{"role": "user", "content": "x" * 5000}],
        ensure_ascii=False,
    )
    heavy_response = "Y" * 6000

    db = SessionLocal()
    _insert_round_with_step(
        db,
        session_id="s-heavy",
        user_id="admin",
        title="HeavyPayload",
        round_id="r-heavy",
        status="completed",
        created_at=now,
        user_message="inspect heavy payload",
        final_response="ok",
        request_messages=heavy_request,
        response_content=heavy_response,
    )
    db.commit()
    db.close()

    rounds_tree = client.get("/admin/rounds-tree", params={"limit": 5, "offset": 0, "status": "all"})
    assert rounds_tree.status_code == 200
    data = rounds_tree.json()
    assert data["total_sessions"] == 1

    step = data["sessions"][0]["rounds"][0]["steps"][0]
    assert step["request_messages"] == ""
    assert step["response_content"] == ""
    assert step["response_tool_calls"] == ""

    detail = client.get(f"/admin/llm-call-records/{step['llm_record_id']}")
    assert detail.status_code == 200
    detail_data = detail.json()
    assert detail_data["request_messages"] == heavy_request
    assert detail_data["response_content"] == heavy_response


def test_rounds_tree_marks_subagent_child_rounds(admin_integration_client):
    client, SessionLocal = admin_integration_client

    now = now_naive()
    db = SessionLocal()
    db.add(AuthUser(
        user_id="admin",
        username="admin",
        auth_type="simple",
        enabled=True,
        is_admin=True,
    ))
    db.flush()
    _insert_round_with_step(
        db,
        session_id="s-subagent",
        user_id="admin",
        title="SubagentSession",
        round_id="parent-run",
        status="completed",
        created_at=now,
        user_message="你能不能给她派生长一点的任务",
        final_response="parent done",
        request_messages=json.dumps([{"role": "user", "content": "parent"}], ensure_ascii=False),
        response_content="parent response",
    )
    child_round = Round(
        id="child-run",
        thread_id="s-subagent",
        session_id="s-subagent",
        parent_run_id="parent-run",
        user_message="You are a child agent run spawned by a parent OpenCapyBox agent.",
        final_response="child done",
        step_count=1,
        status="completed",
        created_at=now + timedelta(seconds=1),
        completed_at=now + timedelta(seconds=6),
    )
    db.add(child_round)
    db.flush()
    db.add(SubagentRun(
        user_id="admin",
        session_id="s-subagent",
        root_run_id="parent-run",
        parent_run_id="parent-run",
        child_run_id="child-run",
        agent_type="general-purpose",
        model_id="sonnet",
        description="vlog完整制作方案+分镜脚本+爆款标题",
        prompt="child prompt",
        status=SubagentRun.COMPLETED,
    ))
    db.commit()
    db.close()

    response = client.get("/admin/rounds-tree", params={"limit": 5, "offset": 0, "status": "all"})

    assert response.status_code == 200
    rounds = response.json()["sessions"][0]["rounds"]
    by_id = {item["round_id"]: item for item in rounds}
    assert by_id["parent-run"]["run_kind"] == "main"
    assert by_id["parent-run"]["subagent_child_count"] == 1
    assert by_id["child-run"]["run_kind"] == "subagent"
    assert by_id["child-run"]["parent_run_id"] == "parent-run"
    assert by_id["child-run"]["subagent_type"] == "general-purpose"
    assert by_id["child-run"]["subagent_description"] == "vlog完整制作方案+分镜脚本+爆款标题"


def test_rounds_tree_resumed_round_does_not_make_session_running(admin_integration_client):
    client, SessionLocal = admin_integration_client

    now = now_naive()
    db = SessionLocal()
    _insert_round_with_step(
        db,
        session_id="s-resumed",
        user_id="admin",
        title="ResumedSession",
        round_id="round-resumed",
        status="resumed",
        created_at=now,
        user_message="你用subagent解决下问题",
        final_response="resumed by follow-up",
        request_messages=json.dumps([{"role": "user", "content": "resume"}], ensure_ascii=False),
        response_content="resume response",
    )
    db.commit()
    db.close()

    response = client.get("/admin/rounds-tree", params={"limit": 5, "offset": 0, "status": "all"})

    assert response.status_code == 200
    session = response.json()["sessions"][0]
    assert session["session_id"] == "s-resumed"
    assert session["status"] == "completed"
    assert session["rounds"][0]["status"] == "resumed"


def test_users_payload_counts_recent_user_run_lock_as_running(admin_integration_client):
    client, SessionLocal = admin_integration_client

    now = now_naive()
    db = SessionLocal()
    db.add(
        AuthUser(
            user_id="demo",
            username="demo",
            auth_type="simple",
            password_hash="hash",
            enabled=True,
            is_admin=False,
            created_by="test",
        )
    )
    db.add(
        AuthUser(
            user_id="idle",
            username="idle",
            auth_type="simple",
            password_hash="hash",
            enabled=True,
            is_admin=False,
            created_by="test",
        )
    )
    db.add(UserRunLock(user_id="demo", session_id="session-lock", lock_id="lock-1", created_at=now, updated_at=now))
    db.commit()
    db.close()

    resp = client.get("/admin/users")

    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["running_total"] == 1
    demo = next(item for item in data["users"] if item["user_id"] == "demo")
    idle = next(item for item in data["users"] if item["user_id"] == "idle")
    assert demo["status"] == "running"
    assert demo["running_rounds"] == 1
    assert idle["status"] == "idle"
    assert idle["running_rounds"] == 0


def test_users_payload_includes_latest_simple_login_ip(admin_integration_client):
    client, SessionLocal = admin_integration_client

    older = now_naive() - timedelta(hours=2)
    newer = now_naive() - timedelta(minutes=5)
    db = SessionLocal()
    db.add(
        AuthUser(
            user_id="demo",
            username="demo",
            auth_type="simple",
            password_hash="hash",
            enabled=True,
            is_admin=False,
            created_by="test",
        )
    )
    db.add(
        AuthLoginEvent(
            user_id="demo",
            username="demo",
            auth_type="simple",
            ip_address="198.51.100.1",
            user_agent="old-browser",
            login_at=older,
        )
    )
    db.add(
        AuthLoginEvent(
            user_id="demo",
            username="demo",
            auth_type="simple",
            ip_address="198.51.100.2",
            user_agent="new-browser",
            login_at=newer,
        )
    )
    db.commit()
    db.close()

    resp = client.get("/admin/users")

    assert resp.status_code == 200
    demo = next(item for item in resp.json()["users"] if item["user_id"] == "demo")
    assert demo["last_login_ip"] == "198.51.100.2"


def test_user_login_events_returns_recent_history(admin_integration_client):
    client, SessionLocal = admin_integration_client

    older = now_naive() - timedelta(hours=2)
    newer = now_naive() - timedelta(minutes=5)
    db = SessionLocal()
    db.add(
        AuthUser(
            user_id="demo",
            username="demo",
            auth_type="simple",
            password_hash="hash",
            enabled=True,
            is_admin=False,
            created_by="test",
        )
    )
    db.add(
        AuthLoginEvent(
            user_id="demo",
            username="demo",
            auth_type="simple",
            ip_address="198.51.100.1",
            user_agent="old-browser",
            login_at=older,
        )
    )
    db.add(
        AuthLoginEvent(
            user_id="demo",
            username="demo",
            auth_type="simple",
            ip_address="198.51.100.2",
            user_agent="new-browser",
            login_at=newer,
        )
    )
    db.commit()
    db.close()

    resp = client.get("/admin/users/demo/login-events", params={"limit": 1})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["user_id"] == "demo"
    assert len(payload["events"]) == 1
    assert payload["events"][0]["ip_address"] == "198.51.100.2"
    assert payload["events"][0]["user_agent"] == "new-browser"


def test_user_login_events_rejects_missing_user(admin_integration_client):
    client, _ = admin_integration_client

    resp = client.get("/admin/users/missing/login-events")

    assert resp.status_code == 404
    assert "用户不存在" in resp.json()["detail"]


def test_delete_user_rejects_recent_run_lock(admin_integration_client):
    client, SessionLocal = admin_integration_client

    now = now_naive()
    db = SessionLocal()
    db.add(
        AuthUser(
            user_id="demo",
            username="demo",
            auth_type="simple",
            password_hash="hash",
            enabled=True,
            is_admin=False,
            created_by="test",
        )
    )
    db.add(UserRunLock(user_id="demo", session_id="session-lock", lock_id="lock-1", created_at=now, updated_at=now))
    db.commit()
    db.close()

    resp = client.delete("/admin/users/demo")

    assert resp.status_code == 409
    assert "正在运行的任务" in resp.json()["detail"]
    verify_db = SessionLocal()
    try:
        assert verify_db.query(AuthUser).filter(AuthUser.user_id == "demo").count() == 1
    finally:
        verify_db.close()


def test_delete_user_allows_stale_run_lock(admin_integration_client):
    client, SessionLocal = admin_integration_client

    stale_time = now_naive() - timedelta(seconds=admin_routes.get_settings().sse_subscribe_timeout + 1)
    db = SessionLocal()
    db.add(
        AuthUser(
            user_id="demo",
            username="demo",
            auth_type="simple",
            password_hash="hash",
            enabled=True,
            is_admin=False,
            created_by="test",
        )
    )
    db.add(
        UserRunLock(
            user_id="demo",
            session_id="session-lock",
            lock_id="lock-1",
            created_at=stale_time,
            updated_at=stale_time,
        )
    )
    db.commit()
    db.close()

    sandbox_service = MagicMock()
    sandbox_service.get_cached.return_value = None
    with patch("src.api.routes.admin.get_agent_pool") as pool_mock:
        with patch("src.api.routes.admin.SandboxSessionService", return_value=sandbox_service):
            resp = client.delete("/admin/users/demo")

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "demo", "deleted": True}
    pool_mock.return_value.invalidate_user.assert_called_once_with("demo")
    sandbox_service.kill.assert_not_called()
    verify_db = SessionLocal()
    try:
        assert verify_db.query(AuthUser).filter(AuthUser.user_id == "demo").count() == 0
        assert verify_db.query(UserRunLock).filter(UserRunLock.user_id == "demo").count() == 0
    finally:
        verify_db.close()
