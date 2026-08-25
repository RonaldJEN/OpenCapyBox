"""Typed turn and channel lifecycle contracts."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.chat import ContentBlock
from src.agent.schema.agui_events import Context


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WebReplyRoute(StrictModel):
    kind: Literal["web_sse"] = "web_sse"
    session_id: str


class ChannelMessageReplyRoute(StrictModel):
    kind: Literal["channel_message"] = "channel_message"
    channel: str
    account_id: str | None = None
    peer_kind: Literal["direct", "group", "thread"]
    peer_id: str
    external_thread_id: str | None = None


class NoReplyRoute(StrictModel):
    kind: Literal["none"] = "none"


ReplyRoute = Annotated[
    WebReplyRoute | ChannelMessageReplyRoute | NoReplyRoute,
    Field(discriminator="kind"),
]


class Attachment(StrictModel):
    kind: str = "file"
    path: str | None = None
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    raw: dict[str, Any] | None = None


class NormalizedInboundTurn(StrictModel):
    channel: str
    user_id: str
    account_id: str | None = None
    peer_kind: Literal["web", "direct", "group", "thread", "cron", "webhook"]
    peer_id: str
    external_thread_id: str | None = None
    content: list[ContentBlock]
    context: list[Context] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    reply_route: ReplyRoute
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class NormalizedResumeTurn(StrictModel):
    channel: str
    user_id: str
    session_id: str
    interrupt_id: str
    answers: dict[str, str]
    reply_route: ReplyRoute
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnCancelTarget(StrictModel):
    user_id: str
    session_id: str
    round_id: str | None = None
    root_run_id: str | None = None
    requested_after: datetime | None = None
    channel: str = "web"
    reason: str = "user_cancelled"


class RunHandle(StrictModel):
    session_id: str
    round_id: str
    run_id: str
    root_run_id: str
    parent_run_id: str | None = None
    reply_route: ReplyRoute
    started_at: datetime


class RoundData(StrictModel):
    round_id: str
    parent_run_id: str | None = None
    status: Literal[
        "running",
        "completed",
        "failed",
        "waiting_interaction",
        "cancelled",
        "max_steps_reached",
    ]


class MessageTarget(StrictModel):
    peer_kind: Literal["direct", "group", "thread", "web", "cron", "webhook"]
    peer_id: str
    external_thread_id: str | None = None
    account_id: str | None = None


class MessageBody(StrictModel):
    text: str | None = None
    blocks: list[dict[str, Any]] = Field(default_factory=list)


class MessageRelation(StrictModel):
    session_id: str | None = None
    round_id: str | None = None
    run_id: str | None = None
    parent_message_id: str | None = None


class MessageOrigin(StrictModel):
    idempotency_key: str | None = None
    received_at: datetime | None = None
    raw_event_id: str | None = None


class ChannelMessage(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    channel: str
    account_id: str | None = None
    direction: Literal["inbound", "outbound"]
    target: MessageTarget
    body: MessageBody | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    relation: MessageRelation | None = None
    origin: MessageOrigin | None = None
    raw: dict[str, Any] | None = None


class DeliveryIntent(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    channel: str
    message: ChannelMessage
    durability: Literal["required", "best_effort", "disabled"]
    idempotency_key: str


class DeliveryReceipt(StrictModel):
    intent_id: str
    status: Literal["sent", "skipped", "failed"] = "sent"
    platform_message_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    retryable: bool = False
    raw: dict[str, Any] | None = None


class ChannelReceiveResult(StrictModel):
    disposition: Literal["accepted", "duplicate", "ignored", "unauthorized", "failed"]
    message: ChannelMessage | None = None
    turn: NormalizedInboundTurn | NormalizedResumeTurn | None = None
    ack_required: bool = False
    dedupe_key: str | None = None
    error_code: str | None = None
    raw: dict[str, Any] | None = None


class LiveUpdate(StrictModel):
    channel: str
    account_id: str | None = None
    kind: Literal["typing", "progress", "preview", "stream_delta", "heartbeat"]
    target: MessageTarget
    body: MessageBody | None = None
    relation: MessageRelation | None = None
    durability: Literal["best_effort", "disabled"] = "best_effort"
    raw: dict[str, Any] | None = None


class ChannelStateSnapshot(StrictModel):
    channel: str
    account_id: str | None = None
    target: MessageTarget
    binding_id: str | None = None
    session_id: str | None = None
    active_run_id: str | None = None
    last_inbound_dedupe_key: str | None = None
    last_delivery_intent_id: str | None = None
    last_receipt: DeliveryReceipt | None = None
    raw: dict[str, Any] | None = None


class CancelResult(StrictModel):
    request_id: str
    state: Literal["requested", "acked", "completed"]
    target_run_id: str | None = None
