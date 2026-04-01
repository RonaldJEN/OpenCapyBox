"""Model Registry 測試 — 驗證模型配置載入、查詢、校驗邏輯

替代已移除的 test_config.py（舊 CLI Config 模組測試）。
"""
import os
import pytest
import yaml
from pathlib import Path
from unittest.mock import patch

from src.api.model_registry import (
    ModelConfig,
    ModelRegistry,
    _resolve_env,
    VALID_PROVIDERS,
    VALID_REASONING_FORMATS,
)


# ============================================================
# _resolve_env 環境變數解析
# ============================================================


class TestResolveEnv:
    """測試 ${ENV_VAR} 解析"""

    def test_literal_value_passthrough(self):
        """普通字串直接返回"""
        assert _resolve_env("my-api-key") == "my-api-key"

    def test_env_var_resolved(self, monkeypatch):
        """${VAR} 格式正確解析"""
        monkeypatch.setenv("TEST_KEY_ABC", "resolved-value")
        assert _resolve_env("${TEST_KEY_ABC}") == "resolved-value"

    def test_env_var_with_spaces(self, monkeypatch):
        """帶空白的 ${VAR} 也能解析"""
        monkeypatch.setenv("TEST_KEY_XYZ", "val")
        assert _resolve_env("  ${TEST_KEY_XYZ}  ") == "val"

    def test_env_var_not_set_raises(self):
        """環境變數未設置時拋出 ValueError"""
        # 確保這個變數不存在
        os.environ.pop("NONEXISTENT_VAR_12345", None)
        with pytest.raises(ValueError, match="未設置"):
            _resolve_env("${NONEXISTENT_VAR_12345}")

    def test_partial_dollar_not_treated_as_env(self):
        """非完整 ${} 格式的美元符號不做解析"""
        assert _resolve_env("$NOT_ENV") == "$NOT_ENV"
        assert _resolve_env("prefix-${SUFFIX") == "prefix-${SUFFIX"


# ============================================================
# ModelConfig 數據結構
# ============================================================


class TestModelConfig:
    """測試 ModelConfig 校驗邏輯"""

    def _make_config(self, **overrides) -> ModelConfig:
        """創建帶預設值的測試用 ModelConfig"""
        defaults = {
            "id": "test-model",
            "display_name": "Test Model",
            "provider": "openai",
            "api_base": "https://api.example.com/v1",
            "api_key": "test-key",
            "model_name": "test-model-v1",
            "max_tokens": 8192,
        }
        defaults.update(overrides)
        return ModelConfig(**defaults)

    def test_valid_config(self):
        """正常配置不報錯"""
        cfg = self._make_config()
        assert cfg.id == "test-model"
        assert cfg.provider == "openai"

    def test_invalid_provider_raises(self):
        """無效 provider 拋出 ValueError"""
        with pytest.raises(ValueError, match="provider.*無效"):
            self._make_config(provider="invalid")

    def test_invalid_reasoning_format_raises(self):
        """無效 reasoning_format 拋出 ValueError"""
        with pytest.raises(ValueError, match="reasoning_format.*無效"):
            self._make_config(reasoning_format="unknown")

    def test_zero_max_tokens_raises(self):
        """max_tokens <= 0 拋出 ValueError"""
        with pytest.raises(ValueError, match="max_tokens"):
            self._make_config(max_tokens=0)

    def test_negative_max_tokens_raises(self):
        """負 max_tokens 拋出 ValueError"""
        with pytest.raises(ValueError, match="max_tokens"):
            self._make_config(max_tokens=-100)

    def test_supports_thinking_true(self):
        """reasoning_format != 'none' 時 supports_thinking 為 True"""
        cfg = self._make_config(reasoning_format="reasoning_content")
        assert cfg.supports_thinking is True

    def test_supports_thinking_false(self):
        """reasoning_format == 'none' 時 supports_thinking 為 False"""
        cfg = self._make_config(reasoning_format="none")
        assert cfg.supports_thinking is False

    def test_resolve_api_key_literal(self):
        """直接 API key 解析"""
        cfg = self._make_config(api_key="literal-key")
        assert cfg.resolve_api_key() == "literal-key"

    def test_resolve_api_key_env_var(self, monkeypatch):
        """${ENV_VAR} API key 解析"""
        monkeypatch.setenv("TEST_MODEL_KEY", "from-env")
        cfg = self._make_config(api_key="${TEST_MODEL_KEY}")
        assert cfg.resolve_api_key() == "from-env"

    def test_resolve_api_key_empty_raises(self, monkeypatch):
        """API key 為空時拋出 ValueError"""
        monkeypatch.setenv("EMPTY_KEY", "")
        cfg = self._make_config(api_key="${EMPTY_KEY}")
        with pytest.raises(ValueError, match="API key 為空"):
            cfg.resolve_api_key()

    def test_to_public_dict_no_sensitive_fields(self):
        """公開字典不含 api_key 和 api_base"""
        cfg = self._make_config(api_key="secret-key", api_base="https://secret.api")
        public = cfg.to_public_dict()
        assert "api_key" not in public
        assert "api_base" not in public
        assert public["id"] == "test-model"
        assert public["name"] == "Test Model"
        assert public["supports_image"] is False
        assert public["max_images"] == 0

    def test_supports_image_requires_positive_max_images(self):
        """supports_image=true 時 max_images 必須 > 0"""
        with pytest.raises(ValueError, match="max_images"):
            self._make_config(supports_image=True, max_images=0)

    def test_supports_video_requires_positive_max_videos(self):
        """supports_video=true 時 max_videos 必須 > 0"""
        with pytest.raises(ValueError, match="max_videos"):
            self._make_config(supports_video=True, max_videos=0)

    def test_all_valid_providers(self):
        """所有合法 provider 不報錯"""
        for provider in VALID_PROVIDERS:
            cfg = self._make_config(provider=provider)
            assert cfg.provider == provider

    def test_all_valid_reasoning_formats(self):
        """所有合法 reasoning_format 不報錯"""
        for fmt in VALID_REASONING_FORMATS:
            cfg = self._make_config(reasoning_format=fmt)
            assert cfg.reasoning_format == fmt

    def test_default_tags_empty_list(self):
        """tags 預設為空列表"""
        cfg = self._make_config()
        assert cfg.tags == []

    def test_custom_tags(self):
        """自定義 tags"""
        cfg = self._make_config(tags=["thinking", "coding"])
        assert cfg.tags == ["thinking", "coding"]


