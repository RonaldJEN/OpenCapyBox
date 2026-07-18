"""MCP catalog API and persistence tests."""

import json
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_current_admin_user, get_current_user
from src.api.config import get_settings
from src.api.models.auth_user import AuthUser
from src.api.models.database import Base, get_db
from src.api.models.mcp import (
    McpConfigVersion,
    McpCredential,
    McpInstallation,
    McpServer,
    McpToolSearchIndex,
    McpToolSnapshot,
    McpToolVisibility,
)
from src.api.routes import admin_mcp, mcp
from src.api.schemas.mcp import AdminMcpServerPatch
from src.api.services import mcp_service as mcp_service_module
from src.api.services.mcp_runtime import (
    McpRequiredServerUnavailable,
    McpRuntime,
    McpToolSnapshot as RuntimeMcpToolSnapshot,
    _SqlAlchemyMcpRepository,
    mcp_tool_schema_hash,
    resolve_effective_mcp_installation,
)
from src.api.services.mcp_service import (
    _config_version_upsert_statement,
    _get_or_create_installation,
    _provision_required_installation_if_current,
    bump_config_version,
    resolve_installation_headers,
)
from src.api.services.mcp_security import McpSecurityError
from src.api.services.mcp_tool_search_service import (
    McpToolSearchIndexTarget,
    McpToolSearchService,
    SqlAlchemyMcpToolSearchRepository,
    _prepare_candidate,
    sync_mcp_tool_search_indexes,
)
from src.api.services.embedding_service import EmbeddingRequestConfig
from src.api.utils.timezone import now_naive
from src.agent.tools.tool_discovery import ToolSearchDocument
from tests.db_safety import (
    build_pytest_pg_engine,
    create_all_for_test_engine,
    reset_all_tables,
)

_PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture()
def mcp_client():
    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    create_all_for_test_engine(engine, Base.metadata)
    reset_all_tables(engine, Base.metadata)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with SessionLocal() as db:
        db.add_all([
            AuthUser(user_id="admin", username="admin", enabled=True, is_admin=True),
            AuthUser(user_id="alice", username="alice", enabled=True),
            AuthUser(user_id="bob", username="bob", enabled=True),
        ])
        db.commit()

    app = FastAPI()
    app.include_router(admin_mcp.router, prefix="/admin/mcp")
    app.include_router(mcp.router, prefix="/mcp")

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_admin_user] = lambda: "admin"
    app.dependency_overrides[get_current_user] = lambda: "alice"
    with TestClient(app) as client:
        client.SessionLocal = SessionLocal  # type: ignore[attr-defined]
        yield client
    reset_all_tables(engine, Base.metadata)
    engine.dispose()


def _create_official(client: TestClient, **overrides):
    payload = {
        "name": "official-search",
        "description": "Official search MCP",
        "url": "https://93.184.216.34/mcp",
        "status": "published",
        "auth_type": "bearer",
        "bearer_token": "platform-secret-token",
    }
    payload.update(overrides)
    response = client.post("/admin/mcp/servers", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_healthy_required_official(client: TestClient, **overrides):
    overrides.setdefault("auth_type", "none")
    overrides.setdefault("bearer_token", None)
    overrides["required"] = True
    overrides["status"] = "draft"
    created = _create_official(client, **overrides)
    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=([], 3)),
    ):
        tested = client.post(f"/admin/mcp/servers/{created['id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True
    published = client.patch(
        f"/admin/mcp/servers/{created['id']}",
        json={"status": "published"},
    )
    assert published.status_code == 200, published.text
    return published.json()


def test_models_are_registered_in_metadata():
    expected = {
        "mcp_servers",
        "mcp_credentials",
        "mcp_installations",
        "mcp_tool_visibility",
        "mcp_tool_snapshots",
        "mcp_tool_search_indexes",
        "mcp_config_versions",
    }
    assert expected.issubset(Base.metadata.tables)


def test_tool_search_index_sync_retains_and_invalidates_vectors(mcp_client: TestClient):
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        server = McpServer(
            source="personal",
            owner_user_id="alice",
            name="market-data",
            description="market capabilities",
            url="https://example.com/mcp",
            status="published",
            auth_type="none",
        )
        db.add(server)
        db.flush()
        installation = McpInstallation(
            server_id=server.id,
            user_id="alice",
            enabled=True,
        )
        db.add(installation)
        db.flush()
        target = McpToolSearchIndexTarget(
            installation_id=installation.id,
            tool_name="quotes",
            server_name=server.name,
            server_description=server.description,
            title="Realtime quotes",
            description="Intraday market snapshot",
            schema_hash="schema-1",
            connection_fingerprint="connection-1",
        )
        sync_mcp_tool_search_indexes(
            db,
            installation_id=installation.id,
            targets=[target],
        )
        db.flush()
        row = db.query(McpToolSearchIndex).one()
        row.embedding = [1.0, 0.0, 0.0]
        row.embedding_model_fingerprint = "model-1"
        row.embedded_document_hash = row.search_document_hash
        db.commit()

        sync_mcp_tool_search_indexes(
            db,
            installation_id=installation.id,
            targets=[target],
        )
        db.commit()
        retained = db.query(McpToolSearchIndex).one()
        assert retained.embedding is not None
        assert retained.embedding_model_fingerprint == "model-1"

        changed = McpToolSearchIndexTarget(
            **{**target.__dict__, "description": "Changed capability"}
        )
        sync_mcp_tool_search_indexes(
            db,
            installation_id=installation.id,
            targets=[changed],
        )
        db.commit()
        invalidated = db.query(McpToolSearchIndex).one()
        assert invalidated.embedding is None
        assert invalidated.embedding_model_fingerprint is None
        assert invalidated.embedded_document_hash is None

        sync_mcp_tool_search_indexes(
            db,
            installation_id=installation.id,
            targets=[],
        )
        db.commit()
        assert db.query(McpToolSearchIndex).count() == 0


def test_tool_search_index_claims_are_leased_retriable_and_fenced(
    mcp_client: TestClient,
):
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        server = McpServer(
            source="personal",
            owner_user_id="alice",
            name="market-data",
            url="https://example.com/mcp",
            status="published",
            auth_type="none",
        )
        db.add(server)
        db.flush()
        installation = McpInstallation(
            server_id=server.id,
            user_id="alice",
            enabled=True,
        )
        db.add(installation)
        db.flush()
        installation_id = str(installation.id)
        target = McpToolSearchIndexTarget(
            installation_id=installation_id,
            tool_name="quotes",
            server_name=server.name,
            server_description="market capabilities",
            title="Realtime quotes",
            description="Intraday market snapshot",
            schema_hash="schema-1",
            connection_fingerprint="connection-1",
        )
        sync_mcp_tool_search_indexes(
            db,
            installation_id=installation_id,
            targets=[target],
        )
        db.commit()

    candidate = _prepare_candidate(ToolSearchDocument(
        model_name="mcp__market__quotes",
        provider="mcp",
        tool_name="quotes",
        installation_id=installation_id,
        server_name="market-data",
        server_description="market capabilities",
        title="Realtime quotes",
        description="Intraday market snapshot",
        schema_hash="schema-1",
        connection_fingerprint="connection-1",
    ))
    repository = SqlAlchemyMcpToolSearchRepository(
        mcp_client.SessionLocal  # type: ignore[attr-defined]
    )

    first = repository.claim_missing([candidate], model_fingerprint="model-1")
    assert len(first) == 1
    assert repository.claim_missing(
        [candidate], model_fingerprint="model-1"
    ) == []

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        row = db.query(McpToolSearchIndex).one()
        row.lease_expires_at = now_naive() - timedelta(seconds=1)
        db.commit()
    reclaimed = repository.claim_missing([candidate], model_fingerprint="model-1")
    assert len(reclaimed) == 1
    assert reclaimed[0].claim_token != first[0].claim_token

    changed_target = McpToolSearchIndexTarget(
        **{**target.__dict__, "description": "Changed capability"}
    )
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        sync_mcp_tool_search_indexes(
            db,
            installation_id=installation_id,
            targets=[changed_target],
        )
        db.commit()
    repository.finalize_claims(
        reclaimed,
        [[1.0, 0.0, 0.0]],
        model_fingerprint="model-1",
    )
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        fenced = db.query(McpToolSearchIndex).one()
        assert fenced.embedding is None
        assert fenced.claim_token is None

    changed_candidate = _prepare_candidate(ToolSearchDocument(
        **{
            **candidate.document.__dict__,
            "description": "Changed capability",
        }
    ))
    failed = repository.claim_missing(
        [changed_candidate],
        model_fingerprint="model-1",
    )
    assert len(failed) == 1
    repository.finalize_claims(
        failed,
        [None],
        model_fingerprint="model-1",
    )
    assert repository.claim_missing(
        [changed_candidate],
        model_fingerprint="model-1",
    ) == []
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        retrying = db.query(McpToolSearchIndex).one()
        assert retrying.claim_token is None
        assert retrying.retry_after is not None


@pytest.mark.asyncio
async def test_pgvector_tool_search_is_restricted_to_current_candidates(
    mcp_client: TestClient,
):
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        alice_server = McpServer(
            source="personal",
            owner_user_id="alice",
            name="alice-market",
            url="https://alice.example/mcp",
            status="published",
            auth_type="none",
        )
        bob_server = McpServer(
            source="personal",
            owner_user_id="bob",
            name="bob-market",
            url="https://bob.example/mcp",
            status="published",
            auth_type="none",
        )
        db.add_all([alice_server, bob_server])
        db.flush()
        alice_installation = McpInstallation(
            server_id=alice_server.id,
            user_id="alice",
            enabled=True,
        )
        bob_installation = McpInstallation(
            server_id=bob_server.id,
            user_id="bob",
            enabled=True,
        )
        db.add_all([alice_installation, bob_installation])
        db.flush()

        alice_targets = [
            McpToolSearchIndexTarget(
                installation_id=alice_installation.id,
                tool_name=name,
                server_name=alice_server.name,
                server_description="",
                title=name,
                description=description,
                schema_hash=f"schema-{name}",
                connection_fingerprint="alice-connection",
            )
            for name, description in (
                ("close", "intraday market snapshot"),
                ("far", "company filing archive"),
                ("hidden", "hidden closest vector"),
            )
        ]
        bob_target = McpToolSearchIndexTarget(
            installation_id=bob_installation.id,
            tool_name="bob-private",
            server_name=bob_server.name,
            server_description="",
            title="bob-private",
            description="private closest vector",
            schema_hash="schema-bob-private",
            connection_fingerprint="bob-connection",
        )
        sync_mcp_tool_search_indexes(
            db,
            installation_id=alice_installation.id,
            targets=alice_targets,
        )
        sync_mcp_tool_search_indexes(
            db,
            installation_id=bob_installation.id,
            targets=[bob_target],
        )
        db.flush()
        for row in db.query(McpToolSearchIndex).all():
            if row.tool_name not in {"hidden", "bob-private"}:
                continue
            row.embedding = [1.0, 0.0, 0.0]
            row.embedding_model_fingerprint = "embedding-fp"
            row.embedded_document_hash = row.search_document_hash
        alice_installation_id = str(alice_installation.id)
        alice_server_name = str(alice_server.name)
        db.commit()

    candidates = [
        ToolSearchDocument(
            model_name=name,
            provider="mcp",
            tool_name=name,
            installation_id=alice_installation_id,
            server_name=alice_server_name,
            server_description="",
            title=name,
            description=description,
            schema_hash=f"schema-{name}",
            connection_fingerprint="alice-connection",
        )
        for name, description in (
            ("close", "intraday market snapshot"),
            ("far", "company filing archive"),
        )
    ]
    config = EmbeddingRequestConfig(
        identity="embedding-fp",
        api_key="secret",
        api_base="https://embedding.example/v1",
        model_name="embedding-model",
        dimensions=3,
    )
    async def embed(texts, _config):
        return [
            [1.0, 0.0, 0.0]
            if text == "semantic-only" or "tool: close" in text
            else [0.0, 1.0, 0.0]
            for text in texts
        ]

    provider = AsyncMock(side_effect=embed)
    repository = SqlAlchemyMcpToolSearchRepository(
        mcp_client.SessionLocal  # type: ignore[attr-defined]
    )
    service = McpToolSearchService(
        repository,
        embedding_provider=provider,
        config_provider=lambda: config,
    )

    ranked = await service.rank("semantic-only", candidates, limit=10)

    assert ranked == ["close"]
    assert provider.await_count == 2
    request_sizes = [len(call.args[0]) for call in provider.await_args_list]
    assert sorted(request_sizes) == [1, 2]
    assert repository.vector_ranking(
        [1.0, 0.0, 0.0],
        [_prepare_candidate(candidate) for candidate in candidates],
        model_fingerprint="embedding-fp",
        min_score=0.25,
    ) == ["close"]
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        indexed = {
            row.tool_name: row
            for row in db.query(McpToolSearchIndex)
            .filter(McpToolSearchIndex.installation_id == alice_installation_id)
            .all()
        }
        assert indexed["close"].embedding_model_fingerprint == "embedding-fp"
        assert indexed["far"].embedding_model_fingerprint == "embedding-fp"


def test_config_version_bump_uses_atomic_upsert_expression(mcp_client: TestClient):
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        bump_config_version(db, "alice")
        bump_config_version(db, "alice")
        db.commit()
        assert db.query(McpConfigVersion).filter_by(scope_key="user:alice").one().version == 2

        class PostgreSQLBind:
            dialect = postgresql.dialect()

        class PostgreSQLSession:
            @staticmethod
            def get_bind():
                return PostgreSQLBind()

        statement = _config_version_upsert_statement(PostgreSQLSession(), "global")
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT" in sql
        assert "mcp_config_versions.version +" in sql


def test_config_version_bump_is_lossless_under_concurrency():
    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    create_all_for_test_engine(engine, Base.metadata)
    reset_all_tables(engine, Base.metadata)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def increment_many():
        for _ in range(12):
            with SessionLocal() as db:
                bump_config_version(db, "alice")
                db.commit()

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _index: increment_many(), range(3)))

    with SessionLocal() as db:
        assert db.query(McpConfigVersion).filter_by(scope_key="user:alice").one().version == 36
    reset_all_tables(engine, Base.metadata)
    engine.dispose()


