import asyncio
import json
from io import BytesIO
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.models.composer_draft_attachment import ComposerDraftAttachment
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.sandbox_cleanup import SandboxCleanupJob
from src.api.models.session import Session
from src.api.models.user_sandbox import UserSandbox
from src.api.services.composer_draft_attachment_service import ComposerDraftAttachmentService
from src.api.services.sandbox_cleanup_service import _validate_target
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
