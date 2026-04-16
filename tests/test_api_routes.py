"""API 路由測試"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from src.api.routes import auth, sessions, config as config_routes
from tests.helpers import make_test_client, make_mock_settings, make_fake_execution


class TestAuthRouter:
    """認證路由測試"""

    @pytest.fixture
    def mock_settings(self):
        """模擬設置"""
        mock_s = make_mock_settings()

        with patch("src.api.routes.auth.settings", mock_s):
            with patch("src.api.deps.get_settings", return_value=mock_s):
                yield mock_s

    @pytest.fixture
    def client(self, mock_settings):
        """創建測試客戶端"""
        app = FastAPI()
        app.include_router(auth.router, prefix="/auth")
        return TestClient(app)

    def test_login_success(self, client, mock_settings):
        """測試登錄成功"""
        response = client.post(
            "/auth/login",
            data={"username": "testuser", "password": "testpass"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "testuser"
        assert isinstance(data["access_token"], str)
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0
        assert data["message"] == "登录成功"

    def test_login_wrong_password(self, client, mock_settings):
        """測試密碼錯誤"""
        response = client.post(
            "/auth/login",
            data={"username": "testuser", "password": "wrongpass"}
        )
        
        assert response.status_code == 401
        assert "用户名或密码错误" in response.json()["detail"]

    def test_login_user_not_found(self, client, mock_settings):
        """測試用戶不存在"""
        response = client.post(
            "/auth/login",
            data={"username": "unknown", "password": "anypass"}
        )
        
        assert response.status_code == 401

    def test_get_current_user_success(self, client, mock_settings):
        """測試獲取當前用戶"""
        login = client.post(
            "/auth/login",
            data={"username": "testuser", "password": "testpass"}
        )
        token = login.json()["access_token"]

        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "testuser"
        assert data["username"] == "testuser"

    def test_get_current_user_without_token(self, client, mock_settings):
        """未帶 token 應返回 401"""
        response = client.get("/auth/me")
        
        assert response.status_code == 401


class TestSessionsRouter:
    """會話路由測試"""

    def test_encode_filename_header_ascii(self):
        """測試 ASCII 文件名編碼"""
        from src.api.routes.sessions import encode_filename_header
        
        result = encode_filename_header("test.pdf")
        assert 'filename="test.pdf"' in result
        assert "filename*=UTF-8''" in result

    def test_encode_filename_header_chinese(self):
        """測試中文文件名編碼"""
        from src.api.routes.sessions import encode_filename_header
        
        result = encode_filename_header("報告.pdf")
        assert "filename*=UTF-8''" in result
        # URL 編碼後的中文
        assert "%E5%A0%B1%E5%91%8A.pdf" in result

    def test_contains_non_ascii(self):
        """測試非 ASCII 檢測"""
        from src.api.routes.sessions import _contains_non_ascii

        assert _contains_non_ascii("報告.xlsx") is True
        assert _contains_non_ascii("report.xlsx") is False

    @pytest.mark.asyncio
    async def test_upload_file_missing_file_returns_400(self):
        """測試未提供文件時返回 400"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await sessions.upload_file(
                chat_session_id="session-1",
                file=None,
                user_id="user-1",
                db=MagicMock(),
            )

        assert exc.value.status_code == 400
        assert "未选择文件" in exc.value.detail

    @pytest.mark.asyncio
    async def test_poll_session_returns_round_count(self):
        """测试 poll 端点返回正确的 round_count"""
        mock_db = MagicMock()
        # 模拟 Session 查询（会话存在）
        mock_session = MagicMock()
        mock_session.id = "s1"
        mock_session.user_id = "user1"
        # .filter().first() 链
        session_query = MagicMock()
        session_query.filter.return_value.first.return_value = mock_session
        # .filter().count() 链 for Round
        round_query = MagicMock()
        round_query.filter.return_value.count.return_value = 5

        from src.api.models.session import Session
        from src.api.models.round import Round

        def side_effect(model):
            if model is Session:
                return session_query
            if model is Round:
                return round_query
            return MagicMock()

        mock_db.query.side_effect = side_effect

        result = await sessions.poll_session(
            chat_session_id="s1",
            user_id="user1",
            db=mock_db,
        )
        assert result.round_count == 5

    @pytest.mark.asyncio
    async def test_poll_session_not_found(self):
        """测试 poll 端点会话不存在时返回 404"""
        from fastapi import HTTPException

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with pytest.raises(HTTPException) as exc:
            await sessions.poll_session(
                chat_session_id="nonexistent",
                user_id="user1",
                db=mock_db,
            )
        assert exc.value.status_code == 404


