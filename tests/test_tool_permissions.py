"""Tool permission policy and Agent approval boundary tests."""

import hashlib

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.agent.agent import Agent
from src.agent.schema import FunctionCall, LLMResponse, Message, ToolCall
from src.agent.schema.agui_events import EventType
from src.agent.tools.base import ToolExposure, ToolRef as AgentToolRef, ToolResult
from src.agent.tools.mcp_tool import McpRemoteTool
from src.api.models.database import Base
from src.api.models.auth_user import AuthUser
from src.api.models.database import get_db
from src.api.models.mcp import McpInstallation, McpServer, McpToolSnapshot
from src.api.models.tool_permission import (
    ToolApprovalRequest,
    ToolPermissionAudit,
    ToolPermissionRule,
)
from src.api.deps import get_current_admin_user, get_current_user
from src.api.routes import admin_permissions, permissions
from src.api.services.agent_service import AgentService
from src.api.services.mcp_runtime import (
    EffectiveMcpInstallation,
    McpToolSnapshot as RuntimeMcpToolSnapshot,
)
from src.api.services.secret_crypto import decrypt_secret
from src.api.services.tool_permission_service import (
    APPROVAL_EXECUTION_FAILED_ERROR,
    APPROVAL_OUTCOME_UNKNOWN_ERROR,
    RULE_CONDITIONS_VERSION,
    ToolPermissionCheck,
    ToolRef,
    _acquire_user_tool_selection_lock,
    _tool_selection_advisory_lock_key,
    claim_approval_request,
    clear_user_tool_selection,
    create_approval_request,
    create_permission_rule,
    evaluate_tool_permission,
    evaluate_tool_permissions,
    finish_approval_request,
    record_permission_audit,
    reconcile_expired_approval_leases,
    renew_approval_execution_lease,
    replace_user_tool_selection,
    replace_user_tool_selections,
)
from src.api.utils.timezone import now_naive
from tests.helpers import MockLLMClient, MockTool
from tests.db_safety import (
    build_pytest_pg_engine,
    create_all_for_test_engine,
    reset_all_tables,
)


_PROJECT_ROOT = Path(__file__).parent.parent


class _ApprovalMcpTool(MockTool):
    """Small MCP-shaped tool used to exercise durable approval resume checks."""

    def __init__(
        self,
        name: str = "mcp__server__RemoteTool",
        *,
        schema_hash: str | None = "schema-v1",
        connection_fingerprint: str | None = "connection-v1",
        exposure: ToolExposure = ToolExposure.DIRECT,
    ) -> None:
        super().__init__(name)
        self._schema_hash = schema_hash
        self._connection_fingerprint = connection_fingerprint
        self.live_connection_fingerprint = connection_fingerprint
        self.exposure = exposure

    @property
    def tool_ref(self) -> AgentToolRef:
        return AgentToolRef(
            provider="mcp",
            name="RemoteTool",
            server_id="server-1",
            installation_id="installation-1",
        )

    @property
    def schema_hash(self) -> str | None:
        return self._schema_hash

    @property
    def connection_fingerprint(self) -> str | None:
        return self._connection_fingerprint

    def current_connection_fingerprint(self) -> str | None:
        return self.live_connection_fingerprint


class _BlockingApprovalTool(MockTool):
    def __init__(self) -> None:
        super().__init__("blocking_approved_tool")
        import asyncio

        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, **kwargs) -> ToolResult:
        self.execute_count += 1
        self.last_args = kwargs
        self.started.set()
        await self.release.wait()
        return ToolResult(success=True, content="completed")


class _UncertainApprovalTool(MockTool):
    async def execute(self, **kwargs) -> ToolResult:
        self.execute_count += 1
        self.last_args = kwargs
        return ToolResult(
            success=False,
            error="remote result was lost; do not retry",
            outcome_uncertain=True,
        )


@pytest.fixture()
def permission_db():
    """Use a real transactional store so matching and claim semantics are tested."""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def permission_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    with session_factory() as db:
        db.add_all([
            AuthUser(user_id="admin", username="admin", enabled=True, is_admin=True),
            AuthUser(user_id="alice", username="alice", enabled=True),
        ])
        server = McpServer(
            id="server-case",
            source="official",
            name="Case Tools",
            url="https://93.184.216.34/mcp",
            status="published",
            auth_type="none",
        )
        installation = McpInstallation(
            id="installation-case",
            server_id=server.id,
            user_id="alice",
            enabled=True,
        )
        db.add_all([server, installation, McpToolSnapshot(
            installation_id=installation.id,
            tool_name="CaseSensitiveTool",
            title="Case Sensitive Tool",
            description="Preserve the remote name exactly",
            input_schema_json="{}",
            schema_hash="schema-case",
        )])
        db.commit()

    app = FastAPI()
    app.include_router(permissions.router, prefix="/permissions")
    app.include_router(admin_permissions.router, prefix="/admin/tool-permissions")

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: "alice"
    app.dependency_overrides[get_current_admin_user] = lambda: "admin"
    with TestClient(app) as client:
        client.SessionLocal = session_factory  # type: ignore[attr-defined]
        yield client
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _rule(
    db,
    *,
    scope_type: str,
    scope_id: str | None,
    ref: ToolRef,
    effect: str,
    priority: int = 0,
    managed: bool = False,
):
    return create_permission_rule(
        db,
        scope_type=scope_type,
        scope_id=scope_id,
        ref=ref,
        effect=effect,
        priority=priority,
        managed=managed,
        created_by="admin" if managed else "alice",
    )


def _decision(
    db,
    ref: ToolRef,
    *,
    session_id: str = "session-a",
    schema_hash: str | None = None,
    connection_fingerprint: str | None = None,
):
    return evaluate_tool_permission(
        db,
        user_id="alice",
        session_id=session_id,
        ref=ref,
        schema_hash=schema_hash,
        connection_fingerprint=connection_fingerprint,
    )


def test_selection_lock_uses_stable_exact_tool_identity():
    builtin = ToolRef(provider="builtin", tool_name="read_file")
    mcp = ToolRef(provider="mcp", server_id="server-1", tool_name="read_file")

    key = _tool_selection_advisory_lock_key("alice", builtin)

    assert key == _tool_selection_advisory_lock_key("alice", builtin)
    assert -(2**63) <= key < 2**63
    assert key != _tool_selection_advisory_lock_key("bob", builtin)
    assert key != _tool_selection_advisory_lock_key("alice", mcp)


def test_selection_lock_emits_postgresql_transaction_advisory_lock():
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql")
    )
    ref = ToolRef(provider="mcp", server_id="server-1", tool_name="search")

    _acquire_user_tool_selection_lock(db, user_id="alice", ref=ref)

    statement, parameters = db.execute.call_args.args
    assert str(statement) == "SELECT pg_advisory_xact_lock(:lock_key)"
    assert parameters == {
        "lock_key": _tool_selection_advisory_lock_key("alice", ref)
    }


def test_selection_lock_skips_postgresql_sql_on_sqlite():
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="sqlite")
    )

    _acquire_user_tool_selection_lock(
        db,
        user_id="alice",
        ref=ToolRef(provider="builtin", tool_name="read_file"),
    )

    db.execute.assert_not_called()


def test_selection_lock_serializes_real_postgresql_transactions():
    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    create_all_for_test_engine(engine, Base.metadata)
    reset_all_tables(engine, Base.metadata)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    ref = ToolRef(provider="builtin", tool_name="read_file")
    contender_started = Event()
    contender_acquired = Event()

    def acquire_in_contender() -> None:
        with session_factory() as contender:
            contender_started.set()
            _acquire_user_tool_selection_lock(
                contender,
                user_id="alice",
                ref=ref,
            )
            contender_acquired.set()
            contender.commit()

    try:
        with session_factory() as owner:
            _acquire_user_tool_selection_lock(owner, user_id="alice", ref=ref)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(acquire_in_contender)
                try:
                    assert contender_started.wait(timeout=2)
                    assert contender_acquired.wait(timeout=0.2) is False
                finally:
                    # Always release the transaction lock so a failed timing
                    # assertion cannot strand the contender thread.
                    owner.commit()
                assert contender_acquired.wait(timeout=5)
                future.result(timeout=5)
    finally:
        reset_all_tables(engine, Base.metadata)
        engine.dispose()


def test_selection_acquires_lock_before_delete_and_insert():
    events: list[str] = []
    db = MagicMock()
    db.query.return_value.filter.return_value.delete.side_effect = (
        lambda **_kwargs: events.append("delete")
    )

    def record_lock(*_args, **_kwargs):
        events.append("lock")

    def record_insert(*_args, **_kwargs):
        events.append("insert")
        return MagicMock()

    with (
        patch(
            "src.api.services.tool_permission_service."
            "_acquire_user_tool_selection_lock",
            side_effect=record_lock,
        ),
        patch(
            "src.api.services.tool_permission_service.create_permission_rule",
            side_effect=record_insert,
        ),
    ):
        replace_user_tool_selection(
            db,
            user_id="alice",
            ref=ToolRef(provider="builtin", tool_name="read_file"),
            effect="allow",
            commit=False,
        )

    assert events == ["lock", "delete", "insert"]


def test_selection_replaces_conditional_grant_with_single_unconditional_rule(permission_db):
    ref = ToolRef(provider="mcp", server_id="server-1", tool_name="search")
    create_permission_rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ref,
        effect="allow",
        created_by="alice",
        conditions={
            "version": RULE_CONDITIONS_VERSION,
            "schema_hash": "schema-v1",
            "connection_fingerprint": "conn-v1",
        },
    )

    rule = replace_user_tool_selection(
        permission_db,
        user_id="alice",
        ref=ref,
        effect="deny",
    )

    remaining = (
        permission_db.query(ToolPermissionRule)
        .filter(
            ToolPermissionRule.scope_type == "user",
            ToolPermissionRule.scope_id == "alice",
            ToolPermissionRule.provider == "mcp",
            ToolPermissionRule.server_id == "server-1",
            ToolPermissionRule.tool_name == "search",
        )
        .all()
    )
    assert [row.id for row in remaining] == [rule.id]
    assert rule.effect == "deny"
    assert rule.conditions_json is None
    assert rule.enabled is True
    assert rule.expires_at is None
    assert rule.scope_type == "user"
    assert rule.managed is False


