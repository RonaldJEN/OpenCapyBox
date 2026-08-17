"""Administrator-managed network exceptions for personal MCP servers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from src.api.models.mcp import (
    McpInstallation,
    McpPersonalNetworkPolicy,
    McpServer,
)
from src.api.services.mcp_security import (
    McpSecurityError,
    PersonalMcpNetworkPolicy,
    authorize_personal_mcp_endpoint,
    normalize_personal_mcp_network_policy,
)
from src.api.utils.timezone import now_naive


@dataclass(frozen=True)
class StoredPersonalMcpNetworkPolicy:
    policy: PersonalMcpNetworkPolicy
    updated_at: Any | None


def _decode_string_list(value: str | None) -> list[str]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        return []
    return decoded


def load_personal_mcp_network_policy(
    db: DBSession,
    *,
    lock: bool = False,
) -> StoredPersonalMcpNetworkPolicy:
    query = db.query(McpPersonalNetworkPolicy).filter(
        McpPersonalNetworkPolicy.scope_key == "global"
    )
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        return StoredPersonalMcpNetworkPolicy(
            policy=PersonalMcpNetworkPolicy(),
            updated_at=None,
        )
    try:
        policy = normalize_personal_mcp_network_policy(
            _decode_string_list(row.domain_suffixes_json),
            _decode_string_list(row.cidrs_json),
            version=int(row.version or 0),
        )
    except McpSecurityError:
        # Corrupt durable policy must fail closed rather than widening access.
        policy = PersonalMcpNetworkPolicy(version=int(row.version or 0))
    return StoredPersonalMcpNetworkPolicy(policy=policy, updated_at=row.updated_at)


def personal_mcp_network_policy_payload(
    db: DBSession,
    *,
    disabled_installations: int = 0,
) -> dict[str, Any]:
    stored = load_personal_mcp_network_policy(db)
    return {
        "domain_suffixes": list(stored.policy.domain_suffixes),
        "cidrs": list(stored.policy.cidrs),
        "version": stored.policy.version,
        "updated_at": stored.updated_at,
        "disabled_installations": int(disabled_installations),
    }


def _reevaluate_authorization(
    raw: str,
    policy: PersonalMcpNetworkPolicy,
):
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("invalid network authorization")
    scheme = str(decoded.get("scheme") or "")
    hostname = str(decoded.get("hostname") or "")
    addresses = decoded.get("addresses")
    if scheme not in {"http", "https"} or not hostname or not isinstance(addresses, list):
        raise ValueError("invalid network authorization")
    if not addresses or not all(isinstance(item, str) and item for item in addresses):
        raise ValueError("invalid network authorization")
    return authorize_personal_mcp_endpoint(
        scheme=scheme,
        hostname=hostname,
        addresses=tuple(addresses),
        policy=policy,
    )


def update_personal_mcp_network_policy(
    db: DBSession,
    *,
    domain_suffixes: list[str],
    cidrs: list[str],
    admin_user_id: str,
) -> dict[str, Any]:
    try:
        normalized = normalize_personal_mcp_network_policy(domain_suffixes, cidrs)
    except McpSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = (
        db.query(McpPersonalNetworkPolicy)
        .filter(McpPersonalNetworkPolicy.scope_key == "global")
        .with_for_update()
        .first()
    )
    if row is None:
        row = McpPersonalNetworkPolicy(scope_key="global", version=0)
        db.add(row)
        db.flush()
    current = load_personal_mcp_network_policy(db)
    if (
        current.policy.domain_suffixes == normalized.domain_suffixes
        and current.policy.cidrs == normalized.cidrs
    ):
        db.rollback()
        return personal_mcp_network_policy_payload(db)

    row.domain_suffixes_json = json.dumps(
        list(normalized.domain_suffixes), ensure_ascii=False, separators=(",", ":")
    )
    row.cidrs_json = json.dumps(list(normalized.cidrs), separators=(",", ":"))
    row.version = max(1, int(row.version or 0) + 1)
    row.updated_by = admin_user_id
    row.updated_at = now_naive()
    effective_policy = PersonalMcpNetworkPolicy(
        domain_suffixes=normalized.domain_suffixes,
        cidrs=normalized.cidrs,
        version=int(row.version),
    )

    affected = (
        db.query(McpInstallation, McpServer)
        .join(McpServer, McpServer.id == McpInstallation.server_id)
        .filter(
            McpServer.source == "personal",
            McpInstallation.enabled.is_(True),
            McpInstallation.network_authorization_json.isnot(None),
        )
        .with_for_update()
        .all()
    )
    disabled = 0
    for installation, server in affected:
        try:
            authorization = _reevaluate_authorization(
                str(installation.network_authorization_json),
                effective_policy,
            )
        except (McpSecurityError, TypeError, ValueError, json.JSONDecodeError):
            installation.enabled = False
            installation.network_authorization_json = None
            server.last_error = "管理员已收紧个人 MCP 网络白名单，请重新激活连接"
            disabled += 1
            continue
        installation.network_authorization_json = (
            None
            if authorization is None
            else json.dumps(
                authorization.to_jsonable(), ensure_ascii=False, separators=(",", ":")
            )
        )

    from src.api.services.mcp_service import bump_config_version

    bump_config_version(db, None)
    db.commit()
    return personal_mcp_network_policy_payload(
        db,
        disabled_installations=disabled,
    )
