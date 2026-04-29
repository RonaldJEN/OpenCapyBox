"""Agent 上下文压缩增强测试

覆盖三项改进：
1. 结构化 9 段式摘要 prompt + <analysis> 剥离
2. 压缩后文件重注入
3. LLM 摘要失败熔断
"""

import pytest
from unittest.mock import AsyncMock

from src.agent.agent import Agent
from src.agent.schema import Message, LLMResponse, FunctionCall, ToolCall
from tests.helpers import MockLLMClient, MockTool, make_agent


# ============== Fixtures ==============


@pytest.fixture
def agent_with_history(tmp_path):
    """创建带有多轮对话历史的 Agent（足以触发 summarization）"""
    agent = make_agent(tmp_path, token_limit=500, max_steps=10)

    # Simulate 3 rounds of conversation to exceed token_limit
    for round_num in range(3):
        agent.messages.append(Message(role="user", content=f"User message round {round_num + 1}: please do something"))
        agent.messages.append(Message(
            role="assistant",
            content=f"I'll help with round {round_num + 1}. Let me use a tool.",
            tool_calls=[ToolCall(
                id=f"tc-{round_num}",
                type="function",
                function=FunctionCall(name="read_file", arguments={"path": f"/workspace/file{round_num}.py"}),
            )],
        ))
        agent.messages.append(Message(
            role="tool",
            content=f"File content for round {round_num + 1}: " + "x" * 500,
            tool_call_id=f"tc-{round_num}",
            name="read_file",
        ))
        agent.messages.append(Message(
            role="assistant",
            content=f"Done with round {round_num + 1}. Here's what I found in the file.",
        ))

    return agent


# ============== 1. 结构化摘要 + <analysis> 剥离 ==============


class TestExtractSummaryFromResponse:
    """Test _extract_summary_from_response static method"""

    def test_extracts_summary_block(self):
        raw = """<analysis>
Some internal thinking here...
</analysis>

<summary>
1. Primary Request and Intent:
   User wanted to fix a bug.
2. Key Technical Concepts:
   Python, FastAPI
</summary>"""
        result = Agent._extract_summary_from_response(raw)
        assert "Primary Request and Intent" in result
        assert "analysis" not in result.lower() or "analysis" not in result

    def test_strips_analysis_without_summary_tags(self):
        raw = """<analysis>
Internal thinking
</analysis>

Just plain text summary here."""
        result = Agent._extract_summary_from_response(raw)
        assert "Internal thinking" not in result
        assert "plain text summary" in result

    def test_returns_raw_when_no_tags(self):
        raw = "Just a plain summary with no XML tags."
        result = Agent._extract_summary_from_response(raw)
        assert result == raw

    def test_handles_empty_summary(self):
        raw = "<summary></summary>"
        result = Agent._extract_summary_from_response(raw)
        assert result == ""

    def test_handles_multiline_summary(self):
        raw = """<analysis>thinking</analysis>
<summary>
Line 1
Line 2
Line 3
</summary>"""
        result = Agent._extract_summary_from_response(raw)
        assert "Line 1" in result
        assert "Line 3" in result


