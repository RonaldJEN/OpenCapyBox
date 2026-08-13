"""数据库配置"""
import logging
from sqlalchemy import create_engine, text, inspect, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError

from src.agent.schema.skill_key import MAX_SKILL_KEY_LENGTH
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
    from src.api.models import channel_session_binding as _  # noqa: F401
    from src.api.models import subagent_run as _  # noqa: F401
    from src.api.models.auth_user import AuthUser as _  # noqa: F401
    from src.api.models.auth_login_event import AuthLoginEvent as _  # noqa: F401
    from src.api.models.admin_operation_log import AdminOperationLog as _  # noqa: F401
    from src.api.models.llm_model import LLMModel, LLMModelSettings  # noqa: F401
    from src.api.models.model_permission import (  # noqa: F401
        ModelPermissionGroup,
        ModelPermissionGroupModel,
        UserModelPermissionGroup,
    )
    from src.api.models.sandbox_profile import SandboxProfile as _  # noqa: F401
    from src.api.models.user_sandbox_config import UserSandboxConfig as _  # noqa: F401
    from src.api.models.user_sandbox import UserSandbox as _  # noqa: F401
    from src.api.models.user_skill_inventory import UserSkillInventorySnapshot as _  # noqa: F401
    from src.api.models.conversation_message import ConversationMessage as _  # noqa: F401
    from src.api.models.interrupt_resolution import InterruptResolution as _  # noqa: F401
    from src.api.models.llm_call_record import LLMCallRecord as _  # noqa: F401
    from src.api.models.context_checkpoint import ContextCheckpoint as _  # noqa: F401
    from src.api.models.user_memory import (  # noqa: F401
        UserMemory, MemoryEmbedding, CronJobRun, UserSkillConfig
    )
    from src.api.models.cron_job import CronJob as _  # noqa: F401
    from src.api.models.cron_fire import CronFire as _  # noqa: F401
    from src.api.models.mcp import (  # noqa: F401
        McpServer,
        McpCredential,
        McpInstallation,
        McpToolVisibility,
        McpToolSnapshot,
        McpToolSearchIndex,
        McpConfigVersion,
    )
    from src.api.models.tool_permission import (  # noqa: F401
        ToolPermissionRule,
        ToolApprovalRequest,
        ToolPermissionAudit,
    )

logger = logging.getLogger(__name__)

# 从 Settings 读取数据库 URL（可通过 .env 的 DATABASE_URL 覆盖）
_settings = get_settings()
DATABASE_URL = _settings.database_url

# 系统事实库只支持 PostgreSQL：非 PG URL 直接 fail-fast，避免误用 SQLite。
if not DATABASE_URL.startswith(("postgresql", "postgres")):
    raise RuntimeError(
        f"DATABASE_URL 必须是 PostgreSQL（当前: {DATABASE_URL!r}）。"
        "本项目只支持 PostgreSQL 作为事实库。"
    )

# 创建引擎：PostgreSQL 使用 QueuePool + pool_pre_ping 保证连接活性。
# pool_timeout 默认较短，避免连接池耗尽时同步 SQLAlchemy checkout 长时间阻塞
# asyncio 事件循环，表现为整个服务不响应。
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_size=_settings.database_pool_size,
    max_overflow=_settings.database_max_overflow,
    pool_timeout=_settings.database_pool_timeout_seconds,
    pool_recycle=_settings.database_pool_recycle_seconds,
    pool_pre_ping=True,
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
        try:
            db.rollback()
        except OperationalError:
            logger.warning("回滚数据库会话时连接已断开", exc_info=True)
        try:
            db.close()
        except OperationalError:
            logger.warning("关闭数据库会话时连接已断开", exc_info=True)