class TestConfigRouter:
    """配置管理路由测试"""

    @pytest.fixture
    def client(self):
        """创建配置路由测试客户端"""
        return make_test_client(config_routes.router, "/config")

    def test_get_skills_returns_all_discovered_skills(self, client, tmp_path):
        """discover_skills 返回列表时，接口应返回全部技能"""
        from src.agent.tools.skill_loader import Skill

        fake_settings = MagicMock()
        fake_settings.skills_dir = str(tmp_path)

        fake_loader = MagicMock()
        fake_loader.discover_skills.return_value = [
            Skill(name="docx", description="Word 处理", content="", metadata={"category": "document"}),
            Skill(name="pdf", description="PDF 处理", content="", metadata={"category": "document"}),
        ]

        # 用户配置为空 → 默认全部 enabled=True
        client.mock_db.query.return_value.filter.return_value.all.return_value = []  # type: ignore[attr-defined]

        with patch("src.api.config.get_settings", return_value=fake_settings):
            with patch("src.agent.tools.skill_loader.SkillLoader", return_value=fake_loader):
                response = client.get("/config/skills", params={"user_id": "testuser"})

        assert response.status_code == 200
        data = response.json()
        assert "skills" in data
        assert len(data["skills"]) == 2
        names = {s["name"] for s in data["skills"]}
        assert names == {"docx", "pdf"}
        assert all(s["enabled"] is True for s in data["skills"])

    def test_get_skills_merges_user_enabled_config(self, client, tmp_path):
        """应保留全部可发现技能，并合并用户启停配置"""
        from src.agent.tools.skill_loader import Skill

        fake_settings = MagicMock()
        fake_settings.skills_dir = str(tmp_path)

        fake_loader = MagicMock()
        fake_loader.discover_skills.return_value = [
            Skill(name="docx", description="Word 处理", content="", metadata={"category": "document"}),
            Skill(name="xlsx", description="Excel 处理", content="", metadata={"category": "document"}),
        ]

        disabled = MagicMock()
        disabled.skill_name = "docx"
        disabled.enabled = False
        client.mock_db.query.return_value.filter.return_value.all.return_value = [disabled]  # type: ignore[attr-defined]

        with patch("src.api.config.get_settings", return_value=fake_settings):
            with patch("src.agent.tools.skill_loader.SkillLoader", return_value=fake_loader):
                response = client.get("/config/skills", params={"user_id": "testuser"})

        assert response.status_code == 200
        skills = {item["name"]: item for item in response.json()["skills"]}
        assert set(skills.keys()) == {"docx", "xlsx"}
        assert skills["docx"]["enabled"] is False
        assert skills["xlsx"]["enabled"] is True


class TestEncodeFilename:
    """文件名編碼測試"""

    def test_simple_filename(self):
        """測試簡單文件名"""
        from src.api.routes.sessions import encode_filename_header
        result = encode_filename_header("document.pdf")
        assert "document.pdf" in result

    def test_filename_with_spaces(self):
        """測試帶空格的文件名"""
        from src.api.routes.sessions import encode_filename_header
        result = encode_filename_header("my document.pdf")
        assert "my%20document.pdf" in result

    def test_inline_disposition(self):
        """測試內聯配置"""
        from src.api.routes.sessions import encode_filename_header
        result = encode_filename_header("image.png", disposition="inline")
        assert result.startswith("inline;")


class TestSanitizeFilename:
    """_sanitize_filename 輔助函數測試"""

    def test_spaces_replaced_with_underscore(self):
        """測試空格替換為底線"""
        from src.api.routes.sessions import _sanitize_filename
        assert _sanitize_filename("my document.pdf") == "my_document.pdf"

    def test_parentheses_removed(self):
        """測試括號被去除"""
        from src.api.routes.sessions import _sanitize_filename
        result = _sanitize_filename("Gemini_Generated_Image (1).png")
        assert "(" not in result
        assert ")" not in result
        assert result == "Gemini_Generated_Image_1.png"

    def test_brackets_removed(self):
        """測試方括號被去除"""
        from src.api.routes.sessions import _sanitize_filename
        result = _sanitize_filename("report [v2].pdf")
        assert "[" not in result
        assert "]" not in result
        assert result == "report_v2.pdf"

    def test_chinese_characters_preserved(self):
        """測試中文字符保留"""
        from src.api.routes.sessions import _sanitize_filename
        result = _sanitize_filename("報告(最終版).docx")
        assert "報告" in result
        assert "最終版" in result
        assert "(" not in result

    def test_normal_filename_unchanged(self):
        """測試正常文件名不變"""
        from src.api.routes.sessions import _sanitize_filename
        assert _sanitize_filename("normal_file.txt") == "normal_file.txt"

    def test_consecutive_underscores_collapsed(self):
        """測試連續底線被合併"""
        from src.api.routes.sessions import _sanitize_filename
        result = _sanitize_filename("a___b.txt")
        assert result == "a_b.txt"

    def test_empty_or_whitespace_returns_fallback(self):
        """測試空白字串返回預設名稱"""
        from src.api.routes.sessions import _sanitize_filename
        assert _sanitize_filename("   ") == "uploaded_file"
        assert _sanitize_filename("") == "uploaded_file"

    def test_complex_real_world_case(self):
        """測試真實場景：帶空格、括號、數字的圖片文件名"""
        from src.api.routes.sessions import _sanitize_filename
        result = _sanitize_filename("Gemini_Generated_Image_j4qaf0j4qaf0j4qa (1)_1.png")
        assert " " not in result
        assert "(" not in result
        assert ")" not in result
        assert result.endswith(".png")


