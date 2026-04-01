"""用户记忆与人格相关数据模型

包含：
- UserMemory：Markdown 记忆文件持久化（USER.md / MEMORY.md / SOUL.md / AGENTS.md / HEARTBEAT.md）
- MemoryEmbedding：向量索引（SQLite JSON 列，零依赖）
- CronJobRun：HEARTBEAT.md 定时任务执行历史
- UserSkillConfig：Skill 启用/禁用状态
"""
from sqlalchemy import Column, String, Integer, Text, Boolean, DateTime
from .database import Base
from src.api.utils.timezone import now_naive


class UserMemory(Base):
    """用户记忆/人格 Markdown 文件持久化

    沙箱文件（/home/user/*.md）为缓存层，DB 为持久化源。

    file_type 枚举值：
    - user_md       → USER.md（用户画像/偏好，Agent 对话中自动提炼，用户可编辑）
    - memory_md     → MEMORY.md（长期共识/知识）
    - soul_md       → SOUL.md（Agent 沟通风格/人格）
    - agents_md     → AGENTS.md（行为规则/任务指南）
    - heartbeat_md  → HEARTBEAT.md（定时任务定义）
    """

    __tablename__ = "user_memory"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    # user_md / memory_md / soul_md / agents_md / heartbeat_md
    file_type = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    # 乐观锁：防止并发写冲突
    version = Column(Integer, default=1, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)


class MemoryEmbedding(Base):
    """记忆向量索引

    使用 OpenAI Embedding API 生成向量，存为 JSON float array。
    若未配置 EMBEDDING_API_KEY，则降级为关键词检索。
    """

    __tablename__ = "memory_embeddings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    # 例如 "memory/2026-03-26.md"
    file_path = Column(String(255), nullable=True)
    chunk_index = Column(Integer, nullable=True)
    chunk_text = Column(Text, nullable=False)
    # JSON float array，例如 "[0.12, -0.34, ...]"
    embedding = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_naive)


class CronJobRun(Base):
    """HEARTBEAT.md 定时任务执行历史

    任务定义在 HEARTBEAT.md（Agent 可直读写），DB 仅存执行结果。
    """

    __tablename__ = "cron_job_runs"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    # 来自 HEARTBEAT.md 的任务名
    job_name = Column(String(100), nullable=False)
    cron_expr = Column(String(50), nullable=False)
    started_at = Column(DateTime, default=now_naive)
    completed_at = Column(DateTime, nullable=True)
    # running / success / failed
    status = Column(String(20), default="running")
    output = Column(Text, nullable=True)


class UserSkillConfig(Base):
    """用户 Skill 启用/禁用配置"""

    __tablename__ = "user_skill_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    skill_name = Column(String(100), nullable=False)
    enabled = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)
