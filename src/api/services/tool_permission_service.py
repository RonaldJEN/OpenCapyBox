"""Policy evaluation and durable human approval for all agent tools."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import or_, text
from sqlalchemy.orm import Session as DBSession

from src.api.models.tool_permission import (
    ToolApprovalRequest,
    ToolPermissionAudit,
    ToolPermissionRule,
)
from src.api.services.secret_crypto import decrypt_secret, encrypt_secret
from src.api.config import get_settings
from src.api.utils.timezone import now_naive


VALID_EFFECTS = {"allow", "ask", "deny"}
VALID_PROVIDERS = {"builtin", "mcp"}
VALID_SCOPES = {"platform", "user", "session"}
APPROVAL_RESOLUTIONS = {"allow_once", "allow_session", "allow_always", "deny"}
RULE_CONDITIONS_VERSION = 1
APPROVAL_OUTCOME_UNKNOWN_ERROR = (
    "外部副作用结果未知，工具可能已执行；绝不自动重试。"
)
APPROVAL_EXECUTION_FAILED_ERROR = "工具执行失败，详细结果已加密保存。"
_MAX_AUDIT_REASON_BYTES = 8 * 1024
# Leave ample headroom for scope/server parameters on SQLite builds that retain
# the historical 999-variable statement limit. Larger batches load the already
# bounded scope/provider/server policy slice and filter tool names in memory.
_MAX_PERMISSION_RULE_NAME_BINDS = 500
_TOOL_SELECTION_LOCK_NAMESPACE = "tool-permission-user-selection:v1"


def _validated_rule_conditions(
    provider: str,
    conditions: Any,
) -> dict[str, Any]:
    """Validate the versioned, provider-specific policy condition envelope.

    A non-null condition is security-sensitive metadata.  Keep the accepted
    shapes deliberately small so a typo or a future, unsupported condition can
    never turn a conditional grant into an unconditional one.
    """

    if not isinstance(conditions, dict) or not conditions:
        raise ValueError("rule conditions must be a non-empty object")
    version = conditions.get("version")
    if isinstance(version, bool) or version != RULE_CONDITIONS_VERSION:
        raise ValueError(
            f"rule conditions require version={RULE_CONDITIONS_VERSION}"
        )

    if provider == "mcp":
        expected_keys = {
            "version",
            "schema_hash",
            "connection_fingerprint",
        }
    elif provider == "builtin":
        expected_keys = {"version", "schema_hash"}
    else:
        raise ValueError(f"unsupported tool provider: {provider}")

    unknown_keys = set(conditions) - expected_keys
    missing_keys = expected_keys - set(conditions)
    if unknown_keys:
        raise ValueError(
            "unsupported rule condition keys: " + ", ".join(sorted(unknown_keys))
        )
    if missing_keys:
        raise ValueError(
            "missing rule condition keys: " + ", ".join(sorted(missing_keys))
        )

    schema_hash = conditions.get("schema_hash")
    if not isinstance(schema_hash, str) or not schema_hash:
        raise ValueError("rule conditions require a non-empty schema_hash")
    if provider == "mcp":
        connection_fingerprint = conditions.get("connection_fingerprint")
        if not isinstance(connection_fingerprint, str) or not connection_fingerprint:
            raise ValueError(
                "MCP rule conditions require a non-empty connection_fingerprint"
            )
    return dict(conditions)


@dataclass(frozen=True)
class ToolRef:
    provider: str
    tool_name: str
    server_id: str | None = None

    @property
    def canonical(self) -> str:
        if self.provider == "mcp":
            return f"mcp:{self.server_id}:{self.tool_name}"
        return f"builtin:{self.tool_name}"


@dataclass(frozen=True)
class PolicyDecision:
    effect: str
    reason: str
    matched_rule_id: str | None = None
    managed: bool = False

    @property
    def exposed(self) -> bool:
        return self.effect != "deny"


@dataclass(frozen=True)
class ToolPermissionCheck:
    """One policy input for :func:`evaluate_tool_permissions`.

    Keeping the mutable database lookup outside the per-tool evaluator lets
    callers project a complete tool surface with one rule query while retaining
    the exact same matching semantics used at the execution boundary.
    """

    ref: ToolRef
    default_effect: str | None = None
    schema_hash: str | None = None
    connection_fingerprint: str | None = None


@dataclass(frozen=True)
class ApprovalClaim:
    request_id: str
    resolution: str
    should_execute: bool
    arguments: dict[str, Any]
    request: ToolApprovalRequest
    claim_token: str | None = None
    lease_expires_at: datetime | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def arguments_hash(arguments: Any) -> str:
    return hashlib.sha256(_canonical_json(arguments).encode("utf-8")).hexdigest()


def _bounded_audit_reason(reason: str | None) -> str | None:
    """Bound untrusted execution errors before they reach append-only storage."""

    if reason is None:
        return None
    normalized = str(reason)
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_AUDIT_REASON_BYTES:
        return normalized

    digest = hashlib.sha256(encoded).hexdigest()
    marker = f"\n… [truncated; sha256={digest}]".encode("utf-8")
    prefix_budget = max(0, _MAX_AUDIT_REASON_BYTES - len(marker))
    prefix = encoded[:prefix_budget].decode("utf-8", errors="ignore")
    return prefix + marker.decode("utf-8")


def _validate_ref(ref: ToolRef) -> None:
    if ref.provider not in VALID_PROVIDERS:
        raise ValueError(f"unsupported tool provider: {ref.provider}")
    if not isinstance(ref.tool_name, str) or not ref.tool_name or len(ref.tool_name) > 255:
        raise ValueError("tool_name must contain 1-255 characters")
    if ref.tool_name != ref.tool_name.strip():
        raise ValueError("tool_name cannot contain leading or trailing whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in ref.tool_name):
        raise ValueError("tool_name cannot contain ASCII control characters")
    if ref.provider == "mcp" and not ref.server_id:
        raise ValueError("MCP ToolRef requires server_id")
    if ref.provider == "builtin" and ref.server_id is not None:
        raise ValueError("builtin ToolRef cannot contain server_id")


def _tool_selection_advisory_lock_key(user_id: str, ref: ToolRef) -> int:
    """Return a stable signed-bigint lock key for one user's exact tool ref."""

    identity = _canonical_json(
        [
            _TOOL_SELECTION_LOCK_NAMESPACE,
            user_id,
            ref.provider,
            ref.server_id,
            ref.tool_name,
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _acquire_user_tool_selection_lock(
    db: DBSession,
    *,
    user_id: str,
    ref: ToolRef,
) -> None:
    """Serialize exact user-tool selection changes for this transaction.

    PostgreSQL transaction-level advisory locks are released automatically on
    commit or rollback, including when a batch caller owns the transaction.
    SQLite test databases do not implement advisory locks and already
    serialize writers, so no PostgreSQL-only SQL is emitted there.
    """

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _tool_selection_advisory_lock_key(user_id, ref)},
    )


