"""用户级并发限制测试 - 数据库锁（跨 worker）"""

import asyncio
import sqlite3
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.user_run_lock import UserRunLock
from src.api.routes.chat import _acquire_user_run_lock, _release_user_run_lock
from src.api.utils.timezone import now_naive


@pytest.fixture
def lock_db_session():
    """创建隔离的内存数据库会话用于并发锁测试。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _add_session(db, *, session_id: str, user_id: str):
    obj = Session(id=session_id, user_id=user_id, status="active", model_id="model-test")
    db.add(obj)
    db.commit()
    return obj


def _add_round(db, *, round_id: str, session_id: str, status: str):
    obj = Round(id=round_id, session_id=session_id, user_message="hello", status=status)
    db.add(obj)
    db.commit()
    return obj


class TestUserRunLockHelpers:
    """数据库锁辅助函数测试。

    _acquire_user_run_lock 使用独立 SessionLocal，
    测试中通过 patch SessionLocal 注入内存数据库 session。
    """

    @pytest.fixture(autouse=True)
    def _setup_isolated_db(self):
        """为每个测试创建隔离内存 DB 并 patch SessionLocal。"""
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self._TestingSessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=engine
        )
        Base.metadata.create_all(bind=engine)
        self._engine = engine
        yield
        engine.dispose()

    def _get_db(self):
        return self._TestingSessionLocal()

    async def test_acquire_user_lock_success(self):
        with patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal):
            lock_id = await _acquire_user_run_lock(user_id="user-1", session_id="session-a")

        assert isinstance(lock_id, str)
        with self._TestingSessionLocal() as db:
            lock_row = db.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
            assert lock_row is not None
            assert lock_row.session_id == "session-a"
            assert lock_row.lock_id == lock_id

    async def test_acquire_rejects_when_lock_exists_and_heartbeat_fresh(self):
        """心跳新鲜的锁不能被回收（worker 还活着）。"""
        with self._TestingSessionLocal() as db:
            db.add(UserRunLock(user_id="user-1", session_id="session-init", lock_id="young-lock"))
            db.commit()

        with patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal):
            ok = await _acquire_user_run_lock(user_id="user-1", session_id="session-steal")

        assert ok is None
        with self._TestingSessionLocal() as db:
            lock_row = db.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
            assert lock_row is not None
            assert lock_row.session_id == "session-init"  # 原锁保持不变

    async def test_acquire_cleans_stale_lock_by_heartbeat(self):
        """心跳过期的锁应被回收（worker 已死），同时清理孤儿 round。"""
        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-old", user_id="user-1")
            old_lock = UserRunLock(user_id="user-1", session_id="session-old", lock_id="stale")
            old_lock.created_at = now_naive() - timedelta(seconds=600)
            old_lock.updated_at = now_naive() - timedelta(seconds=600)  # 心跳过期
            db.add(old_lock)
            # 模拟 worker 崩溃遗留的 running round
            db.add(Round(id="orphan-round", session_id="session-old", user_message="hi", status="running"))
            db.commit()

        with patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal):
            lock_id = await _acquire_user_run_lock(user_id="user-1", session_id="session-new")

        assert isinstance(lock_id, str)
        with self._TestingSessionLocal() as db:
            lock_row = db.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
            assert lock_row is not None
            assert lock_row.session_id == "session-new"
            # 孤儿 round 应被标记为 cancelled
            orphan = db.query(Round).filter(Round.id == "orphan-round").first()
            assert orphan.status == "cancelled"

    async def test_release_requires_matching_session(self, lock_db_session):
        lock_db_session.add(UserRunLock(user_id="user-1", session_id="session-a", lock_id="lock-a"))
        lock_db_session.commit()

        released = await _release_user_run_lock(lock_db_session, user_id="user-1", session_id="session-b")

        assert released is False
        still_exists = lock_db_session.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
        assert still_exists is not None
        assert still_exists.session_id == "session-a"

    async def test_release_requires_matching_lock_id(self, lock_db_session):
        lock_db_session.add(UserRunLock(user_id="user-1", session_id="session-a", lock_id="current-lock"))
        lock_db_session.commit()

        released = await _release_user_run_lock(lock_db_session, user_id="user-1", lock_id="stale-lock")

        assert released is False
        still_exists = lock_db_session.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
        assert still_exists is not None
        assert still_exists.lock_id == "current-lock"

    async def test_release_without_session_releases_any_lock(self, lock_db_session):
        lock_db_session.add(UserRunLock(user_id="user-1", session_id="session-a", lock_id="lock-a"))
        lock_db_session.commit()

        released = await _release_user_run_lock(lock_db_session, user_id="user-1")

        assert released is True
        lock_row = lock_db_session.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
        assert lock_row is None


class TestSendMessageConcurrencyBlock:
    """send_message_stream 端点并发限制测试。"""

    def test_rejects_when_user_has_active_run(self):
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.status = "active"
        mock_session.model_id = "model-1"

        chain = mock_db.query.return_value.filter.return_value
        chain.first.return_value = mock_session

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch("src.api.routes.chat._acquire_user_run_lock", new_callable=AsyncMock, return_value=None):
            response = client.post(
                "/chat/session-1/message/stream",
                json={"content": [{"type": "text", "text": "hello"}]},
            )

        assert response.status_code == 429
        assert "正在运行" in response.json()["detail"]

    def test_lock_exception_returns_503(self):
        """_acquire_user_run_lock 抛异常时应返回 503（独立 session，异常直接透传）。"""
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.status = "active"
        mock_session.model_id = "model-1"

        chain = mock_db.query.return_value.filter.return_value
        chain.first.return_value = mock_session

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch(
            "src.api.routes.chat._acquire_user_run_lock",
            new_callable=AsyncMock,
            side_effect=OperationalError(
                "INSERT INTO user_run_locks",
                {},
                sqlite3.OperationalError("database is locked"),
            ),
        ):
            response = client.post(
                "/chat/session-1/message/stream",
                json={"content": [{"type": "text", "text": "hello"}]},
            )

        assert response.status_code == 503
        assert "暂时不可用" in response.json()["detail"]

    def test_init_fail_releases_user_lock(self):
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-new"
        mock_session.user_id = "testuser"
        mock_session.status = "active"
        mock_session.model_id = "model-1"
        mock_session.updated_at = None

        mock_sandbox_row = MagicMock()
        mock_sandbox_row.sandbox_id = "sbx-123"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox
            from src.api.models.round import Round as RoundModel

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = mock_sandbox_row
            elif model is RoundModel:
                chain.filter.return_value.count.return_value = 0
            return chain

        mock_db.query.side_effect = query_side_effect

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch("src.api.routes.chat._acquire_user_run_lock", new_callable=AsyncMock, return_value="lock-1"), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True
        ) as release_lock, patch("src.api.routes.chat.get_agent_pool") as mock_pool:
            pool_instance = MagicMock()
            pool_instance.get_or_create = AsyncMock(side_effect=RuntimeError("test: init fail"))
            pool_instance.cleanup_expired_async = AsyncMock()
            mock_pool.return_value = pool_instance

            with client.stream(
                "POST",
                "/chat/session-new/message/stream",
                json={"content": [{"type": "text", "text": "hello"}]},
            ) as response:
                for _ in response.iter_text():
                    pass
                assert response.status_code == 200

        release_lock.assert_called_once_with(
            user_id="testuser",
            lock_id="lock-1",
            session_id="session-new",
        )

    def test_pre_stream_commit_failure_does_not_block_stream(self):
        """session.updated_at commit 失败是次要操作，不应阻塞 SSE 流。

        现行为：warning + rollback 后继续返回 StreamingResponse（200），
        锁释放由 producer finally 负责，不在此处提前释放。
        """
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.status = "active"
        mock_session.model_id = "model-1"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox
            from src.api.models.round import Round as RoundModel

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = None
            elif model is RoundModel:
                chain.filter.return_value.count.return_value = 0
            return chain

        mock_db.query.side_effect = query_side_effect
        mock_db.commit.side_effect = RuntimeError("db commit failed before stream")

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch("src.api.routes.chat._acquire_user_run_lock", new_callable=AsyncMock, return_value="lock-pre-commit"), patch(
            "src.api.routes.chat._clear_pending_cancel_request", new_callable=AsyncMock, return_value=False
        ), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True
        ) as release_lock:
            response = client.post(
                "/chat/session-1/message/stream",
                json={"content": [{"type": "text", "text": "hello"}]},
            )

        # updated_at 失败现在不 503，返回 SSE 流（200）
        assert response.status_code == 200

    def test_resume_pre_stream_commit_failure_does_not_block_stream(self):
        """resume 时 session.updated_at commit 失败是次要操作，不应阻塞 SSE 流。"""
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.model_id = "model-1"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = None
            return chain

        mock_db.query.side_effect = query_side_effect
        mock_db.commit.side_effect = RuntimeError("resume pre-stream commit failed")

        mock_agent_service = MagicMock()
        mock_agent_service.has_pending_interrupt.return_value = True

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch("src.api.routes.chat.get_agent_pool") as mock_pool, patch(
            "src.api.routes.chat._acquire_user_run_lock", new_callable=AsyncMock, return_value="lock-resume-commit"
        ), patch(
            "src.api.routes.chat._clear_pending_cancel_request", new_callable=AsyncMock, return_value=False
        ), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True
        ) as release_lock:
            mock_pool.return_value.get_or_create = AsyncMock(return_value=mock_agent_service)
            response = client.post(
                "/chat/session-1/resume",
                json={"interrupt_id": "int-1", "answers": {"question": "answer"}},
            )

        # updated_at 失败现在不 503，返回 SSE 流（200）
        assert response.status_code == 200

    def test_non_sqlite_exception_returns_503(self):
        """_acquire_user_run_lock 抛非 SQLite 异常时应返回 503（系统错误）。

        回归：RuntimeError 等未知异常不应伪装为 429 并发忙。
        """
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.status = "active"
        mock_session.model_id = "model-1"

        chain = mock_db.query.return_value.filter.return_value
        chain.first.return_value = mock_session

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch(
            "src.api.routes.chat._acquire_user_run_lock",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected failure"),
        ):
            response = client.post(
                "/chat/session-1/message/stream",
                json={"content": [{"type": "text", "text": "hello"}]},
            )

        assert response.status_code == 503
        assert "暂时不可用" in response.json()["detail"]

    def test_non_sqlite_operational_error_returns_503(self):
        """非 SQLite locked 的 OperationalError 应返回 503。"""
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.status = "active"
        mock_session.model_id = "model-1"

        chain = mock_db.query.return_value.filter.return_value
        chain.first.return_value = mock_session

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch(
            "src.api.routes.chat._acquire_user_run_lock",
            new_callable=AsyncMock,
            side_effect=OperationalError(
                "INSERT INTO user_run_locks",
                {},
                Exception("connection refused"),
            ),
        ):
            response = client.post(
                "/chat/session-1/message/stream",
                json={"content": [{"type": "text", "text": "hello"}]},
            )

        assert response.status_code == 503
        assert "暂时不可用" in response.json()["detail"]


class TestResumeConcurrencyBlock:
    """resume_interrupt 端点并发限制测试。"""

    def test_resume_rejects_when_user_has_active_run(self):
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.model_id = "model-1"

        mock_sandbox_row = MagicMock()
        mock_sandbox_row.sandbox_id = "sbx-123"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = mock_sandbox_row
            return chain

        mock_db.query.side_effect = query_side_effect

        mock_agent_service = MagicMock()
        mock_agent_service.has_pending_interrupt.return_value = True

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch("src.api.routes.chat._acquire_user_run_lock", new_callable=AsyncMock, return_value=None), patch(
            "src.api.routes.chat.get_agent_pool"
        ) as mock_pool:
            mock_pool.return_value.get_or_create = AsyncMock(return_value=mock_agent_service)
            response = client.post(
                "/chat/session-1/resume",
                json={"interrupt_id": "int-1", "answers": {"question": "answer"}},
            )

        assert response.status_code == 429
        assert "正在运行" in response.json()["detail"]

    def test_resume_lock_exception_returns_503(self):
        """resume 时 _acquire_user_run_lock 抛异常应返回 503。"""
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.model_id = "model-1"

        mock_sandbox_row = MagicMock()
        mock_sandbox_row.sandbox_id = "sbx-123"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = mock_sandbox_row
            return chain

        mock_db.query.side_effect = query_side_effect

        mock_agent_service = MagicMock()
        mock_agent_service.has_pending_interrupt.return_value = True

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch(
            "src.api.routes.chat._acquire_user_run_lock",
            new_callable=AsyncMock,
            side_effect=OperationalError(
                "INSERT INTO user_run_locks",
                {},
                sqlite3.OperationalError("database is locked"),
            ),
        ), patch("src.api.routes.chat.get_agent_pool") as mock_pool:
            mock_pool.return_value.get_or_create = AsyncMock(return_value=mock_agent_service)
            response = client.post(
                "/chat/session-1/resume",
                json={"interrupt_id": "int-1", "answers": {"question": "answer"}},
            )

        assert response.status_code == 503
        assert "暂时不可用" in response.json()["detail"]

    def test_resume_non_sqlite_exception_returns_503(self):
        """resume 时非 SQLite 异常应返回 503。"""
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.model_id = "model-1"

        mock_sandbox_row = MagicMock()
        mock_sandbox_row.sandbox_id = "sbx-123"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = mock_sandbox_row
            return chain

        mock_db.query.side_effect = query_side_effect

        mock_agent_service = MagicMock()
        mock_agent_service.has_pending_interrupt.return_value = True

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch(
            "src.api.routes.chat._acquire_user_run_lock",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected failure"),
        ), patch("src.api.routes.chat.get_agent_pool") as mock_pool:
            mock_pool.return_value.get_or_create = AsyncMock(return_value=mock_agent_service)
            response = client.post(
                "/chat/session-1/resume",
                json={"interrupt_id": "int-1", "answers": {"question": "answer"}},
            )

        assert response.status_code == 503
        assert "暂时不可用" in response.json()["detail"]


class TestAbortCleansUpUserLock:
    """abort 端点跨 worker 取消请求测试。"""

    def test_abort_with_cancel_token_does_not_release_lock(self):
        """abort 时 worker 存活（cancel_token 可设置），只写入 requested，不释放锁。"""
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.model_id = "model-1"

        mock_round = MagicMock()
        mock_round.id = "round-1"
        mock_round.status = "running"
        mock_round.session_id = "session-1"

        # 模拟锁心跳新鲜（worker 存活）
        mock_lock = MagicMock()
        mock_lock.updated_at = now_naive()
        mock_lock.lock_id = "lock-1"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.round import Round as RoundModel

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is RoundModel:
                chain.filter.return_value.first.return_value = mock_round
            elif model is UserRunLock:
                chain.filter.return_value.first.return_value = mock_lock
            return chain

        mock_db.query.side_effect = query_side_effect

        mock_agent_service = MagicMock()
        mock_agent_service.cancel_token = asyncio.Event()

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch("src.api.routes.chat.get_agent_pool") as mock_pool, patch(
            "src.api.routes.chat._upsert_cancel_request", new_callable=AsyncMock, return_value="req-1"
        ) as upsert_cancel:
            mock_pool.return_value.get.return_value = mock_agent_service
            response = client.post("/chat/session-1/abort")

        assert response.status_code == 200
        assert response.json()["status"] == "cancellation_requested"
        assert response.json()["request_id"] == "req-1"
        upsert_cancel.assert_called_once()
        assert mock_agent_service.cancel_token.is_set()

    def test_abort_live_worker_does_not_mutate_round(self):
        """abort 时 worker 存活，不应直接改写 round 状态。"""
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.model_id = "model-1"

        mock_round = MagicMock()
        mock_round.id = "round-1"
        mock_round.status = "running"
        mock_round.session_id = "session-1"

        # 锁心跳新鲜
        mock_lock = MagicMock()
        mock_lock.updated_at = now_naive()
        mock_lock.lock_id = "lock-2"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.round import Round as RoundModel

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is RoundModel:
                chain.filter.return_value.first.return_value = mock_round
            elif model is UserRunLock:
                chain.filter.return_value.first.return_value = mock_lock
            return chain

        mock_db.query.side_effect = query_side_effect

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch("src.api.routes.chat.get_agent_pool") as mock_pool, patch(
            "src.api.routes.chat._upsert_cancel_request", new_callable=AsyncMock, return_value="req-2"
        ) as upsert_cancel:
            mock_pool.return_value.get.return_value = None
            response = client.post("/chat/session-1/abort")

        assert response.status_code == 200
        assert response.json()["status"] == "cancellation_requested"
        upsert_cancel.assert_called_once()
        # worker 存活时不应直接改写 round 状态
        assert mock_round.status == "running"

    def test_abort_dead_worker_directly_cancels_round(self):
        """abort 检测到 worker 已死（心跳过期），直接标记 round 为 cancelled 并释放锁。"""
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.model_id = "model-1"

        mock_round = MagicMock()
        mock_round.id = "round-dead"
        mock_round.status = "running"
        mock_round.session_id = "session-1"
        mock_round.final_response = ""
        mock_round.step_count = 2

        # 锁心跳过期（worker 已死）
        mock_lock = MagicMock()
        mock_lock.updated_at = now_naive() - timedelta(seconds=600)
        mock_lock.lock_id = "dead-lock"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.round import Round as RoundModel
            from src.api.models.agui_event import AGUIEventLog

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is RoundModel:
                chain.filter.return_value.first.return_value = mock_round
            elif model is UserRunLock:
                chain.filter.return_value.first.return_value = mock_lock
            elif model is AGUIEventLog:
                chain.filter.return_value.count.return_value = 0
            return chain

        mock_db.query.side_effect = query_side_effect

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch("src.api.routes.chat.get_agent_pool") as mock_pool, \
             patch("src.api.routes.chat._upsert_cancel_request", new_callable=AsyncMock, return_value="req-dead"), \
             patch("src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True) as release_lock, \
             patch("src.api.routes.chat._complete_cancel_request_in_new_session", new_callable=AsyncMock, return_value=True), \
             patch("src.api.routes.chat._broadcast_to_subscribers", new_callable=AsyncMock), \
             patch("src.api.routes.chat._cleanup_subscribers"):
            mock_pool.return_value.get.return_value = None
            response = client.post("/chat/session-1/abort")

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert response.json()["reason"] == "worker_dead"
        # round 应被直接标记为 cancelled
        assert mock_round.status == "cancelled"
        # 锁应被释放
        release_lock.assert_called_once_with(user_id="testuser", lock_id="dead-lock")


class TestReleaseLockRetry:
    """锁释放 SQLite locked 重试测试。"""

    async def test_release_retries_on_sqlite_locked_then_succeeds(self, lock_db_session):
        """commit 首次 SQLite locked、重试后成功，释放应成功。"""
        lock_db_session.add(UserRunLock(user_id="user-1", session_id="session-a", lock_id="lock-a"))
        lock_db_session.commit()

        original_commit = lock_db_session.commit
        call_count = 0

        def flaky_commit():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OperationalError(
                    "DELETE FROM user_run_locks",
                    {},
                    sqlite3.OperationalError("database is locked"),
                )
            return original_commit()

        with patch.object(lock_db_session, "commit", side_effect=flaky_commit), \
             patch.object(lock_db_session, "rollback"):
            released = await _release_user_run_lock(lock_db_session, user_id="user-1", lock_id="lock-a")

        assert released is True
        assert call_count == 2  # 首次失败 + 重试成功

    async def test_release_fails_after_max_retries(self):
        """commit 持续 SQLite locked，释放应失败且锁仍在。"""
        mock_db = MagicMock()
        mock_lock = MagicMock()
        mock_lock.user_id = "user-1"
        mock_lock.lock_id = "lock-a"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_lock
        mock_db.commit.side_effect = OperationalError(
            "DELETE FROM user_run_locks",
            {},
            sqlite3.OperationalError("database is locked"),
        )

        released = await _release_user_run_lock(mock_db, user_id="user-1", lock_id="lock-a")

        assert released is False
        # 应该重试了 5 次（max_retries=5）
        assert mock_db.rollback.call_count == 5
