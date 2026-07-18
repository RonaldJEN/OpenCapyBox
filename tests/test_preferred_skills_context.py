import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from src.agent.agent import Agent
from src.agent.schema import Message
from src.agent.schema.agui_events import Context, RunStartedEvent
from src.agent.schema.run_context import (
    AgentRunContext,
    LLMRequestContext,
    RequestedPreferredSkillsContext,
    ResolvedPreferredSkillsContext,
    ResolvedSkillRef,
    current_run_context,
    parse_requested_preferred_skills_contexts,
    render_preferred_skills_context_block,
    render_preferred_skills_system_policy,
    requested_preferred_skills_to_context,
)
from src.agent.tools.skill_loader import Skill
from src.api.schemas.chat import SendMessageRequest
from src.api.services.agent_service import (
    AgentService,
    PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY,
    PreparedAgentRun,
)
from src.api.services.web_chat_adapter import WebChatAdapter


def _run_context(*keys: str) -> AgentRunContext:
    return AgentRunContext(preferred_skills=ResolvedPreferredSkillsContext(
        skills=tuple(
            ResolvedSkillRef(key=key, load_name=key, display_name=key)
            for key in keys
        )
    ))


@pytest.mark.asyncio
async def test_direct_round_persists_resolved_preferred_skill_display_snapshot():
    class FakeAgent:
        def __init__(self):
            self.messages = []

        def has_pending_interrupt(self):
            return False

        def add_user_message(self, content, message_id=None, run_id=None):
            self.messages.append(
                Message(
                    role="user",
                    content=content,
                    id=message_id,
                    run_id=run_id,
                )
            )

    history_service = MagicMock()
    history_service.create_round.side_effect = (
        lambda **kwargs: SimpleNamespace(id=kwargs["round_id"])
    )
    service = object.__new__(AgentService)
    service.agent = FakeAgent()
    service.history_service = history_service
    service.session_id = "session-1"
    service.user_id = "user-1"
    service.skill_loader = MagicMock()
    service._resolve_run_context = AsyncMock(return_value=AgentRunContext(
        preferred_skills=ResolvedPreferredSkillsContext(skills=(
            ResolvedSkillRef(
                key="pdf",
                load_name="pdf",
                display_name="PDF 文档",
            ),
        )),
    ))
    service._normalize_content_blocks = MagicMock(
        return_value=[{"type": "text", "text": "hello"}]
    )
    service._validate_multimodal_blocks = MagicMock()
    service._build_agent_user_content = MagicMock(return_value="hello")
    service._blocks_to_plain_text = MagicMock(return_value="hello")
    service._extract_user_attachments = MagicMock(return_value=[])
    service._refresh_runtime_messages_from_history = MagicMock()
    service._save_conversation_message = MagicMock()

    await service.prepare_chat_round(
        user_content=[{"type": "text", "text": "hello"}],
        contexts=[requested_preferred_skills_to_context(
            RequestedPreferredSkillsContext(keys=("pdf", "missing"))
        )],
    )

    assert history_service.create_round.call_args.kwargs["preferred_skills"] == [
        {"key": "pdf", "display_name": "PDF 文档"},
    ]


@pytest.mark.asyncio
async def test_run_started_carries_authoritative_round_skill_snapshot():
    history_service = MagicMock()
    history_service.is_round_terminal.return_value = False
    history_service.save_agui_event = AsyncMock(return_value=None)
    history_service.complete_round = MagicMock()

    service = object.__new__(AgentService)
    service.history_service = history_service
    service.session_id = "session-1"
    service.cancel_token = None
    service._active_run_count = 0
    service.agent = MagicMock()

    async def fake_run_agui(**_kwargs):
        yield RunStartedEvent(threadId="session-1", runId="round-1")
        await asyncio.sleep(10)

    service.agent.run_agui = fake_run_agui
    stream = service._run_round_stream(
        run_id="round-1",
        user_message="hello",
        round_preferred_skills=[
            {"key": "pdf", "display_name": "PDF 文档"},
        ],
    )

    event = await stream.__anext__()
    assert event.preferred_skills == [
        {"key": "pdf", "display_name": "PDF 文档"},
    ]
    saved_event = history_service.save_agui_event.await_args.args[1]
    assert saved_event.model_dump(by_alias=True)["preferredSkills"] == [
        {"key": "pdf", "display_name": "PDF 文档"},
    ]
    await stream.aclose()


