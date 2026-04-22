"""cron_worker 单元测试。"""

import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.api.services.cron_worker as cron_worker
from src.api.models.cron_fire import CronFire
from src.api.models.cron_job import CronJob
from src.api.models.database import Base


@pytest.fixture(autouse=True)
def clear_background_tasks():
    cron_worker._background_tasks.clear()
    yield
    for task in list(cron_worker._background_tasks):
        if not task.done():
            task.cancel()
    cron_worker._background_tasks.clear()


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
) -> CronJob:
    with session_factory() as db:
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

        await cron_worker._dispatch_and_run("w1", {}, minute)

        with cron_db() as db:
            assert db.query(CronFire).count() == 1
        assert len(spawned) == 1

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

        await cron_worker._dispatch_and_run("w1", {}, minute)

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
            cron_worker._dispatch_and_run("w1", {}, minute),
            cron_worker._dispatch_and_run("w2", {}, minute),
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

        await cron_worker._dispatch_and_run("w1", {}, flaky_match.target_minute)

        # bad 抛错被吞 → good 仍然成功抢占执行权并 spawn
        with cron_db() as db:
            assert db.query(CronFire).count() == 1
        assert len(spawned) == 1


class TestRunBehavior:
    @pytest.mark.asyncio
    async def test_run_respects_per_user_lock(self, monkeypatch):
        timeline = []

        async def fake_run_cron_job(user_id, job_name, run_id):
            timeline.append(("start", job_name, time.perf_counter()))
            await asyncio.sleep(0.03)
            timeline.append(("end", job_name, time.perf_counter()))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        user_locks = {}

        await asyncio.gather(
            cron_worker._run("u1", "job-a", user_locks, "w1"),
            cron_worker._run("u1", "job-b", user_locks, "w1"),
        )

        starts = {name: ts for evt, name, ts in timeline if evt == "start"}
        ends = {name: ts for evt, name, ts in timeline if evt == "end"}
        first = min(starts, key=starts.get)
        second = "job-b" if first == "job-a" else "job-a"

        assert starts[second] >= ends[first]

    @pytest.mark.asyncio
    async def test_run_parallel_for_different_users(self, monkeypatch):
        async def fake_run_cron_job(user_id, job_name, run_id):
            await asyncio.sleep(0.03)

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        user_locks = {}

        start = time.perf_counter()
        await asyncio.gather(
            cron_worker._run("u1", "job-a", user_locks, "w1"),
            cron_worker._run("u2", "job-b", user_locks, "w1"),
        )
        elapsed = time.perf_counter() - start

        assert elapsed < 0.07

    @pytest.mark.asyncio
    async def test_run_uses_provided_run_id(self, monkeypatch):
        calls = []

        async def fake_run_cron_job(user_id, job_name, run_id):
            calls.append(run_id)

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        user_locks = {}

        await cron_worker._run("u1", "job-a", user_locks, "w1", run_id="manual-id")
        await cron_worker._run("u1", "job-a", user_locks, "w1")

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

        async def fake_run_cron_job(user_id, job_name, run_id):
            calls.append((user_id, job_name))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        # 不插入 job → 查询返回 None
        await cron_worker._run_by_id(9999, {}, "w1")

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

        async def fake_run_cron_job(user_id, job_name, run_id):
            calls.append((user_id, job_name))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        await cron_worker._run_by_id(job.id, {}, "w1")

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

        async def fake_run_cron_job(user_id, job_name, run_id):
            calls.append((user_id, job_name))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        await cron_worker._run_by_id(job.id, {}, "w1")

        assert calls == [("u1", "live-job")]

    @pytest.mark.asyncio
    async def test_run_by_id_rechecks_enabled_after_lock(self, monkeypatch):
        """首次快照命中后，若等待锁期间任务被禁用，执行前应再次跳过。"""
        calls = []

        async def fake_run_cron_job(user_id, job_name, run_id):
            calls.append((user_id, job_name, run_id))

        monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

        snapshots = [("u1", "race-job"), None]

        def fake_load_snapshot_if_enabled(job_id):
            return snapshots.pop(0)

        monkeypatch.setattr(
            cron_worker,
            "_load_job_snapshot_if_enabled",
            fake_load_snapshot_if_enabled,
        )

        await cron_worker._run_by_id(42, {}, "w1")

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
                app, user_id="u1", job_name="job1", run_id="r1"
            )
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_raises_503_when_only_locks_present(self):
        """半初始化状态（只有 locks 没有 worker_id）也应拒绝。"""
        from fastapi import HTTPException

        app = SimpleNamespace(state=SimpleNamespace(cron_user_locks={}))
        with pytest.raises(HTTPException) as exc_info:
            await cron_worker.trigger_manual_run(
                app, user_id="u1", job_name="job1", run_id="r1"
            )
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_spawns_task_when_worker_running(self, monkeypatch):
        called = {}

        async def fake_run(user_id, job_name, user_locks, worker_id, run_id=None):
            called["user_id"] = user_id
            called["name"] = job_name
            called["worker_id"] = worker_id
            called["run_id"] = run_id

        monkeypatch.setattr(cron_worker, "_run", fake_run)

        app = SimpleNamespace(
            state=SimpleNamespace(
                cron_user_locks={},
                cron_worker_id="w-test",
            )
        )
        task = await cron_worker.trigger_manual_run(
            app, user_id="u1", job_name="job1", run_id="r1"
        )
        await task
        assert called == {
            "user_id": "u1",
            "name": "job1",
            "worker_id": "w-test",
            "run_id": "r1",
        }
