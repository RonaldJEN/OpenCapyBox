"""认证相关 Schema"""
from pydantic import BaseModel


class LoginRequest(BaseModel):
    """登录请求"""

    username: str
    password: str


class LoginResponse(BaseModel):
    """登录响应"""

    user_id: str  # 登录成功后返回的用户 ID（即 username）
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    message: str = "登录成功"
