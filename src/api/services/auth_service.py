"""认证与用户授权服务。"""

import logging
import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import HTTPException
from ldap3 import Connection, Server
from ldap3.core.exceptions import LDAPBindError, LDAPCommunicationError
from sqlalchemy import func, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DBSession

from src.api.config import Settings, get_settings
from src.api.models.agui_event import AGUIEventLog
from src.api.models.auth_login_event import AuthLoginEvent
from src.api.models.auth_user import AuthUser
from src.api.models.channel_session_binding import ChannelSessionBinding
from src.api.models.conversation_message import ConversationMessage
from src.api.models.cron_fire import CronFire
from src.api.models.cron_job import CronJob
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.round import Round
from src.api.models.run_cancel_request import RunCancelRequest
from src.api.models.session import Session
from src.api.models.subagent_run import SubagentRun
from src.api.models.user_memory import CronJobRun, MemoryEmbedding, UserMemory, UserSkillConfig
from src.api.models.user_run_lock import UserRunLock
from src.api.models.user_sandbox import UserSandbox
from src.api.models.user_sandbox_config import UserSandboxConfig
from src.api.utils.timezone import now_naive


_PASSWORD_ALGORITHM = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 260000
_LDAP_CONNECT_TIMEOUT_SECONDS = 5
_LDAP_RECEIVE_TIMEOUT_SECONDS = 10
_SIMPLE_USERNAME_FORBIDDEN_CHARS = ("\\", "@")
_LOGIN_EVENT_IP_MAX_LENGTH = 64
logger = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PASSWORD_ITERATIONS,
    )
    return "$".join(
        [
            _PASSWORD_ALGORITHM,
            str(_PASSWORD_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored_hash: str) -> bool:
    algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)
    if algorithm != _PASSWORD_ALGORITHM:
        raise ValueError(f"unsupported password hash algorithm: {algorithm}")
    iterations = int(iterations_text)
    salt = base64.b64decode(salt_text.encode("ascii"))
    expected = base64.b64decode(digest_text.encode("ascii"))
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def normalize_domain_user(user_code: str) -> str:
    value = user_code.strip()
    if "\\" in value:
        value = value.split("\\", 1)[1].strip()
    if "@" in value:
        value = value.split("@", 1)[0].strip()
    return value


def bootstrap_auth_users(db: DBSession) -> int:
    """当 auth_users 为空时，从旧 .env 配置初始化第一批 simple 用户。

    PostgreSQL 使用 pg_advisory_xact_lock 串行化多 worker 首次初始化。
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('bootstrap_auth_users'))"))

    existing_count = db.query(func.count(AuthUser.id)).scalar() or 0
    if existing_count > 0:
        return 0

    settings = get_settings()
    auth_users = settings.get_auth_users()
    admin_users = settings.get_admin_users()

    created = 0
    for username, password in sorted(auth_users.items()):
        _validate_simple_username(username)
        user = AuthUser(
            user_id=username,
            username=username,
            auth_type="simple",
            password_hash=hash_password(password),
            enabled=True,
            is_admin=username in admin_users,
            token_limit_per_month=None,
            token_limit_per_week=None,
            created_by="bootstrap",
        )
        db.add(user)
        created += 1

    if created:
        db.commit()
    return created


def get_auth_user(db: DBSession, user_id: str) -> AuthUser | None:
    return db.query(AuthUser).filter(AuthUser.user_id == user_id).first()


def get_enabled_user(db: DBSession, user_id: str) -> AuthUser:
    user = get_auth_user(db, user_id)
    if not user or not user.enabled:
        raise HTTPException(status_code=401, detail="无效或已过期的访问令牌")
    return user


def require_admin_user(db: DBSession, user_id: str) -> AuthUser:
    user = get_enabled_user(db, user_id)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def login_user(db: DBSession, username: str, password: str) -> AuthUser:
    user_id = normalize_domain_user(username)
    user = get_auth_user(db, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.enabled:
        raise HTTPException(status_code=403, detail="账户已被禁用")

    if user.auth_type == "simple":
        if not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
    elif user.auth_type == "ldap":
        authenticate_ldap_credentials(user.user_id, password)
    else:
        raise ValueError(f"unsupported auth_type: {user.auth_type}")

    user.last_login_at = now_naive()
    db.commit()
    return user


def record_login_event(
    db: DBSession,
    *,
    user: AuthUser,
    ip_address: str | None,
    user_agent: str | None,
) -> AuthLoginEvent | None:
    """记录成功登录事件；审计失败不阻断主登录流程。"""
    user_id = user.user_id
    username = user.username
    auth_type = user.auth_type
    try:
        event = AuthLoginEvent(
            user_id=user_id,
            username=username,
            auth_type=auth_type,
            ip_address=_normalize_login_event_ip(ip_address),
            user_agent=user_agent,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event
    except SQLAlchemyError:
        db.rollback()
        logger.warning(
            "Failed to record login audit event for user_id=%s",
            user_id,
            exc_info=True,
        )
        return None


def _normalize_login_event_ip(ip_address: str | None) -> str | None:
    value = (ip_address or "").strip()
    return value[:_LOGIN_EVENT_IP_MAX_LENGTH] if value else None


def authenticate_ldap_credentials(username: str, password: str) -> None:
    if not password:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    settings = get_settings()
    ldap_urls = settings.get_ldap_urls()
    if not ldap_urls:
        raise HTTPException(status_code=503, detail="LDAP 未配置")

    bind_user = _build_ldap_bind_user(settings, username)

    for url_index, ldap_url in enumerate(ldap_urls):
        try:
            _bind_ldap(ldap_url, bind_user, password)
            return
        # ldap3 的 socket open / receive 等网络异常均继承自 LDAPCommunicationError。
        except LDAPCommunicationError:
            if url_index == len(ldap_urls) - 1:
                raise HTTPException(status_code=503, detail="LDAP 服务不可用")
        except LDAPBindError:
            raise HTTPException(status_code=401, detail="用户名或密码错误")


def _build_ldap_bind_user(settings: Settings, username: str) -> str:
    domain = settings.ldap_user_domain.strip()
    if not domain:
        return username
    return f"{username}@{domain}"


def _bind_ldap(ldap_url: str, bind_user: str, password: str) -> None:
    server = Server(ldap_url, connect_timeout=_LDAP_CONNECT_TIMEOUT_SECONDS)
    connection = Connection(
        server,
        user=bind_user,
        password=password,
        auto_bind=True,
        receive_timeout=_LDAP_RECEIVE_TIMEOUT_SECONDS,
    )
    connection.unbind()


def create_simple_user(
    db: DBSession,
    *,
    username: str,
    password: str,
    enabled: bool,
    is_admin: bool,
    token_limit_per_week: int | None,
    token_limit_per_month: int | None,
    created_by: str,
) -> AuthUser:
    _validate_simple_username(username)
    _ensure_user_not_exists(db, username=username, user_id=username)
    user = AuthUser(
        user_id=username,
        username=username,
        auth_type="simple",
        password_hash=hash_password(password),
        enabled=enabled,
        is_admin=is_admin,
        token_limit_per_week=token_limit_per_week,
        token_limit_per_month=token_limit_per_month,
        created_by=created_by,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_ldap_user(
    db: DBSession,
    *,
    user_id: str,
    username: str | None,
    enabled: bool,
    is_admin: bool,
    token_limit_per_week: int | None,
    token_limit_per_month: int | None,
    created_by: str,
) -> AuthUser:
    user_id = normalize_domain_user(user_id)
    actual_username = username or user_id
    _ensure_user_not_exists(db, username=actual_username, user_id=user_id)
    user = AuthUser(
        user_id=user_id,
        username=actual_username,
        auth_type="ldap",
        password_hash=None,
        enabled=enabled,
        is_admin=is_admin,
        token_limit_per_week=token_limit_per_week,
        token_limit_per_month=token_limit_per_month,
        created_by=created_by,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user_enabled(db: DBSession, *, user_id: str, enabled: bool) -> AuthUser:
    user = _get_existing_user(db, user_id)
    user.enabled = enabled
    if not enabled:
        user.token_generation += 1
    db.commit()
    db.refresh(user)
    return user


def update_user_admin(db: DBSession, *, user_id: str, is_admin: bool) -> AuthUser:
    user = _get_existing_user(db, user_id)
    user.is_admin = is_admin
    db.commit()
    db.refresh(user)
    return user


def update_user_token_limits(
    db: DBSession,
    *,
    user_id: str,
    token_limit_per_week: int | None,
    token_limit_per_month: int | None,
) -> AuthUser:
    user = _get_existing_user(db, user_id)
    user.token_limit_per_week = token_limit_per_week
    user.token_limit_per_month = token_limit_per_month
    db.commit()
    db.refresh(user)
    return user


def reset_simple_user_password(db: DBSession, *, user_id: str, password: str) -> AuthUser:
    user = _get_existing_user(db, user_id)
    if user.auth_type != "simple":
        raise HTTPException(status_code=400, detail="ldap 用户不能重置本地密码")
    user.password_hash = hash_password(password)
    user.token_generation += 1
    db.commit()
    db.refresh(user)
    return user


def delete_auth_user(db: DBSession, *, user_id: str) -> str:
    user = _get_existing_user(db, user_id)
    _purge_user_owned_data(db, user_id=user_id)
    db.delete(user)
    db.commit()
    return user_id


def get_user_token_usage(db: DBSession, *, user_id: str, since) -> int:
    value = (
        db.query(func.coalesce(func.sum(LLMCallRecord.usage_total_tokens), 0))
        .join(Session, Session.id == LLMCallRecord.session_id)
        .filter(Session.user_id == user_id, LLMCallRecord.created_at >= since)
        .scalar()
    )
    return int(value or 0)


def enforce_token_limits(db: DBSession, *, user_id: str) -> None:
    user = get_enabled_user(db, user_id)

    now = now_naive()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if user.token_limit_per_week is not None:
        week_used = get_user_token_usage(db, user_id=user_id, since=week_start)
        if week_used >= user.token_limit_per_week:
            raise HTTPException(status_code=429, detail="本周 token 使用量已达上限")

    if user.token_limit_per_month is not None:
        month_used = get_user_token_usage(db, user_id=user_id, since=month_start)
        if month_used >= user.token_limit_per_month:
            raise HTTPException(status_code=429, detail="本月 token 使用量已达上限")


def auth_user_to_payload(user: AuthUser) -> dict:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "auth_type": user.auth_type,
        "enabled": bool(user.enabled),
        "role": "admin" if user.is_admin else "user",
        "is_admin": bool(user.is_admin),
        "token_limit_per_week": user.token_limit_per_week,
        "token_limit_per_month": user.token_limit_per_month,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_by": user.created_by,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        "token_generation": user.token_generation,
    }


def _ensure_user_not_exists(db: DBSession, *, username: str, user_id: str) -> None:
    existing = (
        db.query(AuthUser)
        .filter((AuthUser.username == username) | (AuthUser.user_id == user_id))
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="用户已存在")


def _purge_user_owned_data(db: DBSession, *, user_id: str) -> None:
    job_ids = [row[0] for row in db.query(CronJob.id).filter(CronJob.user_id == user_id).all()]
    if job_ids:
        db.query(CronFire).filter(CronFire.job_id.in_(job_ids)).delete(synchronize_session=False)
    db.query(CronJob).filter(CronJob.user_id == user_id).delete(synchronize_session=False)
    db.query(CronJobRun).filter(CronJobRun.user_id == user_id).delete(synchronize_session=False)

    db.query(MemoryEmbedding).filter(MemoryEmbedding.user_id == user_id).delete(synchronize_session=False)
    db.query(UserMemory).filter(UserMemory.user_id == user_id).delete(synchronize_session=False)
    db.query(UserSkillConfig).filter(UserSkillConfig.user_id == user_id).delete(synchronize_session=False)

    db.query(RunCancelRequest).filter(RunCancelRequest.user_id == user_id).delete(synchronize_session=False)
    db.query(UserRunLock).filter(UserRunLock.user_id == user_id).delete(synchronize_session=False)
    db.query(ChannelSessionBinding).filter(ChannelSessionBinding.user_id == user_id).delete(synchronize_session=False)
    db.query(SubagentRun).filter(SubagentRun.user_id == user_id).delete(synchronize_session=False)

    session_ids = [row[0] for row in db.query(Session.id).filter(Session.user_id == user_id).all()]
    if session_ids:
        round_filter = or_(Round.session_id.in_(session_ids), Round.thread_id.in_(session_ids))
        round_ids = [row[0] for row in db.query(Round.id).filter(round_filter).all()]
        if round_ids:
            db.query(AGUIEventLog).filter(AGUIEventLog.run_id.in_(round_ids)).delete(synchronize_session=False)
            db.query(LLMCallRecord).filter(LLMCallRecord.round_id.in_(round_ids)).delete(synchronize_session=False)
        db.query(LLMCallRecord).filter(LLMCallRecord.session_id.in_(session_ids)).delete(synchronize_session=False)
        db.query(ConversationMessage).filter(ConversationMessage.session_id.in_(session_ids)).delete(synchronize_session=False)
        db.query(Round).filter(round_filter).delete(synchronize_session=False)
        db.query(Session).filter(Session.id.in_(session_ids)).delete(synchronize_session=False)

    db.query(UserSandbox).filter(UserSandbox.user_id == user_id).delete(synchronize_session=False)
    db.query(UserSandboxConfig).filter(UserSandboxConfig.user_id == user_id).delete(synchronize_session=False)


def _validate_simple_username(username: str) -> None:
    if any(char in username for char in _SIMPLE_USERNAME_FORBIDDEN_CHARS):
        raise HTTPException(status_code=400, detail="simple 用户名不能包含域前缀或邮箱后缀")


def _get_existing_user(db: DBSession, user_id: str) -> AuthUser:
    user = get_auth_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user
