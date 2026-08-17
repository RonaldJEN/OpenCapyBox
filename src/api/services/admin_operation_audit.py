"""Request-scoped audit support for administrator routes.

The authentication dependency skips explicitly classified L0 reads. For L1
through L3 actions it durably creates ``started`` evidence before the endpoint
executes. :class:`AdminAuditRoute` then transitions that row to a terminal
outcome before returning a response or propagating an exception. Audit writes
use their own short transaction so endpoint commits or rollbacks cannot erase
the evidence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from src.api.models.admin_operation_log import AdminOperationLog
from src.api.models.database import SessionLocal
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

_ACTION_ATTR = "__admin_audit_action__"
_STATE_ATTR = "_admin_operation_audit"
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")

# L0 actions are intentionally explicit. Adding a new ``*.list`` action must
# not silently make it unaudited; it belongs here only after review.
L0_ACTIONS = frozenset(
    {
        "overview.read",
        "system.read",
        "sandbox.list",
        "model.list",
        "model_group.list",
        "mcp.list",
        "tool_permission.list",
    }
)
L1_ACTIONS = frozenset(
    {
        "audit_log.list",
        "session.list",
        "session.search",
        "session.view",
        "user.list",
        "user.login_history.view",
        "mcp.personal_network_policy.list",
    }
)
L2_ACTIONS = frozenset(
    {
        "step.review.update",
        "user.create",
        "user.enabled.update",
        "user.admin.update",
        "user.token_limits.update",
        "user.model_groups.update",
        "user.password.reset",
        "user.delete",
        "user.export",
        "sandbox.create",
        "sandbox.update",
        "sandbox.default.set",
        "sandbox.enabled.update",
        "user.sandbox.update",
        "model.create",
        "model.update",
        "model.delete",
        "model.settings.update",
        "model_group.create",
        "model_group.update",
        "model_group.models.update",
        "model_group.users.update",
        "mcp.create",
        "mcp.update",
        "mcp.delete",
        "mcp.test",
        "mcp.personal_network_policy.update",
        "tool_permission.create",
        "tool_permission.update",
        "tool_permission.delete",
        "audit_log.export",
    }
)
L3_ACTIONS = frozenset({"step.view"})
HIGH_RISK_ACTIONS = L3_ACTIONS

# Details are intentionally constrained to operational metadata. Exact safe
# keys cover current routes; suffix rules support future counts/boolean flags
# without admitting arbitrary request payloads.
_DETAIL_KEYS = frozenset(
    {
        "status",
        "outcome",
        "has_search",
        "has_status_filter",
        "has_user_filter",
        "has_session_filter",
        "has_action_filter",
        "has_risk_filter",
        "risk_level",
        "limit",
        "offset",
        "page",
        "returned_count",
        "exported_count",
        "round_count",
        "step_count",
        "user_count",
        "credential_changed",
        "password_changed",
        "api_key_changed",
        "deleted",
    }
)
_DETAIL_SUFFIXES = (
    "_count",
    "_changed",
    "_enabled",
    "_filter",
    "_present",
)

_SENSITIVE_PARTS = (
    "password",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "bearer_token",
    "access_token",
    "refresh_token",
    "prompt",
    "thinking",
    "answer",
    "response_content",
    "request_content",
    "message",
    "headers",
)
_SAFE_SENSITIVE_FLAGS = frozenset(
    {
        "password_changed",
        "api_key_changed",
        "credential_changed",
    }
)

# Only these changed fields may retain safe before/after values. All other
# accepted fields are represented by name only.
_CHANGED_VALUE_KEYS = frozenset(
    {
        "manual_review_status",
        "enabled",
        "is_admin",
        "status",
        "effect",
        "priority",
        "token_limit_per_week",
        "token_limit_per_month",
        "sandbox_profile_id",
        "default_model_id",
        "cron_default_model_id",
        "subagent_default_model_id",
    }
)


def _audit_unavailable(exc: BaseException | None = None) -> HTTPException:
    if exc is not None:
        logger.exception("管理员操作审计写入失败", exc_info=exc)
    return HTTPException(status_code=503, detail="管理员操作审计暂不可用，请稍后重试")


def _clean_identifier(value: Any, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, int, uuid.UUID)) or isinstance(value, bool):
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:max_length]


def _clean_integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in _SAFE_SENSITIVE_FLAGS:
        return False
    # Token limits are numeric policy, not credentials.
    if normalized.startswith("token_limit"):
        return False
    return any(part in normalized for part in _SENSITIVE_PARTS)


def _safe_scalar(value: Any) -> bool | int | float | str | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (str, uuid.UUID)):
        return str(value)[:255]
    return None


def _sanitize_user_agent(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.lower()
    credential_markers = (
        "authorization",
        "bearer ",
        "cookie",
        "api_key",
        "api-key",
        "apikey",
        "access_token",
        "refresh_token",
        "password",
        "secret",
    )
    if any(marker in normalized for marker in credential_markers):
        return "[redacted]"
    return value[:2048]


def _safe_changed_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in ("before", "after"):
            if key in value:
                result[key] = _safe_scalar(value[key])
        return result or None
    if isinstance(value, (list, tuple, set, frozenset)):
        values = [_safe_scalar(item) for item in list(value)[:100]]
        return [item for item in values if item is not None]
    return _safe_scalar(value)


def _sanitize_changed_fields(changed_fields: Any) -> dict[str, Any] | None:
    if changed_fields is None:
        return None

    values: Mapping[Any, Any] | None = (
        changed_fields if isinstance(changed_fields, Mapping) else None
    )
    if values is not None:
        raw_names: Iterable[Any] = values.keys()
    elif isinstance(changed_fields, str):
        raw_names = (changed_fields,)
    elif isinstance(changed_fields, Iterable):
        raw_names = changed_fields
    else:
        return None

    fields: list[str] = []
    retained_values: dict[str, Any] = {}
    for raw_name in raw_names:
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()[:100]
        if not name or _is_sensitive_key(name):
            continue
        if name not in fields:
            fields.append(name)
        if values is not None and name in _CHANGED_VALUE_KEYS:
            safe_value = _safe_changed_value(values[raw_name])
            if safe_value is not None:
                retained_values[name] = safe_value
        if len(fields) >= 100:
            break

    if not fields:
        return None
    payload: dict[str, Any] = {"fields": sorted(fields)}
    if retained_values:
        payload["values"] = retained_values
    return payload


def _detail_key_allowed(key: str) -> bool:
    if _is_sensitive_key(key):
        return key in _SAFE_SENSITIVE_FLAGS
    return key in _DETAIL_KEYS or key.endswith(_DETAIL_SUFFIXES)


def _sanitize_details(details: Any) -> dict[str, Any] | None:
    if not isinstance(details, Mapping):
        return None
    result: dict[str, Any] = {}
    for raw_key, raw_value in details.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()[:100]
        if not key or not _detail_key_allowed(key):
            continue
        # Credential/secret-related audit details are flags only.
        if key.endswith("_changed"):
            result[key] = bool(raw_value)
            continue
        safe_value = _safe_scalar(raw_value)
        if safe_value is not None:
            result[key] = safe_value
        if len(result) >= 50:
            break
    return result or None


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _route_spec(request: Request) -> dict[str, Any] | None:
    route = request.scope.get("route")
    spec = getattr(route, "admin_audit_spec", None)
    if isinstance(spec, dict):
        return spec
    endpoint = request.scope.get("endpoint")
    spec = getattr(endpoint, _ACTION_ATTR, None)
    return spec if isinstance(spec, dict) else None


def _request_value(request: Request, parameter_name: str | None) -> Any:
    if not parameter_name:
        return None
    if parameter_name in request.path_params:
        return request.path_params[parameter_name]
    return request.query_params.get(parameter_name)


def admin_audit_action(
    action: str,
    target_type: str | None = None,
    target_param: str | None = None,
    session_param: str | None = None,
    step_param: str | None = None,
    target_user_param: str | None = None,
    query_action_param: str | None = None,
    query_action: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Attach stable audit metadata to an administrator endpoint."""

    if not _ACTION_RE.fullmatch(action):
        raise ValueError(f"invalid admin audit action: {action!r}")
    if (query_action_param is None) != (query_action is None):
        raise ValueError("query_action_param and query_action must be set together")
    if query_action is not None and not _ACTION_RE.fullmatch(query_action):
        raise ValueError(f"invalid query-derived admin audit action: {query_action!r}")
    spec = {
        "action": action,
        "target_type": _clean_identifier(target_type, max_length=50),
        "target_param": _clean_identifier(target_param, max_length=100),
        "session_param": _clean_identifier(session_param, max_length=100),
        "step_param": _clean_identifier(step_param, max_length=100),
        "target_user_param": _clean_identifier(target_user_param, max_length=100),
        "query_action_param": _clean_identifier(query_action_param, max_length=100),
        "query_action": query_action,
    }

    def decorator(endpoint: Callable[P, R]) -> Callable[P, R]:
        setattr(endpoint, _ACTION_ATTR, spec)
        return endpoint

    return decorator


