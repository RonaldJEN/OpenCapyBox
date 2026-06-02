"""认证 API"""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session as DBSession

from src.api.models.database import get_db
from src.api.deps import create_access_token, get_current_user
from src.api.schemas.auth import LoginResponse
from src.api.services.auth_service import (
    auth_user_to_payload,
    get_enabled_user,
    login_user,
    record_login_event,
)

router = APIRouter()


def _get_client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        stripped = real_ip.strip()
        if stripped:
            return stripped

    return request.client.host if request.client else None


@router.post("/login", response_model=LoginResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: DBSession = Depends(get_db),
):
    """
    统一登录接口。

    后端按 auth_users.auth_type 分流到 simple 或 LDAP 认证。
    """
    user = login_user(db, username, password)
    user_id = user.user_id
    token_generation = user.token_generation
    is_admin = bool(user.is_admin)

    record_login_event(
        db,
        user=user,
        ip_address=_get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    token, expires_in = create_access_token(user_id, token_generation=token_generation)
    role = "admin" if is_admin else "user"

    return LoginResponse(
        user_id=user_id,
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        role=role,
        is_admin=is_admin,
        message="登录成功",
    )


@router.get("/me")
async def get_me(
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """
    获取当前用户信息（Bearer Token）
    """
    user = get_enabled_user(db, user_id)
    return auth_user_to_payload(user)
