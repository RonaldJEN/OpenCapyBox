"""Authenticated user's official connections and personal MCP catalog."""

import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.api.models.database import get_db
from src.api.schemas.mcp import (
    McpActivationRequest,
    McpConnectionUpdate,
    McpImportRequest,
    McpImportResponse,
    McpServerListResponse,
    McpServerResponse,
    McpTestResponse,
    McpToolListResponse,
    McpToolVisibilityUpdate,
    UserMcpServerCreate,
    UserMcpServerPatch,
)
from src.api.services.mcp_service import (
    activate_user_server,
    create_personal_server,
    delete_personal_server,
    export_personal_servers,
    import_personal_servers,
    list_server_tools,
    list_user_servers,
    test_user_server,
    update_connection,
    update_personal_server,
    update_tool_visibility,
)
from src.api.services.agent_pool_service import get_agent_pool


router = APIRouter()
logger = logging.getLogger(__name__)


async def _invalidate_user_agents(user_id: str) -> None:
    """Make committed MCP catalog changes visible to existing conversations."""

    try:
        await get_agent_pool().invalidate_user_async(user_id)
    except Exception:
        # The durable MCP config fingerprint remains authoritative across
        # workers. Local eviction is only the immediate same-process fast path.
        logger.warning(
            "MCP 配置已提交，但本地 Agent 缓存立即失效失败: user=%s",
            user_id,
            exc_info=True,
        )


@router.get("/servers", response_model=McpServerListResponse)
async def get_servers(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return list_user_servers(db, user_id)


@router.post(
    "/servers",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_server(
    payload: UserMcpServerCreate,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    result = create_personal_server(db, user_id, payload)
    await _invalidate_user_agents(user_id)
    return result


@router.patch("/servers/{server_id}", response_model=McpServerResponse)
async def patch_server(
    server_id: str,
    payload: UserMcpServerPatch,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    result = update_personal_server(db, user_id, server_id, payload)
    await _invalidate_user_agents(user_id)
    return result


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    result = delete_personal_server(db, user_id, server_id)
    await _invalidate_user_agents(user_id)
    return result


@router.put("/servers/{server_id}/connection", response_model=McpServerResponse)
async def put_connection(
    server_id: str,
    payload: McpConnectionUpdate,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    result = update_connection(db, user_id, server_id, payload)
    await _invalidate_user_agents(user_id)
    return result


@router.post("/servers/{server_id}/test", response_model=McpTestResponse)
async def test_server(
    server_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    result = await test_user_server(db, user_id, server_id)
    await _invalidate_user_agents(user_id)
    return result


@router.post("/servers/{server_id}/activate", response_model=McpServerResponse)
async def activate_server(
    server_id: str,
    payload: McpActivationRequest | None = None,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    result = await activate_user_server(db, user_id, server_id, payload)
    await _invalidate_user_agents(user_id)
    return result


@router.get("/servers/{server_id}/tools", response_model=McpToolListResponse)
async def get_server_tools(
    server_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return list_server_tools(db, user_id, server_id)


@router.put(
    "/servers/{server_id}/tools/visibility",
    response_model=McpToolListResponse,
)
async def put_tool_visibility(
    server_id: str,
    payload: McpToolVisibilityUpdate,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    result = update_tool_visibility(db, user_id, server_id, payload)
    await _invalidate_user_agents(user_id)
    return result


@router.post("/import", response_model=McpImportResponse)
async def import_servers(
    payload: McpImportRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    result = await import_personal_servers(db, user_id, payload)
    if result.get("imported", 0):
        await _invalidate_user_agents(user_id)
    return result


@router.get("/export")
async def export_servers(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return export_personal_servers(db, user_id)