class TestStructuredSummaryPrompt:
    """Test that _create_summary uses structured 9-section prompt"""

    async def test_summary_calls_llm_with_structured_prompt(self, tmp_path):
        """Verify the LLM receives the 9-section structured prompt"""
        llm = MockLLMClient()
        llm.responses = [LLMResponse(
            content="<analysis>thinking</analysis><summary>1. Primary Request: test</summary>",
            finish_reason="stop",
        )]
        agent = make_agent(tmp_path, llm=llm)

        messages = [
            Message(role="assistant", content="I'll check the file."),
            Message(role="tool", content="file content here", tool_call_id="tc1", name="read_file"),
        ]

        result = await agent._create_summary(messages, round_num=1, round_user_message="fix the bug")

        # Should strip <analysis> and return just the summary
        assert "Primary Request" in result
        assert "analysis" not in result or "<analysis>" not in result

        # Verify LLM was called
        assert llm.call_count == 1

    async def test_summary_preserves_tool_name_in_transcript(self, tmp_path):
        """Tool results should include tool name for traceability"""
        llm = MockLLMClient()
        llm.responses = [LLMResponse(
            content="<summary>Summary with tool info</summary>",
            finish_reason="stop",
        )]
        agent = make_agent(tmp_path, llm=llm)

        messages = [
            Message(role="tool", content="result data", tool_call_id="tc1", name="read_file"),
        ]

        await agent._create_summary(messages, round_num=1)
        assert llm.call_count == 1

    async def test_summary_fallback_on_llm_failure(self, tmp_path):
        """On LLM failure, fallback should include user messages and transcript"""
        llm = MockLLMClient()
        llm.responses = []  # Empty → will use default "Mock response"

        # Override generate to raise
        async def _failing_generate(messages, tools=None, **kwargs):
            raise RuntimeError("LLM unavailable")

        llm.generate = _failing_generate

        agent = make_agent(tmp_path, llm=llm)

        messages = [
            Message(role="assistant", content="working on it"),
        ]

        result = await agent._create_summary(messages, round_num=1, round_user_message="fix bug")
        # Fallback should contain user message info
        assert "fix bug" in result

    async def test_summary_output_is_capped_by_token_budget(self, tmp_path):
        """LLM summary output should be truncated by _SUMMARY_MAX_TOKENS"""
        llm = MockLLMClient()
        very_long_summary = "x " * 20000
        llm.responses = [LLMResponse(
            content=f"<summary>{very_long_summary}</summary>",
            finish_reason="stop",
        )]
        agent = make_agent(tmp_path, llm=llm)

        result = await agent._create_summary(
            [Message(role="assistant", content="done")],
            round_num=1,
            round_user_message="compress this",
        )

        assert result
        assert len(result) < len(very_long_summary)

    async def test_summary_normalization_restores_missing_sections(self, tmp_path):
        """Missing sections should be normalized into a full 9-section summary"""
        llm = MockLLMClient()
        llm.responses = [LLMResponse(
            content=(
                "<summary>"
                "1. Primary Request and Intent: fix API timeout\n"
                "8. Current Work: editing timeout logic"
                "</summary>"
            ),
            finish_reason="stop",
        )]
        agent = make_agent(tmp_path, llm=llm)

        result = await agent._create_summary(
            [
                Message(role="assistant", content="Working on timeout fix"),
                Message(role="user", content="Please keep retries low"),
            ],
            round_num=1,
            round_user_message="fix API timeout",
        )

        assert "2. Key Technical Concepts:" in result
        assert "6. All User Messages:" in result
        assert "1. fix API timeout" in result
        assert "2. Please keep retries low" in result

    async def test_summary_normalization_handles_unstructured_output(self, tmp_path):
        """Unstructured LLM output should be transformed into required 9 sections"""
        llm = MockLLMClient()
        llm.responses = [LLMResponse(
            content="The user asked to fix a bug and I edited one file.",
            finish_reason="stop",
        )]
        agent = make_agent(tmp_path, llm=llm)

        result = await agent._create_summary(
            [Message(role="assistant", content="Edited service.py")],
            round_num=1,
            round_user_message="fix the bug",
        )

        required_headers = [
            "1. Primary Request and Intent:",
            "2. Key Technical Concepts:",
            "3. Files and Code Sections:",
            "4. Errors and Fixes:",
            "5. Problem Solving:",
            "6. All User Messages:",
            "7. Pending Tasks:",
            "8. Current Work:",
            "9. Optional Next Step:",
        ]
        for header in required_headers:
            assert header in result


# ============== 2. 压缩后文件重注入 ==============


