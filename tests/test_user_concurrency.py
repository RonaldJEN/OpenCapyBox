"""用户级并发限制测试 - 数据库锁（跨 worker）"""

import asyncio
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.user_run_lock import UserRunLock
from src.api.routes.chat import _acquire_user_run_lock, _release_user_run_lock
from src.api.utils.timezone import now_naive


def _cancel_result(request_id: str):
    from src.api.schemas.turn import CancelResult

    return CancelResult(request_id=request_id, state="acked")


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
            assert lock_row.slot == 0

    async def test_acquire_rejects_when_lock_exists_and_heartbeat_fresh(self):
        """默认串行配置下，心跳新鲜的锁不能被回收（worker 还活着）。"""
        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 1

        with self._TestingSessionLocal() as db:
            db.add(UserRunLock(user_id="user-1", session_id="session-init", lock_id="young-lock"))
            db.commit()

        with (
            patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal),
            patch("src.api.routes.chat.get_settings", return_value=mock_settings),
        ):
            ok = await _acquire_user_run_lock(user_id="user-1", session_id="session-steal")

        assert ok is None
        with self._TestingSessionLocal() as db:
            lock_row = db.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
            assert lock_row is not None
            assert lock_row.session_id == "session-init"  # 原锁保持不变

    async def test_acquire_allows_different_sessions_until_configured_limit(self):
        """同一用户可按配置同时运行多个不同 session。"""
        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 3

        with (
            patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal),
            patch("src.api.routes.chat.get_settings", return_value=mock_settings),
        ):
            lock_a = await _acquire_user_run_lock(user_id="user-1", session_id="session-a")
            lock_b = await _acquire_user_run_lock(user_id="user-1", session_id="session-b")
            lock_c = await _acquire_user_run_lock(user_id="user-1", session_id="session-c")
            lock_d = await _acquire_user_run_lock(user_id="user-1", session_id="session-d")

        assert all(isinstance(lock_id, str) for lock_id in (lock_a, lock_b, lock_c))
        assert lock_d is None
        with self._TestingSessionLocal() as db:
            rows = db.query(UserRunLock).filter(UserRunLock.user_id == "user-1").all()
            assert {row.session_id for row in rows} == {"session-a", "session-b", "session-c"}
            assert {row.slot for row in rows} == {0, 1, 2}

    async def test_acquire_retries_integrity_error_and_returns_busy(self):
        """唯一约束竞争是正常 busy 语义，不应冒泡成 503。"""
        from src.api.services.run_coordinator import RunCoordinator

        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 1
        fresh_lock = SimpleNamespace(
            session_id="session-other",
            slot=0,
            lock_id="lock-other",
            created_at=now_naive(),
            updated_at=now_naive(),
        )

        class FakeQuery:
            def __init__(self, fake_db):
                self.fake_db = fake_db

            def filter(self, *_args, **_kwargs):
                return self

            def all(self):
                self.fake_db.all_calls += 1
                if self.fake_db.all_calls in (1, 2, 3):
                    return []
                return [fresh_lock]

        class FakeDB:
            def __init__(self):
                self.all_calls = 0
                self.rollback_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def query(self, *_args, **_kwargs):
                return FakeQuery(self)

            def execute(self, *_args, **_kwargs):
                raise IntegrityError("INSERT INTO user_run_locks", {}, Exception("duplicate"))

            def commit(self):
                return None

            def rollback(self):
                self.rollback_count += 1

        fake_db = FakeDB()
        coordinator = RunCoordinator(
            session_factory=lambda: fake_db,
            settings_provider=lambda: mock_settings,
        )

        lock_id = await coordinator.acquire_user_run_lock(
            user_id="user-1",
            session_id="session-new",
        )

        assert lock_id is None
        assert fake_db.rollback_count == 1
        assert fake_db.all_calls == 4

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

    async def test_stale_cleanup_only_cancels_that_session_rounds(self):
        """多 session 并发时，回收一个陈旧 slot 不应取消其他活跃 session。"""
        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 2

        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-old", user_id="user-1")
            _add_session(db, session_id="session-live", user_id="user-1")
            old_lock = UserRunLock(user_id="user-1", session_id="session-old", lock_id="stale", slot=0)
            old_lock.created_at = now_naive() - timedelta(seconds=600)
            old_lock.updated_at = now_naive() - timedelta(seconds=600)
            db.add(old_lock)
            db.add(UserRunLock(user_id="user-1", session_id="session-live", lock_id="live", slot=1))
            db.add(Round(id="old-round", session_id="session-old", user_message="hi", status="running"))
            db.add(Round(id="live-round", session_id="session-live", user_message="hi", status="running"))
            db.commit()

        with (
            patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal),
            patch("src.api.routes.chat.get_settings", return_value=mock_settings),
        ):
            lock_id = await _acquire_user_run_lock(user_id="user-1", session_id="session-new")

        assert isinstance(lock_id, str)
        with self._TestingSessionLocal() as db:
            old_round = db.query(Round).filter(Round.id == "old-round").first()
            live_round = db.query(Round).filter(Round.id == "live-round").first()
            assert old_round.status == "cancelled"
            assert live_round.status == "running"

    async def test_release_missing_session_ignores_other_session_slots(self, lock_db_session):
        lock_db_session.add(UserRunLock(user_id="user-1", session_id="session-a", lock_id="lock-a"))
        lock_db_session.commit()

        released = await _release_user_run_lock(lock_db_session, user_id="user-1", session_id="session-b")

        assert released is True
        still_exists = lock_db_session.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
        assert still_exists is not None
        assert still_exists.session_id == "session-a"

    async def test_release_requires_matching_lock_id(self, lock_db_session):
        lock_db_session.add(UserRunLock(user_id="user-1", session_id="session-a", lock_id="current-lock"))
        lock_db_session.commit()

        released = await _release_user_run_lock(
            lock_db_session,
            user_id="user-1",
            lock_id="stale-lock",
            session_id="session-a",
        )

        assert released is False
        still_exists = lock_db_session.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
        assert still_exists is not None
        assert still_exists.lock_id == "current-lock"

    async def test_release_missing_target_lock_ignores_other_session_slots(self, lock_db_session):
        lock_db_session.add(UserRunLock(user_id="user-1", session_id="session-b", lock_id="lock-b", slot=1))
        lock_db_session.commit()

        released = await _release_user_run_lock(
            lock_db_session,
            user_id="user-1",
            lock_id="missing-lock-a",
            session_id="session-a",
        )

        assert released is True
        remaining = lock_db_session.query(UserRunLock).filter(UserRunLock.user_id == "user-1").all()
        assert [(row.session_id, row.lock_id) for row in remaining] == [("session-b", "lock-b")]

    async def test_release_without_session_releases_any_lock(self, lock_db_session):
        lock_db_session.add(UserRunLock(user_id="user-1", session_id="session-a", lock_id="lock-a"))
        lock_db_session.commit()

        released = await _release_user_run_lock(lock_db_session, user_id="user-1")

        assert released is True
        lock_row = lock_db_session.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
        assert lock_row is None