def _load_candidate_rules(
    db: DBSession,
    *,
    user_id: str,
    session_id: str | None,
    checks: list[ToolPermissionCheck],
) -> list[ToolPermissionRule]:
    """Load all SQL candidates for a batch in one query."""

    if not checks:
        return []
    now = now_naive()
    scopes = [
        (ToolPermissionRule.scope_type == "platform")
        & (ToolPermissionRule.scope_id.is_(None)),
        (ToolPermissionRule.scope_type == "user")
        & (ToolPermissionRule.scope_id == user_id),
    ]
    if session_id:
        scopes.append(
            (ToolPermissionRule.scope_type == "session")
            & (ToolPermissionRule.scope_id == session_id)
        )

    tool_names = {"*"}
    tool_names.update(check.ref.tool_name for check in checks)
    identity_filters = []
    if any(check.ref.provider == "builtin" for check in checks):
        identity_filters.append(
            (ToolPermissionRule.provider == "builtin")
            & (ToolPermissionRule.server_id.is_(None))
        )
    mcp_server_ids = {
        check.ref.server_id
        for check in checks
        if check.ref.provider == "mcp" and check.ref.server_id is not None
    }
    if mcp_server_ids:
        identity_filters.append(
            (ToolPermissionRule.provider == "mcp")
            & or_(
                ToolPermissionRule.server_id.in_(mcp_server_ids),
                ToolPermissionRule.server_id.is_(None),
            )
        )

    filters = [
        ToolPermissionRule.enabled.is_(True),
        or_(*scopes),
        or_(*identity_filters),
        or_(
            ToolPermissionRule.expires_at.is_(None),
            ToolPermissionRule.expires_at > now,
        ),
    ]
    if len(tool_names) <= _MAX_PERMISSION_RULE_NAME_BINDS:
        filters.append(ToolPermissionRule.tool_name.in_(tool_names))
    return list(db.query(ToolPermissionRule).filter(*filters).all())


