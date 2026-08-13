"""Append-only SQL equivalent of Codex ``Compacted`` rollout items."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from src.agent.schema import Message
from src.api.models.context_checkpoint import ContextCheckpoint
from src.api.models.session import Session

logger = logging.getLogger(__name__)

CHECKPOINT_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class LoadedContextCheckpoint:
    checkpoint_id: str
    generation: int
    source_round_id: str | None
    source_message_sequence: int
    source_event_sequence: int
    trigger_phase: str
    summary: str
    messages: list[Message]


def canonical_messages_json(messages: Iterable[Message | dict[str, Any]]) -> str:
    payload: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, Message):
            payload.append(message.model_dump(exclude_none=True))
        elif isinstance(message, dict):
            payload.append(dict(message))
        else:
            raise TypeError(f"unsupported checkpoint message: {type(message)!r}")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def checkpoint_messages_token_count(messages: Iterable[Message]) -> int:
    from src.agent.context_compaction import approx_token_count, message_text

    return sum(approx_token_count(message_text(message)) + 4 for message in messages)


class ContextCheckpointService:
    """Persist and restore exact Codex replacement histories."""

    def __init__(self, db: DBSession):
        self.db = db

    def load_latest(
        self,
        session_id: str,
    ) -> LoadedContextCheckpoint | None:
        row = (
            self.db.query(ContextCheckpoint)
            .filter(ContextCheckpoint.session_id == session_id)
            .order_by(ContextCheckpoint.generation.desc())
            .first()
        )
        try:
            if row is None or int(row.schema_version or 0) != CHECKPOINT_SCHEMA_VERSION:
                return None
            raw = json.loads(row.replacement_messages_json or "[]")
            if not isinstance(raw, list):
                raise ValueError("replacement payload must be a list")
            messages = [Message.model_validate(item) for item in raw]
            if any(message.role == "system" for message in messages):
                raise ValueError("replacement must not contain system messages")
            return LoadedContextCheckpoint(
                checkpoint_id=row.checkpoint_id,
                generation=int(row.generation),
                source_round_id=row.source_round_id,
                source_message_sequence=int(row.source_message_sequence or 0),
                source_event_sequence=int(row.source_event_sequence or 0),
                trigger_phase=str(row.trigger_phase or "pre_turn"),
                summary=str(row.summary_text or ""),
                messages=messages,
            )
        except Exception:
            logger.exception(
                "Unable to parse latest v4 compacted history; rebuilding authoritative history: session=%s",
                session_id,
            )
            return None

    def save(
        self,
        *,
        session_id: str,
        source_round_id: str | None,
        source_message_sequence: int = 0,
        source_event_sequence: int = 0,
        trigger_phase: str = "pre_turn",
        summary: str = "",
        messages: list[Message],
        source_token_count: int | None,
        replacement_token_count: int | None,
    ) -> LoadedContextCheckpoint:
        replacement = [message.model_copy(deep=True) for message in messages]
        if any(message.role == "system" for message in replacement):
            raise ValueError("replacement must not contain system messages")
        replacement_json = canonical_messages_json(replacement)
        # Serialize generation allocation per conversation. PostgreSQL locks
        # the parent row; SQLite safely ignores FOR UPDATE in unit tests.
        self.db.query(Session.id).filter(Session.id == session_id).with_for_update().one()
        generation = int(
            self.db.query(func.coalesce(func.max(ContextCheckpoint.generation), 0))
            .filter(ContextCheckpoint.session_id == session_id)
            .scalar()
            or 0
        ) + 1
        row = ContextCheckpoint(
            checkpoint_id=str(uuid.uuid4()),
            session_id=session_id,
            generation=generation,
            source_round_id=source_round_id,
            source_message_sequence=max(int(source_message_sequence or 0), 0),
            source_event_sequence=max(int(source_event_sequence or 0), 0),
            trigger_phase=trigger_phase,
            summary_text=summary,
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            replacement_messages_json=replacement_json,
            source_token_count=source_token_count,
            replacement_token_count=(
                checkpoint_messages_token_count(replacement)
                if replacement_token_count is None
                else replacement_token_count
            ),
            replacement_sha256=None,
        )
        self.db.add(row)
        self.db.commit()
        return LoadedContextCheckpoint(
            checkpoint_id=row.checkpoint_id,
            generation=generation,
            source_round_id=source_round_id,
            source_message_sequence=row.source_message_sequence,
            source_event_sequence=row.source_event_sequence,
            trigger_phase=trigger_phase,
            summary=summary,
            messages=replacement,
        )
