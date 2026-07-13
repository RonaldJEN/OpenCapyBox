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
    role: str
    is_admin: bool
    message: str = "登录成功"


class MobileSessionRequest(BaseModel):
    """移动端企业 SSO 会话交换请求。"""

    nd_auth_token: str | None = None


class MobileSessionResponse(BaseModel):
    """移动端会话信息；JWT 仅通过 HttpOnly Cookie 下发。"""

    user_id: str
    username: str
    role: str
    is_admin: bool
