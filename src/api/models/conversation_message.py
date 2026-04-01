"""Agent 对话消息模型

用于 Agent 上下文恢复，替代从 agui_events 重建消息列表。
前端不感知此表，仅供后端 Agent 内部使用。
"""
from sqlalchemy import Column, String, Integer, Text, Boolean, DateTime, ForeignKey, UniqueConstraint
from .database import Base
from src.api.utils.timezone import now_naive


class ConversationMessage(Base):
    """Agent 上下文消息表

    存储每轮对话的干净消息列表，用于 Agent 重启后快速恢复上下文。
    与 agui_events 区别：
    - agui_events：前端 SSE 重放用，包含所有 UI 事件
    - conversation_messages：后端 Agent 上下文用，仅含 role/content 消息对
    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_convmsg_session_seq"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False, index=True)
    round_id = Column(String(36), nullable=True, index=True)
    # 同一 session 内的消息顺序
    sequence = Column(Integer, nullable=False)
    # user / assistant / tool
    role = Column(String(20), nullable=False)
    # JSON 字符串：纯文本时为 string，含工具调用时为 JSON list
    content = Column(Text, nullable=False)
    # True = 已被摘要压缩，不参与上下文重建
    is_summary = Column(Boolean, default=False)
    # 估算 token 数（可选，用于 budget 计算）
    token_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=now_naive)
