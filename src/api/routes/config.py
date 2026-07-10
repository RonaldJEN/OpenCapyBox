"""配置管理 API

提供 Agent 配置文件编辑和 Skill 管理：
- GET/PUT /api/config/agent-files/{name}: 读写 USER/SOUL/MEMORY 文件
- GET /api/config/skills: 获取用户 Skill 配置列表
- PUT /api/config/skills/{skill_name}: 启用/禁用 Skill
"""

import asyncio
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession

from src.api.models.database import get_db
from src.api.deps import get_current_user
from src.api.services.memory_service import MemoryService
from src.api.models.user_memory import UserSkillConfig

logger = logging.getLogger(__name__)
router = APIRouter()

# agent file name → file_type 映射
_NAME_TO_FILE_TYPE = {
    "user": "user_md",
    "soul": "soul_md",
    "memory": "memory_md",
}

_SKILL_CATEGORY_MAP = {
    "document-skills": "document",
    "financial-skills": "financial",
    "example_skills": "example",
    "example-skills": "example",
}


def _get_skills_dir() -> Path:
    from src.api.config import get_settings

    setting_value = get_settings().skills_dir
    if setting_value:
        return Path(setting_value).resolve()
    return (Path(__file__).parent.parent.parent / "agent" / "skills").resolve()


class AgentFileUpdateRequest(BaseModel):
    content: str


class SkillToggleRequest(BaseModel):
    enabled: bool


