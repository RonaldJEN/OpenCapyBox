"""运行取消请求模型（跨 worker 取消信号）

用于在多 worker 部署下传递 abort 请求：
- abort 端点写入 requested
- 执行中的 worker 轮询并 acked，然后触发本地 cancel_token
- 运行结束后标记 completed
"""

import uuid

from sqlalchemy import Column, String, DateTime

from .database import Base
from src.api.utils.timezone import now_naive


class RunCancelRequest(Base):
    """会话级取消请求。"""

    __tablename__ = "run_cancel_requests"

    # 一会话一条请求记录（会被后续请求覆盖更新）
    session_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)

    # requested -> acked -> completed
    state = Column(String(20), nullable=False, default="requested")
    request_id = Column(String(36), nullable=False, default=lambda: str(uuid.uuid4()))

    requested_at = Column(DateTime, default=now_naive, nullable=False)
    acked_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