def test_selection_leaves_other_scopes_tools_and_users_untouched(permission_db):
    target = ToolRef(provider="builtin", tool_name="read_file")
    platform_rule = _rule(
        permission_db,
        scope_type="platform",
        scope_id=None,
        ref=target,
        effect="deny",
        managed=True,
    )
    session_rule = _rule(
        permission_db,
        scope_type="session",
        scope_id="session-a",
        ref=target,
        effect="allow",
    )
    other_user_rule = _rule(
        permission_db,
        scope_type="user",
        scope_id="bob",
        ref=target,
        effect="allow",
    )
    wildcard_rule = _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ToolRef(provider="builtin", tool_name="*"),
        effect="deny",
    )
    other_tool_rule = _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ToolRef(provider="builtin", tool_name="write_file"),
        effect="deny",
    )
    replaced = _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=target,
        effect="allow",
    )
    permission_db.commit()
    replaced_id = replaced.id

    new_rule = replace_user_tool_selection(
        permission_db,
        user_id="alice",
        ref=target,
        effect="ask",
    )

    surviving = {row.id for row in permission_db.query(ToolPermissionRule).all()}
    assert platform_rule.id in surviving
    assert session_rule.id in surviving
    assert other_user_rule.id in surviving
    assert wildcard_rule.id in surviving
    assert other_tool_rule.id in surviving
    assert replaced_id not in surviving
    assert new_rule.id in surviving
    assert new_rule.effect == "ask"


def test_selection_rolls_back_when_creation_fails(permission_db):
    ref = ToolRef(provider="builtin", tool_name="read_file")
    grant = create_permission_rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ref,
        effect="allow",
        created_by="alice",
        conditions={"version": RULE_CONDITIONS_VERSION, "schema_hash": "schema-v1"},
    )
    permission_db.commit()

    with patch(
        "src.api.services.tool_permission_service.create_permission_rule",
        side_effect=RuntimeError("insert failed"),
    ):
        with pytest.raises(RuntimeError, match="insert failed"):
            replace_user_tool_selection(
                permission_db,
                user_id="alice",
                ref=ref,
                effect="deny",
            )
    permission_db.rollback()

    rows = (
        permission_db.query(ToolPermissionRule)
        .filter(ToolPermissionRule.scope_id == "alice")
        .all()
    )
    assert [row.id for row in rows] == [grant.id]
    assert rows[0].conditions_json is not None


