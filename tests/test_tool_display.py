"""Tool identity survives live events and historical projection without catalog lookups."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.schema import FunctionCall, LLMResponse, ToolCall
from src.agent.tools.base import ToolExposure
from src.agent.tools.mcp_tool import McpRemoteTool
from src.api.schemas.chat import StepData
from src.api.services.history_service import HistoryService
from src.api.services.mcp_runtime import McpToolSnapshot, model_tool_name
from tests.helpers import MockLLMClient, make_agent, make_query_db


def remote_tool():
    snapshot = McpToolSnapshot(
        installation_id="installation-1", server_id="server-12345678",
        server_name="资料服务", source="personal", raw_name="lookup-report",
        model_name=model_tool_name("server-12345678", "lookup-report"),
        title="查找报告", description="Find an existing report.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        schema_hash="schema-1", connection_fingerprint="binding-1",
    )
    runtime = SimpleNamespace(
        call_tool=AsyncMock(return_value={"content": [{"type": "text", "text": "Found report"}]}),
        current_execution_fingerprint=lambda **_: "binding-1",
    )
    tool = McpRemoteTool(user_id="user-1", snapshot=snapshot, runtime=runtime)
    # Direct exposure isolates display transport from deferred discovery policy.
    tool.exposure = ToolExposure.DIRECT
    return tool, runtime


def rebuild_steps(events):
    rows = [SimpleNamespace(
        event_type=event["type"], payload=json.dumps(event), sequence=index,
        timestamp=event.get("timestamp"), created_at=None,
    ) for index, event in enumerate(events, start=1)]
    return HistoryService(make_query_db(all_results=rows))._rebuild_steps_from_events("run-1")


@pytest.mark.asyncio
async def test_agent_tool_display_is_identical_in_live_events_and_cold_ui_history(tmp_path):
    tool, runtime = remote_tool()
    llm = MockLLMClient()
    llm.responses = [
        LLMResponse(content="查一下资料。", finish_reason="tool_calls", tool_calls=[ToolCall(
            id="call-1", type="function", function=FunctionCall(name=tool.name, arguments={"query": "report"}),
        )]),
        LLMResponse(content="查询完成。", finish_reason="stop"),
    ]
    agent = make_agent(tmp_path, llm=llm, tools=[tool], max_steps=5)
    agent.add_user_message("查找报告")
    events = [event.model_dump(mode="json", by_alias=True, exclude_none=True)
              async for event in agent.run_agui("session-1", "run-1")]
    start = next(event for event in events if event["type"] == "TOOL_CALL_START")
    expected = {"provider": "mcp", "server_name": "资料服务",
                "tool_name": "lookup-report", "tool_title": "查找报告"}
    assert start["toolCallName"] == tool.name
    assert start["toolDisplay"] == expected
    runtime.call_tool.assert_awaited_once()

    # A later catalog rename must not change this invocation's historical label.
    tool._snapshot = replace(tool._snapshot, server_name="已改名服务", title="已改名工具")
    steps = rebuild_steps(events)
    restored = StepData.model_validate(steps[0]).tool_calls[0]
    assert restored.name == start["toolCallName"]
    assert restored.tool_display.model_dump(exclude_none=True) == expected

    legacy = [{key: value for key, value in event.items() if key != "toolDisplay"} for event in events]
    assert StepData.model_validate(rebuild_steps(legacy)[0]).tool_calls[0].tool_display is None


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"content": "sub_agent failed: invalid sub_agent_type"}, None),
        ({"content": json.dumps({"success": False})}, False),
        ({"isError": True, "content": "legacy explicit failure"}, False),
        ({"success": True, "isError": True, "content": json.dumps({"success": False})}, True),
    ],
)
def test_history_tool_result_uses_only_explicit_legacy_success_facts(event, expected):
    steps = rebuild_steps([
        {"type": "STEP_STARTED"},
        {"type": "TOOL_CALL_RESULT", "toolCallId": "call-1", **event},
        {"type": "STEP_FINISHED"},
    ])
    assert steps[0]["tool_results"][0]["success"] is expected
