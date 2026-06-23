"""OpenSandbox 會話服務

管理沙箱的完整生命週期：
- create: 首次創建沙箱（用戶首次使用）
- get_or_resume: 獲取已連接的沙箱實例（從記憶體快取）
- pause: 暫停沙箱（TTL 過期且用戶無任何活躍 session 時）
- kill: 銷毀沙箱（用戶刪除時）
- push_skill: 將 skills 資源推送到沙箱

架构（一用户一沙箱）：
  user_id → sandbox（持久化工作空間 /home/user）
    ├── USER.md / SOUL.md / MEMORY.md / AGENTS.md（平台模板）
    └── sessions/{session_id}/   ← 各對話隔離子目錄

  Agent Server (本機) ←→ OpenSandbox Server (遠端)
  文件全部存在沙箱中，Agent Server 僅作為代理。
"""

import asyncio
import logging
import re
import hashlib
import posixpath
import shlex
from datetime import timedelta
from typing import Optional
from pathlib import Path

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import Volume, Host
from sqlalchemy.exc import IntegrityError

from src.api.config import get_settings
from src.api.services.sandbox_profile_service import SandboxRuntimeConfig

logger = logging.getLogger(__name__)
settings = get_settings()


def _normalize_mount_path(mount_path: str | None) -> str:
    if not isinstance(mount_path, str) or not mount_path.startswith("/"):
        mount_path = "/home/user"
    normalized = posixpath.normpath(mount_path)
    return normalized if normalized.startswith("/") else "/home/user"


def get_sandbox_mount_path(mount_path: str | None = None) -> str:
    """獲取容器內沙箱工作根目錄（標準化）。"""
    return _normalize_mount_path(mount_path or getattr(settings, "sandbox_storage_mount_path", "/home/user"))


def resolve_sandbox_path(path: str, mount_path: str | None = None) -> str:
    """將相對/絕對路徑解析為沙箱內絕對路徑。"""
    base = mount_path or get_sandbox_mount_path()
    if not path:
        return base
    if path.startswith("/"):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(base, path))


def is_within_sandbox_root(path: str, mount_path: str | None = None) -> bool:
    """判斷路徑是否位於沙箱根目錄內。"""
    base = mount_path or get_sandbox_mount_path()
    normalized_path = posixpath.normpath(path)
    return normalized_path == base or normalized_path.startswith(f"{base}/")


def to_sandbox_relative_path(path: str, mount_path: str | None = None) -> str | None:
    """將絕對路徑轉為相對於沙箱根目錄的路徑。"""
    base = mount_path or get_sandbox_mount_path()
    normalized_path = posixpath.normpath(path)
    if normalized_path == base:
        return ""
    prefix = f"{base}/"
    if not normalized_path.startswith(prefix):
        return None
    return normalized_path[len(prefix):]


def _extract_command_exit_code(execution) -> int:
    exit_code = getattr(execution, "exit_code", None)
    if isinstance(exit_code, int):
        return exit_code
    return 1 if getattr(execution, "error", None) else 0


def _build_connection_config(runtime_config: SandboxRuntimeConfig | None = None) -> ConnectionConfig:
    """構建 OpenSandbox 連接配置（從 Settings 讀取）"""
    api_key = runtime_config.api_key if runtime_config else settings.sandbox_api_key
    use_server_proxy = (
        runtime_config.use_server_proxy if runtime_config else settings.sandbox_use_server_proxy
    )
    return ConnectionConfig(
        domain=runtime_config.domain if runtime_config else settings.sandbox_domain,
        api_key=api_key,
        protocol=runtime_config.protocol if runtime_config else settings.sandbox_protocol,
        request_timeout=timedelta(seconds=60),
        use_server_proxy=use_server_proxy,
        # Workaround: HealthAdapter 在 use_server_proxy=True 時不會自動帶認證頭
        headers={"OPEN-SANDBOX-API-KEY": api_key} if use_server_proxy else {},
    )


def _runtime_config_from_settings() -> SandboxRuntimeConfig:
    return SandboxRuntimeConfig(
        profile_id="env-default",
        profile_name="默认沙箱",
        profile_version=1,
        profile_source="default",
        domain=settings.sandbox_domain,
        protocol=settings.sandbox_protocol,
        api_key=settings.sandbox_api_key,
        use_server_proxy=bool(settings.sandbox_use_server_proxy),
        mount_path=settings.sandbox_storage_mount_path,
    )