def test_admin_crud_never_returns_secret(mcp_client: TestClient):
    created = _create_official(mcp_client)
    assert created["source"] == "official"
    assert created["credential_set"] is True
    assert created["header_names"] == ["Authorization"]
    assert "platform-secret-token" not in str(created)

    listed = mcp_client.get("/admin/mcp/servers")
    assert listed.status_code == 200
    assert listed.json()["servers"][0]["id"] == created["id"]
    assert "platform-secret-token" not in listed.text

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=([], 2)),
    ):
        tested = mcp_client.post(f"/admin/mcp/servers/{created['id']}/test")
    assert tested.json()["ok"] is True

    patched = mcp_client.patch(
        f"/admin/mcp/servers/{created['id']}",
        json={"description": "updated", "required": True},
    )
    assert patched.status_code == 200
    assert patched.json()["description"] == "updated"
    assert patched.json()["required"] is True

    deleted = mcp_client.delete(f"/admin/mcp/servers/{created['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"id": created["id"], "deleted": True}


def test_user_catalog_redacts_platform_only_official_connection_details(
    mcp_client: TestClient,
):
    official = _create_official(
        mcp_client,
        name="official-query-secret",
        url="https://93.184.216.34/platform/mcp?api_key=TOPSECRET",
        auth_type="headers",
        bearer_token=None,
        headers={"X-Platform-Key": "platform-header-secret"},
    )
    personal_response = mcp_client.post(
        "/mcp/servers",
        json={
            "name": "personal-query-secret",
            "url": "https://93.184.216.34/personal/mcp?api_key=MYSECRET",
            "auth_type": "headers",
            "headers": {"X-Personal-Key": "personal-header-secret"},
        },
    )
    assert personal_response.status_code == 201, personal_response.text
    personal = personal_response.json()

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        db.query(McpServer).filter(McpServer.id == official["id"]).update(
            {McpServer.last_error: "safe platform diagnostic"}
        )
        db.query(McpServer).filter(McpServer.id == personal["id"]).update(
            {McpServer.last_error: "personal diagnostic"}
        )
        db.commit()

    user_catalog = mcp_client.get("/mcp/servers")
    assert user_catalog.status_code == 200
    by_id = {server["id"]: server for server in user_catalog.json()["servers"]}
    user_official = by_id[official["id"]]
    assert user_official["url"] == "https://93.184.216.34/platform/mcp"
    assert user_official["header_names"] == []
    assert user_official["last_error"] is None
    assert "TOPSECRET" not in user_catalog.text
    assert "safe platform diagnostic" not in user_catalog.text

    user_personal = by_id[personal["id"]]
    assert user_personal["url"].endswith("?api_key=MYSECRET")
    assert user_personal["header_names"] == ["X-Personal-Key"]
    assert user_personal["last_error"] == "personal diagnostic"

    admin_catalog = mcp_client.get("/admin/mcp/servers")
    assert admin_catalog.status_code == 200
    admin_official = next(
        server
        for server in admin_catalog.json()["servers"]
        if server["id"] == official["id"]
    )
    assert admin_official["url"].endswith("?api_key=TOPSECRET")
    assert admin_official["header_names"] == ["X-Platform-Key"]
    assert admin_official["last_error"] == "safe platform diagnostic"


def test_official_user_override_exposes_only_users_own_header_names(
    mcp_client: TestClient,
):
    official = _create_official(
        mcp_client,
        name="official-user-override-headers",
        auth_type="headers",
        bearer_token=None,
        headers={"X-Platform-Key": "platform-secret"},
    )

    before = next(
        server
        for server in mcp_client.get("/mcp/servers").json()["servers"]
        if server["id"] == official["id"]
    )
    assert before["header_names"] == []

    connected = mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={
            "enabled": True,
            "auth_type": "headers",
            "headers": {"X-User-Key": "alice-secret"},
        },
    )
    assert connected.status_code == 200, connected.text
    assert connected.json()["header_names"] == ["X-User-Key"]
    assert "X-Platform-Key" not in connected.text


