"""Administrator-managed official MCP catalog routes."""

import logging

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_admin_user
from src.api.models.database import get_db
from src.api.schemas.mcp import (
    AdminMcpServerCreate,
    AdminMcpServerPatch,
    McpServerListResponse,
    McpServerResponse,
    McpTestResponse,
)
from src.api.services.mcp_service import (
    create_admin_server,
    delete_admin_server,
    list_admin_servers,
    test_admin_server,
    update_admin_server,
)
from src.api.services.agent_pool_service import get_agent_pool
from src.api.services.admin_operation_audit import (
    AdminAuditRoute,
    admin_audit_action,
    enrich_admin_audit,
)


router = APIRouter(route_class=AdminAuditRoute)
logger = logging.getLogger(__name__)


_MCP_SAFE_CHANGED_FIELDS = frozenset(
    {
        "name",
        "description",
        "url",
        "status",
        "auth_type",
        "allow_private_network",
        "allow_insecure_http",
        "required",
    }
)
_MCP_CREDENTIAL_FIELDS = frozenset(
    {"bearer_token", "headers", "clear_credential"}
)


def _safe_mcp_changed_fields(
    payload: AdminMcpServerCreate | AdminMcpServerPatch,
) -> list[str]:
    """Return field names only, collapsing every credential input to one flag."""

    provided = set(payload.model_fields_set)
    changed = provided.intersection(_MCP_SAFE_CHANGED_FIELDS)
    if provided.intersection(_MCP_CREDENTIAL_FIELDS):
        changed.add("credential")
    return sorted(changed)


async def _invalidate_all_agents() -> None:
    """Make a committed global MCP catalog change visible on this worker."""

    try:
        await get_agent_pool().invalidate_all_async()
    except Exception:
        # Other workers and failed local evictions still converge through the
        # durable global MCP config fingerprint on their next Agent lookup.
        logger.warning(
            "全局 MCP 配置已提交，但本地 Agent 缓存立即失效失败",
            exc_info=True,
        )


@router.get("/servers", response_model=McpServerListResponse)
@admin_audit_action("mcp.list")
async def get_servers(
    request: Request,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    result = list_admin_servers(db)
    enrich_admin_audit(
        request,
        details={"returned_count": len(result.get("servers", []))},
    )
    return result


@router.post(
    "/servers",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED,
)
@admin_audit_action("mcp.create", target_type="mcp_server")
async def create_server(
    request: Request,
    payload: AdminMcpServerCreate,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    changed_fields = _safe_mcp_changed_fields(payload)
    enrich_admin_audit(
        request,
        changed_fields=changed_fields,
        details={"credential_changed": "credential" in changed_fields},
    )
    result = create_admin_server(db, payload, admin_user_id)
    enrich_admin_audit(request, target_id=result["id"])
    await _invalidate_all_agents()
    return result


@router.patch("/servers/{server_id}", response_model=McpServerResponse)
@admin_audit_action(
    "mcp.update",
    target_type="mcp_server",
    target_param="server_id",
)
async def patch_server(
    request: Request,
    server_id: str,
    payload: AdminMcpServerPatch,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    changed_fields = _safe_mcp_changed_fields(payload)
    enrich_admin_audit(
        request,
        changed_fields=changed_fields,
        details={"credential_changed": "credential" in changed_fields},
    )
    result = update_admin_server(db, server_id, payload)
    await _invalidate_all_agents()
    return result


@router.delete("/servers/{server_id}")
@admin_audit_action(
    "mcp.delete",
    target_type="mcp_server",
    target_param="server_id",
)
async def delete_server(
    request: Request,
    server_id: str,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    enrich_admin_audit(request, changed_fields=["deleted"])
    result = delete_admin_server(db, server_id)
    await _invalidate_all_agents()
    return result


@router.post("/servers/{server_id}/test", response_model=McpTestResponse)
@admin_audit_action(
    "mcp.test",
    target_type="mcp_server",
    target_param="server_id",
)
async def test_server(
    request: Request,
    server_id: str,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    return await test_admin_server(db, server_id)
