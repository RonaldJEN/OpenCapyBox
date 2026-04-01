"""pytest 配置文件 — 统一公共 fixtures，消除跨文件重复

所有 Mock 类和工厂函数定义在 helpers.py 中，
本文件仅提供 pytest fixtures 供测试文件自动使用。
"""
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 確保項目根目錄在 Python 路徑中
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 設置測試環境變量（如果未設置）
if "LLM_API_KEY" not in os.environ:
    os.environ["LLM_API_KEY"] = "test-api-key-for-ci"
if "LLM_API_BASE" not in os.environ:
    os.environ["LLM_API_BASE"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
if "LLM_MODEL" not in os.environ:
    os.environ["LLM_MODEL"] = "deepseek-v3.2"
if "LLM_PROVIDER" not in os.environ:
    os.environ["LLM_PROVIDER"] = "openai"

# 从 helpers 导入共享工具
from tests.helpers import (  # noqa: E402
    MockLLMClient,
    MockTool,
    FakeAsyncStream,
    MockModelConfig,
    MockRegistry,
    make_query_db,
    make_mock_sandbox,
    make_mock_round,
    make_test_client,
    make_mock_settings,
    collect_agui_events,
)


# ============== 基礎 Fixtures ==============

@pytest.fixture
def temp_workspace(tmp_path):
    """創建臨時工作目錄"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def sample_file(temp_workspace):
    """創建示例文件"""
    file_path = temp_workspace / "test.txt"
    file_path.write_text("Hello, World!\nLine 2\nLine 3")
    return file_path


@pytest.fixture
def nested_files(temp_workspace):
    """創建嵌套目錄結構"""
    (temp_workspace / "src").mkdir()
    (temp_workspace / "src" / "utils").mkdir()
    (temp_workspace / "main.py").write_text("print('main')")
    (temp_workspace / "src" / "app.py").write_text("print('app')")
    (temp_workspace / "src" / "utils" / "helper.py").write_text("def helper(): pass")
    return temp_workspace


# ============== 數據庫 Fixtures ==============

@pytest.fixture
def mock_db_session():
    """模擬數據庫會話（統一版本，所有測試文件共用）"""
    return make_query_db()


# 别名 — 部分测试文件使用 mock_db 名称
@pytest.fixture
def mock_db():
    """mock_db_session 的别名"""
    return make_query_db()


# ============== Sandbox Fixtures ==============

@pytest.fixture
def mock_sandbox():
    """模擬 Sandbox 實例（統一版本，所有測試文件共用）"""
    return make_mock_sandbox()


# ============== 環境變量 Fixtures ==============

@pytest.fixture
def clean_env():
    """提供乾淨的環境變量上下文"""
    original_env = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def mock_env_vars(clean_env):
    """設置測試用環境變量"""
    os.environ["API_KEY"] = "test-api-key"
    os.environ["TIMEZONE"] = "UTC+8"
    yield os.environ


# ============== Agent 相關 Fixtures ==============

@pytest.fixture
def mock_llm_client():
    """模擬 LLM 客戶端"""
    return MockLLMClient()


@pytest.fixture
def mock_tool():
    """模擬工具"""
    return MockTool()


# ============== API 測試 Fixtures ==============

@pytest.fixture
def test_client():
    """創建 FastAPI 測試客戶端"""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    return TestClient(app)
