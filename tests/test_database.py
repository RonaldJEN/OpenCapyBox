"""数据库配置测试 — 验证 database.py 正确读取 Settings.database_url"""
import pytest
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestDatabaseConfig:
    """测试数据库配置从 Settings 读取"""

    def test_database_url_from_settings(self):
        """验证 DATABASE_URL 来自 get_settings().database_url"""
        from src.api.models.database import DATABASE_URL
        from src.api.config import get_settings

        settings = get_settings()
        assert DATABASE_URL == settings.database_url

    def test_database_url_default_value(self):
        """验证默认 DATABASE_URL 来自 settings"""
        from src.api.models.database import DATABASE_URL
        from src.api.config import get_settings

        settings = get_settings()
        assert DATABASE_URL == settings.database_url

    def test_engine_created_with_settings_url(self):
        """验证 engine 使用了 Settings 中的 URL"""
        from src.api.models.database import engine, DATABASE_URL

        # 只验证 engine url 中包含数据库名
        if "sqlite" in DATABASE_URL:
            assert "open_capy_box" in str(engine.url)
        else:
            # PostgreSQL: 验证 engine url 包含数据库名
            assert engine.url.database is not None

    def test_database_pool_settings_are_configurable(self):
        """连接池参数应从 Settings 暴露，便于生产环境按并发量调优。"""
        from src.api.config import get_settings

        settings = get_settings()
        assert settings.database_pool_size > 0
        assert settings.database_max_overflow >= 0
        assert settings.database_pool_timeout_seconds > 0
        assert settings.database_pool_recycle_seconds > 0

    def test_engine_pool_diagnostics_includes_live_status(self):
        """诊断信息应包含连接池配置与实时状态。"""
        from src.api.models.database import get_engine_pool_diagnostics

        diagnostics = get_engine_pool_diagnostics()
        assert "status" in diagnostics
        assert diagnostics["configured"]["pool_size"] > 0
        assert diagnostics["configured"]["pool_timeout_seconds"] > 0

    def test_session_local_bound_to_engine(self):
        """验证 SessionLocal 绑定到正确的 engine"""
        from src.api.models.database import SessionLocal, engine

        session = SessionLocal()
        try:
            assert session.bind is engine
        finally:
            session.close()

    def test_get_db_yields_session(self):
        """验证 get_db() 生成器返回数据库会话"""
        from src.api.models.database import get_db

        gen = get_db()
        session = next(gen)
        assert session is not None

        # 清理
        try:
            next(gen)
        except StopIteration:
            pass

    def test_get_db_close_operational_error_is_logged(self, caplog):
        """请求清理阶段连接已断开时记录日志，不再污染已完成响应。"""
        import logging
        from sqlalchemy.exc import OperationalError
        from src.api.models import database as database_module

        mock_session = MagicMock()
        mock_session.close.side_effect = OperationalError(
            "ROLLBACK",
            {},
            Exception("server closed the connection unexpectedly"),
        )

        with patch.object(database_module, "SessionLocal", return_value=mock_session):
            gen = database_module.get_db()
            assert next(gen) is mock_session

            with caplog.at_level(logging.WARNING):
                with pytest.raises(StopIteration):
                    next(gen)

        mock_session.close.assert_called_once()
        assert "关闭数据库会话时连接已断开" in caplog.text

    def test_db_directory_created(self):
        """验证数据库目录被自动创建"""
        from src.api.config import get_settings

        settings = get_settings()
        if settings.database_url.startswith("sqlite"):
            db_path = settings.database_url.split("///", 1)[-1]
            db_dir = Path(db_path).parent
            assert db_dir.exists(), f"数据库目录应该存在: {db_dir}"

    def test_init_db_creates_tables(self):
        """验证 init_db() 创建表"""
        from src.api.models.database import init_db, engine
        from sqlalchemy import inspect

        init_db()
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        # 至少应该有 sessions 表
        assert len(tables) > 0
        assert "llm_call_records" in tables
        assert "subagent_runs" in tables

        llm_columns = {col["name"] for col in inspector.get_columns("llm_call_records")}
        assert "request_message_count" in llm_columns
        assert "manual_review_status" in llm_columns
        assert "first_token_latency_s" in llm_columns
        assert "completion_latency_s" in llm_columns
        assert "compaction_triggered" in llm_columns
        assert "compaction_pre_tokens" in llm_columns
        assert "compaction_post_tokens" in llm_columns
        assert "compaction_tokens_saved" in llm_columns
        assert "compaction_microcompact_compacted_messages" in llm_columns
        assert "compaction_summary_generated_count" in llm_columns
        assert "compaction_summary_reused_count" in llm_columns
        assert "compaction_summary_quality_repair_count" in llm_columns
        assert "compaction_emergency_truncate_dropped_rounds" in llm_columns


