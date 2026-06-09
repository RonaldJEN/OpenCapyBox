"""Agent 實例池管理服務

統一管理 Agent 實例的生命週期，包括：
- 緩存和複用 Agent 實例（按 chat_session_id）
- TTL 過期清理機制（整合沙箱 pause，僅在用戶無任何活躍 session 時 pause）
- 訪問時間追蹤
- 一用戶一沙箱：追蹤 user_id → {session_ids} 映射
"""

import asyncio
import logging
import time
from typing import Optional

from src.api.models.database import SessionLocal
from src.api.services.agent_service import AgentService
from src.api.services.history_service import HistoryService
from src.api.services.sandbox_service import get_sandbox_service

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

    def __new__(cls, ttl: int = 86400) -> "AgentPoolService":
        """單例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, ttl: int = 86400):
        """初始化 Agent 池

        Args:
            ttl: Agent 緩存 TTL（秒），默認 86400（24小時）
        """
        if self._initialized:
            return

        self._cache: dict[str, AgentService] = {}          # chat_session_id → AgentService
        self._last_access: dict[str, float] = {}            # chat_session_id → timestamp
        self._session_user: dict[str, str] = {}             # chat_session_id → user_id
        self._user_sessions: dict[str, set[str]] = {}       # user_id → {chat_session_id}
        self._agent_sandbox_ids: dict[str, str] = {}        # chat_session_id → sandbox_id
        self._last_renew: dict[str, float] = {}             # user_id → last renew timestamp
        self._create_locks: dict[str, asyncio.Lock] = {}    # chat_session_id → Lock
        self._user_creating: set[str] = set()               # 正在創建 Agent 的 user_id 集合
        self._ttl = ttl
        self._initialized = True

    @property
    def cache_size(self) -> int:
        """獲取當前緩存的 Agent 數量"""
        return len(self._cache)

    def _touch(self, chat_session_id: str) -> None:
        """更新 Agent 最後訪問時間"""
        self._last_access[chat_session_id] = time.time()

    @staticmethod
    def _normalize_sandbox_id(sandbox_id) -> str | None:
        """只接受 OpenSandbox 的字串 ID，避免 MagicMock 等測試對象誤參與比較。"""
        return sandbox_id if isinstance(sandbox_id, str) and sandbox_id else None

    @classmethod
    def _sandbox_id_from_agent(cls, agent_service) -> str | None:
        sandbox = getattr(agent_service, "sandbox", None)
        return cls._normalize_sandbox_id(getattr(sandbox, "id", None))

    def _cached_agent_sandbox_id(self, chat_session_id: str) -> str | None:
        cached_id = self._normalize_sandbox_id(self._agent_sandbox_ids.get(chat_session_id))
        if cached_id:
            return cached_id

        agent_service = self._cache.get(chat_session_id)
        return self._sandbox_id_from_agent(agent_service) if agent_service is not None else None

    def _resolve_current_sandbox_id(
        self,
        *,
        sandbox_service,
        user_id: str,
        db_sandbox_id: str | None,
    ) -> str | None:
        """解析當前用戶級 sandbox_id。

        DB 中的 sandbox_id 來自跨 worker 持久化狀態；當它與本進程快取衝突時，
        以 DB 值觸發 Agent 重建，避免繼續使用舊 sandbox。
        """
        service_sandbox_id = self._normalize_sandbox_id(sandbox_service.get_sandbox_id(user_id))
        db_sandbox_id = self._normalize_sandbox_id(db_sandbox_id)
        if service_sandbox_id and db_sandbox_id and service_sandbox_id != db_sandbox_id:
            return db_sandbox_id
        return service_sandbox_id or db_sandbox_id

    def _invalidate_sandbox_cache_if_stale(
        self,
        *,
        sandbox_service,
        user_id: str,
        current_sandbox_id: str | None,
    ) -> None:
        current_sandbox_id = self._normalize_sandbox_id(current_sandbox_id)
        service_sandbox_id = self._normalize_sandbox_id(sandbox_service.get_sandbox_id(user_id))
        if current_sandbox_id and service_sandbox_id and current_sandbox_id != service_sandbox_id:
            sandbox_service.invalidate_cache(user_id)

    def _cached_agent_is_stale(
        self,
        *,
        user_id: str,
        chat_session_id: str,
        sandbox_service,
        db_sandbox_id: str | None,
    ) -> tuple[bool, str | None, str | None]:
        cached_sandbox_id = self._cached_agent_sandbox_id(chat_session_id)
        current_sandbox_id = self._resolve_current_sandbox_id(
            sandbox_service=sandbox_service,
            user_id=user_id,
            db_sandbox_id=db_sandbox_id,
        )

        if not cached_sandbox_id or not current_sandbox_id or cached_sandbox_id == current_sandbox_id:
            return False, cached_sandbox_id, current_sandbox_id

        return True, cached_sandbox_id, current_sandbox_id

    def _invalidate_user_agents_with_different_sandbox(
        self,
        *,
        user_id: str,
        new_sandbox_id: str | None,
        keep_session_id: str,
    ) -> int:
        new_sandbox_id = self._normalize_sandbox_id(new_sandbox_id)
        if not new_sandbox_id:
            return 0

        running_session_ids = self._running_session_ids_for_user(user_id)
        removed = 0
        skipped_running = 0
        for session_id in list(self._user_sessions.get(user_id, set())):
            if session_id == keep_session_id:
                continue

            cached_sandbox_id = self._cached_agent_sandbox_id(session_id)
            if cached_sandbox_id and cached_sandbox_id != new_sandbox_id:
                if (
                    session_id in running_session_ids
                    or self._cached_agent_is_running(session_id)
                ):
                    skipped_running += 1
                    continue
                if self.remove(session_id):
                    removed += 1

        if removed:
            logger.warning(
                "用戶沙箱已切換，已失效同用戶舊 Agent 快取 (user=%s, sandbox_id=%s, sessions=%d)",
                user_id,
                new_sandbox_id,
                removed,
            )
        if skipped_running:
            logger.info(
                "用戶沙箱已切換，保留仍在運行的舊 Agent 快取等待懶失效 "
                "(user=%s, sandbox_id=%s, sessions=%d)",
                user_id,
                new_sandbox_id,
                skipped_running,
            )
        return removed

    def _cached_agent_is_running(self, chat_session_id: str) -> bool:
        agent_service = self._cache.get(chat_session_id)
        if agent_service is None:
            return False

        is_running = getattr(agent_service, "is_running", False)
        if isinstance(is_running, bool):
            return is_running

        active_count = getattr(agent_service, "_active_run_count", 0)
        return isinstance(active_count, int) and active_count > 0

    def _detach_running_agent(self, chat_session_id: str) -> AgentService | None:
        """Remove a still-running Agent from the hot cache without closing it.

        Abort releases the DB run lock before the old runner has necessarily
        reached its next cancellation checkpoint.  A fresh run for the same
        chat_session_id must not reuse that mutable Agent runtime, but closing
        it would break the old runner's in-flight cleanup.
        """
        agent_service = self._cache.pop(chat_session_id, None)
        if agent_service is None:
            return None
        self._last_access.pop(chat_session_id, None)
        self._agent_sandbox_ids.pop(chat_session_id, None)
        logger.warning(
            "Agent 仍在退出，已从热缓存摘出以隔离新 run: session=%s",
            chat_session_id,
        )
        return agent_service

    def _running_session_ids_for_user(self, user_id: str) -> set[str]:
        """Best-effort 查询当前仍持有运行 slot 的 session。"""
        from src.api.models.user_run_lock import UserRunLock

        try:
            with SessionLocal() as db:
                rows = (
                    db.query(UserRunLock.session_id)
                    .filter(UserRunLock.user_id == user_id)
                    .all()
                )
                db.rollback()
                return {row[0] for row in rows if row and row[0]}
        except Exception:
            logger.warning(
                "查詢運行中 session 失敗，保守保留用戶 Agent 快取: user=%s",
                user_id,
                exc_info=True,
            )
            return set(self._user_sessions.get(user_id, set()))

    @staticmethod
    def _user_has_fresh_run_lock(user_id: str) -> bool:
        """查詢 DB 中該用戶是否仍有新鮮的運行鎖（跨 worker 可見）。"""
        from datetime import timedelta

        from src.api.config import get_settings
        from src.api.models.user_run_lock import UserRunLock
        from src.api.utils.timezone import now_naive

        settings = get_settings()
        cutoff = now_naive() - timedelta(seconds=max(settings.sse_subscribe_timeout, 1))

        try:
            with SessionLocal() as db:
                row = (
                    db.query(UserRunLock.lock_id)
                    .filter(
                        UserRunLock.user_id == user_id,
                        UserRunLock.updated_at >= cutoff,
                    )
                    .first()
                )
                db.rollback()
                return row is not None
        except Exception:
            logger.warning(
                "檢查用戶運行鎖失敗，保守跳過暫停沙箱: user=%s",
                user_id,
                exc_info=True,
            )
            return True

    async def _sync_memory_to_sandbox(self, *, user_id: str, sandbox, force: bool = False) -> int:
        """Sync memory using short DB sessions so sandbox awaits do not hold request DB state."""
        from src.api.services.memory_service import (
            DB_BACKED_FILE_TYPES,
            FILE_TYPE_TO_FILENAME,
            MemoryService,
        )
        from src.api.services.sandbox_service import get_sandbox_mount_path

        with SessionLocal() as db:
            memory_svc = MemoryService(db)
            records = memory_svc.get_all_memory_files(user_id)
            agents_template = memory_svc.get_agents_template_content()
            db.rollback()

        if not records and not agents_template.strip():
            return 0

        mount = get_sandbox_mount_path()
        sync_items = []
        for file_type, db_content in records.items():
            if file_type not in DB_BACKED_FILE_TYPES:
                continue
            filename = FILE_TYPE_TO_FILENAME.get(file_type)
            if not filename:
                continue
            sync_items.append((file_type, filename, f"{mount}/{filename}", db_content))

        template_item = None
        if agents_template.strip():
            template_item = ("agents_md", "AGENTS.md", f"{mount}/AGENTS.md", agents_template)

        write_items = sync_items

        if not force and sync_items:
            read_results = await asyncio.gather(
                *(sandbox.files.read_file(path) for _, _, path, _ in sync_items),
                return_exceptions=True,
            )
            write_items = []
            for (file_type, filename, path, db_content), sandbox_content in zip(sync_items, read_results):
                if isinstance(sandbox_content, BaseException):
                    status = (
                        getattr(sandbox_content, "status_code", None)
                        or getattr(getattr(sandbox_content, "response", None), "status_code", None)
                        or getattr(sandbox_content, "status", None)
                    )
                    if status == 404 or isinstance(sandbox_content, FileNotFoundError):
                        write_items.append((file_type, filename, path, db_content))
                    else:
                        logger.warning("读取沙箱文件失败 (%s)，跳过同步: %s", filename, sandbox_content)
                    continue

                if sandbox_content and sandbox_content.strip():
                    if sandbox_content != db_content:
                        try:
                            with SessionLocal() as db:
                                try:
                                    MemoryService(db).upsert_memory_file(user_id, file_type, sandbox_content)
                                    db.commit()
                                except Exception:
                                    db.rollback()
                                    raise
                        except Exception as e:
                            logger.warning("同步记忆到沙箱失败 (%s): %s", filename, e)
                            continue
                        logger.info(
                            "沙箱优先：%s 已从沙箱回写 DB (%d chars)",
                            filename,
                            len(sandbox_content),
                        )
                    continue

                write_items.append((file_type, filename, path, db_content))

        if template_item is not None:
            write_items.append(template_item)

        write_results = await asyncio.gather(
            *(sandbox.files.write_file(path, db_content) for _, _, path, db_content in write_items),
            return_exceptions=True,
        )

        synced = 0
        for (_, filename, _, _), write_result in zip(write_items, write_results):
            if isinstance(write_result, BaseException):
                logger.warning("同步记忆到沙箱失败 (%s): %s", filename, write_result)
                continue
            synced += 1

        return synced

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
        sandbox_service = get_sandbox_service()
        sandbox_cache_invalidated = False

        # 先嘗試從緩存獲取（無鎖快速路徑）
        if chat_session_id in self._cache:
            stale, cached_sandbox_id, current_sandbox_id = self._cached_agent_is_stale(
                user_id=user_id,
                chat_session_id=chat_session_id,
                sandbox_service=sandbox_service,
                db_sandbox_id=sandbox_id,
            )
            if stale:
                logger.warning(
                    "Agent 快取 sandbox_id 已過期，移除並重建 "
                    "(user=%s, session=%s, cached=%s, current=%s)",
                    user_id,
                    chat_session_id,
                    cached_sandbox_id,
                    current_sandbox_id,
                )
                self._invalidate_sandbox_cache_if_stale(
                    sandbox_service=sandbox_service,
                    user_id=user_id,
                    current_sandbox_id=current_sandbox_id,
                )
                sandbox_cache_invalidated = True
                self.remove(chat_session_id)
            elif self._cached_agent_is_running(chat_session_id):
                self._detach_running_agent(chat_session_id)
            else:
                self._touch(chat_session_id)
                # 節流：每300秒才續租一次沙箱
                now = time.time()
                if now - self._last_renew.get(user_id, 0) > 300:
                    if not await sandbox_service.renew(user_id):
                        logger.warning(
                            "沙箱續租失敗，失效用戶所有 Agent 快取後重建 (user=%s, session=%s)",
                            user_id,
                            chat_session_id,
                        )
                        self.invalidate_user(user_id)
                    else:
                        self._last_renew[user_id] = now
                        if chat_session_id in self._cache:
                            return self._cache[chat_session_id]
                else:
                    return self._cache[chat_session_id]

        # 使用 per-session 鎖防止同一 Worker 內並發創建同一 session 的 Agent
        # 注意：此鎖僅作用於單進程（asyncio.Lock），跨 Worker 的唯一性保證依賴 DB 層約束
        lock = self._create_locks.setdefault(chat_session_id, asyncio.Lock())

        async with lock:
            # Double-check：取得鎖後再次確認緩存
            if chat_session_id in self._cache:
                stale, cached_sandbox_id, current_sandbox_id = self._cached_agent_is_stale(
                    user_id=user_id,
                    chat_session_id=chat_session_id,
                    sandbox_service=sandbox_service,
                    db_sandbox_id=sandbox_id,
                )
                if stale:
                    logger.warning(
                        "Agent 快取 sandbox_id 已過期，鎖內移除並重建 "
                        "(user=%s, session=%s, cached=%s, current=%s)",
                        user_id,
                        chat_session_id,
                        cached_sandbox_id,
                        current_sandbox_id,
                    )
                    self._invalidate_sandbox_cache_if_stale(
                        sandbox_service=sandbox_service,
                        user_id=user_id,
                        current_sandbox_id=current_sandbox_id,
                    )
                    sandbox_cache_invalidated = True
                    self._evict_agent(chat_session_id, drop_lock=False)
                elif self._cached_agent_is_running(chat_session_id):
                    self._detach_running_agent(chat_session_id)
                else:
                    self._touch(chat_session_id)
                    return self._cache[chat_session_id]

            if not sandbox_cache_invalidated:
                current_sandbox_id = self._resolve_current_sandbox_id(
                    sandbox_service=sandbox_service,
                    user_id=user_id,
                    db_sandbox_id=sandbox_id,
                )
                self._invalidate_sandbox_cache_if_stale(
                    sandbox_service=sandbox_service,
                    user_id=user_id,
                    current_sandbox_id=current_sandbox_id,
                )

            return await self._create_agent_instance(
                user_id=user_id,
                chat_session_id=chat_session_id,
                db=db,
                model_id=model_id,
                sandbox_id=sandbox_id,
            )

    async def _create_agent_instance(
        self,
        user_id: str,
        chat_session_id: str,
        db,
        model_id: str | None,
        sandbox_id: str | None,
    ) -> "AgentService":
        """創建 Agent 實例的內部實現（已在 per-session 鎖內部）"""

        # ★ 提前注冊 session 映射，防止 cleanup 誤判用戶無活躍 session 而暫停沙箱
        self._touch(chat_session_id)
        self._session_user[chat_session_id] = user_id
        self._user_sessions.setdefault(user_id, set()).add(chat_session_id)
        self._user_creating.add(user_id)

        try:
            return await self._do_create_agent(
                user_id=user_id,
                chat_session_id=chat_session_id,
                db=db,
                model_id=model_id,
                sandbox_id=sandbox_id,
            )
        except Exception:
            # 創建失敗：回滾提前注冊的映射
            self._last_access.pop(chat_session_id, None)
            self._session_user.pop(chat_session_id, None)
            self._agent_sandbox_ids.pop(chat_session_id, None)
            if user_id in self._user_sessions:
                self._user_sessions[user_id].discard(chat_session_id)
                if not self._user_sessions[user_id]:
                    del self._user_sessions[user_id]
            raise
        finally:
            self._user_creating.discard(user_id)

    async def _do_create_agent(
        self,
        user_id: str,
        chat_session_id: str,
        db,
        model_id: str | None,
        sandbox_id: str | None,
    ) -> "AgentService":
        """實際創建 Agent 的邏輯（被 _create_agent_instance 包裝）"""
        from src.api.services.agent_service import AgentService

        # 創建/恢復用戶級沙箱，並在同一 user lifecycle lock 內持久化本次 sandbox.id。
        sandbox_service = get_sandbox_service()
        sandbox, new_sandbox_id = await sandbox_service.get_or_resume_with_persisted_id(user_id, sandbox_id)

        # 后续 Agent metadata / cache 绑定只使用本次返回对象的 sandbox.id，
        # 不再从 mutable sandbox_service cache 二次读取，避免并发初始化错绑。
        new_sandbox_id = self._normalize_sandbox_id(new_sandbox_id)
        if new_sandbox_id:
            self._invalidate_user_agents_with_different_sandbox(
                user_id=user_id,
                new_sandbox_id=new_sandbox_id,
                keep_session_id=chat_session_id,
            )

        history_service = HistoryService(SessionLocal)

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

        try:
            logger.info("正在初始化 Agent (session=%s, user=%s)...", chat_session_id, user_id)
            await agent_service.initialize_agent()
            logger.info("Agent 初始化成功 (session=%s)", chat_session_id)
        except Exception:
            try:
                agent_service.close()
            except Exception:
                pass
            raise

        # 将 DB 记忆同步到沙箱
        try:
            await self._sync_memory_to_sandbox(user_id=user_id, sandbox=sandbox)
        except Exception as e:
            logger.warning("沙箱记忆同步失败（非致命）: %s", e)

        # 存入緩存（session 映射已在 _create_agent_instance 提前注冊）
        self._cache[chat_session_id] = agent_service
        if new_sandbox_id:
            self._agent_sandbox_ids[chat_session_id] = new_sandbox_id
        else:
            self._agent_sandbox_ids.pop(chat_session_id, None)
        self._touch(chat_session_id)

        return agent_service

    def _evict_agent(self, chat_session_id: str, *, drop_lock: bool = True) -> bool:
        removed = False
        if chat_session_id in self._cache:
            agent_svc = self._cache[chat_session_id]
            # 清理該 Agent 實例持有的後台命令追蹤
            try:
                for tool in getattr(agent_svc.agent, 'tools', {}).values():
                    tracker = getattr(tool, '_tracker', None)
                    if tracker and hasattr(tracker, 'cleanup_by_sandbox'):
                        tracker.cleanup_by_sandbox(agent_svc.sandbox)
                        break  # 三個 bash 工具共享同一個 tracker，清理一次即可
            except Exception:
                pass
            try:
                agent_svc.close()
            except Exception:
                pass
            del self._cache[chat_session_id]
            removed = True
            logger.info("已移除 Agent 緩存: %s", chat_session_id)

        if chat_session_id in self._last_access:
            del self._last_access[chat_session_id]

        self._agent_sandbox_ids.pop(chat_session_id, None)

        if drop_lock:
            self._create_locks.pop(chat_session_id, None)

        # 更新 user ↔ session 映射
        user_id = self._session_user.pop(chat_session_id, None)
        if user_id and user_id in self._user_sessions:
            self._user_sessions[user_id].discard(chat_session_id)
            if not self._user_sessions[user_id]:
                del self._user_sessions[user_id]
                self._last_renew.pop(user_id, None)

        return removed

    def remove(self, chat_session_id: str) -> bool:
        """移除 Agent 實例

        Args:
            chat_session_id: 對話會話 ID

        Returns:
            是否成功移除
        """
        return self._evict_agent(chat_session_id)

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
            # ★ 再次檢查：在 await 間隙可能有新 session 被注冊
            if user_id in self._user_creating:
                logger.info("用戶正在創建 Agent，跳過暫停沙箱: user=%s", user_id)
                continue
            current_sessions = self._user_sessions.get(user_id, set())
            if current_sessions:
                logger.info("用戶已有新活躍 session，跳過暫停沙箱: user=%s", user_id)
                continue
            if self._user_has_fresh_run_lock(user_id):
                logger.info("其他 worker 仍有活躍運行鎖，跳過暫停沙箱: user=%s", user_id)
                continue
            await sandbox_service.pause(user_id)
            logger.info("用戶所有 session 均過期，暫停沙箱: user=%s", user_id)

        return expired_sessions

    def clear_all(self) -> int:
        """清空所有 Agent 緩存

        Returns:
            清理的 Agent 數量
        """
        count = len(self._cache)
        for session_id in list(self._cache.keys()):
            self._evict_agent(session_id)

        # 兜底清理尚未進入 cache 的占位 / metadata。
        self._cache.clear()
        self._last_access.clear()
        self._session_user.clear()
        self._user_sessions.clear()
        self._agent_sandbox_ids.clear()
        self._last_renew.clear()
        self._create_locks.clear()
        self._user_creating.clear()
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
            if session_id not in self._cache:
                continue
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
                    "sandbox_id": self._cached_agent_sandbox_id(session_id),
                    "last_access": last_access,
                    "age_seconds": int(current_time - last_access),
                    "expires_in": max(0, int(self._ttl - (current_time - last_access))),
                }
                for session_id, last_access in self._last_access.items()
            }
        }


# 全局單例
_agent_pool: Optional[AgentPoolService] = None


def get_agent_pool(ttl: int = 86400) -> AgentPoolService:
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
