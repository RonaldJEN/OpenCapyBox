"""Admin routes integration tests with real sqlite queries."""

from datetime import timedelta
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_current_admin_user
from src.api.models.database import Base, get_db
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.routes import admin as admin_routes
from src.api.utils.timezone import now_naive


@pytest.fixture
def admin_integration_client(tmp_path):
    db_file = tmp_path / "admin-routes-integration.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Ensure required tables exist for real SQL query coverage.
    Base.metadata.create_all(bind=engine)

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
