"""Agent 實例池管理服務

統一管理 Agent 實例的生命週期，包括：
- 緩存和複用 Agent 實例（按 chat_session_id）
- TTL 過期清理機制（整合沙箱 pause，僅在用戶無任何活躍 session 時 pause）
- 訪問時間追蹤
- 一用戶一沙箱：追蹤 user_id → {session_ids} 映射
"""

import logging
import time
from typing import Optional

from src.api.services.agent_service import AgentService
from src.api.services.history_service import HistoryService
from src.api.services.sandbox_service import get_sandbox_service
from src.api.config import get_settings

logger = logging.getLogger(__name__)


class AgentPoolService:
    """Agent 實例池管理器

    單例模式管理所有 Agent 實例，提供 TTL 過期清理機制。

    架構說明（一用戶一沙箱）：
    - _cache: chat_session_id → AgentService（每個對話一個 Agent 實例）
    - _session_user: chat_session_id → user_id（反查用戶）
    - _user_sessions: user_id → {chat_session_id, ...}（用戶活躍的所有 session）
    - TTL 過期時：僅當某用戶所有 session 都過期，才暫停該用戶的沙箱

    使用方式:
        pool = AgentPoolService()
        agent = await pool.get_or_create(user_id, session_id, chat_session_id, db)

        # 在適當時機調用清理
        await pool.cleanup_expired_async()
    """

    _instance: Optional["AgentPoolService"] = None

    def __new__(cls, ttl: int = 3600) -> "AgentPoolService":
        """單例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, ttl: int = 3600):
        """初始化 Agent 池

        Args:
            ttl: Agent 緩存 TTL（秒），默認 3600（1小時）
        """
        if self._initialized:
            return

        self._cache: dict[str, AgentService] = {}          # chat_session_id → AgentService
        self._last_access: dict[str, float] = {}            # chat_session_id → timestamp
        self._session_user: dict[str, str] = {}             # chat_session_id → user_id
        self._user_sessions: dict[str, set[str]] = {}       # user_id → {chat_session_id}
        self._last_renew: dict[str, float] = {}             # user_id → last renew timestamp
        self._ttl = ttl
        self._initialized = True

    @property
    def cache_size(self) -> int:
        """獲取當前緩存的 Agent 數量"""
        return len(self._cache)

    def _touch(self, chat_session_id: str) -> None:
        """更新 Agent 最後訪問時間"""
        self._last_access[chat_session_id] = time.time()

    def get(self, chat_session_id: str) -> Optional[AgentService]:
        """獲取緩存的 Agent 實例（不創建）

        Args:
            chat_session_id: 對話會話 ID

        Returns:
            AgentService 或 None（如果不存在）
        """
        if chat_session_id in self._cache:
            self._touch(chat_session_id)
            return self._cache[chat_session_id]
        return None

    async def get_or_create(
        self,
        user_id: str,
        session_id: str,
        chat_session_id: str,
        db,
        model_id: str | None = None,
        sandbox_id: str | None = None,
    ) -> AgentService:
        """獲取或創建 Agent 實例（整合沙箱生命週期）

        Args:
            user_id: 用戶 ID（用於查找/創建用戶級沙箱）
            session_id: 用戶 session ID
            chat_session_id: 對話會話 ID
            db: 數據庫會話
            model_id: 模型 ID（來自 Model Registry，可選）
            sandbox_id: 從 UserSandbox 表讀取的 sandbox_id（用於 resume）

        Returns:
            初始化完成的 AgentService 實例

        Raises:
            Exception: Agent 初始化失敗時拋出
        """
        # 先嘗試從緩存獲取
        if chat_session_id in self._cache:
            self._touch(chat_session_id)
            # 節流：每300秒才續租一次沙箱
            now = time.time()
            if now - self._last_renew.get(user_id, 0) > 300:
                sandbox_service = get_sandbox_service()
                if not await sandbox_service.renew(user_id):
                    logger.warning("沙箱續租失敗，移除快取重建 (user=%s, session=%s)", user_id, chat_session_id)
                    self.remove(chat_session_id)
                else:
                    self._last_renew[user_id] = now
                    return self._cache[chat_session_id]
            else:
                return self._cache[chat_session_id]

        # 創建/恢復用戶級沙箱
        sandbox_service = get_sandbox_service()
        sandbox = await sandbox_service.get_or_resume(user_id, sandbox_id)

        # 將 sandbox_id 存入 UserSandbox 表
        new_sandbox_id = sandbox_service.get_sandbox_id(user_id)
        if new_sandbox_id:
            from src.api.models.user_sandbox import UserSandbox
            import uuid
            user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
            if user_sandbox:
                if user_sandbox.sandbox_id != new_sandbox_id:
                    user_sandbox.sandbox_id = new_sandbox_id
                    db.commit()
            else:
                user_sandbox = UserSandbox(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    sandbox_id=new_sandbox_id,
                    status="active",
                )
                db.add(user_sandbox)
                db.commit()

        history_service = HistoryService(db)

        # 在沙箱中創建會話工作目錄（bash 的 working_directory 依賴此目錄存在）
        from src.api.services.sandbox_service import get_sandbox_mount_path
        session_workspace = f"{get_sandbox_mount_path()}/sessions/{chat_session_id}"
        try:
            await sandbox.commands.run(f"mkdir -p {session_workspace}")
        except Exception as e:
            logger.warning("沙箱會話目錄創建失敗（bash 可能不可用）: %s", e)

        agent_service = AgentService(
            sandbox=sandbox,
            history_service=history_service,
            session_id=chat_session_id,
            user_id=user_id,
            model_id=model_id,
        )

        logger.info("正在初始化 Agent (session=%s, user=%s)...", chat_session_id, user_id)
        await agent_service.initialize_agent()
        logger.info("Agent 初始化成功 (session=%s)", chat_session_id)

        # 新用户：将 DB 记忆同步到沙箱 + 写入沙箱独有模板（如 BOOTSTRAP.md）
        try:
            from src.api.services.memory_service import MemoryService
            mem_svc = MemoryService(db)
            await mem_svc.sync_to_sandbox(user_id, sandbox)
            await mem_svc.provision_sandbox_templates(user_id, sandbox)
        except Exception as e:
            logger.warning("沙箱记忆同步/模板写入失败（非致命）: %s", e)

        # 存入緩存，建立 user ↔ session 映射
        self._cache[chat_session_id] = agent_service
        self._touch(chat_session_id)
        self._session_user[chat_session_id] = user_id
        self._user_sessions.setdefault(user_id, set()).add(chat_session_id)

        return agent_service

    def remove(self, chat_session_id: str) -> bool:
        """移除 Agent 實例

        Args:
            chat_session_id: 對話會話 ID

        Returns:
            是否成功移除
        """
        removed = False
        if chat_session_id in self._cache:
            # 清理該 Agent 實例持有的後台命令追蹤
            try:
                agent_svc = self._cache[chat_session_id]
                for tool in getattr(agent_svc.agent, 'tools', {}).values():
                    tracker = getattr(tool, '_tracker', None)
                    if tracker and hasattr(tracker, 'cleanup_by_sandbox'):
                        tracker.cleanup_by_sandbox(agent_svc.sandbox)
                        break  # 三個 bash 工具共享同一個 tracker，清理一次即可
            except Exception:
                pass
            del self._cache[chat_session_id]
            removed = True
            logger.info("已移除 Agent 緩存: %s", chat_session_id)

        if chat_session_id in self._last_access:
            del self._last_access[chat_session_id]

        # 更新 user ↔ session 映射
        user_id = self._session_user.pop(chat_session_id, None)
        if user_id and user_id in self._user_sessions:
            self._user_sessions[user_id].discard(chat_session_id)
            if not self._user_sessions[user_id]:
                del self._user_sessions[user_id]
                self._last_renew.pop(user_id, None)

        return removed

    def cleanup_expired(self) -> list[str]:
        """清理過期的 Agent 實例（同步版本，標記待清理）

        注意：沙箱 pause 是異步操作，這裡只做同步清理。
        實際的沙箱 pause 需要在異步上下文中調用 cleanup_expired_async()。

        Returns:
            被清理的 session ID 列表
        """
        current_time = time.time()
        expired_sessions = [
            session_id
            for session_id, last_access in self._last_access.items()
            if current_time - last_access > self._ttl
        ]

        for session_id in expired_sessions:
            self.remove(session_id)
            logger.info("清理過期 Agent 緩存: %s", session_id)

        return expired_sessions

    async def cleanup_expired_async(self) -> list[str]:
        """異步清理過期的 Agent 實例（含沙箱 pause）

        TTL 邏輯（一用戶一沙箱版本）：
        - 移除過期 session 的 Agent 緩存
        - 僅當某用戶所有 session 均過期，才暫停該用戶的沙箱
        - 避免誤 pause 仍有活躍 session 的用戶沙箱

        Returns:
            被清理的 session ID 列表
        """
        current_time = time.time()
        expired_sessions = [
            session_id
            for session_id, last_access in self._last_access.items()
            if current_time - last_access > self._ttl
        ]

        # 統計哪些用戶的所有 session 均已過期（需要 pause 沙箱）
        expired_set = set(expired_sessions)
        users_to_pause: set[str] = set()
        for session_id in expired_sessions:
            user_id = self._session_user.get(session_id)
            if user_id:
                user_active_sessions = self._user_sessions.get(user_id, set())
                if user_active_sessions.issubset(expired_set):
                    users_to_pause.add(user_id)

        sandbox_service = get_sandbox_service()
        for session_id in expired_sessions:
            self.remove(session_id)
            logger.info("清理過期 Agent 緩存: %s", session_id)

        for user_id in users_to_pause:
            await sandbox_service.pause(user_id)
            logger.info("用戶所有 session 均過期，暫停沙箱: user=%s", user_id)

        return expired_sessions

    def clear_all(self) -> int:
        """清空所有 Agent 緩存

        Returns:
            清理的 Agent 數量
        """
        count = len(self._cache)
        self._cache.clear()
        self._last_access.clear()
        self._session_user.clear()
        self._user_sessions.clear()
        self._last_renew.clear()
        logger.info("已清空所有 Agent 緩存（共 %d 個）", count)
        return count

    def invalidate_user(self, user_id: str) -> int:
        """移除某个用户的所有 Agent 缓存。

        用于用户更新 AGENTS/SOUL/USER 等配置后，确保下一次请求
        会重新初始化 Agent 并加载最新 system prompt。

        Args:
            user_id: 用户 ID

        Returns:
            实际移除的 session 数量
        """
        session_ids = list(self._user_sessions.get(user_id, set()))
        removed = 0
        for session_id in session_ids:
            if self.remove(session_id):
                removed += 1

        if removed:
            logger.info("已失效用户 Agent 缓存: user=%s, sessions=%d", user_id, removed)
        return removed

    def get_stats(self) -> dict:
        """獲取緩存統計信息

        Returns:
            包含緩存狀態的字典
        """
        current_time = time.time()
        return {
            "total_cached": len(self._cache),
            "ttl_seconds": self._ttl,
            "active_users": len(self._user_sessions),
            "sessions": {
                session_id: {
                    "user_id": self._session_user.get(session_id),
                    "last_access": last_access,
                    "age_seconds": int(current_time - last_access),
                    "expires_in": max(0, int(self._ttl - (current_time - last_access))),
                }
                for session_id, last_access in self._last_access.items()
            }
        }


# 全局單例
_agent_pool: Optional[AgentPoolService] = None


def get_agent_pool(ttl: int = 3600) -> AgentPoolService:
    """獲取全局 Agent 池實例

    Args:
        ttl: Agent 緩存 TTL（秒），僅首次調用時生效

    Returns:
        AgentPoolService 單例
    """
    global _agent_pool
    if _agent_pool is None:
        _agent_pool = AgentPoolService(ttl=ttl)
    return _agent_pool
