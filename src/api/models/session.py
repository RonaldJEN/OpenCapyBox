"""Session/Thread 數據模型 - 對應 AG-UI 的 Thread

AG-UI 協議概念映射：
- Thread (threadId) = Session（對話線程）
- 每個 Thread 可以包含多個 Run（執行）

Session 表同時作為 AG-UI 的 Thread 表使用。
"""
from sqlalchemy import Column, String, Integer, DateTime
from datetime import datetime
from .database import Base
from src.api.utils.timezone import now_naive


class Session(Base):
    """AG-UI Thread 表（向後兼容名稱：sessions）
    
    對應 AG-UI 協議中的 Thread 概念：
    - id = threadId（線程的唯一標識）
    - user_id = 用戶標識（AG-UI 中可能對應 forwardedProps.userId）
    """

    __tablename__ = "sessions"

    # threadId - 線程的唯一標識
    id = Column(String(36), primary_key=True)
    
    # 用戶標識
    user_id = Column(String(100), nullable=False, index=True)
    
    # 會話標題（用於 UI 顯示）
    title = Column(String(255), nullable=True)
    
    # 會話狀態
    # "active" = 活躍中
    # "paused" = 已暫停
    # "completed" = 已完成
    status = Column(String(20), default="active", index=True)
    
    # 使用的模型 ID（對應 models.yaml 中的 key，nullable 兼容舊數據）
    model_id = Column(String(50), nullable=True, index=True)

    # 時間戳
    created_at = Column(DateTime, default=now_naive, index=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)
    
    # === 屬性別名（AG-UI 命名） ===
    
    @property
    def thread_id(self) -> str:
        """AG-UI threadId"""
        return self.id
    
    def to_agui_dict(self) -> dict:
        """轉換為 AG-UI 兼容的字典格式"""
        return {
            "threadId": self.id,
            "userId": self.user_id,
            "title": self.title,
            "status": self.status,
            "modelId": self.model_id,
            "createdAt": int(self.created_at.timestamp() * 1000) if self.created_at else None,
            "updatedAt": int(self.updated_at.timestamp() * 1000) if self.updated_at else None,
        }


# 別名：符合 AG-UI 命名習慣
Thread = Session
