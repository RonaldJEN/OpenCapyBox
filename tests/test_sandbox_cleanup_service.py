"""Cleanup scheduling and ownership regressions without remote side effects."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.models.sandbox_cleanup import SandboxCleanupJob
from src.api.services import sandbox_cleanup_service as cleanup
from src.api.services.sandbox_service import SandboxLifecycleError, SandboxTemporarilyUnavailable


@pytest.fixture
def harness(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SandboxCleanupJob.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    clock = SimpleNamespace(now=datetime(2026, 9, 16, 12, 0))
    commands = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(stdout='{"ok": true}')))
    sandbox = SimpleNamespace(commands=commands)
    service = SimpleNamespace(get_existing=AsyncMock(return_value=sandbox))
    monkeypatch.setattr(cleanup, "SessionLocal", factory)
    monkeypatch.setattr(cleanup, "now_naive", lambda: clock.now)
    monkeypatch.setattr(cleanup, "get_sandbox_service", lambda: service)
    yield SimpleNamespace(db=factory, clock=clock, service=service, sandbox=sandbox)
    engine.dispose()


def seed(harness, number=1, **overrides):
    attachment_id = str(UUID(int=number))
    values = dict(
        cleanup_id=attachment_id, user_id="owner", owner_kind="composer_draft_attachment",
        owner_id=attachment_id, sandbox_id="sandbox-old", mount_path="/home/user",
        relative_path=f".composer-drafts/draft_1/{attachment_id}", state="queued",
        created_at=harness.clock.now, next_attempt_at=harness.clock.now,
    )
    values.update(overrides)
    with harness.db() as db:
        db.add(SandboxCleanupJob(**values))
        db.commit()
    return attachment_id


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    "sandbox_binding_changed", "sandbox_profile_mismatch", "sandbox_binding_missing",
    "sandbox_not_found", "sandbox_terminated", "sandbox_failed",
])
async def test_permanent_owner_failure_blocks_without_deleting_or_retrying(harness, code):
    cleanup_id = seed(harness)
    harness.service.get_existing.side_effect = SandboxLifecycleError("owner is gone", code=code)

    assert await cleanup.run_cleanup_job(cleanup_id) is False

    with harness.db() as db:
        job = db.get(SandboxCleanupJob, cleanup_id)
        assert job.state == "blocked"
        assert job.next_attempt_at is None
        assert job.completed_at is None
        assert "owner is gone" in job.error_message
    harness.clock.now += timedelta(days=30)
    assert cleanup_id not in cleanup._due_ids()
    assert cleanup._claim(cleanup_id) is None
    assert await cleanup.run_cleanup_job(cleanup_id) is False
    harness.service.get_existing.assert_awaited_once()
    harness.sandbox.commands.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_twenty_failing_old_jobs_do_not_starve_the_next_job(harness):
    old_time = harness.clock.now - timedelta(days=7)
    old_ids = [seed(harness, number, created_at=old_time, next_attempt_at=old_time)
               for number in range(1, 21)]
    fresh_id = seed(harness, 21, sandbox_id="sandbox-current", next_attempt_at=None)

    async def connect(_user_id, sandbox_id):
        if sandbox_id == "sandbox-old":
            raise SandboxTemporarilyUnavailable("temporarily unavailable", code="sandbox_binding_read_failed")
        return harness.sandbox

    harness.service.get_existing.side_effect = connect
    first_batch = cleanup._due_ids()
    assert set(first_batch) == set(old_ids)
    for cleanup_id in first_batch:
        assert await cleanup.run_cleanup_job(cleanup_id) is False
    harness.clock.now += timedelta(seconds=31)
    for cleanup_id in cleanup._due_ids():
        await cleanup.run_cleanup_job(cleanup_id)

    with harness.db() as db:
        assert db.get(SandboxCleanupJob, fresh_id).state == "completed"
        assert all(db.get(SandboxCleanupJob, cleanup_id).state == "retry" for cleanup_id in old_ids)
    harness.sandbox.commands.run.assert_awaited_once()


def test_claim_respects_retry_time_and_active_lease_but_recovers_expired_lease(harness):
    retry_id = seed(harness, state="retry", next_attempt_at=harness.clock.now + timedelta(minutes=10))
    completed_id = seed(harness, 2, state="completed")
    blocked_id = seed(harness, 3, state="blocked")
    active_id = seed(harness, 4)
    assert all(cleanup._claim(cleanup_id) is None for cleanup_id in (retry_id, completed_id, blocked_id))
    first = cleanup._claim(active_id)
    assert first is not None
    assert cleanup._claim(active_id) is None
    assert active_id not in cleanup._due_ids()

    with harness.db() as db:
        harness.clock.now = db.get(SandboxCleanupJob, active_id).lease_expires_at
    assert active_id in cleanup._due_ids()
    replacement = cleanup._claim(active_id)
    assert replacement is not None
    assert replacement.generation > first.generation
    assert replacement.owner_token != first.owner_token
    cleanup._finish(first, error=None)
    with harness.db() as db:
        assert db.get(SandboxCleanupJob, active_id).owner_token == replacement.owner_token
        assert db.get(SandboxCleanupJob, active_id).state == "running"
    cleanup._finish(replacement, error=None)
    assert cleanup._claim(active_id) is None
    harness.clock.now += timedelta(minutes=5)
    assert cleanup._claim(retry_id) is not None


@pytest.mark.parametrize("error,blocked", [(None, False), ("temporary", False), ("owner gone", True)])
def test_rearmed_late_writer_cannot_be_finished_by_old_claim(harness, error, blocked):
    cleanup_id = seed(harness)
    old_claim = cleanup._claim(cleanup_id)
    with harness.db() as db:
        job = db.get(SandboxCleanupJob, cleanup_id)
        rearmed_id = cleanup.enqueue_cleanup(
            db, user_id=job.user_id, owner_kind=job.owner_kind, owner_id=job.owner_id,
            sandbox_id=job.sandbox_id, mount_path=job.mount_path, relative_path=job.relative_path,
        )
        db.commit()
        assert rearmed_id == cleanup_id
    cleanup._finish(old_claim, error=error, blocked=blocked)

    with harness.db() as db:
        job = db.get(SandboxCleanupJob, cleanup_id)
        assert job.state == "queued"
        assert job.generation > old_claim.generation
        assert job.completed_at is None
    new_claim = cleanup._claim(cleanup_id)
    assert new_claim is not None
    cleanup._finish(new_claim, error=None)
    with harness.db() as db:
        assert db.get(SandboxCleanupJob, cleanup_id).state == "completed"


@pytest.mark.parametrize("previous_attempts,delay_seconds", [(0, 30), (1, 60), (7166, 1800)])
def test_temporary_failure_backoff_is_bounded_for_historical_attempt_counts(harness, previous_attempts, delay_seconds):
    cleanup_id = seed(harness, attempt_count=previous_attempts)
    claim = cleanup._claim(cleanup_id)
    cleanup._finish(claim, error="network down")

    with harness.db() as db:
        job = db.get(SandboxCleanupJob, cleanup_id)
        assert job.state == "retry"
        assert job.next_attempt_at == harness.clock.now + timedelta(seconds=delay_seconds)
        assert job.lease_expires_at is None
    assert cleanup._claim(cleanup_id) is None
