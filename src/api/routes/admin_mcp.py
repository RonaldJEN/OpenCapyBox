"""Administrator-managed official MCP catalog routes."""

import logging

from fastapi import APIRouter, Depends, status
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


router = APIRouter()
logger = logging.getLogger(__name__)


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
async def get_servers(
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    return list_admin_servers(db)


@router.post(
    "/servers",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_server(
    payload: AdminMcpServerCreate,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    result = create_admin_server(db, payload, admin_user_id)
    await _invalidate_all_agents()
    return result


@router.patch("/servers/{server_id}", response_model=McpServerResponse)
async def patch_server(
    server_id: str,
    payload: AdminMcpServerPatch,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    result = update_admin_server(db, server_id, payload)
    await _invalidate_all_agents()
    return result


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: str,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    result = delete_admin_server(db, server_id)
    await _invalidate_all_agents()
    return result


@router.post("/servers/{server_id}/test", response_model=McpTestResponse)
async def test_server(
    server_id: str,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    return await test_admin_server(db, server_id)
