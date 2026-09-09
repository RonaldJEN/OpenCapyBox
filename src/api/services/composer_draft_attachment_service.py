"""Composer draft attachment ownership and Sandbox file transitions."""

from __future__ import annotations

import hashlib
import os
import posixpath
import shlex
import uuid
from datetime import timedelta

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.exc import IntegrityError

from src.api.models.composer_draft_attachment import ComposerDraftAttachment
from src.api.models.database import SessionLocal
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.user_sandbox import UserSandbox
from src.api.services.sandbox_cleanup_service import enqueue_cleanup
from src.api.services.sandbox_service import get_sandbox_service, resolve_sandbox_path
from src.api.utils.timezone import now_naive


_TTL = timedelta(hours=24)


def _safe_name(filename: str | None) -> str:
    raw = os.path.basename(filename or "uploaded_file").strip()
    if not raw or raw in {".", ".."}:
        return "uploaded_file"
    return raw.replace("/", "_").replace("\\", "_").replace("\x00", "_")[:512]


def _draft_root(row: ComposerDraftAttachment) -> str:
    return f".composer-drafts/{row.draft_id}/{row.attachment_id}"


def _draft_path(row: ComposerDraftAttachment) -> str:
    return f"{_draft_root(row)}/{row.name}"


def _serialize(row: ComposerDraftAttachment) -> dict:
    return {
        "attachment_id": row.attachment_id,
        "draft_id": row.draft_id,
        "name": row.name,
        "size": row.size_bytes,
        "type": row.content_type or posixpath.splitext(row.name)[1].lstrip(".").lower() or "file",
        "sha256": row.content_sha256,
        "status": "ready" if row.state in {"ready", "claimed", "attached"} else row.state,
    }


