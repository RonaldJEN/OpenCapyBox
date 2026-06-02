"""DB 用户认证服务测试。"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from ldap3.core.exceptions import LDAPBindError, LDAPSocketOpenError, LDAPSocketReceiveError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.agui_event import AGUIEventLog
from src.api.models.auth_login_event import AuthLoginEvent
from src.api.models.auth_user import AuthUser
from src.api.models.conversation_message import ConversationMessage
from src.api.models.cron_fire import CronFire
from src.api.models.cron_job import CronJob
from src.api.models.database import Base
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.round import Round
from src.api.models.run_cancel_request import RunCancelRequest
from src.api.models.session import Session
from src.api.models.user_memory import CronJobRun, MemoryEmbedding, UserMemory, UserSkillConfig
from src.api.models.user_run_lock import UserRunLock
from src.api.models.user_sandbox import UserSandbox
from src.api.services.auth_service import (
    authenticate_ldap_credentials,
    bootstrap_auth_users,
    create_ldap_user,
    create_simple_user,
    delete_auth_user,
    enforce_token_limits,
    get_auth_user,
    hash_password,
    login_user,
    normalize_domain_user,
    record_login_event,
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


@pytest.mark.parametrize("username", ["demo@example.local", "EXAMPLE\\demo"])
def test_bootstrap_rejects_domain_style_simple_usernames(db, username):
    mock_settings = MagicMock()
    mock_settings.get_auth_users.return_value = {username: "demo123"}
    mock_settings.get_admin_users.return_value = set()

    with patch("src.api.services.auth_service.get_settings", return_value=mock_settings):
        with pytest.raises(HTTPException) as exc_info:
            bootstrap_auth_users(db)

    assert exc_info.value.status_code == 400
    assert "simple 用户名不能包含域前缀或邮箱后缀" in exc_info.value.detail


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


@pytest.mark.parametrize("username", ["local-user@example.local", "EXAMPLE\\local-user"])
def test_create_simple_user_rejects_domain_style_usernames(db, username):
    with pytest.raises(HTTPException) as exc_info:
        create_simple_user(
            db,
            username=username,
            password="pass123",
            enabled=True,
            is_admin=False,
            token_limit_per_week=None,
            token_limit_per_month=None,
            created_by="admin",
        )

    assert exc_info.value.status_code == 400
    assert "simple 用户名不能包含域前缀或邮箱后缀" in exc_info.value.detail


def test_login_user_reports_disabled_simple_user(db):
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
        login_user(db, "disabled-user", "pass123")

    assert exc_info.value.status_code == 403
    assert "账户已被禁用" in exc_info.value.detail


def test_record_login_event_clamps_ip_address(db):
    user = create_simple_user(
        db,
        username="audit-user",
        password="pass123",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="test",
    )

    event = record_login_event(
        db,
        user=user,
        ip_address="1" * 80,
        user_agent="pytest-browser",
    )

    assert event is not None
    assert event.ip_address == "1" * 64
    assert db.query(AuthLoginEvent).filter(AuthLoginEvent.user_id == "audit-user").count() == 1


def test_record_login_event_is_best_effort_on_db_error(db, monkeypatch):
    user = create_simple_user(
        db,
        username="audit-failure-user",
        password="pass123",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="test",
    )
    session_cls = type(db)
    original_rollback = session_cls.rollback
    rollback_calls = 0

    def fail_commit(self):
        raise SQLAlchemyError("audit insert failed")

    def track_rollback(self):
        nonlocal rollback_calls
        rollback_calls += 1
        original_rollback(self)

    monkeypatch.setattr(session_cls, "commit", fail_commit, raising=True)
    monkeypatch.setattr(session_cls, "rollback", track_rollback, raising=True)

    event = record_login_event(
        db,
        user=user,
        ip_address="198.51.100.7",
        user_agent="pytest-browser",
    )

    assert event is None
    assert rollback_calls == 1


def test_login_user_authenticates_simple_user(db):
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

    user = login_user(db, "local-user", "pass123")

    assert user.user_id == "local-user"
    assert user.last_login_at is not None


def test_login_user_authenticates_ldap_user(db):
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

    with patch("src.api.services.auth_service.authenticate_ldap_credentials") as ldap_auth:
        user = login_user(db, "EXAMPLE\\zhangsan", "domain-pass")

    assert user.user_id == "zhangsan"
    assert user.last_login_at is not None
    ldap_auth.assert_called_once_with("zhangsan", "domain-pass")


def test_login_user_rejects_disabled_ldap_user_without_binding(db):
    create_ldap_user(
        db,
        user_id="zhangsan",
        username=None,
        enabled=False,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    with patch("src.api.services.auth_service.authenticate_ldap_credentials") as ldap_auth:
        with pytest.raises(HTTPException) as exc_info:
            login_user(db, "zhangsan", "domain-pass")

    assert exc_info.value.status_code == 403
    assert "账户已被禁用" in exc_info.value.detail
    ldap_auth.assert_not_called()


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
    row = db.query(AuthUser).filter(AuthUser.user_id == "demo-delete").first()
    assert row is None
    assert get_auth_user(db, "demo-delete") is None


def test_delete_auth_user_purges_owned_data(db):
    create_simple_user(
        db,
        username="purge-user",
        password="pass123",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )
    create_simple_user(
        db,
        username="keep-user",
        password="pass123",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    now = now_naive()
    db.add(Session(id="s-purge", user_id="purge-user", title="old", status="active"))
    db.add(Session(id="s-keep", user_id="keep-user", title="keep", status="active"))
    db.add(Round(id="r-purge", session_id="s-purge", user_message="hello", status="completed"))
    db.add(Round(id="r-keep", session_id="s-keep", user_message="hello", status="completed"))
    db.add(ConversationMessage(session_id="s-purge", round_id="r-purge", sequence=1, role="user", content="hi"))
    db.add(AGUIEventLog(run_id="r-purge", event_type="RUN_STARTED", payload="{}", sequence=1))
    db.add(
        LLMCallRecord(
            session_id="s-purge",
            round_id="r-purge",
            step_index=1,
            request_messages="[]",
            request_tools="[]",
        )
    )
    cron_job = CronJob(
        user_id="purge-user",
        name="daily",
        cron_expr="0 9 * * *",
        description="old job",
        content="run",
        enabled=True,
    )
    db.add(cron_job)
    db.flush()
    db.add(CronFire(id="fire-purge", job_id=cron_job.id, scheduled_at=now))
    db.add(CronJobRun(id="run-purge", user_id="purge-user", job_name="daily", cron_expr="0 9 * * *", status="success"))
    db.add(UserMemory(user_id="purge-user", file_type="user_md", content="old memory"))
    db.add(MemoryEmbedding(user_id="purge-user", file_path="memory.md", chunk_index=0, chunk_text="old chunk"))
    db.add(UserSkillConfig(user_id="purge-user", skill_name="docx", enabled=False))
    db.add(RunCancelRequest(session_id="s-purge", user_id="purge-user"))
    db.add(UserRunLock(user_id="purge-user", session_id="s-purge"))
    db.add(UserSandbox(id="usb-purge", user_id="purge-user", sandbox_id="sbx-old", status="active"))
    db.add(UserMemory(user_id="keep-user", file_type="user_md", content="keep memory"))
    db.commit()

    delete_auth_user(db, user_id="purge-user")

    assert db.query(AuthUser).filter(AuthUser.user_id == "purge-user").first() is None
    assert db.query(Session).filter(Session.user_id == "purge-user").count() == 0
    assert db.query(Round).filter(Round.id == "r-purge").count() == 0
    assert db.query(ConversationMessage).filter(ConversationMessage.session_id == "s-purge").count() == 0
    assert db.query(AGUIEventLog).filter(AGUIEventLog.run_id == "r-purge").count() == 0
    assert db.query(LLMCallRecord).filter(LLMCallRecord.session_id == "s-purge").count() == 0
    assert db.query(CronJob).filter(CronJob.user_id == "purge-user").count() == 0
    assert db.query(CronFire).filter(CronFire.id == "fire-purge").count() == 0
    assert db.query(CronJobRun).filter(CronJobRun.user_id == "purge-user").count() == 0
    assert db.query(UserMemory).filter(UserMemory.user_id == "purge-user").count() == 0
    assert db.query(MemoryEmbedding).filter(MemoryEmbedding.user_id == "purge-user").count() == 0
    assert db.query(UserSkillConfig).filter(UserSkillConfig.user_id == "purge-user").count() == 0
    assert db.query(RunCancelRequest).filter(RunCancelRequest.user_id == "purge-user").count() == 0
    assert db.query(UserRunLock).filter(UserRunLock.user_id == "purge-user").count() == 0
    assert db.query(UserSandbox).filter(UserSandbox.user_id == "purge-user").count() == 0
    assert db.query(AuthUser).filter(AuthUser.user_id == "keep-user").count() == 1
    assert db.query(Session).filter(Session.user_id == "keep-user").count() == 1
    assert db.query(UserMemory).filter(UserMemory.user_id == "keep-user").count() == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("EXAMPLE\\zhangsan", "zhangsan"),
        ("zhangsan@EXAMPLE.LOCAL", "zhangsan"),
        (" zhangsan ", "zhangsan"),
    ],
)
def test_normalize_domain_user(raw, expected):
    assert normalize_domain_user(raw) == expected


def test_authenticate_ldap_credentials_uses_fallback_url():
    settings = _make_ldap_settings(["ldap://primary.example.local", "ldap://backup.example.local:8888"])
    fake_connection = MagicMock()

    with patch("src.api.services.auth_service.get_settings", return_value=settings):
        with patch("src.api.services.auth_service.Server") as server_mock:
            with patch(
                "src.api.services.auth_service.Connection",
                side_effect=[LDAPSocketOpenError("primary down"), fake_connection],
            ) as connection_mock:
                authenticate_ldap_credentials("zhangsan", "domain-pass")

    assert server_mock.call_count == 2
    assert server_mock.call_args.args[0] == "ldap://backup.example.local:8888"
    assert server_mock.call_args.kwargs["connect_timeout"] == 5
    assert connection_mock.call_count == 2
    assert connection_mock.call_args.kwargs["user"] == "zhangsan@example.local"
    assert connection_mock.call_args.kwargs["password"] == "domain-pass"
    assert connection_mock.call_args.kwargs["auto_bind"] is True
    assert connection_mock.call_args.kwargs["receive_timeout"] == 10
    fake_connection.search.assert_not_called()
    fake_connection.unbind.assert_called_once()


def test_authenticate_ldap_credentials_binds_short_username_without_domain():
    settings = _make_ldap_settings(["ldap://directory.example.local"], user_domain="")
    fake_connection = MagicMock()

    with patch("src.api.services.auth_service.get_settings", return_value=settings):
        with patch(
            "src.api.services.auth_service.Connection",
            return_value=fake_connection,
        ) as connection_mock:
            authenticate_ldap_credentials("zhangsan", "domain-pass")

    assert connection_mock.call_args.kwargs["user"] == "zhangsan"
    assert connection_mock.call_args.kwargs["password"] == "domain-pass"
    fake_connection.unbind.assert_called_once()


def test_authenticate_ldap_credentials_rejects_invalid_password():
    settings = _make_ldap_settings(["ldap://directory.example.local"])

    with patch("src.api.services.auth_service.get_settings", return_value=settings):
        with patch("src.api.services.auth_service.Connection", side_effect=LDAPBindError("invalid")):
            with pytest.raises(HTTPException) as exc_info:
                authenticate_ldap_credentials("zhangsan", "wrong-pass")

    assert exc_info.value.status_code == 401
    assert "用户名或密码错误" in exc_info.value.detail


def test_authenticate_ldap_credentials_reports_unavailable_after_all_urls_fail():
    settings = _make_ldap_settings(["ldap://primary.example.local", "ldap://backup.example.local:8888"])

    with patch("src.api.services.auth_service.get_settings", return_value=settings):
        with patch(
            "src.api.services.auth_service.Connection",
            side_effect=[LDAPSocketOpenError("primary down"), LDAPSocketReceiveError("backup down")],
        ):
            with pytest.raises(HTTPException) as exc_info:
                authenticate_ldap_credentials("zhangsan", "domain-pass")

    assert exc_info.value.status_code == 503
    assert "LDAP 服务不可用" in exc_info.value.detail


def test_authenticate_ldap_credentials_rejects_empty_password_without_binding():
    settings = _make_ldap_settings(["ldap://directory.example.local"])

    with patch("src.api.services.auth_service.get_settings", return_value=settings) as get_settings_mock:
        with patch("src.api.services.auth_service.Connection") as connection_mock:
            with pytest.raises(HTTPException) as exc_info:
                authenticate_ldap_credentials("zhangsan", "")

    assert exc_info.value.status_code == 401
    get_settings_mock.assert_not_called()
    connection_mock.assert_not_called()


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
    token_generation = user.token_generation
    user = update_user_enabled(db, user_id="toggle-user", enabled=True)

    assert user.token_generation == token_generation


def test_can_recreate_deleted_simple_user_id(db):
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

    user = create_simple_user(
        db,
        username="reuse-test",
        password="pass2",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    assert user.user_id == "reuse-test"
    assert user.token_generation == 0
    assert verify_password("pass2", user.password_hash) is True


def test_can_recreate_deleted_ldap_user_id(db):
    create_simple_user(
        db,
        username="reuse-ldap",
        password="pass",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    delete_auth_user(db, user_id="reuse-ldap")
    user = create_ldap_user(
        db,
        user_id="reuse-ldap",
        username=None,
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    assert user.user_id == "reuse-ldap"
    assert user.auth_type == "ldap"


def test_create_ldap_user_normalizes_domain_prefix(db):
    user = create_ldap_user(
        db,
        user_id="EXAMPLE\\zhangsan",
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
        user_id="lisi@example.local",
        username="李四",
        enabled=True,
        is_admin=False,
        token_limit_per_week=None,
        token_limit_per_month=None,
        created_by="admin",
    )

    assert user.user_id == "lisi"


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


def test_login_user_deleted_simple_user_returns_401(db):
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

    with pytest.raises(HTTPException) as exc_info:
        login_user(db, "del-login", "pass123")

    assert exc_info.value.status_code == 401
    assert "用户名或密码错误" in exc_info.value.detail


def _make_ldap_settings(ldap_urls, user_domain="example.local"):
    settings = MagicMock()
    settings.get_ldap_urls.return_value = ldap_urls
    settings.ldap_user_domain = user_domain
    return settings
