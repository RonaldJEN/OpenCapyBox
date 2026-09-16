import asyncio
import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, UploadFile
from opensandbox.exceptions import SandboxApiException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.models.composer_draft_attachment import ComposerDraftAttachment
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.sandbox_cleanup import SandboxCleanupJob
from src.api.models.session import Session
from src.api.models.user_sandbox import UserSandbox
from src.api.models.user_memory import CronJobRun
from src.api.models.user_run_lock import UserRunLock
from src.api.services.composer_draft_attachment_service import ComposerDraftAttachmentService
from src.api.services.sandbox_cleanup_service import _validate_target
from src.api.services.sandbox_profile_service import SandboxRuntimeConfig
from src.api.services.sandbox_service import SandboxSessionService
from src.api.utils.timezone import now_naive


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add_all([
            Session(id="session-1", user_id="owner", status="active"),
            UserSandbox(id="sandbox-row", user_id="owner", sandbox_id="sandbox-1"),
        ])
        session.commit()
        yield session
    engine.dispose()


def _row(*, state="ready", claimed_session_id=None):
    return ComposerDraftAttachment(
        attachment_id="11111111-1111-1111-1111-111111111111", user_id="owner", draft_id="draft_1",
        name="原件.xlsx", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=3, content_sha256="a" * 64, sandbox_id="sandbox-1", mount_path="/home/user",
        state=state, claimed_session_id=claimed_session_id,
        claimed_path="attachments/11111111-1111-1111-1111-111111111111/原件.xlsx" if claimed_session_id else None,
        expires_at=now_naive() + timedelta(hours=24),
    )


@pytest.fixture
def upload_sandboxes(db, monkeypatch):
    """Keep lifecycle, request binding and attachment writes on the real code path."""
    monkeypatch.setattr(SandboxSessionService, "_instance", None)
    service = SandboxSessionService()
    runtime = SandboxRuntimeConfig(
        profile_id="upload-profile", profile_name="upload", profile_version=1,
        profile_source="default", domain="sandbox.test", protocol="http",
        api_key="test-key", use_server_proxy=True, mount_path="/home/user",
    )
    binding = db.get(UserSandbox, "sandbox-row")
    binding.active_profile_id = runtime.profile_id
    binding.active_profile_version = runtime.profile_version
    db.commit()
    monkeypatch.setattr("src.api.models.database.SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(service, "_resolve_runtime_config", lambda user_id: runtime)
    state_query = AsyncMock(return_value="not_found")
    monkeypatch.setattr(service, "_query_sandbox_state", state_query)

    def sandbox(sandbox_id, healthy):
        return SimpleNamespace(
            id=sandbox_id, is_healthy=AsyncMock(return_value=healthy),
            commands=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(exit_code=0))),
            files=SimpleNamespace(write=AsyncMock()),
        )

    old, replacement = sandbox("sandbox-1", False), sandbox("sandbox-2", True)
    old.commands.run.side_effect = SandboxApiException(status_code=404)
    service._store_cache("owner", old, runtime)
    create = AsyncMock(return_value=replacement)
    connect = AsyncMock(return_value=old)
    monkeypatch.setattr("src.api.services.sandbox_service.Sandbox.create", create)
    monkeypatch.setattr("src.api.services.sandbox_service.Sandbox.connect", connect)
    monkeypatch.setattr("src.api.services.composer_draft_attachment_service.get_sandbox_service", lambda: service)
    return SimpleNamespace(service=service, old=old, replacement=replacement,
                           create=create, connect=connect, state_query=state_query)


async def _upload_test_attachment(db):
    return await ComposerDraftAttachmentService(db).upload(
        user_id="owner", draft_id="draft_1", attachment_id="11111111-1111-1111-1111-111111111111",
        file=UploadFile(filename="hello.md", file=BytesIO(b"hello")),
    )


@pytest.mark.asyncio
async def test_upload_recovers_missing_cached_sandbox_before_writing(db, upload_sandboxes):
    sandboxes = upload_sandboxes
    result = await _upload_test_attachment(db)

    db.expire_all()
    row = db.get(ComposerDraftAttachment, result["attachment_id"])
    assert row.state == "ready"
    assert row.sandbox_id == db.get(UserSandbox, "sandbox-row").sandbox_id == "sandbox-2"
    assert row.mount_path == "/home/user"
    assert row.content_sha256 == hashlib.sha256(b"hello").hexdigest()
    sandboxes.old.commands.run.assert_not_awaited()
    sandboxes.replacement.files.write.assert_awaited_once_with(
        f"/home/user/.composer-drafts/draft_1/{row.attachment_id}/hello.md", b"hello",
    )
    assert db.query(Session).count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["agent", "cron"])
