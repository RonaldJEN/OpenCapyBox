"""默认注入文件（模板 Provision）单元测试

覆盖：
- MemoryService.provision_default_files: 新用户默认模板写入
- MemoryService.is_new_user: 判断用户是否为新用户
- MemoryService._strip_frontmatter: YAML frontmatter 去除
- AgentService._provision_default_files_if_needed: Agent 初始化时的默认文件注入
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.helpers import make_query_db as _make_query_db, make_test_client


# =========================================================================
# MemoryService 默认模板测试
# =========================================================================

class TestStripFrontmatter:
    """_strip_frontmatter 测试"""

    def test_strip_yaml_frontmatter(self):
        from src.api.services.memory_service import MemoryService

        text = "---\nsummary: test\nread_when:\n  - always\n---\n\nActual content here."
        result = MemoryService._strip_frontmatter(text)
        assert result == "Actual content here."

    def test_no_frontmatter(self):
        from src.api.services.memory_service import MemoryService

        text = "No frontmatter here.\nJust content."
        result = MemoryService._strip_frontmatter(text)
        assert result == text

    def test_incomplete_frontmatter(self):
        from src.api.services.memory_service import MemoryService

        text = "---\nsummary: test\nno closing marker"
        result = MemoryService._strip_frontmatter(text)
        assert result == text

    def test_empty_content_after_frontmatter(self):
        from src.api.services.memory_service import MemoryService

        text = "---\nsummary: test\n---\n"
        result = MemoryService._strip_frontmatter(text)
        assert result.strip() == ""


class TestIsNewUser:
    """is_new_user 测试"""

    def test_new_user_returns_true(self):
        from src.api.services.memory_service import MemoryService

        db = _make_query_db(count=0)
        svc = MemoryService(db)
        assert svc.is_new_user("user-123") is True

    def test_existing_user_returns_false(self):
        from src.api.services.memory_service import MemoryService

        db = _make_query_db(count=3)
        svc = MemoryService(db)
        assert svc.is_new_user("user-123") is False


class TestProvisionDefaultFiles:
    """provision_default_files 测试"""

    def test_skip_for_existing_user(self):
        from src.api.services.memory_service import MemoryService

        db = _make_query_db(count=5)
        svc = MemoryService(db)
        result = svc.provision_default_files("existing-user")
        assert result == 0
        db.add.assert_not_called()

    def test_provision_for_new_user(self):
        from src.api.services.memory_service import MemoryService, _TEMPLATE_DIR, _TEMPLATE_FILES

        db = _make_query_db(count=0)
        svc = MemoryService(db)

        # 确保至少有一个模板文件存在
        existing_templates = [
            ft for ft, fn in _TEMPLATE_FILES.items()
            if (_TEMPLATE_DIR / fn).exists()
        ]

        result = svc.provision_default_files("new-user")
        assert result == len(existing_templates)
        assert result > 0  # 至少有一个模板

    def test_template_files_exist(self):
        """验证所有配置的模板文件都存在于 docs/ 目录"""
        from src.api.services.memory_service import _TEMPLATE_DIR, _TEMPLATE_FILES

        for file_type, filename in _TEMPLATE_FILES.items():
            path = _TEMPLATE_DIR / filename
            assert path.exists(), f"模板文件缺失: {path} (file_type={file_type})"

    def test_frontmatter_stripped_in_provision(self):
        """验证写入的内容不包含 YAML frontmatter"""
        from src.api.services.memory_service import MemoryService, _TEMPLATE_DIR, _TEMPLATE_FILES

        db = _make_query_db(count=0)

        svc = MemoryService(db)
        svc.provision_default_files("new-user")

        # 检查 db.add 调用的参数，确认内容不以 --- 开头
        for call in db.add.call_args_list:
            record = call[0][0]
            if hasattr(record, "content"):
                assert not record.content.strip().startswith("---"), \
                    f"内容应去除 frontmatter: {record.content[:50]}"


class TestSandboxTemplateMethodRemoved:
    """确认旧的沙箱独有模板方法已移除"""

    def test_provision_sandbox_templates_removed(self):
        from src.api.services.memory_service import MemoryService

        assert not hasattr(MemoryService, "provision_sandbox_templates")


# =========================================================================
# AgentService._provision_default_files_if_needed 集成测试
# =========================================================================

class TestAgentServiceProvision:
    """AgentService 默认文件注入集成测试"""

    def test_provision_called_on_init(self):
        """验证 _provision_default_files_if_needed 被正确调用"""
        from src.api.services.agent_service import AgentService

        svc = AgentService.__new__(AgentService)
        svc.user_id = "test-user"
        svc.history_service = MagicMock()

        with patch("src.api.services.memory_service.MemoryService") as MockMemSvc:
            mock_instance = MockMemSvc.return_value
            mock_instance.provision_default_files.return_value = 3

            svc._provision_default_files_if_needed()

            mock_instance.provision_default_files.assert_called_once_with("test-user")
            svc.history_service.reset_session.assert_called_once()

    def test_provision_failure_non_fatal(self):
        """验证 provision 失败不会中断 Agent 初始化"""
        from src.api.services.agent_service import AgentService

        svc = AgentService.__new__(AgentService)
        svc.user_id = "test-user"
        svc.history_service = MagicMock()

        with patch("src.api.services.memory_service.MemoryService") as MockMemSvc:
            mock_instance = MockMemSvc.return_value
            mock_instance.provision_default_files.side_effect = RuntimeError("DB error")

            # 不应抛异常
            svc._provision_default_files_if_needed()
            svc.history_service.reset_session.assert_called_once()


# =========================================================================
# Config 路由自动 Provision 测试
# =========================================================================

class TestConfigRouteAutoProvision:
    """GET/PUT /agent-files/{name} 端点新用户自动注入默认模板测试"""

    @pytest.fixture
    def client(self):
        from src.api.routes import config as config_routes
        return make_test_client(config_routes.router, "/config", user="new-user")

    def test_get_single_agent_file_triggers_provision(self, client):
        """GET /agent-files/{name} 对新用户也触发 provision"""
        with patch("src.api.routes.config.MemoryService") as MockMemSvc:
            mock_instance = MockMemSvc.return_value
            mock_instance.provision_default_files.return_value = 5
            record = MagicMock()
            record.content = "soul content"
            record.version = 1
            mock_instance.get_memory_file.return_value = record

            response = client.get("/config/agent-files/soul", params={"user_id": "new-user"})

        assert response.status_code == 200
        mock_instance.provision_default_files.assert_called_once_with("new-user")
        data = response.json()
        assert data["content"] == "soul content"

    def test_get_agents_file_not_exposed(self, client):
        """AGENTS.md 不作为用户可配置文件暴露"""
        with patch("src.api.routes.config.MemoryService") as MockMemSvc:
            mock_instance = MockMemSvc.return_value
            mock_instance.provision_default_files.return_value = 3

            response = client.get("/config/agent-files/agents", params={"user_id": "new-user"})

        assert response.status_code == 400
        MockMemSvc.assert_not_called()

    def test_update_agent_file_invalidates_agent_cache(self, client):
        """PUT /agent-files/{name} 更新后应失效该用户 Agent 缓存"""
        with (
            patch("src.api.routes.config.MemoryService") as MockMemSvc,
            patch("src.api.services.agent_pool_service.get_agent_pool") as mock_get_pool,
        ):
            mock_instance = MockMemSvc.return_value
            record = MagicMock()
            record.version = 2
            mock_instance.upsert_memory_file.return_value = record

            mock_pool = MagicMock()
            mock_pool.invalidate_user_async = AsyncMock(return_value=1)
            mock_get_pool.return_value = mock_pool

            response = client.put(
                "/config/agent-files/soul",
                params={"user_id": "new-user"},
                json={"content": "new soul"},
            )

        assert response.status_code == 200
        mock_pool.invalidate_user_async.assert_awaited_once_with("new-user")

    def test_update_agent_file_syncs_persisted_sandbox_when_cache_exists(self, client):
        """配置同步时应使用 DB 当前沙箱，避免写入旧缓存沙箱。"""
        user_sandbox = MagicMock()
        user_sandbox.sandbox_id = "sbx-new"
        client.mock_db.query.return_value.filter.return_value.first.return_value = user_sandbox

        with (
            patch("src.api.routes.config.MemoryService") as MockMemSvc,
            patch("src.api.services.agent_pool_service.get_agent_pool") as mock_get_pool,
            patch("src.api.services.sandbox_service.get_sandbox_service") as mock_get_sandbox_service,
        ):
            mock_instance = MockMemSvc.return_value
            record = MagicMock()
            record.version = 2
            mock_instance.upsert_memory_file.return_value = record
            mock_instance.sync_to_sandbox = AsyncMock()

            mock_pool = MagicMock()
            mock_pool.invalidate_user_async = AsyncMock(return_value=0)
            mock_get_pool.return_value = mock_pool

            stale_sandbox = MagicMock()
            stale_sandbox.id = "sbx-old"
            fresh_sandbox = MagicMock()
            fresh_sandbox.id = "sbx-new"
            sandbox_service = MagicMock()
            sandbox_service.get_cached.return_value = stale_sandbox
            sandbox_service.get_or_resume = AsyncMock(return_value=fresh_sandbox)
            mock_get_sandbox_service.return_value = sandbox_service

            response = client.put(
                "/config/agent-files/soul",
                params={"user_id": "new-user"},
                json={"content": "new soul"},
            )

        assert response.status_code == 200
        sandbox_service.get_or_resume.assert_awaited_once_with("new-user", "sbx-new")
        mock_instance.sync_to_sandbox.assert_awaited_once_with(
            "new-user",
            fresh_sandbox,
            force=True,
            file_types={"soul_md"},
            include_agents_template=False,
        )

    def test_update_agents_file_not_exposed(self, client):
        """AGENTS.md 不作为用户可配置文件暴露"""
        response = client.put(
            "/config/agent-files/agents",
            params={"user_id": "new-user"},
            json={"content": "new rules"},
        )

        assert response.status_code == 400
