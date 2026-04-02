"""对话相关 Schema"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Annotated, Literal


class TextContentBlock(BaseModel):
    """文本内容块"""

    type: Literal["text"]
    text: str = Field(..., min_length=1, max_length=10000, description="文本内容")


class ImageUrl(BaseModel):
    """图片 URL 对象"""

    url: str = Field(..., min_length=1, description="图片 URL 或 Data URL")


class ImageContentBlock(BaseModel):
    """图片内容块"""

    type: Literal["image_url"]
    image_url: ImageUrl
    file: Optional[Dict[str, Any]] = Field(default=None, description="可选文件元数据（用于历史预览）")


class VideoUrl(BaseModel):
    """视频 URL 对象（V2 预留）"""

    url: str = Field(..., min_length=1, description="视频 URL 或 Data URL")


class VideoContentBlock(BaseModel):
    """视频内容块（V2 预留）"""

    type: Literal["video_url"]
    video_url: VideoUrl


class FileObject(BaseModel):
    """文件对象"""

    path: str = Field(..., min_length=1, description="文件路径（会话工作区相对路径）")
    name: Optional[str] = Field(default=None, description="文件名")
    mime_type: Optional[str] = Field(default=None, description="MIME 类型")
    size: Optional[int] = Field(default=None, description="文件大小（字节）")


class FileContentBlock(BaseModel):
    """文件内容块"""

    type: Literal["file"]
    file: FileObject


ContentBlock = Annotated[
    TextContentBlock | ImageContentBlock | VideoContentBlock | FileContentBlock,
    Field(discriminator="type"),
]


class ChatRequest(BaseModel):
    """对话请求"""

    content: List[ContentBlock] = Field(..., min_length=1, description="用户消息内容块")


class SendMessageRequest(BaseModel):
    """发送消息请求（V2：仅支持 content blocks）"""

    content: List[ContentBlock] = Field(..., min_length=1, description="用户消息内容块")


class ResumeRequest(BaseModel):
    """恢复中断请求（Human-in-the-Loop）"""

    interrupt_id: str = Field(..., description="中断 ID（来自 InterruptDetails.id）")
    answers: Dict[str, str] = Field(
        ...,
        description="用户答案，key 为问题文本，value 为选择的答案",
    )



# 🆕 新增 Round/Step 相关 Schema

class ToolCall(BaseModel):
    """工具调用"""
    name: str
    input: Dict[str, Any]


class ToolResult(BaseModel):
    """工具结果"""
    success: bool
    content: str
    error: Optional[str] = None


class StepData(BaseModel):
    """执行步骤数据"""
    step_number: int
    thinking: Optional[str] = None
    assistant_content: Optional[str] = None
    tool_calls: List[ToolCall] = Field(default_factory=list)
    tool_results: List[ToolResult] = Field(default_factory=list)
    status: str = "completed"
    created_at: Optional[str] = None



class RoundData(BaseModel):
    """对话轮次数据"""
    round_id: str
    user_message: str
    user_attachments: List[Dict[str, Any]] = Field(default_factory=list)
    final_response: Optional[str] = None
    steps: List[StepData] = Field(default_factory=list)
    step_count: int
    status: str
    created_at: str
    completed_at: Optional[str] = None
    interrupt: Optional[Dict[str, Any]] = None


class ChatResponse(BaseModel):
    """对话响应"""

    session_id: str
    message: str
    thinking: Optional[str] = None
    files: List[str] = []
    turn: int
    message_count: int


class MessageHistory(BaseModel):
    """消息历史"""

    role: str
    content: Optional[str]
    thinking: Optional[str] = None
    created_at: str


class HistoryResponse(BaseModel):
    """历史记录响应"""

    session_id: str
    messages: List[MessageHistory]
    total: int


class HistoryResponseV2(BaseModel):
    """历史记录响应 V2（基于 Round）"""
    session_id: str
    rounds: List[RoundData]
    total: int
