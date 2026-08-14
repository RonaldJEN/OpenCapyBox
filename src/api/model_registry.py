"""Model Registry — 模型配置管理

從 YAML 加載模型配置，消除所有硬編碼的 model.startswith() 分支邏輯。

核心職責：
  - 加載 models.yaml 並解析為 ModelConfig 對象
  - 解析 ${ENV_VAR} 環境變數引用
  - 提供 get / list / default / cron / subagent 查詢接口
  - 生成前端安全的模型列表（不含 api_key / api_base）

Usage:
    from src.api.model_registry import get_model_registry

    registry = get_model_registry()
    config = registry.get_or_raise("deepseek-chat")
    api_key = config.resolve_api_key()
"""

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ============================================================
# 環境變數解析
# ============================================================

_ENV_VAR_PATTERN = re.compile(r"^\$\{([^}]+)\}$")


def _resolve_env(value: str) -> str:
    """解析環境變數引用

    支援格式：
      - "${VAR_NAME}" → os.environ["VAR_NAME"]
      - "literal-value" → 原樣返回

    Raises:
        ValueError: 環境變數未設置
    """
    match = _ENV_VAR_PATTERN.match(value.strip())
    if match:
        env_name = match.group(1)
        env_value = os.environ.get(env_name)
        if env_value is None:
            raise ValueError(
                f"環境變數 {env_name} 未設置。"
                f"請在 .env 文件中添加: {env_name}=your-api-key"
            )
        return env_value
    return value


# ============================================================
# ModelConfig 數據結構
# ============================================================

VALID_PROVIDERS = {"anthropic", "openai"}
VALID_THINKING_MODES = {"provider_default", "enabled", "disabled"}
VALID_THINKING_WIRE_FORMATS = {"none", "enable_thinking", "thinking_object"}
VALID_REASONING_FORMATS = {
    "none",                # 不支援思考
    "reasoning_content",   # OpenAI response.reasoning_content（GLM/Qwen/DeepSeek）
    "reasoning_details",   # OpenAI response.reasoning_details（MiniMax）
    "anthropic_thinking",  # Anthropic 原生 thinking content blocks
}


@dataclass
class EmbeddingModelConfig:
    """Embedding 模型配置

    Attributes:
        id: Registry 唯一標識（如 "text-embedding-v3"）
        display_name: 顯示名稱
        api_base: 完整 API 地址
        api_key: API 密鑰值或 ${ENV_VAR} 引用
        model_name: 發送給 API 的實際模型名稱
        dimensions: 向量維度
        enabled: 是否啟用
    """

    id: str
    display_name: str
    api_base: str
    api_key: str
    model_name: str
    dimensions: int = 1024
    enabled: bool = True

    def resolve_api_key(self) -> str:
        """解析 API key（從環境變數或直接值）"""
        resolved = _resolve_env(self.api_key)
        if not resolved.strip():
            raise ValueError(
                f"Embedding 模型 '{self.id}' 的 API key 為空。"
            )
        return resolved