def _applicable_rules_from_loaded(
    rules: Iterable[ToolPermissionRule],
    *,
    ref: ToolRef,
    schema_hash: str | None,
    connection_fingerprint: str | None,
) -> list[ToolPermissionRule]:
    """Filter a preloaded policy snapshot without further database access."""

    return [
        rule
        for rule in rules
        if rule.provider == ref.provider
        and rule.tool_name in {ref.tool_name, "*"}
        and (
            (ref.provider == "builtin" and rule.server_id is None)
            or (ref.provider == "mcp" and rule.server_id in {None, ref.server_id})
        )
        and _rule_conditions_match(
            rule,
            schema_hash=schema_hash,
            connection_fingerprint=connection_fingerprint,
        )
    ]


def _rule_conditions_match(
    rule: ToolPermissionRule,
    *,
    schema_hash: str | None,
    connection_fingerprint: str | None,
) -> bool:
    """Match only the current, fully validated condition schema.

    ``NULL`` is the sole representation of an unconditional rule.  Every
    non-null value is parsed and validated fail-closed so corrupt or legacy
    metadata cannot broaden access.
    """

    if rule.conditions_json is None:
        return True
    try:
        conditions = json.loads(rule.conditions_json)
        conditions = _validated_rule_conditions(rule.provider, conditions)
    except (TypeError, json.JSONDecodeError, ValueError):
        # An invalid conditional ALLOW must never grant access.  Conversely,
        # ignoring an invalid ASK/DENY would remove a restriction, so legacy or
        # corrupt restrictive rows remain conservatively applicable.
        return rule.effect in {"ask", "deny"}

    if not schema_hash or schema_hash != conditions["schema_hash"]:
        return False
    if rule.provider == "mcp":
        return bool(
            connection_fingerprint
            and connection_fingerprint == conditions["connection_fingerprint"]
        )
    return True


def _restrictiveness(effect: str) -> int:
    return {"allow": 1, "ask": 2, "deny": 3}[effect]


def _specificity(rule: ToolPermissionRule, ref: ToolRef) -> tuple[int, int, int, int]:
    scope_rank = {"platform": 0, "user": 1, "session": 2}.get(rule.scope_type, -1)
    server_rank = int(bool(ref.server_id and rule.server_id == ref.server_id))
    tool_rank = int(rule.tool_name == ref.tool_name)
    return scope_rank, server_rank, tool_rank, int(rule.priority or 0)


def _evaluate_tool_permission_from_loaded(
    rules: Iterable[ToolPermissionRule],
    *,
    ref: ToolRef,
    default_effect: str,
    schema_hash: str | None = None,
    connection_fingerprint: str | None = None,
) -> PolicyDecision:
    """Pure in-memory policy resolution for one validated check."""

    applicable_rules = _applicable_rules_from_loaded(
        rules,
        ref=ref,
        schema_hash=schema_hash,
        connection_fingerprint=connection_fingerprint,
    )

    managed_rules = [rule for rule in applicable_rules if rule.managed]
    hard_deny = next((rule for rule in managed_rules if rule.effect == "deny"), None)
    if hard_deny:
        return PolicyDecision(
            effect="deny",
            reason="blocked by managed policy",
            matched_rule_id=hard_deny.id,
            managed=True,
        )

    local_rules = [rule for rule in applicable_rules if not rule.managed]
    managed_allow = next((rule for rule in managed_rules if rule.effect == "allow"), None)
    local_decision: PolicyDecision
    if local_rules:
        best_specificity = max(_specificity(rule, ref) for rule in local_rules)
        finalists = [
            rule for rule in local_rules if _specificity(rule, ref) == best_specificity
        ]
        winner = max(finalists, key=lambda rule: _restrictiveness(rule.effect))
        local_decision = PolicyDecision(
            effect=winner.effect,
            reason="matched explicit permission rule",
            matched_rule_id=winner.id,
            managed=False,
        )
    else:
        local_decision = PolicyDecision(
            effect="allow" if managed_allow else default_effect,
            reason=(
                "allowed by managed policy"
                if managed_allow
                else f"using {ref.provider} default policy"
            ),
            matched_rule_id=managed_allow.id if managed_allow else None,
            managed=bool(managed_allow),
        )

    # Managed ASK is a ceiling: a child rule may tighten it to DENY but never
    # relax it to ALLOW.
    managed_ask = next((rule for rule in managed_rules if rule.effect == "ask"), None)
    if managed_ask and local_decision.effect == "allow":
        return PolicyDecision(
            effect="ask",
            reason="confirmation required by managed policy",
            matched_rule_id=managed_ask.id,
            managed=True,
        )
    return local_decision