def _initial_action(request: Request, spec: Mapping[str, Any]) -> str:
    """Resolve an action using request metadata available before execution."""

    action = str(spec["action"])
    query_param = spec.get("query_action_param")
    query_action = spec.get("query_action")
    if query_param and query_action and request.query_params.get(str(query_param)):
        return str(query_action)
    return action


def begin_admin_audit(request: Request, actor_user_id: str) -> str | None:
    """Skip explicit L0 reads or durably insert the request's ``started`` row.

    This must be called only after both authentication and administrator
    authorization have succeeded. Unknown actions remain audited by default.
    """

    existing = getattr(request.state, _STATE_ATTR, None)
    if isinstance(existing, dict) and existing.get("log_id"):
        return str(existing["request_id"])

    spec = _route_spec(request)
    if not spec or not spec.get("action"):
        # An authenticated administrator route without a declaration is an
        # audit outage, not permission to perform an unlogged action.
        raise _audit_unavailable()

    action = _initial_action(request, spec)
    if action in L0_ACTIONS:
        return None

    request_id = str(uuid.uuid4())
    route = request.scope.get("route")
    route_template = getattr(route, "path", None) or request.url.path
    client_host = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    row = AdminOperationLog(
        request_id=request_id,
        actor_user_id=str(actor_user_id)[:100],
        action=action,
        target_type=spec.get("target_type"),
        target_id=_clean_identifier(
            _request_value(request, spec.get("target_param")), max_length=255
        ),
        target_user_id=_clean_identifier(
            _request_value(request, spec.get("target_user_param")), max_length=100
        ),
        session_id=_clean_identifier(
            _request_value(request, spec.get("session_param")), max_length=36
        ),
        step_record_id=_clean_integer(
            _request_value(request, spec.get("step_param"))
        ),
        outcome="started",
        http_method=request.method.upper()[:10],
        route_template=str(route_template)[:255],
        ip_address=_clean_identifier(client_host, max_length=64),
        user_agent=_sanitize_user_agent(user_agent),
        started_at=now_naive(),
    )
    try:
        with SessionLocal() as db:
            db.add(row)
            db.commit()
            db.refresh(row)
    except Exception as exc:
        raise _audit_unavailable(exc) from exc

    setattr(
        request.state,
        _STATE_ATTR,
        {
            "log_id": row.id,
            "request_id": request_id,
            "action": action,
            "action_override": None,
            "target_id": row.target_id,
            "target_user_id": row.target_user_id,
            "session_id": row.session_id,
            "step_record_id": row.step_record_id,
            "changed_fields": None,
            "details": None,
        },
    )
    return request_id


