"""Sandbox profile resolution and admin helpers."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.api.models.sandbox_profile import SandboxProfile
from src.api.models.user_sandbox import UserSandbox
from src.api.models.user_sandbox_config import UserSandboxConfig
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)
_DEFAULT_PROFILE_BOOTSTRAP_LOCK = "ensure_default_sandbox_profile"


@dataclass(frozen=True)
class SandboxRuntimeConfig:
    profile_id: str
    profile_name: str
    profile_version: int
    profile_source: str
    domain: str
    protocol: str
    api_key: str
    use_server_proxy: bool
    mount_path: str


RUNTIME_RECREATE_FIELDS = {
    "domain",
    "protocol",
    "api_key",
    "use_server_proxy",
}


def _profile_from_settings() -> SandboxProfile:
    from src.api.config import get_settings

    settings = get_settings()
    if not str(settings.sandbox_api_key or "").strip():
        logger.warning("SANDBOX_API_KEY 未配置，默认 Sandbox Profile 将缺少 OpenSandbox API Key")
    return SandboxProfile(
        id=str(uuid.uuid4()),
        name="默认沙箱",
        description="由当前 .env OpenSandbox 配置自动创建",
        department="默认",
        domain=settings.sandbox_domain,
        protocol=settings.sandbox_protocol,
        api_key=settings.sandbox_api_key,
        use_server_proxy=bool(settings.sandbox_use_server_proxy),
        is_default=True,
        enabled=True,
        version=1,
    )


def _lock_default_profile_bootstrap(db: DBSession) -> None:
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text(f"SELECT pg_advisory_xact_lock(hashtext('{_DEFAULT_PROFILE_BOOTSTRAP_LOCK}'))"))


def _normalize_default_sandbox_profiles(db: DBSession) -> SandboxProfile | None:
    defaults = (
        db.query(SandboxProfile)
        .filter(SandboxProfile.is_default.is_(True))
        .order_by(SandboxProfile.created_at.asc(), SandboxProfile.id.asc())
        .all()
    )
    if not defaults:
        return None

    default_profile = defaults[0]
    changed = False
    if not default_profile.enabled:
        default_profile.enabled = True
        changed = True
    for extra_profile in defaults[1:]:
        extra_profile.is_default = False
        changed = True

    if changed:
        default_profile.updated_at = now_naive()
        db.commit()
        db.refresh(default_profile)
    return default_profile


def get_existing_default_sandbox_profile(db: DBSession) -> SandboxProfile | None:
    """Read the current default profile without bootstrap locks or writes.

    Admin read endpoints call this while rendering list data. They must not take
    the bootstrap advisory lock because the frontend loads users and sandbox
    profiles concurrently; a blocking synchronous DB wait in an async route can
    freeze the whole uvicorn event loop.
    """
    return (
        db.query(SandboxProfile)
        .filter(SandboxProfile.is_default.is_(True))
        .order_by(SandboxProfile.created_at.asc(), SandboxProfile.id.asc())
        .first()
    )


def ensure_default_sandbox_profile(db: DBSession) -> SandboxProfile:
    """Ensure exactly one default profile exists for upgraded deployments."""
    _lock_default_profile_bootstrap(db)

    existing_default = _normalize_default_sandbox_profiles(db)
    if existing_default:
        # pg_advisory_xact_lock is transaction-scoped. Returning an unchanged
        # existing profile without ending the transaction leaves the bootstrap
        # lock held while callers may perform slow OpenSandbox network I/O,
        # blocking every later runtime-config request in the process.
        db.commit()
        db.refresh(existing_default)
        return existing_default

    first_profile = db.query(SandboxProfile).order_by(SandboxProfile.created_at.asc()).first()
    if first_profile:
        first_profile.is_default = True
        first_profile.enabled = True
        db.commit()
        db.refresh(first_profile)
        return first_profile

    profile = _profile_from_settings()
    db.add(profile)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _lock_default_profile_bootstrap(db)
        existing_default = _normalize_default_sandbox_profiles(db)
        if existing_default:
            return existing_default
        profile_by_name = db.query(SandboxProfile).filter(SandboxProfile.name == profile.name).first()
        if profile_by_name:
            profile_by_name.is_default = True
            profile_by_name.enabled = True
            profile_by_name.updated_at = now_naive()
            db.commit()
            db.refresh(profile_by_name)
            return profile_by_name
        raise
    db.refresh(profile)
    return profile


def sandbox_profile_to_payload(profile: SandboxProfile, *, bound_users: int = 0) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "department": profile.department,
        "domain": profile.domain,
        "protocol": profile.protocol,
        "api_key_set": bool(profile.api_key),
        "use_server_proxy": bool(profile.use_server_proxy),
        "is_default": bool(profile.is_default),
        "enabled": bool(profile.enabled),
        "version": int(profile.version or 1),
        "bound_users": int(bound_users),
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


def get_default_sandbox_profile(db: DBSession) -> SandboxProfile:
    return ensure_default_sandbox_profile(db)


def get_effective_sandbox_profile(db: DBSession, user_id: str) -> tuple[SandboxProfile, str]:
    config = (
        db.query(UserSandboxConfig)
        .filter(UserSandboxConfig.user_id == user_id)
        .first()
    )
    if config and config.sandbox_profile_id:
        profile = db.query(SandboxProfile).filter(SandboxProfile.id == config.sandbox_profile_id).first()
        if not profile:
            raise HTTPException(status_code=409, detail="用户绑定的沙箱后端不存在")
        if not profile.enabled:
            raise HTTPException(status_code=409, detail="用户绑定的沙箱后端已禁用")
        return profile, "explicit"

    # Startup guarantees a default profile in healthy deployments. Normal
    # request paths use a lock-free read so resolving runtime configuration
    # cannot queue the whole event loop behind the bootstrap advisory lock.
    profile = get_existing_default_sandbox_profile(db)
    if profile is None:
        profile = ensure_default_sandbox_profile(db)
    if not profile.enabled:
        raise HTTPException(status_code=409, detail="默认沙箱后端已禁用")
    return profile, "default"


def runtime_config_from_profile(profile: SandboxProfile, source: str) -> SandboxRuntimeConfig:
    from src.api.config import get_settings

    settings = get_settings()
    return SandboxRuntimeConfig(
        profile_id=profile.id,
        profile_name=profile.name,
        profile_version=int(profile.version or 1),
        profile_source=source,
        domain=profile.domain,
        protocol=profile.protocol,
        api_key=profile.api_key or "",
        use_server_proxy=bool(profile.use_server_proxy),
        mount_path=settings.sandbox_storage_mount_path,
    )


def resolve_sandbox_runtime_config(db: DBSession, user_id: str) -> SandboxRuntimeConfig:
    profile, source = get_effective_sandbox_profile(db, user_id)
    return runtime_config_from_profile(profile, source)


def get_user_sandbox_config_payload(db: DBSession, user_id: str) -> dict:
    desired_config = (
        db.query(UserSandboxConfig)
        .filter(UserSandboxConfig.user_id == user_id)
        .first()
    )
    configured_profile_id = desired_config.sandbox_profile_id if desired_config and desired_config.sandbox_profile_id else None
    profile: SandboxProfile | None = None
    source = "default"
    profile_error: str | None = None
    if configured_profile_id:
        profile = db.query(SandboxProfile).filter(SandboxProfile.id == configured_profile_id).first()
        if profile is None:
            source = "missing"
            profile_error = "用户绑定的沙箱后端不存在"
        else:
            source = "explicit"
            if not profile.enabled:
                source = "disabled"
                profile_error = "用户绑定的沙箱后端已禁用"
    if profile is None and profile_error is None:
        profile = get_existing_default_sandbox_profile(db)
        source = "default"
        if profile is None:
            source = "missing"
            profile_error = "默认沙箱后端不存在"
        elif not profile.enabled:
            source = "disabled"
            profile_error = "默认沙箱后端已禁用"
    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    needs_recreate = bool(
        user_sandbox
        and user_sandbox.sandbox_id
        and (
            profile is None
            or profile_error is not None
            or user_sandbox.active_profile_id != profile.id
            or int(user_sandbox.active_profile_version or 0) != int(profile.version or 1)
        )
    )
    return {
        "sandbox_profile_id": configured_profile_id,
        "sandbox_profile_name": profile.name if profile else None,
        "sandbox_profile_source": source,
        "sandbox_profile_error": profile_error,
        "sandbox_id": user_sandbox.sandbox_id if user_sandbox else None,
        "sandbox_status": user_sandbox.status if user_sandbox else "none",
        "sandbox_active_profile_id": user_sandbox.active_profile_id if user_sandbox else None,
        "sandbox_active_profile_version": user_sandbox.active_profile_version if user_sandbox else None,
        "sandbox_desired_profile_id": profile.id if profile else configured_profile_id,
        "sandbox_desired_profile_version": int(profile.version or 1) if profile else None,
        "sandbox_needs_recreate": needs_recreate,
    }


def set_default_sandbox_profile(db: DBSession, profile_id: str) -> SandboxProfile:
    _lock_default_profile_bootstrap(db)
    profile = db.query(SandboxProfile).filter(SandboxProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="沙箱后端不存在")
    if not profile.enabled:
        raise HTTPException(status_code=400, detail="禁用的沙箱后端不能设为默认")

    changed_defaults = db.query(SandboxProfile).filter(
        SandboxProfile.is_default.is_(True),
        SandboxProfile.id != profile_id,
    ).update(
        {SandboxProfile.is_default: False},
        synchronize_session=False,
    )
    if changed_defaults or not profile.is_default:
        profile.is_default = True
        profile.updated_at = now_naive()
        db.commit()
        db.refresh(profile)
    return profile


def assign_user_sandbox_profile(
    db: DBSession,
    *,
    user_id: str,
    sandbox_profile_id: str | None,
    updated_by: str,
) -> UserSandboxConfig:
    if sandbox_profile_id:
        profile = db.query(SandboxProfile).filter(SandboxProfile.id == sandbox_profile_id).first()
        if not profile:
            raise HTTPException(status_code=404, detail="沙箱后端不存在")
        if not profile.enabled:
            raise HTTPException(status_code=400, detail="禁用的沙箱后端不能分配")
        if profile.is_default:
            sandbox_profile_id = None

    config = db.query(UserSandboxConfig).filter(UserSandboxConfig.user_id == user_id).first()
    if not config:
        config = UserSandboxConfig(id=str(uuid.uuid4()), user_id=user_id)
        db.add(config)
    config.sandbox_profile_id = sandbox_profile_id
    config.updated_by = updated_by
    config.updated_at = now_naive()
    db.commit()
    db.refresh(config)
    return config
