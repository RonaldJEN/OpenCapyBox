"""测试服务器启动时清理残留 running 轮次的逻辑"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.models.database import Base
from src.api.models.round import Round
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

    def test_startup_preserves_all_running_cron_without_owner_lease(self, in_memory_db):
        """Age alone must not kill either fresh or long-running cron work."""
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
            assert cleaned == 0

            old_run_db = db.query(CronJobRun).filter(CronJobRun.id == old_run.id).first()
            recent_run_db = db.query(CronJobRun).filter(CronJobRun.id == recent_run.id).first()
            success_run_db = db.query(CronJobRun).filter(CronJobRun.id == success_run.id).first()
            failed_run_db = db.query(CronJobRun).filter(CronJobRun.id == failed_run.id).first()

            assert old_run_db.status == "running"
            assert old_run_db.output is None
            assert old_run_db.completed_at is None
            assert recent_run_db.status == "running"
            assert recent_run_db.output is None
            assert recent_run_db.completed_at is None
            assert success_run_db.status == "success"
            assert success_run_db.output == "done"
            assert success_run_db.completed_at == success_run.completed_at
            assert failed_run_db.status == "failed"
            assert failed_run_db.output == "already failed"
            assert failed_run_db.completed_at == failed_run.completed_at
