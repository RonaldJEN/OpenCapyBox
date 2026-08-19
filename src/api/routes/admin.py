"""管理后台 API（管理员专用）。

提供以下能力：
- 概览
- rounds 监控
- 用户管理（管理员 / 普通用户）
- 系统监控
"""

import csv
import io
import logging
import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.deps import get_current_admin_user
from src.api.models.auth_login_event import AuthLoginEvent
from src.api.models.auth_user import AuthUser
from src.api.models.database import get_db, get_engine_pool_diagnostics
from src.api.models.llm_model import LLMModel, LLMModelSettings
from src.api.models.model_permission import ModelPermissionGroup, ModelPermissionGroupModel, UserModelPermissionGroup
from src.api.models.sandbox_profile import SandboxProfile
from src.api.models.session import Session
from src.api.models.round import Round
from src.api.models.cron_job import CronJob
from src.api.models.user_sandbox import UserSandbox
from src.api.models.user_sandbox_config import UserSandboxConfig
from src.api.models.user_run_lock import UserRunLock
from src.api.models.user_memory import CronJobRun
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.subagent_run import SubagentRun
from src.api.services.agent_pool_service import get_agent_pool
from src.api.services.auth_service import (
    auth_user_to_payload,
    create_ldap_user,
    create_simple_user,
    delete_auth_user,
    reset_simple_user_password,
    update_user_admin,
    update_user_enabled,
    update_user_token_limits,
)
from src.api.services.sandbox_service import SandboxSessionService
from src.api.services.sandbox_profile_service import (
    RUNTIME_RECREATE_FIELDS,
    assign_user_sandbox_profile,
    ensure_default_sandbox_profile,
    get_user_sandbox_config_payload,
    sandbox_profile_to_payload,
    set_default_sandbox_profile,
)
from src.api.model_registry import (
    ModelConfig,
    VALID_PROVIDERS,
    VALID_REASONING_FORMATS,
    VALID_THINKING_MODES,
    VALID_THINKING_WIRE_FORMATS,
    reload_model_registry,
)
from src.api.services.model_access_service import (
    admin_model_payload,
    get_or_create_default_group,
    group_to_payload,
    list_permission_groups_payload,
    set_group_models,
    set_user_groups,
    user_model_groups_payload,
)
from src.api.services.admin_operation_audit import (
    AdminAuditRoute,
    admin_audit_action,
    enrich_admin_audit,
)
from src.api.utils.timezone import now_naive

router = APIRouter(route_class=AdminAuditRoute)
logger = logging.getLogger(__name__)


class ManualReviewUpdatePayload(BaseModel):
    manual_review_status: str


