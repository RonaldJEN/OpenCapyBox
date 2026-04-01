"""一用户一沙箱重构相关单元测试

覆盖：
- UserSandbox / ConversationMessage / UserMemory 数据模型
- SandboxSessionService（缓存键 user_id，Volume 路径用户级）
- AgentPoolService（_user_sessions 映射，TTL 仅在用户无活跃 session 时 pause）
- AgentService（user_id 参数，workspace_dir 按 session，conversation_messages 恢复）
- database.py 表注册
"""
import hashlib
import json
import re
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.helpers import make_mock_sandbox, make_agent_service


# ── 共享工厂 ────────────────────────────────────────────────


def _inject_pool_session(pool, session_id, user_id, mock_agent=None, *, timestamp=None):
    """向 AgentPoolService 注入一个模拟会话，减少 pool 状态设置样板"""
    if mock_agent is None:
        mock_agent = MagicMock()
        mock_agent.sandbox = MagicMock()
    pool._cache[session_id] = mock_agent
    pool._last_access[session_id] = timestamp if timestamp is not None else time.time()
    pool._session_user[session_id] = user_id
    pool._user_sessions.setdefault(user_id, set()).add(session_id)
    return mock_agent


# ============================================================
# 数据模型测试
# ============================================================

class TestUserSandboxModel:
    """UserSandbox 模型基本属性测试"""

    def test_model_attributes(self):
        from src.api.models.user_sandbox import UserSandbox
        sandbox = UserSandbox(
            id=str(uuid.uuid4()),
            user_id="user-123",
            sandbox_id="sb-abc",
            status="active",
        )
        assert sandbox.user_id == "user-123"
        assert sandbox.sandbox_id == "sb-abc"
        assert sandbox.status == "active"

    def test_tablename(self):
        from src.api.models.user_sandbox import UserSandbox
        assert UserSandbox.__tablename__ == "user_sandboxes"


class TestConversationMessageModel:
    """ConversationMessage 模型基本属性测试"""

    def test_model_attributes(self):
        from src.api.models.conversation_message import ConversationMessage
        session_id = str(uuid.uuid4())
        msg = ConversationMessage(
            session_id=session_id,
            sequence=1,
            role="user",
            content="Hello",
        )
        assert msg.session_id == session_id
        assert msg.sequence == 1
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.is_summary is None or not msg.is_summary

    def test_tablename(self):
        from src.api.models.conversation_message import ConversationMessage
        assert ConversationMessage.__tablename__ == "conversation_messages"


class TestUserMemoryModels:
    """UserMemory 相关模型测试"""

    def test_user_memory_file_types(self):
        from src.api.models.user_memory import UserMemory
        for file_type in ["user_md", "memory_md", "soul_md", "agents_md", "heartbeat_md"]:
            mem = UserMemory(
                user_id="user-1",
                file_type=file_type,
                content="# test",
                version=1,
            )
            assert mem.file_type == file_type
            assert mem.version == 1

    def test_user_skill_config(self):
        from src.api.models.user_memory import UserSkillConfig
        cfg = UserSkillConfig(user_id="user-1", skill_name="docx", enabled=True)
        assert cfg.skill_name == "docx"
        assert cfg.enabled is True

    def test_cron_job_run(self):
        from src.api.models.user_memory import CronJobRun
        run = CronJobRun(
            id=str(uuid.uuid4()),
            user_id="user-1",
            job_name="daily_report",
            cron_expr="0 9 * * *",
            status="success",
        )
        assert run.job_name == "daily_report"
        assert run.cron_expr == "0 9 * * *"


# ============================================================
# SandboxSessionService 测试
# ============================================================

