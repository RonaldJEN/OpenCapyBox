"""用户级并发限制测试 - 数据库锁（跨 worker）"""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.agui_event import AGUIEventLog
from src.api.models.database import Base
from src.api.models.agent_interaction import AgentInteraction
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.tool_permission import ToolApprovalRequest
from src.api.models.user_run_lock import UserRunLock
from src.api.routes.chat import _acquire_user_run_lock, _release_user_run_lock
from src.api.services.agent_interaction_service import (
    AgentInteractionService,
    InteractionConflictError,
)
from src.api.services.agui_event_bus import get_agui_event_bus
from src.api.services.run_coordinator import RunCoordinator
from src.api.utils.timezone import now_naive
from tests.db_safety import (
    build_pytest_pg_engine,
    create_all_for_test_engine,
    reset_all_tables,
)


_PROJECT_ROOT = Path(__file__).parent.parent


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
            def __init__(self, fake_db, entities):
                self.fake_db = fake_db
                self.entities = entities
                self.for_update = False

            def filter(self, *_args, **_kwargs):
                return self

            def order_by(self, *_args, **_kwargs):
                return self

            def with_for_update(self, *_args, **_kwargs):
                self.for_update = True
                return self

            def all(self):
                self.fake_db.all_calls += 1
                if (
                    len(self.entities) == 1
                    and self.entities[0] is UserRunLock
                    and not self.for_update
                ):
                    self.fake_db.active_lock_reads += 1
                    if self.fake_db.active_lock_reads >= 2:
                        return [fresh_lock]
                return []

        class FakeDB:
            def __init__(self):
                self.all_calls = 0
                self.active_lock_reads = 0
                self.rollback_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def query(self, *args, **_kwargs):
                return FakeQuery(self, args)

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
        assert fake_db.active_lock_reads == 2

    async def test_acquire_cleans_stale_lock_by_heartbeat(self):
        """心跳过期的锁应被回收（worker 已死），同时清理孤儿 round。"""
        from src.api.services.agui_event_bus import get_agui_event_bus

        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-old", user_id="user-1")
            old_lock = UserRunLock(user_id="user-1", session_id="session-old", lock_id="stale")
            old_lock.created_at = now_naive() - timedelta(seconds=600)
            old_lock.updated_at = now_naive() - timedelta(seconds=600)  # 心跳过期
            db.add(old_lock)
            # 模拟 worker 崩溃遗留的 running round
            db.add(Round(id="orphan-round", session_id="session-old", user_message="hi", status="running"))
            db.commit()

        bus = get_agui_event_bus()
        subscriber_queue: asyncio.Queue = asyncio.Queue()
        with bus.subscribers_lock:
            bus.subscribers["orphan-round"] = [subscriber_queue]
        try:
            with patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal):
                lock_id = await _acquire_user_run_lock(
                    user_id="user-1",
                    session_id="session-new",
                )
            terminal = await asyncio.wait_for(subscriber_queue.get(), timeout=1.0)
        finally:
            with bus.subscribers_lock:
                bus.subscribers.pop("orphan-round", None)

        assert isinstance(lock_id, str)
        assert terminal["type"] == "RUN_ERROR"
        assert terminal["code"] == "RUN_FAILED"
        with self._TestingSessionLocal() as db:
            lock_row = db.query(UserRunLock).filter(UserRunLock.user_id == "user-1").first()
            assert lock_row is not None
            assert lock_row.session_id == "session-new"
            # Worker crash 是失败，不应伪装成用户取消。
            orphan = db.query(Round).filter(Round.id == "orphan-round").first()
            assert orphan.status == "failed"

    async def test_stale_lock_fails_expired_started_continuation(self):
        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-recoverable", user_id="user-1")
            old = now_naive() - timedelta(seconds=600)
            db.add(UserRunLock(
                user_id="user-1",
                session_id="session-recoverable",
                lock_id="stale-recoverable",
                created_at=old,
                updated_at=old,
            ))
            db.add(Round(
                id="recoverable-round",
                session_id="session-recoverable",
                user_message="continue",
                status="running",
            ))
            db.commit()
            db.add(AgentInteraction(
                id="recoverable-interaction",
                session_id="session-recoverable",
                round_id="recoverable-round",
                kind="user_input",
                status="pending",
                request_payload="{}",
                answer_payload='{"Continue?":"Yes"}',
                tool_result_content="Continue?: Yes",
                claim_token="dead-worker",
                claim_lease_expires_at=old + timedelta(seconds=30),
                continuation_started_at=old,
            ))
            db.commit()

        bus = get_agui_event_bus()
        subscriber_queue: asyncio.Queue = asyncio.Queue()
        with bus.subscribers_lock:
            bus.subscribers["recoverable-round"] = [subscriber_queue]
        try:
            with patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal):
                lock_id = await _acquire_user_run_lock(
                    user_id="user-1",
                    session_id="session-new",
                )
            terminal = await asyncio.wait_for(subscriber_queue.get(), timeout=1.0)
        finally:
            with bus.subscribers_lock:
                bus.subscribers.pop("recoverable-round", None)

        assert isinstance(lock_id, str)
        assert terminal["type"] == "RUN_ERROR"
        with self._TestingSessionLocal() as db:
            round_obj = db.get(Round, "recoverable-round")
            interaction = db.get(AgentInteraction, "recoverable-interaction")
            assert round_obj.status == "failed"
            assert interaction.status == "failed"
            assert interaction.claim_token is None
            assert interaction.claim_lease_expires_at is None

    async def test_stale_cleanup_preserves_active_tool_execution_lease(self):
        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 1

        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-active-execution", user_id="user-1")
            old = now_naive() - timedelta(seconds=600)
            db.add_all([
                UserRunLock(
                    user_id="user-1",
                    session_id="session-active-execution",
                    lock_id="stale-active-execution",
                    created_at=old,
                    updated_at=old,
                ),
                Round(
                    id="round-active-execution",
                    session_id="session-active-execution",
                    user_message="continue",
                    status="running",
                ),
            ])
            db.commit()
            db.add_all([
                AgentInteraction(
                    id="interaction-active-execution",
                    session_id="session-active-execution",
                    round_id="round-active-execution",
                    kind="tool_approval",
                    tool_call_id="tool-active-execution",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"approval":"allow_once"}',
                    tool_result_content="[Tool approval execution pending]",
                    claim_token="expired-continuation",
                    claim_lease_expires_at=old + timedelta(seconds=30),
                    continuation_started_at=old,
                ),
                ToolApprovalRequest(
                    id="interaction-active-execution",
                    user_id="user-1",
                    session_id="session-active-execution",
                    run_id="round-active-execution",
                    tool_call_id="tool-active-execution",
                    provider="builtin",
                    tool_name="shell_exec",
                    model_tool_name="shell_exec",
                    arguments_encrypted="encrypted",
                    arguments_hash="hash-active-execution",
                    status="executing",
                    resolution="allow_once",
                    resolved_at=old,
                    execution_started_at=old,
                    execution_claim_token="live-execution",
                    execution_lease_expires_at=now_naive() + timedelta(seconds=600),
                ),
            ])
            db.commit()

        with (
            patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal),
            patch("src.api.routes.chat.get_settings", return_value=mock_settings),
        ):
            lock_id = await _acquire_user_run_lock(
                user_id="user-1",
                session_id="session-new",
            )

        assert lock_id is None
        with self._TestingSessionLocal() as db:
            restored_lock = db.get(UserRunLock, "stale-active-execution")
            round_obj = db.get(Round, "round-active-execution")
            interaction = db.get(AgentInteraction, "interaction-active-execution")
            approval = db.get(ToolApprovalRequest, "interaction-active-execution")
            assert restored_lock is not None
            assert restored_lock.session_id == "session-active-execution"
            assert round_obj.status == "running"
            assert interaction.status == "pending"
            assert approval.status == "executing"
            assert approval.execution_claim_token == "live-execution"

    async def test_stale_cleanup_marks_expired_tool_execution_unknown_before_failure(self):
        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-expired-execution", user_id="user-1")
            old = now_naive() - timedelta(seconds=600)
            db.add_all([
                UserRunLock(
                    user_id="user-1",
                    session_id="session-expired-execution",
                    lock_id="stale-expired-execution",
                    created_at=old,
                    updated_at=old,
                ),
                Round(
                    id="round-expired-execution",
                    session_id="session-expired-execution",
                    user_message="continue",
                    status="running",
                ),
            ])
            db.commit()
            db.add_all([
                AgentInteraction(
                    id="interaction-expired-execution",
                    session_id="session-expired-execution",
                    round_id="round-expired-execution",
                    kind="tool_approval",
                    tool_call_id="tool-expired-execution",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"approval":"allow_once"}',
                    tool_result_content="[Tool approval execution pending]",
                    claim_token="expired-continuation",
                    claim_lease_expires_at=old + timedelta(seconds=30),
                    continuation_started_at=old,
                ),
                ToolApprovalRequest(
                    id="interaction-expired-execution",
                    user_id="user-1",
                    session_id="session-expired-execution",
                    run_id="round-expired-execution",
                    tool_call_id="tool-expired-execution",
                    provider="builtin",
                    tool_name="shell_exec",
                    model_tool_name="shell_exec",
                    arguments_encrypted="encrypted",
                    arguments_hash="hash-expired-execution",
                    status="executing",
                    resolution="allow_once",
                    resolved_at=old,
                    execution_started_at=old,
                    execution_claim_token="dead-execution",
                    execution_lease_expires_at=old + timedelta(seconds=30),
                ),
            ])
            db.commit()

        bus = get_agui_event_bus()
        subscriber_queue: asyncio.Queue = asyncio.Queue()
        with bus.subscribers_lock:
            bus.subscribers["round-expired-execution"] = [subscriber_queue]
        try:
            with patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal):
                lock_id = await _acquire_user_run_lock(
                    user_id="user-1",
                    session_id="session-new",
                )
            terminal = await asyncio.wait_for(subscriber_queue.get(), timeout=1.0)
        finally:
            with bus.subscribers_lock:
                bus.subscribers.pop("round-expired-execution", None)

        assert isinstance(lock_id, str)
        assert terminal["type"] == "RUN_ERROR"
        assert "工具审批续跑进程中断" in terminal["message"]
        with self._TestingSessionLocal() as db:
            round_obj = db.get(Round, "round-expired-execution")
            interaction = db.get(AgentInteraction, "interaction-expired-execution")
            approval = db.get(ToolApprovalRequest, "interaction-expired-execution")
            assert round_obj.status == "failed"
            assert interaction.status == "failed"
            assert approval.status == "unknown"
            assert approval.execution_claim_token is None
            assert approval.execution_lease_expires_at is None

    async def test_stale_cleanup_preserves_active_continuation_claim(self):
        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 1

        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-active-claim", user_id="user-1")
            old = now_naive() - timedelta(seconds=600)
            db.add(UserRunLock(
                user_id="user-1",
                session_id="session-active-claim",
                lock_id="stale-active-claim",
                created_at=old,
                updated_at=old,
            ))
            db.add(Round(
                id="round-active-claim",
                session_id="session-active-claim",
                user_message="continue",
                status="waiting_interaction",
            ))
            db.commit()
            db.add(AgentInteraction(
                id="interaction-active-claim",
                session_id="session-active-claim",
                round_id="round-active-claim",
                kind="user_input",
                status="pending",
                request_payload="{}",
                answer_payload='{"Continue?":"Yes"}',
                tool_result_content="Continue?: Yes",
                claim_token="live-worker",
                claim_lease_expires_at=now_naive() + timedelta(seconds=600),
            ))
            db.commit()

        with (
            patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal),
            patch("src.api.routes.chat.get_settings", return_value=mock_settings),
        ):
            lock_id = await _acquire_user_run_lock(
                user_id="user-1",
                session_id="session-new",
            )

        assert lock_id is None
        with self._TestingSessionLocal() as db:
            round_obj = db.get(Round, "round-active-claim")
            interaction = db.get(AgentInteraction, "interaction-active-claim")
            restored_lock = db.get(UserRunLock, "stale-active-claim")
            assert round_obj.status == "waiting_interaction"
            assert interaction.claim_token == "live-worker"
            assert restored_lock is not None
            assert restored_lock.session_id == "session-active-claim"

    async def test_missing_lock_active_continuation_counts_as_virtual_slot(self):
        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 1

        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-unlocked-active", user_id="user-1")
            db.add_all([
                Round(
                    id="round-unlocked-active",
                    session_id="session-unlocked-active",
                    user_message="continue",
                    status="running",
                ),
                AgentInteraction(
                    id="interaction-unlocked-active",
                    session_id="session-unlocked-active",
                    round_id="round-unlocked-active",
                    kind="user_input",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"Continue?":"Yes"}',
                    tool_result_content="Continue?: Yes",
                    claim_token="live-unlocked-worker",
                    claim_lease_expires_at=now_naive() + timedelta(minutes=10),
                    continuation_started_at=now_naive(),
                ),
            ])
            db.commit()

        with (
            patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal),
            patch("src.api.routes.chat.get_settings", return_value=mock_settings),
        ):
            lock_id = await _acquire_user_run_lock(
                user_id="user-1",
                session_id="session-new",
            )

        assert lock_id is None
        with self._TestingSessionLocal() as db:
            assert db.get(Round, "round-unlocked-active").status == "running"
            interaction = db.get(
                AgentInteraction,
                "interaction-unlocked-active",
            )
            assert interaction.claim_token == "live-unlocked-worker"
            assert db.query(UserRunLock).count() == 0

    @pytest.mark.parametrize(
        ("parent_round_id", "child_round_id"),
        [
            ("a-active-parent", "z-subagent-child"),
            ("z-active-parent", "a-subagent-child"),
        ],
    )
    async def test_missing_lock_active_parent_protects_running_subagent_child(
        self,
        parent_round_id,
        child_round_id,
    ):
        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 1
        old = now_naive() - timedelta(minutes=10)

        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-active-parent", user_id="user-1")
            db.add_all([
                Round(
                    id=parent_round_id,
                    session_id="session-active-parent",
                    user_message="continue",
                    status="running",
                ),
                Round(
                    id=child_round_id,
                    session_id="session-active-parent",
                    parent_run_id=parent_round_id,
                    user_message="delegated subagent work",
                    status="running",
                ),
            ])
            db.commit()
            db.add_all([
                AgentInteraction(
                    id="interaction-active-parent",
                    session_id="session-active-parent",
                    round_id=parent_round_id,
                    kind="tool_approval",
                    tool_call_id="tool-active-parent",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"approval":"allow_once"}',
                    tool_result_content="[Tool approval execution pending]",
                    claim_token="expired-parent-continuation",
                    claim_lease_expires_at=old,
                    continuation_started_at=old,
                ),
                ToolApprovalRequest(
                    id="interaction-active-parent",
                    user_id="user-1",
                    session_id="session-active-parent",
                    run_id=parent_round_id,
                    tool_call_id="tool-active-parent",
                    provider="builtin",
                    tool_name="sub_agent",
                    model_tool_name="sub_agent",
                    arguments_encrypted="encrypted",
                    arguments_hash="hash-active-parent",
                    status="executing",
                    resolution="allow_once",
                    resolved_at=old,
                    execution_started_at=old,
                    execution_claim_token="live-parent-execution",
                    execution_lease_expires_at=now_naive() + timedelta(minutes=10),
                ),
            ])
            db.commit()

        with (
            patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal),
            patch("src.api.routes.chat.get_settings", return_value=mock_settings),
        ):
            lock_id = await _acquire_user_run_lock(
                user_id="user-1",
                session_id="session-new",
            )

        assert lock_id is None
        with self._TestingSessionLocal() as db:
            assert db.get(Round, parent_round_id).status == "running"
            assert db.get(Round, child_round_id).status == "running"
            approval = db.get(ToolApprovalRequest, "interaction-active-parent")
            assert approval.status == "executing"
            assert approval.execution_claim_token == "live-parent-execution"
            assert (
                db.query(AGUIEventLog)
                .filter(AGUIEventLog.run_id == child_round_id)
                .count()
                == 0
            )

    async def test_missing_lock_expired_waiting_claim_is_fenced_before_new_slot(self):
        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 1
        old = now_naive() - timedelta(minutes=10)

        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-unlocked-expired", user_id="user-1")
            db.add_all([
                Round(
                    id="round-unlocked-expired",
                    session_id="session-unlocked-expired",
                    user_message="continue",
                    status="waiting_interaction",
                ),
                AgentInteraction(
                    id="interaction-unlocked-expired",
                    session_id="session-unlocked-expired",
                    round_id="round-unlocked-expired",
                    kind="user_input",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"Continue?":"Yes"}',
                    tool_result_content="Continue?: Yes",
                    claim_token="expired-unlocked-worker",
                    claim_lease_expires_at=old,
                ),
            ])
            db.commit()

        with (
            patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal),
            patch("src.api.routes.chat.get_settings", return_value=mock_settings),
        ):
            lock_id = await _acquire_user_run_lock(
                user_id="user-1",
                session_id="session-new",
            )

        assert isinstance(lock_id, str)
        with self._TestingSessionLocal() as db:
            interaction = db.get(
                AgentInteraction,
                "interaction-unlocked-expired",
            )
            assert db.get(Round, "round-unlocked-expired").status == "waiting_interaction"
            assert interaction.claim_token is None
            assert interaction.claim_lease_expires_at is None
            with pytest.raises(InteractionConflictError):
                AgentInteractionService.renew_continuation_claim(
                    db,
                    session_id="session-unlocked-expired",
                    interaction_id="interaction-unlocked-expired",
                    claim_token="expired-unlocked-worker",
                )

    async def test_active_claim_uses_one_slot_without_blocking_other_sessions(self):
        mock_settings = MagicMock()
        mock_settings.sse_subscribe_timeout = 300
        mock_settings.agent_user_concurrency_limit = 2

        with self._TestingSessionLocal() as db:
            _add_session(db, session_id="session-active-claim", user_id="user-1")
            old = now_naive() - timedelta(seconds=600)
            db.add(UserRunLock(
                user_id="user-1",
                session_id="session-active-claim",
                lock_id="stale-active-claim",
                slot=0,
                created_at=old,
                updated_at=old,
            ))
            db.add(Round(
                id="round-active-claim",
                session_id="session-active-claim",
                user_message="continue",
                status="waiting_interaction",
            ))
            db.commit()
            db.add(AgentInteraction(
                id="interaction-active-claim",
                session_id="session-active-claim",
                round_id="round-active-claim",
                kind="user_input",
                status="pending",
                request_payload="{}",
                answer_payload='{"Continue?":"Yes"}',
                tool_result_content="Continue?: Yes",
                claim_token="live-worker",
                claim_lease_expires_at=now_naive() + timedelta(seconds=600),
            ))
            db.commit()

        with (
            patch("src.api.routes.chat.SessionLocal", self._TestingSessionLocal),
            patch("src.api.routes.chat.get_settings", return_value=mock_settings),
        ):
            lock_id = await _acquire_user_run_lock(
                user_id="user-1",
                session_id="session-new",
            )

        assert isinstance(lock_id, str)
        with self._TestingSessionLocal() as db:
            restored_lock = db.get(UserRunLock, "stale-active-claim")
            new_lock = db.get(UserRunLock, lock_id)
            round_obj = db.get(Round, "round-active-claim")
            assert restored_lock is not None
            assert restored_lock.slot == 0
            assert new_lock is not None
            assert new_lock.session_id == "session-new"
            assert new_lock.slot == 1
            assert round_obj.status == "waiting_interaction"

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
            assert old_round.status == "failed"
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