def enrich_admin_audit(
    request: Request,
    *,
    action: str | None = None,
    target_id: Any = None,
    target_user_id: Any = None,
    session_id: Any = None,
    step_record_id: Any = None,
    changed_fields: Any = None,
    details: Any = None,
) -> None:
    """Merge endpoint-derived safe context into the pending audit event.

    Calls are intentionally a no-op when authentication is overridden in unit
    tests and no started row exists. Production admin dependencies always begin
    the event before endpoint execution.
    """

    context = getattr(request.state, _STATE_ATTR, None)
    if not isinstance(context, dict) or not context.get("log_id"):
        return

    if action is not None:
        if not _ACTION_RE.fullmatch(action):
            raise ValueError(f"invalid admin audit action override: {action!r}")
        context["action_override"] = action

    identifier_updates = {
        "target_id": (target_id, 255),
        "target_user_id": (target_user_id, 100),
        "session_id": (session_id, 36),
        "step_record_id": (step_record_id, None),
    }
    for key, (value, max_length) in identifier_updates.items():
        cleaned = (
            _clean_integer(value)
            if key == "step_record_id"
            else _clean_identifier(value, max_length=int(max_length))
        )
        if cleaned is not None:
            context[key] = cleaned

    sanitized_changes = _sanitize_changed_fields(changed_fields)
    if sanitized_changes:
        previous = context.get("changed_fields") or {"fields": []}
        fields = sorted(
            set(previous.get("fields", [])) | set(sanitized_changes.get("fields", []))
        )[:100]
        merged: dict[str, Any] = {"fields": fields}
        values = {
            **previous.get("values", {}),
            **sanitized_changes.get("values", {}),
        }
        if values:
            merged["values"] = values
        context["changed_fields"] = merged

    sanitized_details = _sanitize_details(details)
    if sanitized_details:
        context["details"] = {**(context.get("details") or {}), **sanitized_details}


