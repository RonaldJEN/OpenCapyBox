"""认证 API"""

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as DBSession

from src.api.models.database import get_db
from src.api.config import get_settings
from src.api.deps import MOBILE_SESSION_COOKIE_NAME, create_access_token, get_current_user
from src.api.schemas.auth import LoginResponse, MobileSessionRequest, MobileSessionResponse
from src.api.services.auth_service import (
    auth_user_to_payload,
    get_enabled_user,
    login_user,
    record_login_event,
)
from src.api.services.mobile_auth_service import (
    MobileGatewayRedirect,
    fetch_mobile_gateway_user,
    login_mobile_sso_user,
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


@router.post("/mobile/session", response_model=MobileSessionResponse)
async def create_mobile_session(
    payload: MobileSessionRequest,
    request: Request,
    response: Response,
    db: DBSession = Depends(get_db),
):
    """使用企业网关 Cookie/ND token 建立移动端 OpenCapyBox 会话。"""
    gateway_result = await fetch_mobile_gateway_user(
        cookie_header=request.headers.get("cookie"),
        nd_auth_token=payload.nd_auth_token,
    )
    if isinstance(gateway_result, MobileGatewayRedirect):
        return JSONResponse(
            status_code=401,
            content={
                "code": "SSO_REQUIRED",
                "redirect_url": gateway_result.redirect_url,
            },
        )

    user = login_mobile_sso_user(db, gateway_result.account)
    record_login_event(
        db,
        user=user,
        ip_address=_get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    token, expires_in = create_access_token(
        user.user_id,
        token_generation=user.token_generation,
    )
    response.set_cookie(
        key=MOBILE_SESSION_COOKIE_NAME,
        value=token,
        max_age=expires_in,
        httponly=True,
        secure=get_settings().mobile_auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    return MobileSessionResponse(
        user_id=user.user_id,
        username=user.username,
        role="admin" if user.is_admin else "user",
        is_admin=bool(user.is_admin),
    )
