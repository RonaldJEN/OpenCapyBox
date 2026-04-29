"""管理后台路由测试。"""

from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.deps import get_current_admin_user
from src.api.models.database import get_db
from src.api.routes import admin as admin_routes


def _make_client(*, admin_enabled: bool) -> TestClient:
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: MagicMock()

    if admin_enabled:
        app.dependency_overrides[get_current_admin_user] = lambda: "admin"
    else:
        async def _deny_admin():
            raise HTTPException(status_code=403, detail="需要管理员权限")

        app.dependency_overrides[get_current_admin_user] = _deny_admin

    return TestClient(app)


class TestAdminRouter:
    def test_admin_requires_permission(self):
        client = _make_client(admin_enabled=False)
        resp = client.get("/admin/overview")
        assert resp.status_code == 403
        assert "管理员权限" in resp.json()["detail"]

    def test_overview_delegates_to_builder(self):
        client = _make_client(admin_enabled=True)
        payload = {"summary": {"users_total": 2}, "trends": []}

        with patch("src.api.routes.admin._build_overview_payload", return_value=payload) as mocked:
            resp = client.get("/admin/overview", params={"days": 14})

        assert resp.status_code == 200
        assert resp.json() == payload
        assert mocked.call_count == 1
        assert mocked.call_args.args[1] == 14

    def test_rounds_tree_delegates_to_builder(self):
        client = _make_client(admin_enabled=True)
        payload = {"total_sessions": 1, "offset": 0, "limit": 10, "sessions": []}

        with patch("src.api.routes.admin._build_rounds_tree_payload", return_value=payload) as mocked:
            resp = client.get(
                "/admin/rounds-tree",
                params={
                    "limit": 10,
                    "offset": 0,
                    "status": "completed",
                    "user_id": "demo",
                    "search": "hello",
                },
            )

        assert resp.status_code == 200
        assert resp.json() == payload
        assert mocked.call_count == 1
        assert mocked.call_args.kwargs["status"] == "completed"
        assert mocked.call_args.kwargs["user_id"] == "demo"
        assert mocked.call_args.kwargs["search"] == "hello"

    def test_update_llm_review_delegates_to_helper(self):
        client = _make_client(admin_enabled=True)
        payload = {"llm_record_id": 12, "manual_review_status": "有问题"}

        with patch("src.api.routes.admin._update_llm_record_review_status", return_value=payload) as mocked:
            resp = client.put(
                "/admin/llm-call-records/12/review",
                json={"manual_review_status": "有问题"},
            )

        assert resp.status_code == 200
        assert resp.json() == payload
        assert mocked.call_count == 1
        assert mocked.call_args.kwargs["llm_record_id"] == 12
        assert mocked.call_args.kwargs["manual_review_status"] == "有问题"

    def test_update_llm_review_rejects_invalid_status(self):
        client = _make_client(admin_enabled=True)

        resp = client.put(
            "/admin/llm-call-records/7/review",
            json={"manual_review_status": "待确认"},
        )

        assert resp.status_code == 400
        assert "manual_review_status" in resp.json()["detail"]

    def test_get_llm_call_record_detail_delegates_to_builder(self):
        client = _make_client(admin_enabled=True)
        payload = {
            "llm_record_id": 101,
            "round_id": "round-1",
            "step_index": 1,
            "request_message_count": 2,
            "request_messages": "[]",
            "request_tools": "[]",
            "finish_reason": "stop",
            "response_error": None,
            "response_preview": "ok",
            "response_content": "ok",
            "response_thinking": "",
            "response_tool_calls": "[]",
            "usage_prompt_tokens": 10,
            "usage_completion_tokens": 5,
            "usage_total_tokens": 15,
            "first_token_latency_s": 0.2,
            "completion_latency_s": 0.8,
            "compaction_triggered": False,
            "compaction_pre_tokens": 0,
            "compaction_post_tokens": 0,
            "compaction_tokens_saved": 0,
            "compaction_microcompact_compacted_messages": 0,
            "compaction_summary_generated_count": 0,
            "compaction_summary_reused_count": 0,
            "compaction_summary_quality_repair_count": 0,
            "compaction_emergency_truncate_dropped_rounds": 0,
            "manual_review_status": "没问题",
            "created_at": None,
        }

        with patch("src.api.routes.admin._build_llm_record_detail_payload", return_value=payload) as mocked:
            resp = client.get("/admin/llm-call-records/101")

        assert resp.status_code == 200
        assert resp.json() == payload
        assert mocked.call_count == 1
        assert mocked.call_args.args[1] == 101

    def test_users_delegates_to_builder(self):
        client = _make_client(admin_enabled=True)
        payload = {
            "summary": {"users_total": 2, "admins_total": 1, "active_total": 1, "running_total": 0},
            "users": [
                {
                    "user_id": "admin",
                    "role": "admin",
                    "is_admin": True,
                    "status": "active",
                    "sessions_count": 1,
                    "rounds_count": 1,
                    "running_rounds": 0,
                    "total_tokens": 120,
                    "cron_jobs_total": 0,
                    "cron_jobs_enabled": 0,
                    "cron_failed_24h": 0,
                    "last_active_at": None,
                }
            ],
        }

        with patch("src.api.routes.admin._build_users_payload", return_value=payload) as mocked:
            resp = client.get("/admin/users")

        assert resp.status_code == 200
        assert resp.json() == payload
        assert mocked.call_count == 1

    def test_system_delegates_to_builder(self):
        client = _make_client(admin_enabled=True)
        payload = {
            "window_hours": 24,
            "summary": {
                "running_rounds": 0,
                "active_sessions_30m": 0,
                "round_status_counts": {},
                "cron_status_counts": {},
                "avg_completion_latency_s": None,
                "p50_completion_latency_s": None,
                "p95_completion_latency_s": None,
                "avg_first_token_latency_s": None,
                "llm_calls": 0,
                "compaction_calls": 0,
                "compaction_tokens_saved": 0,
                "compaction_quality_repairs": 0,
                "compaction_emergency_drops": 0,
                "llm_response_errors": 0,
            },
        }

        with patch("src.api.routes.admin._build_system_payload", return_value=payload) as mocked:
            resp = client.get("/admin/system", params={"hours": 48})

        assert resp.status_code == 200
        assert resp.json() == payload
        assert mocked.call_count == 1
        assert mocked.call_args.args[1] == 48