def test_admin_probe_error_uses_shared_bounded_redaction(mcp_client: TestClient):
    endpoint = "https://93.184.216.34/mcp?api_key=TOPSECRET"
    official = _create_official(
        mcp_client,
        name="official-safe-probe-errors",
        url=endpoint,
        auth_type="headers",
        bearer_token=None,
        headers={"X-Platform-Key": "HEADERSECRET"},
    )
    reflected_error = ConnectionError(
        f"request to {endpoint} sent HEADERSECRET and Bearer REFLECTEDTOKEN"
        "\r\n\x00forged-line "
        + ("z" * 1000)
    )

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(side_effect=reflected_error),
    ):
        response = mcp_client.post(f"/admin/mcp/servers/{official['id']}/test")

    assert response.status_code == 200
    error = response.json()["error"]
    assert response.json()["ok"] is False
    assert "TOPSECRET" not in error
    assert "HEADERSECRET" not in error
    assert "REFLECTEDTOKEN" not in error
    assert endpoint not in error
    assert "\r" not in error and "\n" not in error and "\x00" not in error
    assert "REDACTED" in error
    assert len(error) <= 500

    listed = mcp_client.get("/admin/mcp/servers")
    persisted_error = next(
        server["last_error"]
        for server in listed.json()["servers"]
        if server["id"] == official["id"]
    )
    assert persisted_error == error


def test_admin_probe_persists_discovered_tool_count(mcp_client: TestClient):
    created = _create_official(
        mcp_client,
        name="official-probe-count",
        auth_type="none",
        bearer_token=None,
    )
    remote_tools = [
        {
            "name": "search",
            "description": "Search",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "read",
            "description": "Read",
            "inputSchema": {"type": "object"},
        },
    ]
    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=(remote_tools, 7)),
    ):
        tested = mcp_client.post(f"/admin/mcp/servers/{created['id']}/test")
    assert tested.status_code == 200
    assert tested.json()["tools_count"] == 2

    listed = mcp_client.get("/admin/mcp/servers")
    server = next(
        item for item in listed.json()["servers"] if item["id"] == created["id"]
    )
    assert server["tools_count"] == 2
    assert server["enabled_tools_count"] == 2


