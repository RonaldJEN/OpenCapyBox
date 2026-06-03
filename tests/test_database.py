"""数据库配置测试 — 验证 database.py 正确读取 Settings.database_url"""
import pytest
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

    def test_migrate_add_columns_is_idempotent(self):
        """验证 _migrate_add_columns 可以重复调用（幂等）"""
        from src.api.models.database import init_db

        # 调用两次不应报错
        init_db()
        init_db()

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