async def test_upload_cannot_replace_missing_sandbox_owned_by_active_run(db, upload_sandboxes, owner):
    if owner == "agent":
        db.add(UserRunLock(user_id="owner", session_id="session-1"))
    else:
        db.add(CronJobRun(
            id="cron-run", user_id="owner", job_name="running", cron_expr="* * * * *",
            status="running", sandbox_id="sandbox-1", claim_token="claim",
            claim_lease_expires_at=now_naive() + timedelta(minutes=5),
        ))
    db.commit()

    with pytest.raises(HTTPException) as error:
        await _upload_test_attachment(db)

    assert error.value.status_code == 503
    db.expire_all()
    assert db.get(UserSandbox, "sandbox-row").sandbox_id == "sandbox-1"
    assert db.query(ComposerDraftAttachment).one().state == "failed"
    upload_sandboxes.create.assert_not_awaited()
    upload_sandboxes.old.commands.run.assert_not_awaited()
    upload_sandboxes.replacement.files.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage, remote_state", [
    ("mkdir", "not_found"), ("verify", "not_found"), ("write", "running"),
])
async def test_upload_404_rechecks_sandbox_on_next_attempt(db, upload_sandboxes, failure_stage, remote_state):
    sandboxes = upload_sandboxes
    sandboxes.old.is_healthy.return_value = True
    failure = SandboxApiException(status_code=404)
    if failure_stage == "verify":
        sandboxes.old.commands.run.side_effect = [SimpleNamespace(exit_code=0), failure]
    elif failure_stage == "write":
        sandboxes.old.commands.run.side_effect = None
        sandboxes.old.files.write.side_effect = failure
    # The 404 happens after the successful health check; the current upload stays on its owner.
    with pytest.raises(HTTPException) as error:
        await _upload_test_attachment(db)
    assert error.value.status_code == 503
    assert sandboxes.service.get_cached("owner") is None
    sandboxes.create.assert_not_awaited()
    db.expire_all()
    failed = db.query(ComposerDraftAttachment).one()
    assert (failed.state, failed.sandbox_id, failed.generation) == ("failed", "sandbox-1", 1)

    sandboxes.state_query.return_value = remote_state
    sandboxes.old.commands.run.side_effect = None
    sandboxes.old.files.write.side_effect = None
    result = await _upload_test_attachment(db)

    db.expire_all()
    row = db.get(ComposerDraftAttachment, result["attachment_id"])
    expected_sandbox = "sandbox-2" if remote_state == "not_found" else "sandbox-1"
    assert (row.state, row.generation) == ("ready", 2)
    assert row.sandbox_id == db.get(UserSandbox, "sandbox-row").sandbox_id == expected_sandbox
    assert db.query(ComposerDraftAttachment).count() == 1
    sandboxes.state_query.assert_awaited()
    if remote_state == "running":
        sandboxes.create.assert_not_awaited()
        sandboxes.connect.assert_awaited_once()
    else:
        sandboxes.create.assert_awaited_once()
        sandboxes.replacement.files.write.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_is_idempotent_for_same_session(db):
    row = _row(state="claimed", claimed_session_id="session-1")
    db.add(row); db.commit()

    files = await ComposerDraftAttachmentService(db).claim(
        user_id="owner", session_id="session-1", draft_id="draft_1", attachment_ids=[row.attachment_id]
    )

    assert files[0]["attachment_id"] == row.attachment_id
    assert files[0]["path"] == row.claimed_path


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RuntimeError('down'), asyncio.CancelledError()])
async def test_claim_failure_restores_ready_for_retry(db, failure):
    row = _row()
    db.add(row); db.commit()
    sandbox_service = type("SandboxService", (), {"get_existing": AsyncMock(side_effect=failure)})()

    with patch("src.api.services.composer_draft_attachment_service.get_sandbox_service", return_value=sandbox_service):
        expected_error = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else HTTPException
        with pytest.raises(expected_error):
            await ComposerDraftAttachmentService(db).claim(
                user_id="owner", session_id="session-1", draft_id="draft_1", attachment_ids=[row.attachment_id]
            )

    db.expire_all()
    restored = db.get(ComposerDraftAttachment, row.attachment_id)
    assert restored.state == "ready"
    assert restored.claimed_session_id == 'session-1'
    assert restored.claimed_path  # Keep the partial target reclaimable.


@pytest.mark.asyncio
async def test_deleted_claim_late_failure_rearms_only_its_target_cleanup(db):
    session_id = '44444444-4444-4444-4444-444444444444'
    row = _row()
    attachment_id = row.attachment_id
    db.add_all([row, Session(id=session_id, user_id='owner', status='active')])
    db.commit()
    service = ComposerDraftAttachmentService(db)

    async def finish_copy_after_delete(_command):
        await service.delete(user_id='owner', draft_id='draft_1', attachment_id=attachment_id)
        # The cleaner finishes before the outstanding copy reports failure.
        for job in db.query(SandboxCleanupJob).all():
            job.state = 'completed'
            job.completed_at = now_naive()
        db.commit()
        return SimpleNamespace(exit_code=1)

    sandbox = SimpleNamespace(commands=SimpleNamespace(run=AsyncMock(side_effect=finish_copy_after_delete)))
    with patch('src.api.services.composer_draft_attachment_service.get_sandbox_service',
               return_value=SimpleNamespace(get_existing=AsyncMock(return_value=sandbox))):
        with pytest.raises(HTTPException) as error:
            await service.claim(user_id='owner', session_id=session_id,
                                draft_id='draft_1', attachment_ids=[attachment_id])

    assert error.value.status_code == 409
    db.expire_all()
    assert db.get(ComposerDraftAttachment, attachment_id).state == 'deleted'
    jobs = {job.relative_path: job for job in db.query(SandboxCleanupJob).all()}
    target = jobs[f'sessions/{session_id}/attachments/{attachment_id}']
    assert target.state == 'queued'
    assert target.completed_at is None
    assert jobs[f'.composer-drafts/draft_1/{attachment_id}'].state == 'completed'