def test_personal_server_is_owner_scoped_and_public_https_only(mcp_client: TestClient):
    rejected = mcp_client.post(
        "/mcp/servers",
        json={"name": "internal", "url": "http://127.0.0.1:8080/mcp"},
    )
    assert rejected.status_code == 400

    created = mcp_client.post(
        "/mcp/servers",
        json={
            "name": "alice-tools",
            "url": "https://93.184.216.34/mcp",
            "auth_type": "headers",
            "headers": {"X-API-Key": "alice-secret"},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["source"] == "personal"
    assert body["enabled"] is True
    assert body["credential_set"] is True
    assert body["header_names"] == ["X-API-Key"]
    assert "alice-secret" not in created.text

    exported = mcp_client.get("/mcp/export")
    assert exported.status_code == 200
    exported_config = exported.json()["mcpServers"]["alice-tools"]
    assert exported_config["type"] == "streamable-http"
    assert "headers" not in exported_config
    assert "alice-secret" not in exported.text

    # Switch auth identity to Bob; Alice's personal ID must not be addressable.
    mcp_client.app.dependency_overrides[get_current_user] = lambda: "bob"
    hidden = mcp_client.patch(
        f"/mcp/servers/{body['id']}",
        json={"description": "stolen"},
    )
    assert hidden.status_code == 404


def test_catalog_fingerprint_refresh_bucket_only_applies_with_effective_installations(
    mcp_client: TestClient,
):
    now = [10.0]
    repository = _SqlAlchemyMcpRepository(
        mcp_client.SessionLocal,  # type: ignore[attr-defined]
        catalog_refresh_seconds=300,
        clock=lambda: now[0],
    )

    empty_first = repository.catalog_fingerprint("alice")
    now[0] = 610.0
    assert repository.catalog_fingerprint("alice") == empty_first

    created = mcp_client.post(
        "/mcp/servers",
        json={
            "name": "periodically-refreshed",
            "url": "https://93.184.216.34/mcp",
            "auth_type": "none",
        },
    )
    assert created.status_code == 201, created.text

    now[0] = 10.0
    bucket_zero = repository.catalog_fingerprint("alice")
    now[0] = 299.0
    assert repository.catalog_fingerprint("alice") == bucket_zero
    now[0] = 300.0
    assert repository.catalog_fingerprint("alice") != bucket_zero


def test_effective_installation_carries_configured_routing_description(
    mcp_client: TestClient,
):
    created = mcp_client.post(
        "/mcp/servers",
        json={
            "name": "同花顺股票 MCP",
            "description": "A 股实时行情、个股资料、财务和公告",
            "url": "https://93.184.216.34/stock-mcp",
        },
    )
    assert created.status_code == 201, created.text

    repository = _SqlAlchemyMcpRepository(
        mcp_client.SessionLocal,  # type: ignore[attr-defined]
    )
    installation = next(
        item
        for item in repository.list_effective_installations("alice")
        if item.server_id == created.json()["id"]
    )

    assert installation.server_name == "同花顺股票 MCP"
    assert installation.server_description == "A 股实时行情、个股资料、财务和公告"


def test_official_connection_uses_user_secret_without_overwriting_platform_secret(
    mcp_client: TestClient,
):
    official = _create_official(mcp_client)
    connected = mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={
            "enabled": True,
            "auth_type": "bearer",
            "bearer_token": "alice-override-token",
        },
    )
    assert connected.status_code == 200, connected.text
    assert connected.json()["enabled"] is True
    assert "alice-override-token" not in connected.text

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        credentials = (
            db.query(McpCredential)
            .filter(McpCredential.server_id == official["id"])
            .order_by(McpCredential.user_id.asc())
            .all()
        )
        assert {row.user_id for row in credentials} == {None, "alice"}
        assert all("token" not in row.encrypted_secret for row in credentials)
        installation = db.query(McpInstallation).filter_by(
            server_id=official["id"], user_id="alice"
        ).one()
        assert installation.credential_id in {row.id for row in credentials if row.user_id == "alice"}


def test_official_origin_change_drops_platform_and_all_user_credentials(
    mcp_client: TestClient,
):
    official = _create_official(mcp_client, name="origin-bound-secret")
    connected = mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={
            "enabled": True,
            "auth_type": "bearer",
            "bearer_token": "alice-old-origin-token",
        },
    )
    assert connected.status_code == 200, connected.text

    changed = mcp_client.patch(
        f"/admin/mcp/servers/{official['id']}",
        json={"url": "https://93.184.216.35/mcp"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["credential_set"] is False

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(McpCredential).filter_by(server_id=official["id"]).count() == 0
        installation = db.query(McpInstallation).filter_by(
            server_id=official["id"], user_id="alice"
        ).one()
        assert installation.credential_id is None
        with pytest.raises(McpSecurityError, match="尚未配置"):
            resolve_installation_headers(db, "alice", installation.id)


def test_admin_server_patch_rejects_null_for_required_columns(
    mcp_client: TestClient,
):
    official = _create_official(mcp_client, name="admin-null-patch")
    for field_name in (
        "name",
        "url",
        "status",
        "auth_type",
        "allow_private_network",
        "allow_insecure_http",
        "required",
    ):
        response = mcp_client.patch(
            f"/admin/mcp/servers/{official['id']}",
            json={field_name: None},
        )
        assert response.status_code == 422, (field_name, response.text)

    blank_name = mcp_client.patch(
        f"/admin/mcp/servers/{official['id']}",
        json={"name": "   "},
    )
    assert blank_name.status_code == 422, blank_name.text


def test_personal_server_patch_rejects_null_for_required_or_forbidden_columns(
    mcp_client: TestClient,
):
    created = mcp_client.post(
        "/mcp/servers",
        json={
            "name": "personal-null-patch",
            "url": "https://93.184.216.34/mcp",
            "auth_type": "none",
        },
    )
    assert created.status_code == 201, created.text
    server_id = created.json()["id"]

    for field_name in ("name", "url", "auth_type", "enabled", "status"):
        response = mcp_client.patch(
            f"/mcp/servers/{server_id}",
            json={field_name: None},
        )
        assert response.status_code == 422, (field_name, response.text)

    blank_name = mcp_client.patch(
        f"/mcp/servers/{server_id}",
        json={"name": "   "},
    )
    assert blank_name.status_code == 422, blank_name.text


def test_official_origin_change_clears_old_secrets_before_installing_new_platform_secret(
    mcp_client: TestClient,
):
    official = _create_official(mcp_client, name="origin-rotated-secret")
    connected = mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={
            "enabled": True,
            "auth_type": "bearer",
            "bearer_token": "alice-old-origin-token",
        },
    )
    assert connected.status_code == 200, connected.text

    changed = mcp_client.patch(
        f"/admin/mcp/servers/{official['id']}",
        json={
            "url": "https://93.184.216.34:8443/mcp",
            "bearer_token": "new-platform-origin-token",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["credential_set"] is True

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        credentials = db.query(McpCredential).filter_by(server_id=official["id"]).all()
        assert len(credentials) == 1
        assert credentials[0].user_id is None
        installation = db.query(McpInstallation).filter_by(
            server_id=official["id"], user_id="alice"
        ).one()
        assert installation.credential_id is None
        assert resolve_installation_headers(db, "alice", installation.id) == {
            "Authorization": "Bearer new-platform-origin-token"
        }


def test_official_path_change_on_same_effective_origin_preserves_credentials(
    mcp_client: TestClient,
):
    official = _create_official(mcp_client, name="same-origin-path")
    connected = mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={
            "enabled": True,
            "auth_type": "bearer",
            "bearer_token": "alice-same-origin-token",
        },
    )
    assert connected.status_code == 200, connected.text

    changed = mcp_client.patch(
        f"/admin/mcp/servers/{official['id']}",
        json={"url": "https://93.184.216.34:443/other-mcp-path"},
    )
    assert changed.status_code == 200, changed.text

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(McpCredential).filter_by(server_id=official["id"]).count() == 2
        installation = db.query(McpInstallation).filter_by(
            server_id=official["id"], user_id="alice"
        ).one()
        assert resolve_installation_headers(db, "alice", installation.id) == {
            "Authorization": "Bearer alice-same-origin-token"
        }


def test_personal_origin_change_drops_stored_credential(mcp_client: TestClient):
    created = mcp_client.post(
        "/mcp/servers",
        json={
            "name": "personal-origin-secret",
            "url": "https://93.184.216.34/mcp",
            "auth_type": "bearer",
            "bearer_token": "alice-personal-old-token",
        },
    ).json()
    assert created["credential_set"] is True

    # A hostname change moves the credential to a different trust boundary and
    # must detach it before the new origin can ever be contacted.
    changed = mcp_client.patch(
        f"/mcp/servers/{created['id']}",
        json={"url": "https://93.184.216.35/mcp"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["credential_set"] is False

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(McpCredential).filter_by(server_id=created["id"]).count() == 0
        installation = db.query(McpInstallation).filter_by(
            server_id=created["id"], user_id="alice"
        ).one()
        assert installation.credential_id is None
        with pytest.raises(McpSecurityError, match="尚未配置"):
            resolve_installation_headers(db, "alice", installation.id)


def test_personal_path_change_on_same_effective_origin_preserves_credential(
    mcp_client: TestClient,
):
    created = mcp_client.post(
        "/mcp/servers",
        json={
            "name": "personal-same-origin",
            "url": "https://93.184.216.34:443/mcp",
            "auth_type": "bearer",
            "bearer_token": "alice-personal-keep-token",
        },
    ).json()
    assert created["credential_set"] is True

    # A path-only change stays within the same scheme/host/effective-port and
    # keeps the credential.
    changed = mcp_client.patch(
        f"/mcp/servers/{created['id']}",
        json={"url": "https://93.184.216.34/other-path"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["credential_set"] is True

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        installation = db.query(McpInstallation).filter_by(
            server_id=created["id"], user_id="alice"
        ).one()
        assert resolve_installation_headers(db, "alice", installation.id) == {
            "Authorization": "Bearer alice-personal-keep-token"
        }


def test_user_probe_race_never_stamps_last_tested_at(mcp_client: TestClient):
    created = mcp_client.post(
        "/mcp/servers",
        json={"name": "probe-race-stamp", "url": "https://93.184.216.34/original"},
    ).json()
    assert created["last_tested_at"] is None

    async def change_target_during_probe(_server, _credential):
        with mcp_client.SessionLocal() as other:  # type: ignore[attr-defined]
            row = other.query(McpServer).filter(McpServer.id == created["id"]).one()
            row.url = "https://93.184.216.34/changed"
            row.version = int(row.version or 0) + 1
            bump_config_version(other, "alice")
            other.commit()
        return [{"name": "search", "inputSchema": {"type": "object"}}], 8

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(side_effect=change_target_during_probe),
    ):
        response = mcp_client.post(f"/mcp/servers/{created['id']}/test")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "配置在测试期间已变化" in response.json()["error"]
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        row = db.query(McpServer).filter(McpServer.id == created["id"]).one()
        assert row.last_tested_at is None
        assert db.query(McpToolSnapshot).count() == 0


def test_user_probe_snapshot_writer_serializes_with_official_config_update(
    mcp_client: TestClient,
):
    """A user probe cannot publish an old snapshot after an admin edit commits.

    The probe has already completed remote I/O when the snapshot hook runs.  A
    concurrent administrator update must block on the probe's server row lock
    until the snapshot transaction commits.  This is the gap the user quota
    lock alone cannot close for official servers, because admin writes do not
    acquire an individual user's quota row.
    """

    official = _create_official(
        mcp_client,
        name="official-user-probe-race",
        auth_type="none",
        bearer_token=None,
    )
    connected = mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={"enabled": True, "auth_type": "none"},
    )
    assert connected.status_code == 200, connected.text
    remote_tools = [{"name": "search", "inputSchema": {"type": "object"}}]
    snapshot_writer_entered = Event()
    admin_server_lock_attempted = Event()
    release_snapshot_writer = Event()
    original_save_tool_snapshots = mcp_service_module._save_tool_snapshots
    original_official_server = mcp_service_module._official_server

    async def probe(_server, _credential):
        return remote_tools, 6

    def gated_save_tool_snapshots(*args, **kwargs):
        snapshot_writer_entered.set()
        assert admin_server_lock_attempted.wait(timeout=5)
        assert release_snapshot_writer.wait(timeout=5)
        return original_save_tool_snapshots(*args, **kwargs)

    def observed_official_server(*args, **kwargs):
        if kwargs.get("lock"):
            admin_server_lock_attempted.set()
        return original_official_server(*args, **kwargs)

    def update_official_server():
        with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
            return mcp_service_module.update_admin_server(
                db,
                official["id"],
                AdminMcpServerPatch(url="https://93.184.216.35/changed"),
            )

    with (
        patch(
            "src.api.services.mcp_service.probe_mcp_server",
            new=AsyncMock(side_effect=probe),
        ),
        patch(
            "src.api.services.mcp_service._save_tool_snapshots",
            side_effect=gated_save_tool_snapshots,
        ),
        patch(
            "src.api.services.mcp_service._official_server",
            side_effect=observed_official_server,
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        probe_future = executor.submit(
            mcp_client.post,
            f"/mcp/servers/{official['id']}/test",
        )
        assert snapshot_writer_entered.wait(timeout=5)
        admin_future = executor.submit(update_official_server)
        assert admin_server_lock_attempted.wait(timeout=5)
        try:
            # The admin request has reached SELECT ... FOR UPDATE but cannot
            # pass it while the probe holds the same server row through commit.
            with pytest.raises(FuturesTimeoutError):
                admin_future.result(timeout=0.5)
        finally:
            release_snapshot_writer.set()
        probe_response = probe_future.result(timeout=5)
        updated_official = admin_future.result(timeout=5)

    assert probe_response.status_code == 200, probe_response.text
    assert probe_response.json() == {
        "ok": True,
        "tools_count": 1,
        "latency_ms": 6,
        "error": None,
    }
    assert updated_official["url"] == "https://93.184.216.35/changed"

    repository = _SqlAlchemyMcpRepository(mcp_client.SessionLocal)  # type: ignore[attr-defined]
    current = next(
        item
        for item in repository.list_effective_installations("alice")
        if item.server_id == official["id"]
    )
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        snapshot = db.query(McpToolSnapshot).one()
        # The admin edit is ordered after the probe commit.  Its new target
        # makes the earlier snapshot stale, so runtime will not execute it; the
        # important invariant is that the old writer cannot commit *after* the
        # new target and masquerade as a current discovery result.
        assert snapshot.connection_fingerprint != current.execution_fingerprint


def test_user_probe_snapshot_failure_stamps_error_and_keeps_lock(mcp_client: TestClient):
    # When snapshot persistence fails, the write is rolled back inside a
    # SAVEPOINT so the outer transaction and its user-quota lock survive; the
    # probe still stamps last_error against the same locked config and never
    # leaves partial snapshots (spec §4.5). A bare rollback would drop the lock.
    created = mcp_client.post(
        "/mcp/servers",
        json={"name": "probe-snapshot-fail", "url": "https://93.184.216.34/snap"},
    ).json()

    async def probe(_server, _credential):
        return [{"name": "search", "inputSchema": {"type": "object"}}], 5

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(side_effect=probe),
    ), patch(
        "src.api.services.mcp_service._save_tool_snapshots",
        side_effect=RuntimeError("snapshot write failed"),
    ):
        response = mcp_client.post(f"/mcp/servers/{created['id']}/test")

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert "配置在测试期间已变化" not in (response.json()["error"] or "")
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        row = db.query(McpServer).filter(McpServer.id == created["id"]).one()
        assert row.last_tested_at is not None
        assert row.last_error is not None
        assert db.query(McpToolSnapshot).count() == 0


def test_import_is_partial_and_export_omits_credentials(mcp_client: TestClient):
    response = mcp_client.post(
        "/mcp/import",
        json={
            "mcpServers": {
                "valid": {
                    "type": "streamable-http",
                    "url": "https://93.184.216.34/mcp",
                    "headers": {"Authorization": "Bearer imported-secret"},
                },
                "invalid": {
                    "type": "stdio",
                    "command": "npx",
                },
            }
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 1
    assert len(body["errors"]) == 1
    assert "imported-secret" not in response.text
    assert "imported-secret" not in mcp_client.get("/mcp/export").text


def test_import_duplicate_does_not_poison_following_valid_entry(mcp_client: TestClient):
    existing = mcp_client.post(
        "/mcp/servers",
        json={"name": "duplicate", "url": "https://93.184.216.34/existing"},
    )
    assert existing.status_code == 201

    imported = mcp_client.post(
        "/mcp/import",
        json={
            "mcpServers": {
                "duplicate": {
                    "type": "streamable-http",
                    "url": "https://93.184.216.34/duplicate",
                },
                "valid-after-duplicate": {
                    "type": "streamable-http",
                    "url": "https://93.184.216.34/valid",
                },
            }
        },
    )

    assert imported.status_code == 200, imported.text
    assert imported.json()["imported"] == 1
    assert len(imported.json()["errors"]) == 1
    assert imported.json()["servers"][0]["name"] == "valid-after-duplicate"


def test_import_item_is_atomic_when_visibility_write_fails(mcp_client: TestClient):
    # A failure while writing the publication policy must not leave an orphaned
    # server behind (spec §5 import failure mode). The whole item rolls back.
    with patch(
        "src.api.services.mcp_service.update_tool_visibility",
        side_effect=RuntimeError("visibility write failed"),
    ):
        imported = mcp_client.post(
            "/mcp/import",
            json={
                "mcpServers": {
                    "atomic-item": {
                        "type": "streamable-http",
                        "url": "https://93.184.216.34/atomic",
                        "disabled_tools": ["blocked"],
                    }
                }
            },
        )

    assert imported.status_code == 200, imported.text
    assert imported.json()["imported"] == 0
    assert len(imported.json()["errors"]) == 1
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert (
            db.query(McpServer)
            .filter(McpServer.name == "atomic-item")
            .count()
            == 0
        )

    # The rolled-back name is free, so a clean re-import succeeds.
    retried = mcp_client.post(
        "/mcp/import",
        json={
            "mcpServers": {
                "atomic-item": {
                    "type": "streamable-http",
                    "url": "https://93.184.216.34/atomic",
                }
            }
        },
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["imported"] == 1


def test_personal_create_and_import_respect_total_server_quota(
    mcp_client: TestClient,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "mcp_personal_server_limit", 2)
    first = mcp_client.post(
        "/mcp/servers",
        json={"name": "quota-one", "url": "https://93.184.216.34/one"},
    )
    assert first.status_code == 201

    imported = mcp_client.post(
        "/mcp/import",
        json={
            "mcpServers": {
                "quota-two": {"type": "streamable-http", "url": "https://93.184.216.34/two"},
                "quota-three": {"type": "streamable-http", "url": "https://93.184.216.34/three"},
            }
        },
    )
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1
    assert "上限（2）" in imported.json()["errors"][0]["error"]

    direct = mcp_client.post(
        "/mcp/servers",
        json={"name": "quota-four", "url": "https://93.184.216.34/four"},
    )
    assert direct.status_code == 409
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert _personal_server_count_for_test(db, "alice") == 2


def _personal_server_count_for_test(db, user_id: str) -> int:
    return db.query(McpServer).filter(
        McpServer.source == "personal",
        McpServer.owner_user_id == user_id,
    ).count()


def test_import_export_round_trip_preserves_tool_publication_policy(
    mcp_client: TestClient,
):
    imported = mcp_client.post(
        "/mcp/import",
        json={
            "mcpServers": {
                "policy-round-trip": {
                    "type": "streamable-http",
                    "url": "https://93.184.216.34/mcp",
                    "enabled_tools": ["search", "futureWrite"],
                    "disabled_tools": ["delete", "futureDangerousTool"],
                },
                "publish-none": {
                    "type": "streamable-http",
                    "url": "https://93.184.216.34/none/mcp",
                    "enabled_tools": [],
                },
            }
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["imported"] == 2
    imported_server = next(
        server
        for server in imported.json()["servers"]
        if server["name"] == "policy-round-trip"
    )
    assert imported_server["enabled_tools"] == ["futureWrite", "search"]
    assert imported_server["disabled_tools"] == ["delete", "futureDangerousTool"]

    alice_export = mcp_client.get("/mcp/export")
    assert alice_export.status_code == 200
    exported_config = alice_export.json()["mcpServers"]["policy-round-trip"]
    assert exported_config["enabled_tools"] == ["futureWrite", "search"]
    assert exported_config["disabled_tools"] == ["delete", "futureDangerousTool"]
    assert alice_export.json()["mcpServers"]["publish-none"]["enabled_tools"] == []

    # Import the exported file for another owner. Unknown/future tool names
    # must survive without requiring a successful discovery first.
    mcp_client.app.dependency_overrides[get_current_user] = lambda: "bob"
    bob_import = mcp_client.post("/mcp/import", json=alice_export.json())
    assert bob_import.status_code == 200, bob_import.text
    assert bob_import.json()["imported"] == 2
    bob_server = next(
        server
        for server in bob_import.json()["servers"]
        if server["name"] == "policy-round-trip"
    )
    assert bob_server["enabled_tools"] == ["futureWrite", "search"]
    assert bob_server["disabled_tools"] == ["delete", "futureDangerousTool"]
    assert (
        mcp_client.get("/mcp/export").json()["mcpServers"]["policy-round-trip"]
        == exported_config
    )


def test_user_probe_persists_tool_snapshot_without_echoing_secret(mcp_client: TestClient):
    created = mcp_client.post(
        "/mcp/servers",
        json={"name": "probe", "url": "https://93.184.216.34/mcp"},
    ).json()
    before_version = mcp_client.get("/mcp/servers").json()["config_version"]
    remote_tools = [{
        "name": "search",
        "description": "Search",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    }]
    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=(remote_tools, 12)),
    ):
        response = mcp_client.post(f"/mcp/servers/{created['id']}/test")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "tools_count": 1, "latency_ms": 12, "error": None}
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        snapshot = db.query(McpToolSnapshot).one()
        search_index = db.query(McpToolSearchIndex).one()
        assert snapshot.tool_name == "search"
        assert snapshot.connection_fingerprint
        assert snapshot.schema_hash == mcp_tool_schema_hash(
            raw_name="search",
            description="Search",
            input_schema=remote_tools[0]["inputSchema"],
            annotations=remote_tools[0]["annotations"],
        )
        assert search_index.schema_hash == snapshot.schema_hash
        assert (
            search_index.connection_fingerprint
            == snapshot.connection_fingerprint
        )
        assert "tool: search" in search_index.search_document
        assert "93.184.216.34" not in search_index.search_document
    assert mcp_client.get("/mcp/servers").json()["config_version"] != before_version


def test_user_probe_of_official_server_does_not_overwrite_platform_health(
    mcp_client: TestClient,
):
    official = _create_official(
        mcp_client,
        name="shared-health",
        auth_type="none",
        bearer_token=None,
    )
    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=([], 4)),
    ):
        assert mcp_client.post(f"/admin/mcp/servers/{official['id']}/test").json()["ok"] is True
    before = next(
        item for item in mcp_client.get("/admin/mcp/servers").json()["servers"]
        if item["id"] == official["id"]
    )

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(side_effect=ConnectionError("user network failed")),
    ):
        user_test = mcp_client.post(f"/mcp/servers/{official['id']}/test")
    assert user_test.json()["ok"] is False
    after = next(
        item for item in mcp_client.get("/admin/mcp/servers").json()["servers"]
        if item["id"] == official["id"]
    )
    assert after["last_tested_at"] == before["last_tested_at"]
    assert after["last_error"] is None


def test_user_probe_discards_result_when_target_changes_during_remote_await(
    mcp_client: TestClient,
):
    created = mcp_client.post(
        "/mcp/servers",
        json={"name": "probe-race", "url": "https://93.184.216.34/original"},
    ).json()
    remote_tools = [{"name": "search", "inputSchema": {"type": "object"}}]

    async def change_target_during_probe(_server, _credential):
        # This independent write would deadlock/fail if the request retained
        # its server-row lock or write transaction across the remote await.
        with mcp_client.SessionLocal() as other:  # type: ignore[attr-defined]
            row = other.query(McpServer).filter(McpServer.id == created["id"]).one()
            row.url = "https://93.184.216.34/changed"
            row.version = int(row.version or 0) + 1
            bump_config_version(other, "alice")
            other.commit()
        return remote_tools, 8

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(side_effect=change_target_during_probe),
    ):
        response = mcp_client.post(f"/mcp/servers/{created['id']}/test")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "配置在测试期间已变化" in response.json()["error"]
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(McpToolSnapshot).count() == 0


@pytest.mark.parametrize("raw_name", [" delete ", "*", "delete\x00all"])
def test_user_probe_rejects_ambiguous_or_reserved_tool_name(
    mcp_client: TestClient,
    raw_name: str,
):
    created = mcp_client.post(
        "/mcp/servers",
        json={"name": "ambiguous-probe", "url": "https://93.184.216.34/mcp"},
    ).json()
    remote_tools = [{"name": raw_name, "inputSchema": {"type": "object"}}]

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=(remote_tools, 4)),
    ):
        response = mcp_client.post(f"/mcp/servers/{created['id']}/test")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"]
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(McpToolSnapshot).count() == 0


@pytest.mark.parametrize(
    "remote_tool",
    [
        {"name": "x" * 256, "inputSchema": {"type": "object"}},
        {"name": "tool", "title": "x" * 256, "inputSchema": {"type": "object"}},
    ],
)
def test_user_probe_rejects_metadata_over_db_column_limits(
    mcp_client: TestClient,
    remote_tool,
):
    created = mcp_client.post(
        "/mcp/servers",
        json={"name": "oversized-probe", "url": "https://93.184.216.34/mcp"},
    ).json()

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=([remote_tool], 4)),
    ):
        response = mcp_client.post(f"/mcp/servers/{created['id']}/test")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "255-character" in response.json()["error"]
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(McpToolSnapshot).count() == 0