class AdminCreateSimpleUserPayload(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=1024)
    enabled: bool = True
    is_admin: bool = False
    token_limit_per_week: int | None = Field(default=None, ge=0)
    token_limit_per_month: int | None = Field(default=None, ge=0)
    sandbox_profile_id: str | None = Field(default=None, max_length=36)

    @field_validator("username", mode="before")
    @classmethod
    def _strip_username(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("password")
    @classmethod
    def _reject_blank_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("密码不能为空")
        return value


class AdminCreateLdapUserPayload(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=100)
    username: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool = True
    is_admin: bool = False
    token_limit_per_week: int | None = Field(default=None, ge=0)
    token_limit_per_month: int | None = Field(default=None, ge=0)
    sandbox_profile_id: str | None = Field(default=None, max_length=36)

    @field_validator("user_id", "username", mode="before")
    @classmethod
    def _strip_identifiers(cls, value):
        return value.strip() if isinstance(value, str) else value


class AdminEnabledUpdatePayload(BaseModel):
    enabled: bool


class AdminFlagUpdatePayload(BaseModel):
    is_admin: bool


class AdminTokenLimitsUpdatePayload(BaseModel):
    token_limit_per_week: int | None = Field(default=None, ge=0)
    token_limit_per_month: int | None = Field(default=None, ge=0)


class AdminSandboxProfilePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    department: str | None = Field(default=None, max_length=100)
    domain: str = Field(..., min_length=1, max_length=255)
    protocol: str = Field(default="http", pattern="^(http|https)$")
    api_key: str = Field(..., min_length=1)
    use_server_proxy: bool = True
    enabled: bool = True

    @field_validator("name", "domain", "protocol", "api_key", mode="before")
    @classmethod
    def _strip_required_strings(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("description", "department", mode="before")
    @classmethod
    def _strip_optional_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

class AdminSandboxProfilePatchPayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    department: str | None = Field(default=None, max_length=100)
    domain: str | None = Field(default=None, min_length=1, max_length=255)
    protocol: str | None = Field(default=None, pattern="^(http|https)$")
    api_key: str | None = None
    use_server_proxy: bool | None = None

    @field_validator("name", "domain", "protocol", mode="before")
    @classmethod
    def _strip_required_strings(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("description", "department", "api_key", mode="before")
    @classmethod
    def _strip_optional_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

class AdminSandboxProfileEnabledPayload(BaseModel):
    enabled: bool


class AdminUserSandboxProfilePayload(BaseModel):
    sandbox_profile_id: str | None = Field(default=None, max_length=36)
    force_recreate: bool = False


class AdminModelPayload(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=255)
    provider: str = Field(..., min_length=1, max_length=20)
    api_base: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    model_name: str = Field(..., min_length=1, max_length=255)
    max_tokens: int = Field(default=16384, gt=0)
    context_window: int = Field(default=128000, gt=0)
    auto_compact_token_limit: int | None = Field(default=None, gt=0)
    tool_output_truncation_bytes: int = Field(default=42667, gt=0)
    reasoning_format: str = "none"
    reasoning_split: bool = False
    enable_thinking: bool = False
    thinking_mode: str = "provider_default"
    thinking_wire_format: str = "enable_thinking"
    reasoning_effort: str | None = Field(default=None, max_length=40)
    supported_reasoning_efforts: list[str] = Field(default_factory=list, max_length=20)
    supports_image: bool = False
    max_images: int = Field(default=0, ge=0)
    supports_video: bool = False
    max_videos: int = Field(default=0, ge=0)
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)

    @field_validator("model_id", "display_name", "provider", "api_base", "api_key", "model_name", "reasoning_format", "thinking_mode", "thinking_wire_format", "reasoning_effort", mode="before")
    @classmethod
    def _strip_model_strings(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("reasoning_effort", mode="after")
    @classmethod
    def _empty_reasoning_effort_is_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("supported_reasoning_efforts")
    @classmethod
    def _valid_supported_reasoning_efforts(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("supported_reasoning_efforts 不能包含空项")
        if any(len(value) > 40 for value in normalized):
            raise ValueError("supported_reasoning_efforts 单项不能超过 40 个字符")
        return normalized

    @field_validator("provider")
    @classmethod
    def _valid_provider(cls, value: str) -> str:
        if value not in VALID_PROVIDERS:
            raise ValueError(f"provider 必须是 {sorted(VALID_PROVIDERS)}")
        return value

    @field_validator("reasoning_format")
    @classmethod
    def _valid_reasoning_format(cls, value: str) -> str:
        if value not in VALID_REASONING_FORMATS:
            raise ValueError(f"reasoning_format 必须是 {sorted(VALID_REASONING_FORMATS)}")
        return value

    @field_validator("thinking_mode")
    @classmethod
    def _valid_thinking_mode(cls, value: str) -> str:
        if value not in VALID_THINKING_MODES:
            raise ValueError(f"thinking_mode 必须是 {sorted(VALID_THINKING_MODES)}")
        return value

    @field_validator("thinking_wire_format")
    @classmethod
    def _valid_thinking_wire_format(cls, value: str) -> str:
        if value not in VALID_THINKING_WIRE_FORMATS:
            raise ValueError(
                f"thinking_wire_format 必须是 {sorted(VALID_THINKING_WIRE_FORMATS)}"
            )
        return value


class AdminModelPatchPayload(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, min_length=1, max_length=20)
    api_base: str | None = Field(default=None, min_length=1)
    api_key: str | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=255)
    max_tokens: int | None = Field(default=None, gt=0)
    context_window: int | None = Field(default=None, gt=0)
    auto_compact_token_limit: int | None = Field(default=None, gt=0)
    tool_output_truncation_bytes: int | None = Field(default=None, gt=0)
    reasoning_format: str | None = None
    reasoning_split: bool | None = None
    enable_thinking: bool | None = None
    thinking_mode: str | None = None
    thinking_wire_format: str | None = None
    reasoning_effort: str | None = Field(default=None, max_length=40)
    supported_reasoning_efforts: list[str] | None = Field(default=None, max_length=20)
    supports_image: bool | None = None
    max_images: int | None = Field(default=None, ge=0)
    supports_video: bool | None = None
    max_videos: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    tags: list[str] | None = None

    @field_validator("display_name", "provider", "api_base", "api_key", "model_name", "reasoning_format", "thinking_mode", "thinking_wire_format", "reasoning_effort", mode="before")
    @classmethod
    def _strip_optional_model_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("supported_reasoning_efforts")
    @classmethod
    def _valid_optional_supported_reasoning_efforts(
        cls,
        values: list[str] | None,
    ) -> list[str] | None:
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("supported_reasoning_efforts 不能包含空项")
        if any(len(value) > 40 for value in normalized):
            raise ValueError("supported_reasoning_efforts 单项不能超过 40 个字符")
        return normalized

    @field_validator("provider")
    @classmethod
    def _valid_optional_provider(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_PROVIDERS:
            raise ValueError(f"provider 必须是 {sorted(VALID_PROVIDERS)}")
        return value

    @field_validator("reasoning_format")
    @classmethod
    def _valid_optional_reasoning_format(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_REASONING_FORMATS:
            raise ValueError(f"reasoning_format 必须是 {sorted(VALID_REASONING_FORMATS)}")
        return value

    @field_validator("thinking_mode")
    @classmethod
    def _valid_optional_thinking_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_THINKING_MODES:
            raise ValueError(f"thinking_mode 必须是 {sorted(VALID_THINKING_MODES)}")
        return value

    @field_validator("thinking_wire_format")
    @classmethod
    def _valid_optional_thinking_wire_format(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_THINKING_WIRE_FORMATS:
            raise ValueError(
                f"thinking_wire_format 必须是 {sorted(VALID_THINKING_WIRE_FORMATS)}"
            )
        return value


class AdminModelSettingsPayload(BaseModel):
    default_model_id: str = Field(..., min_length=1, max_length=100)
    cron_default_model_id: str | None = Field(default=None, max_length=100)
    subagent_default_model_id: str | None = Field(default=None, max_length=100)

    @field_validator("default_model_id", "cron_default_model_id", "subagent_default_model_id", mode="before")
    @classmethod
    def _strip_settings_ids(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class AdminPermissionGroupPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip_group_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class AdminPermissionGroupPatchPayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip_group_patch_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class AdminModelIdsPayload(BaseModel):
    model_ids: list[str] = Field(default_factory=list)


class AdminGroupIdsPayload(BaseModel):
    group_ids: list[str] = Field(default_factory=list)


class AdminUserIdsPayload(BaseModel):
    user_ids: list[str] = Field(default_factory=list)


class AdminResetPasswordPayload(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)

    @field_validator("password")
    @classmethod
    def _reject_blank_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("密码不能为空")
        return value


class AdminUserExportPayload(BaseModel):
    user_ids: list[str] = Field(..., min_length=1, max_length=10000)

    @field_validator("user_ids")
    @classmethod
    def _normalize_user_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item and item.strip()]
        if not normalized or len(normalized) != len(value):
            raise ValueError("user_ids 不能包含空值")
        if any(len(item) > 100 for item in normalized):
            raise ValueError("user_id 长度不能超过 100")
        if len(set(normalized)) != len(normalized):
            raise ValueError("user_ids 不能重复")
        return normalized


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _tags_json(tags: list[str] | None) -> str:
    normalized = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    return json.dumps(list(dict.fromkeys(normalized)), ensure_ascii=False)


def _settings_payload(db: DBSession) -> dict[str, Any]:
    settings = db.query(LLMModelSettings).filter(LLMModelSettings.id == 1).first()
    if not settings:
        return {
            "default_model_id": None,
            "cron_default_model_id": None,
            "subagent_default_model_id": None,
        }
    return {
        "default_model_id": settings.default_model_id,
        "cron_default_model_id": settings.cron_default_model_id,
        "subagent_default_model_id": settings.subagent_default_model_id,
    }


def _ensure_model_ids_exist(db: DBSession, model_ids: list[str], *, enabled_only: bool = False) -> None:
    normalized = [mid for mid in model_ids if mid]
    if not normalized:
        return
    existing = {
        row.model_id: bool(row.enabled)
        for row in (
            db.query(LLMModel.model_id, LLMModel.enabled)
            .filter(LLMModel.model_id.in_(normalized))
            .all()
        )
    }
    missing = [mid for mid in normalized if mid not in existing]
    if missing:
        raise HTTPException(status_code=400, detail=f"模型不存在: {missing}")
    if enabled_only:
        disabled = [mid for mid in normalized if not existing[mid]]
        if disabled:
            raise HTTPException(status_code=400, detail=f"停用模型不能作为默认模型: {disabled}")


def _validate_model_config_values(data: dict[str, Any]) -> ModelConfig:
    """Return the validated domain object so callers persist its normalized state."""
    try:
        return ModelConfig(
            id=data["model_id"],
            display_name=data["display_name"],
            provider=data["provider"],
            api_base=data["api_base"],
            api_key=data["api_key"],
            model_name=data["model_name"],
            max_tokens=data["max_tokens"],
            context_window=data["context_window"],
            auto_compact_token_limit=data.get("auto_compact_token_limit"),
            tool_output_truncation_bytes=data.get("tool_output_truncation_bytes", 42667),
            reasoning_format=data["reasoning_format"],
            reasoning_split=data["reasoning_split"],
            enable_thinking=data["enable_thinking"],
            thinking_mode=data.get("thinking_mode", "provider_default"),
            thinking_wire_format=data.get("thinking_wire_format", "enable_thinking"),
            reasoning_effort=data.get("reasoning_effort"),
            supported_reasoning_efforts=data.get("supported_reasoning_efforts") or [],
            supports_image=data["supports_image"],
            max_images=data["max_images"],
            supports_video=data["supports_video"],
            max_videos=data["max_videos"],
            enabled=data["enabled"],
            tags=data.get("tags") or [],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _build_admin_models_payload(db: DBSession) -> dict[str, Any]:
    models = db.query(LLMModel).order_by(LLMModel.created_at.asc(), LLMModel.model_id.asc()).all()
    session_counts = {
        row[0]: int(row[1] or 0)
        for row in (
            db.query(Session.model_id, func.count(Session.id))
            .filter(Session.model_id.isnot(None))
            .group_by(Session.model_id)
            .all()
        )
    }
    model_payloads = []
    for model in models:
        payload = admin_model_payload(db, model)
        payload["session_count"] = session_counts.get(model.model_id, 0)
        model_payloads.append(payload)
    return {
        "models": model_payloads,
        "settings": _settings_payload(db),
    }


def _create_admin_model(db: DBSession, payload: AdminModelPayload) -> dict[str, Any]:
    payload_data = payload.model_dump()
    if payload.provider != "openai":
        payload_data["thinking_wire_format"] = "none"
    config = _validate_model_config_values(payload_data)
    model = LLMModel(
        model_id=config.id,
        display_name=config.display_name,
        provider=config.provider,
        api_base=config.api_base,
        api_key=config.api_key,
        model_name=config.model_name,
        max_tokens=config.max_tokens,
        context_window=config.context_window,
        auto_compact_token_limit=config.auto_compact_token_limit,
        tool_output_truncation_bytes=config.tool_output_truncation_bytes,
        reasoning_format=config.reasoning_format,
        reasoning_split=config.reasoning_split,
        enable_thinking=config.enable_thinking,
        thinking_mode=config.thinking_mode,
        thinking_wire_format=config.thinking_wire_format,
        reasoning_effort=config.reasoning_effort,
        supported_reasoning_efforts_json=_tags_json(config.supported_reasoning_efforts),
        supports_image=config.supports_image,
        max_images=config.max_images,
        supports_video=config.supports_video,
        max_videos=config.max_videos,
        enabled=config.enabled,
        tags_json=_tags_json(config.tags),
    )
    db.add(model)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="模型 ID 已存在") from e
    db.refresh(model)
    reload_model_registry()
    return admin_model_payload(db, model)


def _update_admin_model(db: DBSession, model_id: str, payload: AdminModelPatchPayload) -> dict[str, Any]:
    model = db.query(LLMModel).filter(LLMModel.model_id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    data = payload.model_dump(exclude_unset=True)
    if data.get("api_key") is None:
        data.pop("api_key", None)
    if "tags" in data:
        model.tags_json = _tags_json(data.pop("tags"))
    if "supported_reasoning_efforts" in data:
        model.supported_reasoning_efforts_json = _tags_json(
            data.pop("supported_reasoning_efforts") or []
        )
    for field_name, value in data.items():
        setattr(model, field_name, value)
    if model.provider != "openai":
        model.thinking_wire_format = "none"
    config = _validate_model_config_values({
        "model_id": model.model_id,
        "display_name": model.display_name,
        "provider": model.provider,
        "api_base": model.api_base,
        "api_key": model.api_key,
        "model_name": model.model_name,
        "max_tokens": model.max_tokens,
        "context_window": model.context_window,
        "auto_compact_token_limit": model.auto_compact_token_limit,
        "tool_output_truncation_bytes": model.tool_output_truncation_bytes,
        "reasoning_format": model.reasoning_format,
        "reasoning_split": model.reasoning_split,
        "enable_thinking": model.enable_thinking,
        "thinking_mode": model.thinking_mode,
        "thinking_wire_format": model.thinking_wire_format,
        "reasoning_effort": model.reasoning_effort,
        "supported_reasoning_efforts": json.loads(
            model.supported_reasoning_efforts_json or "[]"
        ),
        "supports_image": model.supports_image,
        "max_images": model.max_images,
        "supports_video": model.supports_video,
        "max_videos": model.max_videos,
        "enabled": model.enabled,
        "tags": json.loads(model.tags_json or "[]"),
    })
    # ModelConfig is the domain boundary: persist its complete normalized state,
    # not the mutable request DTO. Keeping this explicit also makes any future
    # ModelConfig normalization automatically authoritative for admin updates.
    model.display_name = config.display_name
    model.provider = config.provider
    model.api_base = config.api_base
    model.api_key = config.api_key
    model.model_name = config.model_name
    model.max_tokens = config.max_tokens
    model.context_window = config.context_window
    model.auto_compact_token_limit = config.auto_compact_token_limit
    model.tool_output_truncation_bytes = config.tool_output_truncation_bytes
    model.reasoning_format = config.reasoning_format
    model.reasoning_split = config.reasoning_split
    model.enable_thinking = config.enable_thinking
    model.thinking_mode = config.thinking_mode
    model.thinking_wire_format = config.thinking_wire_format
    model.reasoning_effort = config.reasoning_effort
    model.supported_reasoning_efforts_json = _tags_json(config.supported_reasoning_efforts)
    model.supports_image = config.supports_image
    model.max_images = config.max_images
    model.supports_video = config.supports_video
    model.max_videos = config.max_videos
    model.enabled = config.enabled
    model.tags_json = _tags_json(config.tags)
    if not config.enabled:
        db.query(ModelPermissionGroupModel).filter(
            ModelPermissionGroupModel.model_id == model.model_id
        ).delete(synchronize_session=False)
    model.updated_at = now_naive()
    db.commit()
    db.refresh(model)
    reload_model_registry()
    return admin_model_payload(db, model)


def _delete_admin_model(
    db: DBSession,
    model_id: str,
    *,
    replacement_model_id: str | None = None,
) -> dict[str, Any]:
    model = db.query(LLMModel).filter(LLMModel.model_id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if replacement_model_id == model_id:
        raise HTTPException(status_code=400, detail="替换模型不能是当前要删除的模型")

    replacement_model: LLMModel | None = None
    if replacement_model_id:
        replacement_model = db.query(LLMModel).filter(LLMModel.model_id == replacement_model_id).first()
        if not replacement_model:
            raise HTTPException(status_code=400, detail="替换模型不存在")
        if not replacement_model.enabled:
            raise HTTPException(status_code=400, detail="替换模型必须是启用状态")

    settings = db.query(LLMModelSettings).filter(LLMModelSettings.id == 1).first()
    default_usages: list[str] = []
    if settings:
        if settings.default_model_id == model_id:
            default_usages.append("普通对话默认模型")
        if settings.cron_default_model_id == model_id:
            default_usages.append("Cron 默认模型")
        if settings.subagent_default_model_id == model_id:
            default_usages.append("Subagent 默认模型")
    if default_usages and not replacement_model_id:
        raise HTTPException(
            status_code=400,
            detail=f"模型正在作为{'、'.join(default_usages)}使用，请先切换默认模型后再删除",
        )

    session_count = int(
        db.query(func.count(Session.id))
        .filter(Session.model_id == model_id)
        .scalar()
        or 0
    )
    if session_count and not replacement_model_id:
        raise HTTPException(
            status_code=409,
            detail=f"模型已被 {session_count} 个会话使用，不能直接删除；请选择替换模型迁移历史会话后再删除",
        )

    defaults_reassigned: list[str] = []
    if settings and replacement_model_id:
        if settings.default_model_id == model_id:
            settings.default_model_id = replacement_model_id
            defaults_reassigned.append("default_model_id")
        if settings.cron_default_model_id == model_id:
            settings.cron_default_model_id = replacement_model_id
            defaults_reassigned.append("cron_default_model_id")
        if settings.subagent_default_model_id == model_id:
            settings.subagent_default_model_id = replacement_model_id
            defaults_reassigned.append("subagent_default_model_id")
        if defaults_reassigned:
            settings.updated_at = now_naive()

    sessions_reassigned = 0
    if session_count and replacement_model_id:
        sessions_reassigned = int(
            db.query(Session)
            .filter(Session.model_id == model_id)
            .update(
                {
                    Session.model_id: replacement_model_id,
                    Session.updated_at: now_naive(),
                },
                synchronize_session=False,
            )
            or 0
        )

    old_group_ids = [
        row[0]
        for row in (
            db.query(ModelPermissionGroupModel.group_id)
            .filter(ModelPermissionGroupModel.model_id == model_id)
            .all()
        )
    ]
    if replacement_model_id and old_group_ids:
        replacement_group_ids = {
            row[0]
            for row in (
                db.query(ModelPermissionGroupModel.group_id)
                .filter(
                    ModelPermissionGroupModel.model_id == replacement_model_id,
                    ModelPermissionGroupModel.group_id.in_(old_group_ids),
                )
                .all()
            )
        }
        for group_id in old_group_ids:
            if group_id not in replacement_group_ids:
                db.add(ModelPermissionGroupModel(
                    group_id=group_id,
                    model_id=replacement_model_id,
                    created_by="delete-model-replacement",
                ))

    db.query(ModelPermissionGroupModel).filter(
        ModelPermissionGroupModel.model_id == model_id
    ).delete(synchronize_session=False)
    db.delete(model)
    db.commit()
    reload_model_registry()
    return {
        "model_id": model_id,
        "deleted": True,
        "replacement_model_id": replacement_model_id,
        "sessions_reassigned": sessions_reassigned,
        "defaults_reassigned": defaults_reassigned,
    }


def _update_model_settings(db: DBSession, payload: AdminModelSettingsPayload) -> dict[str, Any]:
    cron_id = payload.cron_default_model_id or payload.default_model_id
    subagent_id = payload.subagent_default_model_id or payload.default_model_id
    _ensure_model_ids_exist(db, [payload.default_model_id, cron_id, subagent_id], enabled_only=True)
    settings = db.query(LLMModelSettings).filter(LLMModelSettings.id == 1).first()
    if not settings:
        settings = LLMModelSettings(id=1)
        db.add(settings)
    settings.default_model_id = payload.default_model_id
    settings.cron_default_model_id = cron_id
    settings.subagent_default_model_id = subagent_id
    settings.updated_at = now_naive()
    db.commit()
    reload_model_registry()
    return _settings_payload(db)


def _create_permission_group(db: DBSession, payload: AdminPermissionGroupPayload, admin_user_id: str) -> dict[str, Any]:
    group = ModelPermissionGroup(
        name=payload.name,
        description=payload.description,
        is_default=False,
        created_by=admin_user_id,
    )
    db.add(group)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="模型权限包名称已存在") from e
    db.refresh(group)
    return group_to_payload(db, group)


def _update_permission_group(db: DBSession, group_id: str, payload: AdminPermissionGroupPatchPayload) -> dict[str, Any]:
    group = db.query(ModelPermissionGroup).filter(ModelPermissionGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="模型权限包不存在")
    data = payload.model_dump(exclude_unset=True)
    if group.is_default and data.get("name") and data["name"] != group.name:
        raise HTTPException(status_code=400, detail="默认权限包不能重命名")
    for field_name, value in data.items():
        setattr(group, field_name, value)
    group.updated_at = now_naive()
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="模型权限包名称已存在") from e
    db.refresh(group)
    return group_to_payload(db, group)


def _auth_login_event_to_payload(event: AuthLoginEvent) -> dict[str, Any]:
    return {
        "id": int(event.id),
        "user_id": event.user_id,
        "username": event.username,
        "auth_type": event.auth_type,
        "ip_address": event.ip_address,
        "user_agent": event.user_agent,
        "login_at": _iso(event.login_at),
    }


def _sandbox_profile_bound_counts(db: DBSession) -> dict[str, int]:
    profiles = db.query(SandboxProfile).all()
    total_users = int(db.query(func.count(AuthUser.id)).scalar() or 0)
    explicit_rows = (
        db.query(UserSandboxConfig.sandbox_profile_id, func.count(UserSandboxConfig.user_id))
        .filter(UserSandboxConfig.sandbox_profile_id.isnot(None))
        .group_by(UserSandboxConfig.sandbox_profile_id)
        .all()
    )
    explicit_counts = {row[0]: int(row[1]) for row in explicit_rows if row[0]}
    explicit_total = sum(explicit_counts.values())
    counts = {profile.id: explicit_counts.get(profile.id, 0) for profile in profiles}
    default_profile = next((profile for profile in profiles if profile.is_default), None)
    if default_profile:
        counts[default_profile.id] = counts.get(default_profile.id, 0) + max(total_users - explicit_total, 0)
    return counts


def _profile_or_404(db: DBSession, profile_id: str) -> SandboxProfile:
    profile = db.query(SandboxProfile).filter(SandboxProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="沙箱后端不存在")
    return profile


def _ensure_profile_assignable(db: DBSession, profile_id: str) -> SandboxProfile:
    profile = _profile_or_404(db, profile_id)
    if not profile.enabled:
        raise HTTPException(status_code=400, detail="禁用的沙箱后端不能分配")
    return profile


def _runtime_field_changed(profile: SandboxProfile, field_name: str, next_value) -> bool:
    current_value = getattr(profile, field_name)
    return current_value != next_value


def _sandbox_profile_list_payload(db: DBSession) -> dict[str, Any]:
    counts = _sandbox_profile_bound_counts(db)
    profiles = db.query(SandboxProfile).order_by(SandboxProfile.is_default.desc(), SandboxProfile.name.asc()).all()
    return {
        "profiles": [
            sandbox_profile_to_payload(profile, bound_users=counts.get(profile.id, 0))
            for profile in profiles
        ],
    }


def _create_sandbox_profile(db: DBSession, payload: AdminSandboxProfilePayload) -> SandboxProfile:
    import uuid

    profile = SandboxProfile(
        id=str(uuid.uuid4()),
        name=payload.name,
        description=payload.description,
        department=payload.department,
        domain=payload.domain,
        protocol=payload.protocol,
        api_key=payload.api_key,
        use_server_proxy=payload.use_server_proxy,
        enabled=payload.enabled,
        is_default=False,
        version=1,
    )
    db.add(profile)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="沙箱后端名称已存在") from e
    db.refresh(profile)
    return profile


def _update_sandbox_profile(
    db: DBSession,
    profile_id: str,
    payload: AdminSandboxProfilePatchPayload,
) -> SandboxProfile:
    profile = _profile_or_404(db, profile_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("api_key") is None:
        data.pop("api_key", None)
    runtime_changed = False
    for field_name, value in data.items():
        if field_name in RUNTIME_RECREATE_FIELDS and _runtime_field_changed(profile, field_name, value):
            runtime_changed = True
        setattr(profile, field_name, value)
    if runtime_changed:
        profile.version = int(profile.version or 1) + 1
    profile.updated_at = now_naive()
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="沙箱后端名称已存在") from e
    db.refresh(profile)
    return profile


def _set_sandbox_profile_enabled(
    db: DBSession,
    profile_id: str,
    enabled: bool,
) -> SandboxProfile:
    profile = _profile_or_404(db, profile_id)
    if profile.is_default and not enabled:
        raise HTTPException(status_code=400, detail="默认沙箱后端不能禁用")
    profile.enabled = enabled
    profile.updated_at = now_naive()
    db.commit()
    db.refresh(profile)
    return profile


def _user_has_running_work(db: DBSession, user_id: str) -> bool:
    lock_cutoff = now_naive() - timedelta(seconds=get_settings().sse_subscribe_timeout)
    active_lock = (
        db.query(UserRunLock)
        .filter(UserRunLock.user_id == user_id, UserRunLock.updated_at >= lock_cutoff)
        .first()
    )
    if active_lock:
        return True
    running_cron = (
        db.query(CronJobRun)
        .filter(CronJobRun.user_id == user_id, CronJobRun.status == "running")
        .first()
    )
    return running_cron is not None


def _normalize_profile_assignment(db: DBSession, profile_id: str | None) -> str | None:
    if isinstance(profile_id, str):
        profile_id = profile_id.strip()
    if not profile_id:
        return None
    profile = _ensure_profile_assignable(db, profile_id)
    return None if profile.is_default else profile.id


def _get_user_assigned_profile_id(db: DBSession, user_id: str) -> str | None:
    config = db.query(UserSandboxConfig).filter(UserSandboxConfig.user_id == user_id).first()
    return config.sandbox_profile_id if config and config.sandbox_profile_id else None


def _user_sandbox_profile_stale(db: DBSession, *, user_id: str, desired_profile_id: str | None) -> bool:
    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    if not user_sandbox or not user_sandbox.sandbox_id:
        return False

    profile = (
        db.query(SandboxProfile).filter(SandboxProfile.id == desired_profile_id).first()
        if desired_profile_id
        else ensure_default_sandbox_profile(db)
    )
    if profile is None:
        return True
    return (
        user_sandbox.active_profile_id != profile.id
        or int(user_sandbox.active_profile_version or 0) != int(profile.version or 1)
    )


async def _clear_user_sandbox_binding_after_kill(
    db: DBSession,
    *,
    user_id: str,
    sandbox_service: SandboxSessionService,
) -> None:
    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    sandbox_id = user_sandbox.sandbox_id if user_sandbox else None
    if sandbox_id or sandbox_service.get_cached(user_id):
        sandbox_deleted = await sandbox_service.kill(user_id, sandbox_id)
        if not sandbox_deleted:
            logger.warning(
                "用户沙箱清理失败，仍继续切换沙箱配置 (user=%s, sandbox_id=%s)",
                user_id,
                sandbox_id,
            )
    if user_sandbox:
        user_sandbox.sandbox_id = None
        user_sandbox.active_profile_id = None
        user_sandbox.active_profile_version = None
        user_sandbox.status = "active"
        db.commit()


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None

    ordered = sorted(values)
    pos = (len(ordered) - 1) * p
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    value = ordered[low] * (1 - frac) + ordered[high] * frac
    return round(value, 3)


def _build_overview_payload(db: DBSession, days: int) -> dict[str, Any]:
    now = now_naive()
    since_24h = now - timedelta(hours=24)
    since_days = now - timedelta(days=days)

    users_total = db.query(func.count(AuthUser.id)).scalar()
    admins_total = db.query(func.count(AuthUser.id)).filter(AuthUser.is_admin.is_(True)).scalar()

    sessions_total = db.query(func.count(Session.id)).scalar()
    rounds_total = db.query(func.count(Round.id)).scalar()
    rounds_24h = db.query(func.count(Round.id)).filter(Round.created_at >= since_24h).scalar()
    rounds_running = db.query(func.count(Round.id)).filter(Round.status == "running").scalar()

    cron_jobs_total = db.query(func.count(CronJob.id)).scalar()
    cron_jobs_enabled = (
        db.query(func.count(CronJob.id))
        .filter(CronJob.enabled.is_(True))
        .scalar()
    )
    cron_failed_24h = (
        db.query(func.count(CronJobRun.id))
        .filter(CronJobRun.status == "failed", CronJobRun.started_at >= since_24h)
        .scalar()
    )

    llm_calls_24h = (
        db.query(func.count(LLMCallRecord.id))
        .filter(LLMCallRecord.created_at >= since_24h)
        .scalar()
    )
    tokens_24h = (
        db.query(func.coalesce(func.sum(LLMCallRecord.usage_total_tokens), 0))
        .filter(LLMCallRecord.created_at >= since_24h)
        .scalar()
    )
    avg_completion_latency_24h = (
        db.query(func.avg(LLMCallRecord.completion_latency_s))
        .filter(
            LLMCallRecord.created_at >= since_24h,
            LLMCallRecord.completion_latency_s.isnot(None),
        )
        .scalar()
    )

    day_labels = [(now.date() - timedelta(days=offset)).isoformat() for offset in reversed(range(days))]
    trend_map = {day: {"date": day, "rounds": 0, "tokens": 0} for day in day_labels}

    round_rows = (
        db.query(Round.created_at)
        .filter(Round.created_at >= since_days)
        .all()
    )
    for (created_at,) in round_rows:
        day = created_at.date().isoformat()
        if day in trend_map:
            trend_map[day]["rounds"] += 1

    token_rows = (
        db.query(LLMCallRecord.created_at, LLMCallRecord.usage_total_tokens)
        .filter(LLMCallRecord.created_at >= since_days)
        .all()
    )
    for created_at, token_count in token_rows:
        day = created_at.date().isoformat()
        if day in trend_map:
            trend_map[day]["tokens"] += int(token_count or 0)

    return {
        "window_days": days,
        "summary": {
            "users_total": users_total,
            "admins_total": admins_total,
            "sessions_total": sessions_total,
            "rounds_total": rounds_total,
            "rounds_24h": rounds_24h,
            "rounds_running": rounds_running,
            "cron_jobs_total": cron_jobs_total,
            "cron_jobs_enabled": cron_jobs_enabled,
            "cron_failed_24h": cron_failed_24h,
            "llm_calls_24h": llm_calls_24h,
            "tokens_24h": int(tokens_24h),
            "avg_completion_latency_24h": round(float(avg_completion_latency_24h), 3)
            if avg_completion_latency_24h is not None
            else None,
        },
        "trends": [trend_map[day] for day in day_labels],
    }

def _build_rounds_tree_payload(
    db: DBSession,
    *,
    limit: int,
    offset: int,
    status: str,
    user_id: str | None,
    search: str | None,
) -> dict[str, Any]:
    filters = _admin_round_filters(status=status, user_id=user_id, search=search)
    matching_rounds = (
        db.query(
            Round.id.label("round_id"),
            Round.session_id.label("session_id"),
            Round.status.label("status"),
            Round.step_count.label("step_count"),
            Round.created_at.label("created_at"),
            Round.completed_at.label("completed_at"),
        )
        .join(Session, Session.id == Round.session_id)
        .filter(*filters)
        .subquery()
    )

    total_sessions = int(
        db.query(func.count(func.distinct(matching_rounds.c.session_id))).scalar() or 0
    )

    usage_by_session = (
        db.query(
            matching_rounds.c.session_id.label("session_id"),
            func.coalesce(func.sum(LLMCallRecord.usage_total_tokens), 0).label("total_tokens"),
            func.count(LLMCallRecord.id).label("llm_calls"),
            func.coalesce(
                func.sum(case((LLMCallRecord.response_error.isnot(None), 1), else_=0)),
                0,
            ).label("error_calls"),
            func.coalesce(
                func.sum(case((LLMCallRecord.call_kind == "compaction", 1), else_=0)),
                0,
            ).label("compaction_steps"),
        )
        .outerjoin(LLMCallRecord, LLMCallRecord.round_id == matching_rounds.c.round_id)
        .group_by(matching_rounds.c.session_id)
        .subquery()
    )

    now_for_duration = now_naive()
    duration_seconds = func.extract(
        "epoch",
        func.coalesce(matching_rounds.c.completed_at, now_for_duration) - matching_rounds.c.created_at,
    )

    session_rows = (
        db.query(
            Session.id.label("session_id"),
            Session.user_id,
            Session.title.label("session_title"),
            func.count(matching_rounds.c.round_id).label("rounds_count"),
            func.max(matching_rounds.c.created_at).label("last_round_at"),
            func.coalesce(func.sum(matching_rounds.c.step_count), 0).label("sum_step_count"),
            func.coalesce(
                func.sum(case((matching_rounds.c.status == "running", 1), else_=0)),
                0,
            ).label("running_rounds"),
            func.coalesce(
                func.sum(case((matching_rounds.c.status.in_({"failed", "cancelled", "interrupted"}), 1), else_=0)),
                0,
            ).label("error_rounds"),
            func.coalesce(usage_by_session.c.total_tokens, 0).label("total_tokens"),
            func.coalesce(usage_by_session.c.llm_calls, 0).label("llm_calls"),
            func.coalesce(usage_by_session.c.error_calls, 0).label("error_calls"),
            func.coalesce(usage_by_session.c.compaction_steps, 0).label("compaction_steps"),
            func.coalesce(
                func.sum(case((matching_rounds.c.created_at.isnot(None), duration_seconds), else_=0)),
                0,
            ).label("total_duration_s"),
        )
        .join(matching_rounds, matching_rounds.c.session_id == Session.id)
        .outerjoin(usage_by_session, usage_by_session.c.session_id == Session.id)
        .group_by(
            Session.id,
            Session.user_id,
            Session.title,
            usage_by_session.c.total_tokens,
            usage_by_session.c.llm_calls,
            usage_by_session.c.error_calls,
            usage_by_session.c.compaction_steps,
        )
        .order_by(func.max(matching_rounds.c.created_at).desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    ordered_sessions: list[dict[str, Any]] = []
    for row in session_rows:
        if int(row.running_rounds or 0) > 0:
            session_status = "running"
        elif int(row.error_rounds or 0) > 0:
            session_status = "error"
        else:
            session_status = "completed"

        ordered_sessions.append({
            "session_id": row.session_id,
            "user_id": row.user_id,
            "session_title": row.session_title,
            "rounds_count": int(row.rounds_count or 0),
            "last_round_at": _iso(row.last_round_at),
            "sum_step_count": int(row.sum_step_count or 0),
            "total_tokens": int(row.total_tokens or 0),
            "llm_calls": int(row.llm_calls or 0),
            "error_calls": int(row.error_calls or 0),
            "compaction_steps": int(row.compaction_steps or 0),
            "total_duration_s": round(float(row.total_duration_s or 0), 3),
            "status": session_status,
            "rounds_loaded": False,
            "rounds": [],
        })

    return {
        "total_sessions": total_sessions,
        "offset": offset,
        "limit": limit,
        "sessions": ordered_sessions,
    }


def _admin_round_filters(
    *,
    status: str,
    user_id: str | None = None,
    search: str | None = None,
    session_id: str | None = None,
) -> list[Any]:
    filters: list[Any] = []
    if session_id:
        filters.append(Round.session_id == session_id)
    if status != "all":
        filters.append(Round.status == status)
    if user_id:
        filters.append(Session.user_id == user_id)
    if search:
        filters.append(or_(Round.user_message.contains(search), Round.final_response.contains(search)))
    return filters


def _build_lightweight_step_payload(row: Any) -> dict[str, Any]:
    return {
        "llm_record_id": int(row.id),
        "step_index": int(row.step_index),
        "call_kind": row.call_kind or "agent_step",
        "checkpoint_id": row.checkpoint_id,
        "request_message_count": int(row.request_message_count or 0),
        "request_messages": "",
        "request_tools": "",
        "finish_reason": row.finish_reason,
        "response_error": row.response_error,
        "response_content": "",
        "response_thinking": "",
        "response_tool_calls": "",
        "response_preview": "",
        "usage_prompt_tokens": int(row.usage_prompt_tokens or 0),
        "usage_completion_tokens": int(row.usage_completion_tokens or 0),
        "usage_total_tokens": int(row.usage_total_tokens or 0),
        "first_token_latency_s": round(float(row.first_token_latency_s), 3)
        if row.first_token_latency_s is not None
        else None,
        "completion_latency_s": round(float(row.completion_latency_s), 3)
        if row.completion_latency_s is not None
        else None,
        "compaction_triggered": bool(row.compaction_triggered),
        "compaction_pre_tokens": int(row.compaction_pre_tokens or 0),
        "compaction_post_tokens": int(row.compaction_post_tokens or 0),
        "compaction_tokens_saved": int(row.compaction_tokens_saved or 0),
        "compaction_microcompact_compacted_messages": int(row.compaction_microcompact_compacted_messages or 0),
        "compaction_summary_generated_count": int(row.compaction_summary_generated_count or 0),
        "compaction_summary_reused_count": int(row.compaction_summary_reused_count or 0),
        "compaction_summary_quality_repair_count": int(row.compaction_summary_quality_repair_count or 0),
        "compaction_emergency_truncate_dropped_rounds": int(row.compaction_emergency_truncate_dropped_rounds or 0),
        "manual_review_status": row.manual_review_status,
        "created_at": _iso(row.created_at),
    }


def _build_round_items_from_rows(db: DBSession, round_rows: list[Any]) -> list[dict[str, Any]]:
    round_ids = [row.id for row in round_rows]

    subagent_edges = (
        db.query(
            SubagentRun.id,
            SubagentRun.child_run_id,
            SubagentRun.parent_run_id,
            SubagentRun.root_run_id,
            SubagentRun.agent_type,
            SubagentRun.description,
            func.substr(func.coalesce(SubagentRun.prompt, ""), 1, 180).label("prompt_preview"),
        )
        .filter(
            or_(
                SubagentRun.child_run_id.in_(round_ids),
                SubagentRun.parent_run_id.in_(round_ids),
            )
        )
        .all()
    ) if round_ids else []
    subagent_by_child_run_id = {
        edge.child_run_id: edge
        for edge in subagent_edges
        if edge.child_run_id
    }
    subagent_child_count_by_parent: dict[str, int] = {}
    for edge in subagent_edges:
        subagent_child_count_by_parent[edge.parent_run_id] = (
            subagent_child_count_by_parent.get(edge.parent_run_id, 0) + 1
        )

    usage_rows = (
        db.query(
            LLMCallRecord.round_id,
            func.coalesce(func.sum(LLMCallRecord.usage_total_tokens), 0).label("total_tokens"),
            func.count(LLMCallRecord.id).label("llm_calls"),
            func.coalesce(
                func.sum(case((LLMCallRecord.response_error.isnot(None), 1), else_=0)),
                0,
            ).label("error_calls"),
            func.coalesce(
                func.sum(case((LLMCallRecord.call_kind == "compaction", 1), else_=0)),
                0,
            ).label("compaction_steps"),
        )
        .filter(LLMCallRecord.round_id.in_(round_ids))
        .group_by(LLMCallRecord.round_id)
        .all()
    ) if round_ids else []

    usage_map: dict[str, dict[str, int]] = {
        row.round_id: {
            "total_tokens": int(row.total_tokens),
            "llm_calls": int(row.llm_calls),
            "error_calls": int(row.error_calls),
            "compaction_steps": int(row.compaction_steps),
        }
        for row in usage_rows
    }

    step_rows = (
        db.query(
            LLMCallRecord.id,
            LLMCallRecord.round_id,
            LLMCallRecord.step_index,
            LLMCallRecord.call_kind,
            LLMCallRecord.checkpoint_id,
            LLMCallRecord.request_message_count,
            LLMCallRecord.finish_reason,
            LLMCallRecord.response_error,
            LLMCallRecord.usage_prompt_tokens,
            LLMCallRecord.usage_completion_tokens,
            LLMCallRecord.usage_total_tokens,
            LLMCallRecord.first_token_latency_s,
            LLMCallRecord.completion_latency_s,
            LLMCallRecord.compaction_triggered,
            LLMCallRecord.compaction_pre_tokens,
            LLMCallRecord.compaction_post_tokens,
            LLMCallRecord.compaction_tokens_saved,
            LLMCallRecord.compaction_microcompact_compacted_messages,
            LLMCallRecord.compaction_summary_generated_count,
            LLMCallRecord.compaction_summary_reused_count,
            LLMCallRecord.compaction_summary_quality_repair_count,
            LLMCallRecord.compaction_emergency_truncate_dropped_rounds,
            LLMCallRecord.manual_review_status,
            LLMCallRecord.created_at,
        )
        .filter(LLMCallRecord.round_id.in_(round_ids))
        .order_by(LLMCallRecord.created_at, LLMCallRecord.id)
        .all()
    ) if round_ids else []

    step_map: dict[str, list[dict[str, Any]]] = {}
    for row in step_rows:
        step_map.setdefault(row.round_id, []).append(_build_lightweight_step_payload(row))

    now = now_naive()
    round_items: list[dict[str, Any]] = []
    for row in round_rows:
        subagent_edge = subagent_by_child_run_id.get(row.id)
        is_subagent_round = subagent_edge is not None
        usage = usage_map.get(
            row.id,
            {
                "total_tokens": 0,
                "llm_calls": 0,
                "error_calls": 0,
                "compaction_steps": 0,
            },
        )
        duration_s = 0.0
        if row.created_at:
            ended_at = row.completed_at or now
            duration_s = round((ended_at - row.created_at).total_seconds(), 3)
        steps = step_map.get(row.id, [])
        subagent_description = subagent_edge.description if subagent_edge else None

        round_items.append({
            "round_id": row.id,
            "session_id": row.session_id,
            "user_id": row.user_id,
            "session_title": row.session_title,
            "run_kind": "subagent" if is_subagent_round else "main",
            "parent_run_id": subagent_edge.parent_run_id if subagent_edge else row.parent_run_id,
            "root_run_id": subagent_edge.root_run_id if subagent_edge else row.id,
            "subagent_edge_id": subagent_edge.id if subagent_edge else None,
            "subagent_type": subagent_edge.agent_type if subagent_edge else None,
            "subagent_description": subagent_description,
            "subagent_prompt_preview": subagent_edge.prompt_preview if subagent_edge else "",
            "subagent_child_count": int(subagent_child_count_by_parent.get(row.id, 0)),
            "status": row.status,
            "step_count": int(row.step_count or 0),
            "started_at": _iso(row.created_at),
            "completed_at": _iso(row.completed_at),
            "duration_s": duration_s,
            "user_message_preview": row.user_message_preview or "",
            "final_response_preview": row.final_response_preview or "",
            **usage,
            "steps": steps,
        })
    return round_items


def _build_session_rounds_payload(
    db: DBSession,
    *,
    session_id: str,
    status: str,
    search: str | None,
) -> dict[str, Any]:
    session = (
        db.query(Session.id)
        .filter(Session.id == session_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session 不存在")

    filters = _admin_round_filters(status=status, search=search, session_id=session_id)
    round_rows = (
        db.query(
            Round.id,
            Round.session_id,
            Session.user_id,
            Session.title.label("session_title"),
            Round.status,
            Round.step_count,
            Round.created_at,
            Round.completed_at,
            Round.parent_run_id,
            func.substr(func.coalesce(Round.user_message, ""), 1, 120).label("user_message_preview"),
            func.substr(func.coalesce(Round.final_response, ""), 1, 180).label("final_response_preview"),
        )
        .join(Session, Session.id == Round.session_id)
        .filter(*filters)
        .order_by(Round.created_at.desc())
        .all()
    )
    return {
        "session_id": session_id,
        "rounds": _build_round_items_from_rows(db, round_rows),
    }


def _build_llm_record_detail_payload(db: DBSession, llm_record_id: int) -> dict[str, Any]:
    row = (
        db.query(
            LLMCallRecord.id,
            LLMCallRecord.round_id,
            LLMCallRecord.step_index,
            LLMCallRecord.call_kind,
            LLMCallRecord.checkpoint_id,
            LLMCallRecord.request_message_count,
            LLMCallRecord.request_messages,
            LLMCallRecord.request_tools,
            LLMCallRecord.finish_reason,
            LLMCallRecord.response_error,
            LLMCallRecord.response_content,
            LLMCallRecord.response_thinking,
            LLMCallRecord.response_tool_calls,
            LLMCallRecord.usage_prompt_tokens,
            LLMCallRecord.usage_completion_tokens,
            LLMCallRecord.usage_total_tokens,
            LLMCallRecord.first_token_latency_s,
            LLMCallRecord.completion_latency_s,
            LLMCallRecord.compaction_triggered,
            LLMCallRecord.compaction_pre_tokens,
            LLMCallRecord.compaction_post_tokens,
            LLMCallRecord.compaction_tokens_saved,
            LLMCallRecord.compaction_microcompact_compacted_messages,
            LLMCallRecord.compaction_summary_generated_count,
            LLMCallRecord.compaction_summary_reused_count,
            LLMCallRecord.compaction_summary_quality_repair_count,
            LLMCallRecord.compaction_emergency_truncate_dropped_rounds,
            LLMCallRecord.manual_review_status,
            LLMCallRecord.created_at,
        )
        .filter(LLMCallRecord.id == llm_record_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="llm_call_record 不存在")

    return {
        "llm_record_id": int(row.id),
        "round_id": row.round_id,
        "step_index": int(row.step_index),
        "call_kind": row.call_kind or "agent_step",
        "checkpoint_id": row.checkpoint_id,
        "request_message_count": int(row.request_message_count or 0),
        "request_messages": row.request_messages or "",
        "request_tools": row.request_tools or "",
        "finish_reason": row.finish_reason,
        "response_error": row.response_error,
        "response_content": row.response_content or "",
        "response_thinking": row.response_thinking or "",
        "response_tool_calls": row.response_tool_calls or "",
        "response_preview": (row.response_content or "")[:180],
        "usage_prompt_tokens": int(row.usage_prompt_tokens or 0),
        "usage_completion_tokens": int(row.usage_completion_tokens or 0),
        "usage_total_tokens": int(row.usage_total_tokens or 0),
        "first_token_latency_s": round(float(row.first_token_latency_s), 3)
        if row.first_token_latency_s is not None
        else None,
        "completion_latency_s": round(float(row.completion_latency_s), 3)
        if row.completion_latency_s is not None
        else None,
        "compaction_triggered": bool(row.compaction_triggered),
        "compaction_pre_tokens": int(row.compaction_pre_tokens or 0),
        "compaction_post_tokens": int(row.compaction_post_tokens or 0),
        "compaction_tokens_saved": int(row.compaction_tokens_saved or 0),
        "compaction_microcompact_compacted_messages": int(row.compaction_microcompact_compacted_messages or 0),
        "compaction_summary_generated_count": int(row.compaction_summary_generated_count or 0),
        "compaction_summary_reused_count": int(row.compaction_summary_reused_count or 0),
        "compaction_summary_quality_repair_count": int(row.compaction_summary_quality_repair_count or 0),
        "compaction_emergency_truncate_dropped_rounds": int(row.compaction_emergency_truncate_dropped_rounds or 0),
        "manual_review_status": row.manual_review_status,
        "created_at": _iso(row.created_at),
    }


def _update_llm_record_review_status(
    db: DBSession,
    *,
    llm_record_id: int,
    manual_review_status: str,
) -> dict[str, Any]:
    if manual_review_status not in {"没问题", "有问题"}:
        raise HTTPException(status_code=400, detail="manual_review_status 仅支持 没问题/有问题")

    record = (
        db.query(LLMCallRecord)
        .filter(LLMCallRecord.id == llm_record_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="llm_call_record 不存在")

    record.manual_review_status = manual_review_status
    db.commit()

    return {
        "llm_record_id": int(record.id),
        "manual_review_status": record.manual_review_status,
    }


def _build_users_payload(db: DBSession) -> dict[str, Any]:
    now = now_naive()
    since_24h = now - timedelta(hours=24)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    lock_cutoff = now - timedelta(seconds=get_settings().sse_subscribe_timeout)

    auth_user_rows = db.query(AuthUser).order_by(AuthUser.username).all()
    user_ids = [user.user_id for user in auth_user_rows]
    if not user_ids:
        return {
            "summary": {
                "users_total": 0,
                "admins_total": 0,
                "active_total": 0,
                "running_total": 0,
            },
            "users": [],
        }

    session_rows = (
        db.query(
            Session.user_id,
            func.count(Session.id).label("sessions_count"),
            func.max(Session.updated_at).label("last_session_at"),
        )
        .filter(Session.user_id.in_(user_ids))
        .group_by(Session.user_id)
        .all()
    )
    session_map = {
        row.user_id: {
            "sessions_count": int(row.sessions_count),
            "last_session_at": row.last_session_at,
        }
        for row in session_rows
    }

    round_rows = (
        db.query(
            Session.user_id,
            func.count(Round.id).label("rounds_count"),
            func.coalesce(
                func.sum(case((Round.status == "running", 1), else_=0)),
                0,
            ).label("running_rounds"),
            func.max(Round.created_at).label("last_round_at"),
        )
        .join(Round, Round.session_id == Session.id)
        .filter(Session.user_id.in_(user_ids))
        .group_by(Session.user_id)
        .all()
    )
    round_map = {
        row.user_id: {
            "rounds_count": int(row.rounds_count),
            "running_rounds": int(row.running_rounds),
            "last_round_at": row.last_round_at,
        }
        for row in round_rows
    }

    token_rows = (
        db.query(
            Session.user_id,
            func.coalesce(func.sum(LLMCallRecord.usage_total_tokens), 0).label("total_tokens"),
            func.coalesce(
                func.sum(case((LLMCallRecord.created_at >= week_start, LLMCallRecord.usage_total_tokens), else_=0)),
                0,
            ).label("weekly_tokens"),
            func.coalesce(
                func.sum(case((LLMCallRecord.created_at >= month_start, LLMCallRecord.usage_total_tokens), else_=0)),
                0,
            ).label("monthly_tokens"),
        )
        .join(LLMCallRecord, LLMCallRecord.session_id == Session.id)
        .filter(Session.user_id.in_(user_ids))
        .group_by(Session.user_id)
        .all()
    )
    token_map = {
        row.user_id: {
            "total_tokens": int(row.total_tokens),
            "weekly_tokens": int(row.weekly_tokens),
            "monthly_tokens": int(row.monthly_tokens),
        }
        for row in token_rows
    }

    cron_rows = (
        db.query(
            CronJob.user_id,
            func.count(CronJob.id).label("cron_jobs_total"),
            func.coalesce(
                func.sum(case((CronJob.enabled.is_(True), 1), else_=0)),
                0,
            ).label("cron_jobs_enabled"),
        )
        .filter(CronJob.user_id.in_(user_ids))
        .group_by(CronJob.user_id)
        .all()
    )
    cron_map = {
        row.user_id: {
            "cron_jobs_total": int(row.cron_jobs_total),
            "cron_jobs_enabled": int(row.cron_jobs_enabled),
        }
        for row in cron_rows
    }

    cron_failed_rows = (
        db.query(
            CronJobRun.user_id,
            func.count(CronJobRun.id).label("cron_failed_24h"),
        )
        .filter(
            CronJobRun.status == "failed",
            CronJobRun.started_at >= since_24h,
            CronJobRun.user_id.in_(user_ids),
        )
        .group_by(CronJobRun.user_id)
        .all()
    )
    cron_failed_map = {row.user_id: int(row.cron_failed_24h) for row in cron_failed_rows}

    lock_rows = (
        db.query(
            UserRunLock.user_id,
            func.count(UserRunLock.user_id).label("running_locks"),
            func.max(UserRunLock.updated_at).label("last_lock_at"),
        )
        .filter(
            UserRunLock.user_id.in_(user_ids),
            UserRunLock.updated_at >= lock_cutoff,
        )
        .group_by(UserRunLock.user_id)
        .all()
    )
    lock_map = {
        row.user_id: {
            "running_locks": int(row.running_locks),
            "last_lock_at": row.last_lock_at,
        }
        for row in lock_rows
    }

    latest_login_subq = (
        db.query(
            AuthLoginEvent.user_id,
            func.max(AuthLoginEvent.login_at).label("last_login_event_at"),
        )
        .filter(AuthLoginEvent.user_id.in_(user_ids))
        .group_by(AuthLoginEvent.user_id)
        .subquery()
    )
    latest_login_rows = (
        db.query(
            AuthLoginEvent.user_id,
            AuthLoginEvent.ip_address,
            AuthLoginEvent.login_at,
        )
        .join(
            latest_login_subq,
            (AuthLoginEvent.user_id == latest_login_subq.c.user_id)
            & (AuthLoginEvent.login_at == latest_login_subq.c.last_login_event_at),
        )
        .all()
    )
    latest_login_map = {
        row.user_id: {
            "ip_address": row.ip_address,
            "login_at": row.login_at,
        }
        for row in latest_login_rows
    }

    users: list[dict[str, Any]] = []
    for auth_user in auth_user_rows:
        user_id = auth_user.user_id
        session_info = session_map.get(user_id, {})
        round_info = round_map.get(user_id, {})
        cron_info = cron_map.get(user_id, {})
        token_info = token_map.get(user_id, {})
        lock_info = lock_map.get(user_id, {})

        last_active_candidates = [
            session_info.get("last_session_at"),
            round_info.get("last_round_at"),
            lock_info.get("last_lock_at"),
        ]
        last_active = max((dt for dt in last_active_candidates if dt is not None), default=None)

        running_rounds = max(
            int(round_info.get("running_rounds", 0)),
            int(lock_info.get("running_locks", 0)),
        )
        if running_rounds > 0:
            status = "running"
        elif last_active and (now - last_active) <= timedelta(days=7):
            status = "active"
        else:
            status = "idle"

        is_admin = bool(auth_user.is_admin)
        sandbox_info = get_user_sandbox_config_payload(db, user_id)
        model_groups = user_model_groups_payload(db, user_id)
        users.append(
            {
                "user_id": user_id,
                "username": auth_user.username,
                "auth_type": auth_user.auth_type,
                "enabled": bool(auth_user.enabled),
                "role": "admin" if is_admin else "user",
                "is_admin": is_admin,
                "status": status,
                "sessions_count": int(session_info.get("sessions_count", 0)),
                "rounds_count": int(round_info.get("rounds_count", 0)),
                "running_rounds": running_rounds,
                "total_tokens": int(token_info.get("total_tokens", 0)),
                "weekly_tokens_used": int(token_info.get("weekly_tokens", 0)),
                "monthly_tokens_used": int(token_info.get("monthly_tokens", 0)),
                "token_limit_per_week": auth_user.token_limit_per_week,
                "token_limit_per_month": auth_user.token_limit_per_month,
                "cron_jobs_total": int(cron_info.get("cron_jobs_total", 0)),
                "cron_jobs_enabled": int(cron_info.get("cron_jobs_enabled", 0)),
                "cron_failed_24h": int(cron_failed_map.get(user_id, 0)),
                "last_active_at": _iso(last_active),
                "last_login_at": _iso(auth_user.last_login_at),
                "last_login_ip": latest_login_map.get(user_id, {}).get("ip_address"),
                "sandbox_profile_id": sandbox_info["sandbox_profile_id"],
                "sandbox_profile_name": sandbox_info["sandbox_profile_name"],
                "sandbox_profile_source": sandbox_info["sandbox_profile_source"],
                "sandbox_profile_error": sandbox_info["sandbox_profile_error"],
                "sandbox_id": sandbox_info["sandbox_id"],
                "sandbox_status": sandbox_info["sandbox_status"],
                "sandbox_needs_recreate": sandbox_info["sandbox_needs_recreate"],
                "model_permission_group_ids": model_groups["group_ids"],
                "model_permission_group_names": model_groups["group_names"],
                "model_permission_default_group_name": model_groups["default_group"]["name"],
                "created_by": auth_user.created_by,
                "created_at": _iso(auth_user.created_at),
                "updated_at": _iso(auth_user.updated_at),
            }
        )

    return {
        "summary": {
            "users_total": len(users),
            "admins_total": len([item for item in users if item["is_admin"]]),
            "active_total": len([item for item in users if item["status"] in {"active", "running"}]),
            "running_total": len([item for item in users if item["status"] == "running"]),
        },
        "users": users,
    }


_USER_EXPORT_FIELDS = (
    "user_id",
    "username",
    "auth_type",
    "enabled",
    "role",
    "status",
    "sessions_count",
    "rounds_count",
    "running_rounds",
    "total_tokens",
    "weekly_tokens_used",
    "monthly_tokens_used",
    "token_limit_per_week",
    "token_limit_per_month",
    "sandbox_profile_name",
    "sandbox_profile_source",
    "sandbox_profile_error",
    "sandbox_id",
    "sandbox_status",
    "sandbox_needs_recreate",
    "last_active_at",
    "last_login_at",
    "last_login_ip",
)


def _safe_csv_value(value: Any) -> Any:
    """Prevent spreadsheet programs from evaluating user-controlled cells."""

    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + value
    return value


def _build_users_export_csv(db: DBSession, user_ids: list[str]) -> bytes:
    users = _build_users_payload(db)["users"]
    by_id = {item["user_id"]: item for item in users}
    missing = [user_id for user_id in user_ids if user_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"用户不存在: {missing[0]}")

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(_USER_EXPORT_FIELDS)
    for user_id in user_ids:
        item = by_id[user_id]
        writer.writerow(
            [_safe_csv_value(item.get(field)) for field in _USER_EXPORT_FIELDS]
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _build_user_login_events_payload(db: DBSession, *, user_id: str, limit: int) -> dict[str, Any]:
    user = db.query(AuthUser).filter(AuthUser.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    events = (
        db.query(AuthLoginEvent)
        .filter(AuthLoginEvent.user_id == user_id)
        .order_by(AuthLoginEvent.login_at.desc(), AuthLoginEvent.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "user_id": user_id,
        "events": [_auth_login_event_to_payload(event) for event in events],
    }


def _build_database_runtime_payload(db: DBSession) -> dict[str, Any]:
    """Expose DB pool and wait-state data for production stall diagnostics."""
    payload: dict[str, Any] = {"pool": get_engine_pool_diagnostics()}

    try:
        activity_rows = db.execute(text("""
            SELECT
                state,
                COALESCE(wait_event_type, 'none') AS wait_event_type,
                COALESCE(wait_event, 'none') AS wait_event,
                COUNT(*) AS count
            FROM pg_stat_activity
            WHERE datname = current_database()
            GROUP BY state, wait_event_type, wait_event
            ORDER BY count DESC
        """)).mappings().all()
        payload["activity"] = [dict(row) for row in activity_rows]

        blocked_locks = db.execute(text("""
            SELECT COUNT(*)
            FROM pg_locks l
            JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE NOT l.granted
              AND a.datname = current_database()
        """)).scalar()
        payload["blocked_locks"] = int(blocked_locks or 0)

        long_queries = db.execute(text("""
            SELECT
                pid,
                state,
                COALESCE(wait_event_type, 'none') AS wait_event_type,
                COALESCE(wait_event, 'none') AS wait_event,
                EXTRACT(EPOCH FROM (now() - query_start))::INT AS age_seconds,
                LEFT(query, 240) AS query_sample
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND state = 'active'
              AND query_start IS NOT NULL
              AND now() - query_start > interval '30 seconds'
            ORDER BY query_start ASC
            LIMIT 20
        """)).mappings().all()
        payload["long_queries"] = [dict(row) for row in long_queries]
    except Exception as exc:
        logger.warning("构建数据库运行态诊断失败", exc_info=True)
        payload["error"] = f"{type(exc).__name__}: {exc}"
        try:
            db.rollback()
        except Exception:
            pass

    return payload


def _build_system_payload(db: DBSession, hours: int) -> dict[str, Any]:
    now = now_naive()
    since = now - timedelta(hours=hours)

    round_status_rows = (
        db.query(Round.status, func.count(Round.id))
        .filter(Round.created_at >= since)
        .group_by(Round.status)
        .all()
    )
    round_status_counts = {status: int(count) for status, count in round_status_rows}

    cron_status_rows = (
        db.query(CronJobRun.status, func.count(CronJobRun.id))
        .filter(CronJobRun.started_at >= since)
        .group_by(CronJobRun.status)
        .all()
    )
    cron_status_counts = {status: int(count) for status, count in cron_status_rows}

    completion_latency_values = [
        float(value)
        for (value,) in (
            db.query(LLMCallRecord.completion_latency_s)
            .filter(
                LLMCallRecord.created_at >= since,
                LLMCallRecord.completion_latency_s.isnot(None),
            )
            .all()
        )
    ]
    first_token_latency_values = [
        float(value)
        for (value,) in (
            db.query(LLMCallRecord.first_token_latency_s)
            .filter(
                LLMCallRecord.created_at >= since,
                LLMCallRecord.first_token_latency_s.isnot(None),
            )
            .all()
        )
    ]

    compaction_agg = (
        db.query(
            func.count(LLMCallRecord.id).label("llm_calls"),
            func.coalesce(
                func.sum(case((LLMCallRecord.call_kind == "compaction", 1), else_=0)),
                0,
            ).label("compaction_calls"),
            func.coalesce(func.sum(LLMCallRecord.compaction_tokens_saved), 0).label("tokens_saved"),
            func.coalesce(
                func.sum(LLMCallRecord.compaction_summary_quality_repair_count),
                0,
            ).label("quality_repairs"),
            func.coalesce(
                func.sum(LLMCallRecord.compaction_emergency_truncate_dropped_rounds),
                0,
            ).label("emergency_drops"),
            func.coalesce(
                func.sum(case((LLMCallRecord.response_error.isnot(None), 1), else_=0)),
                0,
            ).label("response_errors"),
        )
        .filter(LLMCallRecord.created_at >= since)
        .one()
    )

    running_rounds = db.query(func.count(Round.id)).filter(Round.status == "running").scalar()
    active_sessions = (
        db.query(func.count(Session.id))
        .filter(Session.updated_at >= now - timedelta(minutes=30))
        .scalar()
    )

    return {
        "window_hours": hours,
        "summary": {
            "running_rounds": int(running_rounds),
            "active_sessions_30m": int(active_sessions),
            "round_status_counts": round_status_counts,
            "cron_status_counts": cron_status_counts,
            "avg_completion_latency_s": round(sum(completion_latency_values) / len(completion_latency_values), 3)
            if completion_latency_values
            else None,
            "p50_completion_latency_s": _percentile(completion_latency_values, 0.5),
            "p95_completion_latency_s": _percentile(completion_latency_values, 0.95),
            "avg_first_token_latency_s": round(sum(first_token_latency_values) / len(first_token_latency_values), 3)
            if first_token_latency_values
            else None,
            "llm_calls": int(compaction_agg.llm_calls),
            "compaction_calls": int(compaction_agg.compaction_calls),
            "compaction_tokens_saved": int(compaction_agg.tokens_saved),
            "compaction_quality_repairs": int(compaction_agg.quality_repairs),
            "compaction_emergency_drops": int(compaction_agg.emergency_drops),
            "llm_response_errors": int(compaction_agg.response_errors),
        },
        "database": _build_database_runtime_payload(db),
    }


@router.get("/overview")
@admin_audit_action("overview.read", target_type="overview")
async def get_admin_overview(
    days: int = Query(7, ge=1, le=90),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """管理端概览。"""
    return _build_overview_payload(db, days)

@router.get("/rounds-tree")
@admin_audit_action(
    "session.list",
    target_type="session_collection",
    query_action_param="search",
    query_action="session.search",
)
async def get_admin_rounds_tree(
    request: Request,
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str = Query(
        "all",
        description="all|running|completed|failed|interrupted|resumed|cancelled|max_steps_reached",
    ),
    user_id: str | None = Query(None),
    search: str | None = Query(None),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """按 Session 聚合的 rounds 监控树，含 round 内 step 级 LLM 调用明细。"""
    if search:
        # Classify the request before the data query so a downstream failure
        # is still recorded as a search rather than a plain list operation.
        enrich_admin_audit(request, action="session.search")
    result = _build_rounds_tree_payload(
        db,
        limit=limit,
        offset=offset,
        status=status,
        user_id=user_id,
        search=search,
    )
    enrich_admin_audit(
        request,
        target_user_id=user_id,
        details={
            "status": status,
            "has_search": bool(search),
            "limit": limit,
            "offset": offset,
            "returned_count": len(result["sessions"]),
        },
    )
    return result


@router.get("/sessions/{session_id}/rounds")
@admin_audit_action(
    "session.view",
    target_type="session",
    target_param="session_id",
    session_param="session_id",
)
async def get_admin_session_rounds(
    request: Request,
    session_id: str,
    status: str = Query(
        "all",
        description="all|running|completed|failed|interrupted|resumed|cancelled|max_steps_reached",
    ),
    search: str | None = Query(None),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """懒加载单个 Session 下的 Round 和 step 级轻量明细。"""
    result = _build_session_rounds_payload(
        db,
        session_id=session_id,
        status=status,
        search=search,
    )
    owner_user_id = db.query(Session.user_id).filter(Session.id == session_id).scalar()
    enrich_admin_audit(
        request,
        target_user_id=owner_user_id,
        session_id=session_id,
        details={
            "status": status,
            "has_search": bool(search),
            "returned_count": len(result["rounds"]),
        },
    )
    return result


@router.put("/llm-call-records/{llm_record_id}/review")
@admin_audit_action(
    "step.review.update",
    target_type="step",
    target_param="llm_record_id",
    step_param="llm_record_id",
)
async def update_admin_llm_call_review(
    request: Request,
    llm_record_id: int,
    payload: ManualReviewUpdatePayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """更新 step 级 LLM 调用记录的人工审阅状态。"""
    audit_row = (
        db.query(
            LLMCallRecord.manual_review_status,
            LLMCallRecord.session_id,
            Session.user_id,
        )
        .join(Session, Session.id == LLMCallRecord.session_id)
        .filter(LLMCallRecord.id == llm_record_id)
        .first()
    )
    result = _update_llm_record_review_status(
        db,
        llm_record_id=llm_record_id,
        manual_review_status=payload.manual_review_status,
    )
    if audit_row:
        enrich_admin_audit(
            request,
            target_user_id=audit_row.user_id,
            session_id=audit_row.session_id,
            step_record_id=llm_record_id,
            changed_fields={
                "manual_review_status": {
                    "before": audit_row.manual_review_status,
                    "after": payload.manual_review_status,
                }
            },
        )
    return result


@router.get("/llm-call-records/{llm_record_id}")
@admin_audit_action(
    "step.view",
    target_type="step",
    target_param="llm_record_id",
    step_param="llm_record_id",
)
async def get_admin_llm_call_record_detail(
    request: Request,
    llm_record_id: int,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """获取单条 step 级 LLM 调用详情（按需加载）。"""
    result = _build_llm_record_detail_payload(db, llm_record_id)
    audit_row = (
        db.query(LLMCallRecord.session_id, Session.user_id)
        .join(Session, Session.id == LLMCallRecord.session_id)
        .filter(LLMCallRecord.id == llm_record_id)
        .first()
    )
    if audit_row:
        enrich_admin_audit(
            request,
            target_user_id=audit_row.user_id,
            session_id=audit_row.session_id,
            step_record_id=llm_record_id,
        )
    return result


@router.get("/users")
@admin_audit_action("user.list", target_type="user_collection")
async def get_admin_users(
    request: Request,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """管理端用户列表与角色信息。"""
    result = _build_users_payload(db)
    enrich_admin_audit(request, details={"returned_count": len(result["users"])})
    return result


@router.post("/users/export")
@admin_audit_action("user.export", target_type="user_collection")
async def export_admin_users(
    request: Request,
    payload: AdminUserExportPayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """由后端重新查询并导出当前管理端选中的用户。"""
    content = _build_users_export_csv(db, payload.user_ids)
    enrich_admin_audit(request, details={"exported_count": len(payload.user_ids)})
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="opencapybox-users.csv"'},
    )


@router.get("/sandbox-profiles")
@admin_audit_action("sandbox.list", target_type="sandbox_collection")
async def get_admin_sandbox_profiles(
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """列出管理员注册的 OpenSandbox 后端配置（不做实时状态查询）。"""
    return _sandbox_profile_list_payload(db)


@router.post("/sandbox-profiles")
@admin_audit_action("sandbox.create", target_type="sandbox")
async def create_admin_sandbox_profile(
    request: Request,
    payload: AdminSandboxProfilePayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """注册一个新的 OpenSandbox 后端。"""
    profile = _create_sandbox_profile(db, payload)
    result = sandbox_profile_to_payload(profile, bound_users=0)
    changed_fields = payload.model_dump(exclude={"api_key"})
    changed_fields["api_key_changed"] = True
    enrich_admin_audit(
        request,
        target_id=profile.id,
        changed_fields=changed_fields,
        details={"api_key_changed": True},
    )
    return result


@router.patch("/sandbox-profiles/{profile_id}")
@admin_audit_action(
    "sandbox.update",
    target_type="sandbox",
    target_param="profile_id",
)
async def update_admin_sandbox_profile(
    request: Request,
    profile_id: str,
    payload: AdminSandboxProfilePatchPayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """更新 OpenSandbox 后端配置；运行参数变更会递增 version。"""
    profile = _update_sandbox_profile(db, profile_id, payload)
    counts = _sandbox_profile_bound_counts(db)
    changed_fields = payload.model_dump(exclude_unset=True, exclude={"api_key"})
    if "api_key" in payload.model_fields_set:
        changed_fields["api_key_changed"] = bool(payload.api_key)
    enrich_admin_audit(
        request,
        changed_fields=changed_fields,
        details={"api_key_changed": bool(payload.api_key)}
        if "api_key" in payload.model_fields_set
        else None,
    )
    return sandbox_profile_to_payload(profile, bound_users=counts.get(profile.id, 0))


@router.patch("/sandbox-profiles/{profile_id}/default")
@admin_audit_action(
    "sandbox.default.set",
    target_type="sandbox",
    target_param="profile_id",
)
async def set_admin_sandbox_profile_default(
    profile_id: str,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """设置全局默认 OpenSandbox 后端。"""
    profile = set_default_sandbox_profile(db, profile_id)
    counts = _sandbox_profile_bound_counts(db)
    return sandbox_profile_to_payload(profile, bound_users=counts.get(profile.id, 0))


@router.patch("/sandbox-profiles/{profile_id}/enabled")
@admin_audit_action(
    "sandbox.enabled.update",
    target_type="sandbox",
    target_param="profile_id",
)
async def set_admin_sandbox_profile_enabled(
    request: Request,
    profile_id: str,
    payload: AdminSandboxProfileEnabledPayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """启用或禁用 OpenSandbox 后端。禁用不做实时 kill。"""
    previous = db.query(SandboxProfile.enabled).filter(SandboxProfile.id == profile_id).scalar()
    profile = _set_sandbox_profile_enabled(db, profile_id, payload.enabled)
    counts = _sandbox_profile_bound_counts(db)
    enrich_admin_audit(
        request,
        changed_fields={"enabled": {"before": bool(previous), "after": bool(payload.enabled)}},
    )
    return sandbox_profile_to_payload(profile, bound_users=counts.get(profile.id, 0))


@router.patch("/users/{user_id}/sandbox-profile")
@admin_audit_action(
    "user.sandbox.update",
    target_type="user",
    target_param="user_id",
    target_user_param="user_id",
)
async def update_admin_user_sandbox_profile(
    request: Request,
    user_id: str,
    payload: AdminUserSandboxProfilePayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """分配用户所属沙箱后端；切换时清理旧 sandbox，首期不迁移文件。"""
    target_user = db.query(AuthUser).filter(AuthUser.user_id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="用户不存在")
    desired_profile_id = _normalize_profile_assignment(db, payload.sandbox_profile_id)
    current_profile_id = _get_user_assigned_profile_id(db, user_id)
    assignment_changed = current_profile_id != desired_profile_id
    active_profile_stale = _user_sandbox_profile_stale(
        db,
        user_id=user_id,
        desired_profile_id=desired_profile_id,
    )
    needs_recreate = bool(payload.force_recreate or assignment_changed or active_profile_stale)
    enrich_admin_audit(
        request,
        target_user_id=user_id,
        changed_fields={
            "sandbox_profile_id": {
                "before": current_profile_id,
                "after": desired_profile_id,
            },
            "force_recreate": bool(payload.force_recreate),
        },
    )
    if not needs_recreate:
        return get_user_sandbox_config_payload(db, user_id)

    if _user_has_running_work(db, user_id) and not payload.force_recreate:
        raise HTTPException(status_code=409, detail="用户当前有正在运行的任务，无法切换沙箱后端")

    sandbox_service = SandboxSessionService()
    await get_agent_pool().invalidate_user_async(user_id, preserve_running=False)

    await _clear_user_sandbox_binding_after_kill(
        db,
        user_id=user_id,
        sandbox_service=sandbox_service,
    )
    assign_user_sandbox_profile(
        db,
        user_id=user_id,
        sandbox_profile_id=desired_profile_id,
        updated_by=admin_user_id,
    )
    return get_user_sandbox_config_payload(db, user_id)


@router.get("/users/{user_id}/login-events")
@admin_audit_action(
    "user.login_history.view",
    target_type="user",
    target_param="user_id",
    target_user_param="user_id",
)
async def get_admin_user_login_events(
    request: Request,
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """查看指定用户的登录审计历史。"""
    result = _build_user_login_events_payload(db, user_id=user_id, limit=limit)
    enrich_admin_audit(request, details={"returned_count": len(result["events"])})
    return result


@router.get("/models")
@admin_audit_action("model.list", target_type="model_collection")
async def get_admin_models(
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """列出 DB 模型目录和默认模型设置。"""
    get_or_create_default_group(db)
    return _build_admin_models_payload(db)


@router.post("/models")
@admin_audit_action("model.create", target_type="model")
async def create_admin_model(
    request: Request,
    payload: AdminModelPayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """新增一个模型到 DB 模型目录。"""
    result = _create_admin_model(db, payload)
    await get_agent_pool().invalidate_all_async()
    changed_fields = payload.model_dump(exclude={"api_key"})
    changed_fields["api_key_changed"] = True
    enrich_admin_audit(
        request,
        target_id=payload.model_id,
        changed_fields=changed_fields,
        details={"api_key_changed": True},
    )
    return result


@router.patch("/models/settings")
@admin_audit_action("model.settings.update", target_type="model_settings")
async def update_admin_model_settings(
    request: Request,
    payload: AdminModelSettingsPayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """更新普通/Cron/Subagent 默认模型。"""
    result = _update_model_settings(db, payload)
    enrich_admin_audit(request, changed_fields=payload.model_dump())
    return result


@router.patch("/models/{model_id}")
@admin_audit_action("model.update", target_type="model", target_param="model_id")
async def update_admin_model(
    request: Request,
    model_id: str,
    payload: AdminModelPatchPayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """更新模型配置；api_key 留空表示不修改。"""
    result = _update_admin_model(db, model_id, payload)
    await get_agent_pool().invalidate_all_async()
    changed_fields = payload.model_dump(exclude_unset=True, exclude={"api_key"})
    if "api_key" in payload.model_fields_set:
        changed_fields["api_key_changed"] = bool(payload.api_key)
    enrich_admin_audit(
        request,
        changed_fields=changed_fields,
        details={"api_key_changed": bool(payload.api_key)}
        if "api_key" in payload.model_fields_set
        else None,
    )
    return result


@router.delete("/models/{model_id}")
@admin_audit_action("model.delete", target_type="model", target_param="model_id")
async def delete_admin_model(
    request: Request,
    model_id: str,
    replacement_model_id: str | None = Query(None),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """从 DB 模型目录删除未被默认配置或历史会话引用的模型。"""
    result = _delete_admin_model(db, model_id, replacement_model_id=replacement_model_id)
    await get_agent_pool().invalidate_all_async()
    enrich_admin_audit(
        request,
        details={"has_replacement_model": bool(replacement_model_id)},
    )
    return result


@router.get("/model-permission-groups")
@admin_audit_action("model_group.list", target_type="model_group_collection")
async def get_admin_model_permission_groups(
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """列出模型权限包。"""
    return list_permission_groups_payload(db)


@router.post("/model-permission-groups")
@admin_audit_action("model_group.create", target_type="model_group")
async def create_admin_model_permission_group(
    request: Request,
    payload: AdminPermissionGroupPayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """创建业务模型权限包。"""
    result = _create_permission_group(db, payload, admin_user_id)
    enrich_admin_audit(
        request,
        target_id=result.get("id"),
        changed_fields=payload.model_dump(),
    )
    return result


@router.patch("/model-permission-groups/{group_id}")
@admin_audit_action(
    "model_group.update",
    target_type="model_group",
    target_param="group_id",
)
async def update_admin_model_permission_group(
    request: Request,
    group_id: str,
    payload: AdminPermissionGroupPatchPayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """更新模型权限包名称或描述。默认包不能重命名。"""
    result = _update_permission_group(db, group_id, payload)
    enrich_admin_audit(request, changed_fields=payload.model_dump(exclude_unset=True))
    return result


@router.put("/model-permission-groups/{group_id}/models")
@admin_audit_action(
    "model_group.models.update",
    target_type="model_group",
    target_param="group_id",
)
async def update_admin_model_permission_group_models(
    request: Request,
    group_id: str,
    payload: AdminModelIdsPayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """设置某个权限包包含的模型。"""
    result = set_group_models(
        db,
        group_id=group_id,
        model_ids=payload.model_ids,
        admin_user_id=admin_user_id,
    )
    enrich_admin_audit(request, changed_fields={"model_count": len(payload.model_ids)})
    return result


@router.put("/model-permission-groups/{group_id}/users")
@admin_audit_action(
    "model_group.users.update",
    target_type="model_group",
    target_param="group_id",
)
async def update_admin_model_permission_group_users(
    request: Request,
    group_id: str,
    payload: AdminUserIdsPayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """从权限包视角设置绑定用户。默认权限包自动应用全体用户，不能手动绑定。"""
    group = db.query(ModelPermissionGroup).filter(ModelPermissionGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="模型权限包不存在")
    if group.is_default:
        raise HTTPException(status_code=400, detail="默认权限包自动应用给所有用户，不能手动绑定")
    normalized = list(dict.fromkeys(uid.strip() for uid in payload.user_ids if uid and uid.strip()))
    existing_users = {
        row[0]
        for row in db.query(AuthUser.user_id).filter(AuthUser.user_id.in_(normalized)).all()
    } if normalized else set()
    missing = [uid for uid in normalized if uid not in existing_users]
    if missing:
        raise HTTPException(status_code=400, detail=f"用户不存在: {missing}")
    db.query(UserModelPermissionGroup).filter(UserModelPermissionGroup.group_id == group.id).delete(synchronize_session=False)
    for user_id in normalized:
        db.add(UserModelPermissionGroup(user_id=user_id, group_id=group.id, created_by=admin_user_id))
    group.updated_at = now_naive()
    db.commit()
    db.refresh(group)
    enrich_admin_audit(request, changed_fields={"user_count": len(normalized)})
    return group_to_payload(db, group)


@router.post("/users/simple")
@admin_audit_action("user.create", target_type="user")
async def create_admin_simple_user(
    request: Request,
    payload: AdminCreateSimpleUserPayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """创建本地 simple 用户。"""
    sandbox_profile_id = _normalize_profile_assignment(db, payload.sandbox_profile_id)
    user = create_simple_user(
        db,
        username=payload.username,
        password=payload.password,
        enabled=payload.enabled,
        is_admin=payload.is_admin,
        token_limit_per_week=payload.token_limit_per_week,
        token_limit_per_month=payload.token_limit_per_month,
        created_by=admin_user_id,
    )
    if sandbox_profile_id:
        assign_user_sandbox_profile(
            db,
            user_id=user.user_id,
            sandbox_profile_id=sandbox_profile_id,
            updated_by=admin_user_id,
        )
    result = auth_user_to_payload(user)
    created_user_id = result.get("user_id")
    changed_fields = payload.model_dump(exclude={"password"})
    changed_fields["password_changed"] = True
    enrich_admin_audit(
        request,
        target_id=created_user_id,
        target_user_id=created_user_id,
        changed_fields=changed_fields,
        details={"password_changed": True},
    )
    return result


@router.post("/users/ldap")
@admin_audit_action("user.create", target_type="user")
async def create_admin_ldap_user(
    request: Request,
    payload: AdminCreateLdapUserPayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """创建 LDAP 目录账号用户。"""
    sandbox_profile_id = _normalize_profile_assignment(db, payload.sandbox_profile_id)
    user = create_ldap_user(
        db,
        user_id=payload.user_id,
        username=payload.username,
        enabled=payload.enabled,
        is_admin=payload.is_admin,
        token_limit_per_week=payload.token_limit_per_week,
        token_limit_per_month=payload.token_limit_per_month,
        created_by=admin_user_id,
    )
    if sandbox_profile_id:
        assign_user_sandbox_profile(
            db,
            user_id=user.user_id,
            sandbox_profile_id=sandbox_profile_id,
            updated_by=admin_user_id,
        )
    result = auth_user_to_payload(user)
    created_user_id = result.get("user_id")
    enrich_admin_audit(
        request,
        target_id=created_user_id,
        target_user_id=created_user_id,
        changed_fields=payload.model_dump(),
    )
    return result


@router.patch("/users/{user_id}/enabled")
@admin_audit_action(
    "user.enabled.update",
    target_type="user",
    target_param="user_id",
    target_user_param="user_id",
)
async def update_admin_user_enabled(
    request: Request,
    user_id: str,
    payload: AdminEnabledUpdatePayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """启用或禁用用户。"""
    if user_id == admin_user_id and not payload.enabled:
        raise HTTPException(status_code=400, detail="不能禁用当前管理员账号")
    previous = db.query(AuthUser.enabled).filter(AuthUser.user_id == user_id).scalar()
    user = update_user_enabled(db, user_id=user_id, enabled=payload.enabled)
    enrich_admin_audit(
        request,
        changed_fields={"enabled": {"before": bool(previous), "after": bool(payload.enabled)}},
    )
    return auth_user_to_payload(user)


@router.patch("/users/{user_id}/admin")
@admin_audit_action(
    "user.admin.update",
    target_type="user",
    target_param="user_id",
    target_user_param="user_id",
)
async def update_admin_user_admin_flag(
    request: Request,
    user_id: str,
    payload: AdminFlagUpdatePayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """设置或取消管理员权限。"""
    if user_id == admin_user_id and not payload.is_admin:
        raise HTTPException(status_code=400, detail="不能取消自己的管理员权限")
    previous = db.query(AuthUser.is_admin).filter(AuthUser.user_id == user_id).scalar()
    user = update_user_admin(db, user_id=user_id, is_admin=payload.is_admin)
    enrich_admin_audit(
        request,
        changed_fields={"is_admin": {"before": bool(previous), "after": bool(payload.is_admin)}},
    )
    return auth_user_to_payload(user)


@router.patch("/users/{user_id}/token-limits")
@admin_audit_action(
    "user.token_limits.update",
    target_type="user",
    target_param="user_id",
    target_user_param="user_id",
)
async def update_admin_user_token_limits(
    request: Request,
    user_id: str,
    payload: AdminTokenLimitsUpdatePayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """更新用户 token 周/月限额。"""
    previous = (
        db.query(AuthUser.token_limit_per_week, AuthUser.token_limit_per_month)
        .filter(AuthUser.user_id == user_id)
        .first()
    )
    user = update_user_token_limits(
        db,
        user_id=user_id,
        token_limit_per_week=payload.token_limit_per_week,
        token_limit_per_month=payload.token_limit_per_month,
    )
    if previous:
        enrich_admin_audit(
            request,
            changed_fields={
                "token_limit_per_week": {
                    "before": previous.token_limit_per_week,
                    "after": payload.token_limit_per_week,
                },
                "token_limit_per_month": {
                    "before": previous.token_limit_per_month,
                    "after": payload.token_limit_per_month,
                },
            },
        )
    return auth_user_to_payload(user)


@router.put("/users/{user_id}/model-permission-groups")
@admin_audit_action(
    "user.model_groups.update",
    target_type="user",
    target_param="user_id",
    target_user_param="user_id",
)
async def update_admin_user_model_permission_groups(
    request: Request,
    user_id: str,
    payload: AdminGroupIdsPayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """设置用户额外绑定的模型权限包。默认包自动应用，不在这里保存。"""
    result = set_user_groups(
        db,
        user_id=user_id,
        group_ids=payload.group_ids,
        admin_user_id=admin_user_id,
    )
    enrich_admin_audit(request, changed_fields={"group_count": len(payload.group_ids)})
    return result


@router.post("/users/{user_id}/reset-password")
@admin_audit_action(
    "user.password.reset",
    target_type="user",
    target_param="user_id",
    target_user_param="user_id",
)
async def reset_admin_simple_user_password(
    request: Request,
    user_id: str,
    payload: AdminResetPasswordPayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """重置 simple 用户密码。"""
    user = reset_simple_user_password(db, user_id=user_id, password=payload.password)
    enrich_admin_audit(
        request,
        changed_fields={"password_changed": True},
        details={"password_changed": True},
    )
    return auth_user_to_payload(user)


@router.delete("/users/{user_id}")
@admin_audit_action(
    "user.delete",
    target_type="user",
    target_param="user_id",
    target_user_param="user_id",
)
async def delete_admin_auth_user(
    user_id: str,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """硬删除认证用户账号及其用户态数据。"""
    if user_id == admin_user_id:
        raise HTTPException(status_code=400, detail="不能删除当前管理员账号")

    target_user = db.query(AuthUser).filter(AuthUser.user_id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="用户不存在")

    lock_cutoff = now_naive() - timedelta(seconds=get_settings().sse_subscribe_timeout)
    active_lock = (
        db.query(UserRunLock)
        .filter(UserRunLock.user_id == user_id, UserRunLock.updated_at >= lock_cutoff)
        .first()
    )
    if active_lock:
        raise HTTPException(status_code=409, detail="用户当前有正在运行的任务，无法删除")

    running_cron = (
        db.query(CronJobRun)
        .filter(CronJobRun.user_id == user_id, CronJobRun.status == "running")
        .first()
    )
    if running_cron:
        raise HTTPException(status_code=409, detail="用户当前有正在运行的定时任务，无法删除")

    await get_agent_pool().invalidate_user_async(user_id, preserve_running=False)

    sandbox_service = SandboxSessionService()
    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    sandbox_id = user_sandbox.sandbox_id if user_sandbox else None
    if sandbox_id or sandbox_service.get_cached(user_id):
        sandbox_deleted = await sandbox_service.kill(user_id, sandbox_id)
        if not sandbox_deleted:
            raise HTTPException(status_code=409, detail="沙箱清理失败，用户未删除")

    deleted_user_id = delete_auth_user(db, user_id=user_id)
    return {"user_id": deleted_user_id, "deleted": True}


@router.get("/system")
@admin_audit_action("system.read", target_type="system")
async def get_admin_system(
    hours: int = Query(24, ge=1, le=168),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """管理端系统监控聚合指标。"""
    return _build_system_payload(db, hours)
