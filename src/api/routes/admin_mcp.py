"""Administrator-managed official MCP catalog routes."""

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


router = APIRouter()


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
    return create_admin_server(db, payload, admin_user_id)


@router.patch("/servers/{server_id}", response_model=McpServerResponse)
async def patch_server(
    server_id: str,
    payload: AdminMcpServerPatch,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    return update_admin_server(db, server_id, payload)


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: str,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    return delete_admin_server(db, server_id)


@router.post("/servers/{server_id}/test", response_model=McpTestResponse)
async def test_server(
    server_id: str,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    return await test_admin_server(db, server_id)
