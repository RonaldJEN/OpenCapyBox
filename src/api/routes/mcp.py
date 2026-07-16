"""Authenticated user's official connections and personal MCP catalog."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.api.models.database import get_db
from src.api.schemas.mcp import (
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


router = APIRouter()


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
    return create_personal_server(db, user_id, payload)


@router.patch("/servers/{server_id}", response_model=McpServerResponse)
async def patch_server(
    server_id: str,
    payload: UserMcpServerPatch,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return update_personal_server(db, user_id, server_id, payload)


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return delete_personal_server(db, user_id, server_id)


@router.put("/servers/{server_id}/connection", response_model=McpServerResponse)
async def put_connection(
    server_id: str,
    payload: McpConnectionUpdate,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return update_connection(db, user_id, server_id, payload)


@router.post("/servers/{server_id}/test", response_model=McpTestResponse)
async def test_server(
    server_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return await test_user_server(db, user_id, server_id)


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
    return update_tool_visibility(db, user_id, server_id, payload)


@router.post("/import", response_model=McpImportResponse)
async def import_servers(
    payload: McpImportRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return import_personal_servers(db, user_id, payload)


@router.get("/export")
async def export_servers(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return export_personal_servers(db, user_id)
