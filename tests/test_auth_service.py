"""DB 用户认证服务测试。"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.auth_user import AuthUser
from src.api.models.database import Base
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.session import Session
from src.api.services.auth_service import (
    bootstrap_auth_users,
    create_ldap_user,
    create_simple_user,
    delete_auth_user,
    enforce_token_limits,
    get_auth_user,
    hash_password,
    login_sso_user,
    login_simple_user,
    normalize_domain_user,
    reset_simple_user_password,
    update_user_enabled,
    verify_password,
)
from src.api.utils.timezone import now_naive


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_password_hash_roundtrip():
    stored = hash_password("secret")

    assert stored != "secret"
    assert verify_password("secret", stored) is True
    assert verify_password("wrong", stored) is False


def test_bootstrap_creates_simple_users_from_env(db):
    mock_settings = MagicMock()
    mock_settings.get_auth_users.return_value = {"demo": "demo123", "admin": "admin456"}
    mock_settings.get_admin_users.return_value = {"admin"}

    with patch("src.api.services.auth_service.get_settings", return_value=mock_settings):
        created = bootstrap_auth_users(db)
        second_created = bootstrap_auth_users(db)

    assert created == 2
    assert second_created == 0
    admin = db.query(AuthUser).filter(AuthUser.user_id == "admin").first()
    assert admin.auth_type == "simple"
    assert admin.enabled is True
    assert admin.is_admin is True
    assert verify_password("admin456", admin.password_hash) is True


def test_bootstrap_surfaces_integrity_error_on_race(db):
    """模拟多 worker 并发：count=0 通过但 commit 时冲突。"""
    from sqlalchemy.exc import IntegrityError

    mock_settings = MagicMock()
    mock_settings.get_auth_users.return_value = {"race_user": "pass"}
    mock_settings.get_admin_users.return_value = set()

    original_commit = db.commit

    call_count = [0]

    def exploding_commit():
        call_count[0] += 1
        if call_count[0] == 1:
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint"))
        original_commit()

    with patch("src.api.services.auth_service.get_settings", return_value=mock_settings):
        with patch.object(db, "commit", side_effect=exploding_commit):
            with pytest.raises(IntegrityError):
                bootstrap_auth_users(db)


def test_simple_and_ldap_user_semantics(db):
    simple = create_simple_user(
        db,
        username="local-user",
        password="pass123",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    ldap = create_ldap_user(
        db,
        user_id="domain-user",
        username=None,
        enabled=True,
        is_admin=True,
        token_limit_per_week=1000,
        token_limit_per_month=3000,
        created_by="admin",
    )

    assert simple.user_id == "local-user"
    assert simple.password_hash is not None
    assert ldap.username == "domain-user"
    assert ldap.password_hash is None
    assert ldap.is_admin is True


def test_login_simple_rejects_ldap_user(db):
    create_ldap_user(
        db,
        user_id="zhangsan",
        username=None,
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    with pytest.raises(HTTPException) as exc_info:
        login_simple_user(db, "zhangsan", "any")

    assert exc_info.value.status_code == 401
    assert "用户名或密码错误" in exc_info.value.detail


class FakeEnterpriseVerifier:
    def __init__(self, user_id: str):
        self.user_id = user_id

    def resolve_user_id(self, request):
        return self.user_id


def test_sso_login_requires_enabled_setting(db):
    mock_settings = MagicMock()
    mock_settings.enterprise_sso_enabled = False

    with patch("src.api.services.auth_service.get_settings", return_value=mock_settings):
        with pytest.raises(HTTPException) as exc_info:
            login_sso_user(db, MagicMock())

    assert exc_info.value.status_code == 404


def test_sso_login_requires_configured_enterprise_verifier(db):
    mock_settings = MagicMock()
    mock_settings.enterprise_sso_enabled = True

    with patch("src.api.services.auth_service.get_settings", return_value=mock_settings):
        with pytest.raises(HTTPException) as exc_info:
            login_sso_user(db, MagicMock())

    assert exc_info.value.status_code == 501


def test_sso_login_accepts_ldap_user_from_enterprise_verifier(db):
    create_ldap_user(
        db,
        user_id="zhangsan",
        username=None,
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    mock_settings = MagicMock()
    mock_settings.enterprise_sso_enabled = True

    with patch("src.api.services.auth_service.get_settings", return_value=mock_settings), patch(
        "src.api.services.auth_service.get_enterprise_identity_verifier",
        return_value=FakeEnterpriseVerifier("DOMAIN\\zhangsan"),
    ):
        user = login_sso_user(db, MagicMock())

    assert user.user_id == "zhangsan"
    assert user.last_login_at is not None


def test_sso_login_rejects_simple_user_from_enterprise_verifier(db):
    create_simple_user(
        db,
        username="local-user",
        password="pass123",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    mock_settings = MagicMock()
    mock_settings.enterprise_sso_enabled = True

    with patch("src.api.services.auth_service.get_settings", return_value=mock_settings), patch(
        "src.api.services.auth_service.get_enterprise_identity_verifier",
        return_value=FakeEnterpriseVerifier("local-user"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            login_sso_user(db, MagicMock())

    assert exc_info.value.status_code == 403


def test_login_simple_reports_disabled_user_after_valid_password(db):
    create_simple_user(
        db,
        username="disabled-user",
        password="pass123",
        enabled=False,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    with pytest.raises(HTTPException) as exc_info:
        login_simple_user(db, "disabled-user", "pass123")

    assert exc_info.value.status_code == 403
    assert "账户已被禁用" in exc_info.value.detail


def test_reset_password_only_allows_simple_user(db):
    create_ldap_user(
        db,
        user_id="zhangsan",
        username=None,
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    with pytest.raises(HTTPException) as exc_info:
        reset_simple_user_password(db, user_id="zhangsan", password="new-pass")

    assert exc_info.value.status_code == 400


def test_delete_auth_user_removes_account(db):
    create_simple_user(
        db,
        username="demo-delete",
        password="pass123",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    deleted_user_id = delete_auth_user(db, user_id="demo-delete")

    assert deleted_user_id == "demo-delete"
    # 软删除：行仍存在但 deleted_at 已设置，get_auth_user 查不到
    row = db.query(AuthUser).filter(AuthUser.user_id == "demo-delete").first()
    assert row is not None
    assert row.deleted_at is not None
    assert row.enabled is False
    assert get_auth_user(db, "demo-delete") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BOSHI\\zhangsan", "zhangsan"),
        ("zhangsan@BOSHI.COM.CN", "zhangsan"),
        (" zhangsan ", "zhangsan"),
    ],
)
def test_normalize_domain_user(raw, expected):
    assert normalize_domain_user(raw) == expected


def test_enforce_token_limits_blocks_when_weekly_limit_reached(db):
    create_simple_user(
        db,
        username="demo",
        password="demo123",
        enabled=True,
        is_admin=False,
        token_limit_per_week=100,
        token_limit_per_month=None,
        created_by="admin",
    )
    now = now_naive()
    session = Session(id="s1", user_id="demo", title="s", status="active", created_at=now, updated_at=now)
    db.add(session)
    db.add(
        LLMCallRecord(
            session_id="s1",
            round_id="r1",
            step_index=1,
            request_messages="[]",
            request_tools="[]",
            usage_total_tokens=100,
            created_at=now - timedelta(hours=1),
        )
    )
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        enforce_token_limits(db, user_id="demo")

    assert exc_info.value.status_code == 429
    assert "本周" in exc_info.value.detail


def test_enforce_token_limits_rejects_missing_user(db):
    with pytest.raises(HTTPException) as exc_info:
        enforce_token_limits(db, user_id="deleted-user")

    assert exc_info.value.status_code == 401


def test_enforce_token_limits_rejects_disabled_user(db):
    create_simple_user(
        db,
        username="disabled-user",
        password="demo123",
        enabled=False,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    with pytest.raises(HTTPException) as exc_info:
        enforce_token_limits(db, user_id="disabled-user")

    assert exc_info.value.status_code == 401


# ── 回归：禁用用户后 token_generation 递增 ──


def test_disable_user_increments_token_generation(db):
    create_simple_user(
        db,
        username="will-disable",
        password="pass",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    user = update_user_enabled(db, user_id="will-disable", enabled=False)
    assert user.token_generation == 1
    assert user.enabled is False


def test_reenable_user_preserves_token_generation(db):
    """重新启用后 token_generation 不回退，旧 token 无法复活。"""
    create_simple_user(
        db,
        username="toggle-user",
        password="pass",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    user = update_user_enabled(db, user_id="toggle-user", enabled=False)
    gen = user.token_generation
    user = update_user_enabled(db, user_id="toggle-user", enabled=True)
    assert user.token_generation == gen


# ── 问题2 回归：软删除后禁止复用 user_id ──


def test_cannot_recreate_deleted_user_id(db):
    create_simple_user(
        db,
        username="reuse-test",
        password="pass",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    delete_auth_user(db, user_id="reuse-test")

    with pytest.raises(HTTPException) as exc_info:
        create_simple_user(
            db,
            username="reuse-test",
            password="pass2",
            enabled=True,
            is_admin=False,
            token_limit_per_week=None,
            token_limit_per_month=None,
            created_by="admin",
        )
    assert exc_info.value.status_code == 409
    assert "历史数据" in exc_info.value.detail


def test_soft_delete_increments_token_generation(db):
    create_simple_user(
        db,
        username="del-tva",
        password="pass",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    delete_auth_user(db, user_id="del-tva")
    row = db.query(AuthUser).filter(AuthUser.user_id == "del-tva").first()
    assert row.token_generation == 1


# ── 问题3 回归：LDAP 创建时规范化 user_id ──


def test_create_ldap_user_normalizes_domain_prefix(db):
    user = create_ldap_user(
        db,
        user_id="DOMAIN\\zhangsan",
        username=None,
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    assert user.user_id == "zhangsan"
    assert user.username == "zhangsan"


def test_create_ldap_user_normalizes_email_suffix(db):
    user = create_ldap_user(
        db,
        user_id="lisi@corp.com",
        username="李四",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    assert user.user_id == "lisi"


# ── 问题4 回归：密码重置后旧 token 失效 ──


def test_reset_password_increments_token_generation(db):
    create_simple_user(
        db,
        username="pwd-reset",
        password="old-pass",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    user = reset_simple_user_password(db, user_id="pwd-reset", password="new-pass")
    assert user.token_generation == 1


# ── 回归：软删除用户登录表现为不存在 ──


def test_login_simple_deleted_user_returns_401(db):
    """软删除后的用户登录应返回 401（不区分密码正确与否），不泄露账号存在。"""
    create_simple_user(
        db,
        username="del-login",
        password="pass123",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    delete_auth_user(db, user_id="del-login")

    # 密码正确也应返回 401
    with pytest.raises(HTTPException) as exc_info:
        login_simple_user(db, "del-login", "pass123")
    assert exc_info.value.status_code == 401
    assert "用户名或密码错误" in exc_info.value.detail
