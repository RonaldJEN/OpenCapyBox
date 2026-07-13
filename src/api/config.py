"""应用配置"""
import hashlib
import logging
import secrets
from pydantic import field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List

_config_logger = logging.getLogger(__name__)


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
    settings = Settings()
    if not settings.auth_secret_key:
        # 未配置时使用由 app_name 派生的确定性密钥，确保多 Worker / 重启后一致。
        # ⚠️ 生产环境务必在 .env 中配置 AUTH_SECRET_KEY
        derived = hashlib.sha256(
            f"{settings.app_name}:agentskills-default-dev-key".encode()
        ).hexdigest()
        settings.auth_secret_key = derived
        _config_logger.warning(
            "AUTH_SECRET_KEY 未配置，使用由 APP_NAME 派生的默认密钥。"
            "生产环境请在 .env 中设置 AUTH_SECRET_KEY=<随机字符串>"
        )
    if not settings.simple_auth_users:
        _config_logger.warning(
            "SIMPLE_AUTH_USERS 未配置；auth_users 空表首次 bootstrap 不会创建 simple 用户。"
            "已有 auth_users 数据时可忽略。"
        )
    return settings