class SandboxSessionService:
    """OpenSandbox 會話服務（一用戶一沙箱）

    以 user_id 為 key 管理沙箱實例的生命週期。
    每個用戶的所有對話（session）共享同一個沙箱工作空間，
    各 session 在沙箱內使用 /home/user/sessions/{session_id}/ 子目錄隔離。

    使用方式:
        service = SandboxSessionService()

        # 獲取或恢復用戶的沙箱
        sandbox = await service.get_or_resume(user_id, sandbox_id)

        # TTL 過期且無任何活躍 session → 暫停沙箱
        await service.pause(user_id)

        # 用戶刪除 → 銷毀沙箱
        await service.kill(user_id, sandbox_id)
    """

    _instance: Optional["SandboxSessionService"] = None

    def __new__(cls) -> "SandboxSessionService":
        """單例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._cache: dict[str, Sandbox] = {}         # user_id → Sandbox
        self._cache_profile_ids: dict[str, str] = {}  # user_id → sandbox_profile_id
        self._cache_profile_versions: dict[str, int] = {}  # user_id → profile.version
        self._cache_mount_paths: dict[str, str] = {}  # user_id → active mount path
        self._pushed_skills: dict[str, set[str]] = {}  # user_id → pushed skill names
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}  # user_id → sandbox lifecycle lock
        self._initialized = True

    def _get_lifecycle_lock(self, user_id: str) -> asyncio.Lock:
        """Return the per-user sandbox lifecycle lock for this process."""
        return self._lifecycle_locks.setdefault(user_id, asyncio.Lock())

    @staticmethod
    def _sandbox_id_from_instance(sandbox: Sandbox | None) -> str | None:
        sandbox_id = getattr(sandbox, "id", None)
        return sandbox_id if isinstance(sandbox_id, str) and sandbox_id else None

    @staticmethod
    def _collect_skill_files(
        skills_path: Path,
        root_path: Path,
        mount_path: str | None = None,
    ) -> list[tuple[str, bytes]]:
        skills_base = posixpath.join(get_sandbox_mount_path(mount_path), "skills")
        files_to_push: list[tuple[str, bytes]] = []
        for skill_file in skills_path.rglob("*"):
            if not skill_file.is_file():
                continue

            rel_parts = skill_file.relative_to(root_path).parts
            skip_dirs = {"node_modules", "__pycache__", ".git", ".venv", "venv"}
            if any(part in skip_dirs for part in rel_parts):
                continue

            rel_path = str(skill_file.relative_to(root_path)).replace("\\", "/")
            sandbox_path = posixpath.join(skills_base, rel_path)

            try:
                content = skill_file.read_bytes()
                files_to_push.append((sandbox_path, content))
            except Exception as e:
                logger.debug("跳過無法讀取的檔案 %s: %s", skill_file, e)

        return files_to_push

    @staticmethod
    def _extract_skill_name_from_skill_md(text: str) -> str | None:
        """從 SKILL.md frontmatter 中提取 name。"""
        normalized = text.lstrip("\ufeff")
        if not normalized.startswith("---"):
            return None

        end_idx = normalized.find("\n---", 3)
        if end_idx == -1:
            return None

        frontmatter = normalized[3:end_idx]
        match = re.search(r"^\s*name\s*:\s*['\"]?([^'\"\n]+)['\"]?\s*$", frontmatter, flags=re.MULTILINE)
        if not match:
            return None
        return match.group(1).strip()

    @staticmethod
    def _user_storage_host_path(user_id: str) -> str:
        """根據 user_id 生成穩定且安全的宿主機持久化路徑。"""
        root = settings.sandbox_host_storage_root
        if not isinstance(root, str) or not root.startswith("/"):
            root = "/tmp/sandbox"

        normalized_root = root.rstrip("/")
        hashed = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:16]
        safe_user = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)[:64]
        return f"{normalized_root}/user-{safe_user}-{hashed}"

    @staticmethod
    def _build_persistent_volumes(
        user_id: str,
        runtime_config: SandboxRuntimeConfig | None = None,
    ) -> list[Volume] | None:
        enabled = getattr(settings, "sandbox_persistent_storage_enabled", True)
        if not enabled:
            return None

        mount_path = get_sandbox_mount_path(runtime_config.mount_path if runtime_config else None)

        host_path = SandboxSessionService._user_storage_host_path(user_id)
        volume_name = f"user-{hashlib.sha1(user_id.encode('utf-8')).hexdigest()[:12]}"

        return [
            Volume(
                name=volume_name,
                host=Host(path=host_path),
                mount_path=mount_path,
                read_only=False,
            )
        ]

    @staticmethod
    def _resolve_runtime_config(user_id: str) -> SandboxRuntimeConfig:
        try:
            from fastapi import HTTPException
            from src.api.models.database import SessionLocal
            from src.api.services.sandbox_profile_service import resolve_sandbox_runtime_config

            with SessionLocal() as db:
                return resolve_sandbox_runtime_config(db, user_id)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("沙箱 profile 解析失败 (user=%s)", user_id, exc_info=True)
            raise RuntimeError("沙箱 profile 解析失败") from exc

    @staticmethod
    def _resolve_runtime_for_existing_sandbox(
        user_id: str,
        sandbox_id: str | None,
    ) -> SandboxRuntimeConfig:
        if not sandbox_id:
            raise RuntimeError("缺少 sandbox_id，无法解析既有沙箱 profile")
        try:
            from src.api.models.database import SessionLocal
            from src.api.models.sandbox_profile import SandboxProfile
            from src.api.models.user_sandbox import UserSandbox
            from src.api.services.sandbox_profile_service import runtime_config_from_profile

            with SessionLocal() as db:
                user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                if not user_sandbox:
                    raise RuntimeError("既有沙箱记录不存在")
                if user_sandbox.sandbox_id != sandbox_id:
                    raise RuntimeError("既有沙箱记录与待清理 sandbox_id 不一致")
                profile_id = user_sandbox.active_profile_id
                profile_version = user_sandbox.active_profile_version
                if not profile_id or profile_version is None:
                    raise RuntimeError("既有沙箱缺少 active profile 指纹")
                profile = db.query(SandboxProfile).filter(SandboxProfile.id == profile_id).first()
                if not profile:
                    raise RuntimeError("既有沙箱 active profile 不存在")
                if int(profile.version or 0) != int(profile_version or 0):
                    raise RuntimeError("既有沙箱 active profile 版本与当前 Profile 不一致")
                return runtime_config_from_profile(profile, "active")
        except Exception as exc:
            logger.warning(
                "解析既有沙箱 profile 失败，拒绝回退到当前用户配置 (user=%s, sandbox_id=%s)",
                user_id,
                sandbox_id,
                exc_info=True,
            )
            raise RuntimeError("既有沙箱 profile 解析失败") from exc

    def _store_cache(
        self,
        user_id: str,
        sandbox: Sandbox,
        runtime_config: SandboxRuntimeConfig,
    ) -> None:
        self._cache[user_id] = sandbox
        self._cache_profile_ids[user_id] = runtime_config.profile_id
        self._cache_profile_versions[user_id] = runtime_config.profile_version
        self._cache_mount_paths[user_id] = get_sandbox_mount_path(runtime_config.mount_path)
        self._pushed_skills.setdefault(user_id, set())

    def _cache_matches_runtime(
        self,
        user_id: str,
        runtime_config: SandboxRuntimeConfig,
        sandbox_id: str | None = None,
    ) -> bool:
        sandbox = self._cache.get(user_id)
        if not sandbox:
            return False
        cached_sandbox_id = self._sandbox_id_from_instance(sandbox)
        if sandbox_id and cached_sandbox_id != sandbox_id:
            return False
        cached_profile_id = self._cache_profile_ids.get(user_id)
        cached_profile_version = self._cache_profile_versions.get(user_id)
        if cached_profile_id is None and cached_profile_version is None:
            return runtime_config.profile_id == "env-default"
        return (
            cached_profile_id == runtime_config.profile_id
            and cached_profile_version == runtime_config.profile_version
        )

    @staticmethod
    def _persisted_profile_matches_runtime(
        user_id: str,
        sandbox_id: str | None,
        runtime_config: SandboxRuntimeConfig,
    ) -> bool:
        if not sandbox_id:
            return False
        try:
            from src.api.models.database import SessionLocal
            from src.api.models.user_sandbox import UserSandbox

            with SessionLocal() as db:
                user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                if not user_sandbox or user_sandbox.sandbox_id != sandbox_id:
                    return False
                return (
                    user_sandbox.active_profile_id == runtime_config.profile_id
                    and int(user_sandbox.active_profile_version or 0) == runtime_config.profile_version
                )
        except Exception:
            logger.warning("读取持久化 sandbox profile 指纹失败 (user=%s)", user_id, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # 核心生命週期方法
    # ------------------------------------------------------------------

    async def create(self, user_id: str) -> Sandbox:
        """創建新沙箱

        Args:
            user_id: 用戶 ID（作為快取 key）

        Returns:
            已就緒的 Sandbox 實例

        Raises:
            RuntimeError: 沙箱創建失敗
        """
        runtime_config = self._resolve_runtime_config(user_id)
        connection_config = _build_connection_config(runtime_config)
        logger.info(
            "正在創建沙箱 (user=%s, profile=%s)...",
            user_id,
            runtime_config.profile_name,
        )

        try:
            volumes = self._build_persistent_volumes(user_id, runtime_config)
            sandbox = await Sandbox.create(
                settings.sandbox_image,
                connection_config=connection_config,
                timeout=timedelta(minutes=settings.sandbox_timeout_minutes),
                ready_timeout=timedelta(seconds=settings.sandbox_ready_timeout_seconds),
                health_check_polling_interval=timedelta(seconds=2),
                volumes=volumes,
            )
            self._store_cache(user_id, sandbox, runtime_config)
            logger.info(
                "沙箱創建成功 (user=%s, profile=%s, sandbox_id=%s)",
                user_id,
                runtime_config.profile_name,
                sandbox.id,
            )
            return sandbox

        except Exception as e:
            logger.error("沙箱創建失敗 (user=%s): %s", user_id, e, exc_info=True)
            raise RuntimeError(f"沙箱創建失敗: {e}") from e

    async def _query_sandbox_state(
        self,
        sandbox_id: str,
        runtime_config: SandboxRuntimeConfig | None = None,
    ) -> str:
        """查詢沙箱狀態（不經過容器，直接問 OpenSandbox API）

        Returns:
            小寫狀態字串，如 "running", "paused", "terminated" 等。
            查詢失敗時返回空字串。
        """
        try:
            from opensandbox.manager import SandboxManager
            connection_config = _build_connection_config(runtime_config)
            async with await SandboxManager.create(connection_config=connection_config) as manager:
                info = await manager.get_sandbox_info(sandbox_id)
                state = str(getattr(getattr(info, "status", None), "state", "")).lower()
            logger.debug("沙箱狀態查詢結果 (sandbox_id=%s): %s", sandbox_id, state)
            return state
        except Exception as e:
            logger.debug("查詢沙箱狀態失敗 (sandbox_id=%s): %s", sandbox_id, e)
            return ""

    async def get_or_resume(
        self, user_id: str, sandbox_id: str | None = None
    ) -> Sandbox:
        """獲取沙箱實例（按 user_id 在本進程內串行化生命週期）。"""
        async with self._get_lifecycle_lock(user_id):
            return await self._get_or_resume_unlocked(user_id, sandbox_id)

    async def get_or_resume_with_persisted_id(
        self, user_id: str, sandbox_id: str | None = None
    ) -> tuple[Sandbox, str | None]:
        """獲取沙箱並在同一 user lifecycle lock 內持久化本次 sandbox.id。

        調用方應使用返回的 sandbox_id 綁定 Agent / metadata / DB 狀態，
        避免拿到 sandbox 後再讀 mutable cache 造成錯綁。
        """
        async with self._get_lifecycle_lock(user_id):
            sandbox = await self._get_or_resume_unlocked(user_id, sandbox_id)
            current_sandbox_id = self._sandbox_id_from_instance(sandbox)
            runtime_config = self.get_cached_runtime_config(user_id) or self._resolve_runtime_config(user_id)
            if current_sandbox_id:
                self._upsert_user_sandbox_id(user_id, current_sandbox_id, runtime_config=runtime_config)
                self._store_cache(user_id, sandbox, runtime_config)
            return sandbox, current_sandbox_id

    async def _get_or_resume_unlocked(
        self, user_id: str, sandbox_id: str | None = None
    ) -> Sandbox:
        """獲取沙箱實例（先快取 → connect → resume → create）

        Args:
            user_id: 用戶 ID
            sandbox_id: 從 DB 讀取的 sandbox_id（可選）

        Returns:
            可用的 Sandbox 實例
        """
        runtime_config = self._resolve_runtime_config(user_id)
        connection_config = _build_connection_config(runtime_config)
        sandbox_id = sandbox_id if isinstance(sandbox_id, str) and sandbox_id else None
        if sandbox_id and not self._persisted_profile_matches_runtime(user_id, sandbox_id, runtime_config):
            logger.warning(
                "持久化 sandbox profile 指纹已过期，跳过旧 sandbox 并重建 "
                "(user=%s, sandbox_id=%s, current_profile=%s/%s)",
                user_id,
                sandbox_id,
                runtime_config.profile_id,
                runtime_config.profile_version,
            )
            sandbox_id = None

        # 1. 先從記憶體快取獲取
        if user_id in self._cache:
            sandbox = self._cache[user_id]
            cached_sandbox_id = self._sandbox_id_from_instance(sandbox)
            if not self._cache_matches_runtime(user_id, runtime_config, sandbox_id):
                logger.warning(
                    "沙箱快取與當前 profile/持久化 ID 不一致，移除快取 "
                    "(user=%s, cached=%s, persisted=%s, cached_profile=%s/%s, current_profile=%s/%s)",
                    user_id,
                    cached_sandbox_id,
                    sandbox_id,
                    self._cache_profile_ids.get(user_id),
                    self._cache_profile_versions.get(user_id),
                    runtime_config.profile_id,
                    runtime_config.profile_version,
                )
                self.invalidate_cache(user_id)
            else:
                try:
                    # 驗證沙箱是否仍然健康
                    is_healthy = False
                    if hasattr(sandbox, "is_healthy"):
                        is_healthy = await sandbox.is_healthy()
                    else:
                        info = await sandbox.get_info()
                        state = getattr(getattr(info, "status", None), "state", "")
                        is_healthy = str(state).lower() in {"running", "pending"}

                    if is_healthy:
                        logger.debug("沙箱命中快取 (user=%s)", user_id)
                        return sandbox
                    else:
                        logger.warning("快取中的沙箱不健康，嘗試 resume (user=%s)", user_id)
                        self.invalidate_cache(user_id)
                except Exception:
                    logger.warning("沙箱健康檢查失敗，移除快取 (user=%s)", user_id)
                    self.invalidate_cache(user_id)

        # 2. 有 sandbox_id → 先查狀態，再決定走 connect 還是 resume
        if sandbox_id:
            if runtime_config.profile_id == "env-default":
                sandbox_state = await self._query_sandbox_state(sandbox_id)
            else:
                sandbox_state = await self._query_sandbox_state(sandbox_id, runtime_config)

            # 2a. 非 paused → 先嘗試 connect
            if sandbox_state != "paused":
                try:
                    logger.info("正在連接沙箱 (user=%s, sandbox_id=%s)...", user_id, sandbox_id)
                    sandbox = await Sandbox.connect(
                        sandbox_id,
                        connection_config=connection_config,
                        connect_timeout=timedelta(seconds=settings.sandbox_ready_timeout_seconds),
                    )
                    logger.info("沙箱連接成功 (user=%s, sandbox_id=%s)", user_id, sandbox_id)
                    self._store_cache(user_id, sandbox, runtime_config)
                    return sandbox
                except Exception as e:
                    logger.warning(
                        "沙箱連接失敗 (user=%s, sandbox_id=%s): %s — 嘗試 resume",
                        user_id, sandbox_id, e,
                    )
            else:
                logger.info("沙箱處於暫停狀態，跳過 connect 直接 resume (user=%s, sandbox_id=%s)", user_id, sandbox_id)

            # 2b. resume（paused 直接走這裡；非 paused 在 connect 失敗後 fallthrough 到這裡）
            try:
                logger.info("正在恢復沙箱 (user=%s, sandbox_id=%s)...", user_id, sandbox_id)
                sandbox = await Sandbox.resume(
                    sandbox_id,
                    connection_config=connection_config,
                    resume_timeout=timedelta(seconds=settings.sandbox_ready_timeout_seconds),
                )
                logger.info("沙箱恢復成功 (user=%s, sandbox_id=%s)", user_id, sandbox_id)
                self._store_cache(user_id, sandbox, runtime_config)
                return sandbox
            except Exception as e:
                logger.warning(
                    "沙箱恢復失敗 (user=%s, sandbox_id=%s): %s — 將創建新沙箱",
                    user_id, sandbox_id, e,
                )

        # 3. 所有嘗試均失敗 → 創建新沙箱
        sandbox = await self.create(user_id)
        # fallback 路徑：把新 sandbox_id 同步回 user_sandbox 表，避免下次調用方仍拿著
        # 失效的舊 id 走 connect/resume 失敗 → 再次 fallback create → 持續泄漏的問題。
        # 僅 update 既有行，不主動 INSERT（首次創建由 sessions/agent_pool 路徑顯式寫入）。
        try:
            active_runtime = self.get_cached_runtime_config(user_id) or runtime_config
            self._persist_sandbox_id_if_exists(
                user_id,
                sandbox.id,
                previous_id=sandbox_id,
                runtime_config=active_runtime,
            )
        except Exception:
            logger.exception("回寫 user_sandbox 失敗 (user=%s, new_id=%s)", user_id, sandbox.id)
        return sandbox

    @staticmethod
    def _upsert_user_sandbox_id(
        user_id: str,
        sandbox_id: str,
        *,
        runtime_config: SandboxRuntimeConfig | None = None,
    ) -> None:
        """Persist current user sandbox id while the lifecycle lock is held.

        The IntegrityError fallback is only a best-effort guard for cross-worker
        races; it does not replace a distributed lock.
        """
        if not sandbox_id:
            return

        from src.api.models.database import SessionLocal
        from src.api.models.user_sandbox import UserSandbox
        import uuid

        with SessionLocal() as db:
            try:
                user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                if user_sandbox:
                    if (
                        user_sandbox.sandbox_id != sandbox_id
                        or user_sandbox.status != "active"
                        or (
                            runtime_config
                            and (
                                user_sandbox.active_profile_id != runtime_config.profile_id
                                or int(user_sandbox.active_profile_version or 0) != runtime_config.profile_version
                            )
                        )
                    ):
                        user_sandbox.sandbox_id = sandbox_id
                        user_sandbox.status = "active"
                        if runtime_config:
                            user_sandbox.active_profile_id = runtime_config.profile_id
                            user_sandbox.active_profile_version = runtime_config.profile_version
                        db.commit()
                    else:
                        db.rollback()
                    return

                db.add(UserSandbox(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    sandbox_id=sandbox_id,
                    active_profile_id=runtime_config.profile_id if runtime_config else None,
                    active_profile_version=runtime_config.profile_version if runtime_config else None,
                    status="active",
                ))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                    if not user_sandbox:
                        raise
                    user_sandbox.sandbox_id = sandbox_id
                    user_sandbox.status = "active"
                    if runtime_config:
                        user_sandbox.active_profile_id = runtime_config.profile_id
                        user_sandbox.active_profile_version = runtime_config.profile_version
                    db.commit()
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _persist_sandbox_id_if_exists(
        user_id: str,
        new_sandbox_id: str,
        *,
        previous_id: str | None,
        runtime_config: SandboxRuntimeConfig | None = None,
    ) -> None:
        """get_or_resume fallback create 後，將新 sandbox_id 回寫已存在的 user_sandbox 行。"""
        if not new_sandbox_id:
            return
        if new_sandbox_id == previous_id and runtime_config is None:
            return
        from src.api.models.database import SessionLocal
        from src.api.models.user_sandbox import UserSandbox

        with SessionLocal() as db:
            us = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
            if us is None:
                return
            if (
                us.sandbox_id != new_sandbox_id
                or (
                    runtime_config
                    and (
                        us.active_profile_id != runtime_config.profile_id
                        or int(us.active_profile_version or 0) != runtime_config.profile_version
                    )
                )
            ):
                us.sandbox_id = new_sandbox_id
                us.status = "active"
                if runtime_config:
                    us.active_profile_id = runtime_config.profile_id
                    us.active_profile_version = runtime_config.profile_version
                db.commit()
                logger.info(
                    "已回寫 user_sandbox.sandbox_id (user=%s, old=%s, new=%s)",
                    user_id, previous_id, new_sandbox_id,
                )

    async def pause(self, user_id: str) -> bool:
        """暫停沙箱（用戶所有 session TTL 均過期時調用）

        Args:
            user_id: 用戶 ID

        Returns:
            是否成功暫停
        """
        sandbox = self._cache.pop(user_id, None)
        self._cache_profile_ids.pop(user_id, None)
        self._cache_profile_versions.pop(user_id, None)
        self._cache_mount_paths.pop(user_id, None)
        self._pushed_skills.pop(user_id, None)
        if not sandbox:
            logger.debug("無需暫停：沙箱不在快取中 (user=%s)", user_id)
            return False

        try:
            await sandbox.pause()
            logger.info("沙箱已暫停 (user=%s, sandbox_id=%s)", user_id, sandbox.id)
            return True
        except Exception as e:
            logger.warning("沙箱暫停失敗 (user=%s): %s", user_id, e)
            return False
        finally:
            try:
                await sandbox.close()
            except Exception:
                pass

    async def kill(self, user_id: str, sandbox_id: str | None = None) -> bool:
        """銷毀沙箱（用戶帳號刪除時調用）

        流程：先嘗試獲取可用沙箱 → 清理掛載目錄文件 → 銷毀容器。
        如果沙箱已過期不可達，跳過文件清理，僅清除快取。

        注意：刪除單個 session 時不應調用此方法，沙箱屬於用戶而非 session。

        Args:
            user_id: 用戶 ID
            sandbox_id: 從 DB 讀取的 sandbox_id（用於快取中沒有時直接連接銷毀）

        Returns:
            是否成功銷毀
        """
        # 先從快取移除
        sandbox = self._cache.pop(user_id, None)
        cached_mount_path = self._cache_mount_paths.pop(user_id, None)
        self._cache_profile_ids.pop(user_id, None)
        self._cache_profile_versions.pop(user_id, None)
        self._pushed_skills.pop(user_id, None)
        runtime_config = None
        connection_config = None
        cleanup_mount_path = get_sandbox_mount_path(cached_mount_path)

        if not sandbox and sandbox_id:
            try:
                runtime_config = self._resolve_runtime_for_existing_sandbox(user_id, sandbox_id)
            except RuntimeError:
                logger.warning(
                    "無法解析既有沙箱連接配置，跳過銷毀 (user=%s, sandbox_id=%s)",
                    user_id,
                    sandbox_id,
                    exc_info=True,
                )
                return False
            connection_config = _build_connection_config(runtime_config)
            cleanup_mount_path = get_sandbox_mount_path(runtime_config.mount_path)
            # 查詢狀態，決定走 connect 還是 resume
            sandbox_state = await self._query_sandbox_state(sandbox_id, runtime_config)

            if sandbox_state == "paused":
                # paused → 直接 resume（避免 connect 卡死）
                try:
                    sandbox = await Sandbox.resume(
                        sandbox_id,
                        connection_config=connection_config,
                        resume_timeout=timedelta(seconds=settings.sandbox_ready_timeout_seconds),
                    )
                except Exception as e:
                    logger.warning("resume 失敗 (sandbox_id=%s): %s — 無法清理持久化文件", sandbox_id, e)
                    return False
            else:
                # 非 paused → 嘗試 connect
                try:
                    sandbox = await Sandbox.connect(
                        sandbox_id,
                        connection_config=connection_config,
                        connect_timeout=timedelta(seconds=settings.sandbox_ready_timeout_seconds),
                    )
                except Exception as e:
                    logger.warning("connect 失敗 (sandbox_id=%s): %s — 嘗試 resume", sandbox_id, e)
                    sandbox = None

                # connect 失敗 → 嘗試 resume
                if not sandbox:
                    try:
                        sandbox = await Sandbox.resume(
                            sandbox_id,
                            connection_config=connection_config,
                            resume_timeout=timedelta(seconds=settings.sandbox_ready_timeout_seconds),
                        )
                    except Exception as e:
                        logger.warning(
                            "resume 也失敗 (sandbox_id=%s): %s — 無法清理持久化文件",
                            sandbox_id, e,
                        )
                        return False

        if not sandbox:
            logger.debug("無需銷毀：沙箱不存在 (user=%s)", user_id)
            return False

        # 🔥 銷毀前清理掛載目錄中的用戶文件
        quoted_mount_path = shlex.quote(cleanup_mount_path)
        try:
            cleanup_result = await sandbox.commands.run(
                f"rm -rf -- {quoted_mount_path}/.[!.]* {quoted_mount_path}/..?* {quoted_mount_path}/*"
            )
            cleanup_exit_code = _extract_command_exit_code(cleanup_result)
            if cleanup_exit_code != 0:
                logger.warning(
                    "清理沙箱文件失敗 (user=%s, path=%s, exit=%s)",
                    user_id, cleanup_mount_path, cleanup_exit_code,
                )
                try:
                    await sandbox.close()
                except Exception:
                    pass
                return False
            logger.info("已清理沙箱掛載目錄文件 (user=%s, path=%s)", user_id, cleanup_mount_path)
        except Exception as e:
            logger.warning("清理沙箱文件失敗 (user=%s): %s", user_id, e)
            try:
                await sandbox.close()
            except Exception:
                pass
            return False

        try:
            await sandbox.kill()
            logger.info("沙箱已銷毀 (user=%s, sandbox_id=%s)", user_id, sandbox.id)
            return True
        except Exception as e:
            logger.warning("沙箱銷毀失敗 (user=%s): %s", user_id, e)
            return False
        finally:
            try:
                await sandbox.close()
            except Exception:
                pass

    async def renew(self, user_id: str) -> bool:
        """續租沙箱（保持活躍狀態）

        Args:
            user_id: 用戶 ID

        Returns:
            是否成功續租
        """
        sandbox = self._cache.get(user_id)
        if not sandbox:
            return False

        try:
            await sandbox.renew(timedelta(minutes=settings.sandbox_timeout_minutes))
            logger.debug("沙箱已續租 (user=%s)", user_id)
            return True
        except Exception as e:
            logger.warning("沙箱續租失敗 (user=%s): %s", user_id, e)
            return False

    # ------------------------------------------------------------------
    # 輔助方法
    # ------------------------------------------------------------------

    def get_mount_path(self, user_id: str | None = None) -> str:
        """獲取當前配置的容器掛載根目錄。"""
        if user_id and user_id in self._cache_mount_paths:
            return get_sandbox_mount_path(self._cache_mount_paths[user_id])
        if user_id:
            runtime_config = self._resolve_runtime_config(user_id)
            return get_sandbox_mount_path(runtime_config.mount_path)
        return get_sandbox_mount_path()

    def get_cached(self, user_id: str) -> Sandbox | None:
        """獲取快取中的沙箱（不做健康檢查，用於工具層直接存取）"""
        return self._cache.get(user_id)

    def invalidate_cache(self, user_id: str) -> None:
        """移除用戶的沙箱快取（用於陳舊沙箱恢復場景）"""
        removed = self._cache.pop(user_id, None)
        self._cache_profile_ids.pop(user_id, None)
        self._cache_profile_versions.pop(user_id, None)
        self._cache_mount_paths.pop(user_id, None)
        self._pushed_skills.pop(user_id, None)
        if removed:
            logger.info("已移除陳舊沙箱快取 (user=%s, sandbox_id=%s)", user_id, getattr(removed, "id", "?"))

    def get_sandbox_id(self, user_id: str) -> str | None:
        """獲取沙箱 ID（用於存入 DB）"""
        sandbox = self._cache.get(user_id)
        return sandbox.id if sandbox else None

    def get_cached_runtime_config(self, user_id: str) -> SandboxRuntimeConfig | None:
        profile_id = self._cache_profile_ids.get(user_id)
        profile_version = self._cache_profile_versions.get(user_id)
        if not profile_id or profile_version is None:
            return None
        current = self._resolve_runtime_config(user_id)
        mount_path = self._cache_mount_paths.get(user_id, current.mount_path)
        return SandboxRuntimeConfig(
            profile_id=profile_id,
            profile_name=current.profile_name,
            profile_version=profile_version,
            profile_source=current.profile_source,
            domain=current.domain,
            protocol=current.protocol,
            api_key=current.api_key,
            use_server_proxy=current.use_server_proxy,
            mount_path=mount_path,
        )

    def get_current_profile_fingerprint(self, user_id: str) -> tuple[str, int]:
        runtime_config = self._resolve_runtime_config(user_id)
        return runtime_config.profile_id, runtime_config.profile_version

    def get_cached_profile_fingerprint(self, user_id: str) -> tuple[str | None, int | None]:
        return self._cache_profile_ids.get(user_id), self._cache_profile_versions.get(user_id)

    def cached_is_current(self, user_id: str, sandbox_id: str | None = None) -> bool:
        runtime_config = self._resolve_runtime_config(user_id)
        return self._cache_matches_runtime(user_id, runtime_config, sandbox_id)

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    async def push_skills(self, user_id: str, skills_dir: str) -> bool:
        """將本地 skills 目錄推送到沙箱

        由於 Agent Server 和 OpenSandbox Server 在不同機器上，
        無法使用 Volume mount，必須透過 files API 上傳。

        Args:
            user_id: 用戶 ID
            skills_dir: 本地 skills 目錄路徑

        Returns:
            是否成功推送
        """
        sandbox = self._cache.get(user_id)
        if not sandbox:
            logger.warning("無法推送 skills：沙箱不在快取中 (user=%s)", user_id)
            return False

        skills_path = Path(skills_dir)
        if not skills_path.exists():
            logger.warning("Skills 目錄不存在: %s", skills_dir)
            return False

        try:
            files_to_push = self._collect_skill_files(
                skills_path,
                skills_path,
                self.get_mount_path(user_id),
            )

            if not files_to_push:
                logger.info("沒有需要推送的 skill 檔案")
                return True

            # 批次上傳到沙箱（使用 SDK 批量 API）
            from opensandbox.models.filesystem import WriteEntry
            entries = [WriteEntry(path=p, data=c) for p, c in files_to_push]
            await sandbox.files.write_files(entries)

            logger.info(
                "已推送 %d 個 skill 檔案到沙箱 (user=%s)", len(files_to_push), user_id
            )
            return True

        except Exception as e:
            logger.error("Skills 推送失敗 (user=%s): %s", user_id, e, exc_info=True)
            return False

    async def push_skill(self, user_id: str, skills_dir: str, skill_name: str) -> bool:
        """按需推送單一 skill 到沙箱。

        Args:
            user_id: 用戶 ID
            skills_dir: 本地 skills 根目錄
            skill_name: skill 名稱（對應 SKILL.md frontmatter 的 name）
        """
        sandbox = self._cache.get(user_id)
        if not sandbox:
            logger.warning("無法推送 skill：沙箱不在快取中 (user=%s)", user_id)
            return False

        pushed = self._pushed_skills.setdefault(user_id, set())
        if skill_name in pushed:
            logger.debug("skill 已推送，跳過 (user=%s, skill=%s)", user_id, skill_name)
            return True

        skills_path = Path(skills_dir)
        if not skills_path.exists():
            logger.warning("Skills 目錄不存在: %s", skills_dir)
            return False

        skill_marker = None
        for candidate in skills_path.rglob("SKILL.md"):
            try:
                text = candidate.read_text(encoding="utf-8")
            except Exception:
                continue
            candidate_name = self._extract_skill_name_from_skill_md(text)
            if candidate_name == skill_name:
                skill_marker = candidate
                break

        if not skill_marker:
            logger.warning("找不到 skill 定義: %s", skill_name)
            return False

        skill_dir = skill_marker.parent
        files_to_push = self._collect_skill_files(
            skill_dir,
            skills_path,
            self.get_mount_path(user_id),
        )
        if not files_to_push:
            logger.warning("skill 無可推送檔案: %s", skill_name)
            return False

        try:
            from opensandbox.models.filesystem import WriteEntry

            entries = [WriteEntry(path=p, data=c) for p, c in files_to_push]
            await sandbox.files.write_files(entries)
            pushed.add(skill_name)
            logger.info(
                "已按需推送 skill (user=%s, skill=%s, files=%d)",
                user_id,
                skill_name,
                len(files_to_push),
            )
            return True
        except Exception as e:
            logger.error(
                "按需推送 skill 失敗 (user=%s, skill=%s): %s",
                user_id,
                skill_name,
                e,
                exc_info=True,
            )
            return False

    # ------------------------------------------------------------------
    # 沙箱端 Skill 發現（用戶第三方 Skill）
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_skill_description_from_skill_md(text: str) -> str | None:
        """從 SKILL.md frontmatter 中提取 description。"""
        normalized = text.lstrip("\ufeff")
        if not normalized.startswith("---"):
            return None
        end_idx = normalized.find("\n---", 3)
        if end_idx == -1:
            return None
        frontmatter = normalized[3:end_idx]
        match = re.search(
            r"^\s*description\s*:\s*['\"]?([^'\"\n]+)['\"]?\s*$",
            frontmatter,
            flags=re.MULTILINE,
        )
        return match.group(1).strip() if match else None

    async def discover_sandbox_skills(
        self,
        user_id: str,
        official_skill_names: set[str] | None = None,
    ) -> list[dict]:
        """發現沙箱中用戶自行安裝的第三方 Skill。

        在沙箱內執行 ``find`` 命令定位所有 SKILL.md，讀取 frontmatter
        提取 name / description，排除與官方 Skill 同名的條目。

        Args:
            user_id: 用戶 ID
            official_skill_names: 官方 Skill 名稱集合（用於去重）

        Returns:
            包含 ``name``, ``description``, ``sandbox_skill_dir`` 的字典列表。
            沙箱不可用或出錯時返回空列表。
        """
        if official_skill_names is None:
            official_skill_names = set()

        sandbox = self._cache.get(user_id)
        if not sandbox:
            logger.debug("discover_sandbox_skills: 沙箱不在快取中 (user=%s)", user_id)
            return []

        mount_path = self.get_mount_path(user_id)
        skills_root = posixpath.join(mount_path, "skills")

        try:
            exec_result = await sandbox.commands.run(
                f"find {skills_root} -maxdepth 2 -name SKILL.md -type f 2>/dev/null",
                opts=RunCommandOpts(timeout=10),
            )
            logs = getattr(exec_result, "logs", None)
            if logs is None:
                logger.debug("discover_sandbox_skills: find 命令返回空 logs (user=%s)", user_id)
                return []

            stdout_text = getattr(logs, "stdout", "") or ""
            if isinstance(stdout_text, list):
                stdout_text = "\n".join(
                    getattr(line, "text", str(line)) for line in stdout_text
                )
            paths = [p.strip() for p in stdout_text.strip().splitlines() if p.strip()]
        except Exception as e:
            logger.warning("discover_sandbox_skills: find 命令失敗 (user=%s): %s", user_id, e)
            return []

        async def _read_skill(skill_md_path: str):
            content = await sandbox.files.read_file(skill_md_path)
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            return content

        raw_results = await asyncio.gather(
            *(_read_skill(skill_md_path) for skill_md_path in paths),
            return_exceptions=True,
        )

        results: list[dict] = []
        for skill_md_path, raw in zip(paths, raw_results):
            if isinstance(raw, BaseException):
                logger.debug("discover_sandbox_skills: 讀取 %s 失敗: %s", skill_md_path, raw)
                continue

            content = raw

            name = self._extract_skill_name_from_skill_md(content)
            if not name:
                continue
            if name in official_skill_names:
                logger.debug("discover_sandbox_skills: 跳過與官方同名的 skill: %s", name)
                continue

            description = self._extract_skill_description_from_skill_md(content) or ""
            skill_dir = posixpath.dirname(skill_md_path)

            results.append({
                "name": name,
                "description": description,
                "sandbox_skill_dir": skill_dir,
            })

        logger.info(
            "discover_sandbox_skills: 發現 %d 個用戶 Skill (user=%s)",
            len(results),
            user_id,
        )
        return results

    async def read_sandbox_skill_content(
        self,
        user_id: str,
        sandbox_skill_dir: str,
    ) -> str | None:
        """按需讀取沙箱中用戶 Skill 的完整 SKILL.md 內容（去除 frontmatter）。

        Args:
            user_id: 用戶 ID
            sandbox_skill_dir: 沙箱內 Skill 目錄路徑

        Returns:
            Skill 正文（去除 frontmatter 後），或 ``None`` 表示讀取失敗。
        """
        sandbox = self._cache.get(user_id)
        if not sandbox:
            logger.warning("read_sandbox_skill_content: 沙箱不在快取中 (user=%s)", user_id)
            return None

        skill_md_path = posixpath.join(sandbox_skill_dir, "SKILL.md")
        try:
            content = await sandbox.files.read_file(skill_md_path)
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("read_sandbox_skill_content: 讀取失敗 (%s): %s", skill_md_path, e)
            return None

        # Strip frontmatter, return body only
        normalized = content.lstrip("\ufeff")
        if normalized.startswith("---"):
            end_idx = normalized.find("\n---", 3)
            if end_idx != -1:
                return normalized[end_idx + 4:].strip()
        return normalized.strip()


# 全局單例存取
_sandbox_service: Optional[SandboxSessionService] = None


def get_sandbox_service() -> SandboxSessionService:
    """獲取全局 SandboxSessionService 單例"""
    global _sandbox_service
    if _sandbox_service is None:
        _sandbox_service = SandboxSessionService()
    return _sandbox_service
