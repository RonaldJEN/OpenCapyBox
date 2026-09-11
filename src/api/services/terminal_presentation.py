"""Read-only presentation of the terminal writer's provenance; never classify by keywords."""
from typing import Any


def project_terminal_presentation(
    status: str, final_response: str | None, terminal_events: list[dict[str, Any]],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"final_response_origin": "unknown"}
    if not terminal_events:
        return metadata
    event = terminal_events[-1]
    if event.get("type") == "RUN_ERROR" and status == "failed":
        message = event.get("message")
        if isinstance(message, str) and message:
            metadata["error"] = {
                "source": "durable_run_error", "sequence": event.get("sequence"),
                "code": event.get("code"), "message": message,
            }
            # AgentService commits the error message as final_response with this terminal.
            # A mismatching legacy value has no proven writer and stays unknown.
            if final_response == message:
                metadata["final_response_origin"] = "run_error"
    elif event.get("type") == "RUN_FINISHED":
        result = event.get("result")
        result = result if isinstance(result, dict) else {}
        terminal_text = result.get("finalResponse") or result.get("final_response")
        if terminal_text and terminal_text == final_response:
            if status == "completed" and event.get("outcome", "success") == "success":
                metadata["final_response_origin"] = "assistant"
            elif result.get("reason") in {"user_cancelled", "max_steps_reached"}:
                metadata["final_response_origin"] = "system_notice"
    return metadata
