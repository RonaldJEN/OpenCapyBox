"""工具参数必须无损、完整，拒绝后必须保留可供模型纠正的调用结果。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.agent import Agent
from src.agent.llm.openai_client import OpenAIClient
from src.agent.schema import LLMResponse
from src.agent.tools.base import Tool, ToolResult
from tests.helpers import FakeAsyncStream, MockLLMClient, make_agent


class _FileWriteTool(Tool):
    name = "write_report"
    description = "Overwrite the test report with the supplied content."
    repeat_policy = "mutating"
    parameters = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }

    def __init__(self, path):
        self.path = path
        self.execute_count = 0

    async def execute(self, content):
        self.execute_count += 1
        self.path.write_bytes(content.encode("utf-8"))
        return ToolResult(success=True, content="REPORT_WRITTEN")


class _OptionalArgumentsTool(Tool):
    description = "Record an invocation without required parameters."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name="optional_tool"):
        self._name = name
        self.execute_count = 0

    @property
    def name(self):
        return self._name

    async def execute(self, **kwargs):
        self.execute_count += 1
        return ToolResult(success=True, content="OPTIONAL_TOOL_EXECUTED")


class _StreamLLM(MockLLMClient):
    """Use the real provider stream parser without making a network request."""

    def __init__(self, batches, *, observe=None):
        super().__init__()
        self.batches = list(batches)
        self.requests = []
        self.observations = []
        self.observe = observe

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if self.observe is not None:
            self.observations.append(self.observe())
        if not self.batches:
            return LLMResponse(content="done", finish_reason="stop")

        calls, finish_reason = self.batches.pop(0)
        chunks = []
        for index, (call_id, name, arguments) in enumerate(calls):
            midpoint = len(arguments) // 2
            for part, fragment in enumerate((arguments[:midpoint], arguments[midpoint:])):
                delta = SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(
                        index=index,
                        id=call_id if part == 0 else None,
                        function=SimpleNamespace(
                            name=name if part == 0 else None,
                            arguments=fragment,
                        ),
                    )],
                )
                chunks.append(SimpleNamespace(choices=[SimpleNamespace(
                    delta=delta, finish_reason=None,
                )]))
        chunks.append(SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=None),
            finish_reason=finish_reason,
        )]))

        client = object.__new__(OpenAIClient)
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(return_value=FakeAsyncStream(chunks)),
        )))
        return await client._make_stream_request(
            {},
            on_content=kwargs.get("on_content"),
            on_thinking=kwargs.get("on_thinking"),
            on_tool_call=kwargs.get("on_tool_call"),
        )


async def _run(tmp_path, llm, tool, **kwargs):
    agent = make_agent(tmp_path, llm=llm, tools=[tool], max_steps=5, **kwargs)
    agent.add_user_message("执行工具并根据错误重新生成完整参数。")
    events = [event async for event in agent.run_agui("safety-thread", "safety-run")]
    assert events[-1].type.value == "RUN_FINISHED"
    agent._validate_complete_tool_pairs(agent.messages)
    results = {
        event.tool_call_id: event
        for event in events
        if event.type.value == "TOOL_CALL_RESULT"
    }
    return agent, results


def _tool_messages(messages):
    return {message.tool_call_id: message for message in messages if message.role == "tool"}


@pytest.mark.asyncio
async def test_legal_json_writes_chinese_quotes_and_complete_body_unchanged(tmp_path):
    path = tmp_path / "report.txt"
    body = '测试“甲”“乙”。\n‘丙’与「丁」；ASCII "quoted"，末尾校验🙂'
    tool = _FileWriteTool(path)
    llm = _StreamLLM([([
        ("valid-body", tool.name, json.dumps({"content": body}, ensure_ascii=False)),
    ], "tool_calls")])

    _agent, results = await _run(tmp_path, llm, tool)

    assert path.read_bytes() == body.encode("utf-8")
    assert tool.execute_count == 1
    assert results["valid-body"].success is True


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_arguments", [
    '{"content":"未完成正文',
    '{"content":"错误正文",}',
], ids=["unterminated-string", "invalid-trailing-comma"])
async def test_invalid_arguments_preserve_file_then_model_can_retry(tmp_path, raw_arguments):
    path = tmp_path / "report.txt"
    original = "原有报告必须保留\n".encode("utf-8")
    path.write_bytes(original)
    body = "重新生成的完整“正文”。末尾校验"
    tool = _FileWriteTool(path)
    llm = _StreamLLM([
        ([("invalid-body", tool.name, raw_arguments)], "tool_calls"),
        ([("corrected-body", tool.name, json.dumps({"content": body}, ensure_ascii=False))], "tool_calls"),
    ], observe=path.read_bytes)

    _agent, results = await _run(tmp_path, llm, tool)

    assert llm.observations == [original, original, body.encode("utf-8")]
    assert tool.execute_count == 1
    assert results["invalid-body"].success is False
    assert results["corrected-body"].success is True
    correction_input = _tool_messages(llm.requests[1])
    assert "invalid-body" in correction_input
    feedback = str(correction_input["invalid-body"].content)
    assert feedback.startswith("Error: tool_arguments_invalid: invalid_json.")
    assert "not executed" in feedback
    assert path.read_bytes() == body.encode("utf-8")


@pytest.mark.asyncio
async def test_length_response_blocks_complete_and_partial_calls_in_same_batch(tmp_path):
    path = tmp_path / "report.txt"
    original = b"existing report must survive"
    path.write_bytes(original)
    tool = _FileWriteTool(path)
    llm = _StreamLLM([([
        ("complete-in-truncated-response", tool.name, '{"content":"complete but rejected"}'),
        ("partial-in-truncated-response", tool.name, '{"content":"partial'),
    ], "length")])

    _agent, results = await _run(tmp_path, llm, tool)

    assert tool.execute_count == 0
    assert path.read_bytes() == original
    expected_ids = {"complete-in-truncated-response", "partial-in-truncated-response"}
    assert set(results) == expected_ids
    assert all(result.success is False for result in results.values())
    feedback = _tool_messages(llm.requests[1])
    assert set(feedback) == expected_ids
    assert all("truncat" in str(message.content).lower() for message in feedback.values())


@pytest.mark.asyncio
async def test_parse_error_does_not_execute_tool_with_no_required_arguments(tmp_path, monkeypatch):
    permission_audit = MagicMock()
    monkeypatch.setattr(Agent, "_record_permission_audit", permission_audit)
    tool = _OptionalArgumentsTool()
    llm = _StreamLLM([([("invalid-optional", tool.name, '{"unfinished":')], "tool_calls")])

    _agent, results = await _run(tmp_path, llm, tool)

    assert tool.execute_count == 0
    assert results["invalid-optional"].success is False
    assert "invalid-optional" in _tool_messages(llm.requests[1])
    permission_audit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("rejected_arguments", "finish_reason"), [
    ('{"unfinished":', "tool_calls"),
    ("{}", "length"),
], ids=["repeated-parse-errors", "repeated-truncated-responses"])
async def test_rejected_calls_do_not_block_later_valid_retry(
    tmp_path, rejected_arguments, finish_reason,
):
    tool = _OptionalArgumentsTool()
    llm = _StreamLLM([
        ([("rejected-first", tool.name, rejected_arguments)], finish_reason),
        ([("rejected-second", tool.name, rejected_arguments)], finish_reason),
        ([("valid-retry", tool.name, "{}")], "tool_calls"),
    ], observe=lambda: tool.execute_count)

    _agent, results = await _run(tmp_path, llm, tool)

    assert llm.observations == [0, 0, 0, 1]
    assert tool.execute_count == 1
    assert results["rejected-first"].success is False
    assert results["rejected-second"].success is False
    assert results["valid-retry"].success is True
    assert set(_tool_messages(llm.requests[2])) == {"rejected-first", "rejected-second"}


@pytest.mark.asyncio
async def test_parallel_subagent_batch_preserves_error_on_later_call(tmp_path):
    tool = _OptionalArgumentsTool(name="sub_agent")
    llm = _StreamLLM([([
        ("valid-subagent", tool.name, "{}"),
        ("invalid-subagent", tool.name, '{"unfinished":'),
    ], "tool_calls")])

    _agent, results = await _run(tmp_path, llm, tool, subagent_max_parallel=2)

    assert tool.execute_count == 1
    assert results["valid-subagent"].success is True
    assert results["invalid-subagent"].success is False
    assert set(_tool_messages(llm.requests[1])) == {"valid-subagent", "invalid-subagent"}