def get_admin_audit_request_id(request: Request) -> str | None:
    """Return the server-generated correlation id for the current audit row."""

    context = getattr(request.state, _STATE_ATTR, None)
    if not isinstance(context, dict):
        return None
    return _clean_identifier(context.get("request_id"), max_length=36)


def finalize_admin_audit(request: Request, *, status_code: int) -> None:
    """CAS-transition a started row to its immutable terminal outcome."""

    context = getattr(request.state, _STATE_ATTR, None)
    if not isinstance(context, dict) or not context.get("log_id"):
        return
    if context.get("finalized"):
        raise _audit_unavailable()

    completed_at = now_naive()
    outcome = "failed" if int(status_code) >= 400 else "succeeded"
    values = {
        "action": context.get("action_override") or context["action"],
        "target_id": context.get("target_id"),
        "target_user_id": context.get("target_user_id"),
        "session_id": context.get("session_id"),
        "step_record_id": context.get("step_record_id"),
        "changed_fields": _json_text(context.get("changed_fields")),
        "details_json": _json_text(context.get("details")),
        "outcome": outcome,
        "status_code": int(status_code),
        "completed_at": completed_at,
    }
    try:
        with SessionLocal() as db:
            updated = (
                db.query(AdminOperationLog)
                .filter(
                    AdminOperationLog.id == context["log_id"],
                    AdminOperationLog.request_id == context["request_id"],
                    AdminOperationLog.outcome == "started",
                )
                .update(values, synchronize_session=False)
            )
            if updated != 1:
                db.rollback()
                raise RuntimeError("admin audit row is missing or already terminal")
            db.commit()
    except Exception as exc:
        raise _audit_unavailable(exc) from exc
    context["finalized"] = True


class AdminAuditRoute(APIRoute):
    """APIRoute that strictly finalizes an authenticated admin audit row."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        spec = getattr(self.endpoint, _ACTION_ATTR, None)
        self.admin_audit_spec = dict(spec) if isinstance(spec, dict) else None

    def get_route_handler(self) -> Callable[[Request], Any]:
        original_route_handler = super().get_route_handler()

        @wraps(original_route_handler)
        async def audited_route_handler(request: Request):
            try:
                response = await original_route_handler(request)
            except asyncio.CancelledError:
                finalize_admin_audit(request, status_code=499)
                raise
            except Exception as exc:
                status_code = (
                    422
                    if isinstance(exc, RequestValidationError)
                    else int(getattr(exc, "status_code", 500))
                )
                finalize_admin_audit(request, status_code=status_code)
                raise

            finalize_admin_audit(request, status_code=int(response.status_code))
            context = getattr(request.state, _STATE_ATTR, None)
            if isinstance(context, dict) and context.get("request_id"):
                response.headers.setdefault("X-Request-ID", context["request_id"])
            return response

        return audited_route_handler


__all__ = [
    "AdminAuditRoute",
    "HIGH_RISK_ACTIONS",
    "L0_ACTIONS",
    "L1_ACTIONS",
    "L2_ACTIONS",
    "L3_ACTIONS",
    "admin_audit_action",
    "begin_admin_audit",
    "enrich_admin_audit",
    "finalize_admin_audit",
    "get_admin_audit_request_id",
]
