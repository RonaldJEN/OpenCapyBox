"""Message identity/order projection of persisted AG-UI events (no second write store)."""
from typing import Any


class AssistantMessageProjection:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.step = 0
        self.complete_identity = True
        self.final_message_id: str | None = None
        self.final_message_ids: list[str] | None = None
        self.started_at_ts: int | None = None
        self.finished_at_ts: int | None = None
        self.excluded_ids: set[str] = set()

    def apply(self, event: dict[str, Any], sequence: int) -> None:
        kind = event.get("type")
        if kind == "RUN_STARTED":
            self.started_at_ts = event.get("timestamp")
        if kind == "STEP_STARTED":
            self.step += 1
        if kind == "TEXT_MESSAGE_START":
            message_id = event.get("messageId")
            if not message_id or event.get("role", "assistant") != "assistant":
                if message_id:
                    self.excluded_ids.add(message_id)
                return
            if message_id in self.by_id:
                # Current emitter never reuses IDs. Unknown old reset semantics stay legacy.
                self.complete_identity = False
                return
            message = {"message_id": message_id, "step_number": max(self.step, 1),
                       "first_sequence": sequence, "last_sequence": sequence,
                       "content": "", "state": "streaming", "content_committed": True}
            if event.get("phase") in {"commentary", "final_answer"}:
                message["phase"] = event["phase"]
            self.messages.append(message)
            self.by_id[message_id] = message
        elif kind in {"TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"}:
            if event.get("messageId") in self.excluded_ids:
                return
            message = self.by_id.get(event.get("messageId"))
            if message is None:
                if event.get("delta") or event.get("fullContent"):
                    self.complete_identity = False
                return
            if kind == "TEXT_MESSAGE_CONTENT":
                # Old persisted CONTENT is one aggregate per segment. New explicit false
                # is an incremental durable delta; explicit true is a checkpoint replacement.
                if event.get("isAggregate") is True:
                    message["content"] = event.get("delta") or ""
                else:
                    message["content"] += event.get("delta") or ""
            else:
                if not message["content"] and event.get("fullContent"):
                    message["content"] = event["fullContent"]
                message["state"] = "interrupted" if event.get("interrupted") else "complete"
                if event.get("phase") in {"commentary", "final_answer"}:
                    message["phase"] = event["phase"]
            message["last_sequence"] = sequence
        elif kind == "CUSTOM" and event.get("name") == "failover_reset":
            if self.messages and self.messages[-1]["step_number"] == max(self.step, 1):
                self.messages[-1]["state"] = "interrupted"
        elif kind in {"RUN_ERROR", "RUN_FINISHED"}:
            self.finished_at_ts = event.get("timestamp")
            for message in self.messages:
                if message["state"] == "streaming":
                    message["state"] = "interrupted"
            if kind == "RUN_FINISHED":
                self.final_message_id = event.get("finalMessageId")
                self.final_message_ids = event.get("finalMessageIds")

    def result(self, through_sequence: int) -> dict[str, Any]:
        return {
            "assistant_messages": self.messages,
            "final_message_id": self.final_message_id,
            "final_message_ids": self.final_message_ids,
            "started_at_ts": self.started_at_ts,
            "finished_at_ts": self.finished_at_ts,
            "transcript_coverage": {"kind": "complete" if self.complete_identity else "legacy",
                                    "durable_through_sequence": through_sequence},
        }
