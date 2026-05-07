"""LLM 调用记录模型

用于持久化每次 LLM 调用（step 级）的输入输出快照，
便于排查多轮上下文注入与模型输出偏差问题。
"""

from sqlalchemy import Column, String, Integer, Float, Text, DateTime, Boolean, ForeignKey, UniqueConstraint, text

from .database import Base
from src.api.utils.timezone import now_naive


class LLMCallRecord(Base):
    """每次 LLM 调用的快照记录。"""

    __tablename__ = "llm_call_records"
    __table_args__ = (
        UniqueConstraint("round_id", "step_index", name="uq_llm_call_round_step"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False, index=True)
    round_id = Column(String(36), ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    step_index = Column(Integer, nullable=False)
    request_message_count = Column(Integer, nullable=True)
    manual_review_status = Column(
        String(20),
        nullable=False,
        default="没问题",
        server_default=text("'没问题'"),
    )

    request_messages = Column(Text, nullable=False)
    request_tools = Column(Text, nullable=False)

    response_content = Column(Text, nullable=True)
    response_thinking = Column(Text, nullable=True)
    response_tool_calls = Column(Text, nullable=True)
    response_error = Column(Text, nullable=True)
    finish_reason = Column(String(50), nullable=True)

    usage_prompt_tokens = Column(Integer, nullable=True)
    usage_completion_tokens = Column(Integer, nullable=True)
    usage_total_tokens = Column(Integer, nullable=True)
    first_token_latency_s = Column(Float, nullable=True)
    completion_latency_s = Column(Float, nullable=True)

    compaction_triggered = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    compaction_pre_tokens = Column(Integer, nullable=True)
    compaction_post_tokens = Column(Integer, nullable=True)
    compaction_tokens_saved = Column(Integer, nullable=True)
    compaction_microcompact_compacted_messages = Column(Integer, nullable=True)
    compaction_summary_generated_count = Column(Integer, nullable=True)
    compaction_summary_reused_count = Column(Integer, nullable=True)
    compaction_summary_quality_repair_count = Column(Integer, nullable=True)
    compaction_emergency_truncate_dropped_rounds = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=now_naive, nullable=False, index=True)
