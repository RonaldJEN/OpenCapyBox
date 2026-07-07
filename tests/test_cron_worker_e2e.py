"""cron_worker 轻量集成测试。"""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.api.services.cron_worker as cron_worker
from src.api.models.auth_user import AuthUser
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
    db_path = tmp_path / "cron_worker_e2e.sqlite3"
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


def _insert_job(session_factory, user_id: str, name: str, cron_expr: str):
    with session_factory() as db:
        if not db.query(AuthUser).filter(AuthUser.user_id == user_id).first():
            db.add(
                AuthUser(
                    user_id=user_id,
                    username=user_id,
                    auth_type="simple",
                    password_hash="hash",
                    enabled=True,
                    is_admin=False,
                    created_by="test",
                )
            )
        job = CronJob(
            user_id=user_id,
            name=name,
            cron_expr=cron_expr,
            description="e2e",
            enabled=True,
        )
        db.add(job)
        db.commit()


@pytest.mark.asyncio
async def test_two_workers_three_minutes_exactly_once(cron_db, monkeypatch):
    _insert_job(cron_db, "u1", "job-1", "* * * * *")
    _insert_job(cron_db, "u2", "job-2", "* * * * *")

    counter = {"count": 0}

    async def fake_run_cron_job(user_id, job_name, run_id):
        counter["count"] += 1

    monkeypatch.setattr(cron_worker, "run_cron_job", fake_run_cron_job)

    base_minute = datetime.utcnow().replace(second=0, microsecond=0)
    for i in range(3):
        minute = base_minute + timedelta(minutes=i)
        await asyncio.gather(
            cron_worker._dispatch_and_run("w1", minute),
            cron_worker._dispatch_and_run("w2", minute),
        )
        if cron_worker._background_tasks:
            await asyncio.gather(*list(cron_worker._background_tasks))

    with cron_db() as db:
        fire_count = db.query(CronFire).count()

    assert fire_count == 6
    assert counter["count"] == 6
