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
from src.api.models.auth_user import AuthUser
from src.api.models.database import Base, get_db
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.user_run_lock import UserRunLock
from src.api.routes import admin as admin_routes
from src.api.utils.timezone import now_naive
from tests.db_safety import create_all_for_test_engine, ensure_safe_test_database_url, load_dotenv_database_url


@pytest.fixture
def admin_integration_client(tmp_path):
    test_db_url = os.environ.get("TEST_DATABASE_URL", "")
    if test_db_url.startswith("postgresql"):
        ensure_safe_test_database_url(test_db_url, load_dotenv_database_url(Path(__file__).parent.parent))
        engine = create_engine(test_db_url)
    else:
        db_file = tmp_path / "admin-routes-integration.db"
        engine = create_engine(
            f"sqlite:///{db_file}",
            connect_args={"check_same_thread": False},
        )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Ensure required tables exist for real SQL query coverage.
    create_all_for_test_engine(engine, Base.metadata)

    # PG: 清理可能遗留的脏数据（前次失败遗留）
    if test_db_url.startswith("postgresql"):
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

    # 清理：PG 用 TRUNCATE CASCADE，SQLite 直接 drop
    if test_db_url.startswith("postgresql"):
        from sqlalchemy import text as _text
        with engine.begin() as conn:
            table_names = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
            conn.execute(_text(f"TRUNCATE TABLE {table_names} CASCADE"))
    else:
        Base.metadata.drop_all(bind=engine)
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
