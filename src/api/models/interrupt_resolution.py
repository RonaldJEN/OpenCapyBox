"""ask_user 中断恢复事实表。

该表只服务后端 Agent 上下文恢复：把一次 interrupt_id 对应的用户回答
结构化持久化，使冷恢复可以还原热 resume 时写入 tool result 的语义。
"""
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, UniqueConstraint

from .database import Base
from src.api.utils.timezone import now_naive


class InterruptResolution(Base):
    """Human-in-the-loop interrupt 的结构化恢复记录。"""

    __tablename__ = "interrupt_resolutions"
    __table_args__ = (
        UniqueConstraint("resume_round_id", name="uq_interrupt_resolution_resume_round"),
    )

    interrupt_id = Column(String(36), primary_key=True)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_round_id = Column(String(36), ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    resume_round_id = Column(String(36), ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    tool_call_id = Column(String(64), nullable=True)
    answers_json = Column(Text, nullable=False)
    resume_user_message = Column(Text, nullable=False)
    tool_result_content = Column(Text, nullable=False)
    restore_strategy = Column(String(40), nullable=True)
    fallback_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False, index=True)