def get_engine_pool_diagnostics() -> dict[str, object]:
    """Return SQLAlchemy pool settings and live counters for admin diagnostics."""
    pool = engine.pool

    def _maybe_call(name: str):
        value = getattr(pool, name, None)
        if not callable(value):
            return None
        try:
            return value()
        except Exception:
            return None

    return {
        "url_database": engine.url.database,
        "pool_class": type(pool).__name__,
        "status": pool.status(),
        "size": _maybe_call("size"),
        "checked_in": _maybe_call("checkedin"),
        "checked_out": _maybe_call("checkedout"),
        "overflow": _maybe_call("overflow"),
        "configured": {
            "pool_size": _settings.database_pool_size,
            "max_overflow": _settings.database_max_overflow,
            "pool_timeout_seconds": _settings.database_pool_timeout_seconds,
            "pool_recycle_seconds": _settings.database_pool_recycle_seconds,
        },
    }


def init_db():
    """初始化数据库（创建所有表 + 安全迁移新增列）"""
    _import_models()
    _configure_postgres_extensions()
    Base.metadata.create_all(bind=engine)
    _migrate_user_run_locks_schema()
    _migrate_run_cancel_requests_schema()
    _migrate_add_columns()
    _seed_llm_models_from_yaml_if_empty()
    _ensure_default_sandbox_profile()


def _configure_postgres_extensions():
    """PostgreSQL 扩展初始化。"""
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


