"""User-facing tool permission management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.api.models.database import get_db
from src.api.models.tool_permission import ToolPermissionRule
from src.api.schemas.tool_permission import (
    ToolPermissionRuleCreate,
    ToolPermissionRulePatch,
    ToolPermissionSelection,
    ToolPermissionSelectionBatch,
    ToolSelectionItem,
)
from src.api.services.tool_permission_service import (
    ToolPermissionCheck,
    ToolRef,
    clear_user_tool_selection,
    create_permission_rule,
    evaluate_tool_permissions,
    list_rules_for_user,
    replace_user_tool_selection,
    replace_user_tool_selections,
    rule_to_payload,
)


router = APIRouter()

_TOOL_DESCRIPTION_PREVIEW_CHARS = 500


_BUILTIN_TOOLS: tuple[tuple[str, str], ...] = (
    ("read_file", "读取工作区文件"),
    ("read_image_file", "读取工作区图片"),
    ("write_file", "写入工作区文件"),
    ("edit_file", "编辑工作区文件"),
    ("bash", "执行 Shell 命令"),
    ("bash_output", "读取后台命令输出"),
    ("bash_kill", "停止后台命令"),
    ("record_note", "记录会话笔记"),
    ("recall_notes", "检索会话笔记"),
    ("record_memory", "记录每日记忆"),
    ("update_long_term_memory", "更新长期记忆"),
    ("search_memory", "检索记忆"),
    ("read_user", "读取用户画像"),
    ("update_user", "更新用户画像"),
    ("manage_cron", "管理定时任务"),
    ("ask_user", "向用户提问"),
    ("sub_agent", "委托子 Agent"),
    ("get_skill", "加载技能"),
    ("tool_search", "搜索并加载按需工具"),
    ("glm_search", "联网搜索"),
    ("glm_batch_search", "批量联网搜索"),
)


def _description_preview(value: object) -> str:
    text = str(value or "")
    if len(text) <= _TOOL_DESCRIPTION_PREVIEW_CHARS:
        return text
    return text[: _TOOL_DESCRIPTION_PREVIEW_CHARS - 1] + "…"


def _get_user_rule_or_404(
    db: DBSession,
    *,
    rule_id: str,
    user_id: str,
) -> ToolPermissionRule:
    rule = (
        db.query(ToolPermissionRule)
        .filter(
            ToolPermissionRule.id == rule_id,
            ToolPermissionRule.scope_type == "user",
            ToolPermissionRule.scope_id == user_id,
            ToolPermissionRule.managed.is_(False),
        )
        .first()
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="权限规则不存在")
    return rule


def _assert_mcp_server_access(db: DBSession, user_id: str, server_id: str) -> None:
    from src.api.models.mcp import McpServer

    server = db.query(McpServer).filter(McpServer.id == server_id).first()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP 服务器不存在")
    if server.source == "personal" and server.owner_user_id != user_id:
        raise HTTPException(status_code=404, detail="MCP 服务器不存在")
    if server.source == "official" and server.status != "published":
        raise HTTPException(status_code=404, detail="MCP 服务器未发布")


async def _invalidate_user_agents(user_id: str) -> None:
    try:
        from src.api.services.agent_pool_service import get_agent_pool

        await get_agent_pool().invalidate_user_async(user_id)
    except Exception:
        # The DB policy version remains authoritative across workers.  Local
        # eviction only makes the UI change visible sooner.
        pass


@router.get("/rules")
async def get_permission_rules(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    return {"rules": [rule_to_payload(rule) for rule in list_rules_for_user(db, user_id)]}


@router.post("/rules")
async def create_user_permission_rule(
    payload: ToolPermissionRuleCreate,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    if payload.provider == "mcp":
        _assert_mcp_server_access(db, user_id, payload.server_id or "")
    rule = create_permission_rule(
        db,
        scope_type="user",
        scope_id=user_id,
        ref=ToolRef(
            provider=payload.provider,
            server_id=payload.server_id,
            tool_name=payload.tool_name,
        ),
        effect=payload.effect,
        priority=payload.priority,
        description=payload.description,
        expires_at=payload.expires_at,
        created_by=user_id,
    )
    await _invalidate_user_agents(user_id)
    return rule_to_payload(rule)


@router.put("/rules/selection")
async def set_user_tool_selection(
    payload: ToolPermissionSelection,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    if payload.provider == "mcp":
        _assert_mcp_server_access(db, user_id, payload.server_id or "")
    rule = replace_user_tool_selection(
        db,
        user_id=user_id,
        ref=ToolRef(
            provider=payload.provider,
            server_id=payload.server_id,
            tool_name=payload.tool_name,
        ),
        effect=payload.effect,
    )
    await _invalidate_user_agents(user_id)
    return rule_to_payload(rule)


@router.put("/rules/selection/batch")
async def set_user_tool_selection_batch(
    payload: ToolPermissionSelectionBatch,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    refs: list[ToolRef] = []
    for item in payload.items:
        if item.provider == "mcp":
            _assert_mcp_server_access(db, user_id, item.server_id or "")
        refs.append(
            ToolRef(
                provider=item.provider,
                server_id=item.server_id,
                tool_name=item.tool_name,
            )
        )
    rules = replace_user_tool_selections(
        db,
        user_id=user_id,
        refs=refs,
        effect=payload.effect,
    )
    await _invalidate_user_agents(user_id)
    return {"rules": [rule_to_payload(rule) for rule in rules]}


@router.delete("/rules/selection")
async def clear_user_tool_selection_endpoint(
    payload: ToolSelectionItem,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """Restore the provider default for one exact tool in a single transaction.

    Deletes all non-managed user rules for the exact tool identity at once so a
    "restore default" click cannot leave a partial state the way N independent
    DELETE calls could.  MCP access is not re-validated so stale rules for a
    removed server can still be cleaned up (spec §4.6).
    """

    removed = clear_user_tool_selection(
        db,
        user_id=user_id,
        ref=ToolRef(
            provider=payload.provider,
            server_id=payload.server_id,
            tool_name=payload.tool_name,
        ),
    )
    await _invalidate_user_agents(user_id)
    return {
        "deleted": removed,
        "provider": payload.provider,
        "server_id": payload.server_id,
        "tool_name": payload.tool_name,
    }


@router.patch("/rules/{rule_id}")
async def patch_user_permission_rule(
    rule_id: str,
    payload: ToolPermissionRulePatch,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    rule = _get_user_rule_or_404(db, rule_id=rule_id, user_id=user_id)
    changes = payload.model_dump(exclude_unset=True)
    # Approval-derived persistent ALLOW rules are bound to the approved MCP
    # schema/connection.  Restrictive effects must be unconditional; retaining
    # the old binding would make ASK/DENY silently disappear on version drift.
    if changes.get("effect") in {"ask", "deny"}:
        rule.conditions_json = None
    for key, value in changes.items():
        setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    await _invalidate_user_agents(user_id)
    return rule_to_payload(rule)


@router.delete("/rules/{rule_id}")
async def delete_user_permission_rule(
    rule_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    rule = _get_user_rule_or_404(db, rule_id=rule_id, user_id=user_id)
    db.delete(rule)
    db.commit()
    await _invalidate_user_agents(user_id)
    return {"deleted": True, "id": rule_id}


@router.get("/tools")
async def get_permission_tools(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    candidates: list[tuple[dict, ToolPermissionCheck]] = []
    for name, description in _BUILTIN_TOOLS:
        ref = ToolRef(provider="builtin", tool_name=name)
        candidates.append(
            (
                {
                    "tool_ref": ref.canonical,
                    "provider": "builtin",
                    "server_id": None,
                    "server_name": None,
                    "source_type": "builtin",
                    "tool_name": name,
                    "title": name,
                    "description": _description_preview(description),
                },
                ToolPermissionCheck(ref=ref),
            )
        )

    # Tool snapshots are credential/installation scoped.  Joining through the
    # current user's installation prevents another user's discovery result from
    # leaking into this inventory.
    try:
        from src.api.models.mcp import McpInstallation, McpServer, McpToolSnapshot

        from src.api.services.mcp_runtime import resolve_effective_mcp_installation

        rows = (
            db.query(McpToolSnapshot, McpServer, McpInstallation)
            .join(
                McpInstallation,
                McpInstallation.id == McpToolSnapshot.installation_id,
            )
            .join(McpServer, McpServer.id == McpInstallation.server_id)
            .filter(
                McpInstallation.user_id == user_id,
                or_(
                    McpServer.source == "personal",
                    (McpServer.source == "official")
                    & (McpServer.status == "published"),
                ),
            )
            .order_by(McpServer.name.asc(), McpToolSnapshot.tool_name.asc())
            .all()
        )
        installation_fingerprints: dict[str, str | None] = {}
        for snapshot, server, installation in rows:
            ref = ToolRef(
                provider="mcp",
                server_id=server.id,
                tool_name=snapshot.tool_name,
            )
            default_effect = "allow"
            if installation.id not in installation_fingerprints:
                effective = resolve_effective_mcp_installation(
                    db,
                    user_id=user_id,
                    installation_id=installation.id,
                )
                installation_fingerprints[installation.id] = (
                    effective.execution_fingerprint if effective is not None else None
                )
            candidates.append(
                (
                    {
                        "tool_ref": ref.canonical,
                        "provider": "mcp",
                        "server_id": server.id,
                        "server_name": server.name,
                        "source_type": server.source,
                        "tool_name": snapshot.tool_name,
                        "title": getattr(snapshot, "title", None)
                        or snapshot.tool_name,
                        "description": _description_preview(snapshot.description),
                        "schema_hash": snapshot.schema_hash,
                    },
                    ToolPermissionCheck(
                        ref=ref,
                        default_effect=default_effect,
                        schema_hash=snapshot.schema_hash,
                        connection_fingerprint=installation_fingerprints[
                            installation.id
                        ],
                    ),
                )
            )
    except (ImportError, AttributeError):
        # During rolling schema upgrades the built-in inventory remains useful.
        pass

    decisions = evaluate_tool_permissions(
        db,
        user_id=user_id,
        session_id=None,
        checks=[check for _payload, check in candidates],
    )
    tools: list[dict] = []
    for (payload, _check), decision in zip(candidates, decisions):
        payload["effect"] = decision.effect
        payload["matched_rule_id"] = decision.matched_rule_id
        tools.append(payload)
    return {"tools": tools}
