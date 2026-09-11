"""Native Responses messages retain their semantics through Agent and cold replay."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.schema import Message
from src.api.services.agent_service import AgentService
from src.api.services.assistant_message_projection import AssistantMessageProjection
from tests.helpers import make_agent
from tests.test_native_message_phase import (
    EventStream,
    client_with_streams,
    delta_event,
    item_event,
    message_item,
    terminal_event,
)


async def run_events(agent):
    return [
        json.loads(event.model_dump_json(by_alias=True, exclude_none=True))
        async for event in agent.run_agui("session-native", "round-native")
    ]


def project_and_restore(events):
    projection = AssistantMessageProjection()
    rows = []
    for sequence, event in enumerate(events, start=1):
        projection.apply(event, sequence)
        rows.append(SimpleNamespace(payload=json.dumps(event), sequence=sequence))
    return projection.result(len(events)), AgentService._events_to_messages(
        rows, round_id="round-native",
    )


def text_and_phase(messages):
    return [(message.content, message.phase) for message in messages]


def replay_text_and_phase(client, messages):
    _, items = client._convert_messages(messages)
    return [(item["content"], item.get("phase")) for item in items if item.get("role") == "assistant"]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery, parts, final_text, final_indices", [
    pytest.param(
        "stream",
        [("先核对来源。", "commentary"), ("  这是正式答复。\n", "final_answer")],
        "  这是正式答复。\n", [1], id="single-final",
    ),
    pytest.param(
        "stream",
        [("先核对来源。", "commentary"), ("第一部分。", "final_answer"),
         ("", "final_answer"), ("第二部分。", "final_answer")],
        "第一部分。\n\n第二部分。", [1, 3], id="multiple-finals",
    ),
    pytest.param(
        "nonstream",
        [("先核对来源。", "commentary"), ("第一部分。", "final_answer"),
         ("", "final_answer"), ("第二部分。", "final_answer")],
        "第一部分。\n\n第二部分。", [1, 3], id="nonstream-multiple-finals",
    ),
    pytest.param(
        "stream",
        [("先核对来源。", "commentary"), ("目前已查到资料。", "commentary")],
        "先核对来源。目前已查到资料。", [0, 1], id="commentary-only",
    ),
    pytest.param(
        "stream",
        [("未标阶段的第一段。", None), ("未标阶段的第二段。", None)],
        "未标阶段的第一段。未标阶段的第二段。", [0, 1], id="unknown-phase",
    ),
])
async def test_agent_final_aliases_preserve_native_history_checkpoint_and_replay(
    tmp_path, delivery, parts, final_text, final_indices,
):
    items = [
        message_item(f"msg-{index}-" + "p" * 60, content, phase)
        for index, (content, phase) in enumerate(parts)
    ]
    thinking = "独立的推理摘要。"
    reasoning_payload = {
        "type": "reasoning", "id": "rs-native", "encrypted_content": "opaque",
        "summary": [{"type": "summary_text", "text": thinking}],
    }
    reasoning = SimpleNamespace(
        type="reasoning", id="rs-native",
        summary=[SimpleNamespace(text=thinking)],
        model_dump=lambda **_: reasoning_payload,
    )
    provider_events = [SimpleNamespace(type="response.reasoning_summary_text.delta", delta=thinking)]
    streamed_deltas = []
    for index, item in enumerate(items):
        content = parts[index][0]
        provider_events.append(item_event("added", message_item(item.id, phase=item.phase), index=index))
        for delta in (content[:len(content) // 2], content[len(content) // 2:]):
            if delta:
                provider_events.append(delta_event(item.id, delta))
                streamed_deltas.append((index, delta))
        provider_events.append(item_event("done", item, index=index))
    terminal = terminal_event(reasoning, *items)
    stream = EventStream([*provider_events, terminal])
    client, create = client_with_streams(stream)
    if delivery == "nonstream":
        # This client returns ordered response parts without invoking callbacks.
        client.supports_message_callbacks = False
        client.generate_stream = AsyncMock(return_value=client._parse_response(terminal.response))
    agent = make_agent(tmp_path, llm=client, tools=[], max_steps=5)
    agent.add_user_message("核对资料并答复")

    events = await run_events(agent)

    assert [event["type"] for event in events].count("STEP_STARTED") == 1
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["outcome"] == "success"
    assert events[-1]["result"]["final_response"] == final_text
    starts = [event for event in events if event["type"] == "TEXT_MESSAGE_START"]
    local_ids = [event["messageId"] for event in starts]
    assert len(local_ids) == len(set(local_ids)) == len(parts)
    assert all(len(message_id) <= 36 for message_id in local_ids)
    assert set(local_ids).isdisjoint({item.id for item in items})
    assert [event.get("phase") for event in starts] == [phase for _, phase in parts]
    contents = [event for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]
    expected_deltas = streamed_deltas if delivery == "stream" else [
        (index, content) for index, (content, _) in enumerate(parts) if content
    ]
    assert [(event["messageId"], event["delta"]) for event in contents] == [
        (local_ids[index], delta) for index, delta in expected_deltas
    ]
    ends = [event for event in events if event["type"] == "TEXT_MESSAGE_END"]
    assert [event["messageId"] for event in ends] == local_ids
    assert all(not event.get("interrupted") for event in ends)
    final_ids = [local_ids[index] for index in final_indices]
    if len(final_ids) == 1:
        assert events[-1]["finalMessageId"] == final_ids[0]
        assert "finalMessageIds" not in events[-1]
    else:
        assert events[-1]["finalMessageIds"] == final_ids
        assert "finalMessageId" not in events[-1]
    if delivery == "stream":
        thinking_events = [event for event in events if event["type"].startswith("THINKING_")]
        assert [event["type"] for event in thinking_events] == [
            "THINKING_TEXT_MESSAGE_START", "THINKING_TEXT_MESSAGE_CONTENT", "THINKING_TEXT_MESSAGE_END",
        ]
        assert thinking_events[1]["delta"] == thinking
        assert len({event["messageId"] for event in thinking_events}) == 1
        assert thinking_events[0]["messageId"] not in local_ids

    expected_replay = [(content, phase) for content, phase in parts if content]
    projection, restored = project_and_restore(events)
    assert projection["transcript_coverage"]["kind"] == "complete"
    assert [
        (message["content"], message.get("phase"), message["state"], message["step_number"])
        for message in projection["assistant_messages"]
    ] == [(content, phase, "complete", 1) for content, phase in parts]
    assert [message["message_id"] for message in projection["assistant_messages"]] == local_ids
    assert projection["final_message_id"] == (final_ids[0] if len(final_ids) == 1 else None)
    assert projection["final_message_ids"] == (final_ids if len(final_ids) > 1 else None)
    assert len(restored) == 1
    aggregate_content = "".join(content for content, _ in parts)
    assert restored[0].content == aggregate_content
    assert text_and_phase(restored[0].assistant_text_messages) == parts
    assert replay_text_and_phase(client, restored) == expected_replay

    live_assistant = [message for message in agent.messages if message.role == "assistant"]
    assert len(live_assistant) == 1
    assert live_assistant[0].content == aggregate_content
    assert live_assistant[0].thinking == thinking
    assert live_assistant[0].provider_items == [reasoning_payload]
    checkpoint = json.loads(json.dumps(live_assistant[0].model_dump(mode="json", exclude_none=True)))
    resumed_message = Message.model_validate(checkpoint)
    assert resumed_message.assistant_text_messages == live_assistant[0].assistant_text_messages
    assert [part.provider_message_id for part in resumed_message.assistant_text_messages] == [item.id for item in items]
    assert replay_text_and_phase(client, [resumed_message]) == expected_replay
    if delivery == "stream":
        assert create.await_count == 1
        assert stream.closed
    else:
        client.generate_stream.assert_awaited_once()
        assert "on_message" not in client.generate_stream.await_args.kwargs
        create.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_interrupted_body_remains_visible_but_is_excluded_from_model_replay(tmp_path):
    provider_id = "msg-interrupted-" + "x" * 60
    stream = EventStream([
        item_event("added", message_item(provider_id, phase="final_answer")),
        delta_event(provider_id, "已输出的半段"),
        ConnectionError("provider disconnected before message END"),
    ])
    client, create = client_with_streams(stream)
    agent = make_agent(tmp_path, llm=client, tools=[], max_steps=5)
    agent.add_user_message("开始答复")

    events = await run_events(agent)

    assert events[-1]["type"] == "RUN_ERROR"
    assert "provider disconnected" in events[-1]["message"]
    starts = [event for event in events if event["type"] == "TEXT_MESSAGE_START"]
    assert len(starts) == 1
    local_id = starts[0]["messageId"]
    assert len(local_id) <= 36 and local_id != provider_id
    contents = [event for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]
    assert [(event["messageId"], event["delta"]) for event in contents] == [(local_id, "已输出的半段")]
    ends = [event for event in events if event["type"] == "TEXT_MESSAGE_END"]
    assert len(ends) == 1
    assert ends[0]["messageId"] == local_id
    assert ends[0]["phase"] == "final_answer"
    assert ends[0]["interrupted"] is True

    projection, restored = project_and_restore(events)
    assert len(projection["assistant_messages"]) == 1
    message = projection["assistant_messages"][0]
    assert (message["message_id"], message["content"], message["phase"], message["state"]) == (
        local_id, "已输出的半段", "final_answer", "interrupted",
    )
    assert projection["final_message_id"] is None
    assert restored == []
    assert replay_text_and_phase(client, restored) == []
    assert [message for message in agent.messages if message.role == "assistant"] == []
    assert create.await_count == 1
    assert stream.closed


@pytest.mark.asyncio
async def test_failed_response_corrects_closed_final_before_retry_projection(tmp_path):
    provider_id = "reused-provider-id-" + "r" * 60
    old_answer = message_item(provider_id, "失败尝试的旧答复", "final_answer")
    first = EventStream([
        item_event("added", message_item(provider_id, phase="final_answer")),
        delta_event(provider_id, "失败尝试的旧答复"),
        item_event("done", old_answer),
        SimpleNamespace(
            type="response.failed",
            response=SimpleNamespace(
                status="failed", output=[old_answer], usage=None,
                error="provider failed after completing the message item",
            ),
        ),
    ])
    new_answer = message_item(provider_id, "重试后的新答复", "final_answer")
    second = EventStream([
        item_event("added", message_item(provider_id, phase="final_answer")),
        delta_event(provider_id, "重试后的新答复"),
        item_event("done", new_answer), terminal_event(new_answer),
    ])
    client, create = client_with_streams(first, second, retry=True)
    agent = make_agent(tmp_path, llm=client, tools=[], max_steps=5)
    agent.add_user_message("开始答复")

    events = await run_events(agent)

    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["outcome"] == "success"
    starts = [event for event in events if event["type"] == "TEXT_MESSAGE_START"]
    ids = [event["messageId"] for event in starts]
    assert len(ids) == len(set(ids)) == 2
    assert all(len(message_id) <= 36 and message_id != provider_id for message_id in ids)
    assert [event["phase"] for event in starts] == ["final_answer", "final_answer"]
    assert [
        (event["messageId"], event["delta"])
        for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"
    ] == [(ids[0], "失败尝试的旧答复"), (ids[1], "重试后的新答复")]
    ends = [event for event in events if event["type"] == "TEXT_MESSAGE_END"]
    assert [(event["messageId"], bool(event.get("interrupted"))) for event in ends] == [
        (ids[0], False), (ids[0], True), (ids[1], False),
    ]
    correction_index = events.index(ends[1])
    assert correction_index < events.index(starts[1])
    projection, restored = project_and_restore(events)
    assert [
        (message["message_id"], message["content"], message["phase"], message["state"])
        for message in projection["assistant_messages"]
    ] == [
        (ids[0], "失败尝试的旧答复", "final_answer", "interrupted"),
        (ids[1], "重试后的新答复", "final_answer", "complete"),
    ]
    assert projection["transcript_coverage"]["kind"] == "complete"
    assert projection["final_message_id"] == ids[1]
    assert projection["final_message_ids"] is None
    assert len(restored) == 1
    assert restored[0].content == "重试后的新答复"
    assert text_and_phase(restored[0].assistant_text_messages) == [("重试后的新答复", "final_answer")]
    assert replay_text_and_phase(client, restored) == [("重试后的新答复", "final_answer")]
    live_assistant = [message for message in agent.messages if message.role == "assistant"]
    assert [message.content for message in live_assistant] == ["重试后的新答复"]
    assert create.await_count == 2
    assert first.closed and second.closed


@pytest.mark.parametrize("has_identity", [True, False], ids=["identified", "legacy-without-start"])
def test_legacy_failover_reset_filters_only_identified_failed_text(has_identity):
    events = [{"type": "STEP_STARTED", "stepName": "step_1"}]
    if has_identity:
        events.append({"type": "TEXT_MESSAGE_START", "messageId": "old", "role": "assistant"})
    events.extend([
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "old", "delta": "旧模型正文。"},
        {"type": "TEXT_MESSAGE_END", "messageId": "old"},
        {"type": "CUSTOM", "name": "failover_reset", "value": {"model": "fallback"}},
    ])
    if has_identity:
        events.append({"type": "TEXT_MESSAGE_START", "messageId": "new", "role": "assistant"})
    events.extend([
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "new", "delta": "新模型答复。"},
        {"type": "TEXT_MESSAGE_END", "messageId": "new"},
        {"type": "STEP_FINISHED", "stepName": "step_1"},
    ])

    projection, restored = project_and_restore(events)

    assert len(restored) == 1
    if has_identity:
        assert restored[0].content == "新模型答复。"
        assert text_and_phase(restored[0].assistant_text_messages) == [("新模型答复。", None)]
        assert [(message["content"], message["state"]) for message in projection["assistant_messages"]] == [
            ("旧模型正文。", "interrupted"), ("新模型答复。", "complete"),
        ]
    else:
        assert restored[0].content == "旧模型正文。新模型答复。"
        assert restored[0].assistant_text_messages is None


@pytest.mark.asyncio
async def test_native_cancel_publishes_interrupted_end_and_excludes_partial_from_cold_replay(tmp_path):
    gate_entered = asyncio.Event()
    release = asyncio.Event()
    cancel_token = asyncio.Event()

    class WaitingStream(EventStream):
        async def __anext__(self):
            if self.position == self.gate_at:
                gate_entered.set()
            return await super().__anext__()

    answer = message_item("provider-cancel-" + "c" * 60, "已经发布待取消的正文", "final_answer")
    stream = WaitingStream([
        item_event("added", message_item(answer.id, phase=answer.phase)),
        delta_event(answer.id, answer.content[0].text),
        item_event("done", answer), terminal_event(answer),
    ], gate=release, gate_at=2)
    client, create = client_with_streams(stream)
    agent = make_agent(tmp_path, llm=client, tools=[], max_steps=5)
    original_request = "请生成答复，我可能中途停止"
    agent.add_user_message(original_request)
    events = []

    async def collect_until_cancelled():
        async for event in agent.run_agui("session-native", "round-native", cancel_token=cancel_token):
            payload = json.loads(event.model_dump_json(by_alias=True, exclude_none=True))
            events.append(payload)
            if payload["type"] == "TEXT_MESSAGE_CONTENT":
                await asyncio.wait_for(gate_entered.wait(), timeout=2)
                cancel_token.set()

    await asyncio.wait_for(collect_until_cancelled(), timeout=3)

    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["outcome"] == "interrupt"
    assert events[-1]["result"]["reason"] == "user_cancelled"
    assert [event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"] == [
        "已经发布待取消的正文",
    ]
    ends = [event for event in events if event["type"] == "TEXT_MESSAGE_END"]
    assert len(ends) == 1
    assert ends[0]["interrupted"] is True
    assert ends[0]["phase"] == "final_answer"
    projection, restored = project_and_restore(events)
    assert [
        (message["message_id"], message["content"], message["state"])
        for message in projection["assistant_messages"]
    ] == [(ends[0]["messageId"], "已经发布待取消的正文", "interrupted")]
    assert restored == []
    assert [message for message in agent.messages if message.role == "assistant"] == []
    assert [
        message.content for message in agent.messages if message.role == "user" and not message.is_synthetic
    ] == [original_request]
    assert create.await_count == 1
    assert stream.closed


@pytest.mark.parametrize("terminal", [
    pytest.param(
        {"type": "RUN_ERROR", "message": "worker failed before text END was committed"},
        id="run-error",
    ),
    pytest.param(
        {"type": "RUN_FINISHED", "outcome": "interrupt", "result": {"reason": "user_cancelled"}},
        id="abort-committed-before-text-end",
    ),
])
def test_terminal_without_text_end_excludes_known_open_message_from_cold_replay(terminal):
    # A terminal write fence can reject the worker's later TEXT_MESSAGE_END.
    # STEP_FINISHED is already durable, so restoration must consider the whole
    # round before it flushes this earlier step into model history.
    committed_events = [
        {"type": "STEP_STARTED", "stepName": "step_1"},
        {"type": "TEXT_MESSAGE_START", "messageId": "partial", "role": "assistant", "phase": "final_answer"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "partial", "delta": "已提交的半段正文"},
        {"type": "STEP_FINISHED", "stepName": "step_1"},
        terminal,
    ]

    projection, restored = project_and_restore(committed_events)

    assert projection["transcript_coverage"]["kind"] == "complete"
    assert [
        (message["message_id"], message["content"], message["phase"], message["state"])
        for message in projection["assistant_messages"]
    ] == [("partial", "已提交的半段正文", "final_answer", "interrupted")]
    assert projection["final_message_id"] is None
    assert projection["final_message_ids"] is None
    assert restored == []


def test_legacy_step_does_not_disable_terminal_filtering_for_later_unique_message():
    committed_events = [
        {"type": "STEP_STARTED", "stepName": "step_1"},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "旧记录中无法分类的正文。"},
        {"type": "STEP_FINISHED", "stepName": "step_1"},
        {"type": "STEP_STARTED", "stepName": "step_2"},
        {"type": "TEXT_MESSAGE_START", "messageId": "unique-new", "role": "assistant"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "unique-new", "delta": "新步骤中途停止的正文。"},
        {"type": "STEP_FINISHED", "stepName": "step_2"},
        {"type": "RUN_FINISHED", "outcome": "interrupt", "result": {"reason": "user_cancelled"}},
    ]

    projection, restored = project_and_restore(committed_events)

    assert projection["transcript_coverage"]["kind"] == "legacy"
    assert [
        (message["message_id"], message["content"], message["state"])
        for message in projection["assistant_messages"]
    ] == [("unique-new", "新步骤中途停止的正文。", "interrupted")]
    assert len(restored) == 1
    assert restored[0].content == "旧记录中无法分类的正文。"
    assert restored[0].assistant_text_messages is None