def test_runtime_snapshot_replace_bumps_generation_only_on_identity_change(
    mcp_client: TestClient,
):
    created = mcp_client.post(
        "/mcp/servers",
        json={"name": "runtime-refresh", "url": "https://93.184.216.34/mcp"},
    ).json()
    repository = _SqlAlchemyMcpRepository(mcp_client.SessionLocal)  # type: ignore[attr-defined]
    installation = next(
        item
        for item in repository.list_effective_installations("alice")
        if item.server_id == created["id"]
    )

    def current_version():
        with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
            return int(
                db.query(McpConfigVersion)
                .filter(McpConfigVersion.scope_key == "user:alice")
                .one()
                .version
            )

    def snapshot(schema_hash, *, title=None):
        return RuntimeMcpToolSnapshot(
            installation_id=installation.installation_id,
            server_id=installation.server_id,
            server_name=installation.server_name,
            source=installation.source,
            raw_name="search",
            model_name="mcp__runtime__search",
            title=title,
            description="Search",
            input_schema={"type": "object"},
            schema_hash=schema_hash,
            connection_fingerprint=installation.execution_fingerprint,
        )

    before = current_version()
    first = snapshot("a" * 64)
    assert repository.replace_tool_snapshots(installation, [first]) is True
    after_first = current_version()
    assert after_first == before + 1
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        first_discovered_at = db.query(McpToolSnapshot).one().discovered_at
        search_index = db.query(McpToolSearchIndex).one()
        search_index.embedding = [1.0, 0.0, 0.0]
        search_index.embedding_model_fingerprint = "model-1"
        search_index.embedded_document_hash = search_index.search_document_hash
        db.commit()
    assert repository.get_tool_snapshot_binding(installation, "search") == (
        "a" * 64,
        installation.execution_fingerprint,
    )

    assert repository.replace_tool_snapshots(installation, [first]) is True
    assert current_version() == after_first
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(McpToolSnapshot).one().discovered_at > first_discovered_at
        assert db.query(McpToolSearchIndex).one().embedding is not None

    assert repository.replace_tool_snapshots(
        installation,
        [snapshot("a" * 64, title="Search tool")],
    ) is True
    assert current_version() == after_first + 1
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(McpToolSnapshot).one().title == "Search tool"
        changed_index = db.query(McpToolSearchIndex).one()
        assert changed_index.embedding is None
        assert "title: Search tool" in changed_index.search_document

    assert repository.replace_tool_snapshots(
        installation,
        [snapshot("b" * 64)],
    ) is True
    assert current_version() == after_first + 2

    assert repository.replace_tool_snapshots(installation, []) is True
    assert current_version() == after_first + 3
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(McpToolSearchIndex).count() == 0


