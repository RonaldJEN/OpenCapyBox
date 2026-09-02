"""Cron 去重记录模型。

用于在多 worker 场景下通过 UNIQUE 约束实现同一分钟只触发一次。

TODO(迁移): 该表是 PostgreSQL 部署下的执行权仲裁凭证，语义上等价于一个
带 TTL 的去重键。后续如果引入 Redis，可用
`SET NX PX <ttl>` 取代整张表，key 形如 `cron:fire:{job_id}:{scheduled_at}`，
TTL 按 cron 最小粒度设（如 120s）自动回收，无需再做历史行清理。
迁移路径：
  1. 抽象 `FireArbiter` 接口（`try_acquire(job_id, minute) -> bool`）
  2. 保留当前 PostgreSQL 实现为 `PostgresFireArbiter`
  3. 新增 `RedisFireArbiter` 作为可选实现，按部署模式切换
届时本 Model 与 `cron_fires` 表可整体下线。
"""

from sqlalchemy import Column, String, Integer, DateTime, UniqueConstraint, ForeignKey

from .database import Base
from src.api.utils.timezone import now_naive


class CronFire(Base):
    """Cron 去重键记录。

    仅保存最小字段，不承载执行状态。
    """

    __tablename__ = "cron_fires"
    __table_args__ = (
        UniqueConstraint("job_id", "scheduled_at", name="uq_cronfire_job_time"),
    )

    id = Column(String(36), primary_key=True)
    job_id = Column(Integer, ForeignKey("cron_jobs.id"), nullable=False, index=True)
    scheduled_at = Column(DateTime, nullable=False, index=True)
    rule_version = Column(Integer, nullable=False, default=1)
    definition_version = Column(Integer, nullable=False, default=1)
    # 与 Fire 同一事务创建的 durable queued run。
    run_id = Column(String(36), nullable=True, unique=True, index=True)
    created_at = Column(DateTime, default=now_naive)