def evaluate_tool_permissions(
    db: DBSession,
    *,
    user_id: str,
    session_id: str | None,
    checks: Iterable[ToolPermissionCheck],
) -> list[PolicyDecision]:
    """Evaluate many tools using one immutable rule snapshot and one SQL query."""

    prepared: list[tuple[ToolPermissionCheck, str]] = []
    for check in checks:
        if not isinstance(check, ToolPermissionCheck):
            raise TypeError("checks must contain ToolPermissionCheck values")
        _validate_ref(check.ref)
        default_effect = check.default_effect
        if default_effect is None:
            default_effect = "allow"
        if default_effect not in VALID_EFFECTS:
            raise ValueError(f"invalid default effect: {default_effect}")
        prepared.append((check, default_effect))

    if not prepared:
        return []
    loaded_rules = _load_candidate_rules(
        db,
        user_id=user_id,
        session_id=session_id,
        checks=[check for check, _default in prepared],
    )
    return [
        _evaluate_tool_permission_from_loaded(
            loaded_rules,
            ref=check.ref,
            default_effect=default_effect,
            schema_hash=check.schema_hash,
            connection_fingerprint=check.connection_fingerprint,
        )
        for check, default_effect in prepared
    ]


def evaluate_tool_permission(
    db: DBSession,
    *,
    user_id: str,
    session_id: str | None,
    ref: ToolRef,
    default_effect: str | None = None,
    schema_hash: str | None = None,
    connection_fingerprint: str | None = None,
) -> PolicyDecision:
    """Resolve one tool through the same evaluator used by batch callers."""

    return evaluate_tool_permissions(
        db,
        user_id=user_id,
        session_id=session_id,
        checks=[ToolPermissionCheck(
            ref=ref,
            default_effect=default_effect,
            schema_hash=schema_hash,
            connection_fingerprint=connection_fingerprint,
        )],
    )[0]