# ============================================================
# ModelRegistry YAML 載入
# ============================================================


class TestModelRegistryLoad:
    """測試 ModelRegistry 從 YAML 載入"""

    def _write_yaml(self, tmp_path: Path, data: dict) -> Path:
        """寫入測試 YAML 文件"""
        yaml_file = tmp_path / "models.yaml"
        yaml_file.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
        return yaml_file

    def _minimal_yaml(self, **model_overrides) -> dict:
        """最小可用 YAML 結構"""
        model = {
            "display_name": "Test",
            "provider": "openai",
            "api_base": "https://api.test.com/v1",
            "api_key": "test-key",
            "model_name": "test-v1",
            "max_tokens": 4096,
        }
        model.update(model_overrides)
        return {
            "default_model": "test-model",
            "models": {"test-model": model},
        }

    def test_load_minimal_yaml(self, tmp_path):
        """載入最小 YAML 成功"""
        path = self._write_yaml(tmp_path, self._minimal_yaml())
        registry = ModelRegistry.load(path)
        assert registry.get("test-model") is not None
        assert registry.default_model_id == "test-model"

    def test_load_multiple_models(self, tmp_path):
        """載入多個模型"""
        data = {
            "default_model": "model-a",
            "models": {
                "model-a": {
                    "display_name": "A",
                    "provider": "openai",
                    "api_base": "https://a.com/v1",
                    "api_key": "key-a",
                    "model_name": "a-v1",
                },
                "model-b": {
                    "display_name": "B",
                    "provider": "anthropic",
                    "api_base": "https://b.com",
                    "api_key": "key-b",
                    "model_name": "b-v1",
                },
            },
        }
        path = self._write_yaml(tmp_path, data)
        registry = ModelRegistry.load(path)
        assert len(registry.list_models(enabled_only=False)) == 2

    def test_load_missing_file_raises(self, tmp_path):
        """不存在的 YAML 拋出 FileNotFoundError"""
        with pytest.raises(FileNotFoundError):
            ModelRegistry.load(tmp_path / "nonexistent.yaml")

    def test_load_empty_yaml_raises(self, tmp_path):
        """空 YAML 拋出 ValueError"""
        path = self._write_yaml(tmp_path, {})
        with pytest.raises(ValueError, match="models"):
            ModelRegistry.load(path)

    def test_load_missing_models_key_raises(self, tmp_path):
        """缺少 'models' 鍵拋出 ValueError"""
        path = self._write_yaml(tmp_path, {"default_model": "x"})
        with pytest.raises(ValueError, match="models"):
            ModelRegistry.load(path)

    def test_invalid_default_model_raises(self, tmp_path):
        """default_model 指向不存在的模型時拋出 ValueError"""
        data = self._minimal_yaml()
        data["default_model"] = "nonexistent"
        path = self._write_yaml(tmp_path, data)
        with pytest.raises(ValueError, match="nonexistent"):
            ModelRegistry.load(path)

    def test_invalid_provider_in_yaml_raises(self, tmp_path):
        """YAML 中無效 provider 拋出 ValueError"""
        data = self._minimal_yaml(provider="bad-provider")
        path = self._write_yaml(tmp_path, data)
        with pytest.raises(ValueError, match="配置錯誤"):
            ModelRegistry.load(path)

    def test_env_var_api_key_in_yaml(self, tmp_path, monkeypatch):
        """YAML 中 ${ENV_VAR} API key 正確解析"""
        monkeypatch.setenv("MY_TEST_KEY", "resolved")
        data = self._minimal_yaml(api_key="${MY_TEST_KEY}")
        path = self._write_yaml(tmp_path, data)
        registry = ModelRegistry.load(path)
        cfg = registry.get("test-model")
        assert cfg.resolve_api_key() == "resolved"


