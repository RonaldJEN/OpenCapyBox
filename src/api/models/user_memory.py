"""用户记忆与人格相关数据模型

包含：
- UserMemory：Markdown 记忆文件持久化（USER.md / MEMORY.md / SOUL.md / AGENTS.md）
- MemoryEmbedding：向量索引（PostgreSQL pgvector vector(2560)）
- CronJobRun：定时任务执行历史
- UserSkillConfig：Skill 启用/禁用状态
"""
import json

from sqlalchemy import Column, String, Integer, Text, Boolean, DateTime, JSON
from sqlalchemy.types import UserDefinedType
from .database import Base
from src.api.utils.timezone import now_naive
from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS, normalize_embedding_vector, parse_embedding_vector, serialize_pgvector


class PGVector(UserDefinedType):
    """PostgreSQL pgvector 类型。"""

    cache_ok = True

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    def get_col_spec(self, **kw):
        return f"vector({self.dimensions})"

    def bind_processor(self, dialect):
        def process(value):
            return serialize_pgvector(value)

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            # pgvector 返回字符串形式 "[0.1,0.2,...]"，直接解析为 list（写入时已保证维度正确）
            if isinstance(value, str):
                return parse_embedding_vector(value)
            return list(value)

        return process


class UserMemory(Base):
    """用户记忆/人格 Markdown 文件持久化

    沙箱文件（/home/user/*.md）为缓存层，DB 为持久化源。

    file_type 枚举值：
    - user_md       → USER.md（用户画像/偏好，Agent 对话中自动提炼，用户可编辑）
    - memory_md     → MEMORY.md（长期共识/知识）
    - soul_md       → SOUL.md（Agent 沟通风格/人格）
    - agents_md     → AGENTS.md（行为规则/任务指南）
    """

    __tablename__ = "user_memory"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    # user_md / memory_md / soul_md / agents_md
    file_type = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    # 乐观锁：防止并发写冲突
    version = Column(Integer, default=1, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)


class MemoryEmbedding(Base):
    """记忆向量索引

    使用 OpenAI Embedding API 生成向量。
    PostgreSQL 存为 pgvector vector(2560)。
    若未配置 EMBEDDING_API_KEY，则降级为关键词检索。
    """

    __tablename__ = "memory_embeddings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    # 例如 "memory/2026-03-26.md"
    file_path = Column(String(255), nullable=True)
    chunk_index = Column(Integer, nullable=True)
    chunk_text = Column(Text, nullable=False)
    # float array，例如 [0.12, -0.34, ...]；短向量写入前补齐到 2560 维
    embedding = Column(JSON().with_variant(PGVector(MEMORY_EMBEDDING_DIMENSIONS), "postgresql"), nullable=True)
    created_at = Column(DateTime, default=now_naive)


class CronJobRun(Base):
    """定时任务执行历史

    任务定义在 CronJob 表（Agent 通过 manage_cron 工具操作），DB 存执行结果。
    """

    __tablename__ = "cron_job_runs"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    # 来自 CronJob 表的任务名
    job_name = Column(String(100), nullable=False)
    cron_expr = Column(String(50), nullable=False)
    started_at = Column(DateTime, default=now_naive)
    completed_at = Column(DateTime, nullable=True)
    # running / success / failed
    status = Column(String(20), default="running")
    output = Column(Text, nullable=True)
    # 未读标记：新记录默认未读(False)，存量通过迁移 DEFAULT 1 回填为已读
    is_read = Column(Boolean, default=False)
    # 产物文件元数据 JSON 数组，如 [{"name":"report.md","path":"report.md","size":1234,"type":"md"}]
    artifacts = Column(Text, nullable=True)
    # 本次运行的沙箱工作目录绝对路径
    run_workspace = Column(String(500), nullable=True)

    def to_dict(self) -> dict:
        """序列化为前端可用的 dict（单一事实源）。"""
        artifacts = None
        if self.artifacts:
            try:
                artifacts = json.loads(self.artifacts)
            except (ValueError, TypeError):
                pass
        return {
            "id": self.id,
            "job_name": self.job_name,
            "cron_expr": self.cron_expr,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status,
            "output": self.output,
            "is_read": bool(self.is_read),
            "artifacts": artifacts,
            "run_workspace": self.run_workspace,
        }


class UserSkillConfig(Base):
    """用户 Skill 启用/禁用配置"""

    __tablename__ = "user_skill_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    skill_name = Column(String(100), nullable=False)
    enabled = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)