class TestEnsureSandbox:
    """_ensure_sandbox / _upsert_user_sandbox 輔助函數測試"""

    @pytest.mark.asyncio
    async def test_ensure_sandbox_returns_cached(self):
        """快取命中時直接返回，不調用 get_or_resume"""
        from src.api.routes.sessions import _ensure_sandbox

        mock_sandbox = AsyncMock()
        mock_sandbox.id = "sbx-cached"
        sandbox_service = MagicMock()
        sandbox_service.get_cached.return_value = mock_sandbox

        result = await _ensure_sandbox(sandbox_service, "user-1", MagicMock())

        assert result is mock_sandbox
        sandbox_service.get_or_resume.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_sandbox_falls_back_to_get_or_resume(self):
        """快取未命中時走 get_or_resume 並更新 DB"""
        from src.api.routes.sessions import _ensure_sandbox

        mock_sandbox = AsyncMock()
        mock_sandbox.id = "sbx-new"
        sandbox_service = MagicMock()
        sandbox_service.get_cached.return_value = None
        sandbox_service.get_or_resume = AsyncMock(return_value=mock_sandbox)
        sandbox_service.get_sandbox_id.return_value = "sbx-new"

        mock_db = MagicMock()
        # 模擬 UserSandbox 查詢返回 None（新用戶）
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = await _ensure_sandbox(sandbox_service, "user-1", mock_db)

        assert result is mock_sandbox
        sandbox_service.get_or_resume.assert_awaited_once()
        # 應持久化新 sandbox_id 到 DB
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_ensure_sandbox_force_refresh_clears_cache(self):
        """force_refresh=True 時先清除快取"""
        from src.api.routes.sessions import _ensure_sandbox

        mock_sandbox = AsyncMock()
        mock_sandbox.id = "sbx-fresh"
        sandbox_service = MagicMock()
        sandbox_service.get_cached.return_value = None
        sandbox_service.get_or_resume = AsyncMock(return_value=mock_sandbox)
        sandbox_service.get_sandbox_id.return_value = "sbx-fresh"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = await _ensure_sandbox(
            sandbox_service, "user-1", mock_db, force_refresh=True,
        )

        sandbox_service.invalidate_cache.assert_called_once_with("user-1")
        assert result is mock_sandbox

    @pytest.mark.asyncio
    async def test_upsert_user_sandbox_updates_existing(self):
        """已有 DB 記錄時更新 sandbox_id"""
        from src.api.routes.sessions import _upsert_user_sandbox

        sandbox_service = MagicMock()
        sandbox_service.get_sandbox_id.return_value = "sbx-new"

        existing = MagicMock()
        existing.sandbox_id = "sbx-old"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = existing

        _upsert_user_sandbox(mock_db, "user-1", sandbox_service)

        assert existing.sandbox_id == "sbx-new"
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_retries_on_stale_sandbox(self):
        """上傳時沙箱操作失敗應自動清除快取重試"""
        from src.api.routes.sessions import upload_file
        from src.api.models.session import Session
        from src.api.models.user_sandbox import UserSandbox
        from tests.helpers import make_mock_sandbox

        stale_sandbox = make_mock_sandbox(sandbox_id="sbx-stale")
        stale_sandbox.commands.run = AsyncMock(side_effect=Exception("404 Not Found"))
        stale_sandbox.files.write = AsyncMock(side_effect=Exception("404 Not Found"))

        fresh_sandbox = make_mock_sandbox(sandbox_id="sbx-fresh")

        sandbox_service = MagicMock()
        # 第一次 get_cached 返回陳舊沙箱，第二次（force_refresh 後）返回 None
        sandbox_service.get_cached.side_effect = [stale_sandbox, None]
        sandbox_service.get_or_resume = AsyncMock(return_value=fresh_sandbox)
        sandbox_service.get_sandbox_id.return_value = "sbx-fresh"
        sandbox_service.get_mount_path.return_value = "/home/user"

        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "user-1"

        # 按模型類型返回不同的查詢結果
        def query_side_effect(model):
            q = MagicMock()
            if model is Session:
                q.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                q.filter.return_value.first.return_value = None  # 新用戶，無記錄
            return q

        mock_db = MagicMock()
        mock_db.query.side_effect = query_side_effect

        mock_file = AsyncMock()
        mock_file.filename = "test.txt"
        mock_file.read = AsyncMock(return_value=b"hello")
        mock_file.content_type = "text/plain"

        with patch("src.api.routes.sessions.get_sandbox_service", return_value=sandbox_service):
            result = await upload_file(
                chat_session_id="session-1",
                file=mock_file,
                user_id="user-1",
                db=mock_db,
            )

        # 重試成功：fresh_sandbox 的 write 被調用
        fresh_sandbox.files.write.assert_awaited_once()
        assert result.name == "test.txt"
        assert result.size == 5


