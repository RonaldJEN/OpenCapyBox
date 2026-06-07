"""Delivery boundary placeholder for future external channels."""
from __future__ import annotations

from src.api.schemas.turn import DeliveryIntent, DeliveryReceipt


class DeliveryService:
    """No-op delivery service for the Web-only kernel stage."""

    async def deliver(self, intent: DeliveryIntent) -> DeliveryReceipt | None:
        if intent.durability == "disabled":
            return None
        return DeliveryReceipt(
            intent_id=intent.id,
            platform_message_ids=[],
            status="skipped",
            raw={"status": "noop", "channel": intent.channel},
        )


_GLOBAL_DELIVERY_SERVICE = DeliveryService()


def get_delivery_service() -> DeliveryService:
    return _GLOBAL_DELIVERY_SERVICE
