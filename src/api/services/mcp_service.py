"""MCP catalog CRUD, safe serialization, import/export and connection probes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.auth_user import AuthUser
from src.api.models.mcp import (
    McpConfigVersion,
    McpCredential,
    McpInstallation,
    McpServer,
    McpToolSnapshot,
    McpToolVisibility,
)
from src.api.schemas.mcp import (
    AdminMcpServerCreate,
    AdminMcpServerPatch,
    McpConnectionUpdate,
    McpImportRequest,
    McpImportServer,
    McpToolVisibilityUpdate,
    UserMcpServerCreate,
    UserMcpServerPatch,
)
from src.api.services.mcp_security import (
    McpSecurityError,
    credential_header_names,
    credential_headers,
    credential_payload,
    encrypt_credential,
    mcp_url_without_query,
    sanitize_mcp_exception,
    validate_mcp_url,
)
from src.api.services.mcp_runtime import (
    mcp_execution_fingerprint,
    mcp_tool_schema_hash,
    validate_mcp_tool_metadata,
    validate_mcp_tool_name,
)
from src.api.services.secret_crypto import secret_fingerprint
from src.api.utils.timezone import now_naive


@dataclass(frozen=True)
class ResolvedMcpServer:
    server: McpServer
    installation: McpInstallation
    credential: McpCredential | None


@dataclass(frozen=True)
class _ProbeServerSnapshot:
    id: str
    source: str
    name: str
    url: str
    auth_type: str
    allow_private_network: bool
    allow_insecure_http: bool


@dataclass(frozen=True)
class _ProbeCredentialSnapshot:
    encrypted_secret: str


def _probe_server_snapshot(server: McpServer) -> _ProbeServerSnapshot:
    return _ProbeServerSnapshot(
        id=str(server.id),
        source=str(server.source),
        name=str(server.name),
        url=str(server.url),
        auth_type=str(server.auth_type),
        allow_private_network=bool(server.allow_private_network),
        allow_insecure_http=bool(server.allow_insecure_http),
    )


def _probe_credential_snapshot(
    credential: McpCredential | None,
) -> _ProbeCredentialSnapshot | None:
    if credential is None:
        return None
    return _ProbeCredentialSnapshot(encrypted_secret=str(credential.encrypted_secret))


def _probe_target_fingerprint(
    server: McpServer | _ProbeServerSnapshot,
    credential: McpCredential | _ProbeCredentialSnapshot | None,
    *,
    installation_id: str,
    user_id: str,
) -> str:
    return mcp_execution_fingerprint(
        installation_id=installation_id,
        server_id=str(server.id),
        user_id=user_id,
        url=str(server.url),
        auth_type=str(server.auth_type),
        credential_fingerprint=(
            secret_fingerprint(credential.encrypted_secret)
            if credential is not None
            else ""
        ),
        allow_private_network=bool(server.allow_private_network),
        allow_insecure_http=bool(server.allow_insecure_http),
    )


def _scope_key(user_id: str | None) -> str:
    return "global" if user_id is None else f"user:{user_id}"


def bump_config_version(db: DBSession, user_id: str | None) -> None:
    """Atomically create or increment a catalog generation."""

    key = _scope_key(user_id)
    db.execute(_config_version_upsert_statement(db, key))


def _config_version_upsert_statement(db: DBSession, key: str):
    values = {
        "scope_key": key,
        "version": 1,
        "updated_at": now_naive(),
    }
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        statement = postgresql_insert(McpConfigVersion).values(**values)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(McpConfigVersion).values(**values)
    else:  # pragma: no cover - production and tests use PostgreSQL/SQLite
        raise RuntimeError(f"unsupported database dialect for MCP version upsert: {dialect_name}")
    return statement.on_conflict_do_update(
        index_elements=[McpConfigVersion.scope_key],
        set_={
            "version": McpConfigVersion.version + 1,
            "updated_at": values["updated_at"],
        },
    )


def _ensure_config_scope_row(db: DBSession, key: str) -> None:
    values = {
        "scope_key": key,
        "version": 0,
        "updated_at": now_naive(),
    }
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        statement = postgresql_insert(McpConfigVersion).values(**values)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(McpConfigVersion).values(**values)
    else:  # pragma: no cover
        raise RuntimeError(f"unsupported database dialect for MCP quota lock: {dialect_name}")
    db.execute(statement.on_conflict_do_nothing(index_elements=[McpConfigVersion.scope_key]))
    db.query(McpConfigVersion).filter(
        McpConfigVersion.scope_key == key
    ).with_for_update().one()


def get_config_version(db: DBSession, user_id: str) -> str:
    rows = {
        row.scope_key: int(row.version or 0)
        for row in db.query(McpConfigVersion)
        .filter(McpConfigVersion.scope_key.in_(["global", _scope_key(user_id)]))
        .all()
    }
    return f"g{rows.get('global', 0)}:u{rows.get(_scope_key(user_id), 0)}"


def _commit(db: DBSession, *, conflict_detail: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=conflict_detail) from exc


def _official_server(
    db: DBSession,
    server_id: str,
    *,
    lock: bool = False,
) -> McpServer:
    query = db.query(McpServer).filter(
        McpServer.id == server_id,
        McpServer.source == "official",
    )
    if lock:
        query = query.with_for_update()
    server = query.first()
    if server is None:
        raise HTTPException(status_code=404, detail="官方 MCP 服务不存在")
    return server


def _visible_server(
    db: DBSession,
    user_id: str,
    server_id: str,
    *,
    lock: bool = False,
) -> McpServer:
    query = db.query(McpServer).filter(
        McpServer.id == server_id,
        or_(
            (McpServer.source == "official")
            & (McpServer.status == "published"),
            (McpServer.source == "personal") & (McpServer.owner_user_id == user_id),
        ),
    )
    if lock:
        # The server row exists before any per-user installation. Serializing
        # on it closes the concurrent first-write gap where both transactions
        # otherwise try to insert the same (server_id, user_id) installation.
        query = query.with_for_update()
    server = query.first()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    return server


def _personal_server(db: DBSession, user_id: str, server_id: str) -> McpServer:
    server = (
        db.query(McpServer)
        .filter(
            McpServer.id == server_id,
            McpServer.source == "personal",
            McpServer.owner_user_id == user_id,
        )
        .first()
    )
    if server is None:
        raise HTTPException(status_code=404, detail="个人 MCP 服务不存在")
    return server


def _installation(
    db: DBSession,
    server_id: str,
    user_id: str,
    *,
    lock: bool = False,
) -> McpInstallation | None:
    query = db.query(McpInstallation).filter(
        McpInstallation.server_id == server_id,
        McpInstallation.user_id == user_id,
    )
    if lock:
        query = query.with_for_update()
    return query.first()


def _get_or_create_installation(
    db: DBSession,
    *,
    server_id: str,
    user_id: str,
    enabled: bool = False,
) -> McpInstallation:
    installation = _installation(db, server_id, user_id)
    if installation is not None:
        return installation
    # An installation has no row to lock on during its first write. Every
    # creation path therefore serializes on the already-existing server row
    # before checking/inserting the (server_id, user_id) unique key.
    server_exists = (
        db.query(McpServer.id)
        .filter(McpServer.id == server_id)
        .with_for_update()
        .first()
    )
    if server_exists is None:
        raise ValueError("MCP server does not exist")
    # A concurrent creator may have committed while this transaction waited
    # for the stable server-row lock.
    installation = _installation(db, server_id, user_id)
    if installation is None:
        installation = McpInstallation(
            server_id=server_id,
            user_id=user_id,
            enabled=enabled,
        )
        db.add(installation)
        db.flush()
    return installation


def _lock_user_mcp_quota(db: DBSession, user_id: str) -> None:
    user = (
        db.query(AuthUser.user_id)
        .filter(AuthUser.user_id == user_id)
        .with_for_update()
        .first()
    )
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")


def _personal_server_count(db: DBSession, user_id: str) -> int:
    return int(
        db.query(func.count(McpServer.id))
        .filter(
            McpServer.source == "personal",
            McpServer.owner_user_id == user_id,
        )
        .scalar()
        or 0
    )


def _optional_enabled_connection_count(db: DBSession, user_id: str) -> int:
    return int(
        db.query(func.count(McpInstallation.id))
        .join(McpServer, McpServer.id == McpInstallation.server_id)
        .filter(
            McpInstallation.user_id == user_id,
            McpInstallation.enabled.is_(True),
            or_(
                McpServer.source == "personal",
                McpServer.required.is_(False),
            ),
        )
        .scalar()
        or 0
    )


def _enforce_personal_server_quota(db: DBSession, user_id: str) -> None:
    limit = int(get_settings().mcp_personal_server_limit)
    if _personal_server_count(db, user_id) >= limit:
        raise HTTPException(
            status_code=409,
            detail=f"个人 MCP 数量已达到上限（{limit}）",
        )


def _enforce_optional_connection_quota(
    db: DBSession,
    user_id: str,
    *,
    server: McpServer,
    installation: McpInstallation | None,
) -> None:
    if server.source == "official" and bool(server.required):
        return
    if installation is not None and bool(installation.enabled):
        return
    limit = int(get_settings().mcp_user_enabled_connection_limit)
    if _optional_enabled_connection_count(db, user_id) >= limit:
        raise HTTPException(
            status_code=409,
            detail=f"可选 MCP 启用数量已达到上限（{limit}）",
        )


def _enforce_required_official_quota(
    db: DBSession,
    *,
    becoming_required: bool,
    exclude_server_id: str | None = None,
) -> None:
    if not becoming_required:
        return
    _ensure_config_scope_row(db, "global")
    limit = int(get_settings().mcp_required_official_server_limit)
    query = db.query(func.count(McpServer.id)).filter(
        McpServer.source == "official",
        McpServer.required.is_(True),
    )
    if exclude_server_id is not None:
        # update_admin_server has already assigned required=True in-memory;
        # autoflush would otherwise count the target as an existing required
        # row and reject the exact Nth slot.
        query = query.filter(McpServer.id != exclude_server_id)
    count = int(query.scalar() or 0)
    if count >= limit:
        raise HTTPException(
            status_code=409,
            detail=f"平台必需官方 MCP 数量已达到上限（{limit}）",
        )


def _credential_for_scope(
    db: DBSession,
    *,
    server_id: str,
    user_id: str | None,
) -> McpCredential | None:
    query = db.query(McpCredential).filter(McpCredential.server_id == server_id)
    query = query.filter(
        McpCredential.user_id.is_(None)
        if user_id is None
        else McpCredential.user_id == user_id
    )
    return query.first()


def _effective_credential(
    db: DBSession,
    server: McpServer,
    installation: McpInstallation | None,
) -> McpCredential | None:
    if installation is not None and installation.credential_id:
        credential_owner_filter = McpCredential.user_id == installation.user_id
        if server.source == "official":
            credential_owner_filter = or_(
                credential_owner_filter,
                McpCredential.user_id.is_(None),
            )
        credential = (
            db.query(McpCredential)
            .filter(
                McpCredential.id == installation.credential_id,
                McpCredential.server_id == server.id,
                credential_owner_filter,
            )
            .first()
        )
        if credential is not None:
            return credential
    if server.source == "official":
        return _credential_for_scope(db, server_id=server.id, user_id=None)
    return _credential_for_scope(
        db,
        server_id=server.id,
        user_id=server.owner_user_id,
    )


def _clear_server_credentials(db: DBSession, server_id: str) -> None:
    credential_ids = [
        row[0]
        for row in db.query(McpCredential.id)
        .filter(McpCredential.server_id == server_id)
        .all()
    ]
    if credential_ids:
        db.query(McpInstallation).filter(
            McpInstallation.credential_id.in_(credential_ids)
        ).update({McpInstallation.credential_id: None}, synchronize_session=False)
        db.query(McpCredential).filter(McpCredential.id.in_(credential_ids)).delete(
            synchronize_session=False
        )


def _endpoint_origin(url: str) -> tuple[str, str, int]:
    """Return the credential trust boundary for a validated MCP endpoint."""

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, hostname, port


def _set_credential(
    db: DBSession,
    *,
    server: McpServer,
    user_id: str | None,
    bearer_token: str | None,
    headers: dict[str, str] | None,
    clear: bool,
) -> McpCredential | None:
    existing = _credential_for_scope(db, server_id=server.id, user_id=user_id)
    if clear or server.auth_type == "none":
        if existing is not None:
            db.query(McpInstallation).filter(
                McpInstallation.credential_id == existing.id
            ).update({McpInstallation.credential_id: None}, synchronize_session=False)
            db.delete(existing)
        return None

    supplied = bearer_token is not None or headers is not None
    if not supplied:
        return existing
    payload = credential_payload(
        server.auth_type,
        bearer_token=bearer_token,
        headers=headers,
    )
    if payload is None:
        return None
    encrypted = encrypt_credential(payload)
    if existing is None:
        existing = McpCredential(
            server_id=server.id,
            user_id=user_id,
            auth_type=server.auth_type,
            encrypted_secret=encrypted,
        )
        db.add(existing)
        db.flush()
    else:
        existing.auth_type = server.auth_type
        existing.encrypted_secret = encrypted
    return existing


def _tool_visibility_policy(
    db: DBSession,
    installation: McpInstallation | None,
) -> tuple[frozenset[str] | None, frozenset[str], int]:
    if installation is None:
        return None, frozenset(), 0
    row = db.query(McpToolVisibility).filter(
        McpToolVisibility.installation_id == installation.id
    ).first()
    if row is None:
        return None, frozenset(), 0
    try:
        enabled_value = (
            None
            if row.enabled_tools_json is None
            else json.loads(row.enabled_tools_json)
        )
        disabled_value = json.loads(row.disabled_tools_json or "[]")
        if enabled_value is not None and not isinstance(enabled_value, list):
            raise ValueError("invalid enabled_tools")
        if not isinstance(disabled_value, list):
            raise ValueError("invalid disabled_tools")
        if enabled_value is not None and not all(
            isinstance(name, str)
            and name
            and name == name.strip()
            and name != "*"
            and len(name) <= 255
            and not any(ord(char) < 32 or ord(char) == 127 for char in name)
            for name in enabled_value
        ):
            raise ValueError("invalid enabled_tools names")
        if not all(
            isinstance(name, str)
            and name
            and name == name.strip()
            and name != "*"
            and len(name) <= 255
            and not any(ord(char) < 32 or ord(char) == 127 for char in name)
            for name in disabled_value
        ):
            raise ValueError("invalid disabled_tools names")
        enabled_tools = (
            None
            if enabled_value is None
            else frozenset(enabled_value)
        )
        disabled_tools = frozenset(disabled_value)
        return enabled_tools, disabled_tools, int(row.revision or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        # A corrupt visibility row must fail closed rather than publishing all
        # remote tools unexpectedly.
        return frozenset(), frozenset(), int(row.revision or 0)


def _tool_is_enabled(
    tool_name: str,
    enabled_tools: frozenset[str] | None,
    disabled_tools: frozenset[str],
) -> bool:
    return (
        (enabled_tools is None or tool_name in enabled_tools)
        and tool_name not in disabled_tools
    )


def _tool_counts(
    db: DBSession,
    installation: McpInstallation | None,
    enabled_tools: frozenset[str] | None = None,
    disabled_tools: frozenset[str] | None = None,
) -> tuple[int, int]:
    if installation is None:
        return 0, 0
    names = [
        str(row[0])
        for row in db.query(McpToolSnapshot.tool_name)
        .filter(McpToolSnapshot.installation_id == installation.id)
        .all()
    ]
    if disabled_tools is None:
        enabled_tools, disabled_tools, _revision = _tool_visibility_policy(db, installation)
    return len(names), sum(
        _tool_is_enabled(name, enabled_tools, disabled_tools)
        for name in names
    )


def server_to_payload(
    db: DBSession,
    server: McpServer,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    installation = _installation(db, server.id, user_id) if user_id is not None else None
    credential = _effective_credential(db, server, installation)
    connection_enabled = bool(installation and installation.enabled)
    if user_id is None:
        connection_enabled = server.status == "published"
    enabled_tools, disabled_tools, _visibility_revision = _tool_visibility_policy(
        db,
        installation,
    )
    tools_count, enabled_tools_count = _tool_counts(
        db,
        installation,
        enabled_tools,
        disabled_tools,
    )
    if user_id is None:
        # Administrator probes run with the platform credential rather than a
        # user's installation, so their discovered count is stored on the
        # official server instead of in per-installation snapshots.
        tools_count = max(0, int(server.last_tools_count or 0))
        enabled_tools_count = tools_count
    is_user_official_view = user_id is not None and server.source == "official"
    credential_is_user_owned = bool(
        credential is not None
        and user_id is not None
        and credential.user_id == user_id
    )
    header_names = credential_header_names(
        server.auth_type,
        credential.encrypted_secret if credential is not None else None,
    )
    if is_user_official_view and not credential_is_user_owned:
        # Platform credential structure is administrator configuration.  A
        # user can still see header names for their own explicit override.
        header_names = []
    return {
        "id": server.id,
        "name": server.name,
        "description": server.description,
        "url": (
            mcp_url_without_query(str(server.url))
            if is_user_official_view
            else server.url
        ),
        "source": server.source,
        "status": server.status,
        "enabled": bool(connection_enabled and server.status == "published"),
        "auth_type": server.auth_type,
        "credential_set": credential is not None,
        "header_names": header_names,
        "allow_private_network": bool(server.allow_private_network),
        "allow_insecure_http": bool(server.allow_insecure_http),
        "required": bool(server.required),
        "installation_id": installation.id if installation is not None else None,
        "tools_count": tools_count,
        "enabled_tools_count": enabled_tools_count,
        "enabled_tools": sorted(enabled_tools) if enabled_tools is not None else None,
        "disabled_tools": sorted(disabled_tools),
        "last_tested_at": server.last_tested_at,
        "last_error": None if is_user_official_view else server.last_error,
        "version": int(server.version or 1),
        "created_at": server.created_at,
        "updated_at": server.updated_at,
    }


def list_admin_servers(db: DBSession) -> dict[str, Any]:
    servers = (
        db.query(McpServer)
        .filter(McpServer.source == "official")
        .order_by(McpServer.created_at.asc(), McpServer.name.asc())
        .all()
    )
    global_row = db.query(McpConfigVersion).filter(McpConfigVersion.scope_key == "global").first()
    return {
        "servers": [server_to_payload(db, server) for server in servers],
        "config_version": f"g{int(global_row.version or 0) if global_row else 0}",
    }


def _provision_required_installation_if_current(
    db: DBSession,
    *,
    server_id: str,
    user_id: str,
) -> bool:
    """Enable one required integration only after a locked catalog recheck."""

    locked_server = (
        db.query(McpServer)
        .filter(
            McpServer.id == server_id,
            McpServer.source == "official",
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if (
        locked_server is None
        or locked_server.status != "published"
        or not bool(locked_server.required)
    ):
        return False

    existing = _installation(db, server_id, user_id)
    changed = existing is None or not existing.enabled
    installation = existing or _get_or_create_installation(
        db,
        server_id=server_id,
        user_id=user_id,
        enabled=True,
    )
    if not installation.enabled:
        installation.enabled = True
        changed = True
    return changed


def list_user_servers(db: DBSession, user_id: str) -> dict[str, Any]:
    def _query_visible_servers() -> list[McpServer]:
        return (
            db.query(McpServer)
            .filter(
                or_(
                    (McpServer.source == "official")
                    & (McpServer.status == "published"),
                    (McpServer.source == "personal")
                    & (McpServer.owner_user_id == user_id),
                )
            )
            .order_by(
                McpServer.source.asc(),
                McpServer.created_at.asc(),
                McpServer.name.asc(),
            )
            .all()
        )

    servers = _query_visible_servers()
    required_changed = False
    for server in servers:
        if (
            server.source == "official"
            and bool(server.required)
            and server.status == "published"
        ):
            # Required integrations are platform policy, not a user opt-in.
            # Materialize the per-user installation so snapshots, credentials,
            # visibility and runtime routing still use the normal data model.
            # Re-read under a stable server-row lock: the initial catalog read
            # may race an administrator disabling or making the server optional.
            required_changed = (
                _provision_required_installation_if_current(
                    db,
                    server_id=str(server.id),
                    user_id=user_id,
                )
                or required_changed
            )
    if required_changed:
        _commit(
            db,
            conflict_detail="平台必需 MCP 连接正在初始化，请刷新后重试",
        )
    # A status/required change may have committed while the first catalog query
    # was waiting on its row lock. Never serialize that stale entry.
    servers = _query_visible_servers()
    return {
        "servers": [server_to_payload(db, server, user_id=user_id) for server in servers],
        "config_version": get_config_version(db, user_id),
    }


def create_admin_server(
    db: DBSession,
    payload: AdminMcpServerCreate,
    admin_user_id: str,
) -> dict[str, Any]:
    if payload.required and payload.status == "published":
        raise HTTPException(
            status_code=409,
            detail="平台必需 MCP 必须先保存为草稿或停用状态，管理员测试成功后才能发布",
        )
    try:
        url = validate_mcp_url(
            payload.url,
            allow_private_network=payload.allow_private_network,
            allow_insecure_http=payload.allow_insecure_http,
        )
    except McpSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        _enforce_required_official_quota(db, becoming_required=bool(payload.required))
    except HTTPException:
        db.rollback()
        raise
    server = McpServer(
        source="official",
        owner_user_id=None,
        name=payload.name,
        description=payload.description,
        url=url,
        status=payload.status,
        auth_type=payload.auth_type,
        allow_private_network=payload.allow_private_network,
        allow_insecure_http=payload.allow_insecure_http,
        required=payload.required,
        created_by=admin_user_id,
    )
    try:
        db.add(server)
        db.flush()
        _set_credential(
            db,
            server=server,
            user_id=None,
            bearer_token=payload.bearer_token,
            headers=payload.headers,
            clear=payload.clear_credential,
        )
        bump_config_version(db, None)
        db.commit()
    except McpSecurityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="官方 MCP 名称已存在") from exc
    db.refresh(server)
    return server_to_payload(db, server)


def update_admin_server(
    db: DBSession,
    server_id: str,
    payload: AdminMcpServerPatch,
) -> dict[str, Any]:
    server = _official_server(db, server_id, lock=True)
    data = payload.model_dump(exclude_unset=True)
    was_required = bool(server.required)
    old_url = str(server.url)
    old_auth_type = server.auth_type
    old_allow_private_network = bool(server.allow_private_network)
    old_allow_insecure_http = bool(server.allow_insecure_http)
    for field in (
        "name",
        "description",
        "status",
        "auth_type",
        "allow_private_network",
        "allow_insecure_http",
        "required",
    ):
        if field in data:
            setattr(server, field, data[field])
    if "url" in data or "allow_private_network" in data or "allow_insecure_http" in data:
        try:
            server.url = validate_mcp_url(
                data.get("url", server.url),
                allow_private_network=bool(server.allow_private_network),
                allow_insecure_http=bool(server.allow_insecure_http),
            )
        except McpSecurityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    connection_changed = (
        str(server.url) != old_url
        or str(server.auth_type) != str(old_auth_type)
        or bool(server.allow_private_network) != old_allow_private_network
        or bool(server.allow_insecure_http) != old_allow_insecure_http
        or data.get("bearer_token") is not None
        or data.get("headers") is not None
        or bool(data.get("clear_credential"))
    )
    # Credentials authorize an origin, not an arbitrary future target.  A
    # hostname, scheme or effective-port change must detach both the platform
    # credential and every per-user override before a new target can be used.
    # A path-only change stays within the same HTTP origin and may retain them.
    origin_changed = _endpoint_origin(old_url) != _endpoint_origin(str(server.url))
    if old_auth_type != server.auth_type or origin_changed:
        _clear_server_credentials(db, server.id)
    try:
        _set_credential(
            db,
            server=server,
            user_id=None,
            bearer_token=data.get("bearer_token"),
            headers=data.get("headers"),
            clear=bool(data.get("clear_credential")),
        )
    except McpSecurityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if connection_changed:
        server.last_tested_at = None
        server.last_tools_count = None
        server.last_error = None
    try:
        _enforce_required_official_quota(
            db,
            becoming_required=bool(server.required) and not was_required,
            exclude_server_id=str(server.id),
        )
    except HTTPException:
        db.rollback()
        raise
    if (
        bool(server.required)
        and server.status == "published"
        and (server.last_tested_at is None or server.last_error is not None)
    ):
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="平台必需 MCP 必须使用当前配置完成管理员连接测试后才能发布",
        )
    server.version = int(server.version or 0) + 1
    bump_config_version(db, None)
    _commit(db, conflict_detail="官方 MCP 名称已存在")
    db.refresh(server)
    return server_to_payload(db, server)


def delete_admin_server(db: DBSession, server_id: str) -> dict[str, Any]:
    server = _official_server(db, server_id)
    db.delete(server)
    bump_config_version(db, None)
    db.commit()
    return {"id": server_id, "deleted": True}


def create_personal_server(
    db: DBSession,
    user_id: str,
    payload: UserMcpServerCreate,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    try:
        url = validate_mcp_url(payload.url)
    except McpSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        _lock_user_mcp_quota(db, user_id)
        _enforce_personal_server_quota(db, user_id)
    except HTTPException:
        db.rollback()
        raise
    server = McpServer(
        source="personal",
        owner_user_id=user_id,
        name=payload.name,
        description=payload.description,
        url=url,
        status="published",
        auth_type=payload.auth_type,
        allow_private_network=False,
        allow_insecure_http=False,
        required=False,
        created_by=user_id,
    )
    try:
        if payload.enabled:
            _enforce_optional_connection_quota(
                db,
                user_id,
                server=server,
                installation=None,
            )
        db.add(server)
        db.flush()
        installation = _get_or_create_installation(
            db,
            server_id=server.id,
            user_id=user_id,
            enabled=payload.enabled,
        )
        credential = _set_credential(
            db,
            server=server,
            user_id=user_id,
            bearer_token=payload.bearer_token,
            headers=payload.headers,
            clear=payload.clear_credential,
        )
        if credential is not None:
            installation.credential_id = credential.id
        bump_config_version(db, user_id)
        if commit:
            db.commit()
    except McpSecurityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="个人 MCP 名称已存在") from exc
    if commit:
        db.refresh(server)
    return server_to_payload(db, server, user_id=user_id)


def update_personal_server(
    db: DBSession,
    user_id: str,
    server_id: str,
    payload: UserMcpServerPatch,
) -> dict[str, Any]:
    _lock_user_mcp_quota(db, user_id)
    server = _personal_server(db, user_id, server_id)
    data = payload.model_dump(exclude_unset=True)
    old_url = str(server.url)
    old_auth_type = server.auth_type
    for field in ("name", "description", "auth_type"):
        if field in data:
            setattr(server, field, data[field])
    if "url" in data:
        try:
            server.url = validate_mcp_url(data["url"])
        except McpSecurityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Credentials authorize an origin, not an arbitrary future target. A
    # hostname, scheme or effective-port change must detach the stored
    # credential before the new target can be reached; a path-only change stays
    # within the same HTTP origin and may retain it. Mirrors update_admin_server.
    origin_changed = _endpoint_origin(old_url) != _endpoint_origin(str(server.url))
    if old_auth_type != server.auth_type or origin_changed:
        _clear_server_credentials(db, server.id)
    installation = _get_or_create_installation(db, server_id=server.id, user_id=user_id)
    if data.get("enabled") is not None:
        if bool(data["enabled"]):
            _enforce_optional_connection_quota(
                db,
                user_id,
                server=server,
                installation=installation,
            )
        installation.enabled = bool(data["enabled"])
    try:
        credential = _set_credential(
            db,
            server=server,
            user_id=user_id,
            bearer_token=data.get("bearer_token"),
            headers=data.get("headers"),
            clear=bool(data.get("clear_credential")),
        )
    except McpSecurityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    installation.credential_id = credential.id if credential is not None else None
    if (
        str(server.url) != old_url
        or str(server.auth_type) != str(old_auth_type)
        or data.get("bearer_token") is not None
        or data.get("headers") is not None
        or bool(data.get("clear_credential"))
    ):
        server.last_tested_at = None
        server.last_error = None
    server.version = int(server.version or 0) + 1
    bump_config_version(db, user_id)
    _commit(db, conflict_detail="个人 MCP 名称已存在")
    db.refresh(server)
    return server_to_payload(db, server, user_id=user_id)


def delete_personal_server(db: DBSession, user_id: str, server_id: str) -> dict[str, Any]:
    server = _personal_server(db, user_id, server_id)
    db.delete(server)
    bump_config_version(db, user_id)
    db.commit()
    return {"id": server_id, "deleted": True}


def update_connection(
    db: DBSession,
    user_id: str,
    server_id: str,
    payload: McpConnectionUpdate,
) -> dict[str, Any]:
    _lock_user_mcp_quota(db, user_id)
    # Serialize user credential writes with administrator edits of the same
    # official server.  Without the server-row lock, a user transaction that
    # read the old origin could insert a credential after an administrator had
    # already changed the origin and cleared every credential, reviving a
    # secret that is now bound to the wrong trust boundary.
    server = _visible_server(db, user_id, server_id, lock=True)
    if (
        server.source == "official"
        and bool(server.required)
        and server.status == "published"
        and not payload.enabled
    ):
        raise HTTPException(status_code=409, detail="平台必需 MCP 连接不能停用")
    if payload.enabled and server.status != "published":
        raise HTTPException(status_code=409, detail="停用的 MCP 服务不能启用连接")
    if payload.auth_type is not None and payload.auth_type != server.auth_type:
        raise HTTPException(status_code=400, detail="连接 auth_type 必须与 MCP 服务定义一致")
    installation = _get_or_create_installation(db, server_id=server.id, user_id=user_id)
    if payload.enabled:
        _enforce_optional_connection_quota(
            db,
            user_id,
            server=server,
            installation=installation,
        )
    installation.enabled = bool(payload.enabled or server.required)
    try:
        credential = _set_credential(
            db,
            server=server,
            user_id=user_id,
            bearer_token=payload.bearer_token,
            headers=payload.headers,
            clear=payload.clear_credential,
        )
    except McpSecurityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if credential is not None:
        installation.credential_id = credential.id
    elif payload.clear_credential:
        installation.credential_id = None
    if server.source == "personal" and (
        payload.bearer_token is not None
        or payload.headers is not None
        or payload.clear_credential
    ):
        server.last_tested_at = None
        server.last_error = None
    bump_config_version(db, user_id)
    db.commit()
    return server_to_payload(db, server, user_id=user_id)


def list_server_tools(
    db: DBSession,
    user_id: str,
    server_id: str,
) -> dict[str, Any]:
    """List discovered tools and their independent publication state."""

    server = _visible_server(db, user_id, server_id)
    installation = _installation(db, server.id, user_id)
    enabled_tools, disabled_tools, visibility_revision = _tool_visibility_policy(
        db,
        installation,
    )
    rows = [] if installation is None else (
        db.query(McpToolSnapshot)
        .filter(McpToolSnapshot.installation_id == installation.id)
        .order_by(McpToolSnapshot.tool_name.asc())
        .all()
    )
    tools = [
        {
            "name": str(row.tool_name),
            "title": row.title,
            "description": row.description,
            "schema_hash": str(row.schema_hash),
            "enabled": _tool_is_enabled(
                str(row.tool_name),
                enabled_tools,
                disabled_tools,
            ),
            "discovered_at": row.discovered_at,
        }
        for row in rows
    ]
    return {
        "server_id": str(server.id),
        "installation_id": str(installation.id) if installation is not None else None,
        "tools_count": len(tools),
        "enabled_tools_count": sum(bool(tool["enabled"]) for tool in tools),
        "enabled_tools": sorted(enabled_tools) if enabled_tools is not None else None,
        "disabled_tools": sorted(disabled_tools),
        "visibility_revision": visibility_revision,
        "tools": tools,
    }


def update_tool_visibility(
    db: DBSession,
    user_id: str,
    server_id: str,
    payload: McpToolVisibilityUpdate,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    """Atomically replace one installation's complete publication policy.

    Visibility only controls whether a tool is published to the Agent. It does
    not grant ALLOW/ASK/DENY permission, and it does not enable the connection.
    """

    server = _visible_server(db, user_id, server_id, lock=True)
    installation = _get_or_create_installation(
        db,
        server_id=server.id,
        user_id=user_id,
        enabled=False,
    )
    installation = (
        db.query(McpInstallation)
        .filter(McpInstallation.id == installation.id)
        .with_for_update()
        .one()
    )
    visibility = (
        db.query(McpToolVisibility)
        .filter(McpToolVisibility.installation_id == installation.id)
        .with_for_update()
        .first()
    )
    current_revision = int(visibility.revision or 0) if visibility is not None else 0
    if payload.expected_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail="MCP 工具发布设置已被其他操作修改，请刷新后重试",
        )
    if visibility is None:
        visibility = McpToolVisibility(
            installation_id=installation.id,
            revision=1,
        )
        db.add(visibility)
    else:
        visibility.revision = current_revision + 1
    visibility.enabled_tools_json = (
        json.dumps(payload.enabled_tools, ensure_ascii=False)
        if payload.enabled_tools is not None
        else None
    )
    visibility.disabled_tools_json = json.dumps(
        payload.disabled_tools,
        ensure_ascii=False,
    )

    # This invalidates both runtime catalogs and already-cached Agent instances.
    bump_config_version(db, user_id)
    if commit:
        _commit(
            db,
            conflict_detail="MCP 工具发布设置已被其他操作修改，请刷新后重试",
        )
    return list_server_tools(db, user_id, server_id)


def resolve_installation_headers(
    db: DBSession,
    user_id: str,
    installation_id: str,
) -> dict[str, str]:
    installation = (
        db.query(McpInstallation)
        .filter(
            McpInstallation.id == installation_id,
            McpInstallation.user_id == user_id,
        )
        .first()
    )
    if installation is None:
        raise ValueError("MCP installation does not exist")
    server = db.query(McpServer).filter(McpServer.id == installation.server_id).first()
    if server is None:
        raise ValueError("MCP server does not exist")
    credential = _effective_credential(db, server, installation)
    return credential_headers(
        server.auth_type,
        credential.encrypted_secret if credential is not None else None,
    )


def resolve_effective_servers(
    db: DBSession,
    user_id: str,
) -> tuple[list[ResolvedMcpServer], str]:
    rows = (
        db.query(McpInstallation, McpServer)
        .join(McpServer, McpServer.id == McpInstallation.server_id)
        .filter(
            McpInstallation.user_id == user_id,
            McpInstallation.enabled.is_(True),
            McpServer.status == "published",
            or_(
                McpServer.source == "official",
                McpServer.owner_user_id == user_id,
            ),
        )
        .order_by(McpServer.source.asc(), McpServer.name.asc())
        .all()
    )
    resolved = [
        ResolvedMcpServer(
            server=server,
            installation=installation,
            credential=_effective_credential(db, server, installation),
        )
        for installation, server in rows
    ]
    return resolved, get_config_version(db, user_id)


async def probe_mcp_server(
    server: McpServer | _ProbeServerSnapshot,
    credential: McpCredential | _ProbeCredentialSnapshot | None,
) -> tuple[list[Any], int]:
    """Initialize a Streamable HTTP session and list its tools."""

    headers = credential_headers(
        server.auth_type,
        credential.encrypted_secret if credential is not None else None,
    )
    timeout_seconds = float(getattr(get_settings(), "mcp_test_timeout_seconds", 15.0))
    # Import lazily to keep catalog CRUD usable in migration/admin scripts while
    # sharing the exact same SDK compatibility and DNS-recheck boundary as the
    # Agent runtime.
    from src.api.services.mcp_runtime import probe_streamable_http

    started = perf_counter()
    tools = await probe_streamable_http(
        url=server.url,
        headers=headers,
        allow_private_network=bool(server.allow_private_network),
        allow_insecure_http=bool(server.allow_insecure_http),
        timeout_seconds=timeout_seconds,
    )
    for tool in tools:
        raw = _jsonable(tool)
        raw_name = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(raw_name, str) or not raw_name:
            raise ValueError(f"MCP server {server.name!r} returned a nameless tool")
        validate_mcp_tool_name(raw_name)
        input_schema = raw.get("inputSchema") or raw.get("input_schema") or {}
        if not isinstance(input_schema, dict):
            raise ValueError(f"MCP tool {raw_name!r} has an invalid input schema")
        annotations = raw.get("annotations") or {}
        if not isinstance(annotations, dict):
            annotations = _jsonable(annotations)
        if not isinstance(annotations, dict):
            annotations = {}
        title = str(raw["title"]) if raw.get("title") is not None else None
        description = str(raw.get("description") or title or raw_name)
        validate_mcp_tool_metadata(
            raw_name=raw_name,
            title=title,
            description=description,
            input_schema=input_schema,
            annotations=annotations,
        )
    latency_ms = int((perf_counter() - started) * 1000)
    return tools, latency_ms


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return value
    return value


def _safe_probe_error(
    exc: Exception,
    server: McpServer | _ProbeServerSnapshot,
    credential: McpCredential | _ProbeCredentialSnapshot | None,
) -> str:
    """Redact probe failures through the shared MCP security boundary."""

    try:
        outbound = credential_headers(
            server.auth_type,
            credential.encrypted_secret if credential is not None else None,
        )
    except Exception:
        outbound = {}
    return sanitize_mcp_exception(
        exc,
        url=str(server.url),
        headers=outbound,
    )


def _save_tool_snapshots(
    db: DBSession,
    installation: McpInstallation,
    tools: list[Any],
    *,
    connection_fingerprint: str,
    server_name: str,
    server_description: str | None,
) -> None:
    db.query(McpToolSnapshot).filter(
        McpToolSnapshot.installation_id == installation.id
    ).delete(synchronize_session=False)
    discovered_at = now_naive()
    search_targets = []
    for tool in tools:
        raw = _jsonable(tool)
        if not isinstance(raw, dict):
            continue
        raw_name = raw.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            raise ValueError("MCP server returned a nameless tool")
        validate_mcp_tool_name(raw_name)
        name = raw_name
        input_schema = raw.get("inputSchema") or raw.get("input_schema") or {}
        if not isinstance(input_schema, dict):
            raise ValueError(f"MCP tool {name!r} has an invalid input schema")
        annotations = raw.get("annotations") or {}
        if not isinstance(annotations, dict):
            annotations = _jsonable(annotations)
        if not isinstance(annotations, dict):
            annotations = {}
        title = raw.get("title")
        if title is not None:
            title = str(title)
        description = str(raw.get("description") or title or name)
        validate_mcp_tool_metadata(
            raw_name=name,
            title=title,
            description=description,
            input_schema=input_schema,
            annotations=annotations,
        )
        schema_json = json.dumps(input_schema, ensure_ascii=False, sort_keys=True, default=str)
        schema_hash = mcp_tool_schema_hash(
            raw_name=name,
            description=description,
            input_schema=input_schema,
            annotations=annotations,
        )
        db.add(McpToolSnapshot(
            installation_id=installation.id,
            tool_name=name,
            title=title,
            description=description,
            input_schema_json=schema_json,
            annotations_json=(
                json.dumps(annotations, ensure_ascii=False, sort_keys=True, default=str)
                if annotations
                else None
            ),
            schema_hash=schema_hash,
            connection_fingerprint=connection_fingerprint,
            discovered_at=discovered_at,
        ))
        from src.api.services.mcp_tool_search_service import McpToolSearchIndexTarget

        search_targets.append(McpToolSearchIndexTarget(
            installation_id=str(installation.id),
            tool_name=name,
            server_name=server_name,
            server_description=server_description or "",
            title=title or "",
            description=description,
            schema_hash=schema_hash,
            connection_fingerprint=connection_fingerprint,
        ))

    from src.api.services.mcp_tool_search_service import sync_mcp_tool_search_indexes

    sync_mcp_tool_search_indexes(
        db,
        installation_id=str(installation.id),
        targets=search_targets,
    )


async def test_admin_server(db: DBSession, server_id: str) -> dict[str, Any]:
    server = _official_server(db, server_id)
    credential = _effective_credential(db, server, None)
    probe_server = _probe_server_snapshot(server)
    probe_credential = _probe_credential_snapshot(credential)
    target_fingerprint = _probe_target_fingerprint(
        probe_server,
        probe_credential,
        installation_id=f"platform:{server_id}",
        user_id="platform",
    )
    # Never retain an ORM transaction/row lock across remote network I/O.
    db.rollback()
    started = perf_counter()
    tools: list[Any] = []
    safe_error: str | None = None
    try:
        tools, latency_ms = await probe_mcp_server(probe_server, probe_credential)
    except Exception as exc:
        latency_ms = int((perf_counter() - started) * 1000)
        safe_error = _safe_probe_error(exc, probe_server, probe_credential)

    try:
        current_server = _official_server(db, server_id, lock=True)
    except HTTPException:
        db.rollback()
        return {
            "ok": False,
            "tools_count": 0,
            "latency_ms": latency_ms,
            "error": "MCP 配置在测试期间已变化，请重新测试",
        }
    current_credential = _effective_credential(db, current_server, None)
    current_fingerprint = _probe_target_fingerprint(
        current_server,
        current_credential,
        installation_id=f"platform:{server_id}",
        user_id="platform",
    )
    if current_fingerprint != target_fingerprint:
        db.rollback()
        return {
            "ok": False,
            "tools_count": 0,
            "latency_ms": latency_ms,
            "error": "MCP 配置在测试期间已变化，请重新测试",
        }

    current_server.last_tested_at = now_naive()
    current_server.last_tools_count = len(tools) if safe_error is None else 0
    current_server.last_error = safe_error
    if safe_error is not None and current_server.required and current_server.status == "published":
        current_server.status = "disabled"
        current_server.version = int(current_server.version or 0) + 1
        bump_config_version(db, None)
    db.commit()
    return {
        "ok": safe_error is None,
        "tools_count": len(tools) if safe_error is None else 0,
        "latency_ms": latency_ms,
        "error": safe_error,
    }


async def test_user_server(
    db: DBSession,
    user_id: str,
    server_id: str,
) -> dict[str, Any]:
    server = _visible_server(db, user_id, server_id)
    installation = _get_or_create_installation(db, server_id=server.id, user_id=user_id)
    # Persist a first installation and release the stable server-row lock
    # before contacting the remote endpoint.
    db.commit()
    server = _visible_server(db, user_id, server_id)
    installation = _installation(db, server.id, user_id)
    if installation is None:  # pragma: no cover - concurrent delete raced the lock release above
        db.rollback()
        raise HTTPException(status_code=409, detail="MCP 连接在测试前已被删除")
    credential = _effective_credential(db, server, installation)
    probe_server = _probe_server_snapshot(server)
    probe_credential = _probe_credential_snapshot(credential)
    target_fingerprint = _probe_target_fingerprint(
        probe_server,
        probe_credential,
        installation_id=str(installation.id),
        user_id=user_id,
    )
    is_personal = server.source == "personal"
    db.rollback()
    started = perf_counter()
    tools: list[Any] = []
    safe_error: str | None = None
    try:
        tools, latency_ms = await probe_mcp_server(probe_server, probe_credential)
    except Exception as exc:
        latency_ms = int((perf_counter() - started) * 1000)
        safe_error = _safe_probe_error(exc, probe_server, probe_credential)

    # Serialize with user-scoped mutations on the quota row, then acquire the
    # target rows in the same server -> installation order as runtime snapshot
    # CAS.  The server lock is also the serialization point for administrator
    # edits of an official server, which do not take a user's quota lock.  Hold
    # all three locks through fingerprint validation, snapshot persistence and
    # commit.  They are acquired only after remote I/O, so no database lock is
    # ever held across the network probe.
    _lock_user_mcp_quota(db, user_id)
    try:
        current_server = _visible_server(db, user_id, server_id, lock=True)
    except HTTPException:
        db.rollback()
        return {
            "ok": False,
            "tools_count": 0,
            "latency_ms": latency_ms,
            "error": "MCP 配置在测试期间已变化，请重新测试",
        }
    current_installation = _installation(
        db,
        current_server.id,
        user_id,
        lock=True,
    )
    current_credential = (
        _effective_credential(db, current_server, current_installation)
        if current_installation is not None
        else None
    )
    current_fingerprint = (
        _probe_target_fingerprint(
            current_server,
            current_credential,
            installation_id=str(current_installation.id),
            user_id=user_id,
        )
        if current_installation is not None
        else None
    )
    if current_fingerprint != target_fingerprint:
        db.rollback()
        return {
            "ok": False,
            "tools_count": 0,
            "latency_ms": latency_ms,
            "error": "MCP 配置在测试期间已变化，请重新测试",
        }

    if safe_error is None:
        try:
            # Persist snapshots inside a SAVEPOINT so a write failure rolls back
            # only the snapshot changes while keeping the outer transaction and
            # its _lock_user_mcp_quota lock. Otherwise a bare rollback here would
            # drop the lock and let a concurrent config change land between the
            # fingerprint check and the last_tested_at/last_error write below
            # (mcp-spec §4.5: hold the same lock through commit).
            with db.begin_nested():
                _save_tool_snapshots(
                    db,
                    current_installation,
                    tools,
                    connection_fingerprint=target_fingerprint,
                    server_name=str(current_server.name),
                    server_description=(
                        str(current_server.description)
                        if current_server.description is not None
                        else None
                    ),
                )
                bump_config_version(db, user_id)
        except Exception as exc:
            safe_error = _safe_probe_error(exc, probe_server, probe_credential)
    if is_personal:
        current_server.last_tested_at = now_naive()
        current_server.last_error = safe_error
    db.commit()
    return {
        "ok": safe_error is None,
        "tools_count": len(tools) if safe_error is None else 0,
        "latency_ms": latency_ms,
        "error": safe_error,
    }


def import_personal_servers(
    db: DBSession,
    user_id: str,
    payload: McpImportRequest,
) -> dict[str, Any]:
    imported_servers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for raw_name, raw_config in payload.mcp_servers.items():
        name = str(raw_name).strip()
        if not name:
            errors.append({"name": raw_name, "error": "MCP 名称不能为空"})
            continue
        try:
            config = McpImportServer.model_validate(raw_config)
            # Create the server and its publication policy inside a single
            # transaction so a single import item is atomic: a failure while
            # writing visibility must not leave an orphaned server behind that
            # would later collide on re-import (spec §5 import failure mode).
            created = create_personal_server(
                db,
                user_id,
                UserMcpServerCreate(
                    name=name,
                    description=config.description,
                    url=config.url,
                    auth_type="headers" if config.headers else "none",
                    headers=config.headers,
                    enabled=not config.disabled,
                ),
                commit=False,
            )
            # Publication policy is independent from connection state. Keep
            # exact, case-sensitive names (including tools not discovered yet)
            # when importing our mcp.json extension fields.
            if config.enabled_tools is not None or config.disabled_tools:
                update_tool_visibility(
                    db,
                    user_id,
                    str(created["id"]),
                    McpToolVisibilityUpdate(
                        expected_revision=0,
                        enabled_tools=config.enabled_tools,
                        disabled_tools=config.disabled_tools,
                    ),
                    commit=False,
                )
            db.commit()
            created = server_to_payload(
                db,
                _visible_server(db, user_id, str(created["id"])),
                user_id=user_id,
            )
            imported_servers.append(created)
        except HTTPException as exc:
            db.rollback()
            errors.append({"name": name, "error": str(exc.detail)})
        except ValidationError:
            db.rollback()
            # Pydantic's textual error includes the rejected input. Imported
            # configs may contain credentials, so never serialize it here.
            errors.append({"name": name, "error": "MCP 配置格式无效"})
        except Exception as exc:
            db.rollback()
            errors.append({"name": name, "error": "MCP 导入失败"})
    return {
        "imported": len(imported_servers),
        "servers": imported_servers,
        "errors": errors,
    }


def export_personal_servers(db: DBSession, user_id: str) -> dict[str, Any]:
    servers = (
        db.query(McpServer)
        .filter(
            McpServer.source == "personal",
            McpServer.owner_user_id == user_id,
        )
        .order_by(McpServer.created_at.asc(), McpServer.name.asc())
        .all()
    )
    exported: dict[str, Any] = {}
    for server in servers:
        installation = _installation(db, server.id, user_id)
        item: dict[str, Any] = {
            "type": "streamable-http",
            "url": server.url,
            "disabled": not bool(installation and installation.enabled),
        }
        if server.description:
            item["description"] = server.description
        enabled_tools, disabled_tools, _revision = _tool_visibility_policy(
            db,
            installation,
        )
        # These are OpenCapyBox extensions to mcp.json. Omit defaults for
        # compatibility, while preserving explicit allowlists (including [])
        # and future/temporarily missing tool names exactly.
        if enabled_tools is not None:
            item["enabled_tools"] = sorted(enabled_tools)
        if disabled_tools:
            item["disabled_tools"] = sorted(disabled_tools)
        # Deliberately omit bearer_token and headers. Export is safe to share.
        exported[server.name] = item
    return {"mcpServers": exported}
