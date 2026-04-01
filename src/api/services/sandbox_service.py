"""OpenSandbox 會話服務

管理沙箱的完整生命週期：
- create: 首次創建沙箱（用戶首次使用）
- get_or_resume: 獲取已連接的沙箱實例（從記憶體快取）
- pause: 暫停沙箱（TTL 過期且用戶無任何活躍 session 時）
- kill: 銷毀沙箱（用戶刪除時）
- push_skill: 將 skills 資源推送到沙箱

架構（一用戶一沙箱）：
  user_id → sandbox（持久化工作空間 /home/user）
    ├── USER.md / SOUL.md / AGENTS.md / MEMORY.md / HEARTBEAT.md
    └── sessions/{session_id}/   ← 各對話隔離子目錄

  Agent Server (本機) ←→ OpenSandbox Server (遠端)
  文件全部存在沙箱中，Agent Server 僅作為代理。
"""

import logging
import re
import hashlib
import posixpath
from datetime import timedelta
from typing import Optional
from pathlib import Path

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import Volume, Host

from src.api.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def get_sandbox_mount_path() -> str:
    """獲取容器內沙箱工作根目錄（標準化）。"""
    mount_path = getattr(settings, "sandbox_storage_mount_path", "/home/user")
    if not isinstance(mount_path, str) or not mount_path.startswith("/"):
        mount_path = "/home/user"
    normalized = posixpath.normpath(mount_path)
    return normalized if normalized.startswith("/") else "/home/user"


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


