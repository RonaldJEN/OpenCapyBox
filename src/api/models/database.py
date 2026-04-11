"""数据库配置"""
import logging
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pathlib import Path

from src.api.config import get_settings

# 导入所有模型以确保 Base.metadata 包含所有表定义
# （必须在 create_all 之前导入）
def _import_models():
    """延迟导入所有模型，避免循环依赖"""
    from src.api.models import session as _  # noqa: F401
    from src.api.models import round as _  # noqa: F401
    from src.api.models import agui_event as _  # noqa: F401
    from src.api.models.user_sandbox import UserSandbox as _  # noqa: F401
    from src.api.models.conversation_message import ConversationMessage as _  # noqa: F401
    from src.api.models.user_memory import (  # noqa: F401
        UserMemory, MemoryEmbedding, CronJobRun, UserSkillConfig
    )
    from src.api.models.cron_job import CronJob as _  # noqa: F401

logger = logging.getLogger(__name__)

# 从 Settings 读取数据库 URL（可通过 .env 的 DATABASE_URL 覆盖）
_settings = get_settings()
DATABASE_URL = _settings.database_url

# 从 URL 推断并确保数据库目录存在
if DATABASE_URL.startswith("sqlite"):
    # sqlite:///./data/database/open_capy_box.db → ./data/database/
    _db_path = DATABASE_URL.split("///", 1)[-1]
    db_dir = Path(_db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

# 创建引擎
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite 需要
    echo=False,  # 生产环境设为 False
)

# 会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base 类
Base = declarative_base()


def get_db():
    """依赖注入：获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """初始化数据库（创建所有表 + 安全迁移新增列）"""
    _import_models()
    Base.metadata.create_all(bind=engine)
    _migrate_add_columns()


# ============================================================
# 简易数据库迁移（无 Alembic 场景下的安全 ALTER TABLE）
# ============================================================

# 格式: (表名, 列名, 列 DDL 片段)
_PENDING_COLUMNS = [
    ("sessions", "model_id", "VARCHAR(50)"),
    ("rounds", "user_attachments", "TEXT"),
    ("rounds", "interrupt_payload", "TEXT"),
    ("rounds", "idempotency_key", "VARCHAR(64)"),
    ("conversation_messages", "is_synthetic", "BOOLEAN DEFAULT 0"),
]


# 格式: (表名, 约束名, 列列表)
# 用於在存量數據庫上補建 UNIQUE 約束（create_all 只在新建表時生效）
# 注意：值均為可信硬編碼常量，直接用於 DDL 語句拼接
_PENDING_UNIQUE_CONSTRAINTS = [
    ("rounds", "uq_round_session_idempkey", ["session_id", "idempotency_key"]),
]


def _migrate_add_columns():
    """检查并添加缺失的列和约束（幂等，仅在不存在时执行）"""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table_name, column_name, column_type in _PENDING_COLUMNS:
            if not inspector.has_table(table_name):
                continue
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
            if column_name not in existing_columns:
                stmt = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                conn.execute(text(stmt))
                logger.info("DB 迁移: %s 表新增列 %s (%s)", table_name, column_name, column_type)

        # 补建唯一约束（仅对存量库：如果已有覆盖相同列的唯一索引/约束则跳过）
        for table_name, constraint_name, columns in _PENDING_UNIQUE_CONSTRAINTS:
            if not inspector.has_table(table_name):
                continue
            cols_set = set(columns)
            already_covered = any(
                set(idx["column_names"]) == cols_set and idx.get("unique")
                for idx in inspector.get_indexes(table_name)
            ) or any(
                set(uc["column_names"]) == cols_set
                for uc in inspector.get_unique_constraints(table_name)
            )
            if already_covered:
                logger.debug("DB 迁移: 唯一约束已存在，跳过 %s.%s", table_name, constraint_name)
                continue
            cols_str = ", ".join(columns)
            stmt = f"CREATE UNIQUE INDEX IF NOT EXISTS {constraint_name} ON {table_name} ({cols_str})"
            conn.execute(text(stmt))
            logger.info("DB 迁移: 新建唯一约束 %s.%s (%s)", table_name, constraint_name, cols_str)