def test_runtime_snapshot_replace_rejects_stale_execution_target(
    mcp_client: TestClient,
):
    created = mcp_client.post(
        "/mcp/servers",
        json={"name": "runtime-cas", "url": "https://93.184.216.34/v1"},
    ).json()
    repository = _SqlAlchemyMcpRepository(mcp_client.SessionLocal)  # type: ignore[attr-defined]
    original = next(
        item
        for item in repository.list_effective_installations("alice")
        if item.server_id == created["id"]
    )

    changed_response = mcp_client.patch(
        f"/mcp/servers/{created['id']}",
        json={"url": "https://93.184.216.34/v2"},
    )
    assert changed_response.status_code == 200, changed_response.text
    current = next(
        item
        for item in repository.list_effective_installations("alice")
        if item.server_id == created["id"]
    )
    assert current.execution_fingerprint != original.execution_fingerprint

    def snapshot(installation, raw_name, schema_hash):
        return RuntimeMcpToolSnapshot(
            installation_id=installation.installation_id,
            server_id=installation.server_id,
            server_name=installation.server_name,
            source=installation.source,
            raw_name=raw_name,
            model_name=f"mcp__runtime__{raw_name}",
            description=raw_name,
            input_schema={"type": "object"},
            schema_hash=schema_hash,
            connection_fingerprint=installation.execution_fingerprint,
        )

    fresh = snapshot(current, "fresh", "f" * 64)
    assert repository.replace_tool_snapshots(current, [fresh]) is True
    stale = snapshot(original, "stale", "s" * 64)
    assert repository.replace_tool_snapshots(original, [stale]) is False

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        rows = db.query(McpToolSnapshot).all()
        assert [(row.tool_name, row.connection_fingerprint) for row in rows] == [
            ("fresh", current.execution_fingerprint)
        ]


def test_admin_probe_failure_is_reported_as_data(mcp_client: TestClient):
    server = _create_official(mcp_client)
    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(side_effect=RuntimeError("platform-secret-token connection refused")),
    ):
        response = mcp_client.post(f"/admin/mcp/servers/{server['id']}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "connection refused" in response.json()["error"]
    assert "platform-secret-token" not in response.text


def test_required_publish_requires_successful_probe_of_current_configuration(
    mcp_client: TestClient,
):
    one_step = mcp_client.post(
        "/admin/mcp/servers",
        json={
            "name": "unsafe-one-step",
            "url": "https://93.184.216.34/unsafe",
            "status": "published",
            "auth_type": "none",
            "required": True,
        },
    )
    assert one_step.status_code == 409
    assert "测试成功后才能发布" in one_step.json()["detail"]

    draft = _create_official(
        mcp_client,
        name="required-health-gate",
        auth_type="none",
        bearer_token=None,
        required=True,
        status="draft",
    )
    untested = mcp_client.patch(
        f"/admin/mcp/servers/{draft['id']}",
        json={"status": "published"},
    )
    assert untested.status_code == 409

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=([], 7)),
    ):
        assert mcp_client.post(f"/admin/mcp/servers/{draft['id']}/test").json()["ok"] is True
    published = mcp_client.patch(
        f"/admin/mcp/servers/{draft['id']}",
        json={"status": "published"},
    )
    assert published.status_code == 200

    changed_while_published = mcp_client.patch(
        f"/admin/mcp/servers/{draft['id']}",
        json={"url": "https://93.184.216.34/changed"},
    )
    assert changed_while_published.status_code == 409
    assert "当前配置" in changed_while_published.json()["detail"]

    disabled_and_changed = mcp_client.patch(
        f"/admin/mcp/servers/{draft['id']}",
        json={
            "status": "disabled",
            "url": "https://93.184.216.34/changed",
        },
    )
    assert disabled_and_changed.status_code == 200
    assert disabled_and_changed.json()["last_tested_at"] is None
    assert mcp_client.patch(
        f"/admin/mcp/servers/{draft['id']}",
        json={"status": "published"},
    ).status_code == 409

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=([], 5)),
    ):
        assert mcp_client.post(f"/admin/mcp/servers/{draft['id']}/test").json()["ok"] is True
    assert mcp_client.patch(
        f"/admin/mcp/servers/{draft['id']}",
        json={"status": "published"},
    ).status_code == 200


def test_failed_probe_disables_published_required_server(mcp_client: TestClient):
    required = _create_healthy_required_official(
        mcp_client,
        name="required-failure-disable",
    )
    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(side_effect=ConnectionError("offline")),
    ):
        failed = mcp_client.post(f"/admin/mcp/servers/{required['id']}/test")
    assert failed.json()["ok"] is False
    current = next(
        item for item in mcp_client.get("/admin/mcp/servers").json()["servers"]
        if item["id"] == required["id"]
    )
    assert current["status"] == "disabled"
    assert current["last_error"] == "offline"