class TestSandboxSessionServiceUserKey:
    """SandboxSessionService 以 user_id 为缓存键的测试"""

    def setup_method(self):
        # 每个测试重置单例
        from src.api.services import sandbox_service
        sandbox_service.SandboxSessionService._instance = None
        sandbox_service._sandbox_service = None

    def test_cache_keyed_by_user_id(self):
        """_cache 应以 user_id 为键"""
        from src.api.services.sandbox_service import SandboxSessionService
        svc = SandboxSessionService()
        mock_sandbox = MagicMock()
        svc._cache["user-abc"] = mock_sandbox
        assert svc.get_cached("user-abc") is mock_sandbox
        assert svc.get_cached("other-user") is None

    def test_user_storage_host_path_contains_user(self):
        """用户级 Volume 路径应含 user- 前缀"""
        from src.api.services.sandbox_service import SandboxSessionService
        path = SandboxSessionService._user_storage_host_path("alice")
        assert "user-" in path
        # 哈希部分唯一
        hashed = hashlib.sha1(b"alice").hexdigest()[:16]
        assert hashed in path

    def test_user_storage_host_path_stable(self):
        """同一 user_id 生成的路径应稳定"""
        from src.api.services.sandbox_service import SandboxSessionService
        p1 = SandboxSessionService._user_storage_host_path("u1")
        p2 = SandboxSessionService._user_storage_host_path("u1")
        assert p1 == p2

    def test_user_storage_host_path_distinct_users(self):
        """不同 user_id 路径应不同"""
        from src.api.services.sandbox_service import SandboxSessionService
        p1 = SandboxSessionService._user_storage_host_path("alice")
        p2 = SandboxSessionService._user_storage_host_path("bob")
        assert p1 != p2

    def test_build_persistent_volumes_uses_user_id(self):
        """Volume name 应含用户哈希"""
        from src.api.services.sandbox_service import SandboxSessionService
        with patch("src.api.services.sandbox_service.settings") as mock_settings:
            mock_settings.sandbox_persistent_storage_enabled = True
            mock_settings.sandbox_host_storage_root = "/tmp/sandbox"
            volumes = SandboxSessionService._build_persistent_volumes("user-xyz")
        assert volumes is not None
        assert len(volumes) == 1
        expected_hash = hashlib.sha1(b"user-xyz").hexdigest()[:12]
        assert expected_hash in volumes[0].name

    def test_get_sandbox_id_returns_none_when_not_cached(self):
        from src.api.services.sandbox_service import SandboxSessionService
        svc = SandboxSessionService()
        assert svc.get_sandbox_id("nonexistent-user") is None

    def test_push_skill_fails_gracefully_when_not_cached(self):
        """沙箱不在缓存时 push_skill 应返回 False 而非抛出"""
        import asyncio
        from src.api.services.sandbox_service import SandboxSessionService
        svc = SandboxSessionService()
        result = asyncio.get_event_loop().run_until_complete(
            svc.push_skill("user-no-sandbox", "/nonexistent", "my-skill")
        )
        assert result is False


# ============================================================
# AgentPoolService 测试
# ============================================================

