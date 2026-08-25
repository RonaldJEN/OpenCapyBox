"""Runtime-neutral execution boundary used by TurnOrchestrator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

from src.agent.schema.agui_events import AGUIEvent
from src.api.schemas.turn import NormalizedInboundTurn, NormalizedResumeTurn
from src.api.services.agent_service import AgentService, PreparedAgentRun


class AgentEngine(Protocol):
    """Minimal contract a future local or external Agent runtime must implement."""

    def set_cancel_token(self, cancel_token: asyncio.Event) -> None: ...

    def set_liveness_token(self, liveness_token: asyncio.Event) -> None: ...

    async def start_turn(self, turn: NormalizedInboundTurn) -> PreparedAgentRun: ...

    async def answer_interaction(
        self,
        turn: NormalizedResumeTurn,
    ) -> PreparedAgentRun: ...

    def stream(
        self,
        prepared: PreparedAgentRun,
        *,
        error_label: str,
    ) -> AsyncIterator[AGUIEvent]: ...


class InProcessAgentEngine:
    """Adapter for the in-process AgentService implementation."""

    def __init__(self, service: AgentService):
        self.service = service

    def set_cancel_token(self, cancel_token: asyncio.Event) -> None:
        self.service.cancel_token = cancel_token

    def set_liveness_token(self, liveness_token: asyncio.Event) -> None:
        self.service.liveness_token = liveness_token

    async def start_turn(self, turn: NormalizedInboundTurn) -> PreparedAgentRun:
        kwargs = {
            "user_content": turn.content,
            "idempotency_key": turn.idempotency_key,
        }
        if turn.context:
            kwargs["contexts"] = turn.context
        return await self.service.prepare_chat_round(**kwargs)

    async def answer_interaction(
        self,
        turn: NormalizedResumeTurn,
    ) -> PreparedAgentRun:
        return await self.service.prepare_resume_round(
            interrupt_id=turn.interrupt_id,
            answers=turn.answers,
        )

    def stream(
        self,
        prepared: PreparedAgentRun,
        *,
        error_label: str,
    ) -> AsyncIterator[AGUIEvent]:
        return self.service.run_prepared_round(prepared, error_label=error_label)
