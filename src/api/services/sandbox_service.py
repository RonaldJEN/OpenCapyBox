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
from enum import Enum
from typing import Callable, Optional
from pathlib import Path

import yaml
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import Volume, Host
from sqlalchemy.exc import IntegrityError

from src.agent.schema.skill_key import normalize_skill_key
from src.api.config import get_settings
from src.api.services.sandbox_profile_service import SandboxRuntimeConfig
from src.api.services.skill_inventory_service import (
    SkillInventoryValidationError,
    normalize_user_skill_inventory,
)

logger = logging.getLogger(__name__)
settings = get_settings()
MAX_SKILL_DISCOVERY_CANDIDATES = 1024


class SandboxTemporarilyUnavailable(RuntimeError):
    """The persisted sandbox cannot be used safely at the moment."""


class ProfileCompatibility(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    STALE_BINDING = "stale_binding"
    UNKNOWN = "unknown"


_CONNECTABLE_SANDBOX_STATES = {"running", "pending"}
_TERMINAL_SANDBOX_STATES = {"terminated", "failed", "not_found"}
_TRANSITIONAL_SANDBOX_STATES = {"pausing", "stopping"}


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
        self._skill_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._initialized = True

    def _get_lifecycle_lock(self, user_id: str) -> asyncio.Lock:
        """Return the per-user sandbox lifecycle lock for this process."""
        return self._lifecycle_locks.setdefault(user_id, asyncio.Lock())

    def _get_skill_lock(self, user_id: str, skill_name: str) -> asyncio.Lock:
        """Serialize concurrent pushes for one user skill in this worker."""
        return self._skill_locks.setdefault((user_id, skill_name), asyncio.Lock())

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
    def _extract_skill_frontmatter(text: str) -> dict | None:
        """Parse a SKILL.md YAML frontmatter mapping."""

        normalized = text.lstrip("\ufeff")
        if not normalized.startswith("---"):
            return None
        end_idx = normalized.find("\n---", 3)
        if end_idx == -1:
            return None
        try:
            value = yaml.safe_load(normalized[3:end_idx])
        except yaml.YAMLError:
            return None
        return value if isinstance(value, dict) else None

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

    @classmethod
    def _extract_skill_display_name_from_skill_md(cls, text: str) -> str | None:
        """Extract an optional human-facing display name from frontmatter."""

        frontmatter = cls._extract_skill_frontmatter(text)
        if not frontmatter:
            return None
        value = frontmatter.get("display_name") or frontmatter.get("display-name")
        metadata = frontmatter.get("metadata")
        if not value and isinstance(metadata, dict):
            value = metadata.get("display_name") or metadata.get("display-name")
        if not isinstance(value, str):
            return None
        display_name = value.strip()
        return display_name or None

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
    def _persisted_profile_compatibility(
        user_id: str,
        sandbox_id: str | None,
        runtime_config: SandboxRuntimeConfig,
    ) -> ProfileCompatibility:
        if not sandbox_id:
            return ProfileCompatibility.UNKNOWN
        try:
            from src.api.models.database import SessionLocal
            from src.api.models.user_sandbox import UserSandbox

            with SessionLocal() as db:
                user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                if not user_sandbox:
                    return ProfileCompatibility.UNKNOWN
                if user_sandbox.sandbox_id != sandbox_id:
                    return ProfileCompatibility.STALE_BINDING
                if (
                    not user_sandbox.active_profile_id
                    or user_sandbox.active_profile_version is None
                ):
                    return ProfileCompatibility.UNKNOWN
                if (
                    user_sandbox.active_profile_id == runtime_config.profile_id
                    and int(user_sandbox.active_profile_version) == runtime_config.profile_version
                ):
                    return ProfileCompatibility.MATCH
                return ProfileCompatibility.MISMATCH
        except Exception:
            logger.warning("读取持久化 sandbox profile 指纹失败 (user=%s)", user_id, exc_info=True)
            return ProfileCompatibility.UNKNOWN

    @staticmethod
    def _read_persisted_sandbox_id(user_id: str) -> str | None:
        try:
            from src.api.models.database import SessionLocal
            from src.api.models.user_sandbox import UserSandbox

            with SessionLocal() as db:
                user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                sandbox_id = getattr(user_sandbox, "sandbox_id", None) if user_sandbox else None
                return sandbox_id if isinstance(sandbox_id, str) and sandbox_id else None
        except Exception as exc:
            logger.warning("读取最新 sandbox 绑定失败 (user=%s)", user_id, exc_info=True)
            raise SandboxTemporarilyUnavailable("沙箱绑定暂时无法确认") from exc

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
        sandbox = await self._create_candidate(user_id, runtime_config)
        self._store_cache(user_id, sandbox, runtime_config)
        return sandbox

    async def _create_candidate(
        self,
        user_id: str,
        runtime_config: SandboxRuntimeConfig,
    ) -> Sandbox:
        """Create an unbound sandbox candidate without mutating process cache."""
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
            控制面明确返回 404 时返回 ``not_found``；其他查询失败抛出
            ``SandboxTemporarilyUnavailable``，不得被当成终止状态。
        """
        try:
            from opensandbox.manager import SandboxManager
            connection_config = _build_connection_config(runtime_config)
            async with await SandboxManager.create(connection_config=connection_config) as manager:
                info = await manager.get_sandbox_info(sandbox_id)
                raw_state = getattr(getattr(info, "status", None), "state", "")
                state = str(getattr(raw_state, "value", raw_state)).lower()
            logger.debug("沙箱狀態查詢結果 (sandbox_id=%s): %s", sandbox_id, state)
            return state or "unknown"
        except Exception as e:
            try:
                from opensandbox.exceptions import SandboxApiException

                if isinstance(e, SandboxApiException) and e.status_code == 404:
                    logger.info("沙箱不存在 (sandbox_id=%s)", sandbox_id)
                    return "not_found"
            except Exception:
                pass
            logger.warning("查詢沙箱狀態失敗 (sandbox_id=%s): %s", sandbox_id, e)
            raise SandboxTemporarilyUnavailable("沙箱状态暂时无法确认") from e

    async def get_or_resume(
        self, user_id: str, sandbox_id: str | None = None
    ) -> Sandbox:
        """獲取沙箱實例（按 user_id 在本進程內串行化生命週期）。"""
        async with self._get_lifecycle_lock(user_id):
            return await self._get_or_resume_unlocked(user_id, sandbox_id)

    async def get_or_resume_and_renew(
        self, user_id: str, sandbox_id: str | None = None
    ) -> Sandbox:
        """獲取並續租同一沙箱，失敗恢復全程持有用戶生命週期鎖。"""
        async with self._get_lifecycle_lock(user_id):
            sandbox = await self._get_or_resume_unlocked(user_id, sandbox_id)
            if await self._renew_instance(user_id, sandbox):
                return sandbox

            logger.warning(
                "沙箱續租失敗，清理快取後重新獲取 "
                "(user=%s, sandbox_id=%s)",
                user_id,
                self._sandbox_id_from_instance(sandbox) or sandbox_id,
            )
            retry_sandbox_id = self._sandbox_id_from_instance(sandbox) or sandbox_id
            if self._cache.get(user_id) is sandbox:
                self.invalidate_cache(user_id)

            sandbox = await self._get_or_resume_unlocked(user_id, retry_sandbox_id)
            if not await self._renew_instance(user_id, sandbox):
                raise RuntimeError("沙箱續租失敗，無法安全開始任務")
            return sandbox

    async def get_existing(
        self, user_id: str, sandbox_id: str
    ) -> Sandbox:
        """Connect or resume an existing sandbox without creating a replacement."""
        async with self._get_lifecycle_lock(user_id):
            return await self._get_or_resume_unlocked(
                user_id,
                sandbox_id,
                create_if_missing=False,
            )

    async def recover_persisted_sandbox(
        self, user_id: str, sandbox_id: str
    ) -> Sandbox:
        """Restore a persisted sandbox, rebuilding only after confirmed loss."""
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ValueError("sandbox_id 不能为空")
        async with self._get_lifecycle_lock(user_id):
            return await self._get_or_resume_unlocked(
                user_id,
                sandbox_id,
                create_if_missing=True,
            )

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
            if current_sandbox_id and not sandbox_id:
                try:
                    bound_sandbox_id = self._upsert_user_sandbox_id(
                        user_id,
                        current_sandbox_id,
                        runtime_config=runtime_config,
                    )
                except Exception:
                    if self._cache.get(user_id) is sandbox:
                        self.invalidate_cache(user_id)
                    await self._destroy_container_preserve_storage(sandbox)
                    raise

                if bound_sandbox_id != current_sandbox_id:
                    if self._cache.get(user_id) is sandbox:
                        self.invalidate_cache(user_id)
                    await self._destroy_container_preserve_storage(sandbox)
                    sandbox = await self._get_or_resume_unlocked(
                        user_id,
                        bound_sandbox_id,
                        create_if_missing=False,
                    )
                    current_sandbox_id = self._sandbox_id_from_instance(sandbox)
                    runtime_config = (
                        self.get_cached_runtime_config(user_id)
                        or self._resolve_runtime_config(user_id)
                    )
            if current_sandbox_id:
                self._store_cache(user_id, sandbox, runtime_config)
            return sandbox, current_sandbox_id

    async def _get_or_resume_unlocked(
        self,
        user_id: str,
        sandbox_id: str | None = None,
        *,
        create_if_missing: bool = True,
    ) -> Sandbox:
        """按缓存、Profile 和控制面状态安全恢复沙箱。

        Args:
            user_id: 用戶 ID
            sandbox_id: 從 DB 讀取的 sandbox_id（可選）

        Returns:
            可用的 Sandbox 實例
        """
        runtime_config = self._resolve_runtime_config(user_id)
        connection_config = _build_connection_config(runtime_config)
        sandbox_id = sandbox_id if isinstance(sandbox_id, str) and sandbox_id else None
        profile_compatibility = (
            self._persisted_profile_compatibility(user_id, sandbox_id, runtime_config)
            if sandbox_id
            else ProfileCompatibility.UNKNOWN
        )
        if sandbox_id and profile_compatibility != ProfileCompatibility.MATCH:
            # A matching live cache already records the runtime profile used by
            # this process and remains a valid existing sandbox even before its
            # DB row is persisted. Never substitute a *different* cached sandbox
            # for get_existing(), though: that API promises the requested ID or
            # a failure, not a replacement generation.
            matching_live_cache = self._cache_matches_runtime(
                user_id,
                runtime_config,
                sandbox_id,
            )
            if not matching_live_cache and profile_compatibility == ProfileCompatibility.STALE_BINDING:
                if not create_if_missing:
                    raise RuntimeError("既有沙箱绑定已更新")
                winner_id = self._read_persisted_sandbox_id(user_id)
                if not winner_id or winner_id == sandbox_id:
                    raise SandboxTemporarilyUnavailable("沙箱绑定正在变化")
                logger.info(
                    "检测到过期 sandbox 绑定，改用最新 ID "
                    "(user=%s, stale=%s, current=%s)",
                    user_id,
                    sandbox_id,
                    winner_id,
                )
                return await self._get_or_resume_unlocked(
                    user_id,
                    winner_id,
                    create_if_missing=False,
                )
            if not matching_live_cache and profile_compatibility == ProfileCompatibility.UNKNOWN:
                raise SandboxTemporarilyUnavailable("既有沙箱 Profile 暂时无法确认")
            if not matching_live_cache and profile_compatibility == ProfileCompatibility.MISMATCH:
                if not create_if_missing:
                    raise RuntimeError("既有沙箱 profile 指纹不匹配")
                logger.warning(
                    "持久化 sandbox profile 指纹已过期，重建 sandbox "
                    "(user=%s, sandbox_id=%s, current_profile=%s/%s)",
                    user_id,
                    sandbox_id,
                    runtime_config.profile_id,
                    runtime_config.profile_version,
                )
                return await self._rebuild_sandbox_unlocked(
                    user_id,
                    sandbox_id,
                    runtime_config,
                    reason="profile_mismatch",
                )

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
                        logger.warning("快取中的沙箱不健康，重新查询状态 (user=%s)", user_id)
                        self.invalidate_cache(user_id)
                except Exception:
                    logger.warning("沙箱健康檢查失敗，移除快取 (user=%s)", user_id)
                    self.invalidate_cache(user_id)

        # 2. 有 sandbox_id → 先查狀態，只执行与当前状态匹配的一种操作。
        if sandbox_id:
            sandbox_state = await self._query_sandbox_state(sandbox_id, runtime_config)

            if sandbox_state in _CONNECTABLE_SANDBOX_STATES:
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
                        "沙箱連接失敗 (user=%s, sandbox_id=%s): %s",
                        user_id, sandbox_id, e,
                    )
                    raise SandboxTemporarilyUnavailable("沙箱连接暂时失败") from e

            if sandbox_state == "paused":
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
                        "沙箱恢復失敗 (user=%s, sandbox_id=%s): %s",
                        user_id, sandbox_id, e,
                    )
                    raise SandboxTemporarilyUnavailable("沙箱恢复暂时失败") from e

            if sandbox_state in _TERMINAL_SANDBOX_STATES:
                if not create_if_missing:
                    raise RuntimeError("既有沙箱不可用")
                return await self._rebuild_sandbox_unlocked(
                    user_id,
                    sandbox_id,
                    runtime_config,
                    reason=f"state_{sandbox_state}",
                )

            if sandbox_state in _TRANSITIONAL_SANDBOX_STATES:
                raise SandboxTemporarilyUnavailable(
                    f"沙箱正在执行过渡操作: {sandbox_state}"
                )
            raise SandboxTemporarilyUnavailable(
                f"无法安全处理沙箱状态: {sandbox_state or 'unknown'}"
            )

        if not create_if_missing:
            raise RuntimeError("既有沙箱不可用")
        return await self.create(user_id)

    async def _rebuild_sandbox_unlocked(
        self,
        user_id: str,
        previous_id: str,
        runtime_config: SandboxRuntimeConfig,
        *,
        reason: str,
    ) -> Sandbox:
        """Create a candidate and bind it with an old-id compare-and-swap."""
        candidate = await self._create_candidate(user_id, runtime_config)
        candidate_id = self._sandbox_id_from_instance(candidate)
        if not candidate_id:
            await self._destroy_container_preserve_storage(candidate)
            raise SandboxTemporarilyUnavailable("新沙箱缺少有效 ID")

        try:
            won, winner_id = self._compare_and_swap_sandbox_binding(
                user_id,
                previous_id,
                candidate_id,
                runtime_config=runtime_config,
            )
        except Exception:
            await self._destroy_container_preserve_storage(candidate)
            raise

        if won:
            logger.info(
                "沙箱重建绑定成功 (user=%s, old=%s, new=%s, reason=%s)",
                user_id,
                previous_id,
                candidate_id,
                reason,
            )
            self._store_cache(user_id, candidate, runtime_config)
            return candidate

        logger.info(
            "沙箱重建绑定竞争失败，销毁候选容器并复用胜出者 "
            "(user=%s, candidate=%s, winner=%s)",
            user_id,
            candidate_id,
            winner_id,
        )
        await self._destroy_container_preserve_storage(candidate)
        if not winner_id or winner_id == previous_id:
            raise SandboxTemporarilyUnavailable("沙箱重建绑定暂时不可用")
        return await self._get_or_resume_unlocked(
            user_id,
            winner_id,
            create_if_missing=False,
        )

    @staticmethod
    async def _destroy_container_preserve_storage(sandbox: Sandbox) -> None:
        """Destroy only a container; never remove the shared persistent mount."""
        try:
            await sandbox.kill()
        except Exception:
            logger.warning(
                "候选沙箱容器销毁失败，可能需要异步回收 (sandbox_id=%s)",
                getattr(sandbox, "id", None),
                exc_info=True,
            )
        finally:
            try:
                await sandbox.close()
            except Exception:
                pass

    @staticmethod
    def _compare_and_swap_sandbox_binding(
        user_id: str,
        previous_id: str,
        new_sandbox_id: str,
        *,
        runtime_config: SandboxRuntimeConfig,
    ) -> tuple[bool, str | None]:
        from src.api.models.database import SessionLocal
        from src.api.models.user_sandbox import UserSandbox

        with SessionLocal() as db:
            try:
                updated = (
                    db.query(UserSandbox)
                    .filter(
                        UserSandbox.user_id == user_id,
                        UserSandbox.sandbox_id == previous_id,
                    )
                    .update(
                        {
                            UserSandbox.sandbox_id: new_sandbox_id,
                            UserSandbox.status: "active",
                            UserSandbox.active_profile_id: runtime_config.profile_id,
                            UserSandbox.active_profile_version: runtime_config.profile_version,
                        },
                        synchronize_session=False,
                    )
                )
                if updated == 1:
                    db.commit()
                    return True, new_sandbox_id

                db.rollback()
                winner = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                winner_id = getattr(winner, "sandbox_id", None) if winner else None
                return False, winner_id if isinstance(winner_id, str) and winner_id else None
            except Exception as exc:
                db.rollback()
                logger.warning(
                    "条件更新 sandbox 绑定失败 (user=%s, old=%s, new=%s)",
                    user_id,
                    previous_id,
                    new_sandbox_id,
                    exc_info=True,
                )
                raise SandboxTemporarilyUnavailable("沙箱绑定更新暂时失败") from exc

    @staticmethod
    def _upsert_user_sandbox_id(
        user_id: str,
        sandbox_id: str,
        *,
        runtime_config: SandboxRuntimeConfig | None = None,
    ) -> str:
        """Persist current user sandbox id while the lifecycle lock is held.

        The IntegrityError fallback is only a best-effort guard for cross-worker
        races; it does not replace a distributed lock.
        """
        if not sandbox_id:
            return sandbox_id

        from src.api.models.database import SessionLocal
        from src.api.models.user_sandbox import UserSandbox
        import uuid

        with SessionLocal() as db:
            try:
                user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                if user_sandbox:
                    if user_sandbox.sandbox_id and user_sandbox.sandbox_id != sandbox_id:
                        db.rollback()
                        return user_sandbox.sandbox_id
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
                    return sandbox_id

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
                    if user_sandbox.sandbox_id and user_sandbox.sandbox_id != sandbox_id:
                        return user_sandbox.sandbox_id
                    user_sandbox.sandbox_id = sandbox_id
                    user_sandbox.status = "active"
                    if runtime_config:
                        user_sandbox.active_profile_id = runtime_config.profile_id
                        user_sandbox.active_profile_version = runtime_config.profile_version
                    db.commit()
                return sandbox_id
            except Exception:
                db.rollback()
                raise

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
            try:
                sandbox_state = await self._query_sandbox_state(sandbox_id, runtime_config)
            except SandboxTemporarilyUnavailable:
                logger.warning(
                    "無法确认待销毁沙箱状态，跳过破坏性操作 "
                    "(user=%s, sandbox_id=%s)",
                    user_id,
                    sandbox_id,
                )
                return False

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

    async def _renew_instance(self, user_id: str, sandbox: Sandbox) -> bool:
        """直接續租指定實例，避免按 user_id 二次讀取到不同快取。"""
        try:
            await sandbox.renew(timedelta(minutes=settings.sandbox_timeout_minutes))
            logger.debug(
                "沙箱已續租 (user=%s, sandbox_id=%s)",
                user_id,
                self._sandbox_id_from_instance(sandbox),
            )
            return True
        except Exception as e:
            logger.warning(
                "沙箱續租失敗 (user=%s, sandbox_id=%s): %s",
                user_id,
                self._sandbox_id_from_instance(sandbox),
                e,
            )
            return False

    async def renew(self, user_id: str) -> bool:
        """續租沙箱（保持活躍狀態）

        Args:
            user_id: 用戶 ID

        Returns:
            是否成功續租
        """
        async with self._get_lifecycle_lock(user_id):
            sandbox = self._cache.get(user_id)
            if not sandbox:
                return False
            return await self._renew_instance(user_id, sandbox)

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

    async def push_skill(
        self,
        user_id: str,
        skills_dir: str,
        skill_name: str,
        *,
        enabled_check: Callable[[], bool] | None = None,
    ) -> bool:
        """Push one skill while respecting the latest logical enable state."""
        async with self._get_skill_lock(user_id, skill_name):
            if enabled_check is not None and not enabled_check():
                logger.info(
                    "skill 已禁用，取消推送 (user=%s, skill=%s)",
                    user_id,
                    skill_name,
                )
                return False
            pushed = await self._push_skill_unlocked(user_id, skills_dir, skill_name)
            if pushed and enabled_check is not None and not enabled_check():
                logger.info(
                    "skill 推送期间被禁用，保留文件但不暴露给 Agent "
                    "(user=%s, skill=%s)",
                    user_id,
                    skill_name,
                )
                return False
            return pushed

    async def _push_skill_unlocked(
        self,
        user_id: str,
        skills_dir: str,
        skill_name: str,
    ) -> bool:
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
        *,
        strict: bool = False,
    ) -> list[dict]:
        """發現沙箱中用戶自行安裝的第三方 Skill。

        在沙箱內執行 ``find`` 命令定位所有 SKILL.md，讀取 frontmatter
        提取 name / description，排除與官方 Skill 同名的條目。

        Args:
            user_id: 用戶 ID
            official_skill_names: 官方 Skill 名稱集合（用於去重）
            strict: 沙箱访问失败时是否抛出异常。API 列表端点使用严格模式
                区分“没有用户 Skill”和“沙箱不可用”；Agent 发现保持容错。

        Returns:
            包含 ``name``, ``description``, ``sandbox_skill_dir`` 的字典列表。
            容错模式下沙箱不可用或出错时返回空列表；严格模式下抛出异常。
        """
        if official_skill_names is None:
            official_skill_names = set()

        sandbox = self._cache.get(user_id)
        if not sandbox:
            logger.debug("discover_sandbox_skills: 沙箱不在快取中 (user=%s)", user_id)
            if strict:
                raise RuntimeError("沙箱不在缓存中")
            return []

        mount_path = self.get_mount_path(user_id)
        skills_root = posixpath.join(mount_path, "skills")
        quoted_skills_root = shlex.quote(skills_root)

        try:
            exec_result = await sandbox.commands.run(
                "if [ -d {root} ]; then "
                "find {root} -maxdepth 2 -name SKILL.md -type f; "
                "fi".format(root=quoted_skills_root),
                opts=RunCommandOpts(timeout=10),
            )
            exit_code = _extract_command_exit_code(exec_result)
            if exit_code != 0:
                raise RuntimeError(f"find 命令退出码异常: {exit_code}")
            logs = getattr(exec_result, "logs", None)
            if logs is None:
                raise RuntimeError("find 命令返回空 logs")

            stdout_text = getattr(logs, "stdout", "") or ""
            if isinstance(stdout_text, list):
                stdout_text = "\n".join(
                    getattr(line, "text", str(line)) for line in stdout_text
                )
            paths = [p.strip() for p in stdout_text.strip().splitlines() if p.strip()]
            max_candidates = (
                MAX_SKILL_DISCOVERY_CANDIDATES + len(official_skill_names)
            )
            if len(paths) > max_candidates:
                raise SkillInventoryValidationError("Too many Skill discovery candidates")
        except Exception as e:
            logger.warning("discover_sandbox_skills: find 命令失敗 (user=%s): %s", user_id, e)
            if strict:
                raise RuntimeError("用户 Skill 发现失败") from e
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
                if strict:
                    raise RuntimeError(
                        f"读取用户 Skill 失败: {skill_md_path}"
                    ) from raw
                continue

            content = raw
            frontmatter = self._extract_skill_frontmatter(content)
            name_value = frontmatter.get("name") if frontmatter else None
            description_value = frontmatter.get("description") if frontmatter else None
            metadata_value = frontmatter.get("metadata") if frontmatter else None
            display_name_value = None
            invalid_metadata = not isinstance(name_value, str)
            if description_value is not None and not isinstance(description_value, str):
                invalid_metadata = True
            if metadata_value is not None and not isinstance(metadata_value, dict):
                invalid_metadata = True
            if frontmatter:
                display_candidates = [
                    frontmatter.get("display_name"),
                    frontmatter.get("display-name"),
                ]
                if isinstance(metadata_value, dict):
                    display_candidates.extend([
                        metadata_value.get("display_name"),
                        metadata_value.get("display-name"),
                    ])
                for candidate in display_candidates:
                    if candidate is not None and not isinstance(candidate, str):
                        invalid_metadata = True
                    elif (
                        display_name_value is None
                        and isinstance(candidate, str)
                        and candidate.strip()
                    ):
                        display_name_value = candidate
            if invalid_metadata:
                if strict:
                    raise RuntimeError(
                        f"用户 Skill 元数据无效: {skill_md_path}"
                    )
                continue
            try:
                name = normalize_skill_key(name_value)
            except ValueError as exc:
                if strict:
                    raise RuntimeError(
                        f"用户 Skill 元数据无效: {skill_md_path}"
                    ) from exc
                continue
            if name in official_skill_names:
                logger.debug("discover_sandbox_skills: 跳過與官方同名的 skill: %s", name)
                continue

            description = description_value or ""
            display_name = (
                display_name_value.strip()
                if isinstance(display_name_value, str) and display_name_value.strip()
                else name
            )
            skill_dir = posixpath.dirname(skill_md_path)

            results.append({
                "name": name,
                "display_name": display_name,
                "description": description,
                "sandbox_skill_dir": skill_dir,
            })

        try:
            results = normalize_user_skill_inventory(results)
        except SkillInventoryValidationError as exc:
            logger.warning(
                "discover_sandbox_skills: 用户 Skill 清单无效 (user=%s): %s",
                user_id,
                exc,
            )
            if strict:
                raise RuntimeError("用户 Skill 清单无效") from exc
            return []

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