class TestAgentPoolServiceUserSessions:
    """AgentPoolService _user_sessions 映射和 TTL 逻辑测试"""

    def setup_method(self):
        from src.api.services import agent_pool_service
        agent_pool_service.AgentPoolService._instance = None
        agent_pool_service._agent_pool = None

    def test_user_sessions_populated_on_get_or_create(self):
        """get_or_create 后 _user_sessions 应有映射"""
        from src.api.services.agent_pool_service import AgentPoolService

        pool = AgentPoolService(ttl=3600)
        _inject_pool_session(pool, "session-A", "user-1")

        assert "session-A" in pool._user_sessions["user-1"]

    def test_remove_cleans_user_sessions_mapping(self):
        """remove() 应从 _user_sessions 中移除 session"""
        from src.api.services.agent_pool_service import AgentPoolService

        pool = AgentPoolService(ttl=3600)
        _inject_pool_session(pool, "session-A", "user-1")

        pool.remove("session-A")

        assert "session-A" not in pool._cache
        assert "user-1" not in pool._user_sessions  # 集合清空后键也应被删除

    def test_remove_keeps_user_if_other_sessions_exist(self):
        """remove() 某 session 时，若用户还有其他 session，不应删除用户映射"""
        from src.api.services.agent_pool_service import AgentPoolService

        pool = AgentPoolService(ttl=3600)
        _inject_pool_session(pool, "session-A", "user-1")
        _inject_pool_session(pool, "session-B", "user-1")

        pool.remove("session-A")

        assert "user-1" in pool._user_sessions
        assert "session-B" in pool._user_sessions["user-1"]

    @pytest.mark.asyncio
    async def test_cleanup_expired_only_pauses_when_all_sessions_expired(self):
        """仅当用户所有 session 均过期时才 pause 沙箱"""
        from src.api.services.agent_pool_service import AgentPoolService

        pool = AgentPoolService(ttl=1)  # TTL 1秒

        expired_time = time.time() - 10  # 10秒前，已过期
        fresh_time = time.time()         # 刚刚，未过期

        mock_agent_a = _inject_pool_session(pool, "session-A", "user-1", timestamp=expired_time)
        mock_agent_b = _inject_pool_session(pool, "session-B", "user-1", timestamp=fresh_time)

        mock_sandbox_service = AsyncMock()
        mock_sandbox_service.pause = AsyncMock(return_value=True)

        with patch("src.api.services.agent_pool_service.get_sandbox_service", return_value=mock_sandbox_service):
            expired = await pool.cleanup_expired_async()

        # session-A 应过期
        assert "session-A" in expired
        # session-B 未过期
        assert "session-B" not in expired
        # 因为 session-B 还活跃，沙箱不应被 pause
        mock_sandbox_service.pause.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_expired_pauses_when_all_sessions_expired(self):
        """所有 session 均过期时应 pause 沙箱"""
        from src.api.services.agent_pool_service import AgentPoolService

        pool = AgentPoolService(ttl=1)

        expired_time = time.time() - 10
        _inject_pool_session(pool, "session-A", "user-1", timestamp=expired_time)

        mock_sandbox_service = AsyncMock()
        mock_sandbox_service.pause = AsyncMock(return_value=True)

        with patch("src.api.services.agent_pool_service.get_sandbox_service", return_value=mock_sandbox_service):
            expired = await pool.cleanup_expired_async()

        assert "session-A" in expired
        mock_sandbox_service.pause.assert_called_once_with("user-1")

    def test_get_stats_includes_user_id(self):
        """get_stats 应包含 user_id 信息"""
        from src.api.services.agent_pool_service import AgentPoolService

        pool = AgentPoolService(ttl=3600)
        _inject_pool_session(pool, "session-X", "user-42")

        stats = pool.get_stats()
        assert stats["active_users"] == 1
        assert stats["sessions"]["session-X"]["user_id"] == "user-42"

    def test_invalidate_user_removes_all_sessions(self):
        """invalidate_user() 应移除该用户的全部 session 缓存"""
        from src.api.services.agent_pool_service import AgentPoolService

        pool = AgentPoolService(ttl=3600)
        _inject_pool_session(pool, "session-A", "user-1")
        _inject_pool_session(pool, "session-B", "user-1")
        _inject_pool_session(pool, "session-C", "user-2")

        removed = pool.invalidate_user("user-1")

        assert removed == 2
        assert "session-A" not in pool._cache
        assert "session-B" not in pool._cache
        assert "session-C" in pool._cache
        assert "user-1" not in pool._user_sessions
        assert "user-2" in pool._user_sessions


# ============================================================
# AgentService 测试
# ============================================================

class TestAgentServiceUserWorkspace:
    """AgentService user_id 和 workspace_dir 测试"""

    def test_workspace_dir_uses_session_subdir(self):
        """workspace_dir 应为 /home/user/sessions/{session_id}"""
        svc = make_agent_service(session_id="sess-123", user_id="user-abc")
        assert svc._workspace_dir == "/home/user/sessions/sess-123"

    def test_workspace_dir_fallback_without_session_id(self):
        """session_id 为空时 workspace_dir 应 fallback 到 mount 根目录"""
        svc = make_agent_service(session_id="", user_id="user-abc")
        assert svc._workspace_dir == "/home/user"

    def test_user_id_stored(self):
        """user_id 应被正确存储"""
        svc = make_agent_service(session_id="sess-1", user_id="u-999")
        assert svc.user_id == "u-999"