# ============================================================
# ModelRegistry 查詢接口
# ============================================================


class TestModelRegistryQuery:
    """測試 Registry 查詢方法"""

    @pytest.fixture
    def registry(self):
        """創建測試 Registry"""
        models = {
            "enabled-model": ModelConfig(
                id="enabled-model",
                display_name="Enabled",
                provider="openai",
                api_base="https://a.com",
                api_key="key",
                model_name="v1",
                enabled=True,
                tags=["default"],
            ),
            "disabled-model": ModelConfig(
                id="disabled-model",
                display_name="Disabled",
                provider="openai",
                api_base="https://b.com",
                api_key="key",
                model_name="v2",
                enabled=False,
            ),
        }
        return ModelRegistry(models=models, default_model_id="enabled-model")

    def test_get_existing(self, registry):
        """獲取存在的模型"""
        cfg = registry.get("enabled-model")
        assert cfg is not None
        assert cfg.id == "enabled-model"

    def test_get_nonexistent_returns_none(self, registry):
        """獲取不存在的模型返回 None"""
        assert registry.get("no-such-model") is None

    def test_get_or_raise_existing(self, registry):
        """get_or_raise 正常返回"""
        cfg = registry.get_or_raise("enabled-model")
        assert cfg.id == "enabled-model"

    def test_get_or_raise_nonexistent(self, registry):
        """get_or_raise 不存在時拋出 ValueError"""
        with pytest.raises(ValueError, match="不存在"):
            registry.get_or_raise("no-such-model")

    def test_get_or_raise_disabled(self, registry):
        """get_or_raise 已停用時拋出 ValueError"""
        with pytest.raises(ValueError, match="已停用"):
            registry.get_or_raise("disabled-model")

    def test_get_default(self, registry):
        """獲取默認模型"""
        cfg = registry.get_default()
        assert cfg.id == "enabled-model"

    def test_get_default_not_configured(self):
        """未配置默認模型時拋出 ValueError"""
        registry = ModelRegistry(models={}, default_model_id="")
        with pytest.raises(ValueError, match="未配置"):
            registry.get_default()

    def test_list_models_enabled_only(self, registry):
        """僅列出啟用的模型"""
        models = registry.list_models(enabled_only=True)
        assert len(models) == 1
        assert models[0].id == "enabled-model"

    def test_list_models_all(self, registry):
        """列出所有模型"""
        models = registry.list_models(enabled_only=False)
        assert len(models) == 2

    def test_list_public_no_sensitive(self, registry):
        """公開列表不含敏感字段"""
        public_list = registry.list_public()
        assert len(public_list) == 1  # 只有 enabled 的
        for item in public_list:
            assert "api_key" not in item
            assert "api_base" not in item


# ============================================================
# validate_on_startup 自動停用邏輯
# ============================================================


class TestValidateOnStartup:
    """測試啟動校驗"""

    def test_missing_env_var_disables_model(self):
        """API key 環境變數缺失時自動停用模型"""
        # 確保變數不存在
        os.environ.pop("MISSING_KEY_FOR_TEST", None)
        models = {
            "model-x": ModelConfig(
                id="model-x",
                display_name="X",
                provider="openai",
                api_base="https://x.com",
                api_key="${MISSING_KEY_FOR_TEST}",
                model_name="x-v1",
                enabled=True,
            ),
        }
        registry = ModelRegistry(models=models, default_model_id="")
        registry.validate_on_startup()
        assert models["model-x"].enabled is False

    def test_literal_key_stays_enabled(self):
        """直接 API key 的模型保持啟用"""
        models = {
            "model-y": ModelConfig(
                id="model-y",
                display_name="Y",
                provider="openai",
                api_base="https://y.com",
                api_key="literal-key",
                model_name="y-v1",
                enabled=True,
            ),
        }
        registry = ModelRegistry(models=models, default_model_id="model-y")
        registry.validate_on_startup()
        assert models["model-y"].enabled is True

    def test_default_fallback_on_disabled(self):
        """默認模型停用時自動切換到可用模型"""
        os.environ.pop("MISSING_DEFAULT_KEY", None)
        models = {
            "default-bad": ModelConfig(
                id="default-bad",
                display_name="Bad",
                provider="openai",
                api_base="https://bad.com",
                api_key="${MISSING_DEFAULT_KEY}",
                model_name="bad-v1",
                enabled=True,
            ),
            "fallback-good": ModelConfig(
                id="fallback-good",
                display_name="Good",
                provider="openai",
                api_base="https://good.com",
                api_key="literal-key",
                model_name="good-v1",
                enabled=True,
            ),
        }
        registry = ModelRegistry(models=models, default_model_id="default-bad")
        registry.validate_on_startup()
        assert registry.default_model_id == "fallback-good"
