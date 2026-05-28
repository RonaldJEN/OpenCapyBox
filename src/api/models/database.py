"""数据库配置"""
import logging
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, QueuePool
from pathlib import Path
from sqlalchemy.exc import OperationalError

from src.api.config import get_settings
from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS

# 导入所有模型以确保 Base.metadata 包含所有表定义
# （必须在 create_all 之前导入）
def _import_models():
    """延迟导入所有模型，避免循环依赖"""
    from src.api.models import session as _  # noqa: F401
    from src.api.models import round as _  # noqa: F401
    from src.api.models import agui_event as _  # noqa: F401
    from src.api.models import user_run_lock as _  # noqa: F401
    from src.api.models import run_cancel_request as _  # noqa: F401
    from src.api.models.auth_user import AuthUser as _  # noqa: F401
    from src.api.models.user_sandbox import UserSandbox as _  # noqa: F401
    from src.api.models.conversation_message import ConversationMessage as _  # noqa: F401
    from src.api.models.interrupt_resolution import InterruptResolution as _  # noqa: F401
    from src.api.models.llm_call_record import LLMCallRecord as _  # noqa: F401
    from src.api.models.user_memory import (  # noqa: F401
        UserMemory, MemoryEmbedding, CronJobRun, UserSkillConfig
    )
    from src.api.models.cron_job import CronJob as _  # noqa: F401
    from src.api.models.cron_fire import CronFire as _  # noqa: F401

logger = logging.getLogger(__name__)

# 从 Settings 读取数据库 URL（可通过 .env 的 DATABASE_URL 覆盖）
_settings = get_settings()
DATABASE_URL = _settings.database_url
_IS_SQLITE = DATABASE_URL.startswith("sqlite")

