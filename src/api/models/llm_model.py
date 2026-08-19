"""Database-backed LLM model catalog."""

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, text

from .database import Base
from src.api.utils.timezone import now_naive


class LLMModel(Base):
    """Runtime LLM model configuration managed by admins after initial seed."""

    __tablename__ = "llm_models"

    model_id = Column(String(100), primary_key=True)
    display_name = Column(String(255), nullable=False)
    provider = Column(String(20), nullable=False)
    api_base = Column(Text, nullable=False)
    api_key = Column(Text, nullable=False)
    model_name = Column(String(255), nullable=False)
    max_tokens = Column(Integer, nullable=False, default=16384, server_default=text("16384"))
    context_window = Column(Integer, nullable=False, default=128000, server_default=text("128000"))
    auto_compact_token_limit = Column(Integer, nullable=True)
    tool_output_truncation_bytes = Column(Integer, nullable=False, default=42667, server_default=text("42667"))
    reasoning_format = Column(String(40), nullable=False, default="none", server_default=text("'none'"))
    reasoning_split = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    enable_thinking = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    thinking_mode = Column(String(24), nullable=False, default="provider_default", server_default=text("'provider_default'"))
    thinking_wire_format = Column(String(32), nullable=False, default="enable_thinking", server_default=text("'enable_thinking'"))
    reasoning_effort = Column(String(40), nullable=True)
    supported_reasoning_efforts_json = Column(Text, nullable=True)
    supports_image = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    max_images = Column(Integer, nullable=False, default=0, server_default=text("0"))
    supports_video = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    max_videos = Column(Integer, nullable=False, default=0, server_default=text("0"))
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    tags_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)


class LLMModelSettings(Base):
    """Singleton row for global model defaults."""

    __tablename__ = "llm_model_settings"

    id = Column(Integer, primary_key=True, default=1)
    default_model_id = Column(String(100), nullable=True)
    cron_default_model_id = Column(String(100), nullable=True)
    subagent_default_model_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