class ComposerDraftAttachmentService:
    def __init__(self, db: DBSession):
        self.db = db

    def assert_claimed_blocks(self, *, user_id: str, session_id: str, blocks: list[dict], bind: bool = False) -> None:
        """Reject forged composer identities before a Round can be created."""
        expected: dict[str, str] = {}
        for block in blocks:
            file_obj = block.get("file") if isinstance(block, dict) else None
            if isinstance(file_obj, dict) and file_obj.get("composer_draft_attachment_id"):
                expected[str(file_obj["composer_draft_attachment_id"])] = str(file_obj.get("path") or "")
        if not expected:
            return
        query = self.db.query(ComposerDraftAttachment).filter(
            ComposerDraftAttachment.attachment_id.in_(expected),
            ComposerDraftAttachment.user_id == user_id,
            ComposerDraftAttachment.claimed_session_id == session_id,
            ComposerDraftAttachment.state.in_(("claimed", "attached")),
        ).populate_existing()
        rows = (query.with_for_update() if bind else query).all()
        actual = {row.attachment_id: row.claimed_path for row in rows}
        if actual != expected:
            raise HTTPException(status_code=409, detail="草稿附件尚未绑定到当前会话")
        if bind:
            for row in rows:
                row.state = "attached"

    async def upload(self, *, user_id: str, draft_id: str, attachment_id: str, file: UploadFile) -> dict:
        try:
            attachment_id = str(uuid.UUID(attachment_id))
        except (ValueError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail="attachment_id 必须是 UUID") from exc
        if not draft_id or len(draft_id) > 64 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in draft_id):
            raise HTTPException(status_code=422, detail="draft_id 无效")
        content = await file.read()
        name = _safe_name(file.filename)
        digest = hashlib.sha256(content).hexdigest()
        existing = self.db.query(ComposerDraftAttachment).filter(
            ComposerDraftAttachment.attachment_id == attachment_id,
        ).with_for_update().populate_existing().one_or_none()
        if existing:
            if existing.user_id != user_id or existing.draft_id != draft_id:
                raise HTTPException(status_code=404, detail="草稿附件不存在")
            if existing.state == "deleted":
                raise HTTPException(status_code=409, detail="草稿附件已被移除")
            if existing.content_sha256 != digest or existing.name != name:
                raise HTTPException(status_code=409, detail="attachment_id 已对应另一份文件")
            if existing.state in {"ready", "claimed", "attached"}:
                return _serialize(existing)
            if existing.state != "failed":
                raise HTTPException(status_code=409, detail="草稿附件正在变更")

        # Reserve before awaiting Sandbox initialization. A concurrent delete
        # either wins this identity or advances the same row's generation.
        row = existing or ComposerDraftAttachment(attachment_id=attachment_id, user_id=user_id, draft_id=draft_id)
        row.name = name
        row.content_type = file.content_type
        row.size_bytes = len(content)
        row.content_sha256 = digest
        row.state = "uploading"
        row.generation = int(row.generation or 0) + 1
        generation = row.generation
        row.expires_at = now_naive() + _TTL
        if existing is None:
            self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(status_code=409, detail="草稿附件已被移除或正在上传") from exc

        sandbox_service = get_sandbox_service()
        # The Sandbox identity is frozen before the network write.  A later
        # profile replacement cannot redirect this attachment to another owner.
        from src.api.routes.sessions import _ensure_sandbox, _get_sandbox_binding_state
        try:
            binding = _get_sandbox_binding_state(self.db, user_id)
            sandbox = await _ensure_sandbox(sandbox_service, user_id, self.db, binding_state=binding)
            sandbox_id = getattr(sandbox, "id", None)
            if not isinstance(sandbox_id, str) or not sandbox_id:
                raise RuntimeError("沙箱身份不可用")
            mount_path = sandbox_service.get_mount_path(user_id)
            owned = self.db.query(ComposerDraftAttachment).filter(
                ComposerDraftAttachment.attachment_id == attachment_id,
                ComposerDraftAttachment.state == "uploading",
                ComposerDraftAttachment.generation == generation,
            ).update({"sandbox_id": sandbox_id, "mount_path": mount_path}, synchronize_session=False)
            self.db.commit()
            if owned != 1:
                raise HTTPException(status_code=409, detail="草稿附件已被移除")
            destination = resolve_sandbox_path(f".composer-drafts/{draft_id}/{attachment_id}/{name}", mount_path)
            from src.api.routes.sessions import _extract_exit_code
            mkdir = await sandbox.commands.run(f"mkdir -p {shlex.quote(posixpath.dirname(destination))}")
            if _extract_exit_code(mkdir) != 0:
                raise RuntimeError("无法创建草稿附件目录")
            write = getattr(sandbox.files, "write", None)
            if callable(write):
                await write(destination, content)
            else:
                await sandbox.files.write_file(destination, content)
            verification = await sandbox.commands.run(
                f"test \"$(wc -c < {shlex.quote(destination)})\" = {len(content)} && "
                f"test \"$(sha256sum {shlex.quote(destination)} | cut -d ' ' -f1)\" = {shlex.quote(digest)}"
            )
            if _extract_exit_code(verification) != 0:
                raise RuntimeError("草稿附件落盘校验失败")
        except BaseException as exc:
            self.db.rollback()
            failed = self.db.query(ComposerDraftAttachment).filter(
                ComposerDraftAttachment.attachment_id == attachment_id,
                ComposerDraftAttachment.state == "uploading",
                ComposerDraftAttachment.generation == generation,
            ).update({"state": "failed"}, synchronize_session=False)
            self.db.commit()
            if failed != 1:
                self.db.expire_all()
                deleted = self.db.query(ComposerDraftAttachment).filter_by(attachment_id=attachment_id).one_or_none()
                if deleted and deleted.sandbox_id and deleted.mount_path:
                    self._enqueue_path_cleanup(deleted, _draft_root(deleted))
                    self.db.commit()
            if not isinstance(exc, Exception) or isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=503, detail="草稿附件保存失败，可重试") from exc
        updated = self.db.query(ComposerDraftAttachment).filter(
            ComposerDraftAttachment.attachment_id == attachment_id,
            ComposerDraftAttachment.state == "uploading",
            ComposerDraftAttachment.generation == generation,
        ).update({"state": "ready"}, synchronize_session=False)
        self.db.commit()
        if updated != 1:
            self.db.expire_all()
            deleted = self.db.query(ComposerDraftAttachment).filter_by(attachment_id=attachment_id).one_or_none()
            if deleted and deleted.sandbox_id and deleted.mount_path:
                self._enqueue_path_cleanup(deleted, _draft_root(deleted))
                self.db.commit()
            raise HTTPException(status_code=409, detail="草稿附件已被移除")
        self.db.expire_all()
        row = self.db.query(ComposerDraftAttachment).filter_by(attachment_id=attachment_id).one()
        return _serialize(row)

    async def claim(self, *, user_id: str, session_id: str, draft_id: str, attachment_ids: list[str]) -> list[dict]:
        session = self.db.query(Session).filter(Session.id == session_id, Session.user_id == user_id).one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        if not attachment_ids or len(set(attachment_ids)) != len(attachment_ids):
            raise HTTPException(status_code=422, detail="attachment_ids 无效")
        rows = self.db.query(ComposerDraftAttachment).filter(
            ComposerDraftAttachment.attachment_id.in_(attachment_ids),
            ComposerDraftAttachment.user_id == user_id,
            ComposerDraftAttachment.draft_id == draft_id,
        ).with_for_update().all()
        if len(rows) != len(attachment_ids):
            raise HTTPException(status_code=404, detail="草稿附件不存在")
        current_sandbox_id = self.db.query(UserSandbox.sandbox_id).filter(
            UserSandbox.user_id == user_id
        ).scalar()
        if not current_sandbox_id or any(row.sandbox_id != current_sandbox_id for row in rows):
            raise HTTPException(status_code=409, detail="草稿附件所属沙箱已变更，请重新选择文件")
        self.db.commit()
        outputs: list[dict] = []
        for attachment_id in attachment_ids:
            row = self.db.query(ComposerDraftAttachment).filter_by(attachment_id=attachment_id).with_for_update().populate_existing().one()
            if row.state in {"claimed", "attached"} and row.claimed_session_id == session_id:
                outputs.append(self._claimed_file(row))
                self.db.commit()
                continue
            if row.state != "ready" or (row.claimed_session_id and row.claimed_session_id != session_id):
                raise HTTPException(status_code=409, detail="草稿附件尚未就绪")
            # Claim one row at a time.  A failed second file cannot strand the
            # remainder of a batch in ``claiming``.
            row.state = "claiming"; row.claimed_session_id = session_id
            row.claimed_path = f"attachments/{row.attachment_id}/{row.name}"
            row.generation += 1
            generation = row.generation
            attachment_id = row.attachment_id
            mount_path, sandbox_id = row.mount_path, row.sandbox_id
            source_relative, target_relative = _draft_path(row), row.claimed_path
            expected_size, expected_sha = row.size_bytes, row.content_sha256
            self.db.commit()
            try:
                sandbox = await get_sandbox_service().get_existing(user_id, sandbox_id)
                source = resolve_sandbox_path(source_relative, mount_path)
                target = resolve_sandbox_path(
                    f"sessions/{session_id}/{target_relative}", mount_path
                )
                command = (
                    f"mkdir -p {shlex.quote(posixpath.dirname(target))} && cp -- {shlex.quote(source)} {shlex.quote(target)} "
                    f"&& test \"$(wc -c < {shlex.quote(target)})\" = {expected_size} "
                    f"&& test \"$(sha256sum {shlex.quote(target)} | cut -d ' ' -f1)\" = {shlex.quote(expected_sha)}"
                )
                execution = await sandbox.commands.run(command)
                from src.api.routes.sessions import _extract_exit_code
                if _extract_exit_code(execution) != 0:
                    raise RuntimeError("草稿附件目标校验失败")
            except BaseException as exc:
                retryable = self._unclaim_failure(attachment_id, generation)
                if not isinstance(exc, Exception):
                    raise
                if not retryable:
                    raise HTTPException(status_code=409, detail="草稿附件已被移除") from exc
                raise HTTPException(status_code=503, detail="草稿附件绑定失败，可重试") from exc
            updated = self.db.query(ComposerDraftAttachment).filter_by(
                attachment_id=attachment_id, state="claiming", generation=generation,
            ).update({"state": "claimed"}, synchronize_session=False)
            self.db.commit()
            self.db.expire_all()
            current = self.db.query(ComposerDraftAttachment).filter_by(attachment_id=attachment_id).one()
            if updated != 1:
                self._enqueue_path_cleanup(
                    current,
                    f"sessions/{session_id}/{posixpath.dirname(target_relative)}",
                )
                self.db.commit()
                raise HTTPException(status_code=409, detail="草稿附件已被移除")
            outputs.append(self._claimed_file(current))
            self.db.commit()
        return outputs

    def _unclaim_failure(self, attachment_id: str, generation: int) -> bool:
        self.db.rollback()
        row = self.db.query(ComposerDraftAttachment).filter_by(
            attachment_id=attachment_id,
        ).with_for_update().populate_existing().one()
        retryable = row.state == "claiming" and row.generation == generation
        if retryable:
            # Retry owns the same target, including any partial copy.
            row.state = "ready"
        elif row.state == "deleted" and row.claimed_path:
            # Delete/expiry can finish while the remote copy is still running.
            # Even a failed copy may recreate bytes after that cleanup, so its
            # completion must re-arm the precise target under the owner lock.
            self._enqueue_path_cleanup(
                row, f"sessions/{row.claimed_session_id}/{posixpath.dirname(row.claimed_path)}"
            )
        self.db.commit()
        return retryable

    @staticmethod
    def _claimed_file(row: ComposerDraftAttachment) -> dict:
        return {"attachment_id": row.attachment_id, "name": row.name, "path": row.claimed_path,
                "size": row.size_bytes, "type": row.content_type or "file", "modified": row.updated_at.isoformat(),
                "revision": None, "composer_draft_attachment_id": row.attachment_id}

    async def delete(self, *, user_id: str, draft_id: str, attachment_id: str) -> None:
        try:
            attachment_id = str(uuid.UUID(attachment_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="attachment_id 必须是 UUID") from exc
        if not draft_id or len(draft_id) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in draft_id):
            raise HTTPException(status_code=422, detail="draft_id 无效")
        row = self.db.query(ComposerDraftAttachment).filter(
            ComposerDraftAttachment.attachment_id == attachment_id,
        ).with_for_update().populate_existing().one_or_none()
        if row is not None and (row.user_id != user_id or row.draft_id != draft_id):
            raise HTTPException(status_code=404, detail="草稿附件不存在")
        if row is None or row.state == "deleted":
            if row is None:
                # A delete that races a not-yet-uploaded POST leaves a durable
                # tombstone, so the late upload cannot recreate this identity.
                self.db.add(ComposerDraftAttachment(
                    attachment_id=attachment_id, user_id=user_id, draft_id=draft_id,
                    state="deleted", generation=1, expires_at=now_naive(), deleted_at=now_naive(),
                ))
                try:
                    self.db.commit()
                except IntegrityError:
                    self.db.rollback()
                    await self.delete(user_id=user_id, draft_id=draft_id, attachment_id=attachment_id)
            return
        referenced = self._is_round_referenced(row)
        row.state = "deleting"; row.generation += 1
        self._enqueue_path_cleanup(row, _draft_root(row))
        if row.claimed_path and not referenced:
            self._enqueue_path_cleanup(row, f"sessions/{row.claimed_session_id}/{posixpath.dirname(row.claimed_path)}")
        row.state = "attached" if referenced else "deleted"
        row.deleted_at = now_naive()
        self.db.commit()

    def _is_round_referenced(self, row: ComposerDraftAttachment) -> bool:
        # JSON is historical TEXT; inspect rows rather than trusting a claim.
        import json
        for value, in self.db.query(Round.user_attachments).filter(Round.session_id == row.claimed_session_id).all():
            try:
                if any(
                    item.get("path") == row.claimed_path
                    and item.get("composer_draft_attachment_id") in (None, row.attachment_id)
                    for item in json.loads(value or "[]")
                ):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _enqueue_path_cleanup(self, row: ComposerDraftAttachment, relative_path: str) -> None:
        if not relative_path or not row.sandbox_id or not row.mount_path:
            return
        enqueue_cleanup(self.db, user_id=row.user_id, owner_kind="composer_draft_attachment",
                        owner_id=row.attachment_id, sandbox_id=row.sandbox_id,
                        mount_path=row.mount_path, relative_path=relative_path)


def enqueue_expired_composer_draft_cleanup() -> int:
    """Mark expired draft rows and queue only their precise owned paths.

    Claims are merely prepared Session copies.  A claimed copy is retained when
    a real Round already records its attachment identity.
    """
    with SessionLocal() as db:
        now = now_naive()
        rows = db.query(ComposerDraftAttachment).filter(
            ComposerDraftAttachment.state.in_(("uploading", "ready", "claiming", "claimed", "attached", "failed", "deleting")),
            ComposerDraftAttachment.expires_at <= now,
            ComposerDraftAttachment.deleted_at.is_(None),
        ).with_for_update().all()
        service = ComposerDraftAttachmentService(db)
        for row in rows:
            referenced = service._is_round_referenced(row)
            row.state = "deleting"; row.generation += 1
            service._enqueue_path_cleanup(row, _draft_root(row))
            if row.claimed_path and not referenced:
                service._enqueue_path_cleanup(
                    row, f"sessions/{row.claimed_session_id}/{posixpath.dirname(row.claimed_path)}"
                )
            row.state = "attached" if referenced else "deleted"
            row.deleted_at = now
        db.commit()
        return len(rows)
