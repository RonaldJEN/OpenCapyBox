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
from src.api.models.llm_model import LLMModel
from src.api.services.model_access_service import (
    db_model_to_config,
    seed_model_catalog_from_yaml_if_empty,
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

    def test_reasoning_effort_is_normalized(self):
        cfg = self._make_config(reasoning_effort="  high  ")
        assert cfg.reasoning_effort == "high"

    @pytest.mark.parametrize("reserved", ["off", "on"])
    def test_reasoning_effort_rejects_switch_level_aliases(self, reserved):
        with pytest.raises(ValueError, match="reasoning_effort.*保留等级"):
            self._make_config(reasoning_effort=reserved)

    def test_supported_reasoning_efforts_are_normalized(self):
        cfg = self._make_config(
            reasoning_effort="high",
            supported_reasoning_efforts=[" high ", "max", "high"],
        )
        assert cfg.supported_reasoning_efforts == ["high", "max"]

    def test_supported_reasoning_efforts_reject_blank_entries(self):
        with pytest.raises(ValueError, match="不能包含空等级"):
            self._make_config(supported_reasoning_efforts=["high", "   "])

    def test_default_effort_must_be_supported(self):
        with pytest.raises(ValueError, match="supported_reasoning_efforts"):
            self._make_config(
                reasoning_effort="medium",
                supported_reasoning_efforts=["high", "max"],
            )

    def test_boolean_default_level_must_be_supported(self):
        with pytest.raises(ValueError, match="默认推理等级 'off'"):
            self._make_config(
                thinking_mode="disabled",
                thinking_wire_format="enable_thinking",
                supported_reasoning_efforts=["on"],
            )

    def test_yaml_boolean_reasoning_effort_is_rejected(self):
        # YAML 1.1 turns bare `on` into True; never ship it as "True".
        with pytest.raises(ValueError, match="reasoning_effort 必須是字串"):
            self._make_config(reasoning_effort=True)

    def test_yaml_boolean_supported_effort_entry_is_rejected(self):
        with pytest.raises(ValueError, match="supported_reasoning_efforts 必須全部是字串"):
            self._make_config(supported_reasoning_efforts=[False, "high"])

    def test_wire_none_allows_graded_effort_levels(self):
        cfg = self._make_config(
            thinking_wire_format="none",
            reasoning_effort="high",
            supported_reasoning_efforts=["high", "max"],
        )
        assert cfg.supports_reasoning_control is True
        assert cfg.default_reasoning_level == "high"

    def test_wire_none_rejects_switch_only_levels(self):
        with pytest.raises(ValueError, match="thinking_wire_format=none 無法編碼"):
            self._make_config(
                thinking_wire_format="none",
                supported_reasoning_efforts=["off", "high"],
            )

    def test_wire_none_rejects_explicit_switch_default(self):
        with pytest.raises(ValueError, match="thinking_wire_format=none 無法編碼"):
            self._make_config(thinking_wire_format="none", thinking_mode="enabled")

    def test_wire_none_rejects_legacy_enable_thinking_default(self):
        # 界面会把它显示成 On，但 wire=none 永远发不出这个开关。
        with pytest.raises(ValueError, match="thinking_wire_format=none 無法編碼"):
            self._make_config(
                thinking_wire_format="none",
                thinking_mode="provider_default",
                enable_thinking=True,
            )

    def test_blank_reasoning_effort_is_provider_default(self):
        cfg = self._make_config(reasoning_effort="   ")
        assert cfg.reasoning_effort is None

    def test_anthropic_rejects_openai_reasoning_effort(self):
        with pytest.raises(ValueError, match="reasoning_effort.*provider=openai"):
            self._make_config(provider="anthropic", reasoning_effort="high")

    def test_invalid_thinking_mode_raises(self):
        with pytest.raises(ValueError, match="thinking_mode.*無效"):
            self._make_config(thinking_mode="sometimes")

    def test_invalid_thinking_wire_format_raises(self):
        with pytest.raises(ValueError, match="thinking_wire_format.*無效"):
            self._make_config(thinking_wire_format="vendor_magic")

    def test_disabled_thinking_rejects_reasoning_effort(self):
        with pytest.raises(ValueError, match="關閉思考.*reasoning_effort"):
            self._make_config(
                thinking_mode="disabled",
                thinking_wire_format="enable_thinking",
                reasoning_effort="high",
            )

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

    def test_zero_tool_output_truncation_bytes_raises(self):
        with pytest.raises(ValueError, match="tool_output_truncation_bytes must be > 0"):
            self._make_config(tool_output_truncation_bytes=0)

    def test_db_model_zero_tool_output_truncation_bytes_is_not_defaulted(self):
        model = LLMModel(
            model_id="zero-truncation",
            display_name="Zero Truncation",
            provider="openai",
            api_base="https://api.example.com/v1",
            api_key="test-key",
            model_name="zero-truncation",
            max_tokens=8192,
            context_window=128000,
            tool_output_truncation_bytes=0,
        )

        with pytest.raises(ValueError, match="tool_output_truncation_bytes must be > 0"):
            db_model_to_config(model)

    def test_db_model_missing_tool_output_truncation_bytes_uses_default(self):
        model = LLMModel(
            model_id="missing-truncation",
            display_name="Missing Truncation",
            provider="openai",
            api_base="https://api.example.com/v1",
            api_key="test-key",
            model_name="missing-truncation",
            max_tokens=8192,
            context_window=128000,
        )
        model.tool_output_truncation_bytes = None

        assert db_model_to_config(model).tool_output_truncation_bytes == 42667

    def test_supports_thinking_true(self):
        """OpenAI 变体显式启用 reasoning split 时公开支持思考。"""
        cfg = self._make_config(reasoning_format="reasoning_content", reasoning_split=True)
        assert cfg.supports_thinking is True

    def test_supports_thinking_false(self):
        """reasoning_format == 'none' 時 supports_thinking 為 False"""
        cfg = self._make_config(reasoning_format="none")
        assert cfg.supports_thinking is False

    def test_openai_no_think_variant_is_false_even_with_legacy_reasoning_format(self):
        """存量 DB 尚未同步 YAML 时，关闭两项开关的 OpenAI 变体仍不得宣称支持思考。"""
        cfg = self._make_config(
            reasoning_format="reasoning_content",
            reasoning_split=False,
            enable_thinking=False,
        )
        assert cfg.supports_thinking is False

    def test_openai_enable_thinking_does_not_require_reasoning_split(self):
        """DeepSeek native returns reasoning_content without DashScope reasoning_split."""
        cfg = self._make_config(
            reasoning_format="reasoning_content",
            reasoning_split=False,
            enable_thinking=True,
            thinking_wire_format="enable_thinking",
        )
        assert cfg.supports_thinking is True

    def test_explicit_disabled_overrides_legacy_enable_thinking(self):
        cfg = self._make_config(
            reasoning_format="reasoning_content",
            reasoning_split=True,
            enable_thinking=True,
            thinking_mode="disabled",
            thinking_wire_format="enable_thinking",
            supported_reasoning_efforts=["off", "on"],
        )
        assert cfg.effective_thinking_mode == "disabled"
        assert cfg.supports_thinking is False
        assert cfg.supports_reasoning_control is True

    def test_reasoning_format_does_not_invent_selectable_levels(self):
        cfg = self._make_config(
            reasoning_format="reasoning_content",
            reasoning_split=True,
            enable_thinking=True,
            thinking_wire_format="enable_thinking",
        )
        assert cfg.supports_reasoning_control is False

    def test_effort_catalog_declares_control_without_visible_thinking(self):
        cfg = self._make_config(
            reasoning_format="none",
            reasoning_split=False,
            supported_reasoning_efforts=["high", "max"],
        )
        assert cfg.supports_thinking is False
        assert cfg.supports_reasoning_control is True

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
        assert public["thinking_mode"] == "provider_default"
        assert public["reasoning_effort"] is None
        assert public["default_reasoning_level"] is None
        assert public["supports_reasoning_control"] is False

    def test_db_model_preserves_reasoning_effort(self):
        model = LLMModel(
            model_id="reasoning-model",
            display_name="Reasoning Model",
            provider="openai",
            api_base="https://api.example.com/v1",
            api_key="test-key",
            model_name="reasoning-model",
            max_tokens=8192,
            context_window=128000,
            reasoning_effort="high",
        )

        assert db_model_to_config(model).reasoning_effort == "high"

    def test_db_model_preserves_thinking_mode(self):
        model = LLMModel(
            model_id="no-think-model",
            display_name="No Thinking Model",
            provider="openai",
            api_base="https://api.example.com/v1",
            api_key="test-key",
            model_name="no-think-model",
            max_tokens=8192,
            context_window=128000,
            thinking_mode="disabled",
        )

        assert db_model_to_config(model).thinking_mode == "disabled"

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

    def test_compute_token_limit_normal(self):
        """默认阈值是可用输入预算的 80%。"""
        cfg = self._make_config(context_window=128000, max_tokens=8192)
        assert cfg.compute_token_limit() == int((128000 - 8192) * 0.8)

    def test_small_window_uses_same_eighty_percent_input_budget_rule(self):
        cfg = self._make_config(context_window=16000, max_tokens=8000)
        assert cfg.compute_token_limit() == 6400

    def test_compute_token_limit_large_output(self):
        """输出配额从窗口中扣除后再计算自动压缩阈值。"""
        cfg = self._make_config(context_window=200000, max_tokens=65536)
        assert cfg.compute_token_limit() == int((200000 - 65536) * 0.8)

    def test_custom_auto_compact_limit_is_capped_at_default_input_budget_limit(self):
        assert self._make_config(
            context_window=100000,
            auto_compact_token_limit=60000,
        ).compute_token_limit() == 60000
        capped = self._make_config(
            context_window=100000,
            auto_compact_token_limit=99000,
        )
        assert capped.compute_token_limit() == int(
            (capped.context_window - capped.max_tokens) * 0.8
        )


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
        model = registry.get("test-model")
        assert model is not None
        assert model.tool_output_truncation_bytes == 42667
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

    def test_invalid_cron_default_model_raises(self, tmp_path):
        """cron_default_model 指向不存在的模型時拋出 ValueError"""
        data = self._minimal_yaml()
        data["cron_default_model"] = "nonexistent-cron"
        path = self._write_yaml(tmp_path, data)
        with pytest.raises(ValueError, match="nonexistent-cron"):
            ModelRegistry.load(path)

    def test_invalid_subagent_default_model_raises(self, tmp_path):
        """subagent_default_model 指向不存在的模型時拋出 ValueError"""
        data = self._minimal_yaml()
        data["subagent_default_model"] = "nonexistent-subagent"
        path = self._write_yaml(tmp_path, data)
        with pytest.raises(ValueError, match="nonexistent-subagent"):
            ModelRegistry.load(path)

    def test_load_with_cron_default_model(self, tmp_path):
        """載入包含 cron_default_model 的 YAML"""
        data = self._minimal_yaml()
        data["cron_default_model"] = "test-model"
        path = self._write_yaml(tmp_path, data)
        registry = ModelRegistry.load(path)
        assert registry.get_cron_default().id == "test-model"

    def test_load_with_subagent_default_model(self, tmp_path):
        """載入包含 subagent_default_model 的 YAML"""
        data = self._minimal_yaml()
        data["subagent_default_model"] = "test-model"
        path = self._write_yaml(tmp_path, data)
        registry = ModelRegistry.load(path)
        assert registry.get_subagent_default().id == "test-model"
        assert registry.subagent_default_model_id == "test-model"

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

    def test_glm_no_think_model_from_project_yaml_disables_thinking(self):
        """项目模型目录中的 No Thinking 变体必须公开为不支持思考。"""
        project_yaml = Path(__file__).resolve().parent.parent / "models.yaml"
        registry = ModelRegistry.load_yaml(project_yaml)

        config = registry.get_or_raise("glm-5-no-think")

        assert config.reasoning_format == "none"
        assert config.supports_thinking is False

    def test_project_yaml_keeps_deepseek_reasoning_levels_as_strings(self):
        project_yaml = Path(__file__).resolve().parent.parent / "models.yaml"
        registry = ModelRegistry.load_yaml(project_yaml)

        config = registry.get("deepseek-flash")

        assert config is not None
        assert config.supported_reasoning_efforts == ["off", "high", "max"]

    def test_runtime_db_failure_does_not_fallback_to_yaml(self):
        with patch(
            "src.api.models.database.SessionLocal",
            side_effect=RuntimeError("database unavailable"),
        ), patch.object(ModelRegistry, "load_yaml") as load_yaml:
            with pytest.raises(RuntimeError, match="database unavailable"):
                ModelRegistry.load()

        load_yaml.assert_not_called()

    @pytest.mark.asyncio
    async def test_glm_no_think_model_list_api_serializes_supports_thinking_false(self, monkeypatch):
        """GET /api/models 的实际序列化结果与 models.yaml 的 No Thinking 配置一致。"""
        from src.api.routes import models as models_route

        project_yaml = Path(__file__).resolve().parent.parent / "models.yaml"
        registry = ModelRegistry.load_yaml(project_yaml)
        config = registry.get_or_raise("glm-5-no-think")
        # 模拟已经 seed 的存量 DB 仍保留旧解析格式；有效开关均关闭时
        # 用户模型 API 仍必须返回 false。
        config.reasoning_format = "reasoning_content"

        monkeypatch.setattr(models_route, "get_model_registry", lambda: registry)
        monkeypatch.setattr(
            models_route,
            "list_accessible_model_configs",
            lambda db, user_id, registry: [config],
        )
        monkeypatch.setattr(
            models_route,
            "resolve_default_model_for_user",
            lambda db, user_id, *, kind="chat", registry=None: config,
        )

        payload = await models_route.list_models(user_id="test-user", db=object())

        assert payload["models"] == [config.to_public_dict()]
        assert payload["models"][0]["supports_thinking"] is False


class TestSeedCatalogValidation:
    """首次建库必须复用 ModelConfig 校验，不得写入非法目录。"""

    def _sqlite_session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from src.api.models.database import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)(), engine

    def _seed_yaml(self, tmp_path: Path, **model_overrides) -> Path:
        model = {
            "display_name": "Seed",
            "provider": "openai",
            "api_base": "https://api.test.com/v1",
            "api_key": "test-key",
            "model_name": "seed-v1",
            "max_tokens": 4096,
        }
        model.update(model_overrides)
        path = tmp_path / "models.yaml"
        path.write_text(
            yaml.dump({"default_model": "seed-model", "models": {"seed-model": model}}),
            encoding="utf-8",
        )
        return path

    def test_unquoted_yaml_off_level_fails_seed_instead_of_persisting_false(self, tmp_path):
        # YAML 1.1 turns bare `off` into False; it must not reach the DB as "False".
        path = tmp_path / "models.yaml"
        path.write_text(
            "default_model: seed-model\n"
            "models:\n"
            "  seed-model:\n"
            "    display_name: Seed\n"
            "    provider: openai\n"
            "    api_base: https://api.test.com/v1\n"
            "    api_key: test-key\n"
            "    model_name: seed-v1\n"
            "    max_tokens: 4096\n"
            "    supported_reasoning_efforts: [off, high]\n",
            encoding="utf-8",
        )
        db, engine = self._sqlite_session()
        try:
            with pytest.raises(ValueError, match="需加引號"):
                seed_model_catalog_from_yaml_if_empty(db, path)
            db.rollback()
            assert db.query(LLMModel).count() == 0
        finally:
            db.close()
            engine.dispose()

    def test_seed_rejects_model_config_violation(self, tmp_path):
        path = self._seed_yaml(tmp_path, max_tokens=200000, context_window=1000)
        db, engine = self._sqlite_session()
        try:
            with pytest.raises(ValueError, match="context_window"):
                seed_model_catalog_from_yaml_if_empty(db, path)
            db.rollback()
            assert db.query(LLMModel).count() == 0
        finally:
            db.close()
            engine.dispose()

    def test_seed_persists_normalized_levels(self, tmp_path):
        path = self._seed_yaml(
            tmp_path,
            reasoning_effort="  high  ",
            supported_reasoning_efforts=[" high ", "max", "high"],
        )
        db, engine = self._sqlite_session()
        try:
            assert seed_model_catalog_from_yaml_if_empty(db, path) == 1
            stored = db.query(LLMModel).one()
            assert stored.reasoning_effort == "high"
            assert db_model_to_config(stored).supported_reasoning_efforts == ["high", "max"]
        finally:
            db.close()
            engine.dispose()

    def test_db_read_surfaces_non_string_reasoning_levels(self, tmp_path):
        path = self._seed_yaml(tmp_path)
        db, engine = self._sqlite_session()
        try:
            seed_model_catalog_from_yaml_if_empty(db, path)
            stored = db.query(LLMModel).one()
            stored.supported_reasoning_efforts_json = '[false, "high"]'
            db.commit()

            with pytest.raises(ValueError, match="非字符串等级"):
                db_model_to_config(stored)
        finally:
            db.close()
            engine.dispose()


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

    def test_get_cron_default_with_dedicated_model(self, registry):
        """cron_default_model 單獨配置時返回專屬模型"""
        cron_model = ModelConfig(
            id="cron-model",
            display_name="CronModel",
            provider="openai",
            api_base="https://c.com",
            api_key="key",
            model_name="cron-v1",
            enabled=True,
        )
        models = {**registry._models, "cron-model": cron_model}
        r = ModelRegistry(
            models=models,
            default_model_id="enabled-model",
            cron_default_model_id="cron-model",
        )
        assert r.get_cron_default().id == "cron-model"

    def test_get_cron_default_inherits_default_at_load(self, registry):
        """未配置 cron_default_model 时，加载阶段继承 default_model"""
        # registry fixture 未设 cron_default_model_id，应与 default 一致
        assert registry.get_cron_default().id == "enabled-model"

    def test_get_subagent_default_with_dedicated_model(self, registry):
        """subagent_default_model 單獨配置時返回專屬模型"""
        subagent_model = ModelConfig(
            id="subagent-model",
            display_name="SubagentModel",
            provider="openai",
            api_base="https://s.com",
            api_key="key",
            model_name="subagent-v1",
            enabled=True,
        )
        models = {**registry._models, "subagent-model": subagent_model}
        r = ModelRegistry(
            models=models,
            default_model_id="enabled-model",
            subagent_default_model_id="subagent-model",
        )
        assert r.get_subagent_default().id == "subagent-model"
        assert r.subagent_default_model_id == "subagent-model"

    def test_get_subagent_default_inherits_default_at_load(self, registry):
        """未配置 subagent_default_model 时，加载阶段继承 default_model"""
        assert registry.get_subagent_default().id == "enabled-model"

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