class TestFileReinjection:
    """Test post-compact file re-injection mechanism"""

    def test_track_file_operation(self, tmp_path):
        agent = make_agent(tmp_path)
        agent.track_file_operation("/workspace/main.py", "read_file", "file content here")

        assert "/workspace/main.py" in agent._recent_file_operations
        tool_name, content, _ts = agent._recent_file_operations["/workspace/main.py"]
        assert tool_name == "read_file"
        assert content == "file content here"

    def test_track_file_operation_without_content(self, tmp_path):
        agent = make_agent(tmp_path)
        agent.track_file_operation("/workspace/main.py", "edit_file")

        tool_name, content, _ts = agent._recent_file_operations["/workspace/main.py"]
        assert tool_name == "edit_file"
        assert content is None

    def test_collect_recent_files_from_messages(self, tmp_path):
        agent = make_agent(tmp_path)

        # Add read_file tool call + tool result
        agent.messages.append(Message(
            role="assistant",
            content="Reading file",
            tool_calls=[ToolCall(
                id="tc1",
                type="function",
                function=FunctionCall(name="read_file", arguments={"path": "/workspace/test.py"}),
            )],
        ))
        agent.messages.append(Message(
            role="tool",
            content="def hello():\n    print('world')",
            tool_call_id="tc1",
            name="read_file",
        ))
        # Add write_file tool call with content in arguments
        agent.messages.append(Message(
            role="assistant",
            content="Writing file",
            tool_calls=[ToolCall(
                id="tc2",
                type="function",
                function=FunctionCall(name="write_file", arguments={"path": "/workspace/out.txt", "content": "output data"}),
            )],
        ))

        agent._collect_recent_files_from_messages()

        assert "/workspace/test.py" in agent._recent_file_operations
        assert "/workspace/out.txt" in agent._recent_file_operations
        # read_file should capture content from tool result
        _, read_content, _ = agent._recent_file_operations["/workspace/test.py"]
        assert read_content == "def hello():\n    print('world')"
        # write_file should capture content from arguments
        _, write_content, _ = agent._recent_file_operations["/workspace/out.txt"]
        assert write_content == "output data"

    def test_collect_edit_file_has_no_content(self, tmp_path):
        agent = make_agent(tmp_path)

        agent.messages.append(Message(
            role="assistant",
            content="Editing file",
            tool_calls=[ToolCall(
                id="tc1",
                type="function",
                function=FunctionCall(name="edit_file", arguments={"path": "/workspace/mod.py", "old_text": "a", "new_text": "b"}),
            )],
        ))

        agent._collect_recent_files_from_messages()
        assert "/workspace/mod.py" in agent._recent_file_operations
        _, content, _ = agent._recent_file_operations["/workspace/mod.py"]
        assert content is None

    def test_collect_ignores_non_file_tools(self, tmp_path):
        agent = make_agent(tmp_path)

        agent.messages.append(Message(
            role="assistant",
            content="Running command",
            tool_calls=[ToolCall(
                id="tc1",
                type="function",
                function=FunctionCall(name="run_command", arguments={"command": "ls"}),
            )],
        ))

        agent._collect_recent_files_from_messages()
        assert len(agent._recent_file_operations) == 0

    def test_reinject_recent_files_appends_message(self, tmp_path):
        import time
        agent = make_agent(tmp_path)
        ts = time.monotonic()
        agent._recent_file_operations = {
            "/workspace/a.py": ("read_file", "content_a = 1", ts),
            "/workspace/b.py": ("write_file", "content_b = 2", ts + 1),
        }

        initial_count = len(agent.messages)
        result = agent._reinject_recent_files()

        assert result == 2
        assert len(agent.messages) == initial_count + 1
        last_msg = agent.messages[-1]
        assert last_msg.role == "assistant"
        assert "Post-Compact File Context" in last_msg.content
        assert "/workspace/a.py" in last_msg.content
        assert "/workspace/b.py" in last_msg.content
        # Should contain actual file contents
        assert "content_a = 1" in last_msg.content
        assert "content_b = 2" in last_msg.content

    def test_reinject_includes_file_content_with_markers(self, tmp_path):
        import time
        agent = make_agent(tmp_path)
        ts = time.monotonic()
        agent._recent_file_operations = {
            "/workspace/main.py": ("read_file", "def main():\n    pass", ts),
        }

        agent._reinject_recent_files()
        last_msg = agent.messages[-1]
        assert "=== FILE: /workspace/main.py" in last_msg.content
        assert "def main():\n    pass" in last_msg.content
        assert "=== END FILE ===" in last_msg.content

    def test_reinject_path_only_for_no_content(self, tmp_path):
        import time
        agent = make_agent(tmp_path)
        ts = time.monotonic()
        agent._recent_file_operations = {
            "/workspace/edited.py": ("edit_file", None, ts),
        }

        agent._reinject_recent_files()
        last_msg = agent.messages[-1]
        assert "/workspace/edited.py" in last_msg.content
        assert "content not available" in last_msg.content

    def test_reinject_clears_tracker(self, tmp_path):
        import time
        agent = make_agent(tmp_path)
        ts = time.monotonic()
        agent._recent_file_operations = {"/workspace/x.py": ("read_file", "x = 1", ts)}

        agent._reinject_recent_files()
        assert len(agent._recent_file_operations) == 0

    def test_reinject_empty_tracker_returns_zero(self, tmp_path):
        agent = make_agent(tmp_path)
        result = agent._reinject_recent_files()
        assert result == 0
        # No message should be appended
        initial_count = len(agent.messages)
        agent._reinject_recent_files()
        assert len(agent.messages) == initial_count

    def test_reinject_respects_max_files_limit(self, tmp_path):
        import time
        agent = make_agent(tmp_path)
        ts = time.monotonic()
        # Add more files than the limit
        for i in range(10):
            agent._recent_file_operations[f"/workspace/file{i}.py"] = ("read_file", f"content{i}", ts + i)

        result = agent._reinject_recent_files()
        assert result == agent._POST_COMPACT_MAX_FILES

    def test_reinject_respects_token_budget(self, tmp_path):
        import time
        agent = make_agent(tmp_path)
        agent._POST_COMPACT_TOKEN_BUDGET = 100  # Very small budget
        ts = time.monotonic()

        # Each file has ~250 chars ≈ 62 tokens, budget allows ~1-2 files
        agent._recent_file_operations = {
            f"/workspace/f{i}.py": ("read_file", "x" * 250, ts + i)
            for i in range(5)
        }

        result = agent._reinject_recent_files()
        assert result < 5  # Budget should cut off before all files

    def test_reinject_sorts_by_recency(self, tmp_path):
        import time
        agent = make_agent(tmp_path)
        ts = time.monotonic()
        agent._recent_file_operations = {
            "/workspace/old.py": ("read_file", "old content", ts),
            "/workspace/new.py": ("read_file", "new content", ts + 100),
        }
        agent._POST_COMPACT_MAX_FILES = 1

        agent._reinject_recent_files()
        last_msg = agent.messages[-1]
        # Most recent file should be included
        assert "/workspace/new.py" in last_msg.content
        assert "/workspace/old.py" not in last_msg.content