class TestSendMessageConcurrencyBlock:
    """send_message_stream 端点并发限制测试。"""

    @pytest.fixture(autouse=True)
    def _default_token_limit_allows(self):
        with patch("src.api.routes.chat.enforce_token_limits", return_value=None):
            yield

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

    def test_token_limit_rejects_before_user_lock(self):
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
            "src.api.routes.chat.enforce_token_limits",
            side_effect=HTTPException(status_code=429, detail="周 token 限额已用完"),
        ) as enforce_limits, patch(
            "src.api.routes.chat._acquire_user_run_lock",
            new_callable=AsyncMock,
        ) as acquire_lock:
            response = client.post(
                "/chat/session-1/message/stream",
                json={"content": [{"type": "text", "text": "hello"}]},
            )

        assert response.status_code == 429
        assert "token" in response.json()["detail"]
        enforce_limits.assert_called_once_with(mock_db, user_id="testuser")
        acquire_lock.assert_not_called()

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
            from src.api.models.run_cancel_request import RunCancelRequest as RunCancelRequestModel

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = None
            elif model is RunCancelRequestModel:
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

    async def test_resume_returns_stream_before_agent_init(self):
        """resume 不应在返回 StreamingResponse 前等待 Agent 初始化。"""
        from src.api.routes.chat import resume_interrupt
        from src.api.schemas.chat import ResumeRequest
        from starlette.responses import StreamingResponse

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

        with patch("src.api.routes.chat.enforce_token_limits", return_value=None), patch(
            "src.api.routes.chat._acquire_lock_and_clear_cancel", new_callable=AsyncMock, return_value="lock-resume"
        ), patch("src.api.routes.chat.get_agent_pool") as get_pool:
            response = await resume_interrupt(
                "session-1",
                ResumeRequest(interrupt_id="int-1", answers={"question": "answer"}),
                user_id="testuser",
                db=mock_db,
            )

        assert isinstance(response, StreamingResponse)
        get_pool.assert_not_called()

    async def test_resume_no_pending_interrupt_stream_error_releases_lock(self):
        """Agent 初始化已进入 SSE 后，无待处理中断应以 RUN_ERROR 结束并释放锁。"""
        from src.api.routes.chat import resume_interrupt
        from src.api.schemas.chat import ResumeRequest

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.model_id = "model-1"

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox
            from src.api.models.run_cancel_request import RunCancelRequest as RunCancelRequestModel

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = None
            elif model is RunCancelRequestModel:
                chain.filter.return_value.first.return_value = None
            return chain

        mock_db.query.side_effect = query_side_effect

        mock_agent_service = MagicMock()
        mock_agent_service.has_pending_interrupt.return_value = False

        with patch("src.api.routes.chat.enforce_token_limits", return_value=None), patch(
            "src.api.routes.chat._acquire_lock_and_clear_cancel", new_callable=AsyncMock, return_value="lock-resume"
        ), patch("src.api.routes.chat.get_agent_pool") as get_pool, patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True
        ) as release_lock:
            get_pool.return_value.get_or_create = AsyncMock(return_value=mock_agent_service)
            response = await resume_interrupt(
                "session-1",
                ResumeRequest(interrupt_id="int-1", answers={"question": "answer"}),
                user_id="testuser",
                db=mock_db,
            )
            chunks = [chunk async for chunk in response.body_iterator]

        body = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
        assert "RUN_ERROR" in body
        assert "NO_PENDING_INTERRUPT" in body
        release_lock.assert_called_once_with(
            user_id="testuser",
            lock_id="lock-resume",
            session_id="session-1",
        )

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

    @pytest.fixture(autouse=True)
    def _default_token_limit_allows(self):
        with patch("src.api.routes.chat.enforce_token_limits", return_value=None):
            yield

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

    def test_resume_token_limit_rejects_before_agent_init(self):
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-1"
        mock_session.user_id = "testuser"
        mock_session.model_id = "model-1"

        chain = mock_db.query.return_value.filter.return_value
        chain.first.return_value = mock_session

        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch(
            "src.api.routes.chat.enforce_token_limits",
            side_effect=HTTPException(status_code=429, detail="月 token 限额已用完"),
        ) as enforce_limits, patch("src.api.routes.chat.get_agent_pool") as get_pool:
            response = client.post(
                "/chat/session-1/resume",
                json={"interrupt_id": "int-1", "answers": {"question": "answer"}},
            )

        assert response.status_code == 429
        assert "token" in response.json()["detail"]
        enforce_limits.assert_called_once_with(mock_db, user_id="testuser")
        get_pool.assert_not_called()

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
    """abort 端点取消审计与锁释放测试。"""

    def test_abort_with_cancel_token_releases_lock_immediately(self):
        """abort 时 worker 存活（cancel_token 可设置），也应立即释放锁。"""
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
            "src.api.routes.chat._turn_orchestrator.cancel_turn",
            new_callable=AsyncMock,
            return_value=_cancel_result("req-1"),
        ) as cancel_turn, patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True
        ) as release_lock, patch(
            "src.api.routes.chat._complete_cancel_request_in_new_session", new_callable=AsyncMock, return_value=True
        ):
            mock_pool.return_value.get.return_value = mock_agent_service
            response = client.post("/chat/session-1/abort")

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert response.json()["reason"] == "force_aborted"
        assert response.json()["request_id"] == "req-1"
        cancel_turn.assert_called_once()
        release_lock.assert_called_once_with(user_id="testuser", lock_id="lock-1", session_id="session-1")
        assert mock_agent_service.cancel_token.is_set()

    def test_abort_live_worker_mutates_round_to_cancelled(self):
        """abort 时 worker 存活，应立即将 round 收敛为 cancelled。"""
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
            "src.api.routes.chat._turn_orchestrator.cancel_turn",
            new_callable=AsyncMock,
            return_value=_cancel_result("req-2"),
        ) as cancel_turn, patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True
        ), patch(
            "src.api.routes.chat._complete_cancel_request_in_new_session", new_callable=AsyncMock, return_value=True
        ):
            mock_pool.return_value.get.return_value = None
            response = client.post("/chat/session-1/abort")

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert response.json()["reason"] == "force_aborted"
        cancel_turn.assert_called_once()
        assert mock_round.status == "cancelled"

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
             patch(
                 "src.api.routes.chat._turn_orchestrator.cancel_turn",
                 new_callable=AsyncMock,
                 return_value=_cancel_result("req-dead"),
             ), \
             patch("src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True) as release_lock, \
             patch("src.api.routes.chat._complete_cancel_request_in_new_session", new_callable=AsyncMock, return_value=True), \
             patch("src.api.routes.chat._agui_event_bus.cleanup_subscribers"):
            mock_pool.return_value.get.return_value = None
            response = client.post("/chat/session-1/abort")

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert response.json()["reason"] == "worker_dead"
        # round 应被直接标记为 cancelled
        assert mock_round.status == "cancelled"
        # 锁应被释放
        release_lock.assert_called_once_with(user_id="testuser", lock_id="dead-lock", session_id="session-1")


class TestReleaseLockRetry:
    """锁释放 DB 写冲突重试测试。"""

    async def test_release_retries_on_db_locked_then_succeeds(self, lock_db_session):
        """commit 首次 PG 写冲突、重试后成功，释放应成功。"""
        lock_db_session.add(UserRunLock(user_id="user-1", session_id="session-a", lock_id="lock-a"))
        lock_db_session.commit()

        original_commit = lock_db_session.commit
        call_count = 0

        def flaky_commit():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                orig = Exception("deadlock detected")
                orig.pgcode = "40P01"
                raise OperationalError(
                    "DELETE FROM user_run_locks",
                    {},
                    orig,
                )
            return original_commit()

        with patch.object(lock_db_session, "commit", side_effect=flaky_commit), \
             patch.object(lock_db_session, "rollback"):
            released = await _release_user_run_lock(lock_db_session, user_id="user-1", lock_id="lock-a")

        assert released is True
        assert call_count == 2  # 首次失败 + 重试成功

    async def test_release_fails_after_max_retries(self):
        """commit 持续 PG 写冲突，释放应失败且锁仍在。"""
        mock_db = MagicMock()
        mock_lock = MagicMock()
        mock_lock.user_id = "user-1"
        mock_lock.lock_id = "lock-a"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_lock
        orig = Exception("deadlock detected")
        orig.pgcode = "40P01"
        mock_db.commit.side_effect = OperationalError(
            "DELETE FROM user_run_locks",
            {},
            orig,
        )

        released = await _release_user_run_lock(mock_db, user_id="user-1", lock_id="lock-a")

        assert released is False
        # 应该重试了 5 次（max_retries=5）
        assert mock_db.rollback.call_count == 5
