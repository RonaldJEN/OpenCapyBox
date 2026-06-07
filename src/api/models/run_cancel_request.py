"""运行取消请求模型（append-only 审计）。

第一版运行时为单 worker。取消投递依赖进程内 RunCancelService registry，
本表只记录审计/诊断线索，不承担跨 worker command delivery。
"""

import uuid

from sqlalchemy import Column, String, DateTime, Index

from .database import Base
from src.api.utils.timezone import now_naive


class RunCancelRequest(Base):
    """取消请求审计记录。"""

    __tablename__ = "run_cancel_requests"
    __table_args__ = (
        Index("idx_run_cancel_requests_user_session", "user_id", "session_id"),
        Index("idx_run_cancel_requests_target_run", "target_run_id"),
        Index("idx_run_cancel_requests_root_run", "root_run_id"),
        Index("idx_run_cancel_requests_requested_after", "requested_after"),
    )

    request_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(100), nullable=False, index=True)
    target_run_id = Column(String(36), nullable=True, index=True)
    root_run_id = Column(String(36), nullable=True, index=True)
    requested_after = Column(DateTime, nullable=True, index=True)

    # requested -> acked -> completed
    state = Column(String(20), nullable=False, default="requested")

    requested_at = Column(DateTime, default=now_naive, nullable=False)
    acked_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
