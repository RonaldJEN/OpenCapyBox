from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.agent.agent import Agent
from src.agent.context_compaction import (
    DEFAULT_TOOL_OUTPUT_TRUNCATION_BYTES,
    SUMMARY_PREFIX,
    SUMMARIZATION_PROMPT,
    TOOL_OUTPUT_SERIALIZATION_HEADROOM,
    approx_token_count,
    build_compacted_history,
    normalize_history,
    select_recent_user_messages,
    truncate_tool_output,
)
from src.agent.schema import FunctionCall, LLMResponse, Message, TokenUsage, ToolCall
from src.agent.schema.run_context import (
    AgentRunContext,
    LLMRequestContext,
    ResolvedMcpConnectionRef,
    ResolvedSkillRef,
    ResolvedTurnPreferencesContext,
)
from src.api.services.context_checkpoint_service import canonical_messages_json


class RecordingLLM:
    def __init__(self, *, failures: int = 0):
        self.requests: list[list[Message]] = []
        self.failures = failures

    async def generate(self, *, messages, tools=None):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if self.failures:
            self.failures -= 1
            raise RuntimeError("context_length_exceeded: input is too long")
        return LLMResponse(
            content="handoff state",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        )


class BlockingCompactionLLM:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def generate(self, *, messages, tools=None):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def generate_stream(self, **_kwargs):
        raise AssertionError("ordinary provider request must not start after cancellation")


def make_agent(llm, *, context_window=100):
    return Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[],
        workspace_dir=".",
        context_window=context_window,
        max_output_tokens=10,
        token_limit=1,
    )


def test_agent_default_compaction_limit_is_eighty_percent_of_input_budget_and_custom_only_lowers_it(tmp_path):
    default_agent = Agent(
        llm_client=RecordingLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path / "default"),
        context_window=10_000,
        max_output_tokens=1_000,
    )
    capped_agent = Agent(
        llm_client=RecordingLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path / "capped"),
        context_window=10_000,
        max_output_tokens=1_000,
        auto_compact_token_limit=9_900,
    )
    lowered_agent = Agent(
        llm_client=RecordingLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path / "lowered"),
        context_window=10_000,
        max_output_tokens=1_000,
        auto_compact_token_limit=7_000,
    )
    assert default_agent.token_limit == capped_agent.token_limit == 7_200
    assert lowered_agent.token_limit == 7_000


def test_agent_rejects_zero_tool_output_truncation_bytes(tmp_path):
    with pytest.raises(ValueError, match="tool_output_truncation_bytes must be > 0"):
        Agent(
            llm_client=RecordingLLM(),
            system_prompt="system",
            tools=[],
            workspace_dir=str(tmp_path),
            tool_output_truncation_bytes=0,
        )


def test_midturn_token_status_does_not_discard_last_real_provider_usage():
    agent = make_agent(RecordingLLM())
    agent._active_context_tokens = 91
    assert agent._codex_active_tokens(
        [Message(role="user", content="tiny")],
        prefer_usage=False,
    ) == 91


def test_replacement_keeps_newest_users_and_summary_is_synthetic_user():
    old = Message(role="user", content="old objective", id="old")
    newest = Message(role="user", content="n" * 80_000, id="new")
    selected = select_recent_user_messages([old, newest], max_tokens=10)

    assert [message.id for message in selected] == ["new"]
    replacement = build_compacted_history([old, newest], "summary")
    assert replacement[-1].role == "user"
    assert replacement[-1].is_synthetic is True
    assert replacement[-1].content == f"{SUMMARY_PREFIX}\nsummary"
    assert replacement[0].id != "old"


def test_user_budget_uses_utf8_bytes_and_middle_truncates_boundary():
    message = Message(role="user", content="开" * 20)
    selected = select_recent_user_messages([message], max_tokens=5)
    assert len(selected) == 1
    assert "tokens truncated" in selected[0].content
    assert approx_token_count("开") == 1


def test_compacted_text_and_image_user_keeps_only_text_and_checkpoint_has_no_data_url():
    data_url = "data:image/png;base64," + ("A" * 4_000)
    message = Message(
        role="user",
        id="multimodal-user",
        content=[
            {"type": "text", "text": "inspect this image"},
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "input_audio", "input_audio": {"data": "B" * 4_000}},
        ],
    )

    replacement = build_compacted_history([message], "visual findings are summarized")

    assert replacement[0].content == "inspect this image"
    checkpoint_json = canonical_messages_json(replacement)
    assert "data:image" not in checkpoint_json
    assert "base64" not in checkpoint_json
    assert "input_audio" not in checkpoint_json


