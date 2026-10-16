"""Patch syntax corrections remain possible across reads and history compaction."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.agent import Agent, _ToolLoopGuard
from src.agent.context_compaction import SUMMARY_PREFIX
from src.agent.schema import FunctionCall, LLMResponse, Message, ToolCall
from src.agent.tools.apply_patch_tool import SandboxApplyPatchTool
from src.agent.tools.base import ToolResult
from tests.helpers import MockTool


BAD_PATCH = (
    "*** Begin Patch\n*** Update File: demo.py\n@@\n"
    "@@ def build_report():\n+    normalize_dates()\n     return rows\n*** End Patch"
)
GOOD_PATCH = BAD_PATCH.replace("@@\n@@ def build_report():", "@@ def build_report():")


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_repeated_and_changed_syntax_failures_can_be_corrected(tmp_path, compact):
    tool = SandboxApplyPatchTool(MagicMock())
    tool._prepare_patch = AsyncMock(return_value=())
    execute = AsyncMock(wraps=tool.execute)
    tool.execute = execute
    read = MockTool("read_file")
    invalid_patches = [BAD_PATCH] * 5 + [
        GOOD_PATCH.replace("*** Begin Patch", f"*** Invalid Begin {index}")
        for index in range(4)
    ]
    actions = [
        ("apply_patch", {"patch": patch})
        for patch in [*invalid_patches, GOOD_PATCH]
    ]
    actions.insert(4, ("read_file", {"param1": "demo.py"}))

    class ScriptedLLM:
        last_request_snapshot = None

        def __init__(self):
            self.calls = 0
            self.summaries = 0

        async def generate(self, messages, **kwargs):
            self.summaries += 1
            # Exercise real compaction that forgets earlier syntax failures.
            return LLMResponse(content="Continue editing demo.py.", finish_reason="stop")

        async def generate_stream(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 2 and compact:
                assert any(str(m.content).startswith(SUMMARY_PREFIX) for m in messages)
                assert not any(m.role == "tool" for m in messages)
                agent.token_limit = 100_000
            if self.calls > len(actions):
                return LLMResponse(content="done", finish_reason="stop")
            if self.calls == 1 and compact:
                agent.token_limit = 1
            name, args = actions[self.calls - 1]
            return LLMResponse(
                content="", finish_reason="tool_calls",
                tool_calls=[ToolCall(
                    id=f"call-{self.calls}", type="function",
                    function=FunctionCall(name=name, arguments=args),
                )],
            )

    llm = ScriptedLLM()
    agent = Agent(
        llm_client=llm, system_prompt="test", tools=[tool, read],
        workspace_dir=str(tmp_path), token_limit=100_000,
        context_window=30_000, max_output_tokens=2_000, max_steps=len(actions) + 2,
    )
    agent.messages.append(Message(role="user", content="fix demo.py"))
    events = [event async for event in agent.run_agui("thread", "run")]

    assert events[-1].type.value == "RUN_FINISHED"
    assert events[-1].outcome == "success"
    assert [call.kwargs["patch"] for call in execute.await_args_list] == [
        *invalid_patches, GOOD_PATCH,
    ]
    assert tool._prepare_patch.await_count == 1
    assert read.execute_count == 1
    assert llm.summaries == int(compact)
    starts = [event for event in events if event.type.value == "TOOL_CALL_START"]
    results = [event for event in events if event.type.value == "TOOL_CALL_RESULT"]
    expected_ids = [f"call-{index}" for index in range(1, len(actions) + 1)]
    assert [event.tool_call_id for event in starts] == expected_ids
    assert [event.tool_call_id for event in results] == expected_ids
    assert sum(event.success is False for event in results) == len(invalid_patches)
    assert sum("PATCH_EMPTY_CHUNK" in event.content for event in results) == 5
    assert sum("PATCH_INVALID_SYNTAX" in event.content for event in results) == 4
    agent._validate_complete_tool_pairs(agent.messages)


@pytest.mark.parametrize("result", [
    ToolResult(success=False, error="temporary network failure"),
    ToolResult(success=False, error="PatchError: Failed to find expected lines"),
])
def test_runtime_failures_remain_retryable(result):
    tool = SandboxApplyPatchTool(MagicMock())
    guard = _ToolLoopGuard()
    arguments = {"patch": GOOD_PATCH}
    fingerprint, policy = guard._fingerprint(tool, tool.name, arguments)
    guard.observe(
        fingerprint=fingerprint, policy=policy, tool_name=tool.name,
        result=result, result_content=result.error, arguments=arguments,
    )
    assert guard.check(tool=tool, tool_name=tool.name, arguments=arguments)[2] is None


def test_uncertain_patch_requires_readback_even_when_rewritten():
    tool = SandboxApplyPatchTool(MagicMock())
    guard = _ToolLoopGuard(workspace_dir="/home/user")
    arguments = {"patch": GOOD_PATCH}
    fingerprint, policy = guard._fingerprint(tool, tool.name, arguments)
    guard.observe(
        fingerprint=fingerprint, policy=policy, tool_name=tool.name,
        result=ToolResult(success=False, error="lost write response", outcome_uncertain=True),
        result_content="lost write response", arguments=arguments,
    )
    changed = {"patch": GOOD_PATCH.replace("normalize_dates", "normalize_again")}
    assert "read_file" in guard.check(tool=tool, tool_name=tool.name, arguments=changed)[2]
    read = MockTool("read_file")
    read_args = {"path": "/home/user/demo.py"}
    fingerprint, policy = guard._fingerprint(read, read.name, read_args)
    guard.observe(
        fingerprint=fingerprint, policy=policy, tool_name=read.name,
        result=ToolResult(success=True, content="verified"),
        result_content="verified", arguments=read_args,
    )
    assert guard.check(tool=tool, tool_name=tool.name, arguments=changed)[2] is None
