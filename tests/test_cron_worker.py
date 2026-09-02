"""cron_worker 单元测试。"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.api.services.cron_worker as cron_worker
from src.api.models.auth_user import AuthUser
from src.api.models.cron_fire import CronFire
from src.api.models.cron_job import CronJob
from src.api.models.database import Base
from src.api.models.user_memory import CronJobRun
from src.api.models.user_sandbox import UserSandbox


@pytest.fixture(autouse=True)
def clear_background_tasks():
    cron_worker._background_tasks.clear()
    cron_worker._local_run_ids.clear()
    yield
    for task in list(cron_worker._background_tasks):
        if not task.done():
            task.cancel()
    cron_worker._background_tasks.clear()
    cron_worker._local_run_ids.clear()


@pytest.fixture
def cron_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cron_worker.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(cron_worker, "SessionLocal", Session)
    try:
        yield Session
    finally:
        engine.dispose()


def _insert_job(
    session_factory,
    *,
    user_id: str,
    name: str,
    cron_expr: str,
    enabled: bool = True,
    user_enabled: bool = True,
) -> CronJob:
    with session_factory() as db:
        if not db.query(AuthUser).filter(AuthUser.user_id == user_id).first():
            db.add(
                AuthUser(
                    user_id=user_id,
                    username=user_id,
                    auth_type="simple",
                    password_hash="hash",
                    enabled=user_enabled,
                    is_admin=False,
                    created_by="test",
                )
            )
        job = CronJob(
            user_id=user_id,
            name=name,
            cron_expr=cron_expr,
            description="test",
            enabled=enabled,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


class TestCronMatching:
    def test_cron_matches_current_minute(self):
        minute = time.gmtime()
        dt = datetime(
            minute.tm_year,
            minute.tm_mon,
            minute.tm_mday,
            4,
            0,
            0,
        )

        assert cron_worker._cron_matches_minute("* * * * *", dt) is True
        assert cron_worker._cron_matches_minute("0 4 * * *", dt) is True
        assert cron_worker._cron_matches_minute("1 4 * * *", dt) is False

    @pytest.mark.parametrize(
        ("expr", "when", "expected"),
        [
            ("0 9 * * 1-5", datetime(2026, 7, 27, 9), True),   # 周一
            ("0 9 * * 1-5", datetime(2026, 7, 25, 9), False),  # 周六
            ("0 9 * * 1-6", datetime(2026, 7, 25, 9), True),   # 周六
            ("0 9 * * 2-6,0", datetime(2026, 7, 27, 9), False),
            ("0 9 * * 2-6,0", datetime(2026, 7, 26, 9), True),  # 周日
            ("0 9 * * 7", datetime(2026, 7, 26, 9), True),
        ],
    )
    def test_standard_linux_weekdays(self, expr, when, expected):
        assert cron_worker._cron_matches_minute(expr, when) is expected


class TestDispatchCatchUp:
    def test_due_minutes_after_includes_all_missed_minutes(self):
        last = datetime(2026, 7, 8, 8, 59, 32)
        current = datetime(2026, 7, 8, 9, 2, 7)

        due, dropped = cron_worker._due_minutes_after(last, current, max_catch_up_minutes=60)

        assert dropped == 0
        assert due == [
            datetime(2026, 7, 8, 9, 0),
            datetime(2026, 7, 8, 9, 1),
            datetime(2026, 7, 8, 9, 2),
        ]

    def test_due_minutes_after_caps_large_backlog(self):
        last = datetime(2026, 7, 8, 8, 0)
        current = datetime(2026, 7, 8, 10, 5)

        due, dropped = cron_worker._due_minutes_after(last, current, max_catch_up_minutes=3)

        assert dropped == 122
        assert due == [
            datetime(2026, 7, 8, 10, 3),
            datetime(2026, 7, 8, 10, 4),
            datetime(2026, 7, 8, 10, 5),
        ]

    @pytest.mark.asyncio
    async def test_dispatch_due_minutes_replays_missed_ticks(self, monkeypatch):
        dispatched = []

        async def fake_dispatch(worker_id, minute):
            dispatched.append((worker_id, minute))

        monkeypatch.setattr(cron_worker, "_dispatch_and_run", fake_dispatch)

        last, cleanup_date = await cron_worker._dispatch_due_minutes(
            "w1",
            datetime(2026, 7, 8, 8, 59),
            datetime(2026, 7, 8, 9, 2),
            None,
            60,
        )

        assert [minute for _, minute in dispatched] == [
            datetime(2026, 7, 8, 9, 0),
            datetime(2026, 7, 8, 9, 1),
            datetime(2026, 7, 8, 9, 2),
        ]
        assert last == datetime(2026, 7, 8, 9, 2)
        assert cleanup_date is None

    @pytest.mark.asyncio
    async def test_dispatch_due_minutes_runs_midnight_cleanup_once(self, monkeypatch):
        dispatched = []
        cleanup_calls = []

        async def fake_dispatch(worker_id, minute):
            dispatched.append(minute)

        def fake_cleanup():
            cleanup_calls.append("cleanup")

        monkeypatch.setattr(cron_worker, "_dispatch_and_run", fake_dispatch)
        monkeypatch.setattr(cron_worker, "_cleanup_old_fires", fake_cleanup)

        last, cleanup_date = await cron_worker._dispatch_due_minutes(
            "w1",
            datetime(2026, 7, 7, 23, 59),
            datetime(2026, 7, 8, 0, 1),
            None,
            60,
        )

        assert dispatched == [
            datetime(2026, 7, 8, 0, 0),
            datetime(2026, 7, 8, 0, 1),
        ]
        assert cleanup_calls == ["cleanup"]
        assert last == datetime(2026, 7, 8, 0, 1)
        assert cleanup_date == datetime(2026, 7, 8).date()


class TestInsertFire:
    def test_try_insert_fire_succeeds_when_unique(self, cron_db):
        job = _insert_job(
            cron_db,
            user_id="u1",
            name="job-1",
            cron_expr="* * * * *",
        )
        minute = datetime.utcnow().replace(second=0, microsecond=0)

        assert cron_worker._try_insert_fire(job.id, minute) is True

    def test_try_insert_fire_fails_when_duplicate(self, cron_db):
        job = _insert_job(
            cron_db,
            user_id="u1",
            name="job-dup",
            cron_expr="* * * * *",
        )
        minute = datetime.utcnow().replace(second=0, microsecond=0)

        assert cron_worker._try_insert_fire(job.id, minute) is True
        assert cron_worker._try_insert_fire(job.id, minute) is False

    def test_fire_and_queued_run_are_created_atomically_with_frozen_definition(self, cron_db):
        job = _insert_job(
            cron_db,
            user_id="u1",
            name="workspace-job",
            cron_expr="* * * * *",
        )
        with cron_db() as db:
            persisted = db.get(CronJob, job.id)
            persisted.content = "更新工作区日报"
            persisted.definition_version = 3
            db.commit()

        minute = datetime.utcnow().replace(second=0, microsecond=0)
        run_id = cron_worker._enqueue_scheduled_run(job.id, minute, 1, 3)

        assert run_id is not None
        with cron_db() as db:
            fire = db.query(CronFire).one()
            run = db.get(CronJobRun, run_id)
            assert fire.run_id == run_id
            assert run is not None
            assert run.fire_id == fire.id
            assert run.status == "queued"
            assert run.phase == "queued"
            snapshot = json.loads(run.definition_snapshot)
            assert snapshot["content"] == "更新工作区日报"
            assert "workspace_access" not in snapshot

        assert cron_worker._enqueue_scheduled_run(job.id, minute, 1, 3) is None
        with cron_db() as db:
            assert db.query(CronFire).count() == 1
            assert db.query(CronJobRun).count() == 1

    def test_fire_rolls_back_when_queued_run_creation_fails(
        self, cron_db, monkeypatch,
    ):
        job = _insert_job(
            cron_db,
            user_id="u1",
            name="atomic-failure",
            cron_expr="* * * * *",
        )
        real_model = cron_worker.CronJobRun

        def fail_run_model(*args, **kwargs):
            raise RuntimeError("simulated run construction failure")

        monkeypatch.setattr(cron_worker, "CronJobRun", fail_run_model)
        with pytest.raises(RuntimeError, match="construction failure"):
            cron_worker._enqueue_scheduled_run(
                job.id,
                datetime.utcnow().replace(second=0, microsecond=0),
            )
        monkeypatch.setattr(cron_worker, "CronJobRun", real_model)
        with cron_db() as db:
            assert db.query(CronFire).count() == 0
            assert db.query(CronJobRun).count() == 0

    def test_definition_edit_before_enqueue_uses_latest_snapshot_without_dropping_fire(
        self, cron_db,
    ):
        job = _insert_job(
            cron_db,
            user_id="u1",
            name="definition-race",
            cron_expr="* * * * *",
        )
        with cron_db() as db:
            persisted = db.get(CronJob, job.id)
            persisted.definition_version = 2
            persisted.content = "new prompt"
            db.commit()

        run_id = cron_worker._enqueue_scheduled_run(
            job.id,
            datetime.utcnow().replace(second=0, microsecond=0),
            rule_version=1,
            definition_version=1,
        )
        assert run_id is not None
        with cron_db() as db:
            run = db.get(CronJobRun, run_id)
            assert run.definition_version == 2
            assert json.loads(run.definition_snapshot)["content"] == "new prompt"


class TestDurableRunClaims:
    def _queued_run(self, cron_db) -> str:
        job = _insert_job(
            cron_db,
            user_id="u1",
            name=f"job-{time.time_ns()}",
            cron_expr="* * * * *",
        )
        run_id = cron_worker._enqueue_scheduled_run(
            job.id,
            datetime.utcnow().replace(second=0, microsecond=0),
        )
        assert run_id is not None
        return run_id

    def test_claim_is_fenced_and_renewable(self, cron_db):
        run_id = self._queued_run(cron_db)
        with cron_db() as db:
            db.add(UserSandbox(
                id="binding-u1",
                user_id="u1",
                sandbox_id="sbx-claim",
                status="active",
            ))
            db.commit()
        claim = cron_worker._claim_queued_run(run_id, "worker-a")
        assert claim is not None
        assert claim["sandbox_id"] == "sbx-claim"
        assert cron_worker._claim_queued_run(run_id, "worker-b") is None
        assert cron_worker._renew_run_claim(run_id, claim["claim_token"]) is True

        with cron_db() as db:
            run = db.get(CronJobRun, run_id)
            assert run.status == "running"
            assert run.phase == "preparing"
            assert run.claim_worker_id == "worker-a"
            assert run.sandbox_id == "sbx-claim"
            assert run.attempt_count == 1
            assert run.claim_lease_expires_at > run.heartbeat_at

    def test_expired_claim_cannot_be_resurrected_by_late_heartbeat(self, cron_db):
        run_id = self._queued_run(cron_db)
        claim = cron_worker._claim_queued_run(run_id, "worker-a")
        assert claim is not None
        with cron_db() as db:
            run = db.get(CronJobRun, run_id)
            run.claim_lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()

        assert cron_worker._renew_run_claim(run_id, claim["claim_token"]) is False

    def test_workspace_lease_and_change_summary_share_the_claim_fence(self, cron_db):
        from src.api.services.cron_service import (
            append_cron_workspace_change,
            assert_cron_workspace_lease,
        )

        run_id = self._queued_run(cron_db)
        claim = cron_worker._claim_queued_run(run_id, "worker-a")
        assert claim is not None
        with cron_db() as db:
            assert_cron_workspace_lease(
                db,
                user_id="u1",
                run_id=run_id,
                claim_token=claim["claim_token"],
            )
            with pytest.raises(PermissionError):
                assert_cron_workspace_lease(
                    db,
                    user_id="u1",
                    run_id=run_id,
                    claim_token="wrong",
                )

        change = {
            "mutation_id": "mutation-1",
            "entry_id": "entry-1",
            "operation": "updated",
            "revision": 2,
        }
        with cron_db() as db:
            assert append_cron_workspace_change(
                db,
                user_id="u1",
                run_id=run_id,
                claim_token=claim["claim_token"],
                change=change,
            ) is True
        with cron_db() as db:
            assert append_cron_workspace_change(
                db,
                user_id="u1",
                run_id=run_id,
                claim_token=claim["claim_token"],
                change=change,
            ) is True
            run = db.get(CronJobRun, run_id)
            assert json.loads(run.workspace_changes) == [change]

    def test_reconciler_repairs_workspace_changes_from_mutation_journal(self, cron_db):
        from src.api.models.workspace import WorkspaceEntry, WorkspaceMutation

        run_id = self._queued_run(cron_db)
        with cron_db() as db:
            entry = WorkspaceEntry(
                entry_id="entry-repair",
                user_id="u1",
                parent_key="",
                name="日报.md",
                kind="file",
                relative_path="日报.md",
                size_bytes=12,
                mime_type="text/markdown",
                sha256="a" * 64,
                revision=2,
                status="active",
            )
            mutation = WorkspaceMutation(
                mutation_id="mutation-repair",
                user_id="u1",
                entry_id=entry.entry_id,
                actor="cron",
                operation="publish_file",
                state="completed",
                result_status="UPDATED",
                idempotency_key="workspace-tool:repair",
                cron_job_id="1",
                cron_run_id=run_id,
                before_revision=1,
                after_revision=2,
                after_sha256="a" * 64,
            )
            db.add_all([entry, mutation])
            db.commit()

            assert cron_worker._reconcile_workspace_change_summaries(db) == 1
            db.commit()
            summary = json.loads(db.get(CronJobRun, run_id).workspace_changes)
            assert summary[0]["mutation_id"] == "mutation-repair"
            assert summary[0]["path"] == "日报.md"
            assert summary[0]["operation"] == "UPDATED"

            # Hard deletion leaves no entry row; replay metadata comes from the journal.
            db.delete(entry)
            db.add(WorkspaceMutation(
                mutation_id="mutation-delete", user_id="u1", entry_id="entry-repair",
                actor="cron", operation="delete_entries", state="completed",
                result_status="DELETED", idempotency_key="workspace-tool:delete",
                cron_job_id="1", cron_run_id=run_id, after_revision=0,
                details_json=json.dumps({"journal": {
                    "delete_entry_ids": ["entry-repair"],
                    "root_projections": [{
                        "entry_id": "entry-repair", "relative_path": "日报.md",
                        "name": "日报.md", "kind": "file", "revision": 2,
                    }],
                }}),
            ))
            db.commit()
            assert cron_worker._reconcile_workspace_change_summaries(db) == 1
            db.commit()
            deleted = json.loads(db.get(CronJobRun, run_id).workspace_changes)[-1]
            assert deleted["operation"] == "DELETED"
            assert deleted["path"] == deleted["name"] == "日报.md"
            assert deleted["revision"] == 3
            assert deleted["affected_entry_ids"] == ["entry-repair"]

    def test_reconcile_requeues_prestart_but_never_replays_started_work(self, cron_db):
        now = datetime.utcnow()
        preparing_id = self._queued_run(cron_db)
        executing_id = self._queued_run(cron_db)
        active_id = self._queued_run(cron_db)

        for run_id in (preparing_id, executing_id, active_id):
            claim = cron_worker._claim_queued_run(run_id, "worker-a")
            assert claim is not None
        with cron_db() as db:
            preparing = db.get(CronJobRun, preparing_id)
            executing = db.get(CronJobRun, executing_id)
            active = db.get(CronJobRun, active_id)
            preparing.claim_lease_expires_at = now - timedelta(seconds=1)
            executing.phase = "executing"
            executing.claim_lease_expires_at = now - timedelta(seconds=1)
            active.phase = "executing"
            active.claim_lease_expires_at = now + timedelta(minutes=5)
            db.commit()

        assert cron_worker.reconcile_expired_cron_runs(at=now) == (1, 1)
        with cron_db() as db:
            preparing = db.get(CronJobRun, preparing_id)
            executing = db.get(CronJobRun, executing_id)
            active = db.get(CronJobRun, active_id)
            assert (preparing.status, preparing.phase, preparing.claim_token) == (
                "queued", "queued", None,
            )
            assert executing.status == "unknown"
            assert executing.error_code == "worker_lease_expired_after_start"
            assert active.status == "running"
            assert active.claim_token is not None

    @pytest.mark.asyncio
    async def test_run_queued_passes_the_exact_claim_to_runner(
        self, cron_db, monkeypatch,
    ):
        run_id = self._queued_run(cron_db)
        captured = {}

        async def fake_runner(user_id, job_name, actual_run_id, **kwargs):
            captured.update({
                "user_id": user_id,
                "job_name": job_name,
                "run_id": actual_run_id,
                **kwargs,
            })
            with cron_db() as db:
                run = db.get(CronJobRun, actual_run_id)
                assert run.claim_token == kwargs["claim_token"]
                run.status = "success"
                run.phase = "terminal"
                run.claim_token = None
                run.claim_worker_id = None
                run.claim_lease_expires_at = None
                db.commit()
            return "ok"

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_runner)
        await cron_worker._run_queued(run_id, "worker-a")

        assert captured["run_id"] == run_id
        assert captured["claim_token"]
        assert captured["expected_job_id"] is not None
        assert captured["trigger_source"] == "scheduled"


class TestDispatchAndSpawn:
    @pytest.mark.asyncio
    async def test_dispatch_skips_disabled_job(self, cron_db, monkeypatch):
        _insert_job(cron_db, user_id="u1", name="enabled", cron_expr="* * * * *", enabled=True)
        _insert_job(cron_db, user_id="u1", name="disabled", cron_expr="* * * * *", enabled=False)

        spawned = []

        def fake_spawn(coro):
            spawned.append(coro)
            coro.close()
            return MagicMock()

        monkeypatch.setattr(cron_worker, "_spawn", fake_spawn)
        minute = datetime.utcnow().replace(second=0, microsecond=0)

        await cron_worker._dispatch_and_run("w1", minute)

        with cron_db() as db:
            assert db.query(CronFire).count() == 1
        assert len(spawned) == 1

    @pytest.mark.asyncio
    async def test_dispatch_skips_disabled_auth_user(self, cron_db, monkeypatch):
        _insert_job(
            cron_db,
            user_id="disabled-user",
            name="should-not-run",
            cron_expr="* * * * *",
            enabled=True,
            user_enabled=False,
        )

        spawned = []

        def fake_spawn(coro):
            spawned.append(coro)
            coro.close()
            return MagicMock()

        monkeypatch.setattr(cron_worker, "_spawn", fake_spawn)
        minute = datetime.utcnow().replace(second=0, microsecond=0)

        await cron_worker._dispatch_and_run("w1", minute)

        with cron_db() as db:
            assert db.query(CronFire).count() == 0
        assert spawned == []

    @pytest.mark.asyncio
    async def test_dispatch_skips_non_matching_minute(self, cron_db, monkeypatch):
        _insert_job(cron_db, user_id="u1", name="daily4", cron_expr="0 4 * * *", enabled=True)

        spawned = []

        def fake_spawn(coro):
            spawned.append(coro)
            coro.close()
            return MagicMock()

        monkeypatch.setattr(cron_worker, "_spawn", fake_spawn)
        minute = datetime(2026, 4, 17, 5, 0, 0)

        await cron_worker._dispatch_and_run("w1", minute)

        with cron_db() as db:
            assert db.query(CronFire).count() == 0
        assert len(spawned) == 0

    @pytest.mark.asyncio
    async def test_dispatch_winner_runs_job(self, cron_db, monkeypatch):
        _insert_job(cron_db, user_id="u1", name="winner", cron_expr="* * * * *", enabled=True)

        spawned = []

        def fake_spawn(coro):
            spawned.append(coro)
            coro.close()
            return MagicMock()

        monkeypatch.setattr(cron_worker, "_spawn", fake_spawn)
        minute = datetime.utcnow().replace(second=0, microsecond=0)

        await asyncio.gather(
            cron_worker._dispatch_and_run("w1", minute),
            cron_worker._dispatch_and_run("w2", minute),
        )

        with cron_db() as db:
            assert db.query(CronFire).count() == 1
        assert len(spawned) == 1

    @pytest.mark.asyncio
    async def test_dispatch_isolates_per_job_errors(self, cron_db, monkeypatch):
        """单个 job 的匹配/抢占异常不能影响同分钟其他 job 的调度。"""
        _insert_job(cron_db, user_id="u1", name="bad", cron_expr="* * * * *", enabled=True)
        _insert_job(cron_db, user_id="u1", name="good", cron_expr="* * * * *", enabled=True)

        original = cron_worker._cron_matches_minute

        def flaky_match(expr, minute):
            # 让 dispatch 处理 "bad" 任务时炸掉，"good" 任务保持正常
            from src.api.models.cron_job import CronJob
            with cron_db() as db:
                bad = db.query(CronJob).filter(CronJob.name == "bad").first()
                if bad and expr == bad.cron_expr and minute == flaky_match.target_minute:
                    if not flaky_match.fired:
                        flaky_match.fired = True
                        raise RuntimeError("simulated parser error")
            return original(expr, minute)

        flaky_match.fired = False
        flaky_match.target_minute = datetime.utcnow().replace(second=0, microsecond=0)

        spawned = []

        def fake_spawn(coro):
            spawned.append(coro)
            coro.close()
            return MagicMock()

        monkeypatch.setattr(cron_worker, "_cron_matches_minute", flaky_match)
        monkeypatch.setattr(cron_worker, "_spawn", fake_spawn)

        await cron_worker._dispatch_and_run("w1", flaky_match.target_minute)

        # bad 抛错被吞 → good 仍然成功抢占执行权并 spawn
        with cron_db() as db:
            assert db.query(CronFire).count() == 1
        assert len(spawned) == 1


class TestRunBehavior:
    @pytest.mark.asyncio
    async def test_run_parallel_for_same_user(self, monkeypatch):
        timeline = []

        async def fake_run_cron_job(user_id, job_name, run_id, **kwargs):
            timeline.append(("start", job_name, time.perf_counter()))
            await asyncio.sleep(0.03)
            timeline.append(("end", job_name, time.perf_counter()))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        start = time.perf_counter()
        await asyncio.gather(
            cron_worker._run("u1", "job-a", "w1"),
            cron_worker._run("u1", "job-b", "w1"),
        )
        elapsed = time.perf_counter() - start

        assert elapsed < 0.055
        assert {name for evt, name, _ in timeline if evt == "start"} == {"job-a", "job-b"}

    @pytest.mark.asyncio
    async def test_run_parallel_for_different_users(self, monkeypatch):
        async def fake_run_cron_job(user_id, job_name, run_id, **kwargs):
            await asyncio.sleep(0.03)

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        start = time.perf_counter()
        await asyncio.gather(
            cron_worker._run("u1", "job-a", "w1"),
            cron_worker._run("u2", "job-b", "w1"),
        )
        elapsed = time.perf_counter() - start

        assert elapsed < 0.07

    @pytest.mark.asyncio
    async def test_run_uses_provided_run_id(self, monkeypatch):
        calls = []

        async def fake_run_cron_job(user_id, job_name, run_id, **kwargs):
            calls.append(run_id)

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        await cron_worker._run("u1", "job-a", "w1", run_id="manual-id")
        await cron_worker._run("u1", "job-a", "w1")

        assert calls[0] == "manual-id"
        assert calls[1] != "manual-id"
        assert calls[1]


class TestTimezoneScheduling:
    """cron 调度必须基于本地时区，而非 UTC。"""

    def test_matches_local_nine_oclock(self, monkeypatch):
        # 固定 TIMEZONE_OFFSET=8（北京时区），期待 "0 9 * * *" 在本地 9:00 命中
        monkeypatch.setenv("TIMEZONE_OFFSET", "8")
        local_nine = datetime(2026, 4, 17, 9, 0, 0)
        assert cron_worker._cron_matches_minute("0 9 * * *", local_nine) is True

    def test_does_not_match_utc_nine_in_local_tz(self, monkeypatch):
        # 本地 UTC+8，本地 17:00 = UTC 9:00；若错误地按 UTC 匹配会返回 True
        monkeypatch.setenv("TIMEZONE_OFFSET", "8")
        local_seventeen = datetime(2026, 4, 17, 17, 0, 0)
        assert cron_worker._cron_matches_minute("0 9 * * *", local_seventeen) is False

    def test_matches_midnight_across_tz(self, monkeypatch):
        # 切换时区偏移后必须按新时区匹配
        monkeypatch.setenv("TIMEZONE_OFFSET", "0")
        midnight = datetime(2026, 4, 17, 0, 0, 0)
        assert cron_worker._cron_matches_minute("0 0 * * *", midnight) is True


class TestRunByIdRevalidation:
    """dispatch 快照到实际执行之间，job 的删除/禁用必须被识别。"""

    @pytest.mark.asyncio
    async def test_run_by_id_skips_when_job_deleted(self, cron_db, monkeypatch):
        calls = []

        async def fake_run_cron_job(user_id, job_name, run_id, **kwargs):
            calls.append((user_id, job_name))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        # 不插入 job → 查询返回 None
        await cron_worker._run_by_id(9999, "w1")

        assert calls == []

    @pytest.mark.asyncio
    async def test_run_by_id_skips_when_job_disabled(self, cron_db, monkeypatch):
        job = _insert_job(
            cron_db,
            user_id="u1",
            name="disabled-later",
            cron_expr="* * * * *",
            enabled=False,
        )

        calls = []

        async def fake_run_cron_job(user_id, job_name, run_id, **kwargs):
            calls.append((user_id, job_name))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        await cron_worker._run_by_id(job.id, "w1")

        assert calls == []

    @pytest.mark.asyncio
    async def test_run_by_id_skips_when_auth_user_disabled(self, cron_db, monkeypatch):
        job = _insert_job(
            cron_db,
            user_id="disabled-user",
            name="disabled-user-job",
            cron_expr="* * * * *",
            enabled=True,
            user_enabled=False,
        )

        calls = []

        async def fake_run_cron_job(user_id, job_name, run_id, **kwargs):
            calls.append((user_id, job_name))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        await cron_worker._run_by_id(job.id, "w1")

        assert calls == []

    @pytest.mark.asyncio
    async def test_run_by_id_executes_when_enabled(self, cron_db, monkeypatch):
        job = _insert_job(
            cron_db,
            user_id="u1",
            name="live-job",
            cron_expr="* * * * *",
            enabled=True,
        )

        calls = []

        async def fake_run_cron_job(user_id, job_name, run_id, **kwargs):
            calls.append((user_id, job_name))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        await cron_worker._run_by_id(job.id, "w1")

        assert calls == [("u1", "live-job")]

    @pytest.mark.asyncio
    async def test_run_by_id_rechecks_enabled_before_execute(self, monkeypatch):
        """首次快照命中后，若执行前任务被禁用，应再次跳过。"""
        calls = []

        async def fake_run_cron_job(user_id, job_name, run_id, **kwargs):
            calls.append((user_id, job_name, run_id))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        snapshots = [("u1", "race-job", 1), None]

        def fake_load_snapshot_if_enabled(job_id, expected_rule_version=None):
            return snapshots.pop(0)

        monkeypatch.setattr(
            cron_worker,
            "_load_job_snapshot_if_enabled",
            fake_load_snapshot_if_enabled,
        )

        await cron_worker._run_by_id(42, "w1")

        assert calls == []

    @pytest.mark.asyncio
    async def test_run_by_id_drops_old_rule_version(self, cron_db, monkeypatch):
        job = _insert_job(
            cron_db,
            user_id="u1",
            name="changed-rule",
            cron_expr="0 10 * * *",
            enabled=True,
        )
        with cron_db() as db:
            persisted = db.query(CronJob).filter(CronJob.id == job.id).first()
            persisted.rule_version = 2
            db.commit()

        calls = []

        async def fake_run_cron_job(user_id, job_name, run_id, **kwargs):
            calls.append((user_id, job_name))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)
        await cron_worker._run_by_id(
            job.id,
            "w1",
            expected_rule_version=1,
            scheduled_at=datetime(2026, 7, 27, 9),
        )

        assert calls == []


class TestCleanupOldFires:
    """_cleanup_old_fires 应按 max_age_days 清理 cron_fires 历史行。"""

    def test_deletes_rows_older_than_threshold(self, cron_db, monkeypatch):
        Session = cron_db
        # 插入一条"很久以前"的 fire 与一条"最近"的 fire
        old_time = datetime(2020, 1, 1, 0, 0, 0)
        recent_time = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=1)
        with Session() as db:
            db.add(CronFire(id="old-1", job_id=1, scheduled_at=old_time))
            db.add(CronFire(id="new-1", job_id=1, scheduled_at=recent_time))
            db.commit()

        class _FakeSettings:
            cron_fire_max_age_days = 7

        monkeypatch.setattr(cron_worker, "get_settings", lambda: _FakeSettings())

        cron_worker._cleanup_old_fires()

        with Session() as db:
            remaining_ids = {f.id for f in db.query(CronFire).all()}
        assert remaining_ids == {"new-1"}

    def test_max_age_zero_disables_cleanup(self, cron_db, monkeypatch):
        Session = cron_db
        old_time = datetime(2020, 1, 1)
        with Session() as db:
            db.add(CronFire(id="old-1", job_id=1, scheduled_at=old_time))
            db.commit()

        class _FakeSettings:
            cron_fire_max_age_days = 0

        monkeypatch.setattr(cron_worker, "get_settings", lambda: _FakeSettings())

        cron_worker._cleanup_old_fires()

        with Session() as db:
            assert db.query(CronFire).count() == 1


class TestStopCronWorkerGraceful:
    """stop_cron_worker 应优雅等待 in-flight 任务，超时才强制取消。"""

    @pytest.mark.asyncio
    async def test_stop_waits_for_inflight_task(self, monkeypatch):
        finished = asyncio.Event()

        async def short_inflight():
            await asyncio.sleep(0.05)
            finished.set()

        task = cron_worker._spawn(short_inflight())

        app = SimpleNamespace(state=SimpleNamespace(cron_worker_task=None))
        await cron_worker.stop_cron_worker(app)

        assert finished.is_set()
        assert task.done()
        assert not cron_worker._background_tasks

    @pytest.mark.asyncio
    async def test_stop_cancels_when_timeout_exceeded(self, monkeypatch):
        # 伪造 asyncio.wait 直接返回 timeout 结果，避免 30s 实测等待
        async def fake_wait(tasks, timeout):
            return set(), set(tasks)

        monkeypatch.setattr(cron_worker.asyncio, "wait", fake_wait)

        async def long_inflight():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise

        task = cron_worker._spawn(long_inflight())

        app = SimpleNamespace(state=SimpleNamespace(cron_worker_task=None))
        await cron_worker.stop_cron_worker(app)

        assert task.cancelled() or task.done()
        assert not cron_worker._background_tasks


class TestTriggerManualRun:
    """worker 未启动时手动触发必须 503，不能悄悄走 fallback。"""

    @pytest.mark.asyncio
    async def test_raises_503_when_worker_not_started(self):
        from fastapi import HTTPException

        app = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(HTTPException) as exc_info:
            await cron_worker.trigger_manual_run(
                app,
                user_id="u1",
                job_name="job1",
                run_id="r1",
                job_id=1,
                rule_version=1,
            )
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_spawns_task_when_worker_running(self, monkeypatch):
        called = {}

        async def fake_run(
            user_id,
            job_name,
            worker_id,
            run_id=None,
            expected_job_id=None,
            expected_rule_version=None,
        ):
            called["user_id"] = user_id
            called["name"] = job_name
            called["worker_id"] = worker_id
            called["run_id"] = run_id
            called["expected_job_id"] = expected_job_id
            called["expected_rule_version"] = expected_rule_version

        monkeypatch.setattr(cron_worker, "_run", fake_run)

        app = SimpleNamespace(
            state=SimpleNamespace(
                cron_worker_id="w-test",
            )
        )
        task = await cron_worker.trigger_manual_run(
            app,
            user_id="u1",
            job_name="job1",
            run_id="r1",
            job_id=9,
            rule_version=3,
        )
        await task
        assert called == {
            "user_id": "u1",
            "name": "job1",
            "worker_id": "w-test",
            "run_id": "r1",
            "expected_job_id": 9,
            "expected_rule_version": 3,
        }
