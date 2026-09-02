"""用户记忆与人格相关数据模型

包含：
- UserMemory：Markdown 记忆文件持久化（USER.md / MEMORY.md / SOUL.md）
- MemoryEmbedding：向量索引（PostgreSQL pgvector vector(2560)）
- CronJobRun：定时任务执行历史
- UserSkillConfig：Skill 启用/禁用状态
"""
import json

from sqlalchemy import Column, String, Integer, Text, Boolean, DateTime, ForeignKey, JSON

from src.agent.schema.skill_key import MAX_SKILL_KEY_LENGTH

from .database import Base
from src.api.utils.timezone import now_naive
from src.api.utils.sandbox_helpers import filter_workspace_publish_scratch
from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS, PGVector, normalize_embedding_vector


class UserMemory(Base):
    """用户记忆/人格 Markdown 文件持久化

    沙箱文件（/home/user/*.md）为缓存层，DB 为持久化源。

    file_type 枚举值：
    - user_md       → USER.md（用户画像/偏好，Agent 对话中自动提炼，用户可编辑）
    - memory_md     → MEMORY.md（长期共识/知识）
    - soul_md       → SOUL.md（Agent 沟通风格/人格）
    - agents_md     → 兼容旧数据；AGENTS.md 当前由平台模板管理，不再作为用户 DB 配置写入
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
    # New conversation indexes own a real Round. Legacy rows and memory-file
    # indexes intentionally remain nullable and are not backfilled.
    conversation_round_id = Column(
        String(36),
        ForeignKey("rounds.id", ondelete="CASCADE"),
        nullable=True,
    )
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
    # 历史快照字段，不设 FK：删除任务后执行历史仍需保留。
    job_id = Column(Integer, nullable=True, index=True)
    fire_id = Column(String(36), nullable=True, unique=True, index=True)
    # 来自 CronJob 表的任务名
    job_name = Column(String(100), nullable=False)
    cron_expr = Column(String(50), nullable=False)
    rule_version = Column(Integer, nullable=True)
    definition_version = Column(Integer, nullable=True)
    definition_snapshot = Column(Text, nullable=True)
    # 本次调度原本计划触发的分钟；手动触发时为空。
    scheduled_at = Column(DateTime, nullable=True)
    # scheduled / manual
    trigger_source = Column(String(20), nullable=False, default="scheduled")
    queued_at = Column(DateTime, default=now_naive, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    # queued / running / success / failed / conflict / unknown
    status = Column(String(20), default="queued", nullable=False, index=True)
    # queued / preparing / executing / publishing / terminal
    phase = Column(String(20), default="queued", nullable=False)
    claim_token = Column(String(36), nullable=True, index=True)
    claim_worker_id = Column(String(64), nullable=True, index=True)
    claim_lease_expires_at = Column(DateTime, nullable=True, index=True)
    heartbeat_at = Column(DateTime, nullable=True)
    # Claim/dispatch 冻结的 OpenSandbox 实例。执行、被动文件请求和重启恢复
    # 只能连接这个 ID，不得用后来的用户绑定创建替代实例。
    sandbox_id = Column(String(100), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    error_code = Column(String(80), nullable=True)
    output = Column(Text, nullable=True)
    # 未读标记：新记录默认未读(False)，存量通过迁移 DEFAULT 1 回填为已读
    is_read = Column(Boolean, default=False)
    # 产物文件元数据 JSON 数组，如 [{"name":"report.md","path":"report.md","size":1234,"type":"md"}]
    artifacts = Column(Text, nullable=True)
    # 本次运行的沙箱工作目录绝对路径
    run_workspace = Column(String(500), nullable=True)
    # WorkspaceService 回写的结构化变更清单；不与 run artifacts 混用。
    workspace_changes = Column(Text, nullable=True)
    # Proposed/conflict change sets that still need publication or review.
    workspace_change_sets = Column(Text, nullable=True)

    def to_dict(self) -> dict:
        """序列化为前端可用的 dict（单一事实源）。"""
        def _decode_json(raw):
            if not raw:
                return None
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return None

        artifacts = _decode_json(self.artifacts)
        if isinstance(artifacts, list):
            artifacts = filter_workspace_publish_scratch(artifacts)
        workspace_changes = _decode_json(self.workspace_changes) or []
        workspace_change_sets = _decode_json(self.workspace_change_sets) or []
        phase = self.phase
        if self.status in {"success", "failed", "conflict", "unknown"}:
            phase = "terminal"
        return {
            "id": self.id,
            "job_id": self.job_id,
            "fire_id": self.fire_id,
            "job_name": self.job_name,
            "cron_expr": self.cron_expr,
            "rule_version": self.rule_version,
            "definition_version": self.definition_version,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "trigger_source": self.trigger_source,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status,
            "phase": phase,
            "attempt_count": int(self.attempt_count or 0),
            "error_code": self.error_code,
            "output": self.output,
            "is_read": bool(self.is_read),
            "artifacts": artifacts,
            "run_workspace": self.run_workspace,
            "workspace_changes": workspace_changes,
            "workspace_change_sets": workspace_change_sets,
        }


class UserSkillConfig(Base):
    """用户 Skill 启用/禁用配置"""

    __tablename__ = "user_skill_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    skill_name = Column(String(MAX_SKILL_KEY_LENGTH), nullable=False)
    enabled = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)