@pytest.mark.asyncio
async def test_delete_does_not_reclaim_claimed_copy_referenced_by_round(db):
    row = _row(state="claimed", claimed_session_id="session-1")
    db.add_all([row, Round(id="round-1", session_id="session-1", user_message="x", user_attachments=json.dumps([
        {"composer_draft_attachment_id": row.attachment_id, "path": row.claimed_path}
    ]))]); db.commit()

    with patch("src.api.services.composer_draft_attachment_service.enqueue_cleanup", return_value="job") as enqueue:
        await ComposerDraftAttachmentService(db).delete(user_id="owner", draft_id="draft_1", attachment_id=row.attachment_id)

    assert enqueue.call_count == 1
    assert enqueue.call_args.kwargs["relative_path"] == ".composer-drafts/draft_1/11111111-1111-1111-1111-111111111111"


def test_composer_cleanup_scope_is_exact():
    assert _validate_target("composer_draft_attachment", ".composer-drafts/draft_1/11111111-1111-1111-1111-111111111111")
    assert _validate_target("composer_draft_attachment", "sessions/11111111-1111-1111-1111-111111111111/attachments/11111111-1111-1111-1111-111111111111")
    with pytest.raises(ValueError):
        _validate_target("composer_draft_attachment", ".composer-drafts/draft_1")


@pytest.mark.asyncio
async def test_delete_before_upload_reserves_tombstone_without_session(db):
    service = ComposerDraftAttachmentService(db)
    attachment_id = '22222222-2222-2222-2222-222222222222'
    await service.delete(user_id='owner', draft_id='draft_1', attachment_id=attachment_id)
    with pytest.raises(HTTPException) as error:
        await service.upload(user_id='owner', draft_id='draft_1', attachment_id=attachment_id,
                             file=UploadFile(filename='hello.md', file=BytesIO(b'hello')))
    assert error.value.status_code == 409
    assert db.get(ComposerDraftAttachment, attachment_id).state == 'deleted'
    assert db.query(Session).count() == 1


@pytest.mark.asyncio
async def test_claim_command_failure_keeps_batch_retryable_and_tracks_partial_target(db):
    row = _row()
    other = _row()
    other.attachment_id = '33333333-3333-3333-3333-333333333333'
    db.add_all([row, other]); db.commit()
    sandbox = type('Sandbox', (), {'commands': type('Commands', (), {
        'run': AsyncMock(return_value=SimpleNamespace(exit_code=1))})()})()
    sandbox_service = type('Service', (), {'get_existing': AsyncMock(return_value=sandbox)})()
    ids = [row.attachment_id, other.attachment_id]
    with patch('src.api.services.composer_draft_attachment_service.get_sandbox_service', return_value=sandbox_service):
        with pytest.raises(HTTPException):
            await ComposerDraftAttachmentService(db).claim(user_id='owner', session_id='session-1',
                draft_id='draft_1', attachment_ids=ids)
    db.expire_all()
    assert all(db.get(ComposerDraftAttachment, item).state == 'ready' for item in ids)
    assert db.get(ComposerDraftAttachment, ids[0]).claimed_path


@pytest.mark.asyncio
async def test_round_binding_is_rolled_back_or_protects_target_with_round(db):
    row = _row(state='claimed', claimed_session_id='session-1')
    db.add(row); db.commit()
    attachment = {'composer_draft_attachment_id': row.attachment_id, 'path': row.claimed_path}
    service = ComposerDraftAttachmentService(db)
    service.assert_claimed_blocks(user_id='owner', session_id='session-1', blocks=[{'file': attachment}], bind=True)
    db.rollback()
    assert db.get(ComposerDraftAttachment, row.attachment_id).state == 'claimed'
    service.assert_claimed_blocks(user_id='owner', session_id='session-1', blocks=[{'file': attachment}], bind=True)
    db.add(Round(id='bound-round', session_id='session-1', user_message='hi', user_attachments=json.dumps([attachment])))
    db.commit()
    with patch('src.api.services.composer_draft_attachment_service.enqueue_cleanup', return_value='job') as cleanup:
        await service.delete(user_id='owner', draft_id='draft_1', attachment_id=row.attachment_id)
    assert cleanup.call_count == 1
    assert db.get(ComposerDraftAttachment, row.attachment_id).state == 'attached'
