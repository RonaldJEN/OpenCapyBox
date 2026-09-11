"""Merge task identity updates into their parent's ordinary fenced event stream."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator

from src.agent.schema.agui_events import AGUIEvent, CustomEvent


class SubagentActivity:
    def __init__(self) -> None:
        self._updates: deque[CustomEvent] = deque()
        self._ready = asyncio.Event()

    def put(self, value: dict) -> None:
        self._updates.append(CustomEvent(name="subagent_run_updated", value=value))
        self._ready.set()

    async def merge(self, events: AsyncIterator[AGUIEvent]) -> AsyncIterator[AGUIEvent]:
        iterator = events.__aiter__()
        next_event = asyncio.create_task(anext(iterator))
        next_update = asyncio.create_task(self._ready.wait())
        try:
            while True:
                await asyncio.wait((next_event, next_update), return_when=asyncio.FIRST_COMPLETED)
                # Child updates precede the parent tool result / terminal that
                # follows them, including when both become ready together.
                if next_update.done():
                    self._ready.clear()
                    next_update = asyncio.create_task(self._ready.wait())
                while self._updates:
                    yield self._updates.popleft()
                if next_event.done():
                    try:
                        event = next_event.result()
                    except StopAsyncIteration:
                        return
                    yield event
                    next_event = asyncio.create_task(anext(iterator))
        finally:
            for task in (next_event, next_update):
                if not task.done():
                    task.cancel()
            await asyncio.gather(next_event, next_update, return_exceptions=True)
            await iterator.aclose()
            self._updates.clear()
