"""get_current_user Bearer 鉴权依赖单元测试"""

from datetime import datetime
import pytest
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from src.api.models.database import Base
from src.api.services.auth_service import bootstrap_auth_users
from tests.helpers import make_mock_settings


class TestGetCurrentUser:
    """Bearer Token 鉴权依赖测试"""

    @pytest.fixture(autouse=True)
    def _mock_settings(self):
        mock_s = make_mock_settings(
            get_auth_users=MagicMock(return_value={"demo": "demo123", "admin": "admin456"}),
            get_admin_users=MagicMock(return_value={"admin"}),
        )
        with patch("src.api.deps.get_settings", return_value=mock_s):
            with patch("src.api.services.auth_service.get_settings", return_value=mock_s):
                yield mock_s

    @pytest.fixture
    def db(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)
        db = TestingSessionLocal()
        bootstrap_auth_users(db)
        try:
            yield db
        finally:
            db.close()
            Base.metadata.drop_all(bind=engine)
            engine.dispose()

    @pytest.fixture
    def http_request(self):
        return Request({"type": "http", "headers": []})

    @pytest.mark.asyncio
    async def test_valid_bearer_token(self, db, http_request):
        from src.api.deps import create_access_token, get_current_user

        token, _ = create_access_token("demo")
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        result = await get_current_user(
            request=http_request, credentials=credentials, db=db
        )
        assert result == "demo"

    @pytest.mark.asyncio
    async def test_invalid_scheme(self, db, http_request):
        from src.api.deps import get_current_user

        credentials = HTTPAuthorizationCredentials(scheme="Basic", credentials="abc")
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request=http_request, credentials=credentials, db=db
            )

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
    async def test_invalid_bearer_returns_401(
        self, make_token, detail_substr, db, http_request
    ):
        from src.api.deps import get_current_user

        token = make_token()
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request=http_request, credentials=credentials, db=db
            )

        assert exc_info.value.status_code == 401
        if detail_substr:
            assert detail_substr in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_current_admin_user_success(self, db):
        from src.api.deps import get_current_admin_user

        result = await get_current_admin_user(request=None, user_id="admin", db=db)
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_get_current_admin_user_forbidden_for_normal_user(self, db):
        from src.api.deps import get_current_admin_user

        with pytest.raises(HTTPException) as exc_info:
            await get_current_admin_user(request=None, user_id="demo", db=db)

        assert exc_info.value.status_code == 403
        assert "管理员权限" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_old_token_rejected_after_disable(self, db, http_request):
        """回归：禁用用户后重新启用，禁用前签发的 token 不可用。"""
        from src.api.deps import create_access_token, get_current_user
        from src.api.services.auth_service import update_user_enabled

        # gen=0 签发 token
        token, _ = create_access_token("demo", token_generation=0)

        # 禁用 → token_generation 递增到 1
        update_user_enabled(db, user_id="demo", enabled=False)
        update_user_enabled(db, user_id="demo", enabled=True)

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request=http_request, credentials=credentials, db=db
            )
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_new_token_accepted_after_reenable(self, db, http_request):
        """重新启用后用新 generation 签发的 token 可用。"""
        from src.api.deps import create_access_token, get_current_user
        from src.api.services.auth_service import update_user_enabled

        # 禁用再启用 → token_generation=1
        update_user_enabled(db, user_id="demo", enabled=False)
        update_user_enabled(db, user_id="demo", enabled=True)

        # 用新 generation 签发
        token, _ = create_access_token("demo", token_generation=1)

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_current_user(
            request=http_request, credentials=credentials, db=db
        )
        assert result == "demo"

    @pytest.mark.asyncio
    async def test_same_second_token_rejected_after_generation_bump(
        self, db, http_request
    ):
        """同秒内签发的 token 在 generation 递增后被拒绝（无精度问题）。"""
        from src.api.deps import create_access_token, get_current_user
        from src.api.services.auth_service import update_user_enabled

        token, _ = create_access_token("demo", token_generation=0)
        # 同一秒内禁用再启用
        update_user_enabled(db, user_id="demo", enabled=False)
        update_user_enabled(db, user_id="demo", enabled=True)

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request=http_request, credentials=credentials, db=db
            )
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_token_rejected_after_user_id_recreated(self, db, http_request):
        """同名账号硬删除后重建，旧账号签发的 token 不可复用。"""
        from src.api.deps import create_access_token, get_current_user
        from src.api.services.auth_service import create_simple_user, delete_auth_user
        from src.api.utils.timezone import get_timezone

        issued_at = 1_700_000_000
        with patch("src.api.deps.time.time", return_value=issued_at):
            token, _ = create_access_token("demo", token_generation=0, expires_in_seconds=3600)

        delete_auth_user(db, user_id="demo")
        recreated = create_simple_user(
            db,
            username="demo",
            password="new-pass",
            enabled=True,
            is_admin=False,
            token_limit_per_week=None,
            token_limit_per_month=None,
            created_by="admin",
        )
        recreated.created_at = datetime.fromtimestamp(issued_at + 60, tz=get_timezone()).replace(tzinfo=None)
        db.commit()

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                request=http_request, credentials=credentials, db=db
            )
        assert exc_info.value.status_code == 401