def _build_connection_config() -> ConnectionConfig:
    """構建 OpenSandbox 連接配置（從 Settings 讀取）"""
    api_key = settings.sandbox_api_key
    return ConnectionConfig(
        domain=settings.sandbox_domain,
        api_key=api_key,
        protocol=settings.sandbox_protocol,
        request_timeout=timedelta(seconds=60),
        use_server_proxy=settings.sandbox_use_server_proxy,
        # Workaround: HealthAdapter 在 use_server_proxy=True 時不會自動帶認證頭
        headers={"OPEN-SANDBOX-API-KEY": api_key} if settings.sandbox_use_server_proxy else {},
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
        self._pushed_skills: dict[str, set[str]] = {}  # user_id → pushed skill names
        self._config = _build_connection_config()
        self._initialized = True

    @staticmethod
    def _collect_skill_files(skills_path: Path, root_path: Path) -> list[tuple[str, bytes]]:
        skills_base = posixpath.join(get_sandbox_mount_path(), "skills")
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
    def _build_persistent_volumes(user_id: str) -> list[Volume] | None:
        enabled = getattr(settings, "sandbox_persistent_storage_enabled", True)
        if not enabled:
            return None

        mount_path = get_sandbox_mount_path()

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
        logger.info("正在創建沙箱 (user=%s)...", user_id)

        try:
            volumes = self._build_persistent_volumes(user_id)
            sandbox = await Sandbox.create(
                settings.sandbox_image,
                connection_config=self._config,
                timeout=timedelta(minutes=settings.sandbox_timeout_minutes),
                ready_timeout=timedelta(seconds=settings.sandbox_ready_timeout_seconds),
                health_check_polling_interval=timedelta(seconds=2),
                volumes=volumes,
            )
            self._cache[user_id] = sandbox
            self._pushed_skills.setdefault(user_id, set())
            logger.info(
                "沙箱創建成功 (user=%s, sandbox_id=%s)", user_id, sandbox.id
            )
            return sandbox

        except Exception as e:
            logger.error("沙箱創建失敗 (user=%s): %s", user_id, e, exc_info=True)
            raise RuntimeError(f"沙箱創建失敗: {e}") from e

    async def get_or_resume(
        self, user_id: str, sandbox_id: str | None = None
    ) -> Sandbox:
        """獲取沙箱實例（先快取 → connect → resume → create）

        Args:
            user_id: 用戶 ID
            sandbox_id: 從 DB 讀取的 sandbox_id（可選）

        Returns:
            可用的 Sandbox 實例
        """
        # 1. 先從記憶體快取獲取
        if user_id in self._cache:
            sandbox = self._cache[user_id]
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
                    del self._cache[user_id]
            except Exception:
                logger.warning("沙箱健康檢查失敗，移除快取 (user=%s)", user_id)
                del self._cache[user_id]

        # 2. 嘗試 connect（如果有 sandbox_id，優先連接已運行中的沙箱）
        if sandbox_id:
            try:
                logger.info("正在連接沙箱 (user=%s, sandbox_id=%s)...", user_id, sandbox_id)
                sandbox = await Sandbox.connect(
                    sandbox_id,
                    connection_config=self._config,
                    connect_timeout=timedelta(seconds=settings.sandbox_ready_timeout_seconds),
                )

                logger.info("沙箱連接成功 (user=%s, sandbox_id=%s)", user_id, sandbox_id)
                self._cache[user_id] = sandbox
                self._pushed_skills.setdefault(user_id, set())
                return sandbox

            except Exception as e:
                logger.warning(
                    "沙箱連接失敗 (user=%s, sandbox_id=%s): %s — 嘗試 resume",
                    user_id, sandbox_id, e,
                )

        # 3. 嘗試 resume（如果有 sandbox_id）
        if sandbox_id:
            try:
                logger.info("正在恢復沙箱 (user=%s, sandbox_id=%s)...", user_id, sandbox_id)
                sandbox = await Sandbox.resume(
                    sandbox_id,
                    connection_config=self._config,
                    resume_timeout=timedelta(seconds=settings.sandbox_ready_timeout_seconds),
                )

                logger.info("沙箱恢復成功 (user=%s, sandbox_id=%s)", user_id, sandbox_id)

                self._cache[user_id] = sandbox
                self._pushed_skills.setdefault(user_id, set())
                return sandbox

            except Exception as e:
                logger.warning(
                    "沙箱恢復失敗 (user=%s, sandbox_id=%s): %s — 將創建新沙箱",
                    user_id, sandbox_id, e,
                )

        # 4. 所有嘗試均失敗 → 創建新沙箱
        return await self.create(user_id)

    async def pause(self, user_id: str) -> bool:
        """暫停沙箱（用戶所有 session TTL 均過期時調用）

        Args:
            user_id: 用戶 ID

        Returns:
            是否成功暫停
        """
        sandbox = self._cache.pop(user_id, None)
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
        self._pushed_skills.pop(user_id, None)

        if not sandbox and sandbox_id:
            # 快取中沒有，嘗試 connect
            try:
                sandbox = await Sandbox.connect(
                    sandbox_id,
                    connection_config=self._config,
                )
            except Exception as e:
                logger.warning("connect 失敗 (sandbox_id=%s): %s — 嘗試 resume", sandbox_id, e)

            # connect 失敗，嘗試 resume
            if not sandbox and sandbox_id:
                try:
                    sandbox = await Sandbox.resume(
                        sandbox_id,
                        connection_config=self._config,
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
        mount_path = get_sandbox_mount_path()
        try:
            await sandbox.commands.run(
                f"rm -rf {mount_path}/* {mount_path}/.[!.]* 2>/dev/null || true"
            )
            logger.info("已清理沙箱掛載目錄文件 (user=%s, path=%s)", user_id, mount_path)
        except Exception as e:
            logger.warning("清理沙箱文件失敗 (user=%s): %s — 繼續銷毀容器", user_id, e)

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

    def get_mount_path(self) -> str:
        """獲取當前配置的容器掛載根目錄。"""
        return get_sandbox_mount_path()

    def get_cached(self, user_id: str) -> Sandbox | None:
        """獲取快取中的沙箱（不做健康檢查，用於工具層直接存取）"""
        return self._cache.get(user_id)

    def get_sandbox_id(self, user_id: str) -> str | None:
        """獲取沙箱 ID（用於存入 DB）"""
        sandbox = self._cache.get(user_id)
        return sandbox.id if sandbox else None

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
            files_to_push = self._collect_skill_files(skills_path, skills_path)

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
        files_to_push = self._collect_skill_files(skill_dir, skills_path)
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

        mount_path = get_sandbox_mount_path()
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

        results: list[dict] = []
        for skill_md_path in paths:
            try:
                content = await sandbox.files.read_file(skill_md_path)
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")
            except Exception as e:
                logger.debug("discover_sandbox_skills: 讀取 %s 失敗: %s", skill_md_path, e)
                continue

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
