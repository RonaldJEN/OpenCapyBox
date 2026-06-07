"""AG-UI event runtime bus.

PostgreSQL remains the durable source of truth.  The in-process subscriber
registry is only the live fanout layer for the single-worker runtime.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Iterable

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.agent.schema.agui_events import AGUIEvent, EventType
from src.api.models.agui_event import AGUIEventLog
from src.api.models.round import Round

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredEvent:
    """A durable event after DB sequence allocation."""

    run_id: str
    sequence: int
    event: dict[str, Any]
    log_id: int | None = None


class SequencedAGUIEvent:
    """AG-UI event wrapper that preserves attribute access and injects sequence.

    The project event models do not define a ``sequence`` field.  This wrapper
    lets existing code/tests keep using ``event.type`` while SSE encoding emits
    the committed dict payload.
    """

    def __init__(self, base_event: AGUIEvent, stored_event: StoredEvent):
        self._base_event = base_event
        self._stored_event = stored_event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base_event, name)

    @property
    def type(self):
        return self._base_event.type

    @property
    def run_id(self) -> str | None:
        return getattr(self._base_event, "run_id", None)

    @property
    def sequence(self) -> int:
        return self._stored_event.sequence

    def model_dump(self, *args, **kwargs) -> dict[str, Any]:
        return dict(self._stored_event.event)

    def model_dump_json(self, *args, **kwargs) -> str:
        return json.dumps(self._stored_event.event, ensure_ascii=False)


class _StreamViewFilter:
    """Prevent one subscriber from appending both raw live and replay aggregate."""

    _BUFFER_SUFFIXES = {
        "_TEXT": EventType.TEXT_MESSAGE_CONTENT.value,
        "_THINKING": EventType.THINKING_TEXT_MESSAGE_CONTENT.value,
        "_TOOL": EventType.TOOL_CALL_ARGS.value,
    }

    _DELTA_TYPES = {
        EventType.TEXT_MESSAGE_CONTENT.value,
        EventType.THINKING_TEXT_MESSAGE_CONTENT.value,
        EventType.TOOL_CALL_ARGS.value,
    }

    def __init__(self) -> None:
        self._live_raw_segments: set[str] = set()
        self._replayed_aggregate_segments: set[str] = set()
        self._aggregate_required_segments: set[str] = set()

    def mark_buffered_segments(self, stream_buffers: dict[str, str]) -> None:
        """Mark segments that already accumulated live-only content before subscribe."""
        for buffer_key, accumulated in stream_buffers.items():
            if not accumulated:
                continue
            segment_key = self._segment_key_from_buffer_key(buffer_key)
            if segment_key:
                self._aggregate_required_segments.add(segment_key)

    def is_duplicate(self, event: dict[str, Any]) -> bool:
        if event.get("type") not in self._DELTA_TYPES:
            return False
        segment_key = self._segment_key(event)
        if not segment_key:
            return False

        if AguiEventBus._parse_sequence(event) is None:
            if segment_key in self._replayed_aggregate_segments:
                return True
            if segment_key in self._aggregate_required_segments:
                return True
            self._live_raw_segments.add(segment_key)
            return False

        if segment_key in self._aggregate_required_segments:
            self._aggregate_required_segments.discard(segment_key)
            self._replayed_aggregate_segments.add(segment_key)
            return False
        if segment_key in self._live_raw_segments:
            return True
        self._replayed_aggregate_segments.add(segment_key)
        return False

    @staticmethod
    def _segment_key(event: dict[str, Any]) -> str | None:
        event_type = event.get("type")
        if event_type in {
            EventType.TEXT_MESSAGE_CONTENT.value,
            EventType.THINKING_TEXT_MESSAGE_CONTENT.value,
        }:
            message_id = event.get("messageId")
            return f"{event_type}:{message_id}" if message_id else None
        if event_type == EventType.TOOL_CALL_ARGS.value:
            tool_call_id = event.get("toolCallId")
            return f"{event_type}:{tool_call_id}" if tool_call_id else None
        return None

    @classmethod
    def _segment_key_from_buffer_key(cls, buffer_key: str) -> str | None:
        for suffix, event_type in cls._BUFFER_SUFFIXES.items():
            if buffer_key.endswith(suffix):
                segment_id = buffer_key[: -len(suffix)]
                return f"{event_type}:{segment_id}" if segment_id else None
        return None


class AguiEventBus:
    """Durable AG-UI event writer plus in-process fanout."""

    _subscribers: dict[str, list[asyncio.Queue]] = {}
    _subscribers_lock = threading.Lock()
    _stream_buffers: dict[str, dict[str, str]] = {}
    _terminal_runs: set[str] = set()

    _STREAM_DELTA_EVENTS = {
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.THINKING_TEXT_MESSAGE_CONTENT,
        EventType.TOOL_CALL_ARGS,
    }

    _STREAM_END_EVENTS = {
        EventType.TEXT_MESSAGE_END: (EventType.TEXT_MESSAGE_CONTENT, "_TEXT"),
        EventType.THINKING_TEXT_MESSAGE_END: (EventType.THINKING_TEXT_MESSAGE_CONTENT, "_THINKING"),
        EventType.TOOL_CALL_END: (EventType.TOOL_CALL_ARGS, "_TOOL"),
    }

    _TERMINAL_TYPES = {EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value}

    def __init__(self, db: DBSession | Callable[[], DBSession]):
        if callable(db) and not hasattr(db, "query"):
            self._session_factory: Callable[[], DBSession] | None = db
            self._db: DBSession | None = None
        else:
            self._session_factory = None
            self._db = db  # type: ignore[assignment]

    @property
    def subscribers(self) -> dict[str, list[asyncio.Queue]]:
        return self._subscribers

    @property
    def subscribers_lock(self) -> threading.Lock:
        return self._subscribers_lock

    @contextmanager
    def _session_scope(self):
        if self._session_factory is None:
            yield self._db
            return
        db = self._session_factory()
        try:
            yield db
        finally:
            db.close()

    async def publish(self, run_id: str, event: AGUIEvent | dict[str, Any]) -> StoredEvent | None:
        """Persist a durable event and fan it out after commit.

        Streaming delta events are live-only and return ``None``.  Their
        aggregated replay representation is written when the matching END event
        arrives.
        """
        event_dict = self._event_to_dict(event)
        event_type = self._normalise_event_type(event_dict.get("type"))
        if event_type in self._TERMINAL_TYPES:
            raise ValueError("stream terminal events must use RunCompletionService.complete()")

        with self._session_scope() as db:
            if db is None:
                raise RuntimeError("AguiEventBus has no DB session")

            if self._is_round_terminal(db, run_id):
                db.commit()
                self._terminal_runs.add(run_id)
                logger.info("Run %s 已终态，丢弃迟到事件: %s", run_id, event_type)
                return None

            enum_type = self._event_type_to_enum(event_type)
            if enum_type in self._STREAM_DELTA_EVENTS:
                self._buffer_delta(run_id, enum_type, event, event_dict)
                db.commit()
                await self.publish_ephemeral(run_id, event_dict)
                return None

            pending_payloads: list[dict[str, Any]] = []
            pending_meta: list[dict[str, Any]] = []

            if enum_type in self._STREAM_END_EVENTS:
                synthetic = self._build_synthetic_aggregate(run_id, enum_type, event, event_dict)
                if synthetic is not None:
                    synthetic_type, synthetic_payload, synthetic_meta = synthetic
                    pending_payloads.append(synthetic_payload)
                    pending_meta.append({"event_type": synthetic_type, **synthetic_meta})

            pending_payloads.append(event_dict)
            pending_meta.append(self._extract_event_meta(event, event_dict))
            stored_events = self._write_events_with_sequences(db, run_id, pending_payloads, pending_meta)
            stored = stored_events[-1]

        for committed in stored_events:
            await self.publish_committed(run_id, committed.event)
        return stored

    async def publish_ephemeral(self, run_id: str, event: AGUIEvent | dict[str, Any]) -> None:
        """Fan out a live-only event without touching durable sequence."""
        await self._fanout(run_id, self._event_to_dict(event))

    async def publish_committed(self, run_id: str, event: dict[str, Any]) -> None:
        """Fan out an already-committed durable event."""
        await self._fanout(run_id, event)

    async def replay(self, run_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        """Load durable events from PostgreSQL, injecting top-level sequence."""
        with self._session_scope() as db:
            if db is None:
                raise RuntimeError("AguiEventBus has no DB session")
            rows = (
                db.query(AGUIEventLog)
                .filter(
                    AGUIEventLog.run_id == run_id,
                    AGUIEventLog.sequence > after_sequence,
                )
                .order_by(AGUIEventLog.sequence)
                .all()
            )
            events = [self._payload_with_sequence(row) for row in rows]
            db.rollback()
            return events

    async def subscribe(self, run_id: str, after_sequence: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Replay, register, catch up, then stream live events with de-dupe."""
        latest_sequence = after_sequence
        seen_sequences: set[int] = set()
        stream_view_filter = _StreamViewFilter()

        for event in await self.replay(run_id, after_sequence):
            seq = self._parse_sequence(event)
            if seq is not None:
                latest_sequence = max(latest_sequence, seq)
                seen_sequences.add(seq)
            if stream_view_filter.is_duplicate(event):
                continue
            yield event
            if event.get("type") in self._TERMINAL_TYPES:
                return

        if await self.ensure_terminal(run_id):
            return

        queue: asyncio.Queue = asyncio.Queue()
        with self._subscribers_lock:
            self._subscribers.setdefault(run_id, []).append(queue)
        stream_view_filter.mark_buffered_segments(dict(self._stream_buffers.get(run_id, {})))

        try:
            for event in await self.replay(run_id, latest_sequence):
                seq = self._parse_sequence(event)
                if seq is not None:
                    if seq in seen_sequences:
                        continue
                    latest_sequence = max(latest_sequence, seq)
                    seen_sequences.add(seq)
                if stream_view_filter.is_duplicate(event):
                    continue
                yield event
                if event.get("type") in self._TERMINAL_TYPES:
                    return

            while True:
                event = await queue.get()
                seq = self._parse_sequence(event)
                if seq is not None:
                    if seq in seen_sequences:
                        continue
                    latest_sequence = max(latest_sequence, seq)
                    seen_sequences.add(seq)
                if stream_view_filter.is_duplicate(event):
                    continue
                yield event
                if event.get("type") in self._TERMINAL_TYPES:
                    return
        finally:
            self.remove_subscriber(run_id, queue)

    async def ensure_terminal(self, run_id: str) -> bool:
        """Return whether the run already has a durable stream terminal event."""
        with self._session_scope() as db:
            if db is None:
                raise RuntimeError("AguiEventBus has no DB session")
            exists = (
                db.query(AGUIEventLog.id)
                .filter(
                    AGUIEventLog.run_id == run_id,
                    AGUIEventLog.event_type.in_(tuple(self._TERMINAL_TYPES)),
                )
                .first()
                is not None
            )
            db.rollback()
            return exists

    def cleanup_subscribers(self, run_id: str) -> bool:
        removed = False
        with self._subscribers_lock:
            if run_id in self._subscribers:
                del self._subscribers[run_id]
                removed = True
        return removed

    def remove_subscriber(self, run_id: str, queue: asyncio.Queue) -> tuple[bool, int]:
        removed = False
        remaining = 0
        with self._subscribers_lock:
            queues = self._subscribers.get(run_id)
            if queues and queue in queues:
                queues.remove(queue)
                removed = True
                remaining = len(queues)
                if not queues:
                    del self._subscribers[run_id]
        return removed, remaining

    async def _fanout(self, run_id: str, event: dict[str, Any]) -> None:
        with self._subscribers_lock:
            subscribers = list(self._subscribers.get(run_id, []))

        failed_queues: list[asyncio.Queue] = []
        for queue in subscribers:
            try:
                await queue.put(event)
            except Exception:
                failed_queues.append(queue)

        if not failed_queues:
            return

        with self._subscribers_lock:
            active = self._subscribers.get(run_id)
            if not active:
                return
            for queue in failed_queues:
                if queue in active:
                    active.remove(queue)
            if not active:
                del self._subscribers[run_id]

    def _write_events_with_sequences(
        self,
        db: DBSession,
        run_id: str,
        payloads: Iterable[dict[str, Any]],
        metadata: Iterable[dict[str, Any]],
    ) -> list[StoredEvent]:
        payload_list = list(payloads)
        metadata_list = list(metadata)
        if len(payload_list) != len(metadata_list):
            raise ValueError("payload and metadata lengths must match")

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self._lock_round_row_if_possible(db, run_id)
                next_sequence = self._current_high_water(db, run_id) + 1
                stored_events: list[StoredEvent] = []
                for offset, (payload, meta) in enumerate(zip(payload_list, metadata_list)):
                    sequence = next_sequence + offset
                    payload_with_sequence = dict(payload)
                    payload_with_sequence["sequence"] = sequence
                    event_log = AGUIEventLog(
                        run_id=run_id,
                        event_type=meta["event_type"],
                        message_id=meta.get("message_id"),
                        tool_call_id=meta.get("tool_call_id"),
                        timestamp=meta.get("timestamp"),
                        payload=json.dumps(payload_with_sequence, ensure_ascii=False),
                        sequence=sequence,
                    )
                    db.add(event_log)
                    stored_events.append(StoredEvent(run_id, sequence, payload_with_sequence))
                db.commit()
                return stored_events
            except IntegrityError as exc:
                last_exc = exc
                db.rollback()
                if attempt == 2:
                    raise
                logger.warning(
                    "agui_events sequence unique conflict, retrying: run=%s attempt=%d",
                    run_id,
                    attempt + 1,
                )
        raise last_exc  # type: ignore[misc]

    def _lock_round_row_if_possible(self, db: DBSession, run_id: str) -> None:
        round_obj = db.query(Round).filter(Round.id == run_id).first()
        if round_obj is None:
            return
        if not self._is_mapped_instance(round_obj):
            return
        try:
            db.refresh(round_obj, with_for_update=True)
        except TypeError:
            db.refresh(round_obj)

    @staticmethod
    def _is_mapped_instance(obj: Any) -> bool:
        try:
            from sqlalchemy import inspect as sa_inspect

            sa_inspect(obj)
            return True
        except Exception:
            return False

    @staticmethod
    def _current_high_water(db: DBSession, run_id: str) -> int:
        value = (
            db.query(func.coalesce(func.max(AGUIEventLog.sequence), 0))
            .filter(AGUIEventLog.run_id == run_id)
            .scalar()
        )
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _is_round_terminal(self, db: DBSession, run_id: str) -> bool:
        if run_id in self._terminal_runs:
            return True
        row = db.query(Round.status).filter(Round.id == run_id).first()
        status = row[0] if row else None
        is_terminal = bool(status and status in Round.SUBSCRIBE_TERMINAL_STATUSES)
        if is_terminal:
            self._terminal_runs.add(run_id)
        return is_terminal

    @staticmethod
    def _payload_with_sequence(row: AGUIEventLog) -> dict[str, Any]:
        try:
            payload = json.loads(row.payload)
        except json.JSONDecodeError:
            payload = {"type": row.event_type}
        payload["sequence"] = row.sequence
        return payload

    @staticmethod
    def _parse_sequence(event_data: dict[str, Any]) -> int | None:
        for key in ("sequence", "_sequence"):
            value = event_data.get(key)
            if isinstance(value, int):
                return value
        return None

    @staticmethod
    def _event_to_dict(event: AGUIEvent | dict[str, Any]) -> dict[str, Any]:
        if isinstance(event, dict):
            return dict(event)
        return event.model_dump(by_alias=True, exclude_none=True, mode="json")

    @staticmethod
    def _normalise_event_type(value: Any) -> str:
        if isinstance(value, EventType):
            return value.value
        return str(value or "")

    @staticmethod
    def _event_type_to_enum(event_type: str) -> EventType | None:
        try:
            return EventType(event_type)
        except ValueError:
            return None

    def _extract_event_meta(self, event: AGUIEvent | dict[str, Any], event_dict: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_type": self._normalise_event_type(event_dict.get("type")),
            "message_id": event_dict.get("messageId") or getattr(event, "message_id", None),
            "tool_call_id": event_dict.get("toolCallId") or getattr(event, "tool_call_id", None),
            "timestamp": event_dict.get("timestamp") or getattr(event, "timestamp", None),
        }

    def _buffer_delta(
        self,
        run_id: str,
        event_type: EventType | None,
        event: AGUIEvent | dict[str, Any],
        event_dict: dict[str, Any],
    ) -> None:
        buffer_key = self._get_buffer_key(event_type, event, event_dict)
        if not buffer_key:
            return
        self._stream_buffers.setdefault(run_id, {})
        self._stream_buffers[run_id][buffer_key] = (
            self._stream_buffers[run_id].get(buffer_key, "")
            + str(event_dict.get("delta") or getattr(event, "delta", "") or "")
        )

    def _build_synthetic_aggregate(
        self,
        run_id: str,
        event_type: EventType | None,
        event: AGUIEvent | dict[str, Any],
        event_dict: dict[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        if event_type not in self._STREAM_END_EVENTS:
            return None
        content_event_type, _suffix = self._STREAM_END_EVENTS[event_type]
        buffer_key = self._get_buffer_key(event_type, event, event_dict)
        if not buffer_key:
            return None
        accumulated = self._stream_buffers.get(run_id, {}).pop(buffer_key, "")
        if not accumulated:
            return None

        payload: dict[str, Any] = {
            "type": content_event_type.value,
            "delta": accumulated,
        }
        message_id = event_dict.get("messageId") or getattr(event, "message_id", None)
        tool_call_id = event_dict.get("toolCallId") or getattr(event, "tool_call_id", None)
        if message_id:
            payload["messageId"] = message_id
        if tool_call_id:
            payload["toolCallId"] = tool_call_id
        meta = {
            "message_id": message_id,
            "tool_call_id": tool_call_id,
            "timestamp": None,
        }
        return content_event_type.value, payload, meta

    @staticmethod
    def _get_buffer_key(
        event_type: EventType | None,
        event: AGUIEvent | dict[str, Any],
        event_dict: dict[str, Any],
    ) -> str | None:
        message_id = event_dict.get("messageId") or getattr(event, "message_id", None)
        tool_call_id = event_dict.get("toolCallId") or getattr(event, "tool_call_id", None)
        if event_type in {EventType.TEXT_MESSAGE_CONTENT, EventType.TEXT_MESSAGE_END}:
            return f"{message_id}_TEXT" if message_id else None
        if event_type in {
            EventType.THINKING_TEXT_MESSAGE_CONTENT,
            EventType.THINKING_TEXT_MESSAGE_END,
        }:
            return f"{message_id}_THINKING" if message_id else None
        if event_type in {EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END}:
            return f"{tool_call_id}_TOOL" if tool_call_id else None
        return None


_GLOBAL_EVENT_BUS: AguiEventBus | None = None


def get_agui_event_bus() -> AguiEventBus:
    global _GLOBAL_EVENT_BUS
    if _GLOBAL_EVENT_BUS is None:
        from src.api.models.database import SessionLocal

        _GLOBAL_EVENT_BUS = AguiEventBus(SessionLocal)
    return _GLOBAL_EVENT_BUS