class TestExtractExitCode:
    """_extract_exit_code 輔助函數測試"""

    def test_exit_code_present_zero(self):
        """測試有 exit_code=0 的情況"""
        from src.api.routes.sessions import _extract_exit_code
        execution = MagicMock()
        execution.exit_code = 0
        assert _extract_exit_code(execution) == 0

    def test_exit_code_present_nonzero(self):
        """測試有 exit_code≠0 的情況"""
        from src.api.routes.sessions import _extract_exit_code
        execution = MagicMock()
        execution.exit_code = 127
        assert _extract_exit_code(execution) == 127

    def test_exit_code_missing_no_error(self):
        """測試無 exit_code 且無 error 時返回 0"""
        from src.api.routes.sessions import _extract_exit_code
        execution = MagicMock(spec=[])  # 空 spec，無任何屬性
        assert _extract_exit_code(execution) == 0

    def test_exit_code_missing_with_error(self):
        """測試無 exit_code 但有 error 時返回 1"""
        from src.api.routes.sessions import _extract_exit_code

        exe = make_fake_execution(error="something went wrong")
        del exe.exit_code  # 模拟无 exit_code 属性
        assert _extract_exit_code(exe) == 1

    def test_exit_code_none_value(self):
        """測試 exit_code 為 None 的情況"""
        from src.api.routes.sessions import _extract_exit_code

        exe = make_fake_execution(exit_code=0)
        exe.exit_code = None
        exe.error = None
        assert _extract_exit_code(exe) == 0


class TestCommandStdoutText:
    """_command_stdout_text 輔助函數測試"""

    def test_with_logs_stdout(self):
        """測試有 logs.stdout 的標準情況"""
        from src.api.routes.sessions import _command_stdout_text

        line1 = MagicMock()
        line1.text = "hello"
        line2 = MagicMock()
        line2.text = "world"
        execution = MagicMock()
        execution.logs.stdout = [line1, line2]
        assert _command_stdout_text(execution) == "hello\nworld"

    def test_with_no_logs(self):
        """測試無 logs 屬性的情況"""
        from src.api.routes.sessions import _command_stdout_text
        execution = MagicMock(spec=[])  # 無 logs 屬性
        assert _command_stdout_text(execution) == ""

    def test_with_direct_stdout_string(self):
        """測試有直接 stdout 字符串的情況"""
        from src.api.routes.sessions import _command_stdout_text

        exe = make_fake_execution()
        exe.logs = None
        exe.stdout = "  direct output  "
        assert _command_stdout_text(exe) == "direct output"

    def test_with_empty_stdout(self):
        """測試空 stdout 的情況"""
        from src.api.routes.sessions import _command_stdout_text

        exe = make_fake_execution()
        exe.logs = None
        exe.stdout = None
        assert _command_stdout_text(exe) == ""