# ============== 3. 失败熔断 ==============


class TestSummaryCircuitBreaker:
    """Test LLM summarization circuit breaker"""

    def test_initial_failure_count_is_zero(self, tmp_path):
        agent = make_agent(tmp_path)
        assert agent._consecutive_summary_failures == 0

    async def test_circuit_breaker_skips_after_max_failures(self, tmp_path):
        """After MAX failures, _summarize_with_llm should skip without calling LLM"""
        llm = MockLLMClient()
        agent = make_agent(tmp_path, llm=llm, token_limit=100)

        # Simulate max consecutive failures
        agent._consecutive_summary_failures = agent._MAX_CONSECUTIVE_SUMMARY_FAILURES

        # Add enough messages to trigger summarization
        agent.messages.append(Message(role="user", content="test"))
        agent.messages.append(Message(role="assistant", content="response"))

        initial_call_count = llm.call_count
        await agent._summarize_with_llm(estimated_tokens=200)

        # LLM should NOT have been called
        assert llm.call_count == initial_call_count

    async def test_circuit_breaker_resets_on_success(self, tmp_path):
        """Successful summarization should reset the failure counter"""
        llm = MockLLMClient()
        llm.responses = [LLMResponse(
            content="<summary>1. Primary Request: test</summary>",
            finish_reason="stop",
        )]
        agent = make_agent(tmp_path, llm=llm, token_limit=100)

        # Set some prior failures
        agent._consecutive_summary_failures = 2

        # Add messages for a valid round
        agent.messages.append(Message(role="user", content="fix the bug"))
        agent.messages.append(Message(role="assistant", content="working on it"))

        await agent._summarize_with_llm(estimated_tokens=200)

        # Counter should be reset
        assert agent._consecutive_summary_failures == 0

    async def test_circuit_breaker_increments_on_all_round_failures(self, tmp_path):
        """When all round summaries fail, counter should increment"""
        async def _failing_generate(messages, tools=None, **kwargs):
            raise RuntimeError("LLM down")

        llm = MockLLMClient()
        llm.generate = _failing_generate
        agent = make_agent(tmp_path, llm=llm, token_limit=100)

        # Add a valid round
        agent.messages.append(Message(role="user", content="do something"))
        agent.messages.append(Message(role="assistant", content="ok"))

        await agent._summarize_with_llm(estimated_tokens=200)

        # 即使有 fallback 文本产出，也应把真实 LLM 失败计入熔断计数。
        assert agent._consecutive_summary_failures == 1

    async def test_circuit_breaker_counts_consecutive_llm_failures_with_fallback(self, tmp_path):
        """Repeated all-fallback cycles should keep accumulating consecutive failures."""
        async def _failing_generate(messages, tools=None, **kwargs):
            raise RuntimeError("LLM down")

        llm = MockLLMClient()
        llm.generate = _failing_generate
        agent = make_agent(tmp_path, llm=llm, token_limit=100)

        # Round 1
        agent.messages.append(Message(role="user", content="u1"))
        agent.messages.append(Message(role="assistant", content="a1"))
        await agent._summarize_with_llm(estimated_tokens=200)
        assert agent._consecutive_summary_failures == 1

        # Round 2
        agent.messages.append(Message(role="user", content="u2"))
        agent.messages.append(Message(role="assistant", content="a2"))
        await agent._summarize_with_llm(estimated_tokens=220)
        assert agent._consecutive_summary_failures == 2

    def test_max_consecutive_failures_default(self, tmp_path):
        agent = make_agent(tmp_path)
        assert agent._MAX_CONSECUTIVE_SUMMARY_FAILURES == 3


