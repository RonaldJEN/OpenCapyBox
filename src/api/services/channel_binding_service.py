"""Session binding service for web and future external channels."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.api.models.channel_session_binding import ChannelSessionBinding
from src.api.models.session import Session
from src.api.schemas.turn import ReplyRoute
from src.api.services.auth_service import get_enabled_user


class ChannelBindingService:
    """Resolve or lazily create channel-to-session bindings."""

    def build_binding_key(
        self,
        *,
        channel: str,
        account_id: str | None,
        peer_kind: str,
        peer_id: str,
        external_thread_id: str | None = None,
    ) -> str:
        payload = {
            "account_id": account_id,
            "channel": channel,
            "external_thread_id": external_thread_id,
            "peer_id": peer_id,
            "peer_kind": peer_kind,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get_or_create_binding(
        self,
        db: DBSession,
        *,
        user_id: str,
        session_id: str,
        channel: str = "web",
        account_id: str | None = None,
        peer_kind: str = "web",
        peer_id: str | None = None,
        external_thread_id: str | None = None,
        reply_route: ReplyRoute | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChannelSessionBinding:
        get_enabled_user(db, user_id)
        session = db.query(Session).filter(Session.id == session_id, Session.user_id == user_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")

        actual_peer_id = peer_id or session_id
        binding_key = self.build_binding_key(
            channel=channel,
            account_id=account_id,
            peer_kind=peer_kind,
            peer_id=actual_peer_id,
            external_thread_id=external_thread_id,
        )

        existing = self._find(db, user_id=user_id, binding_key=binding_key)
        if existing:
            return existing

        binding = ChannelSessionBinding(
            user_id=user_id,
            session_id=session_id,
            channel=channel,
            account_id=account_id,
            peer_kind=peer_kind,
            peer_id=actual_peer_id,
            external_thread_id=external_thread_id,
            binding_key=binding_key,
            reply_route_json=self._dump_model(reply_route),
            metadata_json=self._dump_dict(metadata),
        )
        db.add(binding)
        try:
            db.commit()
            db.refresh(binding)
            return binding
        except IntegrityError:
            db.rollback()
            existing = self._find(db, user_id=user_id, binding_key=binding_key)
            if existing:
                return existing
            raise

    @staticmethod
    def _find(db: DBSession, *, user_id: str, binding_key: str) -> ChannelSessionBinding | None:
        return (
            db.query(ChannelSessionBinding)
            .filter(
                ChannelSessionBinding.user_id == user_id,
                ChannelSessionBinding.binding_key == binding_key,
            )
            .first()
        )

    @staticmethod
    def _dump_model(value: ReplyRoute | None) -> str | None:
        if value is None:
            return None
        return value.model_dump_json(exclude_none=True)

    @staticmethod
    def _dump_dict(value: dict[str, Any] | None) -> str | None:
        if not value:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True)


_GLOBAL_CHANNEL_BINDING_SERVICE = ChannelBindingService()


def get_channel_binding_service() -> ChannelBindingService:
    return _GLOBAL_CHANNEL_BINDING_SERVICE
