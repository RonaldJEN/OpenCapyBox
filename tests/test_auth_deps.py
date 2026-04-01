"""get_current_user Bearer 鉴权依赖单元测试"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from tests.helpers import make_mock_settings


class TestGetCurrentUser:
    """Bearer Token 鉴权依赖测试"""

    @pytest.fixture(autouse=True)
    def _mock_settings(self):
        mock_s = make_mock_settings(
            get_auth_users=MagicMock(return_value={"demo": "demo123", "admin": "admin456"}),
        )
        with patch("src.api.deps.get_settings", return_value=mock_s):
            yield mock_s

    @pytest.mark.asyncio
    async def test_valid_bearer_token(self):
        from src.api.deps import create_access_token, get_current_user

        token, _ = create_access_token("demo")
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        result = await get_current_user(credentials=credentials)
        assert result == "demo"

    @pytest.mark.asyncio
    async def test_invalid_scheme(self):
        from src.api.deps import get_current_user

        credentials = HTTPAuthorizationCredentials(scheme="Basic", credentials="abc")
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=credentials)

        assert exc_info.value.status_code == 401
        assert "未提供访问令牌" in exc_info.value.detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize("make_token,detail_substr", [
        pytest.param(lambda: "bad.token.value", "无效或已过期的访问令牌", id="invalid_token"),
        pytest.param(
            lambda: __import__("src.api.deps", fromlist=["create_access_token"]).create_access_token("demo", expires_in_seconds=-1)[0],
            "过期",
            id="expired_token",
        ),
        pytest.param(
            lambda: __import__("src.api.deps", fromlist=["create_access_token"]).create_access_token("unknown_user")[0],
            "",
            id="unknown_user",
        ),
    ])
    async def test_invalid_bearer_returns_401(self, make_token, detail_substr):
        from src.api.deps import get_current_user

        token = make_token()
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=credentials)

        assert exc_info.value.status_code == 401
        if detail_substr:
            assert detail_substr in exc_info.value.detail