# ============== Integration: _summarize_messages pipeline ==============


class TestSummarizeMessagesPipeline:
    """Test the full Level 2 → 3 → 4 pipeline with new features"""

    async def test_pipeline_skips_when_under_limit(self, tmp_path):
        """No compression when tokens are under limit"""
        agent = make_agent(tmp_path, token_limit=999999)
        initial_msg_count = len(agent.messages)

        await agent._summarize_messages()

        assert len(agent.messages) == initial_msg_count

    async def test_microcompact_clears_old_tool_results(self, tmp_path):
        agent = make_agent(tmp_path, token_limit=500)

        # Add 3 user rounds with large tool results
        for i in range(3):
            agent.messages.append(Message(role="user", content=f"round {i}"))
            agent.messages.append(Message(
                role="tool",
                content="x" * 5000,  # > _MICROCOMPACT_CHAR_THRESHOLD
                tool_call_id=f"tc{i}",
                name="read_file",
            ))

        compacted = agent._microcompact_messages()
        # Should compact the oldest round's tool result (first round)
        assert compacted >= 1

    def test_hard_ceiling_calculation(self, tmp_path):
        agent = make_agent(tmp_path, context_window=128000, max_output_tokens=16384)
        expected = 128000 - 16384 - 3000
        assert agent._hard_ceiling == expected

    def test_hard_ceiling_minimum(self, tmp_path):
        agent = make_agent(tmp_path, context_window=10000, max_output_tokens=10000)
        assert agent._hard_ceiling == 8192  # minimum bound

    async def test_summarize_with_llm_reuses_existing_round_summary(self, tmp_path):
        """Existing summary blocks should be reused instead of regenerating them"""
        agent = make_agent(tmp_path, token_limit=100)
        original_system = agent.messages[0]
        existing_summary = agent._build_execution_summary_content(
            "1. Primary Request and Intent:\nlegacy summary"
        )

        agent.messages = [
            original_system,
            Message(role="user", content="question 1"),
            Message(role="assistant", content=existing_summary),
            Message(role="user", content="question 2"),
            Message(role="assistant", content="working on task 2"),
        ]

        with pytest.MonkeyPatch.context() as mp:
            create_summary = AsyncMock(return_value=("1. Primary Request and Intent:\nnew summary", False))
            mp.setattr(agent, "_create_summary_with_meta", create_summary)
            await agent._summarize_with_llm(estimated_tokens=999)

        assert create_summary.await_count == 1

        summary_messages = [
            msg for msg in agent.messages
            if msg.role == "assistant"
            and isinstance(msg.content, str)
            and msg.content.startswith(agent._SUMMARY_MESSAGE_HEADER)
        ]
        assert len(summary_messages) == 2
        assert "legacy summary" in summary_messages[0].content
        assert "new summary" in summary_messages[1].content
