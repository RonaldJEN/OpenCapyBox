"""测试服务器启动时清理残留 running 轮次的逻辑"""
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.models.agui_event import AGUIEventLog
from src.api.models.agent_interaction import AgentInteraction
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.user_run_lock import UserRunLock
from src.api.models.user_memory import CronJobRun
from src.api.models.tool_permission import ToolApprovalRequest
from src.api.main import cleanup_stale_runtime_state
from src.api.services.tool_permission_service import (
    APPROVAL_OUTCOME_UNKNOWN_ERROR,
    ToolRef,
    claim_approval_request,
    create_approval_request,
)
from tests.db_safety import (
    build_pytest_pg_engine,
    create_all_for_test_engine,
    reset_all_tables,
)


_PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture
def in_memory_db():
    """创建内存 SQLite 数据库用于测试"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


class TestStartupCleanup:
    """测试启动时清理残留 running 轮次"""

    def test_stale_running_rounds_marked_failed(self, in_memory_db):
        """Only expired-lock and old orphan rounds are reclaimed."""
        from datetime import timedelta
        from src.api.utils.timezone import now_naive

        engine, Session = in_memory_db
        with Session() as db:
            old = now_naive() - timedelta(hours=2)
            # 插入 2 个 running 和 1 个 completed 轮次
            r1 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="hi", status="running", created_at=old)
            r2 = Round(id=str(uuid.uuid4()), session_id="s2", user_message="hello", status="running", created_at=old)
            r3 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="done", status="completed")
            lock = UserRunLock(
                user_id="u1",
                session_id="s1",
                lock_id=str(uuid.uuid4()),
                created_at=old,
                updated_at=old,
            )
            db.add_all([r1, r2, r3, lock])
            db.commit()

            # 模拟启动清理逻辑
            stale_count, zombie_count, stale_lock_count, stale_cron_count = cleanup_stale_runtime_state(db)

            assert stale_count == 2
            assert zombie_count == 0
            assert stale_lock_count == 1
            assert stale_cron_count == 0

            # 验证状态
            all_rounds = db.query(Round).all()
            running = [r for r in all_rounds if r.status == "running"]
            failed = [r for r in all_rounds if r.status == "failed"]
            completed = [r for r in all_rounds if r.status == "completed"]

            assert len(running) == 0
            assert len(failed) == 2
            assert len(completed) == 1

            # 验证 failed 轮次有正确的 final_response 和 completed_at
            for r in failed:
                assert r.final_response == "[运行心跳已过期，执行被中断]"
                assert r.completed_at is not None

            # 验证用户运行锁已清空
            assert db.query(UserRunLock).count() == 0

    def test_startup_restores_stale_lock_for_active_waiting_continuation(
        self,
        in_memory_db,
    ):
        from datetime import timedelta

        from src.api.utils.timezone import now_naive

        _, Session = in_memory_db
        with Session() as db:
            old = now_naive() - timedelta(hours=2)
            run_id = "active-waiting-round"
            db.add_all([
                Round(
                    id=run_id,
                    session_id="active-waiting-session",
                    user_message="continue",
                    status="waiting_interaction",
                    created_at=old,
                ),
                AgentInteraction(
                    id="active-waiting-interaction",
                    session_id="active-waiting-session",
                    round_id=run_id,
                    kind="user_input",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"Continue?":"Yes"}',
                    tool_result_content="Continue?: Yes",
                    claim_token="live-continuation",
                    claim_lease_expires_at=now_naive() + timedelta(minutes=10),
                ),
                UserRunLock(
                    user_id="active-waiting-user",
                    session_id="active-waiting-session",
                    lock_id="active-waiting-lock",
                    created_at=old,
                    updated_at=old,
                ),
            ])
            db.commit()

            stale_count, _, stale_lock_count, _ = cleanup_stale_runtime_state(db)

            db.expire_all()
            assert stale_count == 0
            assert stale_lock_count == 0
            assert db.get(UserRunLock, "active-waiting-lock") is not None
            assert db.get(Round, run_id).status == "waiting_interaction"
            assert (
                db.get(AgentInteraction, "active-waiting-interaction").status
                == "pending"
            )
            assert (
                db.query(AGUIEventLog)
                .filter(AGUIEventLog.run_id == run_id)
                .count()
                == 0
            )

    def test_startup_preserves_active_continuation_when_lock_row_is_missing(
        self,
        in_memory_db,
    ):
        from datetime import timedelta

        from src.api.utils.timezone import now_naive

        _, Session = in_memory_db
        with Session() as db:
            old = now_naive() - timedelta(hours=2)
            run_id = "missing-lock-active-round"
            db.add_all([
                Round(
                    id=run_id,
                    session_id="missing-lock-active-session",
                    user_message="continue",
                    status="running",
                    created_at=old,
                ),
                AgentInteraction(
                    id="missing-lock-active-interaction",
                    session_id="missing-lock-active-session",
                    round_id=run_id,
                    kind="user_input",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"Continue?":"Yes"}',
                    tool_result_content="Continue?: Yes",
                    claim_token="live-missing-lock-worker",
                    claim_lease_expires_at=now_naive() + timedelta(minutes=10),
                    continuation_started_at=old,
                ),
            ])
            db.commit()

            stale_count, _, stale_lock_count, _ = cleanup_stale_runtime_state(db)

            db.expire_all()
            assert stale_count == 0
            assert stale_lock_count == 0
            assert db.get(Round, run_id).status == "running"
            assert (
                db.get(
                    AgentInteraction,
                    "missing-lock-active-interaction",
                ).claim_token
                == "live-missing-lock-worker"
            )
            assert db.query(UserRunLock).count() == 0

    def test_startup_fences_expired_waiting_claim_when_lock_row_is_missing(
        self,
        in_memory_db,
    ):
        from datetime import timedelta

        from src.api.utils.timezone import now_naive

        _, Session = in_memory_db
        with Session() as db:
            old = now_naive() - timedelta(hours=2)
            run_id = "missing-lock-expired-waiting-round"
            db.add_all([
                Round(
                    id=run_id,
                    session_id="missing-lock-expired-waiting-session",
                    user_message="continue",
                    status="waiting_interaction",
                    created_at=old,
                ),
                AgentInteraction(
                    id="missing-lock-expired-waiting-interaction",
                    session_id="missing-lock-expired-waiting-session",
                    round_id=run_id,
                    kind="user_input",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"Continue?":"Yes"}',
                    tool_result_content="Continue?: Yes",
                    claim_token="expired-missing-lock-worker",
                    claim_lease_expires_at=old + timedelta(seconds=30),
                ),
            ])
            db.commit()

            stale_count, _, stale_lock_count, _ = cleanup_stale_runtime_state(db)

            db.expire_all()
            interaction = db.get(
                AgentInteraction,
                "missing-lock-expired-waiting-interaction",
            )
            assert stale_count == 0
            assert stale_lock_count == 0
            assert db.get(Round, run_id).status == "waiting_interaction"
            assert interaction.claim_token is None
            assert interaction.claim_lease_expires_at is None

    def test_startup_restores_stale_lock_for_active_tool_execution(
        self,
        in_memory_db,
    ):
        from datetime import timedelta

        from src.api.utils.timezone import now_naive

        _, Session = in_memory_db
        with Session() as db:
            old = now_naive() - timedelta(hours=2)
            run_id = "active-execution-round"
            db.add_all([
                Round(
                    id=run_id,
                    session_id="active-execution-session",
                    user_message="continue",
                    status="running",
                    created_at=old,
                ),
                AgentInteraction(
                    id="active-execution-interaction",
                    session_id="active-execution-session",
                    round_id=run_id,
                    kind="tool_approval",
                    tool_call_id="active-execution-call",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"approval":"allow_once"}',
                    tool_result_content="[Tool approval execution pending]",
                    claim_token="expired-continuation",
                    claim_lease_expires_at=old + timedelta(seconds=30),
                    continuation_started_at=old,
                ),
                ToolApprovalRequest(
                    id="active-execution-interaction",
                    user_id="active-execution-user",
                    session_id="active-execution-session",
                    run_id=run_id,
                    tool_call_id="active-execution-call",
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
                    execution_lease_expires_at=now_naive() + timedelta(minutes=10),
                ),
                UserRunLock(
                    user_id="active-execution-user",
                    session_id="active-execution-session",
                    lock_id="active-execution-lock",
                    created_at=old,
                    updated_at=old,
                ),
            ])
            db.commit()

            stale_count, _, stale_lock_count, _ = cleanup_stale_runtime_state(db)

            db.expire_all()
            approval = db.get(
                ToolApprovalRequest,
                "active-execution-interaction",
            )
            assert stale_count == 0
            assert stale_lock_count == 0
            assert db.get(UserRunLock, "active-execution-lock") is not None
            assert db.get(Round, run_id).status == "running"
            assert approval.status == "executing"
            assert approval.execution_claim_token == "live-execution"
            assert (
                db.query(AGUIEventLog)
                .filter(AGUIEventLog.run_id == run_id)
                .count()
                == 0
            )

    def test_startup_fails_expired_started_continuation_without_repark(
        self,
        in_memory_db,
    ):
        from datetime import timedelta

        from src.api.utils.timezone import now_naive

        _, Session = in_memory_db
        with Session() as db:
            old = now_naive() - timedelta(hours=2)
            run_id = "stale-interaction-round"
            db.add_all([
                Round(
                    id=run_id,
                    session_id="stale-interaction-session",
                    user_message="continue",
                    status="running",
                    created_at=old,
                ),
                AgentInteraction(
                    id="stale-interaction",
                    session_id="stale-interaction-session",
                    round_id=run_id,
                    kind="tool_approval",
                    tool_call_id="tool-ask",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"approval":"allow_once"}',
                    tool_result_content="[Tool approval execution pending]",
                    claim_token="dead-worker-claim",
                    claim_lease_expires_at=old + timedelta(seconds=30),
                    continuation_started_at=old,
                ),
                ToolApprovalRequest(
                    id="stale-interaction",
                    user_id="stale-interaction-user",
                    session_id="stale-interaction-session",
                    run_id=run_id,
                    tool_call_id="tool-ask",
                    provider="builtin",
                    tool_name="shell_exec",
                    model_tool_name="shell_exec",
                    arguments_encrypted="encrypted",
                    arguments_hash="hash-safe",
                    status="approved",
                    resolution="allow_once",
                    resolved_at=old,
                ),
                UserRunLock(
                    user_id="stale-interaction-user",
                    session_id="stale-interaction-session",
                    lock_id="stale-interaction-lock",
                    created_at=old,
                    updated_at=old,
                ),
            ])
            db.commit()

            stale_count, _, stale_lock_count, _ = cleanup_stale_runtime_state(db)

            db.expire_all()
            round_obj = db.get(Round, run_id)
            interaction = db.get(AgentInteraction, "stale-interaction")
            approval = db.get(ToolApprovalRequest, "stale-interaction")
            terminal_events = (
                db.query(AGUIEventLog)
                .filter(AGUIEventLog.run_id == run_id)
                .order_by(AGUIEventLog.sequence)
                .all()
            )
            assert stale_count == 1
            assert stale_lock_count == 1
            assert round_obj.status == "failed"
            assert interaction.status == "failed"
            assert interaction.resolved_at is not None
            assert interaction.claim_token is None
            assert interaction.claim_lease_expires_at is None
            assert approval.status == "cancelled"
            assert len(terminal_events) == 1
            assert terminal_events[0].event_type == "RUN_ERROR"

    def test_startup_fails_irrecoverable_post_dispatch_continuation(
        self,
        in_memory_db,
    ):
        from datetime import timedelta

        from src.api.utils.timezone import now_naive

        _, Session = in_memory_db
        with Session() as db:
            old = now_naive() - timedelta(hours=2)
            run_id = "stale-tool-round"
            db.add_all([
                Round(
                    id=run_id,
                    session_id="stale-tool-session",
                    user_message="continue",
                    status="running",
                    created_at=old,
                ),
                AgentInteraction(
                    id="stale-tool-interaction",
                    session_id="stale-tool-session",
                    round_id=run_id,
                    kind="tool_approval",
                    tool_call_id="tool-call",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"approval":"allow_once"}',
                    tool_result_content="[Tool approval execution pending]",
                    claim_token="dead-worker-claim",
                    claim_lease_expires_at=old + timedelta(seconds=30),
                    continuation_started_at=old,
                ),
                ToolApprovalRequest(
                    id="stale-tool-interaction",
                    user_id="stale-tool-user",
                    session_id="stale-tool-session",
                    run_id=run_id,
                    tool_call_id="tool-call",
                    provider="builtin",
                    tool_name="shell_exec",
                    model_tool_name="shell_exec",
                    arguments_encrypted="encrypted",
                    arguments_hash="hash",
                    status="executing",
                    resolution="allow_once",
                    resolved_at=old,
                    execution_started_at=old,
                    execution_claim_token="dead-execution",
                    execution_lease_expires_at=old + timedelta(seconds=30),
                ),
                UserRunLock(
                    user_id="stale-tool-user",
                    session_id="stale-tool-session",
                    lock_id="stale-tool-lock",
                    created_at=old,
                    updated_at=old,
                ),
            ])
            db.commit()

            stale_count, _, stale_lock_count, _ = cleanup_stale_runtime_state(db)

            db.expire_all()
            assert stale_count == 1
            assert stale_lock_count == 1
            round_obj = db.get(Round, run_id)
            assert round_obj.status == "failed"
            assert "工具审批续跑进程中断" in round_obj.final_response
            assert db.get(AgentInteraction, "stale-tool-interaction").status == "failed"
            approval = db.get(ToolApprovalRequest, "stale-tool-interaction")
            assert approval.status == "unknown"
            assert approval.error == APPROVAL_OUTCOME_UNKNOWN_ERROR
            assert approval.execution_claim_token is None
            assert approval.execution_lease_expires_at is None
            terminal_events = (
                db.query(AGUIEventLog)
                .filter(AGUIEventLog.run_id == run_id)
                .all()
            )
            assert len(terminal_events) == 1
            assert terminal_events[0].event_type == "RUN_ERROR"

    def test_no_stale_rounds_noop(self, in_memory_db):
        """没有 running 轮次时，清理不影响已有数据"""
        engine, Session = in_memory_db
        with Session() as db:
            r1 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="ok", status="completed")
            db.add(r1)
            db.commit()

            stale_count, zombie_count, stale_lock_count, stale_cron_count = cleanup_stale_runtime_state(db)

            assert stale_count == 0
            assert zombie_count == 0
            assert stale_lock_count == 0
            assert stale_cron_count == 0

            # completed 轮次不受影响
            r = db.query(Round).first()
            assert r.status == "completed"
            assert r.final_response is None

    def test_completed_rounds_untouched(self, in_memory_db):
        """清理只影响 running 状态，不影响 completed/failed"""
        from datetime import timedelta
        from src.api.utils.timezone import now_naive

        engine, Session = in_memory_db
        with Session() as db:
            old = now_naive() - timedelta(hours=2)
            r1 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="a", status="completed",
                       final_response="done")
            r2 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="b", status="failed",
                       final_response="error")
            r3 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="c", status="running", created_at=old)
            lock = UserRunLock(
                user_id="u2",
                session_id="s1",
                lock_id=str(uuid.uuid4()),
                created_at=old,
                updated_at=old,
            )
            db.add_all([r1, r2, r3, lock])
            db.commit()

            stale_count, zombie_count, stale_lock_count, stale_cron_count = cleanup_stale_runtime_state(db)

            assert stale_count == 1
            assert zombie_count == 0
            assert stale_lock_count == 1
            assert stale_cron_count == 0

            # 验证原有 completed/failed 未被修改
            r1_db = db.query(Round).filter(Round.id == r1.id).first()
            assert r1_db.final_response == "done"
            assert r1_db.status == "completed"

            r2_db = db.query(Round).filter(Round.id == r2.id).first()
            assert r2_db.final_response == "error"
            assert r2_db.status == "failed"

            # 验证用户运行锁已清空
            assert db.query(UserRunLock).count() == 0

    def test_rolling_worker_preserves_fresh_lock_and_round(self, in_memory_db):
        """A fresh heartbeat owned by another worker is never touched."""
        from datetime import timedelta
        from src.api.utils.timezone import now_naive

        engine, Session = in_memory_db
        with Session() as db:
            now = now_naive()
            old = now - timedelta(hours=2)
            fresh_round = Round(
                id=str(uuid.uuid4()),
                session_id="s-fresh",
                user_message="still running",
                status="running",
                created_at=old,
            )
            stale_round = Round(
                id=str(uuid.uuid4()),
                session_id="s-stale",
                user_message="abandoned",
                status="running",
                created_at=old,
            )
            fresh_orphan_round = Round(
                id=str(uuid.uuid4()),
                session_id="s-init-window",
                user_message="round created before its lock",
                status="running",
                created_at=now,
            )
            fresh_lock = UserRunLock(
                user_id="u1",
                session_id="s-fresh",
                lock_id=str(uuid.uuid4()),
                created_at=old,
                updated_at=now,
            )
            stale_lock = UserRunLock(
                user_id="u2",
                session_id="s-stale",
                lock_id=str(uuid.uuid4()),
                created_at=old,
                updated_at=old,
            )
            db.add_all([
                fresh_round,
                stale_round,
                fresh_orphan_round,
                fresh_lock,
                stale_lock,
            ])
            db.commit()

            stale_count, zombie_count, stale_lock_count, stale_cron_count = cleanup_stale_runtime_state(db)

            assert stale_count == 1
            assert zombie_count == 0
            assert stale_lock_count == 1
            assert stale_cron_count == 0
            assert db.query(UserRunLock).count() == 1
            assert db.get(UserRunLock, fresh_lock.lock_id) is not None
            assert db.get(Round, fresh_round.id).status == "running"
            assert db.get(Round, fresh_orphan_round.id).status == "running"
            assert db.get(Round, stale_round.id).status == "failed"

    def test_fresh_lock_race_does_not_rollback_prior_session_cleanup(self):
        """A fresh lock for one session must not undo another session's cleanup."""
        from datetime import timedelta

        from src.api.services.agent_interaction_service import AgentInteractionService
        from src.api.utils.timezone import now_naive

        engine = build_pytest_pg_engine(_PROJECT_ROOT)
        create_all_for_test_engine(engine, Base.metadata)
        reset_all_tables(engine, Base.metadata)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        now = now_naive()
        old = now - timedelta(minutes=10)

        try:
            with factory() as seed:
                seed.add_all([
                    Session(
                        id="session-expired-claim",
                        user_id="user-expired-claim",
                        status="active",
                        model_id="model-test",
                    ),
                    Session(
                        id="session-fresh-race",
                        user_id="user-fresh-race",
                        status="active",
                        model_id="model-test",
                    ),
                ])
                seed.commit()
                seed.add_all([
                    Round(
                        id="round-expired-claim",
                        session_id="session-expired-claim",
                        user_message="continue",
                        status="waiting_interaction",
                        created_at=old,
                    ),
                    Round(
                        id="round-fresh-race",
                        session_id="session-fresh-race",
                        user_message="running",
                        status="running",
                        created_at=old,
                    ),
                    UserRunLock(
                        user_id="user-expired-claim",
                        session_id="session-expired-claim",
                        lock_id="stale-expired-claim",
                        created_at=old,
                        updated_at=old,
                    ),
                ])
                seed.commit()
                seed.add(AgentInteraction(
                    id="interaction-expired-claim",
                    session_id="session-expired-claim",
                    round_id="round-expired-claim",
                    kind="user_input",
                    status="pending",
                    request_payload="{}",
                    answer_payload='{"Continue?":"Yes"}',
                    tool_result_content="Continue?: Yes",
                    claim_token="expired-claim",
                    claim_lease_expires_at=old,
                ))
                seed.commit()

            original_lock = (
                AgentInteractionService.lock_running_round_for_terminal_cleanup
            )
            fresh_lock_inserted = False

            def insert_fresh_lock_before_recheck(db, *, session_id, round_id):
                nonlocal fresh_lock_inserted
                locked_round = original_lock(
                    db,
                    session_id=session_id,
                    round_id=round_id,
                )
                if round_id == "round-fresh-race" and not fresh_lock_inserted:
                    fresh_lock_inserted = True
                    with factory() as other:
                        other.add(UserRunLock(
                            user_id="user-fresh-race",
                            session_id="session-fresh-race",
                            lock_id="fresh-race-lock",
                            created_at=now_naive(),
                            updated_at=now_naive(),
                        ))
                        other.commit()
                return locked_round

            with patch.object(
                AgentInteractionService,
                "lock_running_round_for_terminal_cleanup",
                side_effect=insert_fresh_lock_before_recheck,
            ):
                with factory() as cleanup_db:
                    result = cleanup_stale_runtime_state(cleanup_db)

            assert result[2] == 1
            with factory() as verify:
                interaction = verify.get(
                    AgentInteraction,
                    "interaction-expired-claim",
                )
                assert verify.get(UserRunLock, "stale-expired-claim") is None
                assert interaction.claim_token is None
                assert interaction.claim_lease_expires_at is None
                assert verify.get(UserRunLock, "fresh-race-lock") is not None
                assert verify.get(Round, "round-fresh-race").status == "running"
        finally:
            reset_all_tables(engine, Base.metadata)
            engine.dispose()

    def test_startup_only_reconciles_expired_approval_leases(self, in_memory_db):
        """A different worker's live lease survives; abandoned work is unknown."""
        from datetime import timedelta
        from src.api.utils.timezone import now_naive

        _, Session = in_memory_db
        with Session() as db:
            for suffix in ("active", "expired"):
                create_approval_request(
                    db,
                    request_id=f"approval-{suffix}",
                    user_id="alice",
                    session_id="session-a",
                    run_id=f"run-{suffix}",
                    tool_call_id=f"call-{suffix}",
                    ref=ToolRef(provider="builtin", tool_name="shell_exec"),
                    model_tool_name="shell_exec",
                    arguments={"command": "pwd"},
                )
                claim_approval_request(
                    db,
                    request_id=f"approval-{suffix}",
                    user_id="alice",
                    resolution="allow_once",
                )
            expired = db.get(ToolApprovalRequest, "approval-expired")
            expired.execution_lease_expires_at = now_naive() - timedelta(seconds=1)
            db.commit()

            cleanup_stale_runtime_state(db)

            active = db.get(ToolApprovalRequest, "approval-active")
            expired = db.get(ToolApprovalRequest, "approval-expired")
            assert active.status == "executing"
            assert active.execution_claim_token
            assert active.execution_lease_expires_at is not None
            assert expired.status == "unknown"
            assert expired.error == APPROVAL_OUTCOME_UNKNOWN_ERROR
            assert expired.execution_claim_token is None

    def test_startup_marks_all_running_cron_failed(self, in_memory_db):
        """A restart terminates every in-memory cron execution."""
        from datetime import timedelta
        from src.api.utils.timezone import now_naive

        engine, Session = in_memory_db
        with Session() as db:
            now = now_naive()
            old_run = CronJobRun(
                id=str(uuid.uuid4()),
                user_id="u1",
                job_name="old",
                cron_expr="* * * * *",
                status="running",
                output=None,
                is_read=False,
                started_at=now - timedelta(hours=2),
            )
            recent_run = CronJobRun(
                id=str(uuid.uuid4()),
                user_id="u1",
                job_name="recent",
                cron_expr="* * * * *",
                status="running",
                output=None,
                is_read=False,
                started_at=now - timedelta(minutes=1),
            )
            success_run = CronJobRun(
                id=str(uuid.uuid4()),
                user_id="u1",
                job_name="success",
                cron_expr="* * * * *",
                status="success",
                output="done",
                is_read=False,
                started_at=now - timedelta(minutes=20),
                completed_at=now - timedelta(minutes=19),
            )
            failed_run = CronJobRun(
                id=str(uuid.uuid4()),
                user_id="u1",
                job_name="failed",
                cron_expr="* * * * *",
                status="failed",
                output="already failed",
                is_read=False,
                started_at=now - timedelta(minutes=10),
                completed_at=now - timedelta(minutes=9),
            )
            db.add_all([old_run, recent_run, success_run, failed_run])
            db.commit()

            stale_count, zombie_count, stale_lock_count, cleaned = cleanup_stale_runtime_state(db)
            assert stale_count == 0
            assert zombie_count == 0
            assert stale_lock_count == 0
            assert cleaned == 2

            old_run_db = db.query(CronJobRun).filter(CronJobRun.id == old_run.id).first()
            recent_run_db = db.query(CronJobRun).filter(CronJobRun.id == recent_run.id).first()
            success_run_db = db.query(CronJobRun).filter(CronJobRun.id == success_run.id).first()
            failed_run_db = db.query(CronJobRun).filter(CronJobRun.id == failed_run.id).first()

            assert old_run_db.status == "failed"
            assert old_run_db.output == "[服务重启，定时任务执行被中断]"
            assert old_run_db.completed_at is not None
            assert recent_run_db.status == "failed"
            assert recent_run_db.output == "[服务重启，定时任务执行被中断]"
            assert recent_run_db.completed_at is not None
            assert success_run_db.status == "success"
            assert success_run_db.output == "done"
            assert success_run_db.completed_at == success_run.completed_at
            assert failed_run_db.status == "failed"
            assert failed_run_db.output == "already failed"
            assert failed_run_db.completed_at == failed_run.completed_at
