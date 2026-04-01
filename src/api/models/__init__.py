"""數據模型 - 支持 AG-UI 協議

AG-UI 概念映射：
- Thread (threadId) = Session（對話線程）
- Run (runId) = Round（執行回合）
- Event = AGUIEventLog（事件日誌，包含完整的步驟細節）

提供兩套命名：
- 原始命名：Session, Round, AGUIEventLog（向後兼容）
- AG-UI 命名：Thread, Run, Event（協議兼容）
"""
from .database import Base, get_db, init_db
from .session import Session, Thread  # Thread 是 Session 的別名
from .round import Round, Run  # Run 是 Round 的別名
from .agui_event import AGUIEventLog, Event  # Event 是 AGUIEventLog 的別名

__all__ = [
    # 數據庫基礎
    "Base",
    "get_db",
    "init_db",
    
    # 原始命名（向後兼容）
    "Session",
    "Round",
    "AGUIEventLog",
    
    # AG-UI 命名（協議兼容）
    "Thread",  # = Session
    "Run",     # = Round
    "Event",   # = AGUIEventLog
]