def test_compacted_image_only_user_becomes_empty_text_without_media_payload():
    data_url = "data:image/png;base64," + ("A" * 4_000)
    message = Message(
        role="user",
        id="image-only-user",
        content=[{"type": "image_url", "image_url": {"url": data_url}}],
    )

    selected = select_recent_user_messages([message])
    replacement = build_compacted_history([message], "image-only task summary")

    assert len(selected) == 1
    assert selected[0].content == ""
    assert replacement[0].content == ""
    checkpoint_json = canonical_messages_json(replacement)
    assert "data:image" not in checkpoint_json
    assert "base64" not in checkpoint_json


def test_normalization_repairs_missing_outputs_and_drops_orphans():
    call = ToolCall(
        id="call-1",
        type="function",
        function=FunctionCall(name="lookup", arguments={}),
    )
    normalized = normalize_history([
        Message(role="tool", content="orphan", tool_call_id="orphan"),
        Message(role="assistant", content="", tool_calls=[call], run_id="run-1"),
        Message(role="user", content="continue"),
    ])

    assert [message.role for message in normalized] == ["assistant", "tool", "user"]
    assert normalized[1].content == "aborted"
    assert normalized[1].tool_call_id == "call-1"
    assert normalized[1].is_synthetic is True


def test_normalization_strips_unsupported_media():
    normalized = normalize_history([
        Message(role="user", content=[
            {"type": "text", "text": "keep"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            {"type": "input_audio", "input_audio": {"data": "x"}},
        ])
    ], supports_image=False)
    assert normalized[0].content == [{"type": "text", "text": "keep"}]


def test_tool_output_uses_codex_record_time_byte_policy():
    value = "头" * 5000 + "tail"
    truncated = truncate_tool_output(value, 10_000)
    assert "chars truncated" in truncated
    assert truncated.startswith("头")
    assert truncated.endswith("tail")


def test_default_tool_output_budget_is_50_kib():
    budget = int(
        DEFAULT_TOOL_OUTPUT_TRUNCATION_BYTES
        * TOOL_OUTPUT_SERIALIZATION_HEADROOM
    )

    assert DEFAULT_TOOL_OUTPUT_TRUNCATION_BYTES == 42_667
    assert TOOL_OUTPUT_SERIALIZATION_HEADROOM == 1.2
    assert budget == 51_200
    assert truncate_tool_output("x" * budget) == "x" * budget
    assert "chars truncated" in truncate_tool_output("x" * (budget + 1))


@pytest.mark.asyncio
async def test_preturn_compaction_excludes_incoming_user_and_persists_before_publish():
    llm = RecordingLLM()
    agent = make_agent(llm)
    old = Message(role="user", content="old context", run_id="old-run")
    incoming_content = [
        {"type": "text", "text": "latest input"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]
    incoming = Message(role="user", content=incoming_content, run_id="new-run", id="new-run:user")
    agent.messages = [Message(role="system", content="system"), old, incoming]
    agent._active_context_tokens = 90
    persisted: list[dict] = []

    async def persist(payload):
        assert agent.messages[-1].content == incoming_content
        persisted.append(payload)
        return "checkpoint-1"

    agent.set_compaction_persist_hook(persist)
    request = await agent._codex_prepare_provider_request_messages(
        request_context=None,
        exposed_tool_names=set(),
        tools=[],
        incoming_run_id="new-run",
        phase="pre_turn",
    )

    compact_contents = [str(message.content) for message in llm.requests[0]]
    assert "latest input" not in "\n".join(compact_contents)
    assert compact_contents[-1] == SUMMARIZATION_PROMPT
    assert request[-1].content == incoming_content
    assert persisted[0]["source_run_ids"] == ["old-run"]


@pytest.mark.asyncio
async def test_compaction_context_overflow_drops_exactly_one_oldest_item_per_retry():
    llm = RecordingLLM(failures=2)
    agent = make_agent(llm)
    source = [
        Message(role="system", content="system"),
        Message(role="user", content="one"),
        Message(role="assistant", content="two"),
        Message(role="user", content="three"),
    ]

    await agent._codex_compact_history(
        source_messages=source,
        phase="mid_turn",
        request_context=None,
        exposed_tool_names=set(),
    )

    assert len(llm.requests) == 3
    assert [len(request) for request in llm.requests] == [5, 4, 3]
    assert all(request[-1].content == SUMMARIZATION_PROMPT for request in llm.requests)


@pytest.mark.asyncio
async def test_compaction_never_persists_ui_selected_turn_preferences():
    llm = RecordingLLM()
    agent = make_agent(llm)
    source = [
        Message(role="system", content="system"),
        Message(role="user", id="run-1:user", run_id="run-1", content="analyze"),
    ]
    request_context = LLMRequestContext(
        purpose="agent_step",
        user_message_id="run-1:user",
        run_context=AgentRunContext(
            preferences=ResolvedTurnPreferencesContext(
                skills=(ResolvedSkillRef(
                    key="pdf",
                    load_name="pdf",
                    display_name="PDF",
                ),),
                mcp_connections=(ResolvedMcpConnectionRef(
                    server_id="server-a",
                    display_name="东方财富数据",
                ),),
            ),
        ),
    )

    await agent._codex_compact_history(
        source_messages=source,
        phase="mid_turn",
        request_context=request_context,
        exposed_tool_names={"get_skill", "mcp_tool_search"},
    )

    compact_request = "\n".join(str(message.content) for message in llm.requests[0])
    assert "<ui_context" not in compact_request
    assert "trusted UI metadata" not in compact_request
    assert "东方财富数据" not in compact_request


@pytest.mark.asyncio
async def test_two_consecutive_compactions_replace_previous_summary_and_empty_uses_placeholder():
    llm = RecordingLLM()
    agent = make_agent(llm)
    agent.messages = [
        Message(role="system", content="system"),
        Message(role="user", content="first", run_id="r1"),
    ]
    await agent._codex_compact_history(
        source_messages=agent.messages,
        phase="mid_turn",
        request_context=None,
        exposed_tool_names=set(),
    )
    agent.messages.append(Message(role="user", content="second", run_id="r2"))
    llm.requests.clear()
    llm.generate = lambda **_kwargs: _empty_summary_response()
    await agent._codex_compact_history(
        source_messages=agent.messages,
        phase="mid_turn",
        request_context=None,
        exposed_tool_names=set(),
    )

    summaries = [message for message in agent.messages if message.is_synthetic]
    assert len(summaries) == 1
    assert summaries[0].role == "user"
    assert summaries[0].content.endswith("(no summary available)")
    assert [message.content for message in agent.messages if not message.is_synthetic][-2:] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_compaction_cancel_aborts_provider_and_does_not_publish_checkpoint():
    llm = BlockingCompactionLLM()
    agent = make_agent(llm)
    original = [
        Message(role="system", content="system"),
        Message(role="user", content="old context", run_id="old-run"),
    ]
    agent.messages = [message.model_copy(deep=True) for message in original]
    persisted: list[dict] = []

    async def persist(payload):
        persisted.append(payload)
        return "checkpoint-should-not-exist"

    agent.set_compaction_persist_hook(persist)
    cancel_token = asyncio.Event()
    task = asyncio.create_task(agent._codex_compact_history(
        source_messages=agent.messages,
        phase="mid_turn",
        request_context=None,
        exposed_tool_names=set(),
        cancel_token=cancel_token,
    ))
    await asyncio.wait_for(llm.started.wait(), timeout=1)
    cancel_token.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert llm.cancelled.is_set()
    assert persisted == []
    assert agent.messages == original


@pytest.mark.asyncio
async def test_run_cancelled_during_compaction_finishes_as_user_cancelled():
    llm = BlockingCompactionLLM()
    agent = make_agent(llm)
    agent.messages = [
        Message(role="system", content="system"),
        Message(role="user", content="old context", run_id="old-run"),
        Message(role="user", content="new request", run_id="new-run"),
    ]
    agent._active_context_tokens = 90
    cancel_token = asyncio.Event()
    events = []

    async def consume():
        async for event in agent.run_agui(
            "thread-1",
            "new-run",
            cancel_token=cancel_token,
        ):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(llm.started.wait(), timeout=1)
    cancel_token.set()
    await asyncio.wait_for(task, timeout=1)

    assert llm.cancelled.is_set()
    finished = [event for event in events if event.type == "RUN_FINISHED"]
    assert len(finished) == 1
    assert finished[0].outcome == "interrupt"
    assert finished[0].result == {"reason": "user_cancelled"}


async def _empty_summary_response():
    return LLMResponse(content="", finish_reason="stop")