@pytest.mark.asyncio
async def test_run_prepared_round_maps_direct_snapshot_and_resume_to_empty():
    service = object.__new__(AgentService)
    captured = []

    async def fake_run_round_stream(**kwargs):
        captured.append(kwargs)
        if False:
            yield None

    service._run_round_stream = fake_run_round_stream
    direct = PreparedAgentRun(
        run_id="direct-1",
        user_message="hello",
        context=AgentRunContext(preferred_skills=ResolvedPreferredSkillsContext(
            skills=(ResolvedSkillRef(
                key="pdf",
                load_name="pdf",
                display_name="PDF 文档",
            ),),
        )),
    )
    resume = PreparedAgentRun(
        run_id="resume-1",
        user_message="answer",
        parent_run_id="direct-1",
        context=direct.context,
    )

    async for _event in service.run_prepared_round(direct):
        pass
    async for _event in service.run_prepared_round(resume):
        pass

    assert captured[0]["round_preferred_skills"] == [
        {"key": "pdf", "display_name": "PDF 文档"},
    ]
    assert captured[1]["round_preferred_skills"] == []


def test_web_adapter_builds_versioned_context_with_stable_dedupe():
    request = SendMessageRequest.model_validate({
        "content": [{"type": "text", "text": "hello"}],
        "preferred_skill_keys": [" pdf ", "data", "pdf", ""],
    })

    turn = WebChatAdapter().normalize_send(
        session_id="session-1",
        user_id="user-1",
        request=request,
    )

    assert len(turn.context) == 1
    assert turn.context[0].description == "bsbox.preferred_skills.v1"
    assert json.loads(turn.context[0].value) == {
        "mode": "preferred",
        "keys": ["pdf", "data"],
    }


def test_wire_parser_ignores_unknown_and_malformed_versions():
    contexts = [
        Context(description="bsbox.preferred_skills.v2", value="{}"),
        Context(description="bsbox.preferred_skills.v1", value="not-json"),
    ]
    assert parse_requested_preferred_skills_contexts(contexts) is None


def test_send_request_enforces_server_side_limits():
    with pytest.raises(ValidationError):
        SendMessageRequest.model_validate({
            "content": [{"type": "text", "text": "hello"}],
            "preferred_skill_keys": ["x" * 129],
        })
    with pytest.raises(ValidationError):
        SendMessageRequest.model_validate({
            "content": [{"type": "text", "text": "hello"}],
            "preferred_skill_keys": [f"skill-{index}" for index in range(51)],
        })


@pytest.mark.parametrize(
    "key",
    [
        "Self Reflection",
        "Self-Improving Agent (With Self-Reflection)",
        "中文技能（兼容）",
    ],
)
def test_legacy_human_readable_official_skill_keys_remain_valid(key):
    request = SendMessageRequest.model_validate({
        "content": [{"type": "text", "text": "hello"}],
        "preferred_skill_keys": [f"  {key}  "],
    })
    assert request.preferred_skill_keys == [key]


@pytest.mark.parametrize(
    "key",
    [
        "directory/skill",
        r"directory\skill",
        "query?skill",
        "fragment#skill",
        "encoded%2Fskill",
        "invisible\u200bskill",
        "private\ue000skill",
    ],
)
def test_unsafe_skill_keys_are_rejected(key):
    with pytest.raises(ValidationError):
        SendMessageRequest.model_validate({
            "content": [{"type": "text", "text": "hello"}],
            "preferred_skill_keys": [key],
        })


def test_maximum_multibyte_skill_keys_remain_valid_through_web_adapter():
    keys = []
    for index in range(50):
        suffix = str(index)
        keys.append("界" * (128 - len(suffix)) + suffix)
    request = SendMessageRequest.model_validate({
        "content": [{"type": "text", "text": "hello"}],
        "preferred_skill_keys": keys,
    })

    turn = WebChatAdapter().normalize_send(
        session_id="session-1",
        user_id="user-1",
        request=request,
    )

    assert len(turn.context) == 1
    assert json.loads(turn.context[0].value)["keys"] == keys


