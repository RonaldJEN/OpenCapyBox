"""应用配置"""
import hashlib
import logging
import secrets
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List

_config_logger = logging.getLogger(__name__)


_MIN_SECRET_KEY_LENGTH = 32
_DEBUG_EPHEMERAL_AUTH_SECRET = secrets.token_urlsafe(48)
_EXAMPLE_SECRET_SENTINELS = frozenset(
    {
        "change-me",
        "changeme",
        "replace-me",
        "replace-with-a-random-secret-string",
        "replace-with-an-independent-random-secret-string",
        "secret",
        "your-secret",
        "your-secret-key",
    }
)
_EXAMPLE_BOOTSTRAP_CREDENTIALS = frozenset(
    {
        ("demo", "demo123"),
        ("replace-user", "replace-with-a-strong-unique-password"),
        ("test", "test123"),
    }
)


def _is_example_secret(value: str, *, app_name: str) -> bool:
    """Return whether a configured key is a public/example sentinel.

    The legacy app-name-derived key is included so a deployment cannot retain
    the previously documented deterministic fallback by copying its value into
    the environment.
    """

    normalized = str(value or "").strip().lower()
    if not normalized:
        return False
    if normalized in _EXAMPLE_SECRET_SENTINELS:
        return True
    if normalized.startswith(("replace-with-", "your-random-", "example-")):
        return True
    legacy_default = hashlib.sha256(
        f"{app_name}:agentskills-default-dev-key".encode()
    ).hexdigest()
    return normalized == legacy_default


def _unsafe_bootstrap_credentials(settings: "Settings") -> list[str]:
    unsafe: list[str] = []
    for username, password in settings.get_auth_users().items():
        identity = (username.strip().lower(), password)
        if identity in _EXAMPLE_BOOTSTRAP_CREDENTIALS:
            unsafe.append(username.strip())
    return sorted(set(unsafe))


def _apply_runtime_secret_policy(settings: "Settings") -> "Settings":
    """Validate production secrets and install safe development fallbacks."""

    auth_secret = str(settings.auth_secret_key or "").strip()
    mcp_secret = str(settings.mcp_secret_key or "").strip()
    auth_is_example = _is_example_secret(auth_secret, app_name=settings.app_name)
    mcp_is_example = _is_example_secret(mcp_secret, app_name=settings.app_name)

    if not settings.debug:
        errors: list[str] = []
        if not auth_secret:
            errors.append("AUTH_SECRET_KEY must be configured")
        elif auth_is_example:
            errors.append("AUTH_SECRET_KEY must not use an example value")
        elif len(auth_secret) < _MIN_SECRET_KEY_LENGTH:
            errors.append("AUTH_SECRET_KEY must contain at least 32 characters")

        if not mcp_secret:
            errors.append("MCP_SECRET_KEY must be configured independently")
        elif mcp_is_example:
            errors.append("MCP_SECRET_KEY must not use an example value")
        elif len(mcp_secret) < _MIN_SECRET_KEY_LENGTH:
            errors.append("MCP_SECRET_KEY must contain at least 32 characters")
        if (
            auth_secret
            and mcp_secret
            and auth_secret == mcp_secret
        ):
            errors.append("MCP_SECRET_KEY must be different from AUTH_SECRET_KEY")

        unsafe_users = _unsafe_bootstrap_credentials(settings)
        if unsafe_users:
            errors.append(
                "SIMPLE_AUTH_USERS contains public example credentials for: "
                + ", ".join(unsafe_users)
            )
        if errors:
            raise RuntimeError(
                "Unsafe production authentication configuration: "
                + "; ".join(errors)
            )
    else:
        if not auth_secret or auth_is_example:
            settings.auth_secret_key = _DEBUG_EPHEMERAL_AUTH_SECRET
            auth_secret = settings.auth_secret_key
            _config_logger.warning(
                "DEBUG 模式下 AUTH_SECRET_KEY 未配置或使用示例值；"
                "已生成仅当前进程有效的随机密钥，重启后现有登录会话将失效。"
            )
        elif len(auth_secret) < _MIN_SECRET_KEY_LENGTH:
            _config_logger.warning(
                "DEBUG 模式下 AUTH_SECRET_KEY 少于 32 个字符；请改用足够长的随机值。"
            )

        if not mcp_secret or mcp_is_example:
            # Keep the field empty so secret_crypto can explicitly derive its
            # development-only envelope key from the randomized auth secret.
            settings.mcp_secret_key = ""
            _config_logger.warning(
                "DEBUG 模式下 MCP_SECRET_KEY 未配置或使用示例值；"
                "MCP 加密密钥将从当前进程的 AUTH_SECRET_KEY 派生。"
            )
        elif len(mcp_secret) < _MIN_SECRET_KEY_LENGTH:
            _config_logger.warning(
                "DEBUG 模式下 MCP_SECRET_KEY 少于 32 个字符；请改用足够长的随机值。"
            )

        unsafe_users = _unsafe_bootstrap_credentials(settings)
        if unsafe_users:
            _config_logger.warning(
                "DEBUG 模式正在使用公开示例 SIMPLE_AUTH_USERS（%s）；"
                "不得用于可被他人访问的环境。",
                ", ".join(unsafe_users),
            )

    settings.auth_secret_key = str(settings.auth_secret_key).strip()
    settings.mcp_secret_key = str(settings.mcp_secret_key or "").strip()
    return settings


