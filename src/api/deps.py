"""全局依赖注入 — 鉴权与用户标识"""

import time
import uuid
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.database import SessionLocal, get_db
from src.api.services.auth_service import get_enabled_user, require_admin_user
from src.api.utils.timezone import get_timezone

_ISSUER = "opencapybox"
_ALGORITHM = "HS256"

_bearer_scheme = HTTPBearer(auto_error=False)
MOBILE_SESSION_COOKIE_NAME = "opencapybox_mobile_session"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


def create_access_token(user_id: str, *, token_generation: int = 0, expires_in_seconds: int | None = None) -> tuple[str, int]:
    """创建 HS256 签名访问令牌。

    Returns:
        (token, expires_in_seconds)
    """
    settings = get_settings()
    now = int(time.time())
    ttl = (
        expires_in_seconds
        if expires_in_seconds is not None
        else max(int(settings.auth_token_expire_minutes) * 60, 60)
    )

    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
        "iss": _ISSUER,
        "gen": token_generation,
    }

    token = jwt.encode(payload, settings.auth_secret_key, algorithm=_ALGORITHM)
    return token, ttl


def verify_access_token(token: str, db: DBSession | None = None) -> str:
    """校验访问令牌并返回 user_id。"""
    settings = get_settings()

    try:
        payload = jwt.decode(
            token,
            settings.auth_secret_key,
            algorithms=[_ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )

        user_id = payload.get("sub")
        if not isinstance(user_id, str) or not user_id.strip():
            raise jwt.InvalidTokenError("invalid subject")

        if db is None:
            with SessionLocal() as token_db:
                user = get_enabled_user(token_db, user_id)
        else:
            user = get_enabled_user(db, user_id)

        token_gen = payload.get("gen")
        if token_gen is None or token_gen != user.token_generation:
            raise jwt.InvalidTokenError("token generation mismatch")

        token_iat = payload.get("iat")
        if not isinstance(token_iat, int) or isinstance(token_iat, bool):
            raise jwt.InvalidTokenError("invalid issued-at")
        if user.created_at:
            created_at = user.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=get_timezone())
            if token_iat < int(created_at.timestamp()):
                raise jwt.InvalidTokenError("token issued before user creation")

        return user_id
    except jwt.ExpiredSignatureError:
        raise _unauthorized("访问令牌已过期") from None
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        raise _unauthorized(exc.detail) from exc
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("无效或已过期的访问令牌") from exc


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: DBSession = Depends(get_db),
) -> str:
    """从 Bearer Token 或移动端 HttpOnly Cookie 校验当前用户。"""
    if credentials:
        if credentials.scheme.lower() != "bearer":
            raise _unauthorized("未提供访问令牌")
        return verify_access_token(credentials.credentials, db)

    cookie_token = request.cookies.get(MOBILE_SESSION_COOKIE_NAME)
    if not cookie_token:
        raise _unauthorized("未提供访问令牌")
    return verify_access_token(cookie_token, db)


async def get_current_admin_user(
    request: Request,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> str:
    """校验当前用户是否为管理员。"""
    try:
        require_admin_user(db, user_id)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise _forbidden(exc.detail) from exc
        raise
    if request is not None:
        # Import lazily so the authentication primitives stay usable while
        # database models are being initialized.
        from src.api.services.admin_operation_audit import begin_admin_audit

        begin_admin_audit(request, user_id)
    return user_id
