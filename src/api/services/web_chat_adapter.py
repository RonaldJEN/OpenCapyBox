"""Web protocol adapter for typed turn contracts."""
from __future__ import annotations

from datetime import datetime

from src.api.schemas.chat import FileContentBlock, ImageContentBlock, ResumeRequest, SendMessageRequest
from src.api.schemas.turn import (
    Attachment,
    NormalizedInboundTurn,
    NormalizedResumeTurn,
    TurnCancelTarget,
    WebReplyRoute,
)


class WebChatAdapter:
    """Translate Web chat requests into normalized inbound turns."""

    channel = "web"

    def normalize_send(
        self,
        *,
        session_id: str,
        user_id: str,
        request: SendMessageRequest,
    ) -> NormalizedInboundTurn:
        return NormalizedInboundTurn(
            channel=self.channel,
            user_id=user_id,
            peer_kind="web",
            peer_id=session_id,
            content=request.content,
            attachments=_extract_attachments(request),
            reply_route=WebReplyRoute(session_id=session_id),
            idempotency_key=request.idempotency_key,
            metadata={"session_id": session_id},
        )


class WebResumeAdapter:
    """Translate Web resume requests into normalized resume turns."""

    channel = "web"

    def normalize_resume(
        self,
        *,
        session_id: str,
        user_id: str,
        request: ResumeRequest,
    ) -> NormalizedResumeTurn:
        return NormalizedResumeTurn(
            channel=self.channel,
            user_id=user_id,
            session_id=session_id,
            interrupt_id=request.interrupt_id,
            answers=request.answers,
            reply_route=WebReplyRoute(session_id=session_id),
            metadata={"session_id": session_id},
        )


class WebCancelAdapter:
    """Translate Web abort requests into precise cancel targets."""

    channel = "web"

    def normalize_cancel(
        self,
        *,
        session_id: str,
        user_id: str,
        round_id: str | None = None,
        root_run_id: str | None = None,
        requested_after: datetime | None = None,
        reason: str = "user_cancelled",
    ) -> TurnCancelTarget:
        return TurnCancelTarget(
            user_id=user_id,
            session_id=session_id,
            round_id=round_id,
            root_run_id=root_run_id,
            requested_after=requested_after,
            channel=self.channel,
            reason=reason,
        )


def _extract_attachments(request: SendMessageRequest) -> list[Attachment]:
    attachments: list[Attachment] = []
    for block in request.content:
        if isinstance(block, FileContentBlock):
            data = block.file.model_dump(exclude_none=True)
            attachments.append(
                Attachment(
                    kind="file",
                    path=data.get("path"),
                    name=data.get("name"),
                    mime_type=data.get("mime_type"),
                    size=data.get("size"),
                    raw=data,
                )
            )
        elif isinstance(block, ImageContentBlock) and block.file:
            data = dict(block.file)
            attachments.append(
                Attachment(
                    kind="image",
                    path=data.get("path"),
                    name=data.get("name"),
                    mime_type=data.get("mime_type"),
                    size=data.get("size"),
                    raw=data,
                )
            )
    return attachments
