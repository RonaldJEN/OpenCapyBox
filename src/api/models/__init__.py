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
from .llm_call_record import LLMCallRecord
from .agent_interaction import AgentInteraction
from .user_run_lock import UserRunLock
from .context_checkpoint import ContextCheckpoint
from .run_cancel_request import RunCancelRequest
from .channel_session_binding import ChannelSessionBinding
from .subagent_run import SubagentRun
from .auth_user import AuthUser
from .auth_login_event import AuthLoginEvent
from .admin_operation_log import AdminOperationLog
from .user_skill_inventory import UserSkillInventorySnapshot
from .llm_model import LLMModel, LLMModelSettings
from .model_permission import ModelPermissionGroup, ModelPermissionGroupModel, UserModelPermissionGroup
from .mcp import (
    McpServer,
    McpCredential,
    McpInstallation,
    McpToolVisibility,
    McpToolSnapshot,
    McpToolSearchIndex,
    McpConfigVersion,
)
from .tool_permission import ToolPermissionRule, ToolApprovalRequest, ToolPermissionAudit
from .sandbox_cleanup import SandboxCleanupJob
from .workspace import (
    UserWorkspace,
    WorkspaceChangeSet,
    WorkspaceClaim,
    WorkspaceContentObject,
    WorkspaceContentReference,
    WorkspaceEntry,
    WorkspaceFileVersion,
    WorkspaceMutation,
)

__all__ = [
    # 數據庫基礎
    "Base",
    "get_db",
    "init_db",
    
    # 原始命名（向後兼容）
    "Session",
    "Round",
    "AGUIEventLog",
    "LLMCallRecord",
    "AgentInteraction",
    "UserRunLock",
    "ContextCheckpoint",
    "RunCancelRequest",
    "ChannelSessionBinding",
    "SubagentRun",
    "AuthUser",
    "AuthLoginEvent",
    "AdminOperationLog",
    "UserSkillInventorySnapshot",
    "LLMModel",
    "LLMModelSettings",
    "ModelPermissionGroup",
    "ModelPermissionGroupModel",
    "UserModelPermissionGroup",
    "McpServer",
    "McpCredential",
    "McpInstallation",
    "McpToolVisibility",
    "McpToolSnapshot",
    "McpToolSearchIndex",
    "McpConfigVersion",
    "ToolPermissionRule",
    "ToolApprovalRequest",
    "ToolPermissionAudit",
    "SandboxCleanupJob",
    "UserWorkspace",
    "WorkspaceChangeSet",
    "WorkspaceClaim",
    "WorkspaceContentObject",
    "WorkspaceContentReference",
    "WorkspaceEntry",
    "WorkspaceFileVersion",
    "WorkspaceMutation",
    
    # AG-UI 命名（協議兼容）
    "Thread",  # = Session
    "Run",     # = Round
    "Event",   # = AGUIEventLog
]
