"""简单认证 API"""
import hmac

from fastapi import APIRouter, Depends, HTTPException, Form
from src.api.deps import create_access_token, get_current_user
from src.api.schemas.auth import LoginResponse
from src.api.config import get_settings

router = APIRouter()
settings = get_settings()


@router.post("/login", response_model=LoginResponse)
async def login(username: str = Form(...), password: str = Form(...)):
    """
    简单登录接口

    返回用户信息（username 作为 user_id）
    """
    # 获取配置的用户列表
    auth_users = settings.get_auth_users()

    # 验证用户名和密码
    if username not in auth_users:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not hmac.compare_digest(auth_users[username], password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token, expires_in = create_access_token(username)

    # 登录成功，返回用户信息与访问令牌
    return LoginResponse(
        user_id=username,
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        message="登录成功",
    )


@router.get("/me")
async def get_me(user_id: str = Depends(get_current_user)):
    """
    获取当前用户信息（Bearer Token）
    """
    return {"user_id": user_id, "username": user_id}
