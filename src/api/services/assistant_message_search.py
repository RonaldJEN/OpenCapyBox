"""Search the same persisted main-assistant message projection used by history."""
import json
from sqlalchemy import and_, exists, or_

from src.api.models.agui_event import AGUIEventLog
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.subagent_run import SubagentRun
from src.api.services.assistant_message_projection import AssistantMessageProjection


def search_assistant_messages(db, *, session_filters, query: str, limit: int, excluded_sessions=()):
    rows = (
        db.query(Session, Round.id, AGUIEventLog.event_type, AGUIEventLog.payload, AGUIEventLog.sequence)
        .select_from(Session)
        .join(Round, Round.session_id == Session.id)
        .join(AGUIEventLog, AGUIEventLog.run_id == Round.id)
        .filter(*session_filters, ~Session.id.in_(tuple(excluded_sessions)),
                ~exists().where(SubagentRun.child_run_id == Round.id),
                or_(AGUIEventLog.event_type.in_(("STEP_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED", "RUN_ERROR")),
                    and_(AGUIEventLog.event_type == "CUSTOM", AGUIEventLog.payload.contains('"failover_reset"'))))
        .order_by(Session.updated_at.desc(), Session.id, Round.created_at, Round.id, AGUIEventLog.sequence)
        .yield_per(256)
    )
    needle = query.casefold()
    matches = []
    matched_sessions = set()
    current_round = None
    current_session = None
    projection = AssistantMessageProjection()

    def flush():
        if current_session is None or current_session.id in matched_sessions or not projection.complete_identity:
            return
        for message in projection.messages:
            if message["state"] != "superseded" and needle in message["content"].casefold():
                matches.append((current_session, current_round, message["message_id"], message["content"]))
                matched_sessions.add(current_session.id)
                return

    for session, round_id, kind, payload, sequence in rows:
        if round_id != current_round:
            flush()
            if len(matches) >= limit:
                return matches
            current_round, current_session, projection = round_id, session, AssistantMessageProjection()
        try:
            projection.apply({**json.loads(payload), "type": kind}, sequence)
        except (json.JSONDecodeError, TypeError):
            projection.complete_identity = False
    flush()
    return matches[:limit]
