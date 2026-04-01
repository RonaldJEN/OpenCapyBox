"""AG-UI Event 數據模型 - 存儲 AG-UI 協議事件

AG-UI 事件類型（EventType）：
- 生命周期：RUN_STARTED, RUN_FINISHED, RUN_ERROR, STEP_STARTED, STEP_FINISHED
- 文本消息：TEXT_MESSAGE_START, TEXT_MESSAGE_CONTENT, TEXT_MESSAGE_END
- 工具調用：TOOL_CALL_START, TOOL_CALL_ARGS, TOOL_CALL_END, TOOL_CALL_RESULT
- 狀態管理：STATE_SNAPSHOT, STATE_DELTA, MESSAGES_SNAPSHOT
- 活動事件：ACTIVITY_SNAPSHOT, ACTIVITY_DELTA
- 特殊事件：RAW, CUSTOM

此表存儲所有 AG-UI 協議事件，支持：
1. 事件重放（Replay）- 通過 sequence 保證順序
2. 歷史追溯 - 通過 run_id 關聯運行
3. 消息追蹤 - 通過 message_id 關聯消息
4. 工具調用追蹤 - 通過 tool_call_id 關聯工具調用
"""
from sqlalchemy import Column, String, Text, DateTime, Integer, ForeignKey, Index
from .database import Base
from src.api.utils.timezone import now_naive


class AGUIEventLog(Base):
    """AG-UI 事件日誌表
    
    對應 AG-UI 協議中的 BaseEvent：
    - type: EventType（事件類型）
    - timestamp: number（時間戳）
    - rawEvent: any（原始事件，可選）
    
    擴展字段用於查詢優化：
    - run_id: 關聯的運行 ID（對應 RunStartedEvent.runId）
    - message_id: 關聯的消息 ID（對應 TextMessage*.messageId）
    - tool_call_id: 關聯的工具調用 ID（對應 ToolCall*.toolCallId）
    """

    __tablename__ = "agui_events"

    # === 主鍵（自增整數，內部使用，不暴露給前端）===
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # === AG-UI 關聯字段 ===
    
    # runId - 關聯的運行（對應 RunStartedEvent.runId）
    run_id = Column(
        String(36), 
        ForeignKey("rounds.id", ondelete="CASCADE"), 
        nullable=False,
    )
    
    # === AG-UI 事件字段 ===
    
    # type - 事件類型（EventType 枚舉值）
    event_type = Column(String(50), nullable=False)
    
    # timestamp - 事件時間戳（毫秒）
    timestamp = Column(Integer, nullable=True)
    
    # === 索引優化字段 ===
    
    # messageId - 關聯的消息 ID（用於 TextMessage* 和 ToolCallResult 事件）
    message_id = Column(String(36), nullable=True)
    
    # toolCallId - 關聯的工具調用 ID（用於 ToolCall* 事件）
    tool_call_id = Column(String(36), nullable=True)
    
    # === 事件數據 ===
    
    # payload - JSON 序列化的完整事件（包含所有 AG-UI 標準字段）
    payload = Column(Text, nullable=False)
    
    # === 序列號（保證事件順序） ===
    sequence = Column(Integer, nullable=False)
    
    # === 內部時間戳 ===
    created_at = Column(DateTime, default=now_naive)
    
    # === 索引定義 ===
    __table_args__ = (
        # 按運行查詢所有事件
        Index('idx_agui_events_run_id', 'run_id'),
        # 按事件類型過濾
        Index('idx_agui_events_type', 'event_type'),
        # 按序列號排序（用於重放）
        Index('idx_agui_events_run_seq', 'run_id', 'sequence'),
        # 按消息 ID 查詢
        Index('idx_agui_events_message_id', 'message_id'),
        # 按工具調用 ID 查詢
        Index('idx_agui_events_tool_call_id', 'tool_call_id'),
    )
    
    def __repr__(self):
        return f"<AGUIEvent(id={self.id}, run={self.run_id}, type={self.event_type}, seq={self.sequence})>"
    
    def to_agui_dict(self) -> dict:
        """轉換為 AG-UI 兼容的字典格式（從 payload 解析）"""
        import json
        return json.loads(self.payload)


# 別名：更簡短的名稱
Event = AGUIEventLog