class Settings(BaseSettings):
    """BaseSettings 的初始化逻辑自动读取项目根目录下的 .env 文件"""

    # 应用配置
    app_name: str = "OpenCapyBox Backend"
    app_version: str = "0.1.0"
    debug: bool = False

    # API 配置
    api_prefix: str = "/api"
    cors_origins: List[str] = ["http://localhost:3000"]

    # 首次 bootstrap 认证用户（格式：username:password,username2:password2）
    simple_auth_users: str = ""
    auth_admin_users: str = "admin"  # 首次 bootstrap 管理员用户名列表，逗号分隔

    # 认证配置（Bearer Token）
    auth_secret_key: str = ""
    auth_token_expire_minutes: int = 720

    # 企业微信移动端 SSO。浏览器先经企业网关建立 Cookie，后端再调用
    # curUser 校验域账号并签发 OpenCapyBox 自身会话 Cookie。
    mobile_sso_gateway_base_url: str = ""
    mobile_sso_current_user_path: str = "/base/sys/role/curUser"
    mobile_sso_timeout_seconds: float = 10.0
    # 企业网关要求的可选附加请求头；名称留空时不发送。
    mobile_sso_gateway_header_name: str = ""
    mobile_sso_gateway_header_value: str = ""
    mobile_auth_cookie_secure: bool = True

    # LDAP 直连认证配置。LDAP_URLS 支持逗号分隔主备地址。
    ldap_urls: str = ""
    ldap_user_domain: str = ""

    # 数据库配置（仅支持 PostgreSQL，必须在 .env 中配置 DATABASE_URL）
    database_url: str = "postgresql://postgres:postgres@localhost:5432/open_capy_box"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_timeout_seconds: int = 5
    database_pool_recycle_seconds: int = 1800

    # LLM API 配置（仅作为 Model Registry 不可用时的 fallback）
    llm_api_key: str = ""  # API 密钥（必须在 .env 中配置 LLM_API_KEY）
    llm_api_base: str = "https://api.minimax.chat"  # API 基础地址
    llm_model: str = "MiniMax-Text-01"  # 模型名称
    llm_provider: str = "anthropic"  # 提供商：anthropic 或 openai

    # 搜索工具配置（可选）
    bocha_search_appcode: str = ""  # 博查搜索 AppCode，用于 Web 搜索工具

    # OpenSandbox 配置（真实值请在 .env 中配置）
    sandbox_domain: str = ""  # OpenSandbox 服务地址，如 "localhost:8080"
    sandbox_api_key: str = ""  # OpenSandbox API Key
    sandbox_image: str = "code-interpreter-agent:v1.1.0"
    sandbox_protocol: str = "http"  # http 或 https
    sandbox_use_server_proxy: bool = True  # 是否使用服务器代理模式
    sandbox_timeout_minutes: int = 60  # 沙箱超时时间（分钟）
    sandbox_ready_timeout_seconds: int = 120  # 沙箱就绪超时（秒）
    sandbox_persistent_storage_enabled: bool = True  # 啟用 session 持久化存儲掛載
    sandbox_host_storage_root: str = "/tmp/sandbox"  # OpenSandbox 宿主機持久化根路徑
    sandbox_storage_mount_path: str = "/home/user"  # 容器內掛載路徑
    sandbox_background_command_timeout_seconds: int = 21600  # 后台 bash 命令服务端超时（秒），0 表示禁用

    # Agent 配置
    agent_max_steps: int = 100
    agent_max_history_messages: int = 120  # 歷史消息注入上限（條數，含 user/assistant/tool），超出時只保留最近 N 條
    agent_tool_timeout: int = 300  # 单次工具执行超时（秒），0 表示不限
    agent_subagent_max_parallel: int = 3  # 同一父 Agent step 内最多并行执行的 sub_agent 数；1 表示串行
    agent_user_concurrency_limit: int = 1  # 同一用户允许同时运行的不同会话数，至少为 1
    skill_disabled_cache_ttl_seconds: float = 30.0  # Skill 启停快照复用窗口（秒），避免每步 LLM 请求都查库
    # 已批准工具使用可续租执行 lease。lease 过期只会收敛为 unknown，
    # 永远不会把外部调用重新排队执行。
    tool_approval_execution_lease_seconds: float = 120.0
    tool_approval_lease_heartbeat_seconds: float = 30.0
    tool_approval_reconcile_interval_seconds: float = 30.0

    # MCP（仅 Streamable HTTP）
    # 凭证加密主密钥；生产必填，仅 DEBUG 模式可从进程随机 AUTH_SECRET_KEY 派生。
    mcp_secret_key: str = ""
    mcp_test_timeout_seconds: float = 15.0
    # 有有效 MCP 连接的用户按此周期重新发现远端 tools/list，避免同一连接下 schema 漂移。
    mcp_catalog_refresh_seconds: float = 300.0
    # Process-local resolved catalogs are a bounded, per-user LRU. Logical
    # bytes count model-facing metadata, not Python allocator overhead.
    mcp_catalog_cache_max_users: int = 64
    mcp_catalog_cache_max_bytes: int = 64 * 1024 * 1024
    mcp_catalog_cache_idle_ttl_seconds: float = 900.0
    # Per-user catalog/connection budgets. Required official integrations are
    # governed by their own platform limit and do not consume the opt-in quota.
    mcp_personal_server_limit: int = 20
    mcp_user_enabled_connection_limit: int = 20
    mcp_required_official_server_limit: int = 10
    mcp_discovery_timeout_seconds: float = 60.0  # 单个 MCP tools/list 全流程墙钟超时
    mcp_catalog_build_timeout_seconds: float = 120.0  # 单用户完整 MCP 目录构建墙钟超时
    # 单次 tools/call 从 DNS、握手到结果返回的独立全流程墙钟期限。
    # 此安全边界始终开启，不受可关闭的 AGENT_TOOL_TIMEOUT 影响。
    mcp_call_timeout_seconds: float = 300.0
    # 只重试尚未跨过 tools/call dispatch 边界的连接/初始化失败。
    # attempts 包含首次尝试，避免任何已发送的远端操作被重复执行。
    mcp_connect_retry_attempts: int = 3
    mcp_connect_retry_base_delay_seconds: float = 0.5
    mcp_max_concurrent_discoveries_per_user: int = 8
    mcp_max_concurrent_discoveries_global: int = 16  # 单进程内、跨用户发现并发上限
    mcp_max_installations_per_user: int = 32
    mcp_max_tools_per_user: int = 2048

    # Cron 配置（去中心化 worker）
    cron_fire_max_age_days: int = 7  # cron_fires 清理保留天数（后续清理任务使用）
    cron_dispatch_catch_up_max_minutes: int = 60  # worker 醒来后最多补扫的漏调度分钟数

    # Agent 資源路徑配置（可通過 .env 覆蓋，預設相對於 src/agent/）
    skills_dir: str = ""          # 留空則自動定位到 src/agent/skills/

    # SSE 订阅配置
    sse_heartbeat_interval: int = 15  # 心跳间隔（秒），同时用作锁心跳写入间隔
    sse_subscribe_timeout: int = 300  # 订阅超时（秒，5分钟），同时用作锁陈旧阈值
    cancel_watcher_interval_seconds: float = 3.0  # 订阅持久化事件补偿轮询间隔（秒）
    agui_repair_terminal_since_hours: int = 24  # terminal repair 默认扫描窗口（小时）

    # Embedding 配置（不填则向量检索降级为关键词搜索）
    embedding_api_key: str = ""
    embedding_api_base: str = "https://api.openai.com/v1"
    embedding_model: str = "text-embedding-3-small"
    embedding_chunk_size: int = 512  # 分块字符数

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # 忽略额外字段，避免验证错误

    @field_validator("sandbox_background_command_timeout_seconds")
    @classmethod
    def validate_sandbox_background_command_timeout_seconds(cls, value: int) -> int:
        if value < 0:
            raise ValueError("sandbox_background_command_timeout_seconds must be >= 0")
        return value

    @field_validator(
        "database_pool_size",
        "database_pool_timeout_seconds",
        "database_pool_recycle_seconds",
    )
    @classmethod
    def validate_positive_database_pool_settings(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("database pool settings must be > 0")
        return value

    @field_validator("database_max_overflow")
    @classmethod
    def validate_database_max_overflow(cls, value: int) -> int:
        if value < 0:
            raise ValueError("database_max_overflow must be >= 0")
        return value

    @field_validator("cron_dispatch_catch_up_max_minutes")
    @classmethod
    def validate_cron_dispatch_catch_up_max_minutes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("cron_dispatch_catch_up_max_minutes must be > 0")
        return value

    @field_validator("mcp_test_timeout_seconds")
    @classmethod
    def validate_mcp_test_timeout_seconds(cls, value: float) -> float:
        if value <= 0 or value > 120:
            raise ValueError("mcp_test_timeout_seconds must be > 0 and <= 120")
        return value

    @field_validator("mcp_catalog_refresh_seconds")
    @classmethod
    def validate_mcp_catalog_refresh_seconds(cls, value: float) -> float:
        if value <= 0 or value > 86400:
            raise ValueError(
                "mcp_catalog_refresh_seconds must be > 0 and <= 86400"
            )
        return value

    @field_validator("mcp_catalog_cache_max_users")
    @classmethod
    def validate_mcp_catalog_cache_max_users(cls, value: int) -> int:
        if value <= 0 or value > 4096:
            raise ValueError("mcp_catalog_cache_max_users must be > 0 and <= 4096")
        return value

    @field_validator("mcp_catalog_cache_max_bytes")
    @classmethod
    def validate_mcp_catalog_cache_max_bytes(cls, value: int) -> int:
        if value <= 0 or value > 1024 * 1024 * 1024:
            raise ValueError(
                "mcp_catalog_cache_max_bytes must be > 0 and <= 1073741824"
            )
        return value

    @field_validator("mcp_catalog_cache_idle_ttl_seconds")
    @classmethod
    def validate_mcp_catalog_cache_idle_ttl_seconds(cls, value: float) -> float:
        if value <= 0 or value > 7 * 86400:
            raise ValueError(
                "mcp_catalog_cache_idle_ttl_seconds must be > 0 and <= 604800"
            )
        return value

    @field_validator(
        "mcp_personal_server_limit",
        "mcp_user_enabled_connection_limit",
        "mcp_required_official_server_limit",
    )
    @classmethod
    def validate_mcp_resource_limits(cls, value: int) -> int:
        if value <= 0 or value > 512:
            raise ValueError("MCP resource limits must be > 0 and <= 512")
        return value

    @field_validator(
        "mcp_discovery_timeout_seconds",
        "mcp_catalog_build_timeout_seconds",
        "mcp_call_timeout_seconds",
    )
    @classmethod
    def validate_mcp_wall_clock_timeouts(cls, value: float) -> float:
        if not 0 < value <= 600:
            raise ValueError("MCP wall-clock timeouts must be > 0 and <= 600")
        return value

    @field_validator("mcp_connect_retry_attempts")
    @classmethod
    def validate_mcp_connect_retry_attempts(cls, value: int) -> int:
        if value <= 0 or value > 10:
            raise ValueError("mcp_connect_retry_attempts must be > 0 and <= 10")
        return value

    @field_validator("mcp_connect_retry_base_delay_seconds")
    @classmethod
    def validate_mcp_connect_retry_base_delay_seconds(cls, value: float) -> float:
        if not 0 <= value <= 30:
            raise ValueError(
                "mcp_connect_retry_base_delay_seconds must be >= 0 and <= 30"
            )
        return value

    @field_validator(
        "mcp_max_concurrent_discoveries_per_user",
        "mcp_max_concurrent_discoveries_global",
    )
    @classmethod
    def validate_mcp_discovery_concurrency(cls, value: int) -> int:
        if value <= 0 or value > 64:
            raise ValueError("MCP discovery concurrency must be > 0 and <= 64")
        return value

    @field_validator("mcp_max_installations_per_user")
    @classmethod
    def validate_mcp_installation_limit(cls, value: int) -> int:
        if value <= 0 or value > 128:
            raise ValueError("mcp_max_installations_per_user must be > 0 and <= 128")
        return value

    @field_validator("mcp_max_tools_per_user")
    @classmethod
    def validate_mcp_tool_limit(cls, value: int) -> int:
        if value <= 0 or value > 8192:
            raise ValueError("mcp_max_tools_per_user must be > 0 and <= 8192")
        return value

    @field_validator(
        "tool_approval_execution_lease_seconds",
        "tool_approval_lease_heartbeat_seconds",
        "tool_approval_reconcile_interval_seconds",
    )
    @classmethod
    def validate_tool_approval_lease_intervals(cls, value: float) -> float:
        if value <= 0 or value > 3600:
            raise ValueError("tool approval lease intervals must be > 0 and <= 3600")
        return value

    @model_validator(mode="after")
    def validate_mcp_limit_relationships(self):
        if (
            self.mcp_max_concurrent_discoveries_per_user
            > self.mcp_max_concurrent_discoveries_global
        ):
            raise ValueError(
                "mcp_max_concurrent_discoveries_per_user must be <= "
                "mcp_max_concurrent_discoveries_global"
            )
        if (
            self.mcp_user_enabled_connection_limit
            + self.mcp_required_official_server_limit
            > self.mcp_max_installations_per_user
        ):
            raise ValueError(
                "mcp_user_enabled_connection_limit + "
                "mcp_required_official_server_limit must be <= "
                "mcp_max_installations_per_user"
            )
        if (
            self.tool_approval_lease_heartbeat_seconds
            >= self.tool_approval_execution_lease_seconds
        ):
            raise ValueError(
                "tool_approval_lease_heartbeat_seconds must be < "
                "tool_approval_execution_lease_seconds"
            )
        return self

    def get_auth_users(self) -> dict[str, str]:
        """解析简单认证用户列表"""
        users = {}
        for user_pair in self.simple_auth_users.split(","):
            if ":" in user_pair:
                username, password = user_pair.split(":", 1)
                users[username.strip()] = password.strip()
        return users

    def get_admin_users(self) -> set[str]:
        """解析管理员用户名列表。"""
        return {
            username.strip()
            for username in self.auth_admin_users.split(",")
            if username.strip()
        }

    def get_ldap_urls(self) -> list[str]:
        """解析 LDAP 主备地址列表。"""
        return [url.strip() for url in self.ldap_urls.split(",") if url.strip()]


@lru_cache()
def get_settings() -> Settings:
    """获取配置（单例）"""
    settings = _apply_runtime_secret_policy(Settings())
    if not settings.simple_auth_users:
        _config_logger.warning(
            "SIMPLE_AUTH_USERS 未配置；auth_users 空表首次 bootstrap 不会创建 simple 用户。"
            "已有 auth_users 数据时可忽略。"
        )
    return settings
