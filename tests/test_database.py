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
        """验证默认 DATABASE_URL 是 sqlite 路径"""
        from src.api.models.database import DATABASE_URL

        assert "sqlite" in DATABASE_URL
        assert "open_capy_box.db" in DATABASE_URL

    def test_engine_created_with_settings_url(self):
        """验证 engine 使用了 Settings 中的 URL"""
        from src.api.models.database import engine

        # SQLAlchemy engine 的 URL 应该包含我们的数据库名
        assert "open_capy_box" in str(engine.url)

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


class TestDatabaseMigration:
    """测试数据库迁移逻辑"""

    def test_migrate_add_columns_is_idempotent(self):
        """验证 _migrate_add_columns 可以重复调用（幂等）"""
        from src.api.models.database import init_db

        # 调用两次不应报错
        init_db()
        init_db()