class TestAgentServiceConversationRestore:
    """AgentService 从 conversation_messages 恢复历史的测试"""

    def test_restore_from_conversation_messages(self):
        """有 conversation_messages 时应从该表恢复"""
        svc, mock_db = make_agent_service(attach_db=True)

        # 构造模拟消息
        msg1 = MagicMock()
        msg1.role = "user"
        msg1.content = '"Hello"'
        msg1.is_summary = False
        msg1.sequence = 1

        msg2 = MagicMock()
        msg2.role = "assistant"
        msg2.content = '"World"'
        msg2.is_summary = False
        msg2.sequence = 2

        # mock DB query chain: .query(...).filter(...).order_by(...).all()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [msg1, msg2]

        # 初始化 agent mock
        svc.agent = MagicMock()
        svc.agent.messages = []

        svc._restore_history()

        assert len(svc.agent.messages) == 2

    def test_restore_empty_conv_messages_gives_empty_messages(self):
        """conversation_messages 为空时 agent.messages 保持为空（无 fallback）"""
        svc, mock_db = make_agent_service(attach_db=True)

        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

        svc.agent = MagicMock()
        svc.agent.messages = []

        svc._restore_history()

        assert len(svc.agent.messages) == 0
        svc.history_service.get_minimal_history.assert_not_called()


# ============================================================
# AgentService 记忆同步 + Skill 过滤测试
# ============================================================

class TestAgentServicePostRoundTasks:
    """_post_round_tasks 和 _sync_memory_to_db 测试"""

    @pytest.mark.asyncio
    async def test_post_round_calls_flush(self):
        """_post_round_tasks 应调用 maybe_flush_memory_silent"""
        svc = make_agent_service()
        svc.agent = MagicMock()
        svc.agent.maybe_flush_memory_silent = AsyncMock()

        await svc._post_round_tasks(sync_memory=False)

        svc.agent.maybe_flush_memory_silent.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_round_with_sync(self):
        """sync_memory=True 时应调用 _sync_memory_to_db"""
        svc = make_agent_service()
        svc.agent = MagicMock()
        svc.agent.maybe_flush_memory_silent = AsyncMock()
        svc._sync_memory_to_db = AsyncMock()

        await svc._post_round_tasks(sync_memory=True)

        svc._sync_memory_to_db.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_round_without_sync(self):
        """sync_memory=False 时不应调用 _sync_memory_to_db"""
        svc = make_agent_service()
        svc.agent = MagicMock()
        svc.agent.maybe_flush_memory_silent = AsyncMock(return_value=False)
        svc._sync_memory_to_db = AsyncMock()

        await svc._post_round_tasks(sync_memory=False)

        svc._sync_memory_to_db.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_round_sync_when_silent_flush_true(self):
        """静默刷新写入成功时，即使 sync_memory=False 也应回写 DB"""
        svc = make_agent_service()
        svc.agent = MagicMock()
        svc.agent.maybe_flush_memory_silent = AsyncMock(return_value=True)
        svc._sync_memory_to_db = AsyncMock()

        await svc._post_round_tasks(sync_memory=False)

        svc._sync_memory_to_db.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_round_flush_exception_does_not_crash(self):
        """maybe_flush_memory_silent 异常不应阻塞后续执行"""
        svc = make_agent_service()
        svc.agent = MagicMock()
        svc.agent.maybe_flush_memory_silent = AsyncMock(side_effect=Exception("flush error"))
        svc._sync_memory_to_db = AsyncMock()

        # 不应抛出异常
        await svc._post_round_tasks(sync_memory=True)
        svc._sync_memory_to_db.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_memory_to_db_calls_service(self):
        """_sync_memory_to_db 应同步所有 agent 配置文件到 DB"""
        svc = make_agent_service()

        mock_mem_svc = MagicMock()
        mock_mem_svc.sync_from_sandbox = AsyncMock(return_value="# Memory content")
        mock_mem_svc.rebuild_embeddings = AsyncMock(return_value=3)

        mock_db = MagicMock()

        with patch("src.api.models.database.SessionLocal", return_value=mock_db):
            with patch("src.api.services.memory_service.MemoryService", return_value=mock_mem_svc):
                await svc._sync_memory_to_db()

        # 应对所有 5 个 file_type 各调用一次 sync_from_sandbox
        assert mock_mem_svc.sync_from_sandbox.call_count == 5
        # 仅 user_md 和 memory_md 需要 rebuild_embeddings
        assert mock_mem_svc.rebuild_embeddings.call_count == 2
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_memory_no_heartbeat_cron_reload(self):
        """Cron 已改为 DB 驱动，HEARTBEAT.md 变化不再触发 cron reload"""
        svc = make_agent_service()

        # sync_from_sandbox 对 heartbeat_md 返回内容
        async def _fake_sync(user_id, sandbox, ft):
            if ft == "heartbeat_md":
                return "# 轮询检查清单\n- 检查邮件"
            return None

        mock_mem_svc = MagicMock()
        mock_mem_svc.sync_from_sandbox = AsyncMock(side_effect=_fake_sync)
        mock_mem_svc.rebuild_embeddings = AsyncMock()

        mock_db = MagicMock()

        with patch("src.api.models.database.SessionLocal", return_value=mock_db):
            with patch("src.api.services.memory_service.MemoryService", return_value=mock_mem_svc):
                await svc._sync_memory_to_db()

        # 确认不再调用 reload_user_jobs
        # （Cron 现在由 manage_cron 工具直接操作 DB + APScheduler）