@dataclass
class ModelConfig:
    """單個模型的完整配置

    包含創建 LLMClient 所需的全部參數，
    消除 client 代碼中所有 model.startswith() 分支。

    Attributes:
        id: Registry 唯一標識（如 "deepseek-chat"）
        display_name: 前端展示名稱
        provider: SDK 協議（"anthropic" | "openai"）
        api_base: 完整 API 地址（含後綴，直接傳給 SDK）
        api_key: API 密鑰值或 ${ENV_VAR} 引用
        model_name: 發送給 API 的實際模型名稱
        max_tokens: 最大輸出 token 數
        reasoning_format: 思考過程解析格式
        reasoning_split: 是否發送 extra_body.reasoning_split = true（僅 OpenAI）
        thinking_mode: 思考開關三態（provider_default/enabled/disabled，僅 OpenAI）
        thinking_wire_format: 思考开关的请求协议（不发送/enable_thinking/thinking.type）
        enable_thinking: 舊版相容開關；thinking_mode=provider_default 時 true 代表 enabled
        reasoning_effort: 發送給 OpenAI 協議的推理強度（如 low/medium/high）
        enabled: 是否對前端可見
        tags: 分類標籤
    """

    id: str
    display_name: str
    provider: str
    api_base: str
    api_key: str  # 原始值，可能是 ${ENV_VAR}
    model_name: str
    max_tokens: int = 16384          # 單次輸出上限（output tokens）
    context_window: int = 128000     # 模型總上下文窗口大小（input + output tokens）
    auto_compact_token_limit: int | None = None
    tool_output_truncation_bytes: int = 10000
    reasoning_format: str = "none"
    reasoning_split: bool = False
    enable_thinking: bool = False
    thinking_mode: str = "provider_default"
    thinking_wire_format: str = "none"
    reasoning_effort: str | None = None
    supported_reasoning_efforts: list[str] = field(default_factory=list)
    supports_image: bool = False
    max_images: int = 0
    supports_video: bool = False
    max_videos: int = 0
    enabled: bool = True
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        """校驗配置值"""
        if self.provider not in VALID_PROVIDERS:
            raise ValueError(
                f"模型 '{self.id}' 的 provider '{self.provider}' 無效，"
                f"可選: {VALID_PROVIDERS}"
            )
        if self.reasoning_format not in VALID_REASONING_FORMATS:
            raise ValueError(
                f"模型 '{self.id}' 的 reasoning_format '{self.reasoning_format}' 無效，"
                f"可選: {VALID_REASONING_FORMATS}"
            )
        if self.thinking_mode not in VALID_THINKING_MODES:
            raise ValueError(
                f"模型 '{self.id}' 的 thinking_mode '{self.thinking_mode}' 無效，"
                f"可選: {VALID_THINKING_MODES}"
            )
        if self.thinking_wire_format not in VALID_THINKING_WIRE_FORMATS:
            raise ValueError(
                f"模型 '{self.id}' 的 thinking_wire_format '{self.thinking_wire_format}' 無效，"
                f"可選: {VALID_THINKING_WIRE_FORMATS}"
            )
        if self.thinking_mode != "provider_default" and self.provider != "openai":
            raise ValueError(
                f"模型 '{self.id}' 的 thinking_mode 僅支援 provider=openai"
            )
        if self.reasoning_effort is not None:
            # YAML 1.1 turns bare on/off/yes/no into booleans; reject instead of
            # silently shipping "True" to the provider.
            if not isinstance(self.reasoning_effort, str):
                raise ValueError(
                    f"模型 '{self.id}' 的 reasoning_effort 必須是字串，"
                    f"got {self.reasoning_effort!r}；YAML 中的 on/off 需加引號"
                )
            self.reasoning_effort = self.reasoning_effort.strip()
            if not self.reasoning_effort:
                self.reasoning_effort = None
            elif self.reasoning_effort in {"off", "on"}:
                raise ValueError(
                    f"模型 '{self.id}' 的 reasoning_effort 不能使用保留等级 "
                    f"'{self.reasoning_effort}'；請改用 thinking_mode 表达开关"
                )
        normalized_efforts: list[str] = []
        for effort in self.supported_reasoning_efforts:
            if not isinstance(effort, str):
                raise ValueError(
                    f"模型 '{self.id}' 的 supported_reasoning_efforts 必須全部是字串，"
                    f"got {effort!r}；YAML 中的 on/off 需加引號"
                )
            value = effort.strip()
            if not value:
                raise ValueError(
                    f"模型 '{self.id}' 的 supported_reasoning_efforts 不能包含空等级"
                )
            if value not in normalized_efforts:
                normalized_efforts.append(value)
        self.supported_reasoning_efforts = normalized_efforts
        if self.reasoning_effort is not None and self.provider != "openai":
            raise ValueError(
                f"模型 '{self.id}' 的 reasoning_effort 僅支援 provider=openai"
            )
        if self.supported_reasoning_efforts and self.provider != "openai":
            raise ValueError(
                f"模型 '{self.id}' 的 supported_reasoning_efforts 僅支援 provider=openai"
            )
        if (
            self.reasoning_effort is not None
            and self.supported_reasoning_efforts
            and self.reasoning_effort not in self.supported_reasoning_efforts
        ):
            raise ValueError(
                f"模型 '{self.id}' 的 reasoning_effort 必須包含在 supported_reasoning_efforts 中"
            )
        default_level = self.default_reasoning_level
        if (
            default_level in {"off", "on"}
            and self.supported_reasoning_efforts
            and default_level not in self.supported_reasoning_efforts
        ):
            raise ValueError(
                f"模型 '{self.id}' 的默认推理等级 '{default_level}' "
                "必须包含在 supported_reasoning_efforts 中"
            )
        if self.thinking_mode == "disabled" and self.reasoning_effort is not None:
            raise ValueError(
                f"模型 '{self.id}' 關閉思考時不能設定 reasoning_effort"
            )
        if self.thinking_wire_format == "none":
            # A bare reasoning_effort still ships as a top-level field, but the
            # on/off switch has no encoding at all under this wire format.
            unencodable = [
                level for level in self.supported_reasoning_efforts
                if level in {"off", "on"}
            ]
            if unencodable:
                raise ValueError(
                    f"模型 '{self.id}' 的 thinking_wire_format=none 無法編碼 "
                    f"{unencodable} 等級，請改用 enable_thinking 或 thinking_object"
                )
            # Legacy enable_thinking resolves to the same on/off switch, so it
            # must not slip through as a catalog default the wire cannot send.
            effective_mode = self.effective_thinking_mode
            if effective_mode in {"enabled", "disabled"} and self.reasoning_effort is None:
                raise ValueError(
                    f"模型 '{self.id}' 的 thinking_wire_format=none 無法編碼 "
                    f"thinking_mode='{effective_mode}'，請改用 enable_thinking "
                    "或 thinking_object"
                )
        if self.max_tokens <= 0:
            raise ValueError(
                f"模型 '{self.id}' 的 max_tokens 必須 > 0，got {self.max_tokens}"
            )
        if self.context_window <= 0:
            raise ValueError(
                f"模型 '{self.id}' 的 context_window 必須 > 0，got {self.context_window}"
            )
        if self.max_tokens >= self.context_window:
            raise ValueError(
                f"模型 '{self.id}' 的 context_window ({self.context_window}) "
                f"必須大於 max_tokens ({self.max_tokens})"
            )
        if self.auto_compact_token_limit is not None and self.auto_compact_token_limit <= 0:
            raise ValueError("auto_compact_token_limit must be > 0 when configured")
        if self.tool_output_truncation_bytes <= 0:
            raise ValueError("tool_output_truncation_bytes must be > 0")
        if self.max_images < 0:
            raise ValueError(
                f"模型 '{self.id}' 的 max_images 不能為負數，got {self.max_images}"
            )
        if self.supports_image and self.max_images <= 0:
            raise ValueError(
                f"模型 '{self.id}' 啟用 supports_image 時，max_images 必須 > 0"
            )
        if self.max_videos < 0:
            raise ValueError(
                f"模型 '{self.id}' 的 max_videos 不能為負數，got {self.max_videos}"
            )
        if self.supports_video and self.max_videos <= 0:
            raise ValueError(
                f"模型 '{self.id}' 啟用 supports_video 時，max_videos 必須 > 0"
            )

    def resolve_api_key(self) -> str:
        """解析 API key（從環境變數或直接值）

        Returns:
            實際的 API key 字串

        Raises:
            ValueError: 環境變數未設置或為空
        """
        resolved = _resolve_env(self.api_key)
        if not resolved.strip():
            raise ValueError(
                f"模型 '{self.id}' 的 API key 為空。"
                f"請在 .env 文件中設置對應的環境變數。"
            )
        return resolved

    def compute_token_limit(self) -> int:
        """Auto-compaction limit: 80% of the model's usable input budget."""
        default_limit = max(int((self.context_window - self.max_tokens) * 0.8), 1)
        if self.auto_compact_token_limit is None:
            return default_limit
        return min(int(self.auto_compact_token_limit), default_limit)

    @property
    def effective_thinking_mode(self) -> str:
        """Resolve the tri-state mode while preserving legacy enable_thinking configs."""
        if self.thinking_mode != "provider_default":
            return self.thinking_mode
        if self.enable_thinking:
            return "enabled"
        return "provider_default"

    @property
    def default_reasoning_level(self) -> str | None:
        """Catalog-level default; transport switches remain an internal detail."""
        if self.reasoning_effort is not None:
            return self.reasoning_effort
        if self.effective_thinking_mode == "enabled":
            return "on"
        if self.effective_thinking_mode == "disabled":
            return "off"
        return None

    @property
    def supports_thinking(self) -> bool:
        """当前模型变体是否实际启用了可展示的思考过程。"""
        if self.effective_thinking_mode == "disabled":
            return False
        if self.reasoning_format == "none":
            return False
        if self.provider != "openai":
            return True
        return (
            bool(self.reasoning_split)
            or self.effective_thinking_mode == "enabled"
            or any(level != "off" for level in self.supported_reasoning_efforts)
        )

    @property
    def supports_reasoning_control(self) -> bool:
        """Whether the OpenAI-compatible variant can accept per-turn controls."""
        return self.provider == "openai" and bool(self.supported_reasoning_efforts)

    def to_public_dict(self) -> dict:
        """轉換為前端安全的字典（不含 api_key、api_base）"""
        return {
            "id": self.id,
            "name": self.display_name,
            "provider": self.provider,
            "supports_thinking": self.supports_thinking,
            "supports_reasoning_control": self.supports_reasoning_control,
            "thinking_mode": self.effective_thinking_mode,
            "thinking_wire_format": self.thinking_wire_format,
            "reasoning_effort": self.reasoning_effort,
            "default_reasoning_level": self.default_reasoning_level,
            "supported_reasoning_efforts": list(self.supported_reasoning_efforts),
            "supports_image": self.supports_image,
            "max_images": self.max_images,
            "supports_video": self.supports_video,
            "max_videos": self.max_videos,
            "max_tokens": self.max_tokens,
            "context_window": self.context_window,
            "auto_compact_token_limit": self.auto_compact_token_limit,
            "tool_output_truncation_bytes": self.tool_output_truncation_bytes,
            "enabled": self.enabled,
            "tags": self.tags,
        }