def policy_version_for_user(
    db: DBSession,
    *,
    user_id: str,
    session_id: str | None = None,
) -> str:
    rows = db.query(
        ToolPermissionRule.id,
        ToolPermissionRule.updated_at,
        ToolPermissionRule.enabled,
    ).filter(
        or_(
            (ToolPermissionRule.scope_type == "platform")
            & (ToolPermissionRule.scope_id.is_(None)),
            (ToolPermissionRule.scope_type == "user")
            & (ToolPermissionRule.scope_id == user_id),
            (ToolPermissionRule.scope_type == "session")
            & (ToolPermissionRule.scope_id == session_id)
            if session_id
            else False,
        )
    ).all()
    value = "|".join(
        f"{row.id}:{row.updated_at.isoformat() if row.updated_at else ''}:{int(bool(row.enabled))}"
        for row in sorted(rows, key=lambda item: item.id)
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def create_permission_rule(
    db: DBSession,
    *,
    scope_type: str,
    scope_id: str | None,
    ref: ToolRef,
    effect: str,
    created_by: str,
    priority: int = 0,
    managed: bool = False,
    description: str | None = None,
    expires_at: datetime | None = None,
    conditions: dict[str, Any] | None = None,
    commit: bool = True,
) -> ToolPermissionRule:
    _validate_ref(ref)
    if scope_type not in VALID_SCOPES:
        raise ValueError(f"unsupported permission scope: {scope_type}")
    if effect not in VALID_EFFECTS:
        raise ValueError(f"unsupported permission effect: {effect}")
    if scope_type == "platform" and scope_id is not None:
        raise ValueError("platform rules must not have scope_id")
    if scope_type != "platform" and not scope_id:
        raise ValueError(f"{scope_type} rules require scope_id")
    if managed and scope_type != "platform":
        raise ValueError("only platform rules can be managed")
    if conditions is not None and effect != "allow":
        raise ValueError("conditions are supported only for allow rules")
    validated_conditions = (
        _validated_rule_conditions(ref.provider, conditions)
        if conditions is not None
        else None
    )

    rule = ToolPermissionRule(
        id=str(uuid.uuid4()),
        scope_type=scope_type,
        scope_id=scope_id,
        provider=ref.provider,
        server_id=ref.server_id,
        tool_name=ref.tool_name,
        effect=effect,
        priority=int(priority),
        managed=bool(managed),
        conditions_json=(
            _canonical_json(validated_conditions)
            if validated_conditions is not None
            else None
        ),
        description=(description or "").strip() or None,
        enabled=True,
        expires_at=expires_at,
        created_by=created_by,
    )
    db.add(rule)
    if commit:
        db.commit()
        db.refresh(rule)
    else:
        db.flush()
    return rule


def replace_user_tool_selection(
    db: DBSession,
    *,
    user_id: str,
    ref: ToolRef,
    effect: str,
    commit: bool = True,
) -> ToolPermissionRule:
    """Make an explicit user choice the only rule for one exact tool identity.

    Deletes every non-managed, user-scoped rule owned by this user that targets
    the exact provider/server/tool identity — including a prior schema-bound
    approval grant — then creates one unconditional, permanent, enabled user
    rule.  Platform, session, other users', wildcard and other-tool rules are
    never touched, so a manual selection supersedes only the stale binding it
    replaces.  The delete and insert share one transaction: any failure rolls
    back to the prior rule set without leaving a partial state.
    """

    _validate_ref(ref)
    if effect not in VALID_EFFECTS:
        raise ValueError(f"unsupported permission effect: {effect}")

    _acquire_user_tool_selection_lock(db, user_id=user_id, ref=ref)

    return _replace_user_tool_selection_unlocked(
        db,
        user_id=user_id,
        ref=ref,
        effect=effect,
        commit=commit,
    )


def _replace_user_tool_selection_unlocked(
    db: DBSession,
    *,
    user_id: str,
    ref: ToolRef,
    effect: str,
    commit: bool,
) -> ToolPermissionRule:
    """Replace an exact selection after its transaction lock is held."""

    server_match = (
        ToolPermissionRule.server_id == ref.server_id
        if ref.server_id is not None
        else ToolPermissionRule.server_id.is_(None)
    )
    db.query(ToolPermissionRule).filter(
        ToolPermissionRule.scope_type == "user",
        ToolPermissionRule.scope_id == user_id,
        ToolPermissionRule.managed.is_(False),
        ToolPermissionRule.provider == ref.provider,
        ToolPermissionRule.tool_name == ref.tool_name,
        server_match,
    ).delete(synchronize_session=False)

    rule = create_permission_rule(
        db,
        scope_type="user",
        scope_id=user_id,
        ref=ref,
        effect=effect,
        created_by=user_id,
        description=f"用户为 {ref.canonical} 手动设置的规则",
        commit=False,
    )
    if commit:
        db.commit()
        db.refresh(rule)
    else:
        db.flush()
    return rule


def clear_user_tool_selection(
    db: DBSession,
    *,
    user_id: str,
    ref: ToolRef,
    commit: bool = True,
) -> int:
    """Restore the provider default for one exact tool by deleting user rules.

    Removes every non-managed, user-scoped rule owned by this user that targets
    the exact provider/server/tool identity — including prior schema-bound
    approval grants and legacy stale conditional rules — in a single
    transaction.  Platform, session, other users', wildcard and other-tool
    rules are never touched.  MCP access is intentionally NOT re-validated so a
    user can still clean up rules for a deleted or unpublished server (spec
    §4.6 physical cleanup).  Returns the number of rules removed.
    """

    _validate_ref(ref)
    _acquire_user_tool_selection_lock(db, user_id=user_id, ref=ref)
    server_match = (
        ToolPermissionRule.server_id == ref.server_id
        if ref.server_id is not None
        else ToolPermissionRule.server_id.is_(None)
    )
    removed = (
        db.query(ToolPermissionRule)
        .filter(
            ToolPermissionRule.scope_type == "user",
            ToolPermissionRule.scope_id == user_id,
            ToolPermissionRule.managed.is_(False),
            ToolPermissionRule.provider == ref.provider,
            ToolPermissionRule.tool_name == ref.tool_name,
            server_match,
        )
        .delete(synchronize_session=False)
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return int(removed)


def replace_user_tool_selections(
    db: DBSession,
    *,
    user_id: str,
    refs: list[ToolRef],
    effect: str,
    commit: bool = True,
) -> list[ToolPermissionRule]:
    """Apply one explicit choice to many exact tools in a single transaction.

    Each tool is replaced with the same atomic semantics as
    :func:`replace_user_tool_selection`.  All deletes and inserts share one
    transaction, so any failure rolls back the whole batch without leaving a
    partial state.
    """

    if effect not in VALID_EFFECTS:
        raise ValueError(f"unsupported permission effect: {effect}")

    validated_refs: list[ToolRef] = []
    for ref in refs:
        _validate_ref(ref)
        validated_refs.append(ref)

    # Acquire every identity lock before mutating anything.  A deterministic
    # order prevents two overlapping batch requests from deadlocking when the
    # caller supplies the same refs in a different order.
    unique_refs = set(validated_refs)
    for ref in sorted(
        unique_refs,
        key=lambda item: (item.provider, item.server_id or "", item.tool_name),
    ):
        _acquire_user_tool_selection_lock(db, user_id=user_id, ref=ref)

    rules = [
        _replace_user_tool_selection_unlocked(
            db,
            user_id=user_id,
            ref=ref,
            effect=effect,
            commit=False,
        )
        for ref in validated_refs
    ]
    if commit:
        db.commit()
        for rule in rules:
            db.refresh(rule)
    else:
        db.flush()
    return rules


def rule_to_payload(rule: ToolPermissionRule) -> dict[str, Any]:
    conditions = None
    if rule.conditions_json:
        try:
            conditions = json.loads(rule.conditions_json)
        except (TypeError, json.JSONDecodeError):
            conditions = None
    return {
        "id": rule.id,
        "scope_type": rule.scope_type,
        "scope_id": rule.scope_id,
        "provider": rule.provider,
        "server_id": rule.server_id,
        "tool_name": rule.tool_name,
        "tool_ref": ToolRef(
            provider=rule.provider,
            server_id=rule.server_id,
            tool_name=rule.tool_name,
        ).canonical,
        "effect": rule.effect,
        "priority": int(rule.priority or 0),
        "managed": bool(rule.managed),
        "conditions": conditions,
        "description": rule.description,
        "enabled": bool(rule.enabled),
        "expires_at": rule.expires_at.isoformat() if rule.expires_at else None,
        "created_by": rule.created_by,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }


def list_rules_for_user(db: DBSession, user_id: str) -> list[ToolPermissionRule]:
    return list(
        db.query(ToolPermissionRule)
        .filter(
            or_(
                (ToolPermissionRule.scope_type == "platform")
                & (ToolPermissionRule.scope_id.is_(None)),
                (ToolPermissionRule.scope_type == "user")
                & (ToolPermissionRule.scope_id == user_id),
            )
        )
        .order_by(
            ToolPermissionRule.managed.desc(),
            ToolPermissionRule.priority.desc(),
            ToolPermissionRule.created_at.asc(),
        )
        .all()
    )


def create_approval_request(
    db: DBSession,
    *,
    request_id: str,
    user_id: str,
    session_id: str,
    run_id: str,
    tool_call_id: str,
    ref: ToolRef,
    model_tool_name: str,
    arguments: dict[str, Any],
    installation_id: str | None = None,
    schema_hash: str | None = None,
    connection_fingerprint: str | None = None,
    policy_version: str | None = None,
    matched_rule_id: str | None = None,
    commit: bool = True,
) -> ToolApprovalRequest:
    _validate_ref(ref)
    raw_arguments = _canonical_json(arguments)
    request = ToolApprovalRequest(
        id=request_id,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        provider=ref.provider,
        server_id=ref.server_id,
        installation_id=installation_id,
        tool_name=ref.tool_name,
        model_tool_name=model_tool_name,
        arguments_encrypted=encrypt_secret(raw_arguments),
        arguments_hash=hashlib.sha256(raw_arguments.encode("utf-8")).hexdigest(),
        schema_hash=schema_hash,
        connection_fingerprint=connection_fingerprint,
        policy_version=policy_version,
        matched_rule_id=matched_rule_id,
        status="requested",
    )
    db.add(request)
    if commit:
        db.commit()
        db.refresh(request)
    else:
        db.flush()
    return request


def load_approval_arguments(request: ToolApprovalRequest) -> dict[str, Any]:
    value = json.loads(decrypt_secret(request.arguments_encrypted))
    if not isinstance(value, dict):
        raise ValueError("stored tool approval arguments are not an object")
    if arguments_hash(value) != request.arguments_hash:
        raise ValueError("stored tool approval arguments failed integrity check")
    return value


def claim_approval_request(
    db: DBSession,
    *,
    request_id: str,
    user_id: str,
    resolution: str,
    commit: bool = True,
) -> ApprovalClaim:
    if resolution not in APPROVAL_RESOLUTIONS:
        raise ValueError(f"unsupported approval resolution: {resolution}")
    now = now_naive()
    should_execute = resolution != "deny"
    claim_token = uuid.uuid4().hex if should_execute else None
    if should_execute:
        lease_seconds = float(get_settings().tool_approval_execution_lease_seconds)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
    else:
        lease_expires_at = None

    # Exactly one contender may transition requested -> executing/denied. A
    # SELECT ... FOR UPDATE is insufficient on SQLite and easier to misuse on
    # other backends; this status-guarded UPDATE is the execution claim CAS.
    updated = (
        db.query(ToolApprovalRequest)
        .filter(
            ToolApprovalRequest.id == request_id,
            ToolApprovalRequest.user_id == user_id,
            ToolApprovalRequest.status == "requested",
        )
        .update(
            {
                "resolution": resolution,
                "resolved_at": now,
                "status": "executing" if should_execute else "denied",
                "execution_started_at": now if should_execute else None,
                "execution_claim_token": claim_token,
                "execution_lease_expires_at": lease_expires_at,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        existing = (
            db.query(ToolApprovalRequest)
            .populate_existing()
            .filter(
                ToolApprovalRequest.id == request_id,
                ToolApprovalRequest.user_id == user_id,
            )
            .first()
        )
        if existing is None:
            raise LookupError("tool approval request not found")
        raise RuntimeError(
            f"tool approval request already resolved: {existing.status}"
        )
    request = (
        db.query(ToolApprovalRequest)
        .populate_existing()
        .filter(
            ToolApprovalRequest.id == request_id,
            ToolApprovalRequest.user_id == user_id,
        )
        .one()
    )

    if resolution in {"allow_session", "allow_always"}:
        ref = ToolRef(
            provider=request.provider,
            server_id=request.server_id,
            tool_name=request.tool_name,
        )
        remember_binding_valid = True
        if request.provider == "mcp":
            from src.api.models.mcp import McpToolSnapshot
            from src.api.services.mcp_runtime import resolve_effective_mcp_installation

            current_installation = None
            if request.installation_id:
                current_installation = resolve_effective_mcp_installation(
                    db,
                    user_id=request.user_id,
                    installation_id=request.installation_id,
                )
            current_snapshot = None
            if current_installation is not None:
                current_snapshot = (
                    db.query(McpToolSnapshot)
                    .filter(
                        McpToolSnapshot.installation_id == request.installation_id,
                        McpToolSnapshot.tool_name == request.tool_name,
                    )
                    .first()
                )
            remember_binding_valid = bool(
                request.server_id
                and request.schema_hash
                and request.connection_fingerprint
                and current_installation is not None
                and current_installation.server_id == request.server_id
                and current_installation.execution_fingerprint
                == request.connection_fingerprint
                and current_snapshot is not None
                and current_snapshot.schema_hash == request.schema_hash
                and current_snapshot.connection_fingerprint
                == request.connection_fingerprint
            )
        if remember_binding_valid:
            scope_type = "session" if resolution == "allow_session" else "user"
            scope_id = request.session_id if scope_type == "session" else request.user_id
            create_permission_rule(
                db,
                scope_type=scope_type,
                scope_id=scope_id,
                ref=ref,
                effect="allow",
                created_by=user_id,
                description=f"Created from approval {request.id}",
                conditions=(
                    {
                        "version": RULE_CONDITIONS_VERSION,
                        "schema_hash": request.schema_hash,
                        "connection_fingerprint": request.connection_fingerprint,
                    }
                    if request.provider == "mcp"
                    else None
                ),
                commit=False,
            )

    arguments = load_approval_arguments(request)
    if commit:
        db.commit()
        db.refresh(request)
    else:
        db.flush()
    return ApprovalClaim(
        request_id=request.id,
        resolution=resolution,
        should_execute=should_execute,
        arguments=arguments,
        request=request,
        claim_token=request.execution_claim_token,
        lease_expires_at=request.execution_lease_expires_at,
    )


def renew_approval_execution_lease(
    db: DBSession,
    *,
    request_id: str,
    user_id: str,
    claim_token: str,
    lease_seconds: float | None = None,
    commit: bool = True,
) -> bool:
    """Extend an executing claim without ever creating a new execution claim.

    Matching the opaque claim token fences stale workers.  Renewal is allowed
    after the timestamp has elapsed only while the row is still ``executing``;
    whichever transaction first changes the row (renew or reconcile) wins.
    """

    if not claim_token:
        return False
    seconds = float(
        lease_seconds
        if lease_seconds is not None
        else get_settings().tool_approval_execution_lease_seconds
    )
    if seconds <= 0:
        raise ValueError("approval execution lease_seconds must be positive")
    expires_at = now_naive() + timedelta(seconds=seconds)
    updated = (
        db.query(ToolApprovalRequest)
        .filter(
            ToolApprovalRequest.id == request_id,
            ToolApprovalRequest.user_id == user_id,
            ToolApprovalRequest.status == "executing",
            ToolApprovalRequest.execution_claim_token == claim_token,
        )
        .update(
            {"execution_lease_expires_at": expires_at},
            synchronize_session=False,
        )
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return bool(updated)


def reconcile_expired_approval_leases(
    db: DBSession,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Atomically make abandoned executions terminal without retrying them."""

    reconciled_at = now or now_naive()
    updated = (
        db.query(ToolApprovalRequest)
        .filter(
            ToolApprovalRequest.status == "executing",
            or_(
                ToolApprovalRequest.execution_lease_expires_at.is_(None),
                ToolApprovalRequest.execution_lease_expires_at <= reconciled_at,
            ),
        )
        .update(
            {
                "status": "unknown",
                "error": APPROVAL_OUTCOME_UNKNOWN_ERROR,
                "completed_at": reconciled_at,
                "execution_claim_token": None,
                "execution_lease_expires_at": None,
            },
            synchronize_session=False,
        )
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return int(updated or 0)


def finish_approval_request(
    db: DBSession,
    *,
    request_id: str,
    user_id: str,
    claim_token: str | None,
    result_content: str,
    success: bool,
    outcome_uncertain: bool = False,
    commit: bool = True,
) -> ToolApprovalRequest:
    request = (
        db.query(ToolApprovalRequest)
        .filter(
            ToolApprovalRequest.id == request_id,
            ToolApprovalRequest.user_id == user_id,
        )
        .with_for_update()
        .first()
    )
    if request is None:
        raise LookupError("tool approval request not found")
    if request.status != "executing":
        raise RuntimeError(f"approval request is not executing: {request.status}")
    if not claim_token or request.execution_claim_token != claim_token:
        raise RuntimeError("approval execution claim token does not match")
    request.status = (
        "unknown" if outcome_uncertain else "executed" if success else "failed"
    )
    request.result_encrypted = encrypt_secret(result_content)
    request.error = (
        APPROVAL_OUTCOME_UNKNOWN_ERROR
        if outcome_uncertain
        else None if success else APPROVAL_EXECUTION_FAILED_ERROR
    )
    request.completed_at = now_naive()
    request.execution_claim_token = None
    request.execution_lease_expires_at = None
    if commit:
        db.commit()
        db.refresh(request)
    else:
        db.flush()
    return request


def record_permission_audit(
    db: DBSession,
    *,
    user_id: str,
    ref: ToolRef,
    effect: str,
    outcome: str,
    session_id: str | None = None,
    run_id: str | None = None,
    tool_call_id: str | None = None,
    matched_rule_id: str | None = None,
    reason: str | None = None,
    arguments: dict[str, Any] | None = None,
    commit: bool = True,
) -> ToolPermissionAudit:
    _validate_ref(ref)
    row = ToolPermissionAudit(
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        provider=ref.provider,
        server_id=ref.server_id,
        tool_name=ref.tool_name,
        effect=effect,
        matched_rule_id=matched_rule_id,
        reason=_bounded_audit_reason(reason),
        arguments_hash=arguments_hash(arguments) if arguments is not None else None,
        outcome=outcome,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row
