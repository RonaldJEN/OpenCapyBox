"""Cron channel adapter for typed turn boundaries."""
from __future__ import annotations

from src.api.schemas.chat import TextContentBlock
from src.api.schemas.turn import NoReplyRoute, NormalizedInboundTurn


class CronChannelAdapter:
    """Translate Cron executions into normalized no-reply turns."""

    channel = "cron"

    def normalize_run(
        self,
        *,
        user_id: str,
        session_id: str,
        job_name: str,
        run_id: str,
        prompt: str,
        cron_expr: str | None = None,
        source: str = "scheduled",
    ) -> NormalizedInboundTurn:
        return NormalizedInboundTurn(
            channel=self.channel,
            user_id=user_id,
            peer_kind="cron",
            peer_id=job_name,
            content=[TextContentBlock(type="text", text=prompt)],
            reply_route=NoReplyRoute(),
            metadata={
                "session_id": session_id,
                "job_name": job_name,
                "run_id": run_id,
                "cron_expr": cron_expr,
                "source": source,
            },
            idempotency_key=f"cron:{run_id}",
        )

    def render_agent_prompt(self, turn: NormalizedInboundTurn) -> str:
        job_name = str(turn.metadata.get("job_name") or turn.peer_id)
        prompt = self._first_text(turn)
        return (
            f"你是一个定时任务执行器。请执行以下任务：\n\n"
            f"任务名：{job_name}\n"
            f"描述：{prompt}\n\n"
            f"请执行任务并给出简洁的结果摘要。"
        )

    @staticmethod
    def _first_text(turn: NormalizedInboundTurn) -> str:
        for block in turn.content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if text:
                    return text
        return ""


_GLOBAL_CRON_CHANNEL_ADAPTER = CronChannelAdapter()


def get_cron_channel_adapter() -> CronChannelAdapter:
    return _GLOBAL_CRON_CHANNEL_ADAPTER