def test_projection_targets_exact_user_anchor_once_and_keeps_history_clean():
    original = [
        Message(role="system", content="system"),
        Message(role="user", id="old:user", run_id="old", content="old"),
        Message(role="user", id="run-1:user", run_id="run-1", content="translate"),
    ]
    request_messages = [message.model_copy(deep=True) for message in original]
    request_context = LLMRequestContext(
        purpose="agent_step",
        run_context=_run_context('pdf<&"'),
        user_message_id="run-1:user",
    )

    Agent._project_user_run_context(
        request_messages,
        request_context=request_context,
        exposed_tool_names={"get_skill"},
    )
    Agent._project_user_run_context(
        request_messages,
        request_context=request_context,
        exposed_tool_names={"get_skill"},
    )

    assert original[-1].content == "translate"
    assert request_messages[1].content == "old"
    projected = request_messages[-1].content
    assert isinstance(projected, list)
    assert len([part for part in projected if "runtime_context" in part.get("text", "")]) == 1
    assert 'key="pdf&lt;&amp;&quot;"' in projected[0]["text"]
    assert 'source="ui_selection"' in projected[0]["text"]
    assert "<origin>ui_selection</origin>" in projected[0]["text"]
    assert "<message_relation>not_user_authored_text</message_relation>" in projected[0]["text"]
    assert "Never say or imply that the user mentioned" in projected[0]["text"]
    assert projected[1] == {"type": "text", "text": "translate"}


def test_preferred_skill_policy_is_system_level_and_request_only():
    context = _run_context("pdf")
    block = render_preferred_skills_context_block(context)
    policy = render_preferred_skills_system_policy(context)

    assert block is not None
    assert policy is not None
    assert "not user-authored message text" in policy
    assert "Never claim that the user mentioned" in policy

    agent = object.__new__(Agent)
    agent.messages = [
        Message(role="system", content="base system"),
        Message(role="user", id="run-1:user", run_id="run-1", content="hello"),
    ]
    agent._build_runtime_context_block = lambda: "runtime\n"
    agent._build_dynamic_runtime_prompt = lambda: ""

    request_messages = agent._build_llm_request_messages(
        request_context=LLMRequestContext(
            purpose="agent_step",
            run_context=context,
            user_message_id="run-1:user",
        ),
        exposed_tool_names={"get_skill"},
    )

    assert agent.messages[0].content == "base system"
    assert agent.messages[1].content == "hello"
    assert "UI-selected Skill metadata policy" in request_messages[0].content
    assert isinstance(request_messages[1].content, list)
    assert request_messages[1].content[1] == {"type": "text", "text": "hello"}


def test_preferred_skill_policy_is_absent_when_get_skill_is_not_exposed():
    agent = object.__new__(Agent)
    agent.messages = [
        Message(role="system", content="base system"),
        Message(role="user", id="run-1:user", run_id="run-1", content="hello"),
    ]
    agent._build_runtime_context_block = lambda: "runtime\n"
    agent._build_dynamic_runtime_prompt = lambda: ""

    request_messages = agent._build_llm_request_messages(
        request_context=LLMRequestContext(
            purpose="agent_step",
            run_context=_run_context("pdf"),
            user_message_id="run-1:user",
        ),
        exposed_tool_names=set(),
    )

    assert "UI-selected Skill metadata policy" not in request_messages[0].content
    assert request_messages[1].content == "hello"


@pytest.mark.parametrize("purpose", ["title_generation", "conversation_summary", "memory_extraction"])
def test_projection_excludes_non_agent_purposes(purpose):
    messages = [Message(role="user", id="run-1:user", content="hello")]
    Agent._project_user_run_context(
        messages,
        request_context=LLMRequestContext(
            purpose=purpose,
            run_context=_run_context("pdf"),
            user_message_id="run-1:user",
        ),
        exposed_tool_names={"get_skill"},
    )
    assert messages[0].content == "hello"


def test_projection_requires_get_skill_exposure():
    messages = [Message(role="user", id="run-1:user", content="hello")]
    Agent._project_user_run_context(
        messages,
        request_context=LLMRequestContext(
            purpose="agent_step",
            run_context=_run_context("pdf"),
            user_message_id="run-1:user",
        ),
        exposed_tool_names={"read_file"},
    )
    assert messages[0].content == "hello"


