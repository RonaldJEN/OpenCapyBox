"""管理后台路由测试。"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.deps import get_current_admin_user
from src.api.models.database import get_db
from src.api.routes import admin as admin_routes


def _make_client(*, admin_enabled: bool, db=None) -> TestClient:
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/admin")
    test_db = db or MagicMock()
    app.dependency_overrides[get_db] = lambda: test_db

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

    def test_create_simple_user_delegates_to_service(self):
        client = _make_client(admin_enabled=True)
        user_obj = object()
        payload = {"user_id": "demo3", "auth_type": "simple", "enabled": True}

        with patch("src.api.routes.admin.create_simple_user", return_value=user_obj) as create_mock:
            with patch("src.api.routes.admin.auth_user_to_payload", return_value=payload):
                resp = client.post(
                    "/admin/users/simple",
                    json={"username": "demo3", "password": "pass123", "enabled": True},
                )

        assert resp.status_code == 200
        assert resp.json() == payload
        assert create_mock.call_args.kwargs["username"] == "demo3"
        assert create_mock.call_args.kwargs["created_by"] == "admin"

    def test_create_simple_user_strips_username(self):
        client = _make_client(admin_enabled=True)
        user_obj = object()
        payload = {"user_id": "demo3", "auth_type": "simple", "enabled": True}

        with patch("src.api.routes.admin.create_simple_user", return_value=user_obj) as create_mock:
            with patch("src.api.routes.admin.auth_user_to_payload", return_value=payload):
                resp = client.post(
                    "/admin/users/simple",
                    json={"username": " demo3 ", "password": " pass123 "},
                )

        assert resp.status_code == 200
        assert create_mock.call_args.kwargs["username"] == "demo3"
        assert create_mock.call_args.kwargs["password"] == " pass123 "

    def test_create_ldap_user_delegates_to_service(self):
        client = _make_client(admin_enabled=True)
        user_obj = object()
        payload = {"user_id": "zhangsan", "auth_type": "ldap", "enabled": True}

        with patch("src.api.routes.admin.create_ldap_user", return_value=user_obj) as create_mock:
            with patch("src.api.routes.admin.auth_user_to_payload", return_value=payload):
                resp = client.post(
                    "/admin/users/ldap",
                    json={"user_id": "zhangsan", "enabled": True},
                )

        assert resp.status_code == 200
        assert resp.json() == payload
        assert create_mock.call_args.kwargs["user_id"] == "zhangsan"
        assert create_mock.call_args.kwargs["created_by"] == "admin"

    def test_update_token_limits_delegates_to_service(self):
        client = _make_client(admin_enabled=True)
        user_obj = object()
        payload = {"user_id": "demo", "token_limit_per_week": 100, "token_limit_per_month": 1000}

        with patch("src.api.routes.admin.update_user_token_limits", return_value=user_obj) as update_mock:
            with patch("src.api.routes.admin.auth_user_to_payload", return_value=payload):
                resp = client.patch(
                    "/admin/users/demo/token-limits",
                    json={"token_limit_per_week": 100, "token_limit_per_month": 1000},
                )

        assert resp.status_code == 200
        assert resp.json() == payload
        assert update_mock.call_args.kwargs["user_id"] == "demo"
        assert update_mock.call_args.kwargs["token_limit_per_week"] == 100

    def test_update_token_limits_rejects_negative_values(self):
        client = _make_client(admin_enabled=True)

        with patch("src.api.routes.admin.update_user_token_limits") as update_mock:
            resp = client.patch(
                "/admin/users/demo/token-limits",
                json={"token_limit_per_week": -1, "token_limit_per_month": 1000},
            )

        assert resp.status_code == 422
        update_mock.assert_not_called()

    def test_create_simple_user_rejects_negative_token_limit(self):
        client = _make_client(admin_enabled=True)

        with patch("src.api.routes.admin.create_simple_user") as create_mock:
            resp = client.post(
                "/admin/users/simple",
                json={"username": "demo3", "password": "pass123", "token_limit_per_week": -1},
            )

        assert resp.status_code == 422
        create_mock.assert_not_called()

    def test_create_simple_user_rejects_blank_identity_or_password(self):
        client = _make_client(admin_enabled=True)

        with patch("src.api.routes.admin.create_simple_user") as create_mock:
            blank_username = client.post(
                "/admin/users/simple",
                json={"username": "   ", "password": "pass123"},
            )
            blank_password = client.post(
                "/admin/users/simple",
                json={"username": "demo3", "password": "   "},
            )

        assert blank_username.status_code == 422
        assert blank_password.status_code == 422
        create_mock.assert_not_called()

    def test_create_ldap_user_rejects_blank_or_too_long_user_id(self):
        client = _make_client(admin_enabled=True)

        with patch("src.api.routes.admin.create_ldap_user") as create_mock:
            blank_user = client.post("/admin/users/ldap", json={"user_id": "   "})
            long_user = client.post("/admin/users/ldap", json={"user_id": "u" * 101})

        assert blank_user.status_code == 422
        assert long_user.status_code == 422
        create_mock.assert_not_called()

    def test_reset_password_rejects_blank_password(self):
        client = _make_client(admin_enabled=True)

        with patch("src.api.routes.admin.reset_simple_user_password") as reset_mock:
            resp = client.post("/admin/users/demo/reset-password", json={"password": "   "})

        assert resp.status_code == 422
        reset_mock.assert_not_called()

    def test_admin_cannot_disable_self(self):
        client = _make_client(admin_enabled=True)

        with patch("src.api.routes.admin.update_user_enabled") as update_mock:
            resp = client.patch("/admin/users/admin/enabled", json={"enabled": False})

        assert resp.status_code == 400
        assert "不能禁用" in resp.json()["detail"]
        update_mock.assert_not_called()

    def test_admin_cannot_demote_self(self):
        client = _make_client(admin_enabled=True)

        with patch("src.api.routes.admin.update_user_admin") as update_mock:
            resp = client.patch("/admin/users/admin/admin", json={"is_admin": False})

        assert resp.status_code == 400
        assert "不能取消" in resp.json()["detail"]
        update_mock.assert_not_called()

    def test_delete_user_delegates_to_service(self):
        db = MagicMock()
        client = _make_client(admin_enabled=True, db=db)
        user = MagicMock()
        user.user_id = "demo"
        db.query.return_value.filter.return_value.first.side_effect = [user, None, None, None]
        sandbox_service = MagicMock()
        sandbox_service.get_cached.return_value = None

        with patch("src.api.routes.admin.get_agent_pool") as pool_mock:
            with patch("src.api.routes.admin.SandboxSessionService", return_value=sandbox_service):
                with patch("src.api.routes.admin.delete_auth_user", return_value="demo") as delete_mock:
                    resp = client.delete("/admin/users/demo")

        assert resp.status_code == 200
        assert resp.json() == {"user_id": "demo", "deleted": True}
        pool_mock.return_value.invalidate_user.assert_called_once_with("demo")
        sandbox_service.kill.assert_not_called()
        assert delete_mock.call_args.kwargs["user_id"] == "demo"

    def test_delete_user_kills_sandbox_before_service(self):
        db = MagicMock()
        client = _make_client(admin_enabled=True, db=db)
        user = MagicMock()
        user.user_id = "demo"
        user_sandbox = MagicMock()
        user_sandbox.sandbox_id = "sbx-demo"
        db.query.return_value.filter.return_value.first.side_effect = [user, None, None, user_sandbox]
        sandbox_service = MagicMock()
        sandbox_service.get_cached.return_value = None
        sandbox_service.kill = AsyncMock(return_value=True)

        with patch("src.api.routes.admin.get_agent_pool"):
            with patch("src.api.routes.admin.SandboxSessionService", return_value=sandbox_service):
                with patch("src.api.routes.admin.delete_auth_user", return_value="demo") as delete_mock:
                    resp = client.delete("/admin/users/demo")

        assert resp.status_code == 200
        sandbox_service.kill.assert_awaited_once_with("demo", "sbx-demo")
        delete_mock.assert_called_once()

    def test_delete_user_rejects_sandbox_cleanup_failure(self):
        db = MagicMock()
        client = _make_client(admin_enabled=True, db=db)
        user = MagicMock()
        user.user_id = "demo"
        user_sandbox = MagicMock()
        user_sandbox.sandbox_id = "sbx-demo"
        db.query.return_value.filter.return_value.first.side_effect = [user, None, None, user_sandbox]
        sandbox_service = MagicMock()
        sandbox_service.get_cached.return_value = None
        sandbox_service.kill = AsyncMock(return_value=False)

        with patch("src.api.routes.admin.get_agent_pool"):
            with patch("src.api.routes.admin.SandboxSessionService", return_value=sandbox_service):
                with patch("src.api.routes.admin.delete_auth_user") as delete_mock:
                    resp = client.delete("/admin/users/demo")

        assert resp.status_code == 409
        assert "沙箱清理失败" in resp.json()["detail"]
        delete_mock.assert_not_called()

    def test_delete_user_rejects_active_run_lock(self):
        db = MagicMock()
        client = _make_client(admin_enabled=True, db=db)
        user = MagicMock()
        user.user_id = "demo"
        active_lock = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [user, active_lock]

        with patch("src.api.routes.admin.delete_auth_user") as delete_mock:
            resp = client.delete("/admin/users/demo")

        assert resp.status_code == 409
        assert "正在运行的任务" in resp.json()["detail"]
        delete_mock.assert_not_called()

    def test_delete_user_rejects_running_cron(self):
        db = MagicMock()
        client = _make_client(admin_enabled=True, db=db)
        user = MagicMock()
        user.user_id = "demo"
        running_cron = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [user, None, running_cron]

        with patch("src.api.routes.admin.delete_auth_user") as delete_mock:
            resp = client.delete("/admin/users/demo")

        assert resp.status_code == 409
        assert "正在运行的定时任务" in resp.json()["detail"]
        delete_mock.assert_not_called()

    def test_admin_cannot_delete_self(self):
        client = _make_client(admin_enabled=True)

        with patch("src.api.routes.admin.delete_auth_user") as delete_mock:
            resp = client.delete("/admin/users/admin")

        assert resp.status_code == 400
        assert "不能删除" in resp.json()["detail"]
        delete_mock.assert_not_called()

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