def test_local_acquire_mutex_registry_uses_weak_values():
    import weakref

    from src.api.services.run_coordinator import _LOCAL_ACQUIRE_LOCKS

    assert isinstance(_LOCAL_ACQUIRE_LOCKS, weakref.WeakValueDictionary)


def test_postgres_coordination_session_pins_connection_across_commits():
    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    coordinator = RunCoordinator(session_factory=factory)

    try:
        with coordinator._coordination_session() as db:
            assert isinstance(db.get_bind(), Connection)
            advisory_key = coordinator._acquire_postgres_user_mutex(
                db,
                "user-pinned-advisory",
            )
            assert isinstance(advisory_key, int)
            backend_pid_before = db.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            db.commit()
            backend_pid_after = db.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            assert backend_pid_after == backend_pid_before
            coordinator._release_postgres_user_mutex(db, advisory_key)
    finally:
        engine.dispose()


def test_failed_postgres_advisory_unlock_invalidates_session_connection():
    db = MagicMock()
    connection = MagicMock()
    connection.execute.return_value.scalar_one.return_value = False
    db.connection.return_value = connection

    with pytest.raises(RuntimeError, match="advisory lock was not owned"):
        RunCoordinator._release_postgres_user_mutex(db, advisory_key=123)

    connection.invalidate.assert_called_once()
    db.invalidate.assert_not_called()
    db.commit.assert_not_called()