def test_per_installation_tool_visibility_for_personal_and_official_connections(
    mcp_client: TestClient,
):
    remote_tools = [
        {
            "name": "readReport",
            "description": "Read report",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "deleteReport",
            "description": "Delete report",
            "inputSchema": {"type": "object"},
        },
    ]
    personal = mcp_client.post(
        "/mcp/servers",
        json={"name": "visibility-personal", "url": "https://93.184.216.34/mcp"},
    ).json()
    official = _create_official(
        mcp_client,
        name="visibility-official",
        auth_type="none",
        bearer_token=None,
    )
    connected = mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={"enabled": True, "auth_type": "none"},
    )
    assert connected.status_code == 200, connected.text

    with patch(
        "src.api.services.mcp_service.probe_mcp_server",
        new=AsyncMock(return_value=(remote_tools, 5)),
    ):
        assert mcp_client.post(f"/mcp/servers/{personal['id']}/test").status_code == 200
        assert mcp_client.post(f"/mcp/servers/{official['id']}/test").status_code == 200

    repository = _SqlAlchemyMcpRepository(mcp_client.SessionLocal)  # type: ignore[attr-defined]
    before_fingerprint = repository.catalog_fingerprint("alice")
    before_version = mcp_client.get("/mcp/servers").json()["config_version"]
    for server in (personal, official):
        listed = mcp_client.get(f"/mcp/servers/{server['id']}/tools")
        assert listed.status_code == 200, listed.text
        listed_body = listed.json()
        assert listed_body["enabled_tools_count"] == 2
        assert listed_body["enabled_tools"] is None
        assert listed_body["visibility_revision"] == 0
        assert all(tool["enabled"] for tool in listed_body["tools"])

        allowlist = (
            ["deleteReport", "readReport"]
            if server["id"] == personal["id"]
            else None
        )
        updated = mcp_client.put(
            f"/mcp/servers/{server['id']}/tools/visibility",
            json={
                "expected_revision": listed_body["visibility_revision"],
                "enabled_tools": allowlist,
                "disabled_tools": ["deleteReport", "futureDangerousTool"],
            },
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["disabled_tools"] == ["deleteReport", "futureDangerousTool"]
        assert body["enabled_tools"] == allowlist
        assert body["enabled_tools_count"] == 1
        assert body["visibility_revision"] == 1
        assert {tool["name"]: tool["enabled"] for tool in body["tools"]} == {
            "deleteReport": False,
            "readReport": True,
        }

        server_payload = next(
            item
            for item in mcp_client.get("/mcp/servers").json()["servers"]
            if item["id"] == server["id"]
        )
        assert server_payload["disabled_tools"] == ["deleteReport", "futureDangerousTool"]
        assert server_payload["enabled_tools"] == allowlist
        assert server_payload["tools_count"] == 2
        assert server_payload["enabled_tools_count"] == 1

    after_version = mcp_client.get("/mcp/servers").json()["config_version"]
    assert after_version != before_version
    assert repository.catalog_fingerprint("alice") != before_fingerprint
    effective_by_server = {
        item.server_id: item
        for item in repository.list_effective_installations("alice")
    }
    assert effective_by_server[personal["id"]].disabled_tools == frozenset({
        "deleteReport",
        "futureDangerousTool",
    })
    assert effective_by_server[personal["id"]].enabled_tools == frozenset({
        "readReport",
        "deleteReport",
    })
    assert effective_by_server[official["id"]].disabled_tools == frozenset({
        "deleteReport",
        "futureDangerousTool",
    })
    assert effective_by_server[official["id"]].enabled_tools is None
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        overrides = db.query(McpToolVisibility).all()
        assert len(overrides) == 2
        personal_override = next(
            row
            for row in overrides
            if row.installation_id == effective_by_server[personal["id"]].installation_id
        )
        assert json.loads(personal_override.enabled_tools_json) == [
            "deleteReport",
            "readReport",
        ]
        assert json.loads(personal_override.disabled_tools_json) == [
            "deleteReport",
            "futureDangerousTool",
        ]


def test_tool_visibility_is_owner_scoped_and_validates_exact_names(mcp_client: TestClient):
    personal = mcp_client.post(
        "/mcp/servers",
        json={"name": "private-visibility", "url": "https://93.184.216.34/mcp"},
    ).json()
    duplicate = mcp_client.put(
        f"/mcp/servers/{personal['id']}/tools/visibility",
        json={
            "expected_revision": 0,
            "disabled_tools": ["CaseSensitive", "CaseSensitive"],
        },
    )
    assert duplicate.status_code == 422

    ambiguous = mcp_client.put(
        f"/mcp/servers/{personal['id']}/tools/visibility",
        json={
            "expected_revision": 0,
            "disabled_tools": [" delete "],
        },
    )
    assert ambiguous.status_code == 422

    for invalid_name in ("*", "delete\x00all"):
        rejected = mcp_client.put(
            f"/mcp/servers/{personal['id']}/tools/visibility",
            json={"expected_revision": 0, "disabled_tools": [invalid_name]},
        )
        assert rejected.status_code == 422

    distinct_case = mcp_client.put(
        f"/mcp/servers/{personal['id']}/tools/visibility",
        json={
            "expected_revision": 0,
            "enabled_tools": [],
            "disabled_tools": ["CaseSensitive", "casesensitive"],
        },
    )
    assert distinct_case.status_code == 200
    assert distinct_case.json()["enabled_tools"] == []
    assert distinct_case.json()["disabled_tools"] == ["CaseSensitive", "casesensitive"]
    assert distinct_case.json()["visibility_revision"] == 1

    stale = mcp_client.put(
        f"/mcp/servers/{personal['id']}/tools/visibility",
        json={
            "expected_revision": 0,
            "enabled_tools": None,
            "disabled_tools": [],
        },
    )
    assert stale.status_code == 409
    assert "刷新后重试" in stale.json()["detail"]
    unchanged = mcp_client.get(f"/mcp/servers/{personal['id']}/tools").json()
    assert unchanged["visibility_revision"] == 1
    assert unchanged["enabled_tools"] == []

    reset_to_default = mcp_client.put(
        f"/mcp/servers/{personal['id']}/tools/visibility",
        json={
            "expected_revision": 1,
            "enabled_tools": None,
            "disabled_tools": [],
        },
    )
    assert reset_to_default.status_code == 200
    assert reset_to_default.json()["visibility_revision"] == 2
    assert reset_to_default.json()["enabled_tools"] is None

    mcp_client.app.dependency_overrides[get_current_user] = lambda: "bob"
    assert mcp_client.get(f"/mcp/servers/{personal['id']}/tools").status_code == 404
    assert mcp_client.put(
        f"/mcp/servers/{personal['id']}/tools/visibility",
        json={"expected_revision": 2, "disabled_tools": []},
    ).status_code == 404


def test_first_visibility_writers_share_one_installation_and_stale_writer_conflicts(
    mcp_client: TestClient,
):
    official = _create_official(
        mcp_client,
        name="first-visibility-write",
        auth_type="none",
        bearer_token=None,
    )
    initial = mcp_client.get(f"/mcp/servers/{official['id']}/tools").json()
    assert initial["installation_id"] is None
    assert initial["visibility_revision"] == 0

    first = mcp_client.put(
        f"/mcp/servers/{official['id']}/tools/visibility",
        json={"expected_revision": 0, "disabled_tools": ["delete"]},
    )
    stale = mcp_client.put(
        f"/mcp/servers/{official['id']}/tools/visibility",
        json={"expected_revision": 0, "disabled_tools": ["write"]},
    )

    assert first.status_code == 200, first.text
    assert stale.status_code == 409, stale.text
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        installations = db.query(McpInstallation).filter(
            McpInstallation.server_id == official["id"],
            McpInstallation.user_id == "alice",
        ).all()
        assert len(installations) == 1
        assert db.query(McpToolVisibility).filter(
            McpToolVisibility.installation_id == installations[0].id,
        ).count() == 1


def test_disabled_official_server_is_hidden_and_user_routes_never_probe(
    mcp_client: TestClient,
):
    official = _create_official(
        mcp_client,
        name="disabled-user-boundary",
        auth_type="none",
        bearer_token=None,
    )
    assert any(
        item["id"] == official["id"]
        for item in mcp_client.get("/mcp/servers").json()["servers"]
    )
    disabled = mcp_client.patch(
        f"/admin/mcp/servers/{official['id']}",
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200, disabled.text

    assert all(
        item["id"] != official["id"]
        for item in mcp_client.get("/mcp/servers").json()["servers"]
    )
    probe = AsyncMock(return_value=([], 1))
    with patch("src.api.services.mcp_service.probe_mcp_server", new=probe):
        assert mcp_client.post(f"/mcp/servers/{official['id']}/test").status_code == 404
    probe.assert_not_awaited()
    assert mcp_client.get(f"/mcp/servers/{official['id']}/tools").status_code == 404
    assert mcp_client.put(
        f"/mcp/servers/{official['id']}/tools/visibility",
        json={"expected_revision": 0, "disabled_tools": []},
    ).status_code == 404
    assert mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={"enabled": True},
    ).status_code == 404


def test_installation_creation_locks_stable_server_row_before_insert(
    mcp_client: TestClient,
):
    official = _create_official(
        mcp_client,
        name="installation-lock-order",
        auth_type="none",
        bearer_token=None,
    )
    events: list[str] = []
    session_class = mcp_client.SessionLocal.class_  # type: ignore[attr-defined]

    def capture_query(execute_state):
        if (
            execute_state.is_select
            and getattr(execute_state.statement, "_for_update_arg", None) is not None
        ):
            events.append("server-lock")

    def capture_flush(session, _flush_context, _instances):
        if any(isinstance(item, McpInstallation) for item in session.new):
            events.append("installation-insert")

    event.listen(session_class, "do_orm_execute", capture_query)
    event.listen(session_class, "before_flush", capture_flush)
    try:
        with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
            _get_or_create_installation(
                db,
                server_id=official["id"],
                user_id="alice",
            )
    finally:
        event.remove(session_class, "do_orm_execute", capture_query)
        event.remove(session_class, "before_flush", capture_flush)

    assert events.index("server-lock") < events.index("installation-insert")


def test_existing_installation_locks_server_before_user_credential_write(
    mcp_client: TestClient,
):
    official = _create_official(
        mcp_client,
        name="credential-origin-lock-order",
    )
    # Materialize the installation without creating a per-user credential.
    existing = mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={"enabled": False},
    )
    assert existing.status_code == 200, existing.text

    events: list[str] = []
    session_class = mcp_client.SessionLocal.class_  # type: ignore[attr-defined]

    def capture_query(execute_state):
        statement = execute_state.statement
        if (
            execute_state.is_select
            and getattr(statement, "_for_update_arg", None) is not None
            and "mcp_servers" in str(statement)
        ):
            events.append("server-lock")

    def capture_flush(session, _flush_context, _instances):
        if any(isinstance(item, McpCredential) for item in session.new):
            events.append("credential-insert")

    event.listen(session_class, "do_orm_execute", capture_query)
    event.listen(session_class, "before_flush", capture_flush)
    try:
        connected = mcp_client.put(
            f"/mcp/servers/{official['id']}/connection",
            json={
                "enabled": True,
                "auth_type": "bearer",
                "bearer_token": "alice-origin-bound-token",
            },
        )
    finally:
        event.remove(session_class, "do_orm_execute", capture_query)
        event.remove(session_class, "before_flush", capture_flush)

    assert connected.status_code == 200, connected.text
    assert events.index("server-lock") < events.index("credential-insert")