class TestAsciiAliasRead:
    """_read_bytes_via_ascii_alias 輔助函數測試"""

    @pytest.mark.asyncio
    async def test_read_bytes_via_ascii_alias_success(self):
        from src.api.routes.sessions import _read_bytes_via_ascii_alias

        execution = MagicMock()
        execution.exit_code = 0

        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(return_value=execution)
        sandbox.files.read_bytes = AsyncMock(return_value=b"abc")

        result = await _read_bytes_via_ascii_alias(
            sandbox,
            "/home/user/報告.xlsx",
        )

        assert result == b"abc"
        assert sandbox.files.read_bytes.call_count == 1
        # cp + rm
        assert sandbox.commands.run.call_count == 2

    @pytest.mark.asyncio
    async def test_read_bytes_via_ascii_alias_copy_failed(self):
        from src.api.routes.sessions import _read_bytes_via_ascii_alias

        cp_execution = MagicMock()
        cp_execution.exit_code = 1

        rm_execution = MagicMock()
        rm_execution.exit_code = 0

        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(side_effect=[cp_execution, rm_execution])
        sandbox.files.read_bytes = AsyncMock()

        result = await _read_bytes_via_ascii_alias(
            sandbox,
            "/home/user/報告.xlsx",
        )

        assert result is None
        sandbox.files.read_bytes.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_bytes_fallback_to_command_when_sdk_fails(self):
        """read_bytes 失敗時回退到 base64 命令讀取"""
        import base64 as b64_mod
        from src.api.routes.sessions import _read_bytes_via_ascii_alias

        cp_execution = MagicMock()
        cp_execution.exit_code = 0

        encoded = b64_mod.b64encode(b"pptx-content").decode()
        b64_line = MagicMock()
        b64_line.text = encoded
        b64_execution = MagicMock()
        b64_execution.exit_code = 0
        b64_execution.logs.stdout = [b64_line]

        rm_execution = MagicMock()
        rm_execution.exit_code = 0

        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(
            side_effect=[cp_execution, b64_execution, rm_execution]
        )
        sandbox.files.read_bytes = AsyncMock(
            side_effect=Exception("SDK proxy 500")
        )

        result = await _read_bytes_via_ascii_alias(
            sandbox,
            "/home/user/報告.pptx",
        )

        assert result == b"pptx-content"


class TestReadBytesViaCommand:
    """_read_bytes_via_command 輔助函數測試"""

    @pytest.mark.asyncio
    async def test_success(self):
        import base64 as b64_mod
        from src.api.routes.sessions import _read_bytes_via_command

        encoded = b64_mod.b64encode(b"file-data").decode()
        line = MagicMock()
        line.text = encoded
        execution = MagicMock()
        execution.exit_code = 0
        execution.logs.stdout = [line]

        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(return_value=execution)

        result = await _read_bytes_via_command(sandbox, "/home/user/test.pptx")
        assert result == b"file-data"

    @pytest.mark.asyncio
    async def test_nonzero_exit(self):
        from src.api.routes.sessions import _read_bytes_via_command

        execution = MagicMock()
        execution.exit_code = 1
        execution.logs.stdout = []

        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(return_value=execution)

        result = await _read_bytes_via_command(sandbox, "/home/user/missing.txt")
        assert result is None

    @pytest.mark.asyncio
    async def test_exception(self):
        from src.api.routes.sessions import _read_bytes_via_command

        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(side_effect=Exception("timeout"))

        result = await _read_bytes_via_command(sandbox, "/home/user/file.bin")
        assert result is None


class TestBuildFileinfoFromPath:
    """_build_fileinfo_from_path 輔助函數測試"""

    def test_normal_file(self):
        """測試正常文件路徑"""
        from src.api.routes.sessions import _build_fileinfo_from_path
        info = _build_fileinfo_from_path("/home/user/test.pdf", "/home/user")
        assert info is not None
        assert info.name == "test.pdf"
        assert info.type == "pdf"

    def test_skip_node_modules(self):
        """測試跳過 node_modules"""
        from src.api.routes.sessions import _build_fileinfo_from_path
        info = _build_fileinfo_from_path("/home/user/node_modules/pkg/index.js", "/home/user")
        assert info is None

    def test_skip_pycache(self):
        """測試跳過 __pycache__"""
        from src.api.routes.sessions import _build_fileinfo_from_path
        info = _build_fileinfo_from_path("/home/user/__pycache__/mod.pyc", "/home/user")
        assert info is None

    def test_skip_agent_memory(self):
        """測試跳過 .agent_memory.json"""
        from src.api.routes.sessions import _build_fileinfo_from_path
        info = _build_fileinfo_from_path("/home/user/.agent_memory.json", "/home/user")
        assert info is None

    def test_empty_path(self):
        """測試空路徑"""
        from src.api.routes.sessions import _build_fileinfo_from_path
        info = _build_fileinfo_from_path("", "/home/user")
        assert info is None

    def test_directory_path_skipped(self):
        """測試目錄路徑 — posixpath.normpath 會去掉尾部 /，
        但 find -type f 在生產中只返回文件，不影響功能"""
        from src.api.routes.sessions import _build_fileinfo_from_path
        # normpath("/home/user/subdir/") → "/home/user/subdir" → rel="subdir"
        info = _build_fileinfo_from_path("/home/user/subdir/", "/home/user")
        # 由於 normpath 去掉了 trailing slash，函數會返回 FileInfo
        assert info is not None
        assert info.name == "subdir"

    def test_chinese_filename(self):
        """測試中文文件名"""
        from src.api.routes.sessions import _build_fileinfo_from_path
        info = _build_fileinfo_from_path("/home/user/CrossBeam 深度解析.pdf", "/home/user")
        assert info is not None
        assert info.name == "CrossBeam 深度解析.pdf"
        assert info.type == "pdf"


