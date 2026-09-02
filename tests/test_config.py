"""应用配置校验测试。"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.api import config
from src.api.config import Settings


def _load_settings_with(instance: Settings) -> Settings:
    config.get_settings.cache_clear()
    try:
        with patch.object(config, "Settings", return_value=instance):
            return config.get_settings()
    finally:
        config.get_settings.cache_clear()


def _production_settings(**overrides) -> Settings:
    values = {
        "debug": False,
        "auth_secret_key": "prod-auth-key-9d5ee835c53743edb2f27dd65c",
        "mcp_secret_key": "prod-mcp-key-1d77b27885914c26828b7dc01da",
        "simple_auth_users": "admin:a-strong-bootstrap-password",
    }
    values.update(overrides)
    return Settings(**values)


def test_sandbox_background_command_timeout_rejects_negative():
    """后台 bash 服务端 timeout 只允许 0 或正数。"""
    with pytest.raises(ValidationError, match="must be >= 0"):
        Settings(sandbox_background_command_timeout_seconds=-1)


def test_mcp_catalog_refresh_defaults_to_five_minutes():
    assert Settings().mcp_catalog_refresh_seconds == 300.0


def test_human_interactions_have_no_protocol_toggle():
    assert "agent_same_round_interactions" not in Settings.model_fields
    assert not hasattr(Settings(), "agent_same_round_interactions")


def test_mcp_catalog_cache_has_bounded_defaults():
    settings = Settings()
    assert settings.mcp_catalog_cache_max_users == 64
    assert settings.mcp_catalog_cache_max_bytes == 64 * 1024 * 1024
    assert settings.mcp_catalog_cache_idle_ttl_seconds == 900.0


def test_mcp_call_timeout_is_independent_and_enabled_by_default():
    settings = Settings(agent_tool_timeout=0)
    assert settings.agent_tool_timeout == 0
    assert settings.mcp_call_timeout_seconds == 300.0
    assert settings.mcp_connect_retry_attempts == 3
    assert settings.mcp_connect_retry_base_delay_seconds == 0.5


@pytest.mark.parametrize("value", [0, -1, 601, float("nan"), float("inf")])
def test_mcp_call_timeout_cannot_be_disabled_or_unbounded(value: float):
    with pytest.raises(ValidationError, match="must be > 0 and <= 600"):
        Settings(mcp_call_timeout_seconds=value)


@pytest.mark.parametrize("value", [0, -1, 11])
def test_mcp_connect_retry_attempts_are_bounded(value: int):
    with pytest.raises(ValidationError, match="must be > 0 and <= 10"):
        Settings(mcp_connect_retry_attempts=value)


@pytest.mark.parametrize("value", [-1, 31, float("nan"), float("inf")])
def test_mcp_connect_retry_delay_is_bounded(value: float):
    with pytest.raises(ValidationError, match="must be >= 0 and <= 30"):
        Settings(mcp_connect_retry_base_delay_seconds=value)


@pytest.mark.parametrize("value", [0, -1, 86401])
def test_mcp_catalog_refresh_rejects_unsafe_intervals(value: float):
    with pytest.raises(ValidationError, match="must be > 0 and <= 86400"):
        Settings(mcp_catalog_refresh_seconds=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mcp_catalog_cache_max_users", 0),
        ("mcp_catalog_cache_max_bytes", 0),
        ("mcp_catalog_cache_idle_ttl_seconds", 0),
    ],
)
def test_mcp_catalog_cache_rejects_nonpositive_limits(field: str, value):
    with pytest.raises(ValidationError, match="must be > 0"):
        Settings(**{field: value})


def test_mcp_resource_limit_defaults_fit_runtime_catalog_limit():
    settings = Settings()
    assert settings.mcp_personal_server_limit == 20
    assert settings.mcp_user_enabled_connection_limit == 20
    assert settings.mcp_required_official_server_limit == 10
    assert (
        settings.mcp_user_enabled_connection_limit
        + settings.mcp_required_official_server_limit
        <= settings.mcp_max_installations_per_user
    )


def test_mcp_per_user_discovery_concurrency_cannot_exceed_global():
    with pytest.raises(ValidationError, match="must be <="):
        Settings(
            mcp_max_concurrent_discoveries_per_user=5,
            mcp_max_concurrent_discoveries_global=4,
        )


def test_mcp_connection_budgets_cannot_exceed_runtime_installation_limit():
    with pytest.raises(ValidationError, match="must be <="):
        Settings(
            mcp_user_enabled_connection_limit=20,
            mcp_required_official_server_limit=10,
            mcp_max_installations_per_user=29,
        )


def test_tool_approval_lease_defaults_allow_multiple_heartbeats():
    settings = Settings()
    assert settings.tool_approval_execution_lease_seconds == 120.0
    assert settings.tool_approval_lease_heartbeat_seconds == 30.0
    assert settings.tool_approval_reconcile_interval_seconds == 30.0
    assert (
        settings.tool_approval_lease_heartbeat_seconds
        < settings.tool_approval_execution_lease_seconds
    )




def test_tool_approval_heartbeat_must_be_shorter_than_lease():
    with pytest.raises(ValidationError, match="must be <"):
        Settings(
            tool_approval_execution_lease_seconds=10,
            tool_approval_lease_heartbeat_seconds=10,
        )




def test_cron_claim_heartbeat_must_be_shorter_than_lease():
    with pytest.raises(ValidationError, match="cron_claim_heartbeat_seconds must be <"):
        Settings(
            cron_claim_lease_seconds=10,
            cron_claim_heartbeat_seconds=10,
        )


@pytest.mark.parametrize("value", [0, -1, 3601, float("nan"), float("inf")])
def test_cron_runtime_intervals_are_finite_and_bounded(value: float):
    with pytest.raises(ValidationError, match="must be > 0 and <= 3600"):
        Settings(cron_reconcile_interval_seconds=value)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("auth_secret_key", "", "AUTH_SECRET_KEY must be configured"),
        (
            "auth_secret_key",
            "replace-with-a-random-secret-string",
            "AUTH_SECRET_KEY must not use an example value",
        ),
        ("auth_secret_key", "too-short", "AUTH_SECRET_KEY must contain at least 32"),
        ("mcp_secret_key", "", "MCP_SECRET_KEY must be configured independently"),
        (
            "mcp_secret_key",
            "replace-with-an-independent-random-secret-string",
            "MCP_SECRET_KEY must not use an example value",
        ),
        ("mcp_secret_key", "too-short", "MCP_SECRET_KEY must contain at least 32"),
    ],
)
def test_production_fails_fast_for_unsafe_runtime_secrets(field, value, expected):
    with pytest.raises(RuntimeError, match=expected):
        _load_settings_with(_production_settings(**{field: value}))


@pytest.mark.parametrize(
    "simple_auth_users",
    [
        "demo:demo123",
        "replace-user:replace-with-a-strong-unique-password",
        "safe-user:safe-password,test:test123",
    ],
)
def test_production_rejects_public_bootstrap_credentials(simple_auth_users):
    with pytest.raises(RuntimeError, match="public example credentials"):
        _load_settings_with(
            _production_settings(simple_auth_users=simple_auth_users)
        )


def test_debug_missing_secrets_use_random_auth_and_explicit_mcp_derivation_warning(caplog):
    settings = _load_settings_with(
        Settings(
            debug=True,
            auth_secret_key="replace-with-a-random-secret-string",
            mcp_secret_key="replace-with-an-independent-random-secret-string",
        )
    )

    assert len(settings.auth_secret_key) >= 32
    assert settings.auth_secret_key != "replace-with-a-random-secret-string"
    assert settings.mcp_secret_key == ""
    assert "仅当前进程有效的随机密钥" in caplog.text
    assert "MCP 加密密钥将从当前进程的 AUTH_SECRET_KEY 派生" in caplog.text


def test_production_accepts_independent_long_runtime_secrets():
    settings = _load_settings_with(_production_settings())
    assert settings.auth_secret_key.startswith("prod-auth-key-")
    assert settings.mcp_secret_key.startswith("prod-mcp-key-")


def test_production_rejects_reusing_auth_secret_for_mcp_encryption():
    shared = "shared-production-secret-key-71c48208ed8f4b89"
    with pytest.raises(RuntimeError, match="must be different from AUTH_SECRET_KEY"):
        _load_settings_with(
            _production_settings(
                auth_secret_key=shared,
                mcp_secret_key=shared,
            )
        )


@pytest.mark.parametrize(
    "field",
    [
        "workspace_quota_bytes",
        "workspace_history_quota_bytes",
        "workspace_preview_cache_bytes",
        "workspace_max_file_bytes",
        "workspace_max_entries",
        "workspace_mutation_lease_seconds",
        "workspace_version_retention_count",
        "workspace_version_retention_days",
        "workspace_draft_base_retention_days",
        "workspace_draft_revision_retention_count",
        "workspace_history_gc_interval_seconds",
        "workspace_history_gc_batch_size",
    ],
)
def test_workspace_limits_must_be_positive(field):
    with pytest.raises(ValidationError, match="workspace limits and lease must be > 0"):
        Settings(**{field: 0})


def test_workspace_single_file_limit_cannot_exceed_quota():
    with pytest.raises(ValidationError, match="workspace_max_file_bytes must be <="):
        Settings(workspace_quota_bytes=10, workspace_max_file_bytes=11)