def model_config_from_yaml_entry(model_id: str, cfg: dict) -> ModelConfig:
    """Build a validated ModelConfig from one raw models.yaml entry.

    Shared by registry loading and DB seeding so the seed path cannot persist
    values the runtime registry would have rejected.
    """
    return ModelConfig(
        id=str(model_id),
        display_name=cfg.get("display_name", str(model_id)),
        provider=cfg["provider"],
        api_base=cfg["api_base"],
        api_key=cfg.get("api_key", "${LLM_API_KEY}"),
        model_name=cfg.get("model_name", str(model_id)),
        max_tokens=cfg.get("max_tokens", 16384),
        context_window=cfg.get("context_window", 128000),
        auto_compact_token_limit=cfg.get("auto_compact_token_limit"),
        tool_output_truncation_bytes=cfg.get("tool_output_truncation_bytes", 10000),
        reasoning_format=cfg.get("reasoning_format", "none"),
        reasoning_split=cfg.get("reasoning_split", False),
        enable_thinking=cfg.get("enable_thinking", False),
        thinking_mode=cfg.get("thinking_mode", "provider_default"),
        thinking_wire_format=cfg.get(
            "thinking_wire_format",
            "enable_thinking" if cfg.get("provider") == "openai" else "none",
        ),
        reasoning_effort=cfg.get("reasoning_effort"),
        supported_reasoning_efforts=cfg.get("supported_reasoning_efforts", []),
        supports_image=cfg.get("supports_image", False),
        max_images=cfg.get("max_images", 0),
        supports_video=cfg.get("supports_video", False),
        max_videos=cfg.get("max_videos", 0),
        enabled=cfg.get("enabled", True),
        tags=cfg.get("tags", []),
    )


