"""应用配置"""
import hashlib
import logging
import secrets
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

    # 简单认证（临时方案，格式：username:password,username2:password2）
    simple_auth_users: str = ""  # 必须在 .env 中配置 SIMPLE_AUTH_USERS

    # 认证配置（Bearer Token）
    auth_secret_key: str = ""
    auth_token_expire_minutes: int = 720

    # 数据库配置
    database_url: str = "sqlite:///./data/database/open_capy_box.db"

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

    # Agent 配置
    agent_max_steps: int = 100
    agent_token_limit: int = 200000

    # Agent 資源路徑配置（可通過 .env 覆蓋，預設相對於 src/agent/）
    skills_dir: str = ""          # 留空則自動定位到 src/agent/skills/

    # SSE 订阅配置
    sse_heartbeat_interval: int = 15  # 心跳间隔（秒）
    sse_subscribe_timeout: int = 300  # 订阅超时（秒，5分钟）

    # Embedding 配置（不填则向量检索降级为关键词搜索）
    embedding_api_key: str = ""
    embedding_api_base: str = "https://api.openai.com/v1"
    embedding_model: str = "text-embedding-3-small"
    embedding_chunk_size: int = 512  # 分块字符数

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # 忽略额外字段，避免验证错误

    def get_auth_users(self) -> dict[str, str]:
        """解析简单认证用户列表"""
        users = {}
        for user_pair in self.simple_auth_users.split(","):
            if ":" in user_pair:
                username, password = user_pair.split(":", 1)
                users[username.strip()] = password.strip()
        return users


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
            "SIMPLE_AUTH_USERS 未配置，无法登录。"
            "请在 .env 中设置 SIMPLE_AUTH_USERS=username:password"
        )
    return settings