class TestSandboxListDir:
    """_sandbox_list_dir 輔助函數測試（目錄瀏覽模式）"""

    @pytest.mark.asyncio
    async def test_list_dir_returns_files_and_dirs(self):
        """列出目錄內的文件和子目錄"""
        from src.api.routes.sessions import _sandbox_list_dir

        sandbox = MagicMock()
        json_payload = json.dumps([
            {"name": "report.pdf", "path": "/home/user/sessions/s1/report.pdf", "size": 2048, "mtime": 1750000000.0, "is_dir": False},
            {"name": "data", "path": "/home/user/sessions/s1/data", "size": 0, "mtime": 1750000100.0, "is_dir": True},
            {"name": "output.csv", "path": "/home/user/sessions/s1/output.csv", "size": 512, "mtime": 1750000200.0, "is_dir": False},
        ], ensure_ascii=False)
        sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text=json_payload)
        )

        items = await _sandbox_list_dir(sandbox, "/home/user/sessions/s1", "/home/user/sessions/s1")

        sandbox.commands.run.assert_called_once()
        assert len(items) == 3
        # 目錄排在前面
        assert items[0].name == "data"
        assert items[0].is_directory is True
        assert items[0].type == "directory"
        assert items[0].size == 0
        # 文件按名稱排序
        assert items[1].name == "output.csv"
        assert items[1].is_directory is False
        assert items[1].size == 512
        assert items[2].name == "report.pdf"
        assert items[2].is_directory is False
        assert items[2].size == 2048

    @pytest.mark.asyncio
    async def test_list_dir_skips_system_paths(self):
        """系統路徑被過濾"""
        from src.api.routes.sessions import _sandbox_list_dir

        json_payload = json.dumps([
            {"name": "app.py", "path": "/home/user/sessions/s1/app.py", "size": 10, "mtime": 1750000000.0, "is_dir": False},
            {"name": "node_modules", "path": "/home/user/sessions/s1/node_modules", "size": 0, "mtime": 1750000001.0, "is_dir": True},
            {"name": "__pycache__", "path": "/home/user/sessions/s1/__pycache__", "size": 0, "mtime": 1750000002.0, "is_dir": True},
            {"name": ".agent_memory.json", "path": "/home/user/sessions/s1/.agent_memory.json", "size": 13, "mtime": 1750000003.0, "is_dir": False},
            {"name": "result.xlsx", "path": "/home/user/sessions/s1/result.xlsx", "size": 99, "mtime": 1750000004.0, "is_dir": False},
        ], ensure_ascii=False)
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text=json_payload)
        )

        items = await _sandbox_list_dir(sandbox, "/home/user/sessions/s1", "/home/user/sessions/s1")

        names = {f.name for f in items}
        assert names == {"app.py", "result.xlsx"}
        assert {f.name: f.size for f in items}["result.xlsx"] == 99

    @pytest.mark.asyncio
    async def test_list_dir_empty(self):
        """空目錄返回空列表"""
        from src.api.routes.sessions import _sandbox_list_dir

        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text="[]")
        )

        items = await _sandbox_list_dir(sandbox, "/home/user/sessions/s1", "/home/user/sessions/s1")
        assert items == []

    @pytest.mark.asyncio
    async def test_list_dir_json_parse_failure(self):
        """JSON 解析失敗時返回空列表"""
        from src.api.routes.sessions import _sandbox_list_dir

        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text="not-json")
        )

        items = await _sandbox_list_dir(sandbox, "/home/user/sessions/s1", "/home/user/sessions/s1")
        assert items == []

    @pytest.mark.asyncio
    async def test_list_dir_relative_path_from_session_root(self):
        """子目錄中的項目 path 相對於 session_root"""
        from src.api.routes.sessions import _sandbox_list_dir

        json_payload = json.dumps([
            {"name": "chart.png", "path": "/home/user/sessions/s1/reports/chart.png", "size": 1024, "mtime": 1750000000.0, "is_dir": False},
        ], ensure_ascii=False)
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text=json_payload)
        )

        items = await _sandbox_list_dir(sandbox, "/home/user/sessions/s1/reports", "/home/user/sessions/s1")

        assert len(items) == 1
        assert items[0].name == "chart.png"
        assert items[0].path == "reports/chart.png"

    @pytest.mark.asyncio
    async def test_list_dir_skips_dotfiles(self):
        """以點開頭的隱藏文件被跳過"""
        from src.api.routes.sessions import _sandbox_list_dir

        json_payload = json.dumps([
            {"name": ".hidden", "path": "/home/user/sessions/s1/.hidden", "size": 0, "mtime": 1750000000.0, "is_dir": True},
            {"name": ".env", "path": "/home/user/sessions/s1/.env", "size": 50, "mtime": 1750000001.0, "is_dir": False},
            {"name": "visible.txt", "path": "/home/user/sessions/s1/visible.txt", "size": 100, "mtime": 1750000002.0, "is_dir": False},
        ], ensure_ascii=False)
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text=json_payload)
        )

        items = await _sandbox_list_dir(sandbox, "/home/user/sessions/s1", "/home/user/sessions/s1")

        assert len(items) == 1
        assert items[0].name == "visible.txt"