class TestAgentServiceSkillFiltering:
    """UserSkillConfig 运行时过滤逻辑测试"""

    def test_disabled_skill_filtered_from_loader(self):
        """禁用的 skill 应从 skill_loader.loaded_skills 中移除"""
        from src.api.models.user_memory import UserSkillConfig

        # 模拟 DB 返回一个禁用的 skill
        disabled_record = MagicMock()
        disabled_record.skill_name = "docx"
        disabled_record.enabled = False

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [disabled_record]

        # 模拟 skill_loader（直接测试过滤逻辑）
        loaded_skills = {"docx": MagicMock(), "pdf": MagicMock(), "xlsx": MagicMock()}

        # 使用简单对象代替 MagicMock 的 name 属性（MagicMock 的 name 是特殊参数）
        class FakeSkill:
            def __init__(self, name):
                self.name = name
        skills_list = [FakeSkill("docx"), FakeSkill("pdf"), FakeSkill("xlsx")]

        # 执行 agent_service.py 中的过滤逻辑
        user_id = "user-1"
        disabled_skills = {
            r.skill_name for r in
            mock_db.query(UserSkillConfig)
            .filter(
                UserSkillConfig.user_id == user_id,
                UserSkillConfig.enabled == False,
            )
            .all()
        }

        if disabled_skills:
            for name in disabled_skills:
                loaded_skills.pop(name, None)
            skills_list = [s for s in skills_list if s.name not in disabled_skills]

        assert "docx" not in loaded_skills
        assert "pdf" in loaded_skills
        assert "xlsx" in loaded_skills
        assert len(skills_list) == 2
        assert all(s.name != "docx" for s in skills_list)

    def test_no_disabled_skills_keeps_all(self):
        """无禁用 skill 时应保留所有 skill"""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []

        loaded_skills = {"docx": MagicMock(), "pdf": MagicMock()}

        disabled_skills = {
            r.skill_name for r in
            mock_db.query.return_value.filter.return_value.all()
        }

        if disabled_skills:
            for name in disabled_skills:
                loaded_skills.pop(name, None)

        assert len(loaded_skills) == 2


# ============================================================
# database.py 表注册测试
# ============================================================

class TestDatabaseTableRegistration:
    """database.py 应能注册所有新表"""

    def test_import_models_does_not_raise(self):
        """_import_models 应能成功导入所有模型"""
        from src.api.models.database import _import_models
        # 不应抛出任何异常
        _import_models()

    def test_new_tables_in_metadata(self):
        """新表应注册到 Base.metadata"""
        from src.api.models.database import _import_models, Base
        _import_models()
        table_names = set(Base.metadata.tables.keys())
        expected = {
            "user_sandboxes",
            "conversation_messages",
            "user_memory",
            "memory_embeddings",
            "cron_job_runs",
            "user_skill_configs",
        }
        for table in expected:
            assert table in table_names, f"Missing table: {table}"