class TestDatabaseMigration:
    """测试数据库迁移逻辑"""

    def test_mcp_tool_visibility_migration_includes_revision(self):
        """存量工具发布策略表应补齐乐观并发控制版本列。"""
        from src.api.models.database import _PENDING_COLUMNS

        pending = {
            (table_name, column_name, column_type)
            for table_name, column_name, column_type in _PENDING_COLUMNS
        }
        assert (
            "mcp_tool_visibility",
            "revision",
            "INTEGER NOT NULL DEFAULT 1",
        ) in pending

    def test_mcp_approval_binding_migrations_include_connection_fingerprints(self):
        from src.api.models.database import _PENDING_COLUMNS

        pending = {
            (table_name, column_name, column_type)
            for table_name, column_name, column_type in _PENDING_COLUMNS
        }
        assert (
            "tool_approval_requests",
            "connection_fingerprint",
            "VARCHAR(64)",
        ) in pending
        assert (
            "mcp_tool_snapshots",
            "connection_fingerprint",
            "VARCHAR(64)",
        ) in pending
        assert (
            "tool_approval_requests",
            "execution_claim_token",
            "VARCHAR(64)",
        ) in pending
        assert (
            "tool_approval_requests",
            "execution_lease_expires_at",
            "TIMESTAMP",
        ) in pending
        assert ("mcp_servers", "last_tools_count", "INTEGER") in pending

    def test_sandbox_profile_migration_includes_current_schema_columns(self):
        """存量 sandbox_profiles 表应补齐当前模型依赖的新列。"""
        from src.api.models.database import _PENDING_COLUMNS

        pending = {(table_name, column_name) for table_name, column_name, _ in _PENDING_COLUMNS}
        required_columns = {
            "description",
            "department",
            "domain",
            "protocol",
            "api_key",
            "use_server_proxy",
            "is_default",
            "enabled",
            "version",
            "created_at",
            "updated_at",
        }

        missing = {
            column_name
            for column_name in required_columns
            if ("sandbox_profiles", column_name) not in pending
        }
        assert missing == set()

    def test_sandbox_profile_backfill_respects_use_server_proxy_setting(self):
        """存量 Profile 回填应继承环境连接模式，而不是固定写 TRUE。"""
        from src.api.models import database as database_module

        fake_settings = MagicMock()
        fake_settings.sandbox_use_server_proxy = False
        fake_conn = MagicMock()

        with patch.object(database_module, "_settings", fake_settings):
            database_module._backfill_sandbox_profiles(fake_conn, {"use_server_proxy"})

        fake_conn.execute.assert_called_once()
        sql, params = fake_conn.execute.call_args.args
        assert "use_server_proxy = :use_server_proxy" in str(sql)
        assert params == {"use_server_proxy": False}

    def test_migrate_add_columns_is_idempotent(self):
        """验证 _migrate_add_columns 可以重复调用（幂等）"""
        from src.api.models.database import init_db

        # 调用两次不应报错
        init_db()
        init_db()

    def test_default_sandbox_profile_backfills_legacy_user_sandboxes(self):
        """存量 user_sandboxes 应回填默认 Profile 指纹，避免升级后误判 stale。"""
        from src.api.models.database import SessionLocal, init_db, _ensure_default_sandbox_profile
        from src.api.models.sandbox_profile import SandboxProfile
        from src.api.models.user_sandbox import UserSandbox

        init_db()
        user_id = "legacy-profile-backfill-user"
        db = SessionLocal()
        try:
            db.query(UserSandbox).filter(UserSandbox.user_id == user_id).delete()
            db.add(UserSandbox(
                id=str(uuid.uuid4()),
                user_id=user_id,
                sandbox_id="sbx-legacy-profile",
                status="active",
            ))
            db.commit()
        finally:
            db.close()

        _ensure_default_sandbox_profile()

        db = SessionLocal()
        try:
            default_profile = (
                db.query(SandboxProfile)
                .filter(SandboxProfile.is_default.is_(True))
                .order_by(SandboxProfile.created_at.asc())
                .first()
            )
            user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).one()
            assert default_profile is not None
            assert user_sandbox.active_profile_id == default_profile.id
            assert user_sandbox.active_profile_version == int(default_profile.version or 1)
        finally:
            db.query(UserSandbox).filter(UserSandbox.user_id == user_id).delete()
            db.commit()
            db.close()

    def test_ensure_default_sandbox_profile_repairs_multiple_defaults(self):
        """默认 Profile bootstrap 应修复异常多默认状态。"""
        from src.api.models.database import SessionLocal, init_db
        from src.api.models.sandbox_profile import SandboxProfile
        from src.api.services.sandbox_profile_service import ensure_default_sandbox_profile

        init_db()
        profile_a_id = str(uuid.uuid4())
        profile_b_id = str(uuid.uuid4())
        db = SessionLocal()
        try:
            db.add_all([
                SandboxProfile(
                    id=profile_a_id,
                    name=f"default-repair-a-{profile_a_id}",
                    domain="10.0.1.1:8080",
                    api_key="secret-a",
                    is_default=True,
                    enabled=True,
                ),
                SandboxProfile(
                    id=profile_b_id,
                    name=f"default-repair-b-{profile_b_id}",
                    domain="10.0.1.2:8080",
                    api_key="secret-b",
                    is_default=True,
                    enabled=True,
                ),
            ])
            db.commit()

            default_profile = ensure_default_sandbox_profile(db)
            defaults = (
                db.query(SandboxProfile)
                .filter(SandboxProfile.is_default.is_(True))
                .all()
            )

            assert len(defaults) == 1
            assert defaults[0].id == default_profile.id
            assert default_profile.enabled is True
        finally:
            db.query(SandboxProfile).filter(SandboxProfile.id.in_([profile_a_id, profile_b_id])).delete(
                synchronize_session=False
            )
            db.commit()
            ensure_default_sandbox_profile(db)
            db.close()

    def test_sync_postgres_sequence_handles_uncalled_sequence_at_max_id(self):
        from src.api.models import database as database_module

        max_id_result = MagicMock()
        max_id_result.scalar.return_value = 1
        sequence_result = MagicMock()
        sequence_result.one.return_value = (1, False)
        setval_result = MagicMock()
        conn = MagicMock()
        conn.execute.side_effect = [max_id_result, sequence_result, setval_result]

        database_module._sync_postgres_sequence(conn, "user_memory")

        executed_sql = [str(call.args[0]) for call in conn.execute.call_args_list]
        assert executed_sql == [
            "SELECT COALESCE(MAX(id), 0) FROM user_memory",
            "SELECT last_value, is_called FROM user_memory_id_seq",
            "SELECT setval('user_memory_id_seq', 1, true)",
        ]

    def test_run_cancel_request_migration_handles_legacy_schema_without_request_id(self):
        from src.api.models import database as database_module

        inspector = MagicMock()
        inspector.has_table.return_value = True
        inspector.get_pk_constraint.return_value = {"constrained_columns": ["session_id"]}
        inspector.get_columns.return_value = [
            {"name": "session_id"},
            {"name": "user_id"},
            {"name": "state"},
            {"name": "requested_at"},
            {"name": "acked_at"},
            {"name": "completed_at"},
            {"name": "updated_at"},
        ]

        conn = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = conn
        context.__exit__.return_value = None
        fake_engine = MagicMock()
        fake_engine.begin.return_value = context

        with patch.object(database_module, "engine", fake_engine), \
             patch.object(database_module, "inspect", return_value=inspector):
            database_module._migrate_run_cancel_requests_schema()

        insert_sql = next(
            str(call.args[0])
            for call in conn.execute.call_args_list
            if "INSERT INTO run_cancel_requests_new" in str(call.args[0])
        )
        assert "NULLIF(request_id" not in insert_sql
        assert "md5(random()::text || clock_timestamp()::text)" in insert_sql