def test_stale_recovery_mutex_spans_delete_cleanup_and_reallocation_on_postgresql():
    """A second worker cannot claim the deleted slot during orphan cleanup."""

    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    create_all_for_test_engine(engine, Base.metadata)
    reset_all_tables(engine, Base.metadata)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    settings = SimpleNamespace(
        sse_subscribe_timeout=300,
        agent_user_concurrency_limit=1,
    )
    coordinator_a = RunCoordinator(
        session_factory=factory,
        settings_provider=lambda: settings,
    )
    coordinator_b = RunCoordinator(
        session_factory=factory,
        settings_provider=lambda: settings,
    )
    cleanup_entered = Event()
    cleanup_release = Event()
    second_start = Barrier(2)

    try:
        with factory() as seed:
            seed.add(Session(
                id="session-stale-race",
                user_id="user-stale-race",
                status="active",
                model_id="model-test",
            ))
            seed.commit()
            old = now_naive() - timedelta(seconds=600)
            seed.add(UserRunLock(
                user_id="user-stale-race",
                session_id="session-stale-race",
                lock_id="stale-race-lock",
                created_at=old,
                updated_at=old,
            ))
            seed.add(Round(
                id="round-stale-race",
                session_id="session-stale-race",
                user_message="hello",
                status="running",
            ))
            seed.commit()

        original_cleanup = coordinator_a._cleanup_orphaned_rounds_detailed

        def gated_cleanup(db, *, user_id, session_id=None):
            cleanup_entered.set()
            assert cleanup_release.wait(timeout=5)
            return original_cleanup(
                db,
                user_id=user_id,
                session_id=session_id,
            )

        coordinator_a._cleanup_orphaned_rounds_detailed = gated_cleanup  # type: ignore[method-assign]

        def acquire_a():
            return asyncio.run(coordinator_a.acquire_user_run_lock(
                user_id="user-stale-race",
                session_id="session-new-a",
            ))

        def acquire_b():
            second_start.wait(timeout=2)
            return asyncio.run(coordinator_b.acquire_user_run_lock(
                user_id="user-stale-race",
                session_id="session-new-b",
            ))

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(acquire_a)
            assert cleanup_entered.wait(timeout=5)
            second_future = executor.submit(acquire_b)
            second_start.wait(timeout=2)
            with pytest.raises(FutureTimeoutError):
                second_future.result(timeout=0.2)
            cleanup_release.set()
            first_lock_id = first_future.result(timeout=5)
            second_lock_id = second_future.result(timeout=5)

        assert isinstance(first_lock_id, str)
        assert second_lock_id is None
        with factory() as verify:
            locks = (
                verify.query(UserRunLock)
                .filter(UserRunLock.user_id == "user-stale-race")
                .all()
            )
            assert [(lock.session_id, lock.lock_id) for lock in locks] == [
                ("session-new-a", first_lock_id),
            ]
            assert verify.get(Round, "round-stale-race").status == "failed"
    finally:
        cleanup_release.set()
        reset_all_tables(engine, Base.metadata)
        engine.dispose()


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

    def test_model_access_failure_releases_user_lock_before_stream(self):
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-model-denied"
        mock_session.user_id = "testuser"
        mock_session.status = "active"
        mock_session.model_id = "denied-model"

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
        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch("src.api.routes.chat._acquire_user_run_lock", new_callable=AsyncMock, return_value="lock-model"), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True
        ) as release_lock, patch(
            "src.api.routes.chat._resolve_session_model_for_user",
            side_effect=HTTPException(status_code=403, detail="当前用户无权使用模型"),
        ):
            response = client.post(
                "/chat/session-model-denied/message/stream",
                json={"content": [{"type": "text", "text": "hello"}]},
            )

        assert response.status_code == 403
        release_lock.assert_called_once_with(
            user_id="testuser",
            lock_id="lock-model",
            session_id="session-model-denied",
        )

    def test_resume_model_access_failure_releases_user_lock_before_stream(self):
        from tests.helpers import make_test_client
        from src.api.routes import chat as chat_routes

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_session.id = "session-resume-denied"
        mock_session.user_id = "testuser"
        mock_session.model_id = "denied-model"

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
        client = make_test_client(chat_routes.router, "/chat", db=mock_db)

        with patch(
            "src.api.routes.chat._acquire_lock_and_clear_cancel", new_callable=AsyncMock, return_value="lock-resume-model"
        ), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session", new_callable=AsyncMock, return_value=True
        ) as release_lock, patch(
            "src.api.routes.chat._resolve_session_model_for_user",
            side_effect=HTTPException(status_code=403, detail="当前用户无权使用模型"),
        ):
            response = client.post(
                "/chat/session-resume-denied/resume",
                json={"interrupt_id": "int-1", "answers": {"question": "answer"}},
            )

        assert response.status_code == 403
        release_lock.assert_called_once_with(
            user_id="testuser",
            lock_id="lock-resume-model",
            session_id="session-resume-denied",
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

    async def test_resume_conflict_returns_specific_stream_error(self):
        from src.api.routes.chat import resume_interrupt
        from src.api.schemas.chat import ResumeRequest
        from src.api.services.agent_interaction_service import InteractionConflictError

        mock_db = MagicMock()
        mock_session = MagicMock(id="session-1", user_id="testuser", model_id="model-1")

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
        mock_agent_service.has_pending_interrupt.return_value = True

        with patch("src.api.routes.chat.enforce_token_limits", return_value=None), patch(
            "src.api.routes.chat._acquire_lock_and_clear_cancel",
            new_callable=AsyncMock,
            return_value="lock-resume",
        ), patch("src.api.routes.chat.get_agent_pool") as get_pool, patch(
            "src.api.routes.chat._turn_orchestrator.resume_turn",
            new_callable=AsyncMock,
            side_effect=InteractionConflictError("answer already accepted"),
        ), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as release_lock:
            get_pool.return_value.get_or_create = AsyncMock(return_value=mock_agent_service)
            response = await resume_interrupt(
                "session-1",
                ResumeRequest(interrupt_id="int-1", answers={"question": "different"}),
                user_id="testuser",
                db=mock_db,
            )
            chunks = [chunk async for chunk in response.body_iterator]

        body = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
        assert "RESUME_CONFLICT" in body
        assert "answer already accepted" in body
        assert "INTERNAL_ERROR" not in body
        release_lock.assert_called_once()

    async def test_invalid_interaction_response_returns_specific_stream_error(self):
        from src.api.routes.chat import resume_interrupt
        from src.api.schemas.chat import ResumeRequest
        from src.api.services.agent_service import InvalidInteractionResponseError

        mock_db = MagicMock()
        mock_session = MagicMock(id="session-1", user_id="testuser", model_id="model-1")

        def query_side_effect(model):
            from src.api.models.run_cancel_request import (
                RunCancelRequest as RunCancelRequestModel,
            )
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox

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
        mock_agent_service.has_pending_interrupt.return_value = True

        with patch("src.api.routes.chat.enforce_token_limits", return_value=None), patch(
            "src.api.routes.chat._acquire_lock_and_clear_cancel",
            new_callable=AsyncMock,
            return_value="lock-resume",
        ), patch("src.api.routes.chat.get_agent_pool") as get_pool, patch(
            "src.api.routes.chat._turn_orchestrator.resume_turn",
            new_callable=AsyncMock,
            side_effect=InvalidInteractionResponseError("invalid approval resolution"),
        ), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as release_lock:
            get_pool.return_value.get_or_create = AsyncMock(return_value=mock_agent_service)
            response = await resume_interrupt(
                "session-1",
                ResumeRequest(interrupt_id="int-1", answers={"approval": "bogus"}),
                user_id="testuser",
                db=mock_db,
            )
            chunks = [chunk async for chunk in response.body_iterator]

        body = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
        assert "INVALID_INTERACTION_RESPONSE" in body
        assert "invalid approval resolution" in body
        assert "INTERNAL_ERROR" not in body
        release_lock.assert_called_once()

    async def test_send_while_waiting_returns_interaction_pending_stream_error(self):
        from src.api.routes.chat import send_message_stream
        from src.api.schemas.chat import SendMessageRequest
        from src.api.services.agent_interaction_service import InteractionConflictError

        mock_db = MagicMock()
        mock_session = MagicMock(
            id="session-1",
            user_id="testuser",
            status="active",
            model_id="model-1",
        )

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox
            from src.api.models.round import Round as RoundModel
            from src.api.models.run_cancel_request import RunCancelRequest as RunCancelRequestModel

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = None
            elif model is RoundModel:
                chain.filter.return_value.count.return_value = 1
            elif model is RunCancelRequestModel:
                chain.filter.return_value.first.return_value = None
            return chain

        mock_db.query.side_effect = query_side_effect
        mock_agent_service = MagicMock()

        with patch("src.api.routes.chat.enforce_token_limits", return_value=None), patch(
            "src.api.routes.chat._acquire_lock_and_clear_cancel",
            new_callable=AsyncMock,
            return_value="lock-send",
        ), patch("src.api.routes.chat.get_agent_pool") as get_pool, patch(
            "src.api.routes.chat._turn_orchestrator.submit_turn",
            new_callable=AsyncMock,
            side_effect=InteractionConflictError("interaction still pending"),
        ), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as release_lock:
            get_pool.return_value.get_or_create = AsyncMock(return_value=mock_agent_service)
            response = await send_message_stream(
                "session-1",
                SendMessageRequest(content=[{"type": "text", "text": "new message"}]),
                user_id="testuser",
                db=mock_db,
            )
            chunks = [chunk async for chunk in response.body_iterator]

        body = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
        assert "INTERACTION_PENDING" in body
        assert "interaction still pending" in body
        assert "INTERNAL_ERROR" not in body
        release_lock.assert_called_once()

    async def test_first_message_waiting_stream_persists_and_emits_title(self):
        from src.agent.schema.agui_events import CustomEvent
        from src.api.routes.chat import send_message_stream
        from src.api.schemas.chat import SendMessageRequest

        mock_db = MagicMock()
        mock_session = MagicMock(
            id="session-1",
            user_id="testuser",
            status="active",
            model_id="model-1",
        )

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox
            from src.api.models.round import Round as RoundModel
            from src.api.models.run_cancel_request import RunCancelRequest as RunCancelRequestModel

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = None
            elif model is RoundModel:
                chain.filter.return_value.count.return_value = 0
            elif model is RunCancelRequestModel:
                chain.filter.return_value.first.return_value = None
            return chain

        mock_db.query.side_effect = query_side_effect
        title_session = MagicMock(id="session-1", title="新会话")
        title_db = MagicMock()
        title_db.query.return_value.filter.return_value.first.return_value = title_session
        title_db_context = MagicMock()
        title_db_context.__enter__.return_value = title_db
        title_db_context.__exit__.return_value = False

        mock_agent_service = MagicMock()
        mock_agent_service.generate_session_title = AsyncMock(return_value="恢复确认")

        async def waiting_events():
            yield CustomEvent(
                name="interaction_requested",
                value={
                    "interactionId": "interaction-title",
                    "runId": "round-title",
                    "kind": "user_input",
                },
            )

        execution = SimpleNamespace(
            handle=SimpleNamespace(run_id="round-title", session_id="session-1"),
            event_source=waiting_events(),
        )

        with patch("src.api.routes.chat.enforce_token_limits", return_value=None), patch(
            "src.api.routes.chat._acquire_lock_and_clear_cancel",
            new_callable=AsyncMock,
            return_value="lock-title",
        ), patch("src.api.routes.chat._resolve_session_model_for_user", return_value="model-1"), patch(
            "src.api.routes.chat._validate_turn_reasoning_request",
        ), patch("src.api.routes.chat.get_agent_pool") as get_pool, patch(
            "src.api.routes.chat._turn_orchestrator.submit_turn",
            new_callable=AsyncMock,
            return_value=execution,
        ), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "src.api.routes.chat.SessionLocal",
            return_value=title_db_context,
        ), patch(
            "src.api.routes.chat._agui_event_bus.publish_ephemeral",
            new_callable=AsyncMock,
        ) as publish_ephemeral:
            get_pool.return_value.get_or_create = AsyncMock(
                return_value=mock_agent_service,
            )
            response = await send_message_stream(
                "session-1",
                SendMessageRequest(content=[{"type": "text", "text": "hello"}]),
                user_id="testuser",
                db=mock_db,
            )
            chunks = [chunk async for chunk in response.body_iterator]

        body = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
        assert "title_updated" in body
        assert "恢复确认" in body
        assert title_session.title == "恢复确认"
        title_db.commit.assert_called_once()
        publish_ephemeral.assert_awaited_once()

    async def test_first_message_disconnect_still_fans_out_title_to_waiting_subscriber(self):
        from src.agent.schema.agui_events import CustomEvent
        from src.api.routes.chat import send_message_stream
        from src.api.schemas.chat import SendMessageRequest

        mock_db = MagicMock()
        mock_session = MagicMock(
            id="session-1",
            user_id="testuser",
            status="active",
            model_id="model-1",
        )

        def query_side_effect(model):
            from src.api.models.session import Session as SessionModel
            from src.api.models.user_sandbox import UserSandbox
            from src.api.models.round import Round as RoundModel
            from src.api.models.run_cancel_request import RunCancelRequest as RunCancelRequestModel

            chain = MagicMock()
            if model is SessionModel:
                chain.filter.return_value.first.return_value = mock_session
            elif model is UserSandbox:
                chain.filter.return_value.first.return_value = None
            elif model is RoundModel:
                chain.filter.return_value.count.return_value = 0
            elif model is RunCancelRequestModel:
                chain.filter.return_value.first.return_value = None
            return chain

        mock_db.query.side_effect = query_side_effect
        title_session = MagicMock(id="session-1", title="新会话")
        title_db = MagicMock()
        title_db.query.return_value.filter.return_value.first.return_value = title_session
        title_db_context = MagicMock()
        title_db_context.__enter__.return_value = title_db
        title_db_context.__exit__.return_value = False
        title_release = asyncio.Event()
        title_published = asyncio.Event()

        async def _generate_title(_source):
            await title_release.wait()
            return "断线后标题"

        mock_agent_service = MagicMock()
        mock_agent_service.generate_session_title = AsyncMock(
            side_effect=_generate_title,
        )

        async def waiting_events():
            yield CustomEvent(
                name="interaction_requested",
                value={
                    "interactionId": "interaction-title-disconnect",
                    "runId": "round-title-disconnect",
                    "kind": "user_input",
                },
            )

        execution = SimpleNamespace(
            handle=SimpleNamespace(
                run_id="round-title-disconnect",
                session_id="session-1",
            ),
            event_source=waiting_events(),
        )

        async def _publish_title(_run_id, _event):
            title_published.set()

        with patch("src.api.routes.chat.enforce_token_limits", return_value=None), patch(
            "src.api.routes.chat._acquire_lock_and_clear_cancel",
            new_callable=AsyncMock,
            return_value="lock-title-disconnect",
        ), patch("src.api.routes.chat._resolve_session_model_for_user", return_value="model-1"), patch(
            "src.api.routes.chat._validate_turn_reasoning_request",
        ), patch("src.api.routes.chat.get_agent_pool") as get_pool, patch(
            "src.api.routes.chat._turn_orchestrator.submit_turn",
            new_callable=AsyncMock,
            return_value=execution,
        ), patch(
            "src.api.routes.chat._release_user_run_lock_in_new_session",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "src.api.routes.chat.SessionLocal",
            return_value=title_db_context,
        ), patch(
            "src.api.routes.chat._agui_event_bus.publish_ephemeral",
            new_callable=AsyncMock,
            side_effect=_publish_title,
        ) as publish_ephemeral:
            get_pool.return_value.get_or_create = AsyncMock(
                return_value=mock_agent_service,
            )
            response = await send_message_stream(
                "session-1",
                SendMessageRequest(content=[{"type": "text", "text": "hello"}]),
                user_id="testuser",
                db=mock_db,
            )
            first_chunk = await response.body_iterator.__anext__()
            assert "interaction_requested" in (
                first_chunk.decode() if isinstance(first_chunk, bytes) else first_chunk
            )
            await response.body_iterator.aclose()
            title_release.set()
            await asyncio.wait_for(title_published.wait(), timeout=1)

        assert title_session.title == "断线后标题"
        title_db.commit.assert_called_once()
        publish_ephemeral.assert_awaited_once()
        assert publish_ephemeral.await_args.args[0] == "round-title-disconnect"

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