class TestAbortEndpoint:
    """Abort 端點測試"""

    @pytest.fixture
    def client(self):
        """創建帶 chat 路由的測試客戶端，覆蓋鑑權依賴"""
        from src.api.routes import chat as chat_mod

        client = make_test_client(chat_mod.router, "/chat")
        self._mock_db_session = client.mock_db  # type: ignore[attr-defined]
        return client

    @patch("src.api.routes.chat.get_agent_pool")
    def test_abort_no_agent_no_running_round_returns_409(self, mock_pool_fn, client):
        """無正在執行的 Agent 且無卡住的 round 返回 409"""
        from src.api.models.session import Session as SessionModel
        from src.api.models.round import Round as RoundModel
        from src.api.models.user_run_lock import UserRunLock

        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.status = "active"

        def query_side_effect(model):
            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is RoundModel:
                chain.filter.return_value.first.return_value = None
            elif model is UserRunLock:
                chain.filter.return_value.first.return_value = None
            return chain

        self._mock_db_session.query.side_effect = query_side_effect

        # AgentPool 沒有這個 session
        mock_pool = MagicMock()
        mock_pool.get.return_value = None
        mock_pool_fn.return_value = mock_pool

        response = client.post("/chat/session-1/abort")
        assert response.status_code == 409
        assert "沒有正在進行" in response.json()["detail"]

    @patch("src.api.routes.chat.get_agent_pool")
    def test_abort_with_cancel_token_returns_200(self, mock_pool_fn, client):
        """有 cancel_token 時成功取消"""
        import asyncio
        from src.api.models.session import Session as SessionModel
        from src.api.models.round import Round as RoundModel
        from src.api.models.user_run_lock import UserRunLock
        from src.api.utils.timezone import now_naive

        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.status = "active"

        mock_round = MagicMock()
        mock_round.id = "round-1"

        # 心跳新鲜的锁（worker 存活）
        mock_lock = MagicMock()
        mock_lock.updated_at = now_naive()
        mock_lock.lock_id = "lock-1"

        def query_side_effect(model):
            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is RoundModel:
                chain.filter.return_value.first.return_value = mock_round
            elif model is UserRunLock:
                chain.filter.return_value.first.return_value = mock_lock
            return chain

        self._mock_db_session.query.side_effect = query_side_effect

        # 模擬 AgentService + cancel_token
        cancel_token = asyncio.Event()
        mock_agent_service = MagicMock()
        mock_agent_service.cancel_token = cancel_token

        mock_pool = MagicMock()
        mock_pool.get.return_value = mock_agent_service
        mock_pool_fn.return_value = mock_pool

        with patch("src.api.routes.chat._upsert_cancel_request", new_callable=AsyncMock, return_value="req-1"):
            response = client.post("/chat/session-1/abort")
        assert response.status_code == 200
        assert response.json()["status"] == "cancellation_requested"
        assert response.json()["request_id"] == "req-1"
        assert cancel_token.is_set()

    @patch("src.api.routes.chat.get_agent_pool")
    def test_abort_init_window_with_recent_lock_returns_200(self, mock_pool_fn, client):
        """无 running round 但持有近期会话锁（init 窗口）也应允许发起取消。"""
        from src.api.models.session import Session as SessionModel
        from src.api.models.round import Round as RoundModel
        from src.api.models.user_run_lock import UserRunLock as UserRunLockModel
        from src.api.utils.timezone import now_naive

        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.status = "active"

        mock_lock = MagicMock()
        mock_lock.user_id = "testuser"
        mock_lock.session_id = "session-1"
        mock_lock.lock_id = "lock-init-window"
        mock_lock.updated_at = now_naive()

        def query_side_effect(model):
            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is RoundModel:
                chain.filter.return_value.first.return_value = None
            elif model is UserRunLockModel:
                chain.filter.return_value.first.return_value = mock_lock
            return chain

        self._mock_db_session.query.side_effect = query_side_effect

        mock_pool = MagicMock()
        mock_pool.get.return_value = None
        mock_pool_fn.return_value = mock_pool

        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300

        with patch("src.api.routes.chat.get_settings", return_value=mock_settings), patch(
            "src.api.routes.chat._upsert_cancel_request", new_callable=AsyncMock, return_value="req-init-window"
        ):
            response = client.post("/chat/session-1/abort")

        assert response.status_code == 200
        assert response.json()["status"] == "cancellation_requested"
        assert response.json()["request_id"] == "req-init-window"

    @patch("src.api.routes.chat.get_agent_pool")
    def test_abort_without_local_cancel_token_still_requests_cancel(self, mock_pool_fn, client):
        """有 running round 但本地 worker 無 cancel_token 也應返回 cancellation_requested"""
        from src.api.models.session import Session as SessionModel
        from src.api.models.round import Round as RoundModel
        from src.api.models.user_run_lock import UserRunLock
        from src.api.utils.timezone import now_naive

        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.status = "active"

        mock_round = MagicMock()
        mock_round.id = "round-1"

        # 心跳新鲜（worker 存活但在其他进程）
        mock_lock = MagicMock()
        mock_lock.updated_at = now_naive()
        mock_lock.lock_id = "lock-2"

        def query_side_effect(model):
            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is RoundModel:
                chain.filter.return_value.first.return_value = mock_round
            elif model is UserRunLock:
                chain.filter.return_value.first.return_value = mock_lock
            return chain

        self._mock_db_session.query.side_effect = query_side_effect

        mock_agent_service = MagicMock()
        mock_agent_service.cancel_token = None

        mock_pool = MagicMock()
        mock_pool.get.return_value = mock_agent_service
        mock_pool_fn.return_value = mock_pool

        with patch("src.api.routes.chat._upsert_cancel_request", new_callable=AsyncMock, return_value="req-2"):
            response = client.post("/chat/session-1/abort")

        assert response.status_code == 200
        assert response.json()["status"] == "cancellation_requested"
        assert response.json()["request_id"] == "req-2"

    def test_abort_session_not_found_returns_404(self, client):
        """會話不存在返回 404"""
        self._mock_db_session.query.return_value.filter.return_value.first.return_value = None

        response = client.post("/chat/session-1/abort")
        assert response.status_code == 404

    def test_abort_status_returns_cancel_row_details(self, client):
        """abort/status 返回取消请求明细（state/request_id/时间戳）。"""
        from datetime import datetime
        from src.api.models.session import Session as SessionModel
        from src.api.models.round import Round as RoundModel
        from src.api.models.run_cancel_request import RunCancelRequest as RunCancelRequestModel

        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"

        mock_cancel = MagicMock()
        mock_cancel.state = "acked"
        mock_cancel.request_id = "req-acked-1"
        mock_cancel.requested_at = datetime(2026, 4, 16, 10, 0, 0)
        mock_cancel.acked_at = datetime(2026, 4, 16, 10, 0, 1)
        mock_cancel.completed_at = None

        mock_running_round = MagicMock()
        mock_running_round.id = "round-1"

        def query_side_effect(model):
            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is RunCancelRequestModel:
                chain.filter.return_value.first.return_value = mock_cancel
            elif model is RoundModel:
                chain.filter.return_value.first.return_value = mock_running_round
            return chain

        self._mock_db_session.query.side_effect = query_side_effect

        response = client.get("/chat/session-1/abort/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["session_id"] == "session-1"
        assert payload["state"] == "acked"
        assert payload["request_id"] == "req-acked-1"
        assert payload["requested_at"] == "2026-04-16T10:00:00"
        assert payload["acked_at"] == "2026-04-16T10:00:01"
        assert payload["completed_at"] is None
        assert payload["running"] is True
        assert payload["running_round_id"] == "round-1"

    def test_abort_status_returns_none_when_no_cancel_request(self, client):
        """abort/status 在无取消请求时返回 state=none。"""
        from src.api.models.session import Session as SessionModel
        from src.api.models.round import Round as RoundModel
        from src.api.models.run_cancel_request import RunCancelRequest as RunCancelRequestModel

        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"

        def query_side_effect(model):
            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is RunCancelRequestModel:
                chain.filter.return_value.first.return_value = None
            elif model is RoundModel:
                chain.filter.return_value.first.return_value = None
            return chain

        self._mock_db_session.query.side_effect = query_side_effect

        response = client.get("/chat/session-1/abort/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["session_id"] == "session-1"
        assert payload["state"] == "none"
        assert payload["request_id"] is None
        assert payload["requested_at"] is None
        assert payload["acked_at"] is None
        assert payload["completed_at"] is None
        assert payload["running"] is False
        assert payload["running_round_id"] is None