class TestMemoryEmbeddingColumnType:
    """记忆向量列类型测试"""

    def test_embedding_column_compiles_to_json_on_sqlite_and_vector_on_postgres(self):
        from sqlalchemy.dialects import postgresql, sqlite

        from src.api.models.user_memory import MemoryEmbedding
        from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS

        embedding_type = MemoryEmbedding.__table__.c.embedding.type

        assert embedding_type.compile(dialect=sqlite.dialect()) == "JSON"
        assert embedding_type.compile(dialect=postgresql.dialect()) == f"vector({MEMORY_EMBEDDING_DIMENSIONS})"

    def test_postgres_existing_vector_column_resizes_to_target_dimensions(self):
        from src.api.models import database as database_module
        from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS

        inspector = MagicMock()
        inspector.has_table.side_effect = lambda table_name: table_name == "memory_embeddings"

        extension_available = MagicMock()
        extension_available.scalar.return_value = True
        column_type = MagicMock()
        column_type.scalar.return_value = "vector(2048)"

        conn = MagicMock()
        conn.execute.side_effect = [extension_available, MagicMock(), column_type, MagicMock()]

        context = MagicMock()
        context.__enter__.return_value = conn
        context.__exit__.return_value = None
        fake_engine = MagicMock()
        fake_engine.begin.return_value = context

        with patch.object(database_module, "engine", fake_engine), \
             patch.object(database_module, "inspect", return_value=inspector), \
             patch.object(database_module, "_sync_postgres_sequence"):
            database_module._migrate_add_columns()

        executed_sql = [str(call.args[0]) for call in conn.execute.call_args_list]
        alter_sql = next(sql for sql in executed_sql if "ALTER TABLE memory_embeddings" in sql)
        assert f"ALTER COLUMN embedding TYPE vector({MEMORY_EMBEDDING_DIMENSIONS})" in alter_sql
        assert "embedding::text" in alter_sql

    def test_missing_pgvector_extension_fails_fast(self):
        from unittest.mock import MagicMock

        import pytest

        from src.api.models import database as database_module

        conn = MagicMock()
        conn.execute.return_value.scalar.return_value = False

        with pytest.raises(RuntimeError, match="pgvector"):
            database_module._ensure_postgres_vector_extension(conn)
