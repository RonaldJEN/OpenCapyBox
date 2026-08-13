"""Model catalog and permission package service."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.api.model_registry import ModelConfig, ModelRegistry, get_model_registry, reload_model_registry
from src.api.models.auth_user import AuthUser
from src.api.models.llm_model import LLMModel, LLMModelSettings
from src.api.models.model_permission import (
    ModelPermissionGroup,
    ModelPermissionGroupModel,
    UserModelPermissionGroup,
)
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)

DEFAULT_MODEL_GROUP_NAME = "默认"
DEFAULT_MODEL_GROUP_DESCRIPTION = "所有普通用户自动拥有的基础模型范围"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return [str(item) for item in value]
    except json.JSONDecodeError:
        pass
    return []


def _find_yaml() -> Path:
    env_path = __import__("os").environ.get("MODEL_REGISTRY_PATH")
    if env_path:
        return Path(env_path)
    candidates = [
        Path.cwd() / "models.yaml",
        Path(__file__).parent.parent.parent.parent / "models.yaml",
    ]
    for candidate in candidates:
        if candidate.resolve().exists():
            return candidate.resolve()
    raise FileNotFoundError("找不到 models.yaml，无法初始化模型目录")


def get_or_create_default_group(db: DBSession, *, created_by: str = "system") -> ModelPermissionGroup:
    group = (
        db.query(ModelPermissionGroup)
        .filter(ModelPermissionGroup.is_default.is_(True))
        .order_by(ModelPermissionGroup.created_at.asc(), ModelPermissionGroup.id.asc())
        .first()
    )
    if group:
        return group

    group = db.query(ModelPermissionGroup).filter(ModelPermissionGroup.name == DEFAULT_MODEL_GROUP_NAME).first()
    if group:
        group.is_default = True
        group.updated_at = now_naive()
        db.commit()
        db.refresh(group)
        return group

    group = ModelPermissionGroup(
        name=DEFAULT_MODEL_GROUP_NAME,
        description=DEFAULT_MODEL_GROUP_DESCRIPTION,
        is_default=True,
        created_by=created_by,
    )
    db.add(group)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        group = db.query(ModelPermissionGroup).filter(ModelPermissionGroup.name == DEFAULT_MODEL_GROUP_NAME).first()
        if not group:
            raise
        group.is_default = True
        group.updated_at = now_naive()
        db.commit()
    db.refresh(group)
    return group


def seed_model_catalog_from_yaml_if_empty(db: DBSession, yaml_path: str | Path | None = None) -> int:
    """Import models.yaml into DB only when the catalog is empty."""
    existing = int(db.query(func.count(LLMModel.model_id)).scalar() or 0)
    default_group = get_or_create_default_group(db)
    if existing:
        return 0

    path = Path(yaml_path) if yaml_path else _find_yaml()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    models_raw = raw.get("models") or {}
    if not isinstance(models_raw, dict) or not models_raw:
        raise ValueError(f"models.yaml 格式错误：缺少 models ({path})")

    seeded = 0
    for model_id, cfg in models_raw.items():
        model = LLMModel(
            model_id=str(model_id),
            display_name=cfg.get("display_name", str(model_id)),
            provider=cfg["provider"],
            api_base=cfg["api_base"],
            api_key=cfg.get("api_key", "${LLM_API_KEY}"),
            model_name=cfg.get("model_name", str(model_id)),
            max_tokens=int(cfg.get("max_tokens", 16384)),
            context_window=int(cfg.get("context_window", 128000)),
            auto_compact_token_limit=cfg.get("auto_compact_token_limit"),
            tool_output_truncation_bytes=int(cfg.get("tool_output_truncation_bytes", 10000)),
            reasoning_format=cfg.get("reasoning_format", "none"),
            reasoning_split=bool(cfg.get("reasoning_split", False)),
            enable_thinking=bool(cfg.get("enable_thinking", False)),
            supports_image=bool(cfg.get("supports_image", False)),
            max_images=int(cfg.get("max_images", 0)),
            supports_video=bool(cfg.get("supports_video", False)),
            max_videos=int(cfg.get("max_videos", 0)),
            enabled=bool(cfg.get("enabled", True)),
            tags_json=_json_dumps(cfg.get("tags", [])),
        )
        db.add(model)
        seeded += 1
        if model.enabled:
            db.add(ModelPermissionGroupModel(
                group_id=default_group.id,
                model_id=model.model_id,
                created_by="seed",
            ))

    settings = LLMModelSettings(
        id=1,
        default_model_id=raw.get("default_model") or None,
        cron_default_model_id=raw.get("cron_default_model") or raw.get("default_model") or None,
        subagent_default_model_id=raw.get("subagent_default_model") or raw.get("default_model") or None,
    )
    db.merge(settings)
    db.commit()
    return seeded


def db_model_to_config(model: LLMModel) -> ModelConfig:
    return ModelConfig(
        id=model.model_id,
        display_name=model.display_name,
        provider=model.provider,
        api_base=model.api_base,
        api_key=model.api_key,
        model_name=model.model_name,
        max_tokens=int(model.max_tokens or 16384),
        context_window=int(model.context_window or 128000),
        auto_compact_token_limit=(
            int(model.auto_compact_token_limit)
            if model.auto_compact_token_limit is not None
            else None
        ),
        tool_output_truncation_bytes=int(
            10000
            if model.tool_output_truncation_bytes is None
            else model.tool_output_truncation_bytes
        ),
        reasoning_format=model.reasoning_format or "none",
        reasoning_split=bool(model.reasoning_split),
        enable_thinking=bool(model.enable_thinking),
        supports_image=bool(model.supports_image),
        max_images=int(model.max_images or 0),
        supports_video=bool(model.supports_video),
        max_videos=int(model.max_videos or 0),
        enabled=bool(model.enabled),
        tags=_json_loads_list(model.tags_json),
    )


def load_registry_from_db(db: DBSession) -> ModelRegistry:
    models = {
        row.model_id: db_model_to_config(row)
        for row in db.query(LLMModel).order_by(LLMModel.created_at.asc(), LLMModel.model_id.asc()).all()
    }
    settings = db.query(LLMModelSettings).filter(LLMModelSettings.id == 1).first()
    default_model_id = settings.default_model_id if settings else ""
    cron_default_model_id = settings.cron_default_model_id if settings else default_model_id
    subagent_default_model_id = settings.subagent_default_model_id if settings else default_model_id
    embedding_models = {}
    default_embedding_model_id = ""
    try:
        yaml_registry = ModelRegistry.load_yaml()
        embedding_models = getattr(yaml_registry, "_embedding_models", {})
        default_embedding_model_id = getattr(yaml_registry, "_default_embedding_model_id", "")
    except Exception as e:
        logger.warning("加载 YAML embedding 模型配置失败，记忆向量检索可能降级: %s", e)

    registry = ModelRegistry(
        models=models,
        default_model_id=default_model_id or "",
        embedding_models=embedding_models,
        default_embedding_model_id=default_embedding_model_id,
        cron_default_model_id=cron_default_model_id or "",
        subagent_default_model_id=subagent_default_model_id or "",
        source="db",
    )
    registry.validate_on_startup()
    return registry


def is_admin_user(db: DBSession, user_id: str) -> bool:
    user = db.query(AuthUser).filter(AuthUser.user_id == user_id, AuthUser.enabled.is_(True)).first()
    return bool(user and user.is_admin)


def _user_group_ids(db: DBSession, user_id: str) -> set[str]:
    default_group = get_or_create_default_group(db)
    ids = {default_group.id}
    rows = (
        db.query(UserModelPermissionGroup.group_id)
        .filter(UserModelPermissionGroup.user_id == user_id)
        .all()
    )
    ids.update(row[0] for row in rows if row[0])
    return ids


def list_accessible_model_ids(db: DBSession, user_id: str) -> set[str]:
    if is_admin_user(db, user_id):
        return {row[0] for row in db.query(LLMModel.model_id).filter(LLMModel.enabled.is_(True)).all()}
    group_ids = _user_group_ids(db, user_id)
    if not group_ids:
        return set()
    return {
        row[0]
        for row in (
            db.query(ModelPermissionGroupModel.model_id)
            .filter(ModelPermissionGroupModel.group_id.in_(group_ids))
            .all()
        )
        if row[0]
    }


def list_accessible_model_configs(
    db: DBSession,
    user_id: str,
    registry: ModelRegistry | None = None,
) -> list[ModelConfig]:
    registry = registry or get_model_registry()
    models = registry.list_models(enabled_only=True)
    if is_admin_user(db, user_id):
        return models
    accessible_ids = list_accessible_model_ids(db, user_id)
    return [model for model in models if model.id in accessible_ids]


def assert_user_can_access_model(
    db: DBSession,
    user_id: str,
    model_id: str,
    registry: ModelRegistry | None = None,
) -> ModelConfig:
    registry = registry or get_model_registry()
    try:
        config = registry.get_or_raise(model_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if is_admin_user(db, user_id):
        return config
    if config.id not in list_accessible_model_ids(db, user_id):
        raise HTTPException(status_code=403, detail=f"当前用户无权使用模型 '{model_id}'")
    return config


def resolve_default_model_for_user(
    db: DBSession,
    user_id: str,
    *,
    kind: str = "chat",
    registry: ModelRegistry | None = None,
) -> ModelConfig:
    registry = registry or get_model_registry()
    if kind == "cron":
        preferred_id = registry.get_cron_default().id
    elif kind == "subagent":
        preferred_id = registry.get_subagent_default().id
    else:
        preferred_id = registry.get_default().id

    accessible = list_accessible_model_configs(db, user_id, registry)
    if not accessible:
        raise HTTPException(status_code=403, detail="当前用户没有可用模型，请联系管理员配置模型权限")
    preferred = next((model for model in accessible if model.id == preferred_id), None)
    return preferred or accessible[0]


def group_to_payload(db: DBSession, group: ModelPermissionGroup) -> dict[str, Any]:
    model_ids = [
        row[0]
        for row in (
            db.query(ModelPermissionGroupModel.model_id)
            .filter(ModelPermissionGroupModel.group_id == group.id)
            .order_by(ModelPermissionGroupModel.model_id.asc())
            .all()
        )
    ]
    if group.is_default:
        bound_users = int(db.query(func.count(AuthUser.id)).scalar() or 0)
    else:
        bound_users = int(
            db.query(func.count(UserModelPermissionGroup.id))
            .filter(UserModelPermissionGroup.group_id == group.id)
            .scalar()
            or 0
        )
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "is_default": bool(group.is_default),
        "model_ids": model_ids,
        "model_count": len(model_ids),
        "bound_users": bound_users,
        "created_by": group.created_by,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "updated_at": group.updated_at.isoformat() if group.updated_at else None,
    }


def list_permission_groups_payload(db: DBSession) -> dict[str, Any]:
    get_or_create_default_group(db)
    groups = (
        db.query(ModelPermissionGroup)
        .order_by(ModelPermissionGroup.is_default.desc(), ModelPermissionGroup.created_at.asc(), ModelPermissionGroup.name.asc())
        .all()
    )
    return {"groups": [group_to_payload(db, group) for group in groups]}


def set_group_models(db: DBSession, *, group_id: str, model_ids: list[str], admin_user_id: str) -> dict[str, Any]:
    group = db.query(ModelPermissionGroup).filter(ModelPermissionGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="模型权限包不存在")
    normalized = list(dict.fromkeys(mid.strip() for mid in model_ids if mid and mid.strip()))
    existing_models = {
        row.model_id: bool(row.enabled)
        for row in (
            db.query(LLMModel.model_id, LLMModel.enabled)
            .filter(LLMModel.model_id.in_(normalized))
            .all()
        )
    } if normalized else {}
    missing = [mid for mid in normalized if mid not in existing_models]
    if missing:
        raise HTTPException(status_code=400, detail=f"模型不存在: {missing}")
    disabled = [mid for mid in normalized if not existing_models[mid]]
    if disabled:
        raise HTTPException(status_code=400, detail=f"停用模型不能加入权限包: {disabled}")

    db.query(ModelPermissionGroupModel).filter(ModelPermissionGroupModel.group_id == group.id).delete(synchronize_session=False)
    for model_id in normalized:
        db.add(ModelPermissionGroupModel(group_id=group.id, model_id=model_id, created_by=admin_user_id))
    group.updated_at = now_naive()
    db.commit()
    db.refresh(group)
    reload_model_registry()
    return group_to_payload(db, group)


def set_user_groups(db: DBSession, *, user_id: str, group_ids: list[str], admin_user_id: str) -> dict[str, Any]:
    user = db.query(AuthUser).filter(AuthUser.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    normalized = list(dict.fromkeys(gid.strip() for gid in group_ids if gid and gid.strip()))
    groups = db.query(ModelPermissionGroup).filter(ModelPermissionGroup.id.in_(normalized)).all() if normalized else []
    by_id = {group.id: group for group in groups}
    missing = [gid for gid in normalized if gid not in by_id]
    if missing:
        raise HTTPException(status_code=400, detail=f"模型权限包不存在: {missing}")
    default_ids = [gid for gid, group in by_id.items() if group.is_default]
    if default_ids:
        raise HTTPException(status_code=400, detail="默认权限包自动应用给所有用户，不能手动绑定")

    db.query(UserModelPermissionGroup).filter(UserModelPermissionGroup.user_id == user_id).delete(synchronize_session=False)
    for group_id in normalized:
        db.add(UserModelPermissionGroup(user_id=user_id, group_id=group_id, created_by=admin_user_id))
    db.commit()
    return user_model_groups_payload(db, user_id)


def user_model_groups_payload(db: DBSession, user_id: str) -> dict[str, Any]:
    default_group = get_or_create_default_group(db)
    rows = (
        db.query(ModelPermissionGroup)
        .join(UserModelPermissionGroup, UserModelPermissionGroup.group_id == ModelPermissionGroup.id)
        .filter(UserModelPermissionGroup.user_id == user_id)
        .order_by(ModelPermissionGroup.name.asc())
        .all()
    )
    extra = [group_to_payload(db, group) for group in rows if not group.is_default]
    return {
        "user_id": user_id,
        "default_group": group_to_payload(db, default_group),
        "extra_groups": extra,
        "group_ids": [group["id"] for group in extra],
        "group_names": [group["name"] for group in extra],
    }


def model_group_names_by_model(db: DBSession) -> dict[str, list[str]]:
    rows = (
        db.query(ModelPermissionGroupModel.model_id, ModelPermissionGroup.name)
        .join(ModelPermissionGroup, ModelPermissionGroup.id == ModelPermissionGroupModel.group_id)
        .order_by(ModelPermissionGroup.is_default.desc(), ModelPermissionGroup.name.asc())
        .all()
    )
    result: dict[str, list[str]] = {}
    for model_id, group_name in rows:
        result.setdefault(model_id, []).append(group_name)
    return result


def admin_model_payload(db: DBSession, model: LLMModel) -> dict[str, Any]:
    group_names = model_group_names_by_model(db).get(model.model_id, [])
    return {
        "id": model.model_id,
        "name": model.display_name,
        "provider": model.provider,
        "api_base": model.api_base,
        "model_name": model.model_name,
        "max_tokens": model.max_tokens,
        "context_window": model.context_window,
        "auto_compact_token_limit": model.auto_compact_token_limit,
        "tool_output_truncation_bytes": model.tool_output_truncation_bytes,
        "reasoning_format": model.reasoning_format,
        "reasoning_split": bool(model.reasoning_split),
        "enable_thinking": bool(model.enable_thinking),
        "supports_thinking": db_model_to_config(model).supports_thinking,
        "supports_image": bool(model.supports_image),
        "max_images": model.max_images,
        "supports_video": bool(model.supports_video),
        "max_videos": model.max_videos,
        "enabled": bool(model.enabled),
        "tags": _json_loads_list(model.tags_json),
        "api_key_set": bool(model.api_key),
        "group_names": group_names,
        "created_at": model.created_at.isoformat() if model.created_at else None,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
    }