# 从 URL 推断并确保数据库目录存在
if _IS_SQLITE:
    # sqlite:///./data/database/open_capy_box.db → ./data/database/
    _db_path = DATABASE_URL.split("///", 1)[-1]
    db_dir = Path(_db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

# 创建引擎
# SQLite: 使用 NullPool（文件型 DB 连接开销极低，按需创建/立即释放），
# 彻底避免 asyncio 环境下 QueuePool 耗尽导致的 30s 阻塞死锁。
# PostgreSQL: 使用 QueuePool + pool_pre_ping 保证连接活性。
_engine_kwargs: dict = dict(echo=False)

if _IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    _engine_kwargs["poolclass"] = NullPool
else:
    # PostgreSQL / MySQL 等
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10
    _engine_kwargs["pool_pre_ping"] = True

engine = create_engine(DATABASE_URL, **_engine_kwargs)

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
        try:
            db.close()
        except OperationalError:
            logger.warning("关闭数据库会话时连接已断开", exc_info=True)


def init_db():
    """初始化数据库（创建所有表 + 安全迁移新增列）"""
    _import_models()
    _configure_postgres_extensions()
    Base.metadata.create_all(bind=engine)
    _configure_sqlite_pragmas()
    _migrate_user_run_locks_schema()
    _migrate_add_columns()


def _configure_sqlite_pragmas():
    """SQLite 运行时参数（仅 SQLite 生效）。"""
    if not _IS_SQLITE:
        return
    try:
        with engine.begin() as conn:
            mode = conn.execute(text("PRAGMA journal_mode=WAL")).scalar()
            conn.execute(text("PRAGMA busy_timeout=5000"))
            logger.info("SQLite PRAGMA journal_mode=%s", mode)
    except Exception:
        logger.warning("设置 SQLite WAL 模式失败，继续使用默认 journal_mode", exc_info=True)


def _configure_postgres_extensions():
    """PostgreSQL 扩展初始化。"""
    if _IS_SQLITE:
        return
    with engine.begin() as conn:
        _ensure_postgres_vector_extension(conn)


def _ensure_postgres_vector_extension(conn) -> bool:
    available = conn.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
        )
    """)).scalar()
    if not available:
        raise RuntimeError("PostgreSQL 必须安装 pgvector 扩展：CREATE EXTENSION vector")

    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    return True


def _get_postgres_column_type(conn, table_name: str, column_name: str) -> str | None:
    return conn.execute(
        text("""
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            WHERE c.relname = :table_name
              AND a.attname = :column_name
              AND NOT a.attisdropped
        """),
        {"table_name": table_name, "column_name": column_name},
    ).scalar()


def _is_postgres_vector_type(column_type: str) -> bool:
    return column_type.startswith("vector")


def _sync_postgres_sequence(conn, table_name: str) -> None:
    max_id = conn.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {table_name}")).scalar()
    if max_id > 0:
        seq_name = f"{table_name}_id_seq"
        last_value, is_called = conn.execute(text(f"SELECT last_value, is_called FROM {seq_name}")).one()
        if last_value < max_id or not is_called:
            conn.execute(text(f"SELECT setval('{seq_name}', {max_id}, true)"))
            logger.info("DB 迁移: %s 序列 %s -> %s", seq_name, last_value, max_id)


def _migrate_user_run_locks_schema() -> None:
    """重建短生命周期运行锁表，使其支持 per-user 多 slot 并发。

    user_run_locks 只保存运行时心跳锁，服务启动时也会清空，因此遇到旧版
    user_id 主键 schema 时直接 drop/recreate，比在线改主键更简单可靠。
    """
    inspector = inspect(engine)
    if not inspector.has_table("user_run_locks"):
        return

    pk_cols = inspector.get_pk_constraint("user_run_locks").get("constrained_columns") or []
    columns = {col["name"] for col in inspector.get_columns("user_run_locks")}
    if pk_cols == ["lock_id"] and "slot" in columns:
        return

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE user_run_locks"))

    from src.api.models.user_run_lock import UserRunLock

    UserRunLock.__table__.create(bind=engine, checkfirst=True)
    logger.info("DB 迁移: 重建 user_run_locks 表以支持 per-user 并发 slot")


# ============================================================
# 简易数据库迁移（无 Alembic 场景下的安全 ALTER TABLE）
# ============================================================

# 布尔默认值根据方言选择：SQLite 用 0/1，PostgreSQL 用 FALSE/TRUE
_BOOL_FALSE = "0" if _IS_SQLITE else "FALSE"
_BOOL_TRUE = "1" if _IS_SQLITE else "TRUE"

# 格式: (表名, 列名, 列 DDL 片段)
_PENDING_COLUMNS = [
    ("sessions", "model_id", "VARCHAR(50)"),
    ("rounds", "user_attachments", "TEXT"),
    ("rounds", "interrupt_payload", "TEXT"),
    ("rounds", "idempotency_key", "VARCHAR(64)"),
    ("conversation_messages", "is_synthetic", f"BOOLEAN DEFAULT {_BOOL_FALSE}"),
    ("llm_call_records", "request_message_count", "INTEGER"),
    ("llm_call_records", "manual_review_status", "VARCHAR(20) NOT NULL DEFAULT '没问题'"),
    ("llm_call_records", "first_token_latency_s", "FLOAT"),
    ("llm_call_records", "completion_latency_s", "FLOAT"),
    ("llm_call_records", "compaction_triggered", f"BOOLEAN NOT NULL DEFAULT {_BOOL_FALSE}"),
    ("llm_call_records", "compaction_pre_tokens", "INTEGER"),
    ("llm_call_records", "compaction_post_tokens", "INTEGER"),
    ("llm_call_records", "compaction_tokens_saved", "INTEGER"),
    ("llm_call_records", "compaction_microcompact_compacted_messages", "INTEGER"),
    ("llm_call_records", "compaction_summary_generated_count", "INTEGER"),
    ("llm_call_records", "compaction_summary_reused_count", "INTEGER"),
    ("llm_call_records", "compaction_summary_quality_repair_count", "INTEGER"),
    ("llm_call_records", "compaction_emergency_truncate_dropped_rounds", "INTEGER"),
    ("user_run_locks", "lock_id", "VARCHAR(36) DEFAULT ''"),
    ("user_run_locks", "slot", "INTEGER NOT NULL DEFAULT 0"),
    # Cron 消息中心：未读标记（存量默认已读）、产物元数据、运行工作目录
    ("cron_job_runs", "is_read", f"BOOLEAN DEFAULT {_BOOL_TRUE}"),
    ("cron_job_runs", "artifacts", "TEXT"),
    ("cron_job_runs", "run_workspace", "VARCHAR(500)"),
    # Cron 任务表单：结构化时间配置（前端编辑回显）+ 执行内容（Agent prompt）
    ("cron_jobs", "schedule", "TEXT"),
    ("cron_jobs", "content", "TEXT NOT NULL DEFAULT ''"),
    # auth_users: JWT 凭据代次
    ("auth_users", "token_generation", "INTEGER NOT NULL DEFAULT 0"),
]


# 格式: (表名, 约束名, 列列表)
# 用於在存量數據庫上補建 UNIQUE 約束（create_all 只在新建表時生效）
# 注意：值均為可信硬編碼常量，直接用於 DDL 語句拼接
_PENDING_UNIQUE_CONSTRAINTS = [
    ("rounds", "uq_round_session_idempkey", ["session_id", "idempotency_key"]),
    ("user_sandboxes", "uq_user_sandboxes_user_id", ["user_id"]),
]


def _migrate_add_columns():
    """检查并添加缺失的列和约束（幂等，仅在不存在时执行）"""
    inspector = inspect(engine)
    with engine.begin() as conn:
        table_columns_cache: dict[str, set[str] | None] = {}

        for table_name, column_name, column_type in _PENDING_COLUMNS:
            if table_name not in table_columns_cache:
                if not inspector.has_table(table_name):
                    table_columns_cache[table_name] = None
                else:
                    table_columns_cache[table_name] = {col["name"] for col in inspector.get_columns(table_name)}

            existing_columns = table_columns_cache[table_name]
            if existing_columns is None:
                continue
            if column_name not in existing_columns:
                stmt = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                conn.execute(text(stmt))
                existing_columns.add(column_name)
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

        # PostgreSQL: 修正列类型（毫秒时间戳超出 INTEGER 范围；tool_call_id 超出 36 字符）
        if not _IS_SQLITE and inspector.has_table("agui_events"):
            for col in inspector.get_columns("agui_events"):
                if col["name"] == "timestamp" and str(col["type"]) == "INTEGER":
                    conn.execute(text("ALTER TABLE agui_events ALTER COLUMN timestamp TYPE BIGINT"))
                    logger.info("DB 迁移: agui_events.timestamp 从 INTEGER 升级为 BIGINT")
                if col["name"] == "tool_call_id" and hasattr(col["type"], "length") and (col["type"].length or 0) < 64:
                    conn.execute(text("ALTER TABLE agui_events ALTER COLUMN tool_call_id TYPE VARCHAR(64)"))
                    logger.info("DB 迁移: agui_events.tool_call_id 从 VARCHAR(%s) 升级为 VARCHAR(64)", col["type"].length)

        # PostgreSQL: memory_embeddings.embedding 统一为目标 pgvector 维度
        if not _IS_SQLITE and inspector.has_table("memory_embeddings"):
            has_pgvector = _ensure_postgres_vector_extension(conn)
            if has_pgvector:
                col_type = _get_postgres_column_type(conn, "memory_embeddings", "embedding")
                target_vector_type = f"vector({MEMORY_EMBEDDING_DIMENSIONS})"
                if col_type == target_vector_type:
                    pass
                elif col_type and _is_postgres_vector_type(col_type):
                    conn.execute(text(f"""
                        ALTER TABLE memory_embeddings
                        ALTER COLUMN embedding TYPE vector({MEMORY_EMBEDDING_DIMENSIONS})
                        USING CASE
                            WHEN embedding IS NULL THEN NULL
                            ELSE (
                                '[' || array_to_string(
                                    CASE
                                        WHEN cardinality(translate(embedding::text, '[]', '{{}}')::DOUBLE PRECISION[]) < {MEMORY_EMBEDDING_DIMENSIONS}
                                        THEN translate(embedding::text, '[]', '{{}}')::DOUBLE PRECISION[]
                                            || array_fill(0.0::DOUBLE PRECISION, ARRAY[{MEMORY_EMBEDDING_DIMENSIONS} - cardinality(translate(embedding::text, '[]', '{{}}')::DOUBLE PRECISION[])])
                                        ELSE translate(embedding::text, '[]', '{{}}')::DOUBLE PRECISION[]
                                    END,
                                    ','
                                ) || ']'
                            )::vector({MEMORY_EMBEDDING_DIMENSIONS})
                        END
                        """))
                    logger.info("DB 迁移: memory_embeddings.embedding 从 %s 调整为 vector(%s)", col_type, MEMORY_EMBEDDING_DIMENSIONS)
                elif col_type == "text":
                    conn.execute(text(f"""
                            ALTER TABLE memory_embeddings
                            ALTER COLUMN embedding TYPE vector({MEMORY_EMBEDDING_DIMENSIONS})
                            USING CASE
                                WHEN embedding IS NULL OR embedding = '' THEN NULL
                                ELSE (
                                    '[' || array_to_string(
                                        CASE
                                            WHEN cardinality(translate(embedding, '[]', '{{}}')::DOUBLE PRECISION[]) < {MEMORY_EMBEDDING_DIMENSIONS}
                                            THEN translate(embedding, '[]', '{{}}')::DOUBLE PRECISION[]
                                                || array_fill(0.0::DOUBLE PRECISION, ARRAY[{MEMORY_EMBEDDING_DIMENSIONS} - cardinality(translate(embedding, '[]', '{{}}')::DOUBLE PRECISION[])])
                                            ELSE translate(embedding, '[]', '{{}}')::DOUBLE PRECISION[]
                                        END,
                                        ','
                                    ) || ']'
                                )::vector({MEMORY_EMBEDDING_DIMENSIONS})
                            END
                        """))
                    logger.info("DB 迁移: memory_embeddings.embedding 从 TEXT(JSON) 升级为 vector(%s)", MEMORY_EMBEDDING_DIMENSIONS)
                elif col_type == "double precision[]":
                    conn.execute(text(f"""
                        ALTER TABLE memory_embeddings
                        ALTER COLUMN embedding TYPE vector({MEMORY_EMBEDDING_DIMENSIONS})
                        USING CASE
                            WHEN embedding IS NULL THEN NULL
                            ELSE (
                                '[' || array_to_string(
                                    CASE
                                        WHEN cardinality(embedding) < {MEMORY_EMBEDDING_DIMENSIONS}
                                        THEN embedding || array_fill(0.0::DOUBLE PRECISION, ARRAY[{MEMORY_EMBEDDING_DIMENSIONS} - cardinality(embedding)])
                                        ELSE embedding
                                    END,
                                    ','
                                ) || ']'
                            )::vector({MEMORY_EMBEDDING_DIMENSIONS})
                        END
                        """))
                    logger.info("DB 迁移: memory_embeddings.embedding 从 DOUBLE PRECISION[] 升级为 vector(%s)", MEMORY_EMBEDDING_DIMENSIONS)
                else:
                    raise RuntimeError(f"memory_embeddings.embedding 类型不支持迁移: {col_type}")

        # PostgreSQL: 同步自增序列到 max(id)（pgloader 等工具插入显式 id 后序列可能过时）
        # 注意：下方表名均为硬编码常量，不来自外部输入，f-string 拼接安全。
        if not _IS_SQLITE:
            _SERIAL_TABLES = [
                "agui_events", "conversation_messages", "llm_call_records",
                "user_memory", "memory_embeddings", "cron_jobs", "user_skill_configs",
            ]
            for t in _SERIAL_TABLES:
                if inspector.has_table(t):
                    _sync_postgres_sequence(conn, t)
