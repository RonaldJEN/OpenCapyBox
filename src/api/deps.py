"""全局依赖注入 — 鉴权与用户标识"""

import uuid
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.api.config import get_settings

_ISSUER = "opencapybox"
_ALGORITHM = "HS256"

_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def create_access_token(user_id: str, expires_in_seconds: int | None = None) -> tuple[str, int]:
    """创建 HS256 签名访问令牌。

    Returns:
        (token, expires_in_seconds)
    """
    import time

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
    }

    token = jwt.encode(payload, settings.auth_secret_key, algorithm=_ALGORITHM)
    return token, ttl


def verify_access_token(token: str) -> str:
    """校验访问令牌并返回 user_id。"""
    settings = get_settings()

    try:
        payload = jwt.decode(
            token,
            settings.auth_secret_key,
            algorithms=[_ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["exp", "iss", "sub"]},
        )

        user_id = payload.get("sub")
        if not isinstance(user_id, str) or not user_id.strip():
            raise jwt.InvalidTokenError("invalid subject")

        # 令牌用户必须仍在可登录用户列表中
        auth_users = settings.get_auth_users()
        if user_id not in auth_users:
            raise jwt.InvalidTokenError("unknown user")

        return user_id
    except jwt.ExpiredSignatureError:
        raise _unauthorized("访问令牌已过期") from None
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("无效或已过期的访问令牌") from exc


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """从 Bearer Token 校验并返回当前用户 ID。"""
    if not credentials or credentials.scheme.lower() != "bearer":
        raise _unauthorized("未提供访问令牌")
    return verify_access_token(credentials.credentials)