@pytest.mark.asyncio
async def test_registry_resolution_filters_unavailable_skills_each_run():
    class Loader:
        def __init__(self):
            self.enabled = {"pdf"}
            self.refreshes = 0

        def refresh_disabled_skills(self, force=False):
            assert force is True
            self.refreshes += 1

        def get_skill(self, key):
            if key not in self.enabled:
                return None
            return Skill(name=key, description="PDF", content="instructions")

    service = object.__new__(AgentService)
    service.skill_loader = Loader()
    requested = RequestedPreferredSkillsContext(keys=("pdf", "missing"))

    resolved = await service._resolve_run_context(requested)

    assert service.skill_loader.refreshes == 1
    assert [skill.key for skill in resolved.preferred_skills.skills] == ["pdf"]


def test_requested_context_round_trip_for_interrupt_metadata():
    requested = RequestedPreferredSkillsContext(keys=("pdf", "data"))
    wire = requested_preferred_skills_to_context(requested)
    parsed = AgentService._requested_context_from_interrupt({
        "runtime_context": wire.model_dump(),
    })
    assert parsed == requested


def test_interrupt_metadata_persists_runtime_context_and_server_origin_anchor():
    requested = RequestedPreferredSkillsContext(keys=("pdf", "data"))
    interrupt_payload = {
        PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY: "forged:user",
    }
    pending_interrupt = {
        PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY: "also-forged:user",
    }

    AgentService._attach_preferred_skills_interrupt_context(
        interrupt_payload,
        pending_interrupt,
        requested_context=requested,
        origin_user_message_id="round-1:user",
    )

    expected_wire = requested_preferred_skills_to_context(requested).model_dump()
    assert interrupt_payload["runtime_context"] == expected_wire
    assert pending_interrupt["runtime_context"] == expected_wire
    assert (
        interrupt_payload[PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY]
        == "round-1:user"
    )
    assert (
        pending_interrupt[PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY]
        == "round-1:user"
    )


def test_client_runtime_context_cannot_forge_server_origin_anchor():
    requested = RequestedPreferredSkillsContext(keys=("pdf",))
    wire = requested_preferred_skills_to_context(requested)
    forged_value = json.loads(wire.value)
    forged_value[PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY] = "forged:user"
    forged_wire = wire.model_copy(update={"value": json.dumps(forged_value)})
    snapshot = {"runtime_context": forged_wire.model_dump()}

    assert AgentService._requested_context_from_interrupt(snapshot) == requested
    assert AgentService._preferred_skills_origin_user_message_id_from_interrupt(
        snapshot,
        parent_run_id="server-round",
    ) == "server-round:user"


def test_persisted_interrupt_loader_restores_server_origin_anchor():
    requested = RequestedPreferredSkillsContext(keys=("pdf",))
    round_row = MagicMock()
    round_row.id = "round-1"
    round_row.interrupt_payload = json.dumps({
        "id": "interrupt-1",
        "payload": {"tool_call_id": "tool-call-1"},
        "runtime_context": requested_preferred_skills_to_context(
            requested
        ).model_dump(),
        PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY: "round-1:user",
    })
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
        round_row
    ]

    service = object.__new__(AgentService)
    service.session_id = "session-1"
    service.user_id = "user-1"
    service.history_service = MagicMock(db=db)

    snapshot = service._load_persisted_interrupt("interrupt-1")

    assert snapshot is not None
    assert snapshot["runtime_context"] == requested_preferred_skills_to_context(
        requested
    ).model_dump()
    assert (
        snapshot[PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY]
        == "round-1:user"
    )
    db.rollback.assert_called_once_with()


