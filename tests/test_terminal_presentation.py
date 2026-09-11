from src.api.services.terminal_presentation import project_terminal_presentation
import json
import pytest


def test_error_provenance_uses_terminal_writer_and_preserves_unknown_legacy():
    event = {"type": "RUN_ERROR", "message": "provider unavailable", "sequence": 9, "code": "PROVIDER_ERROR"}
    known = project_terminal_presentation("failed", event["message"], [event])
    assert known["final_response_origin"] == "run_error"
    assert known["error"]["sequence"] == 9
    assert project_terminal_presentation("failed", "older independent text", [event])["final_response_origin"] == "unknown"
    assert project_terminal_presentation("failed", "Failed", []) == {"final_response_origin": "unknown"}


def test_final_provenance_uses_outcome_not_error_words():
    event = {"type": "RUN_FINISHED", "outcome": "success", "result": {"final_response": "Failed is an English word"}}
    assert project_terminal_presentation("completed", event["result"]["final_response"], [event])["final_response_origin"] == "assistant"
    event = {"type": "RUN_FINISHED", "outcome": "interrupt", "result": {"reason": "max_steps_reached", "finalResponse": "达到步数限制"}}
    assert project_terminal_presentation("max_steps_reached", "达到步数限制", [event])["final_response_origin"] == "system_notice"


@pytest.mark.parametrize('status,expected', [('failed', 'older independent text'), ('running', 'provider unavailable')])
def test_history_preserves_terminal_legacy_text_but_updates_a_stale_running_header(status, expected):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as DBSession
    from src.api.models.database import Base
    from src.api.models.session import Session
    from src.api.models.round import Round
    from src.api.models.agui_event import AGUIEventLog
    from src.api.services.history_service import HistoryService
    engine = create_engine('sqlite://')
    try:
        Base.metadata.create_all(engine)
        with DBSession(engine) as db:
            db.add(Session(id='s-terminal', user_id='test'))
            db.add(Round(id='r-terminal', session_id='s-terminal', user_message='question', status=status, final_response='older independent text'))
            db.add(AGUIEventLog(run_id='r-terminal', sequence=1, event_type='RUN_ERROR', payload=json.dumps({'type':'RUN_ERROR','message':'provider unavailable'})))
            db.commit()
            result = HistoryService(db).get_session_rounds('s-terminal')[0]
            assert result['status'] == 'failed'
            assert result['final_response'] == expected
            assert result['terminal_presentation']['error']['message'] == 'provider unavailable'
            assert result['terminal_presentation']['final_response_origin'] == ('unknown' if status == 'failed' else 'run_error')
    finally:
        engine.dispose()