def _migrate_run_cancel_requests_schema() -> None:
    """Migrate cancel requests to append-only request_id primary key."""
    inspector = inspect(engine)
    if not inspector.has_table("run_cancel_requests"):
        return

    pk_cols = inspector.get_pk_constraint("run_cancel_requests").get("constrained_columns") or []
    columns = {col["name"] for col in inspector.get_columns("run_cancel_requests")}
    required = {"request_id", "session_id", "user_id", "target_run_id", "root_run_id", "requested_after"}
    if pk_cols == ["request_id"] and required.issubset(columns):
        return

    def _col_or_default(column_name: str, default_sql: str) -> str:
        return column_name if column_name in columns else default_sql

    request_id_expr = (
        "COALESCE(NULLIF(request_id, ''), md5(random()::text || clock_timestamp()::text))"
        if "request_id" in columns
        else "md5(random()::text || clock_timestamp()::text)"
    )
    state_expr = _col_or_default("state", "'completed'")
    requested_at_expr = _col_or_default("requested_at", "NOW()")
    updated_at_expr = _col_or_default("updated_at", requested_at_expr)
    acked_at_expr = _col_or_default("acked_at", "NULL")
    completed_at_expr = _col_or_default("completed_at", "NULL")
    user_id_expr = _col_or_default("user_id", "''")

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE run_cancel_requests_new (
                request_id VARCHAR(36) PRIMARY KEY,
                session_id VARCHAR(36) NOT NULL,
                user_id VARCHAR(100) NOT NULL,
                target_run_id VARCHAR(36),
                root_run_id VARCHAR(36),
                requested_after TIMESTAMP,
                state VARCHAR(20) NOT NULL DEFAULT 'requested',
                requested_at TIMESTAMP NOT NULL,
                acked_at TIMESTAMP,
                completed_at TIMESTAMP,
                updated_at TIMESTAMP NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO run_cancel_requests_new (
                request_id, session_id, user_id, state,
                requested_at, acked_at, completed_at, updated_at
            )
            SELECT
                {request_id_expr},
                session_id,
                {user_id_expr},
                COALESCE({state_expr}, 'completed'),
                COALESCE({requested_at_expr}, {updated_at_expr}, NOW()),
                {acked_at_expr},
                {completed_at_expr},
                COALESCE({updated_at_expr}, {requested_at_expr}, NOW())
            FROM run_cancel_requests
        """.format(
            request_id_expr=request_id_expr,
            user_id_expr=user_id_expr,
            state_expr=state_expr,
            requested_at_expr=requested_at_expr,
            updated_at_expr=updated_at_expr,
            acked_at_expr=acked_at_expr,
            completed_at_expr=completed_at_expr,
        )))
        conn.execute(text("DROP TABLE run_cancel_requests"))
        conn.execute(text("ALTER TABLE run_cancel_requests_new RENAME TO run_cancel_requests"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_run_cancel_requests_session_id ON run_cancel_requests (session_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_run_cancel_requests_user_id ON run_cancel_requests (user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_run_cancel_requests_user_session ON run_cancel_requests (user_id, session_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_run_cancel_requests_target_run ON run_cancel_requests (target_run_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_run_cancel_requests_root_run ON run_cancel_requests (root_run_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_run_cancel_requests_requested_after ON run_cancel_requests (requested_after)"))

    logger.info("DB 迁移: run_cancel_requests 调整为 request_id 主键 append-only 审计表")


# ============================================================
# 简易数据库迁移（无 Alembic 场景下的安全 ALTER TABLE）
# ============================================================

# 布尔默认值（PostgreSQL）
_BOOL_FALSE = "FALSE"
_BOOL_TRUE = "TRUE"

# 格式: (表名, 列名, 列 DDL 片段)
_PENDING_COLUMNS = [
    ("sessions", "model_id", "VARCHAR(50)"),
    ("rounds", "user_attachments", "TEXT"),
    ("rounds", "preferred_skills", "TEXT"),
    ("rounds", "interrupt_payload", "TEXT"),
    ("rounds", "idempotency_key", "VARCHAR(64)"),
    ("conversation_messages", "is_synthetic", f"BOOLEAN DEFAULT {_BOOL_FALSE}"),
    ("llm_call_records", "request_message_count", "INTEGER"),
    ("llm_call_records", "call_kind", "VARCHAR(30) NOT NULL DEFAULT 'agent_step'"),
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
    ("llm_call_records", "history_strategy", "VARCHAR(30)"),
    ("llm_call_records", "checkpoint_id", "VARCHAR(36)"),
    ("llm_call_records", "history_payload_sha256", "VARCHAR(64)"),
    ("llm_call_records", "history_breakdown_json", "TEXT"),
    ("llm_models", "auto_compact_token_limit", "INTEGER"),
    ("llm_models", "tool_output_truncation_bytes", "INTEGER NOT NULL DEFAULT 10000"),
    ("context_checkpoints", "source_message_sequence", "INTEGER NOT NULL DEFAULT 0"),
    ("context_checkpoints", "source_event_sequence", "INTEGER NOT NULL DEFAULT 0"),
    ("context_checkpoints", "trigger_phase", "VARCHAR(30) NOT NULL DEFAULT 'pre_turn'"),
    ("context_checkpoints", "summary_text", "TEXT NOT NULL DEFAULT ''"),
    ("user_run_locks", "lock_id", "VARCHAR(36) DEFAULT ''"),
    ("user_run_locks", "slot", "INTEGER NOT NULL DEFAULT 0"),
    # Cron 消息中心：未读标记（存量默认已读）、产物元数据、运行工作目录
    ("cron_job_runs", "is_read", f"BOOLEAN DEFAULT {_BOOL_TRUE}"),
    ("cron_job_runs", "artifacts", "TEXT"),
    ("cron_job_runs", "run_workspace", "VARCHAR(500)"),
    ("cron_job_runs", "rule_version", "INTEGER"),
    ("cron_job_runs", "scheduled_at", "TIMESTAMP"),
    ("cron_job_runs", "trigger_source", "VARCHAR(20) NOT NULL DEFAULT 'scheduled'"),
    # Cron 任务表单：结构化时间配置（前端编辑回显）+ 执行内容（Agent prompt）
    ("cron_jobs", "schedule", "TEXT"),
    ("cron_jobs", "content", "TEXT NOT NULL DEFAULT ''"),
    ("cron_jobs", "rule_version", "INTEGER NOT NULL DEFAULT 1"),
    ("cron_fires", "rule_version", "INTEGER NOT NULL DEFAULT 1"),
    ("user_skill_inventory_snapshots", "issues_json", "TEXT NOT NULL DEFAULT '[]'"),
    # auth_users: JWT 凭据代次
    ("auth_users", "token_generation", "INTEGER NOT NULL DEFAULT 0"),
    # sandbox profile routing
    ("sandbox_profiles", "description", "TEXT"),
    ("sandbox_profiles", "department", "VARCHAR(100)"),
    ("sandbox_profiles", "domain", "VARCHAR(255)"),
    ("sandbox_profiles", "protocol", "VARCHAR(10) DEFAULT 'http'"),
    ("sandbox_profiles", "api_key", "TEXT"),
    ("sandbox_profiles", "use_server_proxy", f"BOOLEAN DEFAULT {_BOOL_TRUE}"),
    ("sandbox_profiles", "is_default", f"BOOLEAN DEFAULT {_BOOL_FALSE}"),
    ("sandbox_profiles", "enabled", f"BOOLEAN DEFAULT {_BOOL_TRUE}"),
    ("sandbox_profiles", "version", "INTEGER DEFAULT 1"),
    ("sandbox_profiles", "created_at", "TIMESTAMP DEFAULT NOW()"),
    ("sandbox_profiles", "updated_at", "TIMESTAMP DEFAULT NOW()"),
    ("user_sandboxes", "active_profile_id", "VARCHAR(36)"),
    ("user_sandboxes", "active_profile_version", "INTEGER"),
    ("mcp_servers", "last_tools_count", "INTEGER"),
    ("mcp_tool_visibility", "revision", "INTEGER NOT NULL DEFAULT 1"),
    ("tool_approval_requests", "connection_fingerprint", "VARCHAR(64)"),
    ("tool_approval_requests", "execution_claim_token", "VARCHAR(64)"),
    ("tool_approval_requests", "execution_lease_expires_at", "TIMESTAMP"),
    ("mcp_tool_snapshots", "connection_fingerprint", "VARCHAR(64)"),
]


# 格式: (表名, 约束名, 列列表)
# 用於在存量數據庫上補建 UNIQUE 約束（create_all 只在新建表時生效）
# 注意：值均為可信硬編碼常量，直接用於 DDL 語句拼接
_PENDING_UNIQUE_CONSTRAINTS = [
    ("rounds", "uq_round_session_idempkey", ["session_id", "idempotency_key"]),
    ("sandbox_profiles", "uq_sandbox_profiles_name", ["name"]),
    ("user_sandboxes", "uq_user_sandboxes_user_id", ["user_id"]),
    ("user_sandbox_configs", "uq_user_sandbox_configs_user_id", ["user_id"]),
]


_DEPRECATED_COLUMNS = [
    ("sandbox_profiles", "image"),
    ("sandbox_profiles", "cpu_limit"),
    ("sandbox_profiles", "memory_limit"),
    ("sandbox_profiles", "storage_root"),
    ("sandbox_profiles", "mount_path"),
]


def _backfill_sandbox_profiles(conn, columns: set[str]) -> None:
    """Backfill rows created by the legacy sandbox_profiles schema."""
    if "domain" in columns:
        conn.execute(
            text("UPDATE sandbox_profiles SET domain = :domain WHERE domain IS NULL OR domain = ''"),
            {"domain": _settings.sandbox_domain},
        )
    if "protocol" in columns:
        conn.execute(
            text("UPDATE sandbox_profiles SET protocol = :protocol WHERE protocol IS NULL OR protocol = ''"),
            {"protocol": _settings.sandbox_protocol or "http"},
        )
    if "api_key" in columns:
        conn.execute(
            text("UPDATE sandbox_profiles SET api_key = :api_key WHERE api_key IS NULL"),
            {"api_key": _settings.sandbox_api_key},
        )
    if "use_server_proxy" in columns:
        conn.execute(
            text("UPDATE sandbox_profiles SET use_server_proxy = :use_server_proxy WHERE use_server_proxy IS NULL"),
            {"use_server_proxy": bool(_settings.sandbox_use_server_proxy)},
        )
    if "is_default" in columns:
        conn.execute(text(f"UPDATE sandbox_profiles SET is_default = {_BOOL_FALSE} WHERE is_default IS NULL"))
    if "enabled" in columns:
        conn.execute(text(f"UPDATE sandbox_profiles SET enabled = {_BOOL_TRUE} WHERE enabled IS NULL"))
    if "version" in columns:
        conn.execute(text("UPDATE sandbox_profiles SET version = 1 WHERE version IS NULL OR version < 1"))
    if "created_at" in columns:
        conn.execute(text("UPDATE sandbox_profiles SET created_at = NOW() WHERE created_at IS NULL"))
    if "updated_at" in columns:
        conn.execute(text("UPDATE sandbox_profiles SET updated_at = NOW() WHERE updated_at IS NULL"))


def _ensure_default_sandbox_profile() -> None:
    """Bootstrap a default sandbox profile from legacy environment settings."""
    from src.api.services.sandbox_profile_service import ensure_default_sandbox_profile
    from src.api.models.user_sandbox import UserSandbox

    with SessionLocal() as db:
        default_profile = ensure_default_sandbox_profile(db)
        updated = (
            db.query(UserSandbox)
            .filter(UserSandbox.sandbox_id.isnot(None))
            .filter(
                or_(
                    UserSandbox.active_profile_id.is_(None),
                    UserSandbox.active_profile_version.is_(None),
                )
            )
            .update(
                {
                    UserSandbox.active_profile_id: default_profile.id,
                    UserSandbox.active_profile_version: int(default_profile.version or 1),
                },
                synchronize_session=False,
            )
        )
        if updated:
            db.commit()
            logger.info("DB 迁移: 已回填 %s 条 user_sandboxes 默认 profile 指纹", updated)


def _seed_llm_models_from_yaml_if_empty() -> None:
    """Seed the DB model catalog from models.yaml once.

    After the first seed, admins own runtime model configuration in the DB.
    """
    from src.api.services.model_access_service import seed_model_catalog_from_yaml_if_empty

    with SessionLocal() as db:
        seeded = seed_model_catalog_from_yaml_if_empty(db)
        if seeded:
            logger.info("DB 初始化: 已从 models.yaml 导入 %s 个模型", seeded)


def _migrate_add_columns(target_engine=None):
    """检查并添加缺失的列和约束（幂等，仅在不存在时执行）"""
    bind_engine = target_engine or engine
    inspector = inspect(bind_engine)
    with bind_engine.begin() as conn:
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

        # The periodic reconciler must not scan the full approval history on
        # an existing production database. ``create_all`` covers new installs;
        # this explicit migration covers tables that predate execution leases.
        if inspector.has_table("tool_approval_requests"):
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_tool_approval_execution_lease "
                "ON tool_approval_requests (status, execution_lease_expires_at)"
            ))

        for table_name, column_name in _DEPRECATED_COLUMNS:
            if not inspector.has_table(table_name):
                continue
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
            if column_name in existing_columns:
                conn.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))
                logger.info("DB 迁移: %s 表删除废弃列 %s", table_name, column_name)

        sandbox_profile_columns = table_columns_cache.get("sandbox_profiles")
        if sandbox_profile_columns:
            _backfill_sandbox_profiles(conn, sandbox_profile_columns)

        _ensure_agui_events_run_sequence_unique(conn, inspector)
        _ensure_admin_monitor_indexes(conn, inspector)

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
        if inspector.has_table("agui_events"):
            for col in inspector.get_columns("agui_events"):
                if col["name"] == "timestamp" and str(col["type"]) == "INTEGER":
                    conn.execute(text("ALTER TABLE agui_events ALTER COLUMN timestamp TYPE BIGINT"))
                    logger.info("DB 迁移: agui_events.timestamp 从 INTEGER 升级为 BIGINT")
                if col["name"] == "tool_call_id" and hasattr(col["type"], "length") and (col["type"].length or 0) < 64:
                    conn.execute(text("ALTER TABLE agui_events ALTER COLUMN tool_call_id TYPE VARCHAR(64)"))
                    logger.info("DB 迁移: agui_events.tool_call_id 从 VARCHAR(%s) 升级为 VARCHAR(64)", col["type"].length)

        if inspector.has_table("sessions"):
            for col in inspector.get_columns("sessions"):
                if col["name"] == "model_id" and hasattr(col["type"], "length") and (col["type"].length or 0) < 100:
                    conn.execute(text("ALTER TABLE sessions ALTER COLUMN model_id TYPE VARCHAR(100)"))
                    logger.info("DB 迁移: sessions.model_id 从 VARCHAR(%s) 升级为 VARCHAR(100)", col["type"].length)

        if (
            bind_engine.dialect.name == "postgresql"
            and inspector.has_table("context_checkpoints")
        ):
            conn.execute(text(
                "ALTER TABLE context_checkpoints ALTER COLUMN source_round_id DROP NOT NULL"
            ))
            conn.execute(text(
                "ALTER TABLE context_checkpoints ALTER COLUMN replacement_sha256 DROP NOT NULL"
            ))

        if inspector.has_table("user_skill_configs"):
            for col in inspector.get_columns("user_skill_configs"):
                if (
                    col["name"] == "skill_name"
                    and hasattr(col["type"], "length")
                    and (col["type"].length or 0) < MAX_SKILL_KEY_LENGTH
                ):
                    conn.execute(text(
                        "ALTER TABLE user_skill_configs ALTER COLUMN skill_name "
                        f"TYPE VARCHAR({MAX_SKILL_KEY_LENGTH})"
                    ))
                    logger.info(
                        "DB 迁移: user_skill_configs.skill_name 从 VARCHAR(%s) 升级为 VARCHAR(%s)",
                        col["type"].length,
                        MAX_SKILL_KEY_LENGTH,
                    )

        # PostgreSQL: memory_embeddings.embedding 统一为目标 pgvector 维度
        if inspector.has_table("memory_embeddings"):
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

        # PostgreSQL: 同步自增序列到 max(id)（外部工具插入显式 id 后序列可能过时）
        # 注意：下方表名均为硬编码常量，不来自外部输入，f-string 拼接安全。
        _SERIAL_TABLES = [
            "agui_events", "conversation_messages", "llm_call_records",
            "user_memory", "memory_embeddings", "cron_jobs", "user_skill_configs",
        ]
        for t in _SERIAL_TABLES:
            if inspector.has_table(t):
                _sync_postgres_sequence(conn, t)


def _ensure_agui_events_run_sequence_unique(conn, inspector) -> None:
    """Ensure agui_events(run_id, sequence) is unique without rewriting history."""
    table_name = "agui_events"
    if not inspector.has_table(table_name):
        return

    cols_set = {"run_id", "sequence"}
    already_covered = any(
        set(idx["column_names"]) == cols_set and idx.get("unique")
        for idx in inspector.get_indexes(table_name)
    ) or any(
        set(uc["column_names"]) == cols_set
        for uc in inspector.get_unique_constraints(table_name)
    )
    if already_covered:
        logger.debug("DB 迁移: agui_events(run_id, sequence) 唯一约束已存在，跳过")
        return

    duplicates = conn.execute(text("""
        SELECT run_id, sequence, COUNT(*) AS count
        FROM agui_events
        GROUP BY run_id, sequence
        HAVING COUNT(*) > 1
        ORDER BY count DESC, run_id, sequence
        LIMIT 10
    """)).mappings().all()
    if duplicates:
        sample = [
            f"run_id={row['run_id']} sequence={row['sequence']} count={row['count']}"
            for row in duplicates
        ]
        raise RuntimeError(
            "agui_events 存在重复 (run_id, sequence)，请先用一次性运维脚本清洗后再启动："
            + "; ".join(sample)
        )

    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agui_events_run_sequence "
        "ON agui_events (run_id, sequence)"
    ))
    logger.info("DB 迁移: 新建唯一索引 agui_events.uq_agui_events_run_sequence (run_id, sequence)")


def _ensure_admin_monitor_indexes(conn, inspector) -> None:
    """Create indexes used by the admin Session monitor on existing databases."""
    if inspector.has_table("rounds"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rounds_session_created_at "
            "ON rounds (session_id, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rounds_status_created_at "
            "ON rounds (status, created_at)"
        ))
    if inspector.has_table("llm_call_records"):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_llm_call_records_round_step "
            "ON llm_call_records (round_id, step_index)"
        ))
