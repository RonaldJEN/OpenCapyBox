"""配置管理 API

提供 Agent 配置文件编辑和 Skill 管理：
- GET/PUT /api/config/agent-files/{name}: 读写 USER/SOUL/AGENTS/MEMORY/HEARTBEAT 文件
- GET /api/config/skills: 获取用户 Skill 配置列表
- PUT /api/config/skills/{skill_name}: 启用/禁用 Skill
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession

from src.api.models.database import get_db
from src.api.deps import get_current_user
from src.api.services.memory_service import MemoryService, FILE_TYPE_TO_FILENAME
from src.api.models.user_memory import UserSkillConfig

logger = logging.getLogger(__name__)
router = APIRouter()

# agent file name → file_type 映射
_NAME_TO_FILE_TYPE = {
    "user": "user_md",
    "soul": "soul_md",
    "agents": "agents_md",
    "memory": "memory_md",
    "heartbeat": "heartbeat_md",
}

_SKILL_CATEGORY_MAP = {
    "document-skills": "document",
    "financial-skills": "financial",
    "example_skills": "example",
    "example-skills": "example",
}


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
        removed = get_agent_pool().invalidate_user(user_id)
        if removed > 0:
            logger.info("Agent 配置更新后已失效缓存: user=%s, removed=%d", user_id, removed)
    except Exception as e:
        logger.warning("失效 Agent 缓存失败（非致命）: %s", e)

    # 同步到沙箱（如果有活跃沙箱）
    try:
        from src.api.services.sandbox_service import get_sandbox_service
        sandbox_service = get_sandbox_service()
        sandbox = sandbox_service.get_cached(user_id)
        if sandbox:
            await svc.sync_to_sandbox(user_id, sandbox)
    except Exception as e:
        logger.warning("同步配置到沙箱失败: %s", e)

    # HEARTBEAT.md 更新后重新注册该用户的 Cron 任务
    if name == "heartbeat":
        try:
            from src.api.services.cron_service import reload_user_jobs
            import src.api.main as _main_mod

            scheduler = getattr(getattr(_main_mod.app, "state", None), "scheduler", None)
            if scheduler:
                count = reload_user_jobs(user_id, scheduler)
                logger.info("已重新注册用户 %s 的 %d 个 Cron 任务", user_id, count)
        except Exception as e:
            logger.warning("重新注册 Cron 任务失败: %s", e)

    return {
        "name": name,
        "file_type": file_type,
        "version": record.version,
        "message": "ok",
    }


@router.get("/agent-files")
async def list_agent_files(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """列出所有 Agent 配置文件的元数据

    新用户首次访问时自动写入默认模板（Bootstrap）。
    """
    svc = MemoryService(db)

    # 新用户自动注入默认模板
    try:
        count = svc.provision_default_files(user_id)
        if count > 0:
            logger.info("新用户默认文件注入完成: user=%s, count=%d", user_id, count)
    except Exception as e:
        logger.warning("默认文件注入失败（非致命）: %s", e)

    all_files = svc.get_all_memory_files(user_id)

    files = []
    for name, file_type in _NAME_TO_FILE_TYPE.items():
        content = all_files.get(file_type, "")
        record = svc.get_memory_file(user_id, file_type)
        files.append({
            "name": name,
            "file_type": file_type,
            "filename": FILE_TYPE_TO_FILENAME.get(file_type, ""),
            "has_content": bool(content),
            "version": record.version if record else 0,
            "updated_at": record.updated_at.isoformat() if record and record.updated_at else None,
        })

    return {"files": files}


@router.get("/skills")
async def get_skills(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """获取用户的 Skill 配置列表"""
    # 从 SkillLoader 获取所有可用 skills
    from pathlib import Path
    from src.agent.tools.skill_loader import SkillLoader, Skill
    from src.api.config import get_settings

    settings = get_settings()
    skills_dir_setting = settings.skills_dir
    if skills_dir_setting:
        skills_dir = Path(skills_dir_setting).resolve()
    else:
        skills_dir = (Path(__file__).parent.parent.parent / "agent" / "skills").resolve()

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
                })
        except Exception as e:
            logger.warning("Skills 发现失败: %s", e)

    # 获取用户配置
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

    return {"skills": result}


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
