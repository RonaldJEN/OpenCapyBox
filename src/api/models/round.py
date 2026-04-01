"""Run（運行）數據模型 - 對應 AG-UI 的 Run

AG-UI 協議概念映射：
- Thread (threadId) = Session（對話線程）
- Run (runId) = Round（單次執行）
- parentRunId = 用於分支/時間旅行

此模型同時支持舊的 Round 命名和新的 AG-UI Run 命名。
"""
from sqlalchemy import Column, String, Text, DateTime, Integer, ForeignKey
from datetime import datetime
from .database import Base
from src.api.utils.timezone import now_naive


class Round(Base):
    """AG-UI Run 表（向後兼容名稱：rounds）
    
    對應 AG-UI 協議中的 Run 概念：
    - id = runId（運行的唯一標識）
    - thread_id = threadId（對話線程 ID，關聯 Session）
    - parent_run_id = parentRunId（父運行 ID，用於分支）
    - outcome = RunFinishedEvent.outcome（success | interrupt）
    """

    __tablename__ = "rounds"

    # === AG-UI 標準字段 ===
    
    # runId - 運行的唯一標識
    id = Column(String(36), primary_key=True)
    
    # threadId - 對話線程 ID（AG-UI 標準命名）
    thread_id = Column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=True,  # 遷移期間可為空
        index=True,
    )
    
    # parentRunId - 父運行 ID（用於分支/時間旅行）
    parent_run_id = Column(String(36), nullable=True, index=True)
    
    # outcome - 運行結果（AG-UI RunFinishedOutcome）
    # "success" = 正常完成
    # "interrupt" = 需要人工介入
    outcome = Column(String(20), nullable=True)
    
    # === 業務字段 ===
    
    # 用戶輸入（對應 RunAgentInput.messages 中的用戶消息）
    user_message = Column(Text, nullable=False)

    # 用戶附件（JSON 字符串，保存 path/name/mime_type 等）
    user_attachments = Column(Text, nullable=True)
    
    # 最終響應（Agent 的最後輸出）
    final_response = Column(Text, nullable=True)
    
    # 步驟計數
    step_count = Column(Integer, default=0)
    
    # 運行狀態（內部使用）
    # "running" = 執行中
    # "completed" = 已完成
    # "failed" = 執行失敗
    # "interrupted" = 已中斷（等待人工介入）
    status = Column(String(20), default="running")
    
    # 時間戳
    created_at = Column(DateTime, default=now_naive, index=True)
    completed_at = Column(DateTime, nullable=True)
    
    # === 向後兼容字段 ===
    
    # session_id - 舊的會話 ID 字段（逐步遷移到 thread_id）
    session_id = Column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE", use_alter=True, name="fk_round_session"),
        nullable=True,
        index=True,
    )
    
    # === 屬性別名（AG-UI 命名） ===
    
    @property
    def run_id(self) -> str:
        """AG-UI runId"""
        return self.id
    
    @property
    def effective_thread_id(self) -> str:
        """獲取有效的 threadId（優先 thread_id，否則 session_id）"""
        return self.thread_id or self.session_id
    
    def to_agui_dict(self) -> dict:
        """轉換為 AG-UI 兼容的字典格式"""
        return {
            "runId": self.id,
            "threadId": self.effective_thread_id,
            "parentRunId": self.parent_run_id,
            "outcome": self.outcome,
            "status": self.status,
            "createdAt": int(self.created_at.timestamp() * 1000) if self.created_at else None,
            "completedAt": int(self.completed_at.timestamp() * 1000) if self.completed_at else None,
        }


# 別名：符合 AG-UI 命名習慣
Run = Round
