"""OpenSandbox 會話服務測試

使用 mock 替代真實的 OpenSandbox SDK，測試 SandboxSessionService 的:
- 生命週期管理（create / get_or_resume / pause / kill / renew）
- 記憶體快取
- push_skills 文件上傳
- 全局單例
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ============== Fixtures ==============

@pytest.fixture(autouse=True)
def reset_singleton():
    """每個測試前重置單例，確保隔離"""
    from src.api.services.sandbox_service import SandboxSessionService
    SandboxSessionService._instance = None
    yield
    SandboxSessionService._instance = None


@pytest.fixture
def mock_settings():
    """模擬配置"""
    with patch("src.api.services.sandbox_service.get_settings") as mock:
        s = MagicMock()
        s.sandbox_domain = "test.sandbox.io"
        s.sandbox_api_key = "test-key"
        s.sandbox_protocol = "http"
        s.sandbox_use_server_proxy = True
        s.sandbox_timeout_minutes = 10
        s.sandbox_ready_timeout_seconds = 120
        s.sandbox_image = "test-image:v1"
        s.sandbox_persistent_storage_enabled = True
        s.sandbox_host_storage_root = "/tmp/sandbox"
        s.sandbox_storage_mount_path = "/home/user"
        mock.return_value = s
        yield s


@pytest.fixture
def service(mock_settings):
    """創建 SandboxSessionService 實例"""
    from src.api.services.sandbox_service import SandboxSessionService
    return SandboxSessionService()


# mock_sandbox 已在 conftest.py 中统一定义，此处不再重复


# ============== SandboxSessionService 初始化 ==============

class TestSandboxSessionServiceInit:
    """服務初始化測試"""

    def test_singleton(self, mock_settings):
        """測試單例模式"""
        from src.api.services.sandbox_service import SandboxSessionService
        s1 = SandboxSessionService()
        s2 = SandboxSessionService()
        assert s1 is s2

    def test_initial_cache_empty(self, service):
        """測試初始快取為空"""
        assert service.cache_size == 0

    def test_get_cached_returns_none(self, service):
        """測試從空快取獲取返回 None"""
        assert service.get_cached("nonexistent") is None

    def test_get_sandbox_id_returns_none(self, service):
        """測試從空快取獲取 sandbox_id 返回 None"""
        assert service.get_sandbox_id("nonexistent") is None


# ============== create ==============

class TestCreate:
    """沙箱創建測試"""

    @pytest.mark.asyncio
    async def test_create_success(self, service, mock_sandbox):
        """測試成功創建沙箱"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)

            result = await service.create("session-1")

            assert result is mock_sandbox
            assert service.get_cached("session-1") is mock_sandbox
            assert service.cache_size == 1
            MockSandbox.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_failure_raises(self, service):
        """測試創建失敗拋出 RuntimeError"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(side_effect=Exception("connection refused"))

            with pytest.raises(RuntimeError, match="沙箱創建失敗"):
                await service.create("session-1")

            assert service.cache_size == 0

    @pytest.mark.asyncio
    async def test_create_stores_in_cache(self, service, mock_sandbox):
        """測試創建後存入快取"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)
            await service.create("session-1")

            assert service.get_sandbox_id("session-1") == "sbx-test-123"

    @pytest.mark.asyncio
    async def test_create_with_persistent_volume(self, service, mock_sandbox):
        """測試 create 會帶入 session 專屬持久化卷"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)

            await service.create("session-abc")

            kwargs = MockSandbox.create.call_args.kwargs
            assert "volumes" in kwargs
            assert kwargs["volumes"] is not None
            assert len(kwargs["volumes"]) == 1
            volume = kwargs["volumes"][0]
            assert volume.mount_path == "/home/user"
            assert volume.host is not None
            assert volume.host.path.startswith("/tmp/sandbox/")

    @pytest.mark.asyncio
    async def test_create_without_persistent_volume_when_disabled(self, service, mock_sandbox, mock_settings):
        """測試可關閉持久化卷掛載"""
        mock_settings.sandbox_persistent_storage_enabled = False
        with patch("src.api.services.sandbox_service.settings", mock_settings):
            with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
                MockSandbox.create = AsyncMock(return_value=mock_sandbox)

                await service.create("session-no-volume")

                kwargs = MockSandbox.create.call_args.kwargs
                assert kwargs.get("volumes") is None

    @pytest.mark.asyncio
    async def test_create_with_custom_mount_path(self, service, mock_sandbox, mock_settings):
        """測試自定義 mount path 會生效到 volume 配置"""
        mock_settings.sandbox_persistent_storage_enabled = True
        mock_settings.sandbox_storage_mount_path = "/workspace/session-root"
        with patch("src.api.services.sandbox_service.settings", mock_settings):
            with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
                MockSandbox.create = AsyncMock(return_value=mock_sandbox)

                await service.create("session-custom-mount")

                kwargs = MockSandbox.create.call_args.kwargs
                volume = kwargs["volumes"][0]
                assert volume.mount_path == "/workspace/session-root"


class TestPathHelpers:
    """路徑解析輔助函式測試"""

    def test_get_sandbox_mount_path_normalized(self):
        from src.api.services import sandbox_service as sandbox_module
        mock_s = MagicMock()
        mock_s.sandbox_storage_mount_path = "/workspace/app/"
        with patch.object(sandbox_module, "settings", mock_s):
            assert sandbox_module.get_sandbox_mount_path() == "/workspace/app"

    def test_resolve_sandbox_path_relative(self):
        from src.api.services.sandbox_service import resolve_sandbox_path
        result = resolve_sandbox_path("docs/readme.md", "/workspace/app")
        assert result == "/workspace/app/docs/readme.md"

    def test_resolve_sandbox_path_absolute(self):
        from src.api.services.sandbox_service import resolve_sandbox_path
        result = resolve_sandbox_path("/tmp/x.txt", "/workspace/app")
        assert result == "/tmp/x.txt"

    def test_to_sandbox_relative_path(self):
        from src.api.services.sandbox_service import to_sandbox_relative_path
        result = to_sandbox_relative_path("/workspace/app/folder/a.txt", "/workspace/app")
        assert result == "folder/a.txt"

    def test_is_within_sandbox_root(self):
        from src.api.services.sandbox_service import is_within_sandbox_root
        assert is_within_sandbox_root("/workspace/app/a.txt", "/workspace/app") is True
        assert is_within_sandbox_root("/tmp/a.txt", "/workspace/app") is False


# ============== get_or_resume ==============

class TestGetOrResume:
    """獲取/恢復沙箱測試"""

    @pytest.mark.asyncio
    async def test_cache_hit_healthy(self, service, mock_sandbox):
        """測試快取命中且健康"""
        service._cache["session-1"] = mock_sandbox

        result = await service.get_or_resume("session-1")

        assert result is mock_sandbox
        mock_sandbox.is_healthy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_hit_unhealthy_falls_to_connect(self, service, mock_sandbox):
        """測試快取命中但不健康 -> 優先嘗試 connect"""
        unhealthy = AsyncMock()
        unhealthy.is_healthy = AsyncMock(return_value=False)
        service._cache["session-1"] = unhealthy

        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(return_value=mock_sandbox)
            result = await service.get_or_resume("session-1", "sbx-old-id")

        assert result is mock_sandbox
        assert service.get_cached("session-1") is mock_sandbox

    @pytest.mark.asyncio
    async def test_connect_from_sandbox_id(self, service, mock_sandbox):
        """測試通過 sandbox_id connect"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(return_value=mock_sandbox)

            result = await service.get_or_resume("session-1", "sbx-old-id")

            assert result is mock_sandbox
            MockSandbox.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_failure_falls_to_resume(self, service, mock_sandbox):
        """測試 connect 失敗 -> fallback 到 resume"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(side_effect=Exception("connect failed"))
            MockSandbox.resume = AsyncMock(return_value=mock_sandbox)

            result = await service.get_or_resume("session-1", "sbx-old-id")

            assert result is mock_sandbox
            MockSandbox.resume.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_failure_creates_new(self, service, mock_sandbox):
        """測試 resume 失敗 -> 創建新沙箱"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(side_effect=Exception("connect failed"))
            MockSandbox.resume = AsyncMock(side_effect=Exception("not found"))
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)

            result = await service.get_or_resume("session-1", "sbx-old-id")

            assert result is mock_sandbox
            MockSandbox.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_sandbox_id_creates_new(self, service, mock_sandbox):
        """測試沒有 sandbox_id -> 直接創建"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.create = AsyncMock(return_value=mock_sandbox)

            result = await service.get_or_resume("session-1", None)

            assert result is mock_sandbox
            MockSandbox.create.assert_awaited_once()


# ============== pause ==============

class TestPause:
    """暫停沙箱測試"""

    @pytest.mark.asyncio
    async def test_pause_success(self, service, mock_sandbox):
        """測試成功暫停"""
        service._cache["session-1"] = mock_sandbox

        result = await service.pause("session-1")

        assert result is True
        mock_sandbox.pause.assert_awaited_once()
        assert service.get_cached("session-1") is None

    @pytest.mark.asyncio
    async def test_pause_not_in_cache(self, service):
        """測試暫停不在快取中的沙箱"""
        result = await service.pause("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_pause_failure(self, service, mock_sandbox):
        """測試暫停失敗"""
        mock_sandbox.pause = AsyncMock(side_effect=Exception("network error"))
        service._cache["session-1"] = mock_sandbox

        result = await service.pause("session-1")

        assert result is False
        # 即使暫停失敗，也應該從快取中移除
        assert service.get_cached("session-1") is None


# ============== kill ==============

class TestKill:
    """銷毀沙箱測試"""

    @pytest.mark.asyncio
    async def test_kill_from_cache(self, service, mock_sandbox):
        """測試從快取中銷毀（含文件清理）"""
        service._cache["session-1"] = mock_sandbox

        result = await service.kill("session-1")

        assert result is True
        # 應先執行 rm -rf 清理命令
        mock_sandbox.commands.run.assert_awaited_once()
        cmd = mock_sandbox.commands.run.call_args[0][0]
        assert "rm -rf" in cmd
        # 再執行 kill
        mock_sandbox.kill.assert_awaited_once()
        assert service.get_cached("session-1") is None

    @pytest.mark.asyncio
    async def test_kill_by_sandbox_id(self, service, mock_sandbox):
        """測試通過 sandbox_id 銷毀（不在快取中）"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(return_value=mock_sandbox)

            result = await service.kill("session-1", "sbx-old-id")

            assert result is True
            # 應先清理文件再 kill
            mock_sandbox.commands.run.assert_awaited_once()
            mock_sandbox.kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kill_no_sandbox(self, service):
        """測試銷毀不存在的沙箱"""
        result = await service.kill("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_kill_connect_failure_then_resume_failure(self, service):
        """測試銷毀時 connect 和 resume 都失敗"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(side_effect=Exception("not found"))
            MockSandbox.resume = AsyncMock(side_effect=Exception("not found"))

            result = await service.kill("session-1", "sbx-dead-id")
            assert result is False

    @pytest.mark.asyncio
    async def test_kill_connect_fails_resume_succeeds(self, service, mock_sandbox):
        """測試 connect 失敗後 resume 成功，仍能清理文件並銷毀"""
        with patch("src.api.services.sandbox_service.Sandbox") as MockSandbox:
            MockSandbox.connect = AsyncMock(side_effect=Exception("expired"))
            MockSandbox.resume = AsyncMock(return_value=mock_sandbox)

            result = await service.kill("session-1", "sbx-expired-id")

            assert result is True
            # 應先清理文件
            mock_sandbox.commands.run.assert_awaited_once()
            cmd = mock_sandbox.commands.run.call_args[0][0]
            assert "rm -rf" in cmd
            # 再 kill
            mock_sandbox.kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kill_cleanup_fails_still_destroys(self, service, mock_sandbox):
        """清理命令失敗不應阻斷容器銷毀"""
        mock_sandbox.commands.run = AsyncMock(side_effect=RuntimeError("command failed"))
        service._cache["session-1"] = mock_sandbox

        result = await service.kill("session-1")

        assert result is True
        # 清理失敗但 kill 仍然被調用
        mock_sandbox.kill.assert_awaited_once()


# ============== renew ==============

class TestRenew:
    """續租沙箱測試"""

    @pytest.mark.asyncio
    async def test_renew_success(self, service, mock_sandbox):
        """測試成功續租"""
        service._cache["session-1"] = mock_sandbox

        result = await service.renew("session-1")

        assert result is True
        mock_sandbox.renew.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_renew_not_in_cache(self, service):
        """測試續租不在快取中的沙箱"""
        result = await service.renew("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_renew_failure(self, service, mock_sandbox):
        """測試續租失敗"""
        mock_sandbox.renew = AsyncMock(side_effect=Exception("timeout"))
        service._cache["session-1"] = mock_sandbox

        result = await service.renew("session-1")
        assert result is False


# ============== push_skills ==============

class TestPushSkills:
    """Skills 推送測試"""

    @pytest.mark.asyncio
    async def test_push_skills_success(self, service, mock_sandbox, tmp_path):
        """測試成功推送 skills"""
        service._cache["session-1"] = mock_sandbox

        # 創建測試 skill 目錄
        skill_dir = tmp_path / "skills" / "docx"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Docx Skill")

        result = await service.push_skills("session-1", str(tmp_path / "skills"))

        assert result is True
        mock_sandbox.files.write_files.assert_awaited_once()
        call_args = mock_sandbox.files.write_files.call_args[0][0]
        assert len(call_args) == 1
        from src.api.services.sandbox_service import get_sandbox_mount_path
        assert call_args[0].path == f"{get_sandbox_mount_path()}/skills/docx/SKILL.md"

    @pytest.mark.asyncio
    async def test_push_skills_not_in_cache(self, service, tmp_path):
        """測試沙箱不在快取中"""
        result = await service.push_skills("session-1", str(tmp_path))
        assert result is False

    @pytest.mark.asyncio
    async def test_push_skills_dir_not_exists(self, service, mock_sandbox):
        """測試 skills 目錄不存在"""
        service._cache["session-1"] = mock_sandbox

        result = await service.push_skills("session-1", "/nonexistent/path")
        assert result is False

    @pytest.mark.asyncio
    async def test_push_skills_skips_node_modules(self, service, mock_sandbox, tmp_path):
        """測試跳過 node_modules 目錄"""
        service._cache["session-1"] = mock_sandbox

        skill_dir = tmp_path / "skills" / "docx"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill")
        nm_dir = skill_dir / "node_modules" / "pkg"
        nm_dir.mkdir(parents=True)
        (nm_dir / "index.js").write_text("module.exports = {}")

        result = await service.push_skills("session-1", str(tmp_path / "skills"))

        assert result is True
        # 只上傳 SKILL.md，不上傳 node_modules
        call_args = mock_sandbox.files.write_files.call_args[0][0]
        assert len(call_args) == 1
        assert "node_modules" not in call_args[0].path

    @pytest.mark.asyncio
    async def test_push_skills_empty_dir(self, service, mock_sandbox, tmp_path):
        """測試空 skills 目錄"""
        service._cache["session-1"] = mock_sandbox
        skills_dir = tmp_path / "empty_skills"
        skills_dir.mkdir()

        result = await service.push_skills("session-1", str(skills_dir))
        assert result is True


class TestPushSkillLazy:
    """按需推送單一 skill 測試"""

    @pytest.mark.asyncio
    async def test_push_skill_success(self, service, mock_sandbox, tmp_path):
        service._cache["session-1"] = mock_sandbox

        skills_root = tmp_path / "skills"
        skill_dir = skills_root / "document-skills" / "pdf"
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: pdf\ndescription: pdf skill\n---\n")
        (scripts_dir / "read_pdf.py").write_text("print('ok')")

        result = await service.push_skill("session-1", str(skills_root), "pdf")

        assert result is True
        mock_sandbox.files.write_files.assert_awaited_once()
        entries = mock_sandbox.files.write_files.call_args[0][0]
        paths = [entry.path for entry in entries]
        from src.api.services.sandbox_service import get_sandbox_mount_path
        skills_root = f"{get_sandbox_mount_path()}/skills/document-skills/pdf"
        assert f"{skills_root}/SKILL.md" in paths
        assert f"{skills_root}/scripts/read_pdf.py" in paths

    @pytest.mark.asyncio
    async def test_push_skill_skip_when_already_pushed(self, service, mock_sandbox, tmp_path):
        service._cache["session-1"] = mock_sandbox
        service._pushed_skills["session-1"] = {"pdf"}

        result = await service.push_skill("session-1", str(tmp_path), "pdf")

        assert result is True
        mock_sandbox.files.write_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_push_skill_not_found(self, service, mock_sandbox, tmp_path):
        service._cache["session-1"] = mock_sandbox
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        result = await service.push_skill("session-1", str(skills_root), "unknown")

        assert result is False


# ============== get_sandbox_service ==============

class TestGetSandboxService:
    """全局服務存取測試"""

    def test_get_sandbox_service_returns_instance(self, mock_settings):
        """測試獲取全局服務實例"""
        from src.api.services import sandbox_service as mod
        mod._sandbox_service = None  # 重置

        svc = mod.get_sandbox_service()
        assert isinstance(svc, mod.SandboxSessionService)

    def test_get_sandbox_service_is_stable(self, mock_settings):
        """測試多次調用返回同一實例"""
        from src.api.services import sandbox_service as mod
        mod._sandbox_service = None

        svc1 = mod.get_sandbox_service()
        svc2 = mod.get_sandbox_service()
        assert svc1 is svc2


# ============== discover_sandbox_skills ==============

class TestDiscoverSandboxSkills:
    """沙箱端用戶 Skill 發現測試"""

    @pytest.fixture
    def sandbox_with_skills(self, mock_sandbox):
        """模擬沙箱中含有用戶 Skill 的場景"""
        # find 命令返回 SKILL.md 路徑列表
        exec_result = MagicMock()
        exec_result.logs = MagicMock()
        exec_result.logs.stdout = (
            "/home/user/skills/industry-report/SKILL.md\n"
            "/home/user/skills/custom-tool/SKILL.md\n"
        )
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        # read_file 依次返回兩個 SKILL.md 的內容
        skill_contents = {
            "/home/user/skills/industry-report/SKILL.md": (
                "---\nname: industry-report\ndescription: 行业研究报告\n---\n## Usage\n"
            ),
            "/home/user/skills/custom-tool/SKILL.md": (
                "---\nname: custom-tool\ndescription: Custom tool\n---\n## Custom\n"
            ),
        }

        async def _read_file(path):
            return skill_contents.get(path, "")

        mock_sandbox.files.read_file = AsyncMock(side_effect=_read_file)
        return mock_sandbox

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_basic(self, service, sandbox_with_skills):
        """測試基本發現功能"""
        service._cache["user-1"] = sandbox_with_skills

        results = await service.discover_sandbox_skills("user-1")

        assert len(results) == 2
        names = {r["name"] for r in results}
        assert "industry-report" in names
        assert "custom-tool" in names
        assert results[0]["sandbox_skill_dir"] == "/home/user/skills/industry-report"

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_dedup_official(self, service, sandbox_with_skills):
        """測試去除與官方同名的 Skill"""
        service._cache["user-1"] = sandbox_with_skills

        results = await service.discover_sandbox_skills(
            "user-1",
            official_skill_names={"custom-tool"},
        )

        assert len(results) == 1
        assert results[0]["name"] == "industry-report"

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_sandbox_not_cached(self, service):
        """測試沙箱不在快取中返回空列表"""
        results = await service.discover_sandbox_skills("user-not-exist")
        assert results == []

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_find_fails(self, service, mock_sandbox):
        """測試 find 命令失敗時返回空列表"""
        service._cache["user-1"] = mock_sandbox
        mock_sandbox.commands.run = AsyncMock(side_effect=Exception("timeout"))

        results = await service.discover_sandbox_skills("user-1")
        assert results == []

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_no_skills(self, service, mock_sandbox):
        """測試沙箱中無 Skill 時返回空列表"""
        service._cache["user-1"] = mock_sandbox
        exec_result = MagicMock()
        exec_result.logs = MagicMock()
        exec_result.logs.stdout = ""
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        results = await service.discover_sandbox_skills("user-1")
        assert results == []

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_logs_none(self, service, mock_sandbox):
        """測試 find 命令返回 logs=None（靜默失敗）"""
        service._cache["user-1"] = mock_sandbox
        exec_result = MagicMock()
        exec_result.logs = None
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        results = await service.discover_sandbox_skills("user-1")
        assert results == []

    @pytest.mark.asyncio
    async def test_discover_sandbox_skills_read_file_fails(self, service, mock_sandbox):
        """測試單個 SKILL.md 讀取失敗時跳過，不影響其他"""
        service._cache["user-1"] = mock_sandbox

        exec_result = MagicMock()
        exec_result.logs = MagicMock()
        exec_result.logs.stdout = (
            "/home/user/skills/good/SKILL.md\n"
            "/home/user/skills/bad/SKILL.md\n"
        )
        mock_sandbox.commands.run = AsyncMock(return_value=exec_result)

        async def _read_file(path):
            if "bad" in path:
                raise IOError("read failed")
            return "---\nname: good-skill\ndescription: Good\n---\ncontent"

        mock_sandbox.files.read_file = AsyncMock(side_effect=_read_file)

        results = await service.discover_sandbox_skills("user-1")
        assert len(results) == 1
        assert results[0]["name"] == "good-skill"


# ============== read_sandbox_skill_content ==============

class TestReadSandboxSkillContent:
    """從沙箱讀取用戶 Skill 完整內容測試"""

    @pytest.mark.asyncio
    async def test_read_success(self, service, mock_sandbox):
        """測試成功讀取 Skill 內容（去除 frontmatter）"""
        service._cache["user-1"] = mock_sandbox
        mock_sandbox.files.read_file = AsyncMock(
            return_value="---\nname: my-skill\ndescription: Test\n---\n## Usage\n\nDo something."
        )

        content = await service.read_sandbox_skill_content(
            "user-1", "/home/user/skills/my-skill"
        )

        assert content is not None
        assert "## Usage" in content
        assert "Do something." in content
        # frontmatter 已被去除
        assert "---" not in content
        assert "name: my-skill" not in content

    @pytest.mark.asyncio
    async def test_read_no_frontmatter(self, service, mock_sandbox):
        """測試沒有 frontmatter 的內容"""
        service._cache["user-1"] = mock_sandbox
        mock_sandbox.files.read_file = AsyncMock(return_value="Just plain content")

        content = await service.read_sandbox_skill_content(
            "user-1", "/home/user/skills/plain"
        )

        assert content == "Just plain content"

    @pytest.mark.asyncio
    async def test_read_sandbox_not_cached(self, service):
        """測試沙箱不在快取中"""
        content = await service.read_sandbox_skill_content(
            "user-not-exist", "/home/user/skills/x"
        )
        assert content is None

    @pytest.mark.asyncio
    async def test_read_file_fails(self, service, mock_sandbox):
        """測試讀取失敗"""
        service._cache["user-1"] = mock_sandbox
        mock_sandbox.files.read_file = AsyncMock(side_effect=Exception("not found"))

        content = await service.read_sandbox_skill_content(
            "user-1", "/home/user/skills/broken"
        )
        assert content is None

    @pytest.mark.asyncio
    async def test_read_bytes_content(self, service, mock_sandbox):
        """測試 read_file 返回 bytes 時自動解碼"""
        service._cache["user-1"] = mock_sandbox
        raw = "---\nname: b\ndescription: B\n---\n## Bytes Content".encode("utf-8")
        mock_sandbox.files.read_file = AsyncMock(return_value=raw)

        content = await service.read_sandbox_skill_content(
            "user-1", "/home/user/skills/bytes-skill"
        )

        assert content is not None
        assert "## Bytes Content" in content


# ============== _extract_skill_description_from_skill_md ==============

class TestExtractSkillDescription:
    """提取 Skill description 輔助方法測試"""

    def test_basic_extraction(self, service):
        text = "---\nname: foo\ndescription: A foo skill\n---\ncontent"
        result = service._extract_skill_description_from_skill_md(text)
        assert result == "A foo skill"

    def test_quoted_description(self, service):
        text = '---\nname: bar\ndescription: "Quoted desc"\n---\n'
        result = service._extract_skill_description_from_skill_md(text)
        assert result == "Quoted desc"

    def test_no_description(self, service):
        text = "---\nname: baz\n---\ncontent"
        result = service._extract_skill_description_from_skill_md(text)
        assert result is None

    def test_no_frontmatter(self, service):
        text = "Just content"
        result = service._extract_skill_description_from_skill_md(text)
        assert result is None
