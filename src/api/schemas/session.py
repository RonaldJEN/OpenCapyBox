"""会话相关 Schema"""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class SessionCreate(BaseModel):
    """创建会话请求"""

    title: Optional[str] = None


class CreateSessionResponse(BaseModel):
    """创建会话响应"""

    session_id: str
    model_id: Optional[str] = None
    message: str = "会话创建成功"


class SessionResponse(BaseModel):
    """会话响应"""

    id: str
    user_id: str
    status: str
    created_at: datetime
    updated_at: datetime  # 前端期望 updated_at 而不是 last_active
    title: Optional[str] = None
    model_id: Optional[str] = None

    class Config:
        from_attributes = True


class SessionListResponse(BaseModel):
    """会话列表响应"""

    sessions: list[SessionResponse]


class FileInfo(BaseModel):
    """文件信息"""

    name: str = Field(..., description="文件名")
    path: str = Field(..., description="相对路径")
    size: int = Field(..., description="文件大小（字节）")
    modified: str = Field(..., description="修改时间（ISO格式）")
    type: str = Field(..., description="文件类型（扩展名）")


class UpdateSessionTitleRequest(BaseModel):
    """更新会话标题请求"""

    title: str = Field(..., min_length=1, max_length=255, description="新标题")


class SessionPollResponse(BaseModel):
    """会话轮询响应（轻量级）"""

    round_count: int = Field(..., description="轮次总数")


class FileListResponse(BaseModel):
    """文件列表响应"""

    files: list[FileInfo]
    total: int = Field(..., description="文件总数")