# ============================================================
# ModelRegistry
# ============================================================


class ModelRegistry:
    """模型配置註冊表

    從 YAML 文件加載所有模型配置，提供查詢接口。

    Usage:
        registry = ModelRegistry.load()

        config = registry.get("deepseek-chat")
        models = registry.list_models(enabled_only=True)
        default = registry.get_default()
    """

    def __init__(
        self,
        models: dict[str, ModelConfig],
        default_model_id: str,
        embedding_models: dict[str, EmbeddingModelConfig] | None = None,
        default_embedding_model_id: str = "",
        cron_default_model_id: str = "",
        subagent_default_model_id: str = "",
        source: str = "yaml",
    ):
        self._models = models
        self._default_model_id = default_model_id
        self._embedding_models = embedding_models or {}
        self._default_embedding_model_id = default_embedding_model_id
        self._cron_default_model_id = cron_default_model_id or default_model_id
        self._subagent_default_model_id = subagent_default_model_id or default_model_id
        self.source = source

    @classmethod
    def load(cls, yaml_path: str | Path | None = None) -> "ModelRegistry":
        """Load model registry.

        Explicit ``yaml_path`` keeps the historical YAML behavior for tests and
        tooling. Runtime calls without a path use the DB catalog seeded from
        models.yaml during startup. A DB failure is surfaced instead of silently
        switching the runtime source of truth.
        """
        if yaml_path is not None:
            return cls.load_yaml(yaml_path)

        from src.api.models.database import SessionLocal
        from src.api.services.model_access_service import load_registry_from_db

        with SessionLocal() as db:
            registry = load_registry_from_db(db)
            if registry.list_models(enabled_only=False):
                return registry
        logger.warning("DB 模型目录为空，使用 models.yaml 初始化目录")
        return cls.load_yaml()

    @classmethod
    def load_yaml(cls, yaml_path: str | Path | None = None) -> "ModelRegistry":
        """從 YAML 文件加載模型配置

        Args:
            yaml_path: YAML 文件路徑。不指定則自動搜索。

        Returns:
            ModelRegistry 實例

        Raises:
            FileNotFoundError: 找不到 YAML 文件
            ValueError: YAML 格式錯誤或模型配置無效
        """
        if yaml_path is None:
            yaml_path = cls._find_yaml()

        yaml_path = Path(yaml_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"模型配置文件不存在: {yaml_path}")

        with open(yaml_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not raw or "models" not in raw:
            raise ValueError(f"YAML 格式錯誤：缺少 'models' 字段 ({yaml_path})")

        default_model_id = raw.get("default_model", "")
        cron_default_model_id = raw.get("cron_default_model", "") or default_model_id
        subagent_default_model_id = raw.get("subagent_default_model", "") or default_model_id
        models: dict[str, ModelConfig] = {}

        for model_id, cfg in raw["models"].items():
            try:
                models[model_id] = model_config_from_yaml_entry(model_id, cfg)
            except (KeyError, ValueError) as e:
                raise ValueError(f"模型 '{model_id}' 配置錯誤: {e}") from e

        # 校驗 default_model 存在
        if default_model_id and default_model_id not in models:
            available = list(models.keys())
            raise ValueError(
                f"default_model '{default_model_id}' 不在 models 中。"
                f"可選: {available}"
            )

        # 校驗 cron_default_model 存在（可選字段）
        if cron_default_model_id and cron_default_model_id not in models:
            available = list(models.keys())
            raise ValueError(
                f"cron_default_model '{cron_default_model_id}' 不在 models 中。"
                f"可選: {available}"
            )

        # 校驗 subagent_default_model 存在（可選字段）
        if subagent_default_model_id and subagent_default_model_id not in models:
            available = list(models.keys())
            raise ValueError(
                f"subagent_default_model '{subagent_default_model_id}' 不在 models 中。"
                f"可選: {available}"
            )

        # ---- 加載 Embedding 模型 ----
        embedding_models: dict[str, EmbeddingModelConfig] = {}
        default_embedding_model_id = raw.get("default_embedding_model", "")

        for emb_id, emb_cfg in raw.get("embedding_models", {}).items():
            try:
                embedding_models[emb_id] = EmbeddingModelConfig(
                    id=emb_id,
                    display_name=emb_cfg.get("display_name", emb_id),
                    api_base=emb_cfg["api_base"],
                    api_key=emb_cfg.get("api_key", "${LLM_API_KEY}"),
                    model_name=emb_cfg.get("model_name", emb_id),
                    dimensions=emb_cfg.get("dimensions", 1024),
                    enabled=emb_cfg.get("enabled", True),
                )
            except (KeyError, ValueError) as e:
                logger.warning("Embedding 模型 '%s' 配置錯誤: %s", emb_id, e)

        logger.info(
            "已加載 %d 個模型配置（啟用: %d，默認: %s），%d 個 Embedding 模型",
            len(models),
            sum(1 for m in models.values() if m.enabled),
            default_model_id or "未設置",
            len(embedding_models),
        )

        registry = cls(
            models=models,
            default_model_id=default_model_id,
            embedding_models=embedding_models,
            default_embedding_model_id=default_embedding_model_id,
            cron_default_model_id=cron_default_model_id,
            subagent_default_model_id=subagent_default_model_id,
            source="yaml",
        )
        registry.validate_on_startup()
        return registry

    @staticmethod
    def _find_yaml() -> Path:
        """自動查找 models.yaml"""
        # 1. 環境變數指定
        env_path = os.environ.get("MODEL_REGISTRY_PATH")
        if env_path:
            return Path(env_path)

        # 2. 項目根目錄（支援多種啟動位置）
        candidates = [
            Path.cwd() / "models.yaml",
            # src/api/model_registry.py → ../../models.yaml
            Path(__file__).parent.parent.parent / "models.yaml",
        ]

        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.exists():
                return resolved

        raise FileNotFoundError(
            "找不到 models.yaml。請確保文件在項目根目錄，"
            "或設置環境變數 MODEL_REGISTRY_PATH"
        )

    # ---- 查詢接口 ----

    def get(self, model_id: str) -> Optional[ModelConfig]:
        """獲取模型配置（不存在返回 None）"""
        return self._models.get(model_id)

    def get_or_raise(self, model_id: str) -> ModelConfig:
        """獲取模型配置，不存在或停用則拋出異常

        Raises:
            ValueError: 模型不存在或已停用
        """
        config = self._models.get(model_id)
        if config is None:
            available = [m.id for m in self._models.values() if m.enabled]
            raise ValueError(
                f"模型 '{model_id}' 不存在。可用模型: {available}"
            )
        if not config.enabled:
            raise ValueError(f"模型 '{model_id}' 已停用")
        return config

    def get_default(self) -> ModelConfig:
        """獲取默認模型配置

        Raises:
            ValueError: 默認模型未配置或不存在
        """
        if not self._default_model_id:
            raise ValueError("未配置 default_model")
        config = self._models.get(self._default_model_id)
        if config is None:
            raise ValueError(
                f"默認模型 '{self._default_model_id}' 在配置中不存在"
            )
        return config

    def get_cron_default(self) -> ModelConfig:
        """獲取 Cron 任務默認模型（加載時已確定，無運行時 fallback）"""
        config = self._models.get(self._cron_default_model_id)
        if config is None:
            raise ValueError(
                f"cron_default_model '{self._cron_default_model_id}' 在配置中不存在"
            )
        return config

    def get_subagent_default(self) -> ModelConfig:
        """獲取 Subagent 默認模型（加載時已確定，無運行時 fallback）"""
        config = self._models.get(self._subagent_default_model_id)
        if config is None:
            raise ValueError(
                f"subagent_default_model '{self._subagent_default_model_id}' 在配置中不存在"
            )
        return config

    def validate_on_startup(self) -> None:
        """啟動時校驗所有已啟用模型的 API Key 可用性

        環境變數缺失的模型會被自動停用，並記錄 WARNING 日誌。
        這樣可以在啟動時就發現問題，而不是等到用戶使用時才 500。
        """
        disabled_count = 0
        for model in self._models.values():
            if not model.enabled:
                continue
            try:
                model.resolve_api_key()
            except ValueError as e:
                logger.warning(
                    "模型 '%s' API key 未就緒: %s — 已自動停用",
                    model.id, e,
                )
                model.enabled = False
                disabled_count += 1

        if disabled_count:
            logger.warning(
                "共 %d 個模型因 API key 缺失被自動停用。"
                "請檢查 .env 中的環境變數配置。",
                disabled_count,
            )

        # 驗證 Embedding 模型
        self.validate_embedding_models()

        # 驗證默認模型仍可用
        if self._default_model_id:
            default = self._models.get(self._default_model_id)
            if default and not default.enabled:
                # 嘗試 fallback 到第一個可用模型
                fallback = next(
                    (m for m in self._models.values() if m.enabled), None
                )
                if fallback:
                    logger.warning(
                        "默認模型 '%s' 已停用，自動切換到 '%s'",
                        self._default_model_id, fallback.id,
                    )
                    self._default_model_id = fallback.id
                else:
                    logger.error("所有模型均不可用，請檢查 .env 配置")

    def list_models(self, enabled_only: bool = True) -> list[ModelConfig]:
        """列出模型配置"""
        models = list(self._models.values())
        if enabled_only:
            models = [m for m in models if m.enabled]
        return models

    def list_public(self) -> list[dict]:
        """列出前端安全的模型信息（不含敏感字段）"""
        return [m.to_public_dict() for m in self.list_models(enabled_only=True)]

    @property
    def default_model_id(self) -> str:
        return self._default_model_id

    @property
    def subagent_default_model_id(self) -> str:
        return self._subagent_default_model_id

    # ---- Embedding 模型查詢 ----

    def get_embedding_model(self, model_id: str | None = None) -> Optional[EmbeddingModelConfig]:
        """獲取 Embedding 模型配置

        Args:
            model_id: 模型 ID，為 None 時返回默認模型

        Returns:
            EmbeddingModelConfig 或 None（未配置/不可用）
        """
        if model_id is None:
            model_id = self._default_embedding_model_id
        if not model_id:
            return None
        config = self._embedding_models.get(model_id)
        if config and not config.enabled:
            return None
        return config

    def validate_embedding_models(self) -> None:
        """啟動時校驗 Embedding 模型的 API Key 可用性"""
        for emb in self._embedding_models.values():
            if not emb.enabled:
                continue
            try:
                emb.resolve_api_key()
            except ValueError as e:
                logger.warning(
                    "Embedding 模型 '%s' API key 未就緒: %s — 已自動停用",
                    emb.id, e,
                )
                emb.enabled = False


# ============================================================
# 全局單例
# ============================================================

_registry: Optional[ModelRegistry] = None
_registry_lock = threading.Lock()


def _ensure_dotenv_loaded() -> None:
    """確保 .env 已加載到 os.environ（僅首次生效）

    pydantic_settings.BaseSettings 讀 .env 時只填充自身字段，
    不會把值放入 os.environ。model_registry 需要通過
    os.environ.get() 解析 ${ENV_VAR}，所以必須顯式 load_dotenv。
    """
    # load_dotenv 自身有冪等保護（override=False 時不覆蓋已有值）
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).parent.parent.parent / ".env",
    ]
    for candidate in candidates:
        if candidate.resolve().exists():
            load_dotenv(candidate.resolve(), override=False)
            return


def get_model_registry() -> ModelRegistry:
    """獲取全局 ModelRegistry 單例（首次調用自動加載，線程安全）"""
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        # Double-checked locking
        if _registry is None:
            _ensure_dotenv_loaded()
            _registry = ModelRegistry.load()
    return _registry


def reload_model_registry() -> ModelRegistry:
    """重新加載模型配置（hot-reload）"""
    global _registry
    with _registry_lock:
        _ensure_dotenv_loaded()
        _registry = ModelRegistry.load()
    return _registry
