"""Administrator-managed tool permission ceilings."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_admin_user
from src.api.models.database import get_db
from src.api.models.tool_permission import ToolPermissionRule
from src.api.schemas.tool_permission import ToolPermissionRuleCreate, ToolPermissionRulePatch
from src.api.services.tool_permission_service import ToolRef, create_permission_rule, rule_to_payload


router = APIRouter()


def _get_managed_rule(db: DBSession, rule_id: str) -> ToolPermissionRule:
    rule = (
        db.query(ToolPermissionRule)
        .filter(
            ToolPermissionRule.id == rule_id,
            ToolPermissionRule.scope_type == "platform",
            ToolPermissionRule.managed.is_(True),
        )
        .first()
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="平台权限规则不存在")
    return rule


@router.get("")
async def list_managed_tool_permissions(
    _admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    rows = (
        db.query(ToolPermissionRule)
        .filter(
            ToolPermissionRule.scope_type == "platform",
            ToolPermissionRule.managed.is_(True),
        )
        .order_by(ToolPermissionRule.priority.desc(), ToolPermissionRule.created_at.asc())
        .all()
    )
    return {"rules": [rule_to_payload(row) for row in rows]}


@router.post("")
async def create_managed_tool_permission(
    payload: ToolPermissionRuleCreate,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    if payload.provider == "mcp":
        from src.api.models.mcp import McpServer

        if db.query(McpServer.id).filter(McpServer.id == payload.server_id).first() is None:
            raise HTTPException(status_code=404, detail="MCP 服务器不存在")
    rule = create_permission_rule(
        db,
        scope_type="platform",
        scope_id=None,
        ref=ToolRef(
            provider=payload.provider,
            server_id=payload.server_id,
            tool_name=payload.tool_name,
        ),
        effect=payload.effect,
        priority=payload.priority,
        description=payload.description,
        expires_at=payload.expires_at,
        created_by=admin_user_id,
        managed=True,
    )
    return rule_to_payload(rule)


@router.patch("/{rule_id}")
async def patch_managed_tool_permission(
    rule_id: str,
    payload: ToolPermissionRulePatch,
    _admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    rule = _get_managed_rule(db, rule_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return rule_to_payload(rule)


@router.delete("/{rule_id}")
async def delete_managed_tool_permission(
    rule_id: str,
    _admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    rule = _get_managed_rule(db, rule_id)
    db.delete(rule)
    db.commit()
    return {"deleted": True, "id": rule_id}