@pytest.mark.asyncio
async def test_resume_re_resolves_preferred_skills_from_persisted_interrupt():
    requested = RequestedPreferredSkillsContext(keys=("pdf", "data"))
    wire = requested_preferred_skills_to_context(requested)
    persisted_interrupt = {
        "interrupt_id": "approval-1",
        "round_id": "round-1",
        "kind": "tool_approval",
        "runtime_context": wire.model_dump(),
    }
    resolved = _run_context("pdf")
    prepared = MagicMock()

    service = object.__new__(AgentService)
    service.agent = MagicMock()
    service._resume_lock = asyncio.Lock()
    service._pending_interrupt_round_ids = {}
    service._load_persisted_interrupt = MagicMock(return_value=persisted_interrupt)
    service._get_agent_pending_interrupt_snapshot = MagicMock(return_value=None)
    service._resolve_run_context = AsyncMock(return_value=resolved)
    service._prepare_tool_approval_resume_locked = MagicMock(return_value=prepared)

    result = await service.prepare_resume_round(
        interrupt_id="approval-1",
        answers={"approve": "yes"},
    )

    assert result is prepared
    service._resolve_run_context.assert_awaited_once_with(requested)
    service._prepare_tool_approval_resume_locked.assert_called_once_with(
        interrupt_id="approval-1",
        answers={"approve": "yes"},
        parent_run_id="round-1",
        preferred_skills_origin_user_message_id="round-1:user",
        requested_context=requested,
        run_context=resolved,
    )


@pytest.mark.asyncio
async def test_consecutive_resumes_keep_original_preferred_skill_anchor():
    requested = RequestedPreferredSkillsContext(keys=("pdf",))
    wire = requested_preferred_skills_to_context(requested).model_dump()
    persisted_interrupts = [
        {
            "interrupt_id": "approval-1",
            "round_id": "round-1",
            "kind": "tool_approval",
            "runtime_context": wire,
            PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY: "round-1:user",
        },
        {
            "interrupt_id": "approval-2",
            "round_id": "round-2",
            "kind": "tool_approval",
            "runtime_context": wire,
            PREFERRED_SKILLS_ORIGIN_USER_MESSAGE_ID_KEY: "round-1:user",
        },
    ]
    resolved = _run_context("pdf")

    def build_prepared(**kwargs):
        return PreparedAgentRun(
            run_id=f"{kwargs['parent_run_id']}-child",
            user_message="Tool approval: allow_once",
            user_message_id=kwargs["preferred_skills_origin_user_message_id"],
            context=kwargs["run_context"],
            requested_context=kwargs["requested_context"],
            parent_run_id=kwargs["parent_run_id"],
        )

    service = object.__new__(AgentService)
    service.agent = MagicMock()
    service._resume_lock = asyncio.Lock()
    service._pending_interrupt_round_ids = {}
    service._load_persisted_interrupt = MagicMock(side_effect=persisted_interrupts)
    service._get_agent_pending_interrupt_snapshot = MagicMock(return_value=None)
    service._resolve_run_context = AsyncMock(return_value=resolved)
    service._prepare_tool_approval_resume_locked = MagicMock(side_effect=build_prepared)

    second_round = await service.prepare_resume_round(
        interrupt_id="approval-1",
        answers={"approval": "allow_once"},
    )
    third_round = await service.prepare_resume_round(
        interrupt_id="approval-2",
        answers={"approval": "allow_once"},
    )

    assert second_round.user_message_id == "round-1:user"
    assert third_round.user_message_id == "round-1:user"
    assert service._resolve_run_context.await_args_list == [
        ((requested,),),
        ((requested,),),
    ]

    request_messages = [
        Message(role="user", id="round-1:user", content="original request"),
        Message(role="user", id="round-2:user", content="resume answer"),
    ]
    Agent._project_user_run_context(
        request_messages,
        request_context=LLMRequestContext(
            purpose="agent_step",
            run_context=third_round.context,
            user_message_id=third_round.user_message_id,
        ),
        exposed_tool_names={"get_skill"},
    )
    assert isinstance(request_messages[0].content, list)
    assert request_messages[1].content == "resume answer"


@pytest.mark.asyncio
async def test_contextvar_isolates_concurrent_runs():
    async def observe(key: str):
        context = _run_context(key)
        token = current_run_context.set(context)
        try:
            await asyncio.sleep(0)
            return current_run_context.get()
        finally:
            current_run_context.reset(token)

    pdf_context, data_context = await asyncio.gather(observe("pdf"), observe("data"))

    assert pdf_context.preferred_skills.skills[0].key == "pdf"
    assert data_context.preferred_skills.skills[0].key == "data"
    assert current_run_context.get() is None
