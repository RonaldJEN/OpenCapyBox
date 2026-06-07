"""Projection boundary from AG-UI events to channel delivery intents."""
from __future__ import annotations

from typing import Any

from src.agent.schema.agui_events import EventType
from src.api.schemas.turn import (
    ChannelMessage,
    ChannelMessageReplyRoute,
    DeliveryIntent,
    MessageBody,
    MessageRelation,
    MessageTarget,
    RunHandle,
)


class ChannelProjection:
    """Render channel messages without sending them over the network."""

    def project_event(self, *, handle: RunHandle, event: dict[str, Any]) -> list[DeliveryIntent]:
        route = handle.reply_route
        if not isinstance(route, ChannelMessageReplyRoute):
            return []
        if event.get("type") != EventType.RUN_FINISHED.value:
            return []

        text = self._extract_final_text(event)
        if not text:
            return []

        message = ChannelMessage(
            channel=route.channel,
            account_id=route.account_id,
            direction="outbound",
            target=MessageTarget(
                peer_kind=route.peer_kind,
                peer_id=route.peer_id,
                external_thread_id=route.external_thread_id,
                account_id=route.account_id,
            ),
            body=MessageBody(text=text),
            relation=MessageRelation(
                session_id=handle.session_id,
                round_id=handle.round_id,
                run_id=handle.run_id,
            ),
            raw=event,
        )
        return [
            DeliveryIntent(
                channel=route.channel,
                message=message,
                durability="best_effort",
                idempotency_key=f"{handle.run_id}:{event.get('sequence', 'terminal')}:final",
            )
        ]

    @staticmethod
    def _extract_final_text(event: dict[str, Any]) -> str:
        result = event.get("result")
        if isinstance(result, dict):
            value = result.get("finalResponse") or result.get("final_response")
            if isinstance(value, str):
                return value
        value = event.get("message")
        return value if isinstance(value, str) else ""