@router.get("/agent-files/{name}")
async def get_agent_file(
    name: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """读取 Agent 配置文件（新用户自动注入默认模板）"""
    file_type = _NAME_TO_FILE_TYPE.get(name)
    if not file_type:
        raise HTTPException(
            status_code=400,
            detail=f"无效的文件名 '{name}'，可选: {list(_NAME_TO_FILE_TYPE.keys())}",
        )

    svc = MemoryService(db)

    # 新用户自动注入默认模板
    try:
        svc.provision_default_files(user_id)
    except Exception:
        pass

    record = svc.get_memory_file(user_id, file_type)
    return {
        "name": name,
        "file_type": file_type,
        "content": record.content if record else "",
        "version": record.version if record else 0,
    }


@router.put("/agent-files/{name}")
async def update_agent_file(
    name: str,
    request: AgentFileUpdateRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """更新 Agent 配置文件"""
    file_type = _NAME_TO_FILE_TYPE.get(name)
    if not file_type:
        raise HTTPException(
            status_code=400,
            detail=f"无效的文件名 '{name}'，可选: {list(_NAME_TO_FILE_TYPE.keys())}",
        )

    svc = MemoryService(db)
    try:
        record = svc.upsert_memory_file(user_id, file_type, request.content)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # 配置更新后使该用户缓存中的 Agent 失效，确保后续请求读取最新 system prompt
    try:
        from src.api.services.agent_pool_service import get_agent_pool
        removed = await get_agent_pool().invalidate_user_async(user_id)
        if removed > 0:
            logger.info("Agent 配置更新后已失效缓存: user=%s, removed=%d", user_id, removed)
    except Exception as e:
        logger.warning("失效 Agent 缓存失败（非致命）: %s", e)

    # 同步到沙箱（如果有活跃沙箱）
    try:
        from src.api.services.sandbox_service import get_sandbox_service
        from src.api.models.user_sandbox import UserSandbox

        sandbox_service = get_sandbox_service()
        sandbox = sandbox_service.get_cached(user_id)
        if sandbox:
            user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
            persisted_sandbox_id = user_sandbox.sandbox_id if user_sandbox else None
            if not isinstance(persisted_sandbox_id, str) or not persisted_sandbox_id:
                persisted_sandbox_id = None
            cached_sandbox_id = getattr(sandbox, "id", None)
            cached_current = not persisted_sandbox_id or cached_sandbox_id == persisted_sandbox_id
            cached_is_current = getattr(sandbox_service, "cached_is_current", None)
            if callable(cached_is_current):
                current_result = cached_is_current(user_id, persisted_sandbox_id)
                if isinstance(current_result, bool):
                    cached_current = current_result
            if not cached_current:
                sandbox = await sandbox_service.get_or_resume(user_id, persisted_sandbox_id)
            await svc.sync_to_sandbox(
                user_id,
                sandbox,
                force=True,
                file_types={file_type},
                include_agents_template=False,
            )
    except Exception as e:
        logger.warning("同步配置到沙箱失败: %s", e)

    return {
        "name": name,
        "file_type": file_type,
        "version": record.version,
        "message": "ok",
    }


@router.get("/skills")
async def get_skills(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """获取用户的 Skill 配置列表"""
    from src.agent.tools.skill_loader import SkillLoader, Skill
    from src.api.models.user_sandbox import UserSandbox
    from src.api.services.sandbox_service import get_sandbox_service

    skills_dir = _get_skills_dir()

    available_skills: list[dict] = []
    if skills_dir.exists():
        try:
            loader = SkillLoader(str(skills_dir))
            discovered: list[Skill] = loader.discover_skills()
            for skill in discovered:
                category = "general"
                if isinstance(skill.metadata, dict):
                    category = str(skill.metadata.get("category") or category)

                if category == "general" and skill.skill_path is not None:
                    try:
                        rel_parent = skill.skill_path.parent.relative_to(skills_dir)
                        if rel_parent.parts:
                            category = _SKILL_CATEGORY_MAP.get(rel_parent.parts[0], category)
                    except Exception:
                        pass

                available_skills.append({
                    "name": skill.name,
                    "description": skill.description,
                    "category": category,
                    "source": "official",
                })
        except Exception as e:
            logger.warning("Skills 发现失败: %s", e)

    official_names = {skill["name"] for skill in available_skills}
    sandbox_status = "not_created"
    sandbox_id: str | None = None

    # 只在本地 DB 查询期间占用连接；远程沙箱 I/O 前主动结束事务，
    # 避免并发打开设置页时把连接池耗尽。
    try:
        user_sandbox = (
            db.query(UserSandbox)
            .filter(UserSandbox.user_id == user_id)
            .first()
        )
        persisted_id = getattr(user_sandbox, "sandbox_id", None)
        if isinstance(persisted_id, str) and persisted_id:
            sandbox_id = persisted_id
    finally:
        db.rollback()

    try:
        sandbox_service = get_sandbox_service()
        cached_sandbox = sandbox_service.get_cached(user_id)
        cached_id = getattr(cached_sandbox, "id", None)
        candidate_id = sandbox_id or (
            cached_id if isinstance(cached_id, str) and cached_id else None
        )

        if candidate_id:
            sandbox_status = "unavailable"
            await asyncio.wait_for(
                sandbox_service.get_existing(user_id, candidate_id),
                timeout=10,
            )
            sandbox_skills = await asyncio.wait_for(
                sandbox_service.discover_sandbox_skills(
                    user_id,
                    official_names,
                    strict=True,
                ),
                timeout=12,
            )
            sandbox_status = "available"
            for skill in sandbox_skills:
                available_skills.append({
                    "name": skill["name"],
                    "description": skill["description"],
                    "category": "user",
                    "source": "user",
                })
        elif cached_sandbox is not None:
            sandbox_status = "unavailable"
    except Exception as e:
        sandbox_status = "unavailable"
        logger.warning("读取用户沙箱 Skills 失败（仍返回官方 Skills）: %s", e)

    # 沙箱 I/O 完成后再读取最新配置，避免长请求用旧快照覆盖刚完成的 toggle。
    configs = (
        db.query(UserSkillConfig)
        .filter(UserSkillConfig.user_id == user_id)
        .all()
    )
    user_config = {c.skill_name: c.enabled for c in configs}

    # 合并
    result = []
    for skill in available_skills:
        skill_name = skill["name"]
        result.append({
            **skill,
            "enabled": user_config.get(skill_name, True),  # 默认启用
        })

    return {"skills": result, "sandbox_status": sandbox_status}


@router.put("/skills/{skill_name}")
async def toggle_skill(
    skill_name: str,
    request: SkillToggleRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """启用/禁用指定 Skill"""
    config = (
        db.query(UserSkillConfig)
        .filter(
            UserSkillConfig.user_id == user_id,
            UserSkillConfig.skill_name == skill_name,
        )
        .first()
    )

    if config:
        config.enabled = request.enabled
    else:
        config = UserSkillConfig(
            user_id=user_id,
            skill_name=skill_name,
            enabled=request.enabled,
        )
        db.add(config)

    db.commit()
    return {
        "skill_name": skill_name,
        "enabled": request.enabled,
        "message": "ok",
    }
