"""Temporary composer uploads.  They deliberately do not create Sessions."""

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.api.models.database import get_db
from src.api.services.composer_draft_attachment_service import ComposerDraftAttachmentService

router = APIRouter()


class DraftAttachmentClaimRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=64)
    attachment_ids: list[str] = Field(min_length=1, max_length=20)


@router.post("/composer-drafts/{draft_id}/attachments")
async def upload_draft_attachment(draft_id: str, attachment_id: str = Form(...), file: UploadFile = File(...),
                                  user_id: str = Depends(get_current_user), db: DBSession = Depends(get_db)):
    return await ComposerDraftAttachmentService(db).upload(
        user_id=user_id, draft_id=draft_id, attachment_id=attachment_id, file=file)


@router.delete("/composer-drafts/{draft_id}/attachments/{attachment_id}", status_code=204)
async def delete_draft_attachment(draft_id: str, attachment_id: str, user_id: str = Depends(get_current_user),
                                  db: DBSession = Depends(get_db)):
    await ComposerDraftAttachmentService(db).delete(user_id=user_id, draft_id=draft_id, attachment_id=attachment_id)


@router.post("/sessions/{session_id}/draft-attachments/claim")
async def claim_draft_attachments(session_id: str, request: DraftAttachmentClaimRequest,
                                  user_id: str = Depends(get_current_user), db: DBSession = Depends(get_db)):
    files = await ComposerDraftAttachmentService(db).claim(
        user_id=user_id, session_id=session_id, draft_id=request.draft_id, attachment_ids=request.attachment_ids)
    return {"files": files}
