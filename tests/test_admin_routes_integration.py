"""Admin routes integration tests with real database queries."""

from datetime import timedelta
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_current_admin_user
from src.api.models.auth_login_event import AuthLoginEvent
from src.api.models.auth_user import AuthUser
from src.api.models.database import Base, get_db
from src.api.models.llm_model import LLMModel, LLMModelSettings
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.model_permission import ModelPermissionGroup, ModelPermissionGroupModel
from src.api.models.round import Round
from src.api.models.sandbox_profile import SandboxProfile
from src.api.models.session import Session
from src.api.models.subagent_run import SubagentRun
from src.api.models.user_sandbox import UserSandbox
from src.api.models.user_sandbox_config import UserSandboxConfig
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


def test_sandbox_profile_patch_empty_api_key_keeps_existing_secret(admin_integration_client):
    client, SessionLocal = admin_integration_client

    create_resp = client.post("/admin/sandbox-profiles", json={
        "name": "profile-a",
        "domain": "10.0.0.1:8080",
        "api_key": "secret-a",
    })
    assert create_resp.status_code == 200
    profile_id = create_resp.json()["id"]
    assert create_resp.json()["api_key_set"] is True

    patch_resp = client.patch(
        f"/admin/sandbox-profiles/{profile_id}",
        json={"description": "keep key", "api_key": ""},
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["api_key_set"] is True
    assert patched["version"] == 1

    db = SessionLocal()
    try:
        profile = db.query(SandboxProfile).filter(SandboxProfile.id == profile_id).one()
        assert profile.api_key == "secret-a"
        assert profile.description == "keep key"
    finally:
        db.close()


def test_sandbox_profile_create_requires_api_key(admin_integration_client):
    client, _ = admin_integration_client

    response = client.post("/admin/sandbox-profiles", json={
        "name": "profile-no-key",
        "domain": "10.0.0.9:8080",
    })

    assert response.status_code == 422


def test_sandbox_profile_set_default_is_idempotent(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        db.add(SandboxProfile(
            id="default-idempotent-profile",
            name="default-idempotent-profile",
            domain="10.0.0.8:8080",
            api_key="secret-default",
            is_default=True,
            enabled=True,
        ))
        db.commit()
    finally:
        db.close()

    first = client.patch("/admin/sandbox-profiles/default-idempotent-profile/default")
    second = client.patch("/admin/sandbox-profiles/default-idempotent-profile/default")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["is_default"] is True

    db = SessionLocal()
    try:
        defaults = db.query(SandboxProfile).filter(SandboxProfile.is_default.is_(True)).all()
        assert [profile.id for profile in defaults] == ["default-idempotent-profile"]
    finally:
        db.close()


def test_default_sandbox_profile_assignment_is_normalized_to_null(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        db.add(AuthUser(
            user_id="default-normalized-user",
            username="default-normalized-user",
            auth_type="simple",
            enabled=True,
        ))
        db.add(SandboxProfile(
            id="default-normalized-profile",
            name="default-normalized-profile",
            domain="10.0.0.12:8080",
            api_key="secret-default-normalized",
            is_default=True,
            enabled=True,
        ))
        db.commit()
    finally:
        db.close()

    with patch(
        "src.api.routes.admin.get_agent_pool",
        side_effect=AssertionError("default profile id should normalize to no-op"),
    ), patch(
        "src.api.routes.admin.SandboxSessionService",
        side_effect=AssertionError("default profile id should not trigger sandbox cleanup"),
    ):
        response = client.patch(
            "/admin/users/default-normalized-user/sandbox-profile",
            json={"sandbox_profile_id": "default-normalized-profile"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sandbox_profile_id"] is None
    assert payload["sandbox_profile_name"] == "default-normalized-profile"
    assert payload["sandbox_profile_source"] == "default"

    db = SessionLocal()
    try:
        config = db.query(UserSandboxConfig).filter(UserSandboxConfig.user_id == "default-normalized-user").first()
        assert config is None or config.sandbox_profile_id is None
    finally:
        db.close()


def _add_test_llm_model(db, *, model_id: str, enabled: bool = True) -> None:
    db.add(LLMModel(
        model_id=model_id,
        display_name=model_id,
        provider="openai",
        api_base="https://api.example.com/v1",
        api_key="test-key",
        model_name=model_id,
        max_tokens=1024,
        # Must leave the same real provider-input budget enforced by admin
        # create/update validation: context - output - 3000 >= 8192.
        context_window=16384,
        enabled=enabled,
    ))


def test_create_non_openai_model_normalizes_thinking_wire_format(
    admin_integration_client,
):
    client, SessionLocal = admin_integration_client

    response = client.post("/admin/models", json={
        "model_id": "anthropic-wire-normalized",
        "display_name": "Anthropic Wire Normalized",
        "provider": "anthropic",
        "api_base": "https://api.example.com",
        "api_key": "test-key",
        "model_name": "anthropic-wire-normalized",
        "max_tokens": 1024,
        "context_window": 16384,
        "thinking_wire_format": "enable_thinking",
    })

    assert response.status_code == 200
    assert response.json()["thinking_wire_format"] == "none"

    db = SessionLocal()
    try:
        model = db.query(LLMModel).filter(
            LLMModel.model_id == "anthropic-wire-normalized"
        ).one()
        assert model.thinking_wire_format == "none"
    finally:
        db.close()


def test_admin_model_writes_persist_normalized_reasoning_levels(
    admin_integration_client,
):
    client, SessionLocal = admin_integration_client

    create_response = client.post("/admin/models", json={
        "model_id": "normalized-reasoning-levels",
        "display_name": "Normalized Reasoning Levels",
        "provider": "openai",
        "api_base": "https://api.example.com/v1",
        "api_key": "test-key",
        "model_name": "normalized-reasoning-levels",
        "max_tokens": 1024,
        "context_window": 16384,
        "supported_reasoning_efforts": [" high ", "max", "high"],
    })

    assert create_response.status_code == 200
    assert create_response.json()["supported_reasoning_efforts"] == ["high", "max"]

    db = SessionLocal()
    try:
        created = db.query(LLMModel).filter(
            LLMModel.model_id == "normalized-reasoning-levels"
        ).one()
        assert json.loads(created.supported_reasoning_efforts_json) == ["high", "max"]
    finally:
        db.close()

    patch_response = client.patch(
        "/admin/models/normalized-reasoning-levels",
        json={"supported_reasoning_efforts": ["off", "on", "off"]},
    )

    assert patch_response.status_code == 200
    assert patch_response.json()["supported_reasoning_efforts"] == ["off", "on"]

    db = SessionLocal()
    try:
        model = db.query(LLMModel).filter(
            LLMModel.model_id == "normalized-reasoning-levels"
        ).one()
        assert json.loads(model.supported_reasoning_efforts_json) == ["off", "on"]
    finally:
        db.close()


def test_unrelated_admin_patch_reconciles_legacy_duplicate_reasoning_levels(
    admin_integration_client,
):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        _add_test_llm_model(db, model_id="legacy-duplicate-reasoning-levels")
        db.flush()
        model = db.query(LLMModel).filter(
            LLMModel.model_id == "legacy-duplicate-reasoning-levels"
        ).one()
        model.supported_reasoning_efforts_json = json.dumps(["high", "max", "high"])
        db.commit()
    finally:
        db.close()

    catalog_response = client.get("/admin/models")
    assert catalog_response.status_code == 200
    legacy_payload = next(
        item
        for item in catalog_response.json()["models"]
        if item["id"] == "legacy-duplicate-reasoning-levels"
    )
    assert legacy_payload["supported_reasoning_efforts"] == ["high", "max"]

    response = client.patch(
        "/admin/models/legacy-duplicate-reasoning-levels",
        json={"display_name": "Legacy Levels Reconciled"},
    )

    assert response.status_code == 200
    assert response.json()["supported_reasoning_efforts"] == ["high", "max"]

    db = SessionLocal()
    try:
        stored = db.query(LLMModel).filter(
            LLMModel.model_id == "legacy-duplicate-reasoning-levels"
        ).one()
        assert json.loads(stored.supported_reasoning_efforts_json) == ["high", "max"]
    finally:
        db.close()


def test_model_permission_group_rejects_disabled_models(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        group = ModelPermissionGroup(id="biz-disabled-reject", name="业务停用模型校验", created_by="admin")
        db.add(group)
        _add_test_llm_model(db, model_id="enabled-model", enabled=True)
        _add_test_llm_model(db, model_id="disabled-model", enabled=False)
        db.commit()
    finally:
        db.close()

    response = client.put(
        "/admin/model-permission-groups/biz-disabled-reject/models",
        json={"model_ids": ["enabled-model", "disabled-model"]},
    )

    assert response.status_code == 400
    assert "停用模型不能加入权限包" in response.json()["detail"]

    db = SessionLocal()
    try:
        rows = db.query(ModelPermissionGroupModel).filter(
            ModelPermissionGroupModel.group_id == "biz-disabled-reject"
        ).all()
        assert rows == []
    finally:
        db.close()


def test_disabling_model_removes_it_from_permission_groups(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        group = ModelPermissionGroup(id="biz-disable-cleanup", name="业务停用清理", created_by="admin")
        db.add(group)
        _add_test_llm_model(db, model_id="cleanup-model", enabled=True)
        db.flush()
        db.add(ModelPermissionGroupModel(
            group_id="biz-disable-cleanup",
            model_id="cleanup-model",
            created_by="admin",
        ))
        db.commit()
    finally:
        db.close()

    response = client.patch("/admin/models/cleanup-model", json={"enabled": False})

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["group_names"] == []

    db = SessionLocal()
    try:
        rows = db.query(ModelPermissionGroupModel).filter(
            ModelPermissionGroupModel.model_id == "cleanup-model"
        ).all()
        assert rows == []
    finally:
        db.close()


def test_delete_model_removes_catalog_entry_and_permission_bindings(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        group = ModelPermissionGroup(id="biz-delete-cleanup", name="业务删除清理", created_by="admin")
        db.add(group)
        _add_test_llm_model(db, model_id="delete-model", enabled=True)
        db.flush()
        db.add(ModelPermissionGroupModel(
            group_id="biz-delete-cleanup",
            model_id="delete-model",
            created_by="admin",
        ))
        db.commit()
    finally:
        db.close()

    response = client.delete("/admin/models/delete-model")

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_id"] == "delete-model"
    assert payload["deleted"] is True
    assert payload["replacement_model_id"] is None
    assert payload["sessions_reassigned"] == 0
    assert payload["defaults_reassigned"] == []

    db = SessionLocal()
    try:
        assert db.query(LLMModel).filter(LLMModel.model_id == "delete-model").first() is None
        rows = db.query(ModelPermissionGroupModel).filter(
            ModelPermissionGroupModel.model_id == "delete-model"
        ).all()
        assert rows == []
    finally:
        db.close()


def test_delete_model_rejects_default_model(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        _add_test_llm_model(db, model_id="default-delete-model", enabled=True)
        db.add(LLMModelSettings(
            id=1,
            default_model_id="default-delete-model",
            cron_default_model_id="default-delete-model",
            subagent_default_model_id="default-delete-model",
        ))
        db.commit()
    finally:
        db.close()

    response = client.delete("/admin/models/default-delete-model")

    assert response.status_code == 400
    assert "切换默认模型" in response.json()["detail"]

    db = SessionLocal()
    try:
        assert db.query(LLMModel).filter(LLMModel.model_id == "default-delete-model").first() is not None
    finally:
        db.close()


def test_delete_model_rejects_session_references(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        _add_test_llm_model(db, model_id="used-delete-model", enabled=True)
        db.add(Session(
            id="used-delete-session",
            user_id="demo-user",
            title="uses model",
            status="active",
            model_id="used-delete-model",
        ))
        db.commit()
    finally:
        db.close()

    response = client.delete("/admin/models/used-delete-model")

    assert response.status_code == 409
    assert "会话使用" in response.json()["detail"]

    db = SessionLocal()
    try:
        assert db.query(LLMModel).filter(LLMModel.model_id == "used-delete-model").first() is not None
    finally:
        db.close()


def test_delete_model_can_replace_session_defaults_and_permissions(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        group = ModelPermissionGroup(id="biz-delete-replace", name="业务删除替换", created_by="admin")
        db.add(group)
        _add_test_llm_model(db, model_id="old-delete-model", enabled=True)
        _add_test_llm_model(db, model_id="replacement-delete-model", enabled=True)
        db.flush()
        db.add(ModelPermissionGroupModel(
            group_id="biz-delete-replace",
            model_id="old-delete-model",
            created_by="admin",
        ))
        db.add(LLMModelSettings(
            id=1,
            default_model_id="old-delete-model",
            cron_default_model_id="old-delete-model",
            subagent_default_model_id="old-delete-model",
        ))
        db.add(Session(
            id="replace-delete-session",
            user_id="demo-user",
            title="uses old model",
            status="active",
            model_id="old-delete-model",
        ))
        db.commit()
    finally:
        db.close()

    response = client.delete(
        "/admin/models/old-delete-model",
        params={"replacement_model_id": "replacement-delete-model"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_id"] == "old-delete-model"
    assert payload["replacement_model_id"] == "replacement-delete-model"
    assert payload["sessions_reassigned"] == 1
    assert set(payload["defaults_reassigned"]) == {
        "default_model_id",
        "cron_default_model_id",
        "subagent_default_model_id",
    }

    db = SessionLocal()
    try:
        assert db.query(LLMModel).filter(LLMModel.model_id == "old-delete-model").first() is None
        session = db.query(Session).filter(Session.id == "replace-delete-session").one()
        assert session.model_id == "replacement-delete-model"
        settings = db.query(LLMModelSettings).filter(LLMModelSettings.id == 1).one()
        assert settings.default_model_id == "replacement-delete-model"
        assert settings.cron_default_model_id == "replacement-delete-model"
        assert settings.subagent_default_model_id == "replacement-delete-model"
        assert db.query(ModelPermissionGroupModel).filter(
            ModelPermissionGroupModel.group_id == "biz-delete-replace",
            ModelPermissionGroupModel.model_id == "old-delete-model",
        ).first() is None
        assert db.query(ModelPermissionGroupModel).filter(
            ModelPermissionGroupModel.group_id == "biz-delete-replace",
            ModelPermissionGroupModel.model_id == "replacement-delete-model",
        ).first() is not None
    finally:
        db.close()


def test_delete_model_rejects_disabled_replacement(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        _add_test_llm_model(db, model_id="old-disabled-replacement-model", enabled=True)
        _add_test_llm_model(db, model_id="disabled-replacement-model", enabled=False)
        db.add(Session(
            id="disabled-replacement-session",
            user_id="demo-user",
            title="uses old model",
            status="active",
            model_id="old-disabled-replacement-model",
        ))
        db.commit()
    finally:
        db.close()

    response = client.delete(
        "/admin/models/old-disabled-replacement-model",
        params={"replacement_model_id": "disabled-replacement-model"},
    )

    assert response.status_code == 400
    assert "启用状态" in response.json()["detail"]

    db = SessionLocal()
    try:
        assert db.query(LLMModel).filter(LLMModel.model_id == "old-disabled-replacement-model").first() is not None
    finally:
        db.close()


def test_create_user_validates_sandbox_profile_before_persisting_user(admin_integration_client):
    client, SessionLocal = admin_integration_client

    missing_resp = client.post("/admin/users/simple", json={
        "username": "profile-missing-user",
        "password": "pw",
        "sandbox_profile_id": "missing-profile",
    })
    assert missing_resp.status_code == 404

    db = SessionLocal()
    try:
        assert db.query(AuthUser).filter(AuthUser.user_id == "profile-missing-user").first() is None
        db.add(SandboxProfile(
            id="disabled-profile",
            name="disabled-profile",
            domain="10.0.0.2:8080",
            api_key="secret-disabled",
            enabled=False,
        ))
        db.commit()
    finally:
        db.close()

    disabled_resp = client.post("/admin/users/simple", json={
        "username": "profile-disabled-user",
        "password": "pw",
        "sandbox_profile_id": "disabled-profile",
    })
    assert disabled_resp.status_code == 400

    db = SessionLocal()
    try:
        assert db.query(AuthUser).filter(AuthUser.user_id == "profile-disabled-user").first() is None
    finally:
        db.close()


def test_sandbox_profile_payload_is_connection_only(admin_integration_client):
    client, SessionLocal = admin_integration_client

    response = client.post("/admin/sandbox-profiles", json={
        "name": "profile-connection-only",
        "domain": "10.0.0.3:8080",
        "api_key": "secret-connection",
        "protocol": "http",
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_key_set"] is True
    assert "image" not in payload
    assert "cpu_limit" not in payload
    assert "memory_limit" not in payload
    assert "storage_root" not in payload

    db = SessionLocal()
    try:
        profile = db.query(SandboxProfile).filter(SandboxProfile.id == payload["id"]).one()
        assert profile.domain == "10.0.0.3:8080"
    finally:
        db.close()


def test_user_sandbox_profile_patch_same_active_profile_is_noop(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        db.add(AuthUser(
            user_id="sandbox-noop-user",
            username="sandbox-noop-user",
            auth_type="simple",
            enabled=True,
        ))
        db.add(SandboxProfile(
            id="sandbox-noop-profile",
            name="sandbox-noop-profile",
            domain="10.0.0.4:8080",
            api_key="secret-noop",
            enabled=True,
            version=3,
        ))
        db.add(UserSandboxConfig(
            id="sandbox-noop-config",
            user_id="sandbox-noop-user",
            sandbox_profile_id="sandbox-noop-profile",
            updated_by="admin",
        ))
        db.add(UserSandbox(
            id="sandbox-noop-binding",
            user_id="sandbox-noop-user",
            sandbox_id="sbx-noop",
            active_profile_id="sandbox-noop-profile",
            active_profile_version=3,
            status="active",
        ))
        db.commit()
    finally:
        db.close()

    with patch("src.api.routes.admin.get_agent_pool") as get_agent_pool_mock, \
         patch("src.api.routes.admin.SandboxSessionService") as sandbox_service_cls:
        response = client.patch(
            "/admin/users/sandbox-noop-user/sandbox-profile",
            json={"sandbox_profile_id": "sandbox-noop-profile"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sandbox_profile_id"] == "sandbox-noop-profile"
    assert payload["sandbox_needs_recreate"] is False
    get_agent_pool_mock.assert_not_called()
    sandbox_service_cls.assert_not_called()

    db = SessionLocal()
    try:
        user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == "sandbox-noop-user").one()
        assert user_sandbox.sandbox_id == "sbx-noop"
        assert user_sandbox.active_profile_id == "sandbox-noop-profile"
        assert user_sandbox.active_profile_version == 3
    finally:
        db.close()


def test_users_payload_exposes_missing_explicit_sandbox_profile(admin_integration_client):
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        db.add(AuthUser(
            user_id="sandbox-missing-profile-user",
            username="sandbox-missing-profile-user",
            auth_type="simple",
            enabled=True,
        ))
        db.add(UserSandboxConfig(
            id="sandbox-missing-profile-config",
            user_id="sandbox-missing-profile-user",
            sandbox_profile_id="deleted-profile",
            updated_by="admin",
        ))
        db.commit()
    finally:
        db.close()

    response = client.get("/admin/users")

    assert response.status_code == 200
    user = next(item for item in response.json()["users"] if item["user_id"] == "sandbox-missing-profile-user")
    assert user["sandbox_profile_id"] == "deleted-profile"
    assert user["sandbox_profile_name"] is None
    assert user["sandbox_profile_source"] == "missing"
    assert user["sandbox_profile_error"] == "用户绑定的沙箱后端不存在"


def test_admin_read_paths_do_not_bootstrap_default_sandbox_profile(admin_integration_client):
    """Users + sandbox profile list are loaded concurrently by the frontend.

    These read paths must not take the default-profile advisory bootstrap lock,
    otherwise one request can hold the lock while another blocks the async server
    event loop waiting for it.
    """
    client, SessionLocal = admin_integration_client

    db = SessionLocal()
    try:
        db.add(AuthUser(
            user_id="admin-read-user",
            username="admin-read-user",
            auth_type="simple",
            enabled=True,
        ))
        db.add(SandboxProfile(
            id="admin-read-default-profile",
            name="admin-read-default-profile",
            domain="10.0.0.11:8080",
            api_key="secret-read",
            is_default=True,
            enabled=True,
        ))
        db.commit()
    finally:
        db.close()

    with patch(
        "src.api.routes.admin.ensure_default_sandbox_profile",
        side_effect=AssertionError("read path should not bootstrap default profile"),
    ), patch(
        "src.api.services.sandbox_profile_service._lock_default_profile_bootstrap",
        side_effect=AssertionError("read path should not take advisory bootstrap lock"),
    ):
        profiles_resp = client.get("/admin/sandbox-profiles")
        users_resp = client.get("/admin/users")

    assert profiles_resp.status_code == 200
    assert users_resp.status_code == 200
    user = next(item for item in users_resp.json()["users"] if item["user_id"] == "admin-read-user")
    assert user["sandbox_profile_source"] == "default"


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
    assert [item["total_duration_s"] for item in page1_data["sessions"]] == [5.0, 5.0]
    assert all(item["rounds"] == [] for item in page1_data["sessions"])
    assert all(item["rounds_loaded"] is False for item in page1_data["sessions"])

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
    assert failed_data["sessions"][0]["total_duration_s"] == 5.0

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
    assert data["sessions"][0]["rounds"] == []

    session_rounds = client.get("/admin/sessions/s-heavy/rounds", params={"status": "all"})
    assert session_rounds.status_code == 200
    step = session_rounds.json()["rounds"][0]["steps"][0]
    assert step["request_messages"] == ""
    assert step["response_content"] == ""
    assert step["response_tool_calls"] == ""

    detail = client.get(f"/admin/llm-call-records/{step['llm_record_id']}")
    assert detail.status_code == 200
    detail_data = detail.json()
    assert detail_data["request_messages"] == heavy_request
    assert detail_data["response_content"] == heavy_response


def test_session_round_steps_are_ordered_by_creation_time(admin_integration_client):
    client, SessionLocal = admin_integration_client

    now = now_naive()
    db = SessionLocal()
    try:
        _insert_round_with_step(
            db,
            session_id="s-step-order",
            user_id="admin",
            title="StepOrder",
            round_id="r-step-order",
            status="completed",
            created_at=now,
            user_message="inspect step order",
            final_response="ok",
            request_messages="[]",
            response_content="normal step one",
        )
        db.flush()
        first = db.query(LLMCallRecord).filter(
            LLMCallRecord.round_id == "r-step-order"
        ).one()
        first.created_at = now + timedelta(seconds=2)
        db.add_all([
            LLMCallRecord(
                session_id="s-step-order",
                round_id="r-step-order",
                step_index=-1,
                call_kind="compaction",
                request_messages="[]",
                request_tools="[]",
                response_content="first compaction",
                created_at=now + timedelta(seconds=1),
            ),
            LLMCallRecord(
                session_id="s-step-order",
                round_id="r-step-order",
                step_index=-2,
                call_kind="compaction",
                request_messages="[]",
                request_tools="[]",
                response_content="second compaction",
                created_at=now + timedelta(seconds=3),
            ),
            LLMCallRecord(
                session_id="s-step-order",
                round_id="r-step-order",
                step_index=2,
                call_kind="agent_step",
                request_messages="[]",
                request_tools="[]",
                response_content="normal step two",
                created_at=now + timedelta(seconds=4),
            ),
        ])
        db.commit()
    finally:
        db.close()

    response = client.get("/admin/sessions/s-step-order/rounds", params={"status": "all"})

    assert response.status_code == 200
    steps = response.json()["rounds"][0]["steps"]
    assert [(step["step_index"], step["call_kind"]) for step in steps] == [
        (-1, "compaction"),
        (1, "agent_step"),
        (-2, "compaction"),
        (2, "agent_step"),
    ]


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
    assert response.json()["sessions"][0]["rounds"] == []

    response = client.get("/admin/sessions/s-subagent/rounds", params={"status": "all"})
    assert response.status_code == 200
    rounds = response.json()["rounds"]
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
    assert session["rounds"] == []

    response = client.get("/admin/sessions/s-resumed/rounds", params={"status": "all"})
    assert response.status_code == 200
    assert response.json()["rounds"][0]["status"] == "resumed"


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
        pool_mock.return_value.invalidate_user_async = AsyncMock(return_value=0)
        with patch("src.api.routes.admin.SandboxSessionService", return_value=sandbox_service):
            resp = client.delete("/admin/users/demo")

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "demo", "deleted": True}
    pool_mock.return_value.invalidate_user_async.assert_awaited_once_with(
        "demo",
        preserve_running=False,
    )
    sandbox_service.kill.assert_not_called()
    verify_db = SessionLocal()
    try:
        assert verify_db.query(AuthUser).filter(AuthUser.user_id == "demo").count() == 0
        assert verify_db.query(UserRunLock).filter(UserRunLock.user_id == "demo").count() == 0
    finally:
        verify_db.close()