def test_required_official_server_is_auto_enabled_and_cannot_be_disabled(
    mcp_client: TestClient,
):
    required = _create_healthy_required_official(
        mcp_client,
        name="required-official",
    )

    # Runtime provisioning must not depend on opening the settings page first.
    repository = _SqlAlchemyMcpRepository(mcp_client.SessionLocal)  # type: ignore[attr-defined]
    bob_effective = repository.list_effective_installations("bob")
    assert any(item.server_id == required["id"] and item.required for item in bob_effective)

    listed = mcp_client.get("/mcp/servers")
    body = next(item for item in listed.json()["servers"] if item["id"] == required["id"])
    assert body["enabled"] is True
    assert body["installation_id"] is not None

    disabled = mcp_client.put(
        f"/mcp/servers/{required['id']}/connection",
        json={"enabled": False},
    )
    assert disabled.status_code == 409
    assert "不能停用" in disabled.json()["detail"]
    assert next(
        item for item in mcp_client.get("/mcp/servers").json()["servers"]
        if item["id"] == required["id"]
    )["enabled"] is True


def test_optional_connection_quota_excludes_required_official_servers(
    mcp_client: TestClient,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "mcp_user_enabled_connection_limit", 1)
    first = mcp_client.post(
        "/mcp/servers",
        json={"name": "enabled-one", "url": "https://93.184.216.34/one"},
    )
    second = mcp_client.post(
        "/mcp/servers",
        json={
            "name": "disabled-two",
            "url": "https://93.184.216.34/two",
            "enabled": False,
        },
    )
    assert first.status_code == 201
    assert second.status_code == 201

    rejected = mcp_client.put(
        f"/mcp/servers/{second.json()['id']}/connection",
        json={"enabled": True},
    )
    assert rejected.status_code == 409
    assert "启用数量" in rejected.json()["detail"]

    required = _create_healthy_required_official(
        mcp_client,
        name="required-outside-user-quota",
    )
    listed = mcp_client.get("/mcp/servers")
    required_body = next(
        item for item in listed.json()["servers"] if item["id"] == required["id"]
    )
    assert required_body["enabled"] is True


def test_disabled_optional_connection_keeps_its_quota_slot(
    mcp_client: TestClient,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "mcp_user_enabled_connection_limit", 1)
    official = _create_official(
        mcp_client,
        name="disabled-reserved-slot",
        auth_type="none",
        bearer_token=None,
    )
    enabled = mcp_client.put(
        f"/mcp/servers/{official['id']}/connection",
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert mcp_client.patch(
        f"/admin/mcp/servers/{official['id']}",
        json={"status": "disabled"},
    ).status_code == 200

    personal = mcp_client.post(
        "/mcp/servers",
        json={
            "name": "cannot-bypass-disabled-slot",
            "url": "https://93.184.216.34/personal",
            "enabled": False,
        },
    )
    assert personal.status_code == 201, personal.text
    rejected = mcp_client.put(
        f"/mcp/servers/{personal.json()['id']}/connection",
        json={"enabled": True},
    )
    assert rejected.status_code == 409
    assert "启用数量" in rejected.json()["detail"]


def test_required_official_server_quota_is_enforced(
    mcp_client: TestClient,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "mcp_required_official_server_limit", 1)
    first = _create_official(
        mcp_client,
        name="required-quota-one",
        auth_type="none",
        bearer_token=None,
        required=False,
        status="draft",
    )
    second = _create_official(
        mcp_client,
        name="required-quota-two",
        url="https://93.184.216.34/two",
        auth_type="none",
        bearer_token=None,
        required=False,
        status="draft",
    )

    first_update = mcp_client.patch(
        f"/admin/mcp/servers/{first['id']}",
        json={"required": True},
    )
    assert first_update.status_code == 200, first_update.text
    assert first_update.json()["required"] is True

    second_update = mcp_client.patch(
        f"/admin/mcp/servers/{second['id']}",
        json={"required": True},
    )
    assert second_update.status_code == 409
    assert "上限（1）" in second_update.json()["detail"]

    listed = mcp_client.get("/admin/mcp/servers").json()["servers"]
    required_by_id = {item["id"]: item["required"] for item in listed}
    assert required_by_id[first["id"]] is True
    assert required_by_id[second["id"]] is False


@pytest.mark.asyncio
async def test_required_official_server_without_credential_fails_closed(
    mcp_client: TestClient,
):
    required = _create_official(
        mcp_client,
        name="required-missing-credential",
        auth_type="bearer",
        bearer_token=None,
        required=True,
        status="draft",
    )
    # Simulate a legacy/corrupt row that predates the publish health gate.
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        row = db.query(McpServer).filter(McpServer.id == required["id"]).one()
        row.status = "published"
        db.commit()
    repository = _SqlAlchemyMcpRepository(mcp_client.SessionLocal)  # type: ignore[attr-defined]

    class Connector:
        called = False

        async def list_tools(self, _installation):
            self.called = True
            return []

        async def call_tool(self, _installation, _raw_name, _arguments):
            raise AssertionError("not reached")

    connector = Connector()
    with pytest.raises(McpRequiredServerUnavailable, match="credential"):
        await McpRuntime(repository=repository, connector=connector).resolve_catalog("alice")

    assert connector.called is False
    effective = repository.list_effective_installations("alice")
    item = next(item for item in effective if item.server_id == required["id"])
    assert item.configuration_error is not None


def test_caller_owned_effective_resolution_never_commits_pending_state(
    mcp_client: TestClient,
):
    required = _create_healthy_required_official(
        mcp_client,
        name="pure-effective-resolution",
    )
    listed = mcp_client.get("/mcp/servers").json()["servers"]
    installation_id = next(
        item["installation_id"] for item in listed if item["id"] == required["id"]
    )

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        alice = db.query(AuthUser).filter(AuthUser.user_id == "alice").one()
        alice.username = "must-rollback"
        resolved = resolve_effective_mcp_installation(
            db,
            user_id="alice",
            installation_id=installation_id,
        )
        assert resolved is not None
        db.rollback()

    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        assert db.query(AuthUser).filter(AuthUser.user_id == "alice").one().username == "alice"


def test_required_provisioning_rechecks_locked_server_state(
    mcp_client: TestClient,
):
    required = _create_healthy_required_official(
        mcp_client,
        name="required-race-recheck",
    )
    with mcp_client.SessionLocal() as db:  # type: ignore[attr-defined]
        stale = db.query(McpServer).filter(McpServer.id == required["id"]).one()
        assert stale.status == "published"
        db.query(McpServer).filter(McpServer.id == required["id"]).update(
            {McpServer.status: "disabled"},
            synchronize_session=False,
        )
        # The identity map still carries the initial catalog snapshot. The
        # provisioning helper must refresh it under the row lock and refuse.
        assert stale.status == "published"
        assert _provision_required_installation_if_current(
            db,
            server_id=required["id"],
            user_id="alice",
        ) is False
        assert db.query(McpInstallation).filter(
            McpInstallation.server_id == required["id"],
            McpInstallation.user_id == "alice",
        ).count() == 0
        db.rollback()
