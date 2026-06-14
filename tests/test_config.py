"""应用配置校验测试。"""

import pytest
from pydantic import ValidationError

from src.api.config import Settings


def test_sandbox_background_command_timeout_rejects_negative():
    """后台 bash 服务端 timeout 只允许 0 或正数。"""
    with pytest.raises(ValidationError, match="must be >= 0"):
        Settings(sandbox_background_command_timeout_seconds=-1)
