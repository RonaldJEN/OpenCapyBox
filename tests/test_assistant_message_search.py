import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.models.database import Base
from src.api.models.session import Session
from src.api.models.round import Round
from src.api.models.conversation_message import ConversationMessage
from src.api.models.subagent_run import SubagentRun
from src.api.services.agui_event_bus import AguiEventBus
from src.api.services.run_completion_service import RunCompletionService
from src.api.routes.sessions import list_sessions


@pytest.mark.asyncio
async def test_message_search_crosses_deltas_and_excludes_errors_other_users_and_subagents():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with factory() as db:
            db.add_all([Session(id='s-main', user_id='alice'), Session(id='s-other', user_id='bob')])
            db.add_all([Round(id='r-main', session_id='s-main', user_message='request', status='running'),
                        Round(id='r-child', session_id='s-main', user_message='子任务内部委派词', status='running'),
                        Round(id='r-legacy-child', session_id='s-main', user_message='legacy child', status='completed', final_response='旧子任务终稿'),
                        Round(id='r-legacy-main', session_id='s-main', parent_run_id='r-main', user_message='主聊天分支问题', status='completed', final_response='主聊天分支终稿'),
                        Round(id='r-other', session_id='s-other', user_message='private', status='running')])
            db.add(SubagentRun(user_id='alice', session_id='s-main', root_run_id='r-main', parent_run_id='r-main', child_run_id='r-child', prompt='internal'))
            db.add(SubagentRun(user_id='alice', session_id='s-main', root_run_id='r-main', parent_run_id='r-main', child_run_id='r-legacy-child', prompt='legacy internal'))
            db.add_all([
                ConversationMessage(session_id='s-main', round_id='r-legacy-child', sequence=1, role='assistant', content='旧子任务正文'),
                ConversationMessage(session_id='s-main', round_id='r-legacy-main', sequence=2, role='assistant', content='主聊天分支正文'),
            ])
            db.commit()
        bus = AguiEventBus(factory)
        for rid, parts in [('r-main', ['体检', '通知内容']), ('r-child', ['子任务秘密']), ('r-other', ['其他账号秘密'])]:
            await bus.publish(rid, {'type': 'STEP_STARTED', 'stepName': 'step_1'})
            await bus.publish(rid, {'type': 'TEXT_MESSAGE_START', 'messageId': f'm-{rid}', 'role': 'assistant'})
            for part in parts:
                await bus.publish(rid, {'type': 'TEXT_MESSAGE_CONTENT', 'messageId': f'm-{rid}', 'delta': part})
            await bus.publish(rid, {'type': 'TEXT_MESSAGE_END', 'messageId': f'm-{rid}'})
        RunCompletionService(factory).complete_sync(run_id='r-main', status='failed', final_response='系统错误不可搜索')
        with factory() as db:
            result = await list_sessions(q='体检通知', user_id='alice', db=db)
            assert len(result.sessions) == 1
            assert result.sessions[0].match_round_id == 'r-main'
            assert result.sessions[0].match_message_id == 'm-r-main'
            for query in ['子任务秘密', '子任务内部委派词', '旧子任务正文', '旧子任务终稿', '其他账号秘密', '系统错误不可搜索']:
                assert not (await list_sessions(q=query, user_id='alice', db=db)).sessions
            # A parent_run_id alone does not make a Round an internal subagent.
            for query in ['主聊天分支问题', '主聊天分支正文', '主聊天分支终稿']:
                matches = (await list_sessions(q=query, user_id='alice', db=db)).sessions
                assert len(matches) == 1
                assert matches[0].match_round_id == 'r-legacy-main'
    finally:
        engine.dispose()