def test_selection_endpoint_replaces_rules_for_builtin(permission_client):
    response = permission_client.put(
        "/permissions/rules/selection",
        json={"provider": "builtin", "tool_name": "read_file", "effect": "deny"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effect"] == "deny"
    assert body["scope_type"] == "user"
    assert body["scope_id"] == "alice"
    assert body["conditions"] is None
    assert body["managed"] is False
    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        rows = (
            db.query(ToolPermissionRule)
            .filter(
                ToolPermissionRule.scope_type == "user",
                ToolPermissionRule.scope_id == "alice",
                ToolPermissionRule.tool_name == "read_file",
            )
            .all()
        )
        assert len(rows) == 1


def test_selection_endpoint_accepts_accessible_mcp(permission_client):
    response = permission_client.put(
        "/permissions/rules/selection",
        json={
            "provider": "mcp",
            "server_id": "server-case",
            "tool_name": "CaseSensitiveTool",
            "effect": "allow",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["tool_ref"] == "mcp:server-case:CaseSensitiveTool"


def test_patch_conditional_allow_to_deny_clears_version_binding(permission_client):
    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        rule = create_permission_rule(
            db,
            scope_type="user",
            scope_id="alice",
            ref=ToolRef(
                provider="mcp",
                server_id="server-case",
                tool_name="CaseSensitiveTool",
            ),
            effect="allow",
            created_by="alice",
            conditions={
                "version": RULE_CONDITIONS_VERSION,
                "schema_hash": "schema-case",
                "connection_fingerprint": "connection-v1",
            },
        )
        rule_id = rule.id

    response = permission_client.patch(
        f"/permissions/rules/{rule_id}",
        json={"effect": "deny"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["effect"] == "deny"
    assert response.json()["conditions"] is None

    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        persisted = db.query(ToolPermissionRule).filter_by(id=rule_id).one()
        assert persisted.conditions_json is None
        decision = evaluate_tool_permission(
            db,
            user_id="alice",
            session_id=None,
            ref=ToolRef(
                provider="mcp",
                server_id="server-case",
                tool_name="CaseSensitiveTool",
            ),
            default_effect="ask",
            schema_hash="schema-after-rediscovery",
            connection_fingerprint="connection-v2",
        )
        assert decision.effect == "deny"


def test_permission_patch_rejects_null_for_required_columns(permission_client):
    created = permission_client.post(
        "/permissions/rules",
        json={"provider": "builtin", "tool_name": "read_file", "effect": "allow"},
    )
    assert created.status_code == 200, created.text

    for field_name in ("effect", "priority", "enabled"):
        response = permission_client.patch(
            f"/permissions/rules/{created.json()['id']}",
            json={field_name: None},
        )
        assert response.status_code == 422, (field_name, response.text)


def test_admin_permission_patch_rejects_null_for_required_columns(permission_client):
    created = permission_client.post(
        "/admin/tool-permissions",
        json={"provider": "builtin", "tool_name": "read_file", "effect": "ask"},
    )
    assert created.status_code == 200, created.text

    for field_name in ("effect", "priority", "enabled"):
        response = permission_client.patch(
            f"/admin/tool-permissions/{created.json()['id']}",
            json={field_name: None},
        )
        assert response.status_code == 422, (field_name, response.text)


def test_selection_endpoint_rejects_inaccessible_mcp(permission_client):
    response = permission_client.put(
        "/permissions/rules/selection",
        json={
            "provider": "mcp",
            "server_id": "ghost-server",
            "tool_name": "search",
            "effect": "allow",
        },
    )
    assert response.status_code == 404
    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert (
            db.query(ToolPermissionRule)
            .filter(ToolPermissionRule.scope_id == "alice")
            .count()
            == 0
        )


def test_selection_batch_replaces_all_items_in_one_transaction(permission_db):
    ref_a = ToolRef(provider="builtin", tool_name="read_file")
    ref_b = ToolRef(provider="builtin", tool_name="write_file")
    create_permission_rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ref_a,
        effect="allow",
        created_by="alice",
        conditions={"version": RULE_CONDITIONS_VERSION, "schema_hash": "schema-v1"},
    )
    _rule(permission_db, scope_type="user", scope_id="alice", ref=ref_b, effect="allow")
    permission_db.commit()

    rules = replace_user_tool_selections(
        permission_db,
        user_id="alice",
        refs=[ref_a, ref_b],
        effect="deny",
    )

    assert {rule.tool_name for rule in rules} == {"read_file", "write_file"}
    assert all(rule.effect == "deny" and rule.conditions_json is None for rule in rules)
    remaining = (
        permission_db.query(ToolPermissionRule)
        .filter(ToolPermissionRule.scope_id == "alice")
        .all()
    )
    assert len(remaining) == 2


def test_selection_batch_acquires_exact_tool_locks_in_stable_order(permission_db):
    refs = [
        ToolRef(provider="mcp", server_id="server-z", tool_name="search"),
        ToolRef(provider="builtin", tool_name="write_file"),
        ToolRef(provider="builtin", tool_name="read_file"),
    ]
    locked: list[tuple[str, str | None, str]] = []

    def record_lock(_db, *, user_id, ref):
        assert user_id == "alice"
        locked.append((ref.provider, ref.server_id, ref.tool_name))

    with patch(
        "src.api.services.tool_permission_service."
        "_acquire_user_tool_selection_lock",
        side_effect=record_lock,
    ):
        rules = replace_user_tool_selections(
            permission_db,
            user_id="alice",
            refs=refs,
            effect="ask",
        )

    assert locked == [
        ("builtin", None, "read_file"),
        ("builtin", None, "write_file"),
        ("mcp", "server-z", "search"),
    ]
    assert [rule.tool_name for rule in rules] == [
        "search",
        "write_file",
        "read_file",
    ]


def test_selection_batch_endpoint_applies_to_builtin_items(permission_client):
    response = permission_client.put(
        "/permissions/rules/selection/batch",
        json={
            "effect": "deny",
            "items": [
                {"provider": "builtin", "tool_name": "read_file"},
                {"provider": "builtin", "tool_name": "bash"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    rules = response.json()["rules"]
    assert {rule["tool_name"] for rule in rules} == {"read_file", "bash"}
    assert all(rule["effect"] == "deny" for rule in rules)


def test_selection_batch_endpoint_rejects_inaccessible_mcp_without_mutation(permission_client):
    response = permission_client.put(
        "/permissions/rules/selection/batch",
        json={
            "effect": "allow",
            "items": [
                {
                    "provider": "mcp",
                    "server_id": "server-case",
                    "tool_name": "CaseSensitiveTool",
                },
                {"provider": "mcp", "server_id": "ghost-server", "tool_name": "search"},
            ],
        },
    )
    assert response.status_code == 404
    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert (
            db.query(ToolPermissionRule)
            .filter(ToolPermissionRule.scope_id == "alice")
            .count()
            == 0
        )


def test_selection_batch_endpoint_rejects_duplicate_items(permission_client):
    response = permission_client.put(
        "/permissions/rules/selection/batch",
        json={
            "effect": "deny",
            "items": [
                {"provider": "builtin", "tool_name": "read_file"},
                {"provider": "builtin", "tool_name": "read_file"},
            ],
        },
    )
    assert response.status_code == 422


def test_clear_selection_removes_all_user_rules_for_tool_atomically(permission_db):
    ref = ToolRef(provider="mcp", server_id="server-1", tool_name="search")
    # A manual user rule plus a leftover schema-bound approval grant for the
    # exact same tool: restore-default must remove both in one transaction.
    _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ref,
        effect="allow",
    )
    create_permission_rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ref,
        effect="allow",
        created_by="alice",
        conditions={
            "version": RULE_CONDITIONS_VERSION,
            "schema_hash": "schema-v1",
            "connection_fingerprint": "conn-v1",
        },
    )
    # Rules that must never be touched by a per-tool restore.
    platform_rule = _rule(
        permission_db,
        scope_type="platform",
        scope_id=None,
        ref=ref,
        effect="deny",
        managed=True,
    )
    session_rule = _rule(
        permission_db,
        scope_type="session",
        scope_id="session-a",
        ref=ref,
        effect="allow",
    )
    other_tool_rule = _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ToolRef(provider="mcp", server_id="server-1", tool_name="other"),
        effect="deny",
    )
    permission_db.commit()

    removed = clear_user_tool_selection(permission_db, user_id="alice", ref=ref)

    assert removed == 2
    surviving = {row.id for row in permission_db.query(ToolPermissionRule).all()}
    assert platform_rule.id in surviving
    assert session_rule.id in surviving
    assert other_tool_rule.id in surviving
    assert (
        permission_db.query(ToolPermissionRule)
        .filter(
            ToolPermissionRule.scope_type == "user",
            ToolPermissionRule.scope_id == "alice",
            ToolPermissionRule.tool_name == "search",
        )
        .count()
        == 0
    )


def test_clear_selection_endpoint_removes_rules(permission_client):
    permission_client.put(
        "/permissions/rules/selection",
        json={"provider": "builtin", "tool_name": "read_file", "effect": "deny"},
    )
    response = permission_client.request(
        "DELETE",
        "/permissions/rules/selection",
        json={"provider": "builtin", "tool_name": "read_file"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1
    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert (
            db.query(ToolPermissionRule)
            .filter(
                ToolPermissionRule.scope_id == "alice",
                ToolPermissionRule.tool_name == "read_file",
            )
            .count()
            == 0
        )


def test_clear_selection_endpoint_cleans_up_rules_for_inaccessible_mcp(permission_client):
    # Seed a leftover user rule referencing a server the user can no longer
    # reach, then restore-default: cleanup must succeed (unlike set selection
    # which asserts access), so stale conditional grants can be purged.
    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        create_permission_rule(
            db,
            scope_type="user",
            scope_id="alice",
            ref=ToolRef(provider="mcp", server_id="ghost-server", tool_name="search"),
            effect="allow",
            created_by="alice",
        )
        db.commit()

    response = permission_client.request(
        "DELETE",
        "/permissions/rules/selection",
        json={"provider": "mcp", "server_id": "ghost-server", "tool_name": "search"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1
    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert (
            db.query(ToolPermissionRule)
            .filter(ToolPermissionRule.scope_id == "alice")
            .count()
            == 0
        )


def test_default_policy_allows_builtin_and_asks_for_mcp(permission_db):
    builtin = _decision(permission_db, ToolRef(provider="builtin", tool_name="read_file"))
    remote = _decision(
        permission_db,
        ToolRef(provider="mcp", server_id="server-1", tool_name="search"),
    )

    assert builtin.effect == "allow"
    assert builtin.reason == "using builtin default policy"
    assert remote.effect == "ask"
    assert remote.reason == "using mcp default policy"


def test_batch_permission_evaluator_matches_single_semantics_with_one_rule_query(
    permission_db,
):
    managed_ask = ToolRef(provider="builtin", tool_name="*")
    _rule(
        permission_db,
        scope_type="platform",
        scope_id=None,
        ref=managed_ask,
        effect="ask",
        managed=True,
    )
    _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ToolRef(provider="builtin", tool_name="read_file"),
        effect="allow",
    )
    _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ToolRef(provider="builtin", tool_name="write_file"),
        effect="deny",
    )
    mcp_ref = ToolRef(provider="mcp", server_id="server-1", tool_name="search")
    create_permission_rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=mcp_ref,
        effect="allow",
        created_by="alice",
        conditions={
            "version": 1,
            "schema_hash": "schema-v1",
            "connection_fingerprint": "connection-v1",
        },
    )
    checks = [
        ToolPermissionCheck(
            ref=ToolRef(provider="builtin", tool_name="read_file")
        ),
        ToolPermissionCheck(
            ref=ToolRef(provider="builtin", tool_name="write_file")
        ),
        ToolPermissionCheck(
            ref=ToolRef(provider="builtin", tool_name="record_note")
        ),
        ToolPermissionCheck(
            ref=mcp_ref,
            schema_hash="schema-v1",
            connection_fingerprint="connection-v1",
        ),
        ToolPermissionCheck(
            ref=mcp_ref,
            schema_hash="schema-v2",
            connection_fingerprint="connection-v1",
        ),
    ]
    expected = [
        evaluate_tool_permission(
            permission_db,
            user_id="alice",
            session_id="session-a",
            ref=check.ref,
            default_effect=check.default_effect,
            schema_hash=check.schema_hash,
            connection_fingerprint=check.connection_fingerprint,
        )
        for check in checks
    ]

    rule_selects: list[str] = []

    def count_rule_selects(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = statement.casefold()
        if "select" in normalized and "tool_permission_rules" in normalized:
            rule_selects.append(statement)

    engine = permission_db.get_bind()
    event.listen(engine, "before_cursor_execute", count_rule_selects)
    try:
        actual = evaluate_tool_permissions(
            permission_db,
            user_id="alice",
            session_id="session-a",
            checks=checks,
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_rule_selects)

    assert actual == expected
    assert [decision.effect for decision in actual] == [
        "ask",
        "deny",
        "ask",
        "allow",
        "ask",
    ]
    assert len(rule_selects) == 1


def test_large_permission_batch_avoids_oversized_sqlite_name_in_clause(permission_db):
    _rule(
        permission_db,
        scope_type="platform",
        scope_id=None,
        ref=ToolRef(provider="builtin", tool_name="*"),
        effect="ask",
        managed=True,
    )
    _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ToolRef(provider="builtin", tool_name="tool_1199"),
        effect="deny",
    )
    checks = [
        ToolPermissionCheck(
            ref=ToolRef(provider="builtin", tool_name=f"tool_{index}")
        )
        for index in range(1200)
    ]
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _parameters, _context, _many):
        if "tool_permission_rules" in statement.casefold():
            statements.append(statement)

    engine = permission_db.get_bind()
    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        decisions = evaluate_tool_permissions(
            permission_db,
            user_id="alice",
            session_id="session-a",
            checks=checks,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert len(decisions) == 1200
    assert decisions[0].effect == "ask"
    assert decisions[-1].effect == "deny"
    assert len(statements) == 1
    assert "tool_permission_rules.tool_name in" not in statements[0].casefold()


def test_permission_audit_autoincrements_on_sqlite(permission_db):
    first = record_permission_audit(
        permission_db,
        user_id="alice",
        ref=ToolRef(provider="builtin", tool_name="read_file"),
        effect="allow",
        outcome="executed",
    )
    second = record_permission_audit(
        permission_db,
        user_id="alice",
        ref=ToolRef(provider="builtin", tool_name="write_file"),
        effect="deny",
        outcome="blocked",
    )

    assert first.id == 1
    assert second.id == 2
    assert permission_db.query(ToolPermissionAudit).count() == 2


def test_permission_audit_bounds_large_utf8_reason_with_hash(permission_db):
    reason = "远端错误🔒" * 4000
    expected_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()

    row = record_permission_audit(
        permission_db,
        user_id="alice",
        ref=ToolRef(provider="mcp", server_id="server-1", tool_name="search"),
        effect="allow",
        outcome="failed",
        reason=reason,
    )

    assert row.reason is not None
    assert len(row.reason.encode("utf-8")) <= 8 * 1024
    assert row.reason.endswith(f"[truncated; sha256={expected_hash}]")
    assert reason not in row.reason


def test_permission_api_preserves_mcp_name_and_enforces_managed_ask_ceiling(
    permission_client: TestClient,
):
    managed = permission_client.post(
        "/admin/tool-permissions",
        json={
            "provider": "mcp",
            "server_id": "server-case",
            "tool_name": "CaseSensitiveTool",
            "effect": "ask",
        },
    )
    assert managed.status_code == 200, managed.text

    local = permission_client.post(
        "/permissions/rules",
        json={
            "provider": "mcp",
            "server_id": "server-case",
            "tool_name": "CaseSensitiveTool",
            "effect": "allow",
        },
    )
    assert local.status_code == 200, local.text
    assert local.json()["tool_name"] == "CaseSensitiveTool"

    inventory = permission_client.get("/permissions/tools")
    assert inventory.status_code == 200, inventory.text
    remote = next(
        item for item in inventory.json()["tools"]
        if item["tool_name"] == "CaseSensitiveTool"
    )
    assert remote["effect"] == "ask"


def test_permission_inventory_hides_disabled_official_tools(
    permission_client: TestClient,
):
    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        server = db.query(McpServer).filter(McpServer.id == "server-case").one()
        server.status = "disabled"
        db.commit()

    inventory = permission_client.get("/permissions/tools")
    assert inventory.status_code == 200, inventory.text
    assert all(
        item["server_id"] != "server-case"
        for item in inventory.json()["tools"]
    )
    create = permission_client.post(
        "/permissions/rules",
        json={
            "provider": "mcp",
            "server_id": "server-case",
            "tool_name": "CaseSensitiveTool",
            "effect": "allow",
        },
    )
    assert create.status_code == 404


def test_permission_inventory_exposes_dynamic_tool_search(
    permission_client: TestClient,
):
    created = permission_client.post(
        "/permissions/rules",
        json={
            "provider": "builtin",
            "tool_name": "tool_search",
            "effect": "deny",
        },
    )
    assert created.status_code == 200, created.text

    inventory = permission_client.get("/permissions/tools")
    assert inventory.status_code == 200, inventory.text
    tool_search = next(
        item
        for item in inventory.json()["tools"]
        if item["tool_ref"] == "builtin:tool_search"
    )
    assert tool_search["tool_name"] == "tool_search"
    assert tool_search["description"] == "搜索并加载按需工具"
    assert tool_search["effect"] == "deny"
    assert tool_search["matched_rule_id"] == created.json()["id"]


def test_permission_inventory_batches_policy_queries_and_truncates_descriptions(
    permission_client: TestClient,
):
    with permission_client.SessionLocal() as db:  # type: ignore[attr-defined]
        for index in range(20):
            db.add(McpToolSnapshot(
                installation_id="installation-case",
                tool_name=f"LongDescription{index:02d}",
                title=None,
                description="界" * 2000,
                input_schema_json="{}",
                schema_hash=f"schema-{index}",
            ))
        db.commit()

    engine = permission_client.SessionLocal.kw["bind"]  # type: ignore[attr-defined]
    rule_selects: list[str] = []

    def count_rule_selects(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = statement.casefold()
        if "select" in normalized and "tool_permission_rules" in normalized:
            rule_selects.append(statement)

    event.listen(engine, "before_cursor_execute", count_rule_selects)
    try:
        response = permission_client.get("/permissions/tools")
    finally:
        event.remove(engine, "before_cursor_execute", count_rule_selects)

    assert response.status_code == 200, response.text
    long_tools = [
        item
        for item in response.json()["tools"]
        if item["tool_name"].startswith("LongDescription")
    ]
    assert len(long_tools) == 20
    assert all(len(item["description"]) == 500 for item in long_tools)
    assert all(item["description"].endswith("…") for item in long_tools)
    assert len(rule_selects) == 1


def test_permission_tool_names_reject_surrounding_whitespace(
    permission_client: TestClient,
    permission_db,
):
    response = permission_client.post(
        "/permissions/rules",
        json={
            "provider": "mcp",
            "server_id": "server-case",
            "tool_name": " delete ",
            "effect": "deny",
        },
    )
    assert response.status_code == 422

    control = permission_client.post(
        "/permissions/rules",
        json={
            "provider": "mcp",
            "server_id": "server-case",
            "tool_name": "delete\x00all",
            "effect": "deny",
        },
    )
    assert control.status_code == 422

    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        create_permission_rule(
            permission_db,
            scope_type="user",
            scope_id="alice",
            ref=ToolRef(
                provider="mcp",
                server_id="server-case",
                tool_name=" delete ",
            ),
            effect="deny",
            created_by="alice",
        )

    with pytest.raises(ValueError, match="ASCII control characters"):
        create_permission_rule(
            permission_db,
            scope_type="user",
            scope_id="alice",
            ref=ToolRef(
                provider="mcp",
                server_id="server-case",
                tool_name="delete\x00all",
            ),
            effect="deny",
            created_by="alice",
        )


def test_managed_allow_is_baseline_ask_is_ceiling_and_deny_is_hard_block(permission_db):
    managed_allow_ref = ToolRef(
        provider="mcp", server_id="server-1", tool_name="managed-allow"
    )
    _rule(
        permission_db,
        scope_type="platform",
        scope_id=None,
        ref=managed_allow_ref,
        effect="allow",
        managed=True,
    )
    assert _decision(permission_db, managed_allow_ref).effect == "allow"

    # Platform ALLOW is a baseline, not a relaxation ceiling: a user may make
    # their own policy stricter.
    _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=managed_allow_ref,
        effect="deny",
    )
    assert _decision(permission_db, managed_allow_ref).effect == "deny"

    managed_ask_ref = ToolRef(provider="builtin", tool_name="managed-ask")
    managed_ask = _rule(
        permission_db,
        scope_type="platform",
        scope_id=None,
        ref=managed_ask_ref,
        effect="ask",
        managed=True,
    )
    _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=managed_ask_ref,
        effect="allow",
    )
    ask_decision = _decision(permission_db, managed_ask_ref)
    assert ask_decision.effect == "ask"
    assert ask_decision.managed is True
    assert ask_decision.matched_rule_id == managed_ask.id

    managed_deny_ref = ToolRef(provider="builtin", tool_name="managed-deny")
    managed_deny = _rule(
        permission_db,
        scope_type="platform",
        scope_id=None,
        ref=managed_deny_ref,
        effect="deny",
        managed=True,
    )
    _rule(
        permission_db,
        scope_type="session",
        scope_id="session-a",
        ref=managed_deny_ref,
        effect="allow",
        priority=999,
    )
    deny_decision = _decision(permission_db, managed_deny_ref)
    assert deny_decision.effect == "deny"
    assert deny_decision.managed is True
    assert deny_decision.matched_rule_id == managed_deny.id


def test_session_scope_and_specificity_precede_rule_priority(permission_db):
    target = ToolRef(provider="builtin", tool_name="shell_exec")

    # Exact user rule beats a higher-priority wildcard at the same scope.
    _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ToolRef(provider="builtin", tool_name="*"),
        effect="deny",
        priority=1000,
    )
    _rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=target,
        effect="allow",
        priority=0,
    )
    assert _decision(permission_db, target, session_id="other-session").effect == "allow"

    # Session scope is more specific than user scope, even for a wildcard.
    _rule(
        permission_db,
        scope_type="session",
        scope_id="session-a",
        ref=ToolRef(provider="builtin", tool_name="*"),
        effect="ask",
        priority=-100,
    )
    assert _decision(permission_db, target).effect == "ask"

    # Within otherwise identical specificity, priority selects the winner.
    _rule(
        permission_db,
        scope_type="session",
        scope_id="session-a",
        ref=target,
        effect="allow",
        priority=1,
    )
    _rule(
        permission_db,
        scope_type="session",
        scope_id="session-a",
        ref=target,
        effect="deny",
        priority=2,
    )
    assert _decision(permission_db, target).effect == "deny"


@pytest.mark.parametrize(
    ("resolution", "expected_scope", "expected_scope_id"),
    [
        ("allow_session", "session", "session-a"),
        ("allow_always", "user", "alice"),
    ],
)
def test_approval_claim_is_single_use_and_persists_allow_rule(
    permission_db,
    resolution: str,
    expected_scope: str,
    expected_scope_id: str,
):
    request_id = f"approval-{resolution}"
    create_approval_request(
        permission_db,
        request_id=request_id,
        user_id="alice",
        session_id="session-a",
        run_id=f"run-{resolution}",
        tool_call_id=f"call-{resolution}",
        ref=ToolRef(provider="builtin", tool_name="shell_exec"),
        model_tool_name="shell_exec",
        arguments={"command": "pwd", "nested": {"value": 1}},
    )

    claim = claim_approval_request(
        permission_db,
        request_id=request_id,
        user_id="alice",
        resolution=resolution,
    )

    assert claim.should_execute is True
    assert claim.arguments == {"command": "pwd", "nested": {"value": 1}}
    assert claim.request.status == "executing"
    assert claim.claim_token
    assert claim.request.execution_claim_token == claim.claim_token
    assert claim.lease_expires_at is not None
    persisted_rule = (
        permission_db.query(ToolPermissionRule)
        .filter(ToolPermissionRule.description == f"Created from approval {request_id}")
        .one()
    )
    assert persisted_rule.scope_type == expected_scope
    assert persisted_rule.scope_id == expected_scope_id
    assert persisted_rule.effect == "allow"
    assert persisted_rule.conditions_json is None

    with pytest.raises(RuntimeError, match="already resolved: executing"):
        claim_approval_request(
            permission_db,
            request_id=request_id,
            user_id="alice",
            resolution=resolution,
        )


def test_approval_claim_cas_allows_only_one_database_session(permission_db):
    create_approval_request(
        permission_db,
        request_id="approval-cas",
        user_id="alice",
        session_id="session-a",
        run_id="run-cas",
        tool_call_id="call-cas",
        ref=ToolRef(provider="builtin", tool_name="shell_exec"),
        model_tool_name="shell_exec",
        arguments={"command": "pwd"},
    )
    contender_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=permission_db.get_bind(),
    )
    with contender_factory() as first, contender_factory() as second:
        winner = claim_approval_request(
            first,
            request_id="approval-cas",
            user_id="alice",
            resolution="allow_once",
        )
        with pytest.raises(RuntimeError, match="already resolved: executing"):
            claim_approval_request(
                second,
                request_id="approval-cas",
                user_id="alice",
                resolution="allow_once",
            )
    assert winner.claim_token


def test_uncommitted_claim_returns_fresh_token_and_lease(permission_db):
    request = create_approval_request(
        permission_db,
        request_id="approval-uncommitted",
        user_id="alice",
        session_id="session-a",
        run_id="run-uncommitted",
        tool_call_id="call-uncommitted",
        ref=ToolRef(provider="builtin", tool_name="shell_exec"),
        model_tool_name="shell_exec",
        arguments={"command": "pwd"},
    )
    assert request.status == "requested"

    claim = claim_approval_request(
        permission_db,
        request_id=request.id,
        user_id="alice",
        resolution="allow_once",
        commit=False,
    )

    assert claim.request is request
    assert claim.request.status == "executing"
    assert claim.claim_token == claim.request.execution_claim_token
    assert claim.claim_token
    assert claim.lease_expires_at == claim.request.execution_lease_expires_at
    permission_db.rollback()


def test_execution_lease_renews_and_token_fences_stale_worker(permission_db):
    create_approval_request(
        permission_db,
        request_id="approval-renew",
        user_id="alice",
        session_id="session-a",
        run_id="run-renew",
        tool_call_id="call-renew",
        ref=ToolRef(provider="builtin", tool_name="shell_exec"),
        model_tool_name="shell_exec",
        arguments={"command": "pwd"},
    )
    claim = claim_approval_request(
        permission_db,
        request_id="approval-renew",
        user_id="alice",
        resolution="allow_once",
    )
    initial_expiry = claim.lease_expires_at

    assert renew_approval_execution_lease(
        permission_db,
        request_id=claim.request_id,
        user_id="alice",
        claim_token=claim.claim_token or "",
        lease_seconds=600,
    ) is True
    permission_db.refresh(claim.request)
    assert claim.request.execution_lease_expires_at > initial_expiry
    assert renew_approval_execution_lease(
        permission_db,
        request_id=claim.request_id,
        user_id="alice",
        claim_token="stale-token",
    ) is False
    with pytest.raises(RuntimeError, match="claim token"):
        finish_approval_request(
            permission_db,
            request_id=claim.request_id,
            user_id="alice",
            claim_token="stale-token",
            result_content="ok",
            success=True,
        )


def test_expired_execution_reconciles_to_unknown_and_cannot_retry(permission_db):
    create_approval_request(
        permission_db,
        request_id="approval-expired",
        user_id="alice",
        session_id="session-a",
        run_id="run-expired",
        tool_call_id="call-expired",
        ref=ToolRef(provider="builtin", tool_name="shell_exec"),
        model_tool_name="shell_exec",
        arguments={"command": "pwd"},
    )
    claim = claim_approval_request(
        permission_db,
        request_id="approval-expired",
        user_id="alice",
        resolution="allow_once",
    )
    token = claim.claim_token
    claim.request.execution_lease_expires_at = now_naive() - timedelta(seconds=1)
    permission_db.commit()

    assert reconcile_expired_approval_leases(permission_db) == 1
    assert reconcile_expired_approval_leases(permission_db) == 0
    permission_db.refresh(claim.request)
    assert claim.request.status == "unknown"
    assert claim.request.error == APPROVAL_OUTCOME_UNKNOWN_ERROR
    assert claim.request.execution_claim_token is None
    with pytest.raises(RuntimeError, match="already resolved: unknown"):
        claim_approval_request(
            permission_db,
            request_id=claim.request_id,
            user_id="alice",
            resolution="allow_once",
        )
    with pytest.raises(RuntimeError, match="not executing: unknown"):
        finish_approval_request(
            permission_db,
            request_id=claim.request_id,
            user_id="alice",
            claim_token=token,
            result_content="late result",
            success=True,
        )


def test_active_execution_lease_survives_reconciliation(permission_db):
    create_approval_request(
        permission_db,
        request_id="approval-active",
        user_id="alice",
        session_id="session-a",
        run_id="run-active",
        tool_call_id="call-active",
        ref=ToolRef(provider="builtin", tool_name="shell_exec"),
        model_tool_name="shell_exec",
        arguments={"command": "pwd"},
    )
    claim = claim_approval_request(
        permission_db,
        request_id="approval-active",
        user_id="alice",
        resolution="allow_once",
    )

    assert reconcile_expired_approval_leases(permission_db) == 0
    permission_db.refresh(claim.request)
    assert claim.request.status == "executing"


def test_uncertain_finish_is_terminal_unknown(permission_db):
    create_approval_request(
        permission_db,
        request_id="approval-uncertain",
        user_id="alice",
        session_id="session-a",
        run_id="run-uncertain",
        tool_call_id="call-uncertain",
        ref=ToolRef(provider="builtin", tool_name="remote_write"),
        model_tool_name="remote_write",
        arguments={"value": 1},
    )
    claim = claim_approval_request(
        permission_db,
        request_id="approval-uncertain",
        user_id="alice",
        resolution="allow_once",
    )

    finished = finish_approval_request(
        permission_db,
        request_id=claim.request_id,
        user_id="alice",
        claim_token=claim.claim_token,
        result_content="remote response was lost",
        success=False,
        outcome_uncertain=True,
    )
    assert finished.status == "unknown"
    assert finished.error == APPROVAL_OUTCOME_UNKNOWN_ERROR
    assert finished.execution_claim_token is None


def test_failed_approval_result_is_not_stored_in_plaintext(permission_db):
    create_approval_request(
        permission_db,
        request_id="approval-failed-secret",
        user_id="alice",
        session_id="session-a",
        run_id="run-failed-secret",
        tool_call_id="call-failed-secret",
        ref=ToolRef(provider="builtin", tool_name="remote_write"),
        model_tool_name="remote_write",
        arguments={"value": 1},
    )
    claim = claim_approval_request(
        permission_db,
        request_id="approval-failed-secret",
        user_id="alice",
        resolution="allow_once",
    )
    sensitive_result = "upstream failed while handling customer-secret-value"

    finished = finish_approval_request(
        permission_db,
        request_id=claim.request_id,
        user_id="alice",
        claim_token=claim.claim_token,
        result_content=sensitive_result,
        success=False,
    )

    assert finished.status == "failed"
    assert finished.error == APPROVAL_EXECUTION_FAILED_ERROR
    assert sensitive_result not in (finished.error or "")
    assert decrypt_secret(finished.result_encrypted) == sensitive_result


@pytest.mark.parametrize("resolution", ["allow_session", "allow_always"])
def test_remembered_mcp_allow_is_bound_to_approved_schema(
    permission_db,
    resolution: str,
):
    server = McpServer(
        id="server-schema",
        source="official",
        name="Schema Tools",
        url="https://93.184.216.34/mcp",
        status="published",
        auth_type="none",
    )
    installation = McpInstallation(
        id="installation-schema",
        server_id=server.id,
        user_id="alice",
        enabled=True,
    )
    connection_fingerprint = EffectiveMcpInstallation(
        installation_id=installation.id,
        server_id=server.id,
        user_id="alice",
        server_name=server.name,
        url=server.url,
        auth_type="none",
    ).execution_fingerprint
    snapshot = McpToolSnapshot(
        installation_id=installation.id,
        tool_name="MutableTool",
        description="Mutable tool",
        input_schema_json="{}",
        schema_hash="schema-v1",
        connection_fingerprint=connection_fingerprint,
    )
    permission_db.add_all([server, installation, snapshot])
    permission_db.commit()
    ref = ToolRef(
        provider="mcp",
        server_id=server.id,
        tool_name="MutableTool",
    )
    request_id = f"approval-schema-{resolution}"
    create_approval_request(
        permission_db,
        request_id=request_id,
        user_id="alice",
        session_id="session-a",
        run_id=f"run-schema-{resolution}",
        tool_call_id=f"call-schema-{resolution}",
        ref=ref,
        model_tool_name="mcp__schema__MutableTool",
        arguments={"value": 1},
        installation_id=installation.id,
        schema_hash="schema-v1",
        connection_fingerprint=connection_fingerprint,
    )

    claim_approval_request(
        permission_db,
        request_id=request_id,
        user_id="alice",
        resolution=resolution,
    )

    persisted_rule = (
        permission_db.query(ToolPermissionRule)
        .filter(ToolPermissionRule.description == f"Created from approval {request_id}")
        .one()
    )
    assert persisted_rule.conditions_json == (
        '{"connection_fingerprint":"' + connection_fingerprint
        + '","schema_hash":"schema-v1","version":1}'
    )
    assert _decision(
        permission_db,
        ref,
        schema_hash="schema-v1",
        connection_fingerprint=connection_fingerprint,
    ).effect == "allow"
    assert _decision(
        permission_db,
        ref,
        schema_hash="schema-v2",
        connection_fingerprint=connection_fingerprint,
    ).effect == "ask"
    assert _decision(
        permission_db,
        ref,
        schema_hash="schema-v1",
        connection_fingerprint="changed-target",
    ).effect == "ask"
    assert _decision(permission_db, ref).effect == "ask"


@pytest.mark.parametrize(
    "invalid_binding",
    ["missing_schema", "missing_connection", "changed_endpoint"],
)
def test_mcp_approval_never_creates_unbound_remembered_allow(
    permission_db,
    invalid_binding: str,
):
    suffix = invalid_binding.replace("_", "-")
    server = McpServer(
        id=f"server-{suffix}",
        source="official",
        name=f"Binding {suffix}",
        url="https://93.184.216.34/mcp",
        status="published",
        auth_type="none",
    )
    installation = McpInstallation(
        id=f"installation-{suffix}",
        server_id=server.id,
        user_id="alice",
        enabled=True,
    )
    original_fingerprint = EffectiveMcpInstallation(
        installation_id=installation.id,
        server_id=server.id,
        user_id="alice",
        server_name=server.name,
        url=server.url,
        auth_type="none",
    ).execution_fingerprint
    permission_db.add_all([
        server,
        installation,
        McpToolSnapshot(
            installation_id=installation.id,
            tool_name="BoundTool",
            description="Bound tool",
            input_schema_json="{}",
            schema_hash="schema-v1",
            connection_fingerprint=original_fingerprint,
        ),
    ])
    permission_db.commit()
    create_approval_request(
        permission_db,
        request_id=f"approval-{suffix}",
        user_id="alice",
        session_id="session-a",
        run_id=f"run-{suffix}",
        tool_call_id=f"call-{suffix}",
        ref=ToolRef(
            provider="mcp",
            server_id=server.id,
            tool_name="BoundTool",
        ),
        model_tool_name=f"mcp__{suffix}__BoundTool",
        arguments={"value": 1},
        installation_id=installation.id,
        schema_hash=None if invalid_binding == "missing_schema" else "schema-v1",
        connection_fingerprint=(
            None
            if invalid_binding == "missing_connection"
            else original_fingerprint
        ),
    )
    if invalid_binding == "changed_endpoint":
        server.url = "https://93.184.216.35/mcp"
        permission_db.commit()

    claim = claim_approval_request(
        permission_db,
        request_id=f"approval-{suffix}",
        user_id="alice",
        resolution="allow_always",
    )

    assert claim.should_execute is True
    assert permission_db.query(ToolPermissionRule).filter(
        ToolPermissionRule.description == f"Created from approval approval-{suffix}"
    ).count() == 0


@pytest.mark.parametrize(
    "conditions",
    [
        {},
        {"schema_hash": "schema-v1", "connection_fingerprint": "connection-v1"},
        {"version": 2, "schema_hash": "schema-v1", "connection_fingerprint": "connection-v1"},
        {"version": 1, "schema_hash": "schema-v1"},
        {
            "version": 1,
            "schema_hash": "schema-v1",
            "connection_fingerprint": "connection-v1",
            "future_condition": True,
        },
    ],
)
def test_condition_rule_creation_requires_exact_versioned_shape(permission_db, conditions):
    with pytest.raises(ValueError):
        create_permission_rule(
            permission_db,
            scope_type="user",
            scope_id="alice",
            ref=ToolRef(provider="mcp", server_id="server-1", tool_name="search"),
            effect="allow",
            created_by="alice",
            conditions=conditions,
        )


def test_conditions_are_supported_only_for_allow_rules(permission_db):
    with pytest.raises(ValueError, match="only for allow"):
        create_permission_rule(
            permission_db,
            scope_type="user",
            scope_id="alice",
            ref=ToolRef(provider="builtin", tool_name="shell_exec"),
            effect="deny",
            created_by="alice",
            conditions={"version": 1, "schema_hash": "schema-v1"},
        )


@pytest.mark.parametrize(
    "conditions_json",
    [
        "{not-json",
        "[]",
        "{}",
        '{"version":2,"schema_hash":"schema-v1","connection_fingerprint":"connection-v1"}',
        '{"version":1,"schema_hash":"schema-v1"}',
        '{"version":1,"schema_hash":"schema-v1","connection_fingerprint":"connection-v1","unknown":true}',
    ],
)
def test_invalid_persisted_allow_conditions_fail_closed(permission_db, conditions_json):
    tool_name = f"invalid-{abs(hash(conditions_json))}"
    permission_db.add(ToolPermissionRule(
        id=f"rule-{abs(hash(conditions_json))}",
        scope_type="user",
        scope_id="alice",
        provider="mcp",
        server_id="server-1",
        tool_name=tool_name,
        effect="allow",
        priority=0,
        managed=False,
        conditions_json=conditions_json,
        enabled=True,
        created_by="alice",
    ))
    permission_db.commit()

    decision = _decision(
        permission_db,
        ToolRef(provider="mcp", server_id="server-1", tool_name=tool_name),
        schema_hash="schema-v1",
        connection_fingerprint="connection-v1",
    )

    assert decision.effect == "ask"


@pytest.mark.parametrize("effect", ["ask", "deny"])
def test_invalid_persisted_managed_restrictions_remain_effective(permission_db, effect):
    permission_db.add(ToolPermissionRule(
        id=f"managed-invalid-{effect}",
        scope_type="platform",
        scope_id=None,
        provider="builtin",
        server_id=None,
        tool_name=f"restricted-{effect}",
        effect=effect,
        priority=0,
        managed=True,
        conditions_json='{"unknown":true}',
        enabled=True,
        created_by="admin",
    ))
    permission_db.commit()

    decision = _decision(
        permission_db,
        ToolRef(provider="builtin", tool_name=f"restricted-{effect}"),
    )

    assert decision.effect == effect


def test_versioned_mcp_allow_requires_both_live_bindings(permission_db):
    ref = ToolRef(provider="mcp", server_id="server-1", tool_name="bound-search")
    create_permission_rule(
        permission_db,
        scope_type="user",
        scope_id="alice",
        ref=ref,
        effect="allow",
        created_by="alice",
        conditions={
            "version": 1,
            "schema_hash": "schema-v1",
            "connection_fingerprint": "connection-v1",
        },
    )

    assert _decision(
        permission_db,
        ref,
        schema_hash="schema-v1",
        connection_fingerprint="connection-v1",
    ).effect == "allow"
    assert _decision(
        permission_db,
        ref,
        schema_hash="schema-v1",
        connection_fingerprint=None,
    ).effect == "ask"


def test_deleted_mcp_target_can_resolve_without_creating_dangling_allow_rule(
    permission_db,
):
    create_approval_request(
        permission_db,
        request_id="approval-deleted-mcp",
        user_id="alice",
        session_id="session-a",
        run_id="run-deleted-mcp",
        tool_call_id="call-deleted-mcp",
        ref=ToolRef(
            provider="mcp",
            server_id="deleted-server",
            tool_name="RemoteTool",
        ),
        model_tool_name="mcp__deleted__RemoteTool",
        arguments={"value": 1},
        installation_id="deleted-installation",
    )

    claim = claim_approval_request(
        permission_db,
        request_id="approval-deleted-mcp",
        user_id="alice",
        resolution="allow_always",
    )

    assert claim.should_execute is True
    assert claim.request.status == "executing"
    assert permission_db.query(ToolPermissionRule).filter(
        ToolPermissionRule.server_id == "deleted-server"
    ).count() == 0


def _tool_call_response(
    tool_name: str,
    arguments: dict | None = None,
) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="call-1",
                type="function",
                function=FunctionCall(
                    name=tool_name,
                    arguments=(
                        {"param1": "value"}
                        if arguments is None
                        else arguments
                    ),
                ),
            )
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_agent_ask_emits_human_approval_interrupt_without_execution(tmp_path):
    tool = MockTool("protected_tool")
    llm = MockLLMClient()
    llm.stream_responses = [_tool_call_response(tool.name)]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )
    payload = {
        "kind": "tool_approval",
        "provider": "builtin",
        "tool_ref": "builtin:protected_tool",
        "tool_name": "protected_tool",
        "tool_call_id": "call-1",
    }
    ask = SimpleNamespace(
        effect="ask",
        reason="confirmation required",
        matched_rule_id="rule-ask",
    )

    with (
        patch.object(agent, "_visible_tools_for_request", return_value=[tool]),
        patch.object(agent, "_resolve_tool_permission", return_value=ask),
        patch.object(
            agent,
            "_create_tool_approval",
            return_value=("approval-1", payload),
        ) as create_approval,
        patch.object(agent, "_record_permission_audit"),
    ):
        events = [event async for event in agent.run_agui("session-a", "run-a")]

    assert tool.execute_count == 0
    create_approval.assert_called_once()
    finished = next(event for event in events if event.type == EventType.RUN_FINISHED)
    assert finished.outcome == "interrupt"
    assert finished.interrupt is not None
    assert finished.interrupt.id == "approval-1"
    assert finished.interrupt.reason == "human_approval"
    assert finished.interrupt.payload["kind"] == "tool_approval"
    assert agent.get_pending_interrupt()["interrupt_id"] == "approval-1"


@pytest.mark.asyncio
async def test_agent_deny_records_blocked_result_and_never_executes_tool(tmp_path):
    tool = MockTool("blocked_tool")
    llm = MockLLMClient()
    llm.stream_responses = [
        _tool_call_response(tool.name),
        LLMResponse(content="The tool was blocked.", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )
    deny = SimpleNamespace(
        effect="deny",
        reason="blocked by managed policy",
        matched_rule_id="rule-deny",
    )

    with (
        patch.object(agent, "_visible_tools_for_request", return_value=[tool]),
        patch.object(agent, "_resolve_tool_permission", return_value=deny),
        patch.object(agent, "_record_permission_audit") as audit,
    ):
        events = [event async for event in agent.run_agui("session-a", "run-a")]

    assert tool.execute_count == 0
    results = [event for event in events if event.type == EventType.TOOL_CALL_RESULT]
    assert len(results) == 1
    assert results[0].content == "Tool is unavailable in this conversation"
    audit.assert_called_once()
    assert audit.call_args.kwargs["effect"] == "deny"
    assert audit.call_args.kwargs["outcome"] == "blocked"
    finished = next(event for event in events if event.type == EventType.RUN_FINISHED)
    assert finished.outcome == "success"


@pytest.mark.asyncio
async def test_authenticated_builtin_fails_closed_when_policy_store_is_unavailable(
    tmp_path,
):
    tool = MockTool("builtin_tool")
    llm = MockLLMClient()
    llm.stream_responses = [
        _tool_call_response(tool.name),
        LLMResponse(content="blocked", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )

    with patch(
        "src.api.models.database.SessionLocal",
        side_effect=RuntimeError("permission database unavailable"),
    ):
        assert agent._visible_tools_for_request("session-a") == []
        assert agent._resolve_tool_permission(tool, session_id="session-a").effect == "deny"
        events = [event async for event in agent.run_agui("session-a", "run-a")]

    assert tool.execute_count == 0
    result = next(event for event in events if event.type == EventType.TOOL_CALL_RESULT)
    assert result.content == "Tool is unavailable in this conversation"


@pytest.mark.asyncio
async def test_deny_precedes_argument_validation_without_schema_leak(tmp_path):
    tool = MockTool("blocked_tool")
    llm = MockLLMClient()
    llm.stream_responses = [
        _tool_call_response(tool.name, arguments={}),
        LLMResponse(content="blocked", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )
    deny = SimpleNamespace(
        effect="deny",
        reason="blocked by managed policy",
        matched_rule_id="rule-deny",
    )

    with (
        patch.object(agent, "_visible_tools_for_request", return_value=[tool]),
        patch.object(agent, "_resolve_tool_permission", return_value=deny),
        patch.object(
            agent,
            "_validate_tool_arguments",
            wraps=agent._validate_tool_arguments,
        ) as validate,
        patch.object(agent, "_record_permission_audit") as audit,
    ):
        events = [event async for event in agent.run_agui("session-a", "run-a")]

    assert tool.execute_count == 0
    validate.assert_not_called()
    result = next(event for event in events if event.type == EventType.TOOL_CALL_RESULT)
    assert result.content == "Tool is unavailable in this conversation"
    assert "param1" not in result.content
    audit.assert_called_once()
    assert audit.call_args.kwargs["effect"] == "deny"
    assert audit.call_args.kwargs["outcome"] == "blocked"


@pytest.mark.asyncio
async def test_policy_flip_to_deny_at_execution_boundary_prevents_execution(tmp_path):
    tool = MockTool("mutable_policy_tool")
    llm = MockLLMClient()
    llm.stream_responses = [
        _tool_call_response(tool.name),
        LLMResponse(content="blocked", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )
    allow = SimpleNamespace(
        effect="allow",
        reason="allowed",
        matched_rule_id="rule-allow",
    )
    deny = SimpleNamespace(
        effect="deny",
        reason="policy changed",
        matched_rule_id="rule-deny",
    )

    with (
        patch.object(agent, "_visible_tools_for_request", return_value=[tool]),
        patch.object(
            agent,
            "_resolve_tool_permission",
            side_effect=[allow, allow, deny],
        ) as resolve,
        patch.object(agent, "_record_permission_audit") as audit,
    ):
        events = [event async for event in agent.run_agui("session-a", "run-a")]

    assert resolve.call_count == 3
    assert tool.execute_count == 0
    result = next(event for event in events if event.type == EventType.TOOL_CALL_RESULT)
    assert result.content == "Tool is unavailable in this conversation"
    audit.assert_called_once()
    assert audit.call_args.kwargs["effect"] == "deny"
    assert audit.call_args.kwargs["outcome"] == "blocked_at_execution"


@pytest.mark.asyncio
async def test_claimed_approval_executes_once_before_resume_llm(tmp_path):
    tool = MockTool("protected_tool")
    llm = MockLLMClient()
    llm.stream_responses = [
        LLMResponse(content="done", tool_calls=[], finish_reason="stop"),
        LLMResponse(content="still done", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
    )
    agent.messages.append(Message(
        role="tool",
        content="[Awaiting tool approval]",
        tool_call_id="call-1",
        name=tool.name,
    ))
    agent.queue_tool_approval_resume(
        request_id="approval-1",
        tool_call_id="call-1",
        function_name=tool.name,
        arguments={"param1": "value"},
        provider="builtin",
        tool_name=tool.name,
        server_id=None,
        installation_id=None,
        schema_hash=None,
        resolution="allow_once",
        should_execute=True,
        claim_token="claim-approved",
    )

    first_events = [event async for event in agent.run_agui("session-a", "resume-1")]
    second_events = [event async for event in agent.run_agui("session-a", "resume-2")]

    assert tool.execute_count == 1
    first_results = [
        event for event in first_events if event.type == EventType.TOOL_CALL_RESULT
    ]
    assert len(first_results) == 1
    assert first_results[0].content == "Mock tool executed"
    assert not [
        event for event in second_events if event.type == EventType.TOOL_CALL_RESULT
    ]
    placeholders = [
        message for message in agent.messages
        if message.role == "tool" and message.tool_call_id == "call-1"
    ]
    assert len(placeholders) == 1
    assert placeholders[0].content == "Mock tool executed"


def _queue_mcp_approval_resume(
    agent: Agent,
    tool: _ApprovalMcpTool,
    *,
    schema_hash: str,
    connection_fingerprint: str | None = None,
) -> None:
    agent.messages.append(Message(
        role="tool",
        content="[Awaiting tool approval]",
        tool_call_id="call-mcp-1",
        name=tool.name,
    ))
    ref = tool.tool_ref
    agent.queue_tool_approval_resume(
        request_id="approval-mcp-1",
        tool_call_id="call-mcp-1",
        function_name=tool.name,
        arguments={"param1": "value"},
        provider=ref.provider,
        tool_name=ref.name,
        server_id=ref.server_id,
        installation_id=ref.installation_id,
        schema_hash=schema_hash,
        connection_fingerprint=(
            tool.connection_fingerprint
            if connection_fingerprint is None
            else connection_fingerprint
        ),
        resolution="allow_once",
        should_execute=True,
    )


@pytest.mark.asyncio
async def test_claimed_mcp_approval_blocks_when_current_schema_hash_is_missing(tmp_path):
    tool = _ApprovalMcpTool(schema_hash=None)
    llm = MockLLMClient()
    llm.stream_responses = [
        LLMResponse(content="blocked", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
    )
    _queue_mcp_approval_resume(agent, tool, schema_hash="schema-v1")

    events = [event async for event in agent.run_agui("session-a", "resume-missing-schema")]

    assert tool.execute_count == 0
    result = next(event for event in events if event.type == EventType.TOOL_CALL_RESULT)
    assert "schema" in result.content.lower()


@pytest.mark.asyncio
async def test_claimed_mcp_approval_blocks_after_endpoint_or_credential_change(tmp_path):
    tool = _ApprovalMcpTool(
        schema_hash="schema-v1",
        connection_fingerprint="connection-v1",
    )
    llm = MockLLMClient()
    llm.stream_responses = [
        LLMResponse(content="blocked", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
    )
    _queue_mcp_approval_resume(agent, tool, schema_hash="schema-v1")
    tool.live_connection_fingerprint = "connection-v2"

    events = [
        event async for event in agent.run_agui(
            "session-a",
            "resume-changed-connection",
        )
    ]

    assert tool.execute_count == 0
    result = next(event for event in events if event.type == EventType.TOOL_CALL_RESULT)
    assert "endpoint or credential changed" in result.content


def test_ask_creation_rejects_schema_snapshot_from_old_connection(tmp_path):
    tool = _ApprovalMcpTool(
        schema_hash="schema-v1",
        connection_fingerprint="connection-v1",
    )
    tool.live_connection_fingerprint = "connection-v2"
    agent = Agent(
        llm_client=MockLLMClient(),
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )

    with pytest.raises(RuntimeError, match="schema or endpoint/credential binding is stale"):
        agent._create_tool_approval(
            tool=tool,
            decision=SimpleNamespace(
                effect="ask",
                reason="confirmation required",
                matched_rule_id=None,
            ),
            session_id="session-a",
            run_id="run-a",
            tool_call_id="call-a",
            arguments={"param1": "value"},
        )


def test_ask_creation_validates_full_mcp_schema_before_persisting(tmp_path):
    runtime = MagicMock()
    snapshot = RuntimeMcpToolSnapshot(
        installation_id="installation-1",
        server_id="server-1",
        server_name="Strict Tools",
        source="personal",
        raw_name="StrictTool",
        model_name="mcp__server__StrictTool",
        description="Strict remote tool",
        input_schema={
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["safe"]}},
            "required": ["mode"],
            "additionalProperties": False,
        },
        schema_hash="schema-v1",
        connection_fingerprint="connection-v1",
    )
    tool = McpRemoteTool(user_id="alice", snapshot=snapshot, runtime=runtime)
    agent = Agent(
        llm_client=MockLLMClient(),
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )

    with pytest.raises(ValueError, match="input schema"):
        agent._create_tool_approval(
            tool=tool,
            decision=SimpleNamespace(
                effect="ask",
                reason="confirmation required",
                matched_rule_id=None,
            ),
            session_id="session-a",
            run_id="run-a",
            tool_call_id="call-a",
            arguments={"mode": "unsafe", "unexpected": True},
        )

    runtime.current_execution_fingerprint.assert_not_called()


@pytest.mark.asyncio
async def test_claimed_approval_blocks_tool_that_became_hidden(tmp_path):
    tool = _ApprovalMcpTool(schema_hash="schema-v1", exposure=ToolExposure.DIRECT)
    llm = MockLLMClient()
    llm.stream_responses = [
        LLMResponse(content="blocked", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
    )
    _queue_mcp_approval_resume(agent, tool, schema_hash="schema-v1")
    tool.exposure = ToolExposure.HIDDEN

    events = [event async for event in agent.run_agui("session-a", "resume-hidden")]

    assert tool.execute_count == 0
    result = next(event for event in events if event.type == EventType.TOOL_CALL_RESULT)
    assert "not executed" in result.content.lower()


@pytest.mark.asyncio
async def test_exact_claimed_deferred_mcp_approval_can_cold_resume_without_discovery(
    tmp_path,
):
    tool = _ApprovalMcpTool(
        schema_hash="schema-v1",
        exposure=ToolExposure.DEFERRED,
    )
    llm = MockLLMClient()
    llm.stream_responses = [
        LLMResponse(content="done", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
    )
    _queue_mcp_approval_resume(agent, tool, schema_hash="schema-v1")
    assert not agent._activated_deferred_tools

    events = [event async for event in agent.run_agui("session-a", "resume-cold")]

    assert tool.execute_count == 1
    result = next(event for event in events if event.type == EventType.TOOL_CALL_RESULT)
    assert result.content == "Mock tool executed"
    assert not agent._activated_deferred_tools


@pytest.mark.asyncio
async def test_approved_execution_finalizes_request_without_resolution_row(tmp_path):
    tool = MockTool("approved_builtin")
    agent = Agent(
        llm_client=MockLLMClient(),
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )
    agent.queue_tool_approval_resume(
        request_id="approval-without-resolution",
        tool_call_id="call-approved",
        function_name=tool.name,
        arguments={"param1": "value"},
        provider="builtin",
        tool_name=tool.name,
        server_id=None,
        installation_id=None,
        schema_hash=None,
        resolution="allow_once",
        should_execute=True,
        claim_token="claim-without-resolution",
    )
    decision = SimpleNamespace(effect="allow", reason="allowed", matched_rule_id=None)
    db = MagicMock()
    db.__enter__.return_value = db
    db.query.return_value.filter.return_value.first.return_value = None

    with (
        patch.object(agent, "_resolve_tool_permission", return_value=decision),
        patch.object(agent, "_record_permission_audit"),
        patch("src.api.models.database.SessionLocal", return_value=db),
        patch(
            "src.api.services.tool_permission_service.renew_approval_execution_lease",
            return_value=True,
        ),
        patch(
            "src.api.services.tool_permission_service.finish_approval_request"
        ) as finish,
    ):
        record = await agent._execute_pending_approved_tool(
            thread_id="session-a",
            run_id="resume-a",
            cancel_token=None,
        )

    assert record is not None and record.result.success is True
    finish.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_approved_execution_renews_lease_until_tool_finishes(tmp_path):
    import asyncio

    tool = _BlockingApprovalTool()
    agent = Agent(
        llm_client=MockLLMClient(),
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )
    agent.queue_tool_approval_resume(
        request_id="approval-heartbeat",
        tool_call_id="call-heartbeat",
        function_name=tool.name,
        arguments={"param1": "value"},
        provider="builtin",
        tool_name=tool.name,
        server_id=None,
        installation_id=None,
        schema_hash=None,
        resolution="allow_once",
        should_execute=True,
        claim_token="claim-heartbeat",
    )
    decision = SimpleNamespace(effect="allow", reason="allowed", matched_rule_id=None)
    db = MagicMock()
    db.__enter__.return_value = db
    db.query.return_value.filter.return_value.first.return_value = None

    with (
        patch.object(agent, "_resolve_tool_permission", return_value=decision),
        patch.object(agent, "_record_permission_audit"),
        patch("src.api.models.database.SessionLocal", return_value=db),
        patch(
            "src.api.config.get_settings",
            return_value=SimpleNamespace(tool_approval_lease_heartbeat_seconds=0.01),
        ),
        patch(
            "src.api.services.tool_permission_service.renew_approval_execution_lease",
            return_value=True,
        ) as renew,
        patch(
            "src.api.services.tool_permission_service.finish_approval_request"
        ) as finish,
    ):
        task = asyncio.create_task(agent._execute_pending_approved_tool(
            thread_id="session-a",
            run_id="resume-heartbeat",
            cancel_token=None,
        ))
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await asyncio.sleep(0.035)
        tool.release.set()
        record = await task

    assert record is not None and record.result.success is True
    assert renew.call_count >= 2
    assert all(
        call.kwargs["claim_token"] == "claim-heartbeat"
        for call in renew.call_args_list
    )
    assert finish.call_args.kwargs["claim_token"] == "claim-heartbeat"


@pytest.mark.asyncio
async def test_reconciled_approval_cannot_dispatch_after_lease_is_lost(
    tmp_path,
    permission_db,
):
    tool = MockTool("expired_claim_tool")
    request = create_approval_request(
        permission_db,
        request_id="approval-expired-before-dispatch",
        user_id="alice",
        session_id="session-a",
        run_id="original-run",
        tool_call_id="call-expired-before-dispatch",
        ref=ToolRef(provider="builtin", tool_name=tool.name),
        model_tool_name=tool.name,
        arguments={"param1": "value"},
    )
    claim = claim_approval_request(
        permission_db,
        request_id=request.id,
        user_id="alice",
        resolution="allow_once",
    )
    claim.request.execution_lease_expires_at = now_naive() - timedelta(seconds=1)
    permission_db.commit()
    assert reconcile_expired_approval_leases(permission_db) == 1

    agent = Agent(
        llm_client=MockLLMClient(),
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )
    agent.queue_tool_approval_resume(
        request_id=request.id,
        tool_call_id=request.tool_call_id,
        function_name=tool.name,
        arguments={"param1": "value"},
        provider="builtin",
        tool_name=tool.name,
        server_id=None,
        installation_id=None,
        schema_hash=None,
        resolution="allow_once",
        should_execute=True,
        claim_token=claim.claim_token,
    )
    session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=permission_db.get_bind(),
    )
    with (
        patch("src.api.models.database.SessionLocal", session_factory),
        patch.object(agent, "_record_permission_audit") as audit,
        patch(
            "src.api.services.tool_permission_service.finish_approval_request",
            wraps=finish_approval_request,
        ) as finish,
    ):
        record = await agent._execute_pending_approved_tool(
            thread_id="session-a",
            run_id="resume-after-reconcile",
            cancel_token=None,
        )

    assert record is not None
    assert record.result.success is False
    assert "lease was lost" in record.result_content
    assert tool.execute_count == 0
    audit.assert_not_called()
    finish.assert_not_called()
    permission_db.expire_all()
    persisted = permission_db.query(ToolApprovalRequest).filter(
        ToolApprovalRequest.id == request.id
    ).one()
    assert persisted.status == "unknown"
    assert persisted.error == APPROVAL_OUTCOME_UNKNOWN_ERROR


@pytest.mark.asyncio
async def test_live_approval_renews_before_dispatch_and_finishes_normally(
    tmp_path,
    permission_db,
):
    tool = MockTool("live_claim_tool")
    request = create_approval_request(
        permission_db,
        request_id="approval-live-before-dispatch",
        user_id="alice",
        session_id="session-a",
        run_id="original-run",
        tool_call_id="call-live-before-dispatch",
        ref=ToolRef(provider="builtin", tool_name=tool.name),
        model_tool_name=tool.name,
        arguments={"param1": "value"},
    )
    claim = claim_approval_request(
        permission_db,
        request_id=request.id,
        user_id="alice",
        resolution="allow_once",
    )
    agent = Agent(
        llm_client=MockLLMClient(),
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )
    agent.queue_tool_approval_resume(
        request_id=request.id,
        tool_call_id=request.tool_call_id,
        function_name=tool.name,
        arguments={"param1": "value"},
        provider="builtin",
        tool_name=tool.name,
        server_id=None,
        installation_id=None,
        schema_hash=None,
        resolution="allow_once",
        should_execute=True,
        claim_token=claim.claim_token,
    )
    session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=permission_db.get_bind(),
    )
    decision = SimpleNamespace(effect="allow", reason="allowed", matched_rule_id=None)
    with (
        patch("src.api.models.database.SessionLocal", session_factory),
        patch.object(agent, "_resolve_tool_permission", return_value=decision),
        patch.object(agent, "_record_permission_audit"),
    ):
        record = await agent._execute_pending_approved_tool(
            thread_id="session-a",
            run_id="resume-live",
            cancel_token=None,
        )

    assert record is not None and record.result.success is True
    assert tool.execute_count == 1
    permission_db.expire_all()
    persisted = permission_db.query(ToolApprovalRequest).filter(
        ToolApprovalRequest.id == request.id
    ).one()
    assert persisted.status == "executed"


@pytest.mark.asyncio
async def test_uncertain_approved_result_finishes_and_audits_unknown(tmp_path):
    tool = _UncertainApprovalTool("uncertain_approved_tool")
    agent = Agent(
        llm_client=MockLLMClient(),
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
    )
    agent.queue_tool_approval_resume(
        request_id="approval-unknown",
        tool_call_id="call-unknown",
        function_name=tool.name,
        arguments={"param1": "value"},
        provider="builtin",
        tool_name=tool.name,
        server_id=None,
        installation_id=None,
        schema_hash=None,
        resolution="allow_once",
        should_execute=True,
        claim_token="claim-unknown",
    )
    decision = SimpleNamespace(effect="allow", reason="allowed", matched_rule_id=None)
    db = MagicMock()
    db.__enter__.return_value = db
    db.query.return_value.filter.return_value.first.return_value = None

    with (
        patch.object(agent, "_resolve_tool_permission", return_value=decision),
        patch.object(agent, "_record_permission_audit") as audit,
        patch("src.api.models.database.SessionLocal", return_value=db),
        patch(
            "src.api.services.tool_permission_service.finish_approval_request"
        ) as finish,
    ):
        record = await agent._execute_pending_approved_tool(
            thread_id="session-a",
            run_id="resume-unknown",
            cancel_token=None,
        )

    assert record is not None and record.result.outcome_uncertain is True
    audit.assert_called_once()
    assert audit.call_args.kwargs["outcome"] == "unknown"
    assert finish.call_args.kwargs["outcome_uncertain"] is True
    assert finish.call_args.kwargs["claim_token"] == "claim-unknown"


@pytest.mark.asyncio
async def test_subagent_mode_hides_ask_tools_and_never_creates_approval(tmp_path):
    tool = MockTool("remote_like_tool")
    llm = MockLLMClient()
    llm.stream_responses = [
        _tool_call_response(tool.name),
        LLMResponse(content="unavailable", tool_calls=[], finish_reason="stop"),
    ]
    agent = Agent(
        llm_client=llm,
        system_prompt="test",
        tools=[tool],
        workspace_dir=str(tmp_path / "workspace"),
        user_id="alice",
        allow_human_interrupts=False,
    )
    ask = SimpleNamespace(
        effect="ask",
        reason="confirmation required",
        matched_rule_id="rule-ask",
    )

    with (
        patch.object(agent, "_visible_tools_for_request", return_value=[]),
        patch.object(agent, "_resolve_tool_permission", return_value=ask),
        patch.object(agent, "_create_tool_approval") as create_approval,
        patch.object(agent, "_record_permission_audit"),
    ):
        events = [event async for event in agent.run_agui("session-a", "child-run")]

    assert tool.execute_count == 0
    create_approval.assert_not_called()
    assert not agent.has_pending_interrupt()
    result = next(event for event in events if event.type == EventType.TOOL_CALL_RESULT)
    assert result.content == "Tool is unavailable in this conversation"


def test_resume_tool_result_event_is_not_replayed_as_orphan_history_message():
    events = [
        SimpleNamespace(payload={
            "type": "CUSTOM",
            "name": "tool_approval_resume",
            "value": {"toolCallId": "call-1"},
        }),
        SimpleNamespace(payload={
            "type": "TOOL_CALL_RESULT",
            "toolCallId": "call-1",
            "content": "executed",
        }),
        SimpleNamespace(payload={
            "type": "TEXT_MESSAGE_CONTENT",
            "delta": "done",
        }),
        SimpleNamespace(payload={"type": "STEP_FINISHED"}),
    ]

    messages = AgentService._events_to_messages(events, round_id="resume-1")

    assert [(message.role, message.content) for message in messages] == [
        ("assistant", "done")
    ]
