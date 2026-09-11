"""Provider message boundaries must survive streaming, interruption and retries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.llm.openai_responses_client import OpenAIResponsesClient
from src.agent.retry import RetryConfig
from src.agent.schema import Message
from src.agent.schema.schema import AssistantMessageStreamEvent


class EventStream:
    def __init__(self, events, *, gate=None, gate_at=None):
        self.events = iter(events)
        self.gate = gate
        self.gate_at = gate_at
        self.position = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.gate is not None and self.position == self.gate_at:
            await self.gate.wait()
        event = next(self.events, None)
        if event is None:
            raise StopAsyncIteration
        self.position += 1
        if isinstance(event, Exception):
            raise event
        return event

    async def close(self):
        self.closed = True


def message_item(message_id, content="", phase=None, *, refusal=False):
    part = (
        SimpleNamespace(type="refusal", refusal=content)
        if refusal else SimpleNamespace(type="output_text", text=content)
    )
    return SimpleNamespace(
        type="message", role="assistant", id=message_id,
        phase=phase, content=[part] if content else [], status="completed",
    )


def item_event(kind, item, index=0):
    return SimpleNamespace(type=f"response.output_item.{kind}", item=item, output_index=index)


def delta_event(message_id, delta, *, refusal=False):
    return SimpleNamespace(
        type="response.refusal.delta" if refusal else "response.output_text.delta",
        item_id=message_id, content_index=0, delta=delta,
    )


def terminal_event(*items):
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(status="completed", output=list(items), usage=None),
    )


def client_with_streams(*streams, retry=False):
    client = OpenAIResponsesClient(
        api_key="test", api_base="http://127.0.0.1:1/v1", model="test-model",
        enable_reasoning_split=False,
        retry_config=RetryConfig(enabled=retry, max_retries=1, initial_delay=0),
    )
    create = AsyncMock(side_effect=list(streams))
    client.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return client, create


@pytest.mark.asyncio
async def test_native_final_start_is_visible_before_its_first_delta_or_llm_return():
    progress = message_item("msg-progress", "先核对。", "commentary")
    answer = message_item("msg-answer", "结论。", "final_answer")
    release_answer = asyncio.Event()
    stream = EventStream([
        item_event("added", message_item(progress.id, phase=progress.phase)),
        delta_event(progress.id, progress.content[0].text),
        item_event("done", progress),
        item_event("added", message_item(answer.id, phase=answer.phase), index=1),
        delta_event(answer.id, answer.content[0].text),
        item_event("done", answer, index=1),
        terminal_event(progress, answer),
    ], gate=release_answer, gate_at=4)
    client, create = client_with_streams(stream)
    observed = []
    final_started = asyncio.Event()
    legacy_content = AsyncMock()

    async def on_message(event):
        assert isinstance(event, AssistantMessageStreamEvent)
        observed.append(event)
        if event.kind == "start" and event.phase == "final_answer":
            final_started.set()

    task = asyncio.create_task(client.generate_stream(
        [Message(role="user", content="核对资料")],
        on_content=legacy_content, on_message=on_message,
    ))
    start_waiter = asyncio.create_task(final_started.wait())
    try:
        done, _ = await asyncio.wait({task, start_waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()
            pytest.fail("LLM returned before the gated final answer was released")
        assert start_waiter in done, "final_answer START was buffered behind later provider events"
        assert not task.done()
        assert [(event.kind, event.provider_message_id) for event in observed] == [
            ("start", progress.id), ("delta", progress.id), ("end", progress.id),
            ("start", answer.id),
        ]
        assert observed[-1].delta == ""
        legacy_content.assert_not_awaited()
        release_answer.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        release_answer.set()
        for pending in (task, start_waiter):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, start_waiter, return_exceptions=True)

    assert [(event.kind, event.provider_message_id, event.phase, event.delta) for event in observed] == [
        ("start", progress.id, "commentary", ""),
        ("delta", progress.id, "commentary", "先核对。"),
        ("end", progress.id, "commentary", ""),
        ("start", answer.id, "final_answer", ""),
        ("delta", answer.id, "final_answer", "结论。"),
        ("end", answer.id, "final_answer", ""),
    ]
    assert len({event.stream_id for event in observed}) == 1
    assert all(event.stream_id and not event.interrupted for event in observed)
    assert result.content == "先核对。结论。"
    assert [(item.content, item.phase) for item in result.assistant_text_messages] == [
        ("先核对。", "commentary"), ("结论。", "final_answer"),
    ]
    legacy_content.assert_not_awaited()
    assert create.await_count == 1
    assert "on_message" not in create.await_args.kwargs
    assert "on_content" not in create.await_args.kwargs
    assert "on_message" not in client.last_request_snapshot
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [False, True], ids=["text", "refusal"])
async def test_legacy_content_callback_still_receives_responses_deltas(refusal):
    message = message_item("msg-legacy", "正常正文" if not refusal else "无法提供。", refusal=refusal)
    stream = EventStream([
        item_event("added", message_item(message.id)),
        delta_event(message.id, message.content[0].refusal if refusal else message.content[0].text, refusal=refusal),
        item_event("done", message), terminal_event(message),
    ])
    client, _ = client_with_streams(stream)
    on_content = AsyncMock()

    result = await client.generate_stream([], on_content=on_content)

    on_content.assert_awaited_once_with("无法提供。" if refusal else "正常正文")
    assert result.content == ("无法提供。" if refusal else "正常正文")
    assert result.assistant_text_messages[0].phase is None
    assert stream.closed


@pytest.mark.asyncio
async def test_native_refusal_delta_keeps_message_identity_and_final_phase():
    message = message_item("msg-refusal", "无法提供。", "final_answer", refusal=True)
    stream = EventStream([
        item_event("added", message_item(message.id, phase="final_answer")),
        delta_event(message.id, "无法提供。", refusal=True),
        item_event("done", message), terminal_event(message),
    ])
    client, _ = client_with_streams(stream)
    on_message = AsyncMock()
    on_content = AsyncMock()

    result = await client.generate_stream([], on_message=on_message, on_content=on_content)

    events = [call.args[0] for call in on_message.await_args_list]
    assert [(event.kind, event.delta) for event in events] == [
        ("start", ""), ("delta", "无法提供。"), ("end", ""),
    ]
    assert all(event.provider_message_id == message.id and event.phase == "final_answer" for event in events)
    assert result.assistant_text_messages[0].content == "无法提供。"
    on_content.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "missing_terminal"])
async def test_broken_attempt_interrupts_all_published_messages_without_replaying_text(failure):
    complete = message_item("msg-complete", "先核对。", "commentary")
    events = [
        item_event("added", message_item(complete.id, phase=complete.phase)),
        delta_event(complete.id, "先核对。"), item_event("done", complete),
        item_event("added", message_item("msg-open", phase="final_answer"), index=1),
        delta_event("msg-open", "尚未结束"),
    ]
    if failure == "exception":
        events.append(ConnectionError("upstream connection lost"))
    stream = EventStream(events)
    client, _ = client_with_streams(stream)
    on_message = AsyncMock()

    with pytest.raises(
        ConnectionError if failure == "exception" else RuntimeError,
        match="upstream connection lost" if failure == "exception" else "terminal response",
    ):
        await client.generate_stream([], on_message=on_message)

    observed = [call.args[0] for call in on_message.await_args_list]
    endings = [event for event in observed if event.kind == "end"]
    assert [(event.provider_message_id, event.interrupted) for event in endings] == [
        ("msg-complete", False), ("msg-complete", True), ("msg-open", True),
    ]
    assert [
        (event.provider_message_id, event.delta) for event in observed if event.kind == "delta"
    ] == [("msg-complete", "先核对。"), ("msg-open", "尚未结束")]
    assert [event.provider_message_id for event in observed if event.kind == "start"] == [
        "msg-complete", "msg-open",
    ]
    assert endings[1].phase == "commentary"
    assert endings[-1].phase == "final_answer"
    assert all(event.delta == "" for event in endings)
    assert stream.closed


@pytest.mark.asyncio
async def test_done_message_publishes_only_unstreamed_suffix_before_end():
    answer = message_item("msg-answer", "AB", "final_answer")
    stream = EventStream([
        item_event("added", message_item(answer.id, phase=answer.phase)),
        delta_event(answer.id, "A"),
        item_event("done", answer), terminal_event(answer),
    ])
    client, _ = client_with_streams(stream)
    on_message = AsyncMock()
    on_content = AsyncMock()

    result = await client.generate_stream([], on_message=on_message, on_content=on_content)

    observed = [call.args[0] for call in on_message.await_args_list]
    assert [(event.kind, event.delta, event.interrupted) for event in observed] == [
        ("start", "", False), ("delta", "A", False),
        ("delta", "B", False), ("end", "", False),
    ]
    published = "".join(event.delta for event in observed if event.kind == "delta")
    assert result.content == published == "AB"
    assert result.assistant_text_messages[0].content == published
    on_content.assert_not_awaited()
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_at", ["done_replaces_prefix", "terminal_extends_closed_message"])
async def test_conflicting_completed_body_is_protocol_error_without_post_end_deltas(conflict_at):
    initial = message_item("msg-answer", "A", "final_answer")
    done = message_item(initial.id, "Z", initial.phase) if conflict_at == "done_replaces_prefix" else initial
    terminal = message_item(initial.id, "AB", initial.phase)
    stream = EventStream([
        item_event("added", message_item(initial.id, phase=initial.phase)),
        delta_event(initial.id, "A"), item_event("done", done),
        terminal_event(terminal),
    ])
    client, _ = client_with_streams(stream)
    on_message = AsyncMock()

    with pytest.raises(ValueError):
        await client.generate_stream([], on_message=on_message)

    observed = [call.args[0] for call in on_message.await_args_list]
    assert [(event.kind, event.delta) for event in observed if event.kind != "end"] == [
        ("start", ""), ("delta", "A"),
    ]
    endings = [event.interrupted for event in observed if event.kind == "end"]
    assert endings == ([True] if conflict_at == "done_replaces_prefix" else [False, True])
    assert observed[-1].kind == "end" and observed[-1].interrupted
    assert all(event.provider_message_id == initial.id for event in observed)
    assert stream.closed


@pytest.mark.asyncio
async def test_retry_separates_attempt_ids_and_does_not_append_failed_body():
    message_id = "provider-reused-id"
    first = EventStream([
        item_event("added", message_item(message_id, phase="final_answer")),
        delta_event(message_id, "旧的半段"),
        ConnectionError("retryable stream disconnect"),
    ])
    answer = message_item(message_id, "完整新答复", "final_answer")
    second = EventStream([
        item_event("added", message_item(message_id, phase="final_answer")),
        delta_event(message_id, "完整新答复"),
        item_event("done", answer), terminal_event(answer),
    ])
    client, create = client_with_streams(first, second, retry=True)
    on_message = AsyncMock()
    on_content = AsyncMock()

    result = await client.generate_stream([], on_message=on_message, on_content=on_content)

    observed = [call.args[0] for call in on_message.await_args_list]
    assert [(event.kind, event.delta, event.interrupted) for event in observed] == [
        ("start", "", False), ("delta", "旧的半段", False), ("end", "", True),
        ("start", "", False), ("delta", "完整新答复", False), ("end", "", False),
    ]
    first_ids = {event.stream_id for event in observed[:3]}
    second_ids = {event.stream_id for event in observed[3:]}
    assert len(first_ids) == len(second_ids) == 1
    assert first_ids.isdisjoint(second_ids)
    assert all(event.provider_message_id == message_id for event in observed)
    assert result.content == "完整新答复"
    assert [message.content for message in result.assistant_text_messages] == ["完整新答复"]
    assert create.await_count == 2
    assert first.closed and second.closed
    on_content.assert_not_awaited()
