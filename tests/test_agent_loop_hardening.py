"""Agent loop 加固功能測試

覆蓋範圍：
- Phase 1: ModelConfig context_window 字段
- Phase 2: Tool result 預算截斷 (token_utils + max_result_tokens)
- Phase 3: Codex 风格上下文压缩与工具回环
- Phase 4: finish_reason 修復 + 多層退出檢查
- Review 修復: hard_ceiling 真實預算、failover 參數恢復、歷史消息輪次裁剪
"""
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.agent.agent import Agent, _ToolLoopGuard
from src.agent.context_compaction import SUMMARY_PREFIX
from src.agent.llm.tool_schema import tools_to_anthropic_schema, tools_to_openai_schema
from src.agent.schema import Message, LLMResponse, FunctionCall, ToolCall
from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.mcp_tool import McpRemoteTool
from src.agent.tools.memory_tools import (
    SearchMemoryTool,
    UpdateLongTermMemoryTool,
    UpdateUserProfileTool,
)
from src.agent.tools.sandbox_note_tool import SandboxRecallNoteTool, SandboxSessionNoteTool
from src.agent.utils.token_utils import count_text_tokens, truncate_text_by_tokens
from src.api.model_registry import ModelConfig
from tests.helpers import MockLLMClient, MockTool


# ============================================================
# Phase 1: ModelConfig context_window
# ============================================================


class TestModelConfigContextWindow:
    """驗證 context_window 字段的添加和校驗"""

    def _make_config(self, **overrides):
        defaults = dict(
            id="test-model",
            display_name="Test",
            provider="openai",
            api_base="https://test.api/v1",
            api_key="test-key",
            model_name="test",
        )
        defaults.update(overrides)
        return ModelConfig(**defaults)

    def test_default_context_window(self):
        cfg = self._make_config()
        assert cfg.context_window == 128000

    def test_custom_context_window(self):
        cfg = self._make_config(context_window=200000)
        assert cfg.context_window == 200000

    def test_context_window_in_public_dict(self):
        cfg = self._make_config(context_window=131072)
        d = cfg.to_public_dict()
        assert d["context_window"] == 131072

    def test_context_window_must_be_positive(self):
        with pytest.raises(ValueError, match="context_window"):
            self._make_config(context_window=0)

    def test_context_window_must_gt_max_tokens(self):
        with pytest.raises(ValueError, match="context_window"):
            self._make_config(max_tokens=32768, context_window=10000)

    def test_context_window_equals_max_tokens_rejected(self):
        """context_window == max_tokens 不允許"""
        with pytest.raises(ValueError, match="context_window"):
            self._make_config(max_tokens=16384, context_window=16384)

    def test_context_window_one_token_above_output_is_valid(self):
        cfg = self._make_config(max_tokens=16384, context_window=16385)
        assert cfg.compute_token_limit() == 1

    def test_context_window_with_minimum_real_input_budget_is_valid(self):
        cfg = self._make_config(
            max_tokens=16_384,
            context_window=16_384 + 3_000 + 8_192,
        )
        assert cfg.compute_token_limit() == int((cfg.context_window - cfg.max_tokens) * 0.8)


class TestCanonicalToolSchemaProjection:
    def test_shared_projection_matches_both_provider_wire_formats(self):
        tool = MockTool("schema_test")

        assert tools_to_anthropic_schema([tool]) == [tool.to_schema()]
        assert tools_to_openai_schema([tool]) == [tool.to_openai_schema()]


# ============================================================
# Phase 2: Token Utils + Tool max_result_tokens
# ============================================================


class TestTruncateTextByTokens:
    """驗證共享的 token 截斷工具函數"""

    def test_short_text_unchanged(self):
        text = "Hello world"
        result = truncate_text_by_tokens(text, 1000)
        assert result == text

    def test_long_text_truncated(self):
        text = "word " * 10000  # ~10000 tokens
        result = truncate_text_by_tokens(text, 100)
        assert len(result) < len(text)
        assert "Content truncated" in result

    def test_truncated_has_head_and_tail(self):
        # Build a text with recognizable head and tail
        text = "HEAD_MARKER " + ("filler " * 5000) + "TAIL_MARKER"
        result = truncate_text_by_tokens(text, 200)
        assert "HEAD_MARKER" in result
        assert "TAIL_MARKER" in result
        assert "Content truncated" in result

    @pytest.mark.parametrize("text", [
        "word " * 2000,
        "🙂🚀🧪✅" * 400 + "plain ascii tail\n" * 400,
        "这是中文执行证据。" * 600 + "ascii\n" * 600,
        "x" * 20000,
    ], ids=["ascii", "emoji_mixed", "cjk_mixed", "no_newline"])
    @pytest.mark.parametrize("max_tokens", [24, 25, 32, 48, 100, 178, 257, 1000])
    def test_never_exceeds_max_tokens_regardless_of_density(self, text, max_tokens):
        """精确按 token 切片，结果不得越界；装不下时返回空串"""
        result = truncate_text_by_tokens(text, max_tokens)
        assert count_text_tokens(result) <= max_tokens

    def test_returns_empty_when_note_alone_exceeds_budget(self):
        assert truncate_text_by_tokens("word " * 5000, 5) == ""

    @pytest.mark.parametrize("max_tokens", range(24, 300, 3))
    def test_never_splits_multi_token_characters(self, max_tokens):
        """按 token 切片不得切进 emoji/CJK 内部产生 U+FFFD"""
        text = "\U0001f642\U0001f680\U0001f9ea\u2705" * 300 + "这是中文证据。" * 200 + "tail\n" * 200
        assert "\ufffd" not in truncate_text_by_tokens(text, max_tokens)


class TestToolMaxResultTokens:
    """驗證 Tool 基類的 max_result_tokens 屬性"""

    def test_base_tool_default(self):
        tool = MockTool()
        assert tool.max_result_tokens == 8000

    def test_custom_override(self):
        class BigTool(MockTool):
            max_result_tokens = 32000
        tool = BigTool()
        assert tool.max_result_tokens == 32000


# ============================================================
# Phase 3: Microcompact + Emergency Truncate
# ============================================================


class TestAgentNewParams:
    """驗證 Agent 構造函數的新參數"""

    def test_default_context_window(self, tmp_path):
        llm = MockLLMClient()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
        )
        assert agent.context_window == 128000
        assert agent.max_output_tokens == 16384

    def test_custom_context_window(self, tmp_path):
        llm = MockLLMClient()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            context_window=200000,
            max_output_tokens=32768,
        )
        assert agent.context_window == 200000
        assert agent.max_output_tokens == 32768


class TestFinishReasonParsing:
    """驗證 OpenAI client _parse_response 不再硬編碼 finish_reason"""

    def test_length_finish_reason_preserved(self):
        from src.agent.llm.openai_client import OpenAIClient

        client = OpenAIClient(
            api_key="test",
            api_base="https://test.api/v1",
            model="test-model",
        )

        # 構造一個模擬的 OpenAI response
        mock_message = MagicMock()
        mock_message.content = "partial output..."
        mock_message.reasoning_content = None
        mock_message.reasoning_details = None
        mock_message.tool_calls = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "length"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = None

        result = client._parse_response(mock_response)
        assert result.finish_reason == "length"

    def test_stop_finish_reason(self):
        from src.agent.llm.openai_client import OpenAIClient

        client = OpenAIClient(
            api_key="test",
            api_base="https://test.api/v1",
            model="test-model",
        )

        mock_message = MagicMock()
        mock_message.content = "complete output"
        mock_message.reasoning_content = None
        mock_message.reasoning_details = None
        mock_message.tool_calls = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = None

        result = client._parse_response(mock_response)
        assert result.finish_reason == "stop"


# ============================================================
# Review 修復: hard_ceiling 下界保護
# ============================================================


class TestFailoverParamRestore:
    """驗證 failover 後 context_window/max_output_tokens 恢復主模型值"""

    def test_primary_params_preserved_in_closure(self, tmp_path):
        """run_agui 內部應保存主模型參數，以便 failover 後恢復"""
        llm = MockLLMClient()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            context_window=128000,
            max_output_tokens=16384,
        )
        # 模擬 failover 覆蓋後手動恢復（單元測試無法直接走 run_agui 的完整流程，
        # 但可以驗證屬性可被覆蓋和恢復）
        primary_cw = agent.context_window
        primary_mot = agent.max_output_tokens

        # 模擬 failover 覆蓋
        agent.context_window = 64000
        agent.max_output_tokens = 8192

        assert agent.context_window == 64000

        # 模擬恢復
        agent.context_window = primary_cw
        agent.max_output_tokens = primary_mot

        assert agent.context_window == 128000
        assert agent.max_output_tokens == 16384

    @pytest.mark.asyncio
    async def test_failover_params_restored_on_cancel(self, tmp_path):
        """驗證 failover + 取消路徑下 context_window/max_output_tokens 仍恢復主模型值。

        模擬：failover 回調將參數改為 fallback 值 → cancel_token 觸發 → return 退出
        → try/finally 應保證恢復。
        """
        import asyncio
        from src.agent.agent import Agent

        cancel_token = asyncio.Event()
        failover_called = asyncio.Event()

        # LLM generate_stream 會在 failover 回調後阻塞直到被取消
        class HangingLLM(MockLLMClient):
            failover_notify = None

            async def generate_stream(self, messages, tools=None,
                                      on_content=None, on_thinking=None, **kw):
                # 模擬 failover 回調（在真實場景中由 LLMClient._call_with_retry 觸發）
                if self.failover_notify:
                    config = SimpleNamespace(
                        id="fallback-model",
                        context_window=64000,
                        max_tokens=8192,
                        auto_compact_token_limit=None,
                    )
                    await self.failover_notify(config, self, "generate_stream", {
                        "messages": messages,
                        "tools": tools,
                    })
                failover_called.set()
                # 然後掛起，等待取消
                await asyncio.sleep(999)

        llm = HangingLLM()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            context_window=128000,
            max_output_tokens=16384,
        )
        agent.messages.append(Message(role="user", content="hello"))

        # 後台任務：等 failover 回調執行後再觸發取消
        async def delayed_cancel():
            await failover_called.wait()
            # 此時 agent 參數已被 failover 覆蓋
            assert agent.context_window == 64000
            assert agent.max_output_tokens == 8192
            cancel_token.set()

        cancel_task = asyncio.create_task(delayed_cancel())

        # 消費所有事件直到結束
        events = []
        async for event in agent.run_agui(
            thread_id="t1", run_id="r1", cancel_token=cancel_token,
        ):
            events.append(event)

        await cancel_task

        # 核心斷言：取消退出後參數應恢復為主模型值
        assert agent.context_window == 128000
        assert agent.max_output_tokens == 16384

    @pytest.mark.asyncio
    async def test_failover_smaller_window_compacts_and_rebuilds_request(self, tmp_path):
        class RejectingFailoverLLM(MockLLMClient):
            failover_notify = None
            prepared = None

            async def generate_stream(self, messages, tools=None, **_kwargs):
                assert self.failover_notify is not None
                config = SimpleNamespace(
                    id="small-fallback",
                    context_window=2_000,
                    max_tokens=500,
                    auto_compact_token_limit=None,
                )
                self.prepared = await self.failover_notify(
                    config,
                    self,
                    "generate_stream",
                    {"messages": messages, "tools": tools},
                )
                return LLMResponse(content="ok", finish_reason="stop")

            async def generate(self, messages, tools=None):
                return LLMResponse(content="summary", finish_reason="stop")

        llm = RejectingFailoverLLM()
        agent = Agent(
            llm_client=llm,
            system_prompt="system",
            tools=[],
            max_steps=1,
            workspace_dir=str(tmp_path / "ws"),
            context_window=128_000,
            max_output_tokens=16_384,
        )
        agent.messages.append(Message(role="user", content="history " * 7_500))

        events = []
        async for event in agent.run_agui(thread_id="t1", run_id="r1"):
            events.append(event)
        assert llm.prepared is not None
        assert any(
            message.role == "user" and message.is_synthetic
            for message in llm.prepared["messages"]
        )

        assert not any(getattr(event, "type", None) == "RUN_ERROR" for event in events)
        assert agent.context_window == 128_000
        assert agent.max_output_tokens == 16_384

    @pytest.mark.asyncio
    async def test_smaller_fallback_retries_compaction_then_really_calls_provider(self, tmp_path):
        class PrimaryCompactor:
            async def generate(self, messages, **_kwargs):
                raise RuntimeError("invalid request: retry exhausted")

        class SmallFallback:
            def __init__(self):
                self.compaction_sizes = []
                self.ordinary_requests = []

            async def generate(self, messages, **_kwargs):
                self.compaction_sizes.append(len(messages))
                if len(self.compaction_sizes) <= 2:
                    raise RuntimeError("context_length_exceeded")
                return LLMResponse(content="fallback handoff", finish_reason="stop")

            async def generate_stream(self, messages, tools=None, **_kwargs):
                self.ordinary_requests.append(messages)
                return LLMResponse(content="fallback provider called", finish_reason="stop")

        class OneShotFailover:
            failover_notify = None

            def __init__(self):
                self._client = PrimaryCompactor()
                self.target = SmallFallback()
                self.last_request_snapshot = None

            async def generate_stream(self, messages, tools=None, **_kwargs):
                prepared = await self.failover_notify(
                    SimpleNamespace(
                        id="small-fallback",
                        context_window=1_000,
                        max_tokens=100,
                        auto_compact_token_limit=None,
                    ),
                    self.target,
                    "generate_stream",
                    {"messages": messages, "tools": tools},
                )
                return await self.target.generate_stream(**prepared)

        llm = OneShotFailover()
        agent = Agent(
            llm_client=llm,
            system_prompt="system",
            tools=[],
            max_steps=1,
            workspace_dir=str(tmp_path / "ws"),
            context_window=128_000,
            max_output_tokens=16_384,
        )
        agent.messages.extend([
            Message(role="user", content="old-one " * 2_000),
            Message(role="assistant", content="old-answer " * 2_000),
            Message(role="user", content="latest " * 2_000),
        ])

        events = [event async for event in agent.run_agui("thread", "run")]

        assert llm.target.compaction_sizes[1] == llm.target.compaction_sizes[0] - 1
        assert llm.target.compaction_sizes[2] == llm.target.compaction_sizes[1] - 1
        assert len(llm.target.ordinary_requests) == 1
        assert any(message.is_synthetic for message in llm.target.ordinary_requests[0])
        assert events[-1].type.value == "RUN_FINISHED"


# ============================================================
# Review 修復: 歷史消息輪次邊界裁剪
# ============================================================


class TestSyntheticMessageMarking:
    """驗證 Message.is_synthetic 字段"""

    def test_default_is_false(self):
        msg = Message(role="user", content="hello")
        assert msg.is_synthetic is False

    def test_explicit_true(self):
        msg = Message(role="user", content="nudge", is_synthetic=True)
        assert msg.is_synthetic is True

    def test_serialization_roundtrip(self):
        """is_synthetic 在 model_dump/model_validate 中保留"""
        msg = Message(role="user", content="test", is_synthetic=True)
        data = msg.model_dump()
        restored = Message.model_validate(data)
        assert restored.is_synthetic is True


class TestTruncationRetryEmitsCustomEvent:
    """驗證 output truncation retry 注入 is_synthetic 消息並產出 CUSTOM 事件"""

    @pytest.mark.asyncio
    async def test_truncation_retry_synthetic_and_custom_event(self, tmp_path):
        llm = MockLLMClient()
        # 第一次返回 length（被截斷），第二次正常結束
        llm.responses = [
            LLMResponse(content="partial...", finish_reason="length"),
            LLMResponse(content="done", finish_reason="stop"),
        ]
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            context_window=128000,
            max_output_tokens=16384,
        )
        agent.messages.append(Message(role="user", content="write a long essay"))

        events = []
        async for event in agent.run_agui(thread_id="t1", run_id="r1"):
            events.append(event)

        event_types = [e.type.value for e in events]
        # 應有 CUSTOM 事件
        assert "CUSTOM" in event_types
        custom_events = [e for e in events if e.type.value == "CUSTOM"]
        assert any(
            getattr(e, "name", "") == "synthetic_user_message" for e in custom_events
        )
        # 注入的消息應標記 is_synthetic
        synthetic_msgs = [m for m in agent.messages if getattr(m, "is_synthetic", False)]
        assert len(synthetic_msgs) >= 1
        assert "truncated" in synthetic_msgs[0].content.lower()


class TestEmptyNudgeEmitsCustomEvent:
    """驗證 empty response nudge 注入 is_synthetic 消息並產出 CUSTOM 事件"""

    @pytest.mark.asyncio
    async def test_empty_nudge_synthetic_and_custom_event(self, tmp_path):
        llm = MockLLMClient()
        # 第一次返回空（觸發 nudge），第二次正常
        llm.responses = [
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="here is your answer", finish_reason="stop"),
        ]
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            context_window=128000,
            max_output_tokens=16384,
        )
        agent.messages.append(Message(role="user", content="hello"))

        events = []
        async for event in agent.run_agui(thread_id="t1", run_id="r1"):
            events.append(event)

        event_types = [e.type.value for e in events]
        assert "CUSTOM" in event_types
        custom_events = [e for e in events if e.type.value == "CUSTOM"]
        assert any(
            getattr(e, "name", "") == "synthetic_user_message" for e in custom_events
        )
        # 注入的消息應標記 is_synthetic
        synthetic_msgs = [m for m in agent.messages if getattr(m, "is_synthetic", False)]
        assert len(synthetic_msgs) >= 1
        assert "empty response" in synthetic_msgs[0].content.lower()


# ============================================================
# Codex-style current-turn continuity + runtime loop guard
# ============================================================


class _MarkerTool(MockTool):
    def __init__(self, name="marker_tool", *, marker="TOOL_OK", repeat_policy="standard"):
        super().__init__(name)
        self.marker = marker
        self.repeat_policy = repeat_policy

    async def execute(self, **kwargs) -> ToolResult:
        self.execute_count += 1
        self.last_args = kwargs
        return ToolResult(success=True, content=self.marker)


class TestCurrentTurnToolContinuity:
    @pytest.mark.asyncio
    async def test_successful_tool_result_is_visible_to_next_provider_request(self, tmp_path):
        tool = _MarkerTool(marker="WRITE_OK:/workspace/valuation.md")

        class RecordingLLM:
            def __init__(self):
                self.requests = []
                self.last_request_snapshot = None

            async def generate_stream(self, messages, tools, **_kwargs):
                self.requests.append([message.model_copy(deep=True) for message in messages])
                if len(self.requests) == 1:
                    return LLMResponse(
                        content="",
                        finish_reason="tool_calls",
                        tool_calls=[ToolCall(
                            id="write-once",
                            type="function",
                            function=FunctionCall(
                                name=tool.name,
                                arguments={"param1": "valuation.md"},
                            ),
                        )],
                    )
                return LLMResponse(content="done", finish_reason="stop")

        llm = RecordingLLM()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[tool],
            workspace_dir=str(tmp_path / "ws"),
            max_steps=5,
        )
        agent.messages.append(Message(role="user", content="write valuation.md"))

        events = [event async for event in agent.run_agui("thread", "run")]

        assert tool.execute_count == 1
        assert len(llm.requests) == 2
        second = llm.requests[1]
        assistant_calls = [
            message
            for message in second
            if message.role == "assistant" and message.tool_calls
        ]
        tool_results = [message for message in second if message.role == "tool"]
        assert assistant_calls[-1].tool_calls[0].id == "write-once"
        assert tool_results[-1].tool_call_id == "write-once"
        assert "WRITE_OK:/workspace/valuation.md" in str(tool_results[-1].content)
        assert events[-1].type.value == "RUN_FINISHED"

    @pytest.mark.asyncio
    async def test_mid_turn_compaction_summarizes_and_retains_complete_tool_pair(self, tmp_path):
        marker = "MIDTURN_TOOL_OK:/workspace/valuation.md"
        tool = _MarkerTool(marker=marker + "\n" + "evidence " * 2_500)

        class CompactingLLM:
            def __init__(self):
                self.stream_requests = []
                self.summary_requests = []
                self.last_request_snapshot = None

            async def generate(self, messages, **_kwargs):
                self.summary_requests.append([
                    message.model_copy(deep=True) for message in messages
                ])
                return LLMResponse(
                    content="Tool write succeeded; continue from valuation.md",
                    finish_reason="stop",
                )

            async def generate_stream(self, messages, tools, **_kwargs):
                self.stream_requests.append([
                    message.model_copy(deep=True) for message in messages
                ])
                if len(self.stream_requests) == 1:
                    agent.token_limit = 1
                    return LLMResponse(
                        content="",
                        finish_reason="tool_calls",
                        tool_calls=[ToolCall(
                            id="midturn-write",
                            type="function",
                            function=FunctionCall(
                                name=tool.name,
                                arguments={"param1": "valuation.md"},
                            ),
                        )],
                    )
                return LLMResponse(content="edited successfully", finish_reason="stop")

        llm = CompactingLLM()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[tool],
            workspace_dir=str(tmp_path / "ws"),
            token_limit=100_000,
            context_window=30_000,
            max_output_tokens=2_000,
            max_steps=5,
        )
        agent.messages.append(Message(role="user", content="build a valuation file"))

        events = [event async for event in agent.run_agui("thread", "run")]

        assert events[-1].type.value == "RUN_FINISHED"
        assert tool.execute_count == 1
        assert len(llm.summary_requests) == 1
        import json
        summary_input = json.dumps(
            [message.model_dump(exclude_none=True) for message in llm.summary_requests[0]],
            ensure_ascii=False,
        )
        assert "midturn-write" in summary_input
        assert marker in summary_input

        assert len(llm.stream_requests) == 2
        continuation = llm.stream_requests[1]
        assert any(
            message.role == "user"
            and message.is_synthetic
            and str(message.content).startswith(SUMMARY_PREFIX)
            for message in continuation
        )
        assert not any(message.role == "tool" for message in continuation)

    @pytest.mark.asyncio
    async def test_mid_turn_compaction_preserves_complete_multi_tool_batch(self, tmp_path):
        first = _MarkerTool(
            name="read_file",
            marker="FIRST_BIG_RESULT\n" + "large evidence " * 2_500,
            repeat_policy="read_only",
        )
        second = _MarkerTool(
            name="find_path",
            marker="SECOND_RESULT:/workspace/valuation.md",
            repeat_policy="read_only",
        )

        class MultiToolCompactingLLM:
            def __init__(self):
                self.stream_requests = []
                self.summary_requests = []
                self.last_request_snapshot = None

            async def generate(self, messages, **_kwargs):
                self.summary_requests.append([
                    message.model_copy(deep=True) for message in messages
                ])
                return LLMResponse(
                    content="Both tool results are available",
                    finish_reason="stop",
                )

            async def generate_stream(self, messages, tools, **_kwargs):
                self.stream_requests.append([
                    message.model_copy(deep=True) for message in messages
                ])
                if len(self.stream_requests) == 1:
                    agent.token_limit = 1
                    return LLMResponse(
                        content="",
                        finish_reason="tool_calls",
                        tool_calls=[
                            ToolCall(
                                id="multi-read",
                                type="function",
                                function=FunctionCall(
                                    name=first.name,
                                    arguments={"param1": "valuation.md"},
                                ),
                            ),
                            ToolCall(
                                id="multi-find",
                                type="function",
                                function=FunctionCall(
                                    name=second.name,
                                    arguments={"param1": "valuation.md"},
                                ),
                            ),
                        ],
                    )
                return LLMResponse(content="done", finish_reason="stop")

        llm = MultiToolCompactingLLM()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[first, second],
            workspace_dir=str(tmp_path / "ws"),
            token_limit=100_000,
            context_window=30_000,
            max_output_tokens=2_000,
            max_steps=5,
        )
        agent.messages.append(Message(role="user", content="inspect valuation file"))

        events = [event async for event in agent.run_agui("thread", "run")]

        assert events[-1].type.value == "RUN_FINISHED"
        assert first.execute_count == second.execute_count == 1
        assert len(llm.summary_requests) == 1
        import json
        compact_prompt = json.dumps(
            [message.model_dump(exclude_none=True) for message in llm.summary_requests[0]],
            ensure_ascii=False,
        )
        for marker in ("multi-read", "multi-find", "FIRST_BIG_RESULT", "SECOND_RESULT"):
            assert marker in compact_prompt

        continuation = llm.stream_requests[1]
        assert not any(message.role == "tool" for message in continuation)
        assert any(
            message.role == "user" and message.is_synthetic
            for message in continuation
        )

    @pytest.mark.asyncio
    async def test_request_is_sent_without_structural_local_preflight(self, tmp_path):
        class RuntimeExpandingTool(_MarkerTool):
            async def execute(inner_self, **kwargs) -> ToolResult:
                result = await super().execute(**kwargs)
                agent._build_runtime_context_block = lambda: "runtime " * 11_000
                return result

        tool = RuntimeExpandingTool(marker="FIRST_CALL_SUCCEEDED")

        class OneCallLLM:
            def __init__(self):
                self.calls = 0
                self.last_request_snapshot = None

            async def generate_stream(self, messages, tools, **_kwargs):
                self.calls += 1
                if self.calls > 1:
                    return LLMResponse(content="provider accepted", finish_reason="stop")
                return LLMResponse(
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[ToolCall(
                        id="expand-runtime",
                        type="function",
                        function=FunctionCall(
                            name=tool.name,
                            arguments={"param1": "once"},
                        ),
                    )],
                )

        llm = OneCallLLM()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[tool],
            workspace_dir=str(tmp_path / "ws"),
            max_steps=5,
        )
        agent.messages.append(Message(role="user", content="run once"))

        events = [event async for event in agent.run_agui("thread", "run")]

        assert llm.calls == 2
        assert tool.execute_count == 1
        assert events[-1].type.value == "RUN_FINISHED"

    def test_linux_workspace_is_not_rendered_with_windows_separators(self):
        agent = Agent(
            llm_client=MockLLMClient(),
            system_prompt="test",
            tools=[],
            workspace_dir="/home/user/sessions/session-1",
        )

        runtime = agent._build_runtime_context_block()

        assert "/home/user/sessions/session-1" in runtime
        assert "\\home\\user\\sessions" not in runtime


class TestRuntimeToolLoopGuard:
    def test_remote_annotations_can_tighten_but_not_relax_repeat_policy(self):
        remote = object.__new__(McpRemoteTool)

        remote._snapshot = SimpleNamespace(annotations={"readOnlyHint": True})
        assert remote.repeat_policy_for({}) == "standard"

        remote._snapshot = SimpleNamespace(
            annotations={"readOnlyHint": True, "destructiveHint": True}
        )
        assert remote.repeat_policy_for({}) == "mutating"

        remote._snapshot = SimpleNamespace(annotations={"readOnlyHint": "true"})
        assert remote.repeat_policy_for({}) == "standard"

    def test_memory_and_note_tools_declare_invocation_aware_policies(self):
        long_term = object.__new__(UpdateLongTermMemoryTool)
        user_profile = object.__new__(UpdateUserProfileTool)

        for tool in (long_term, user_profile):
            assert tool.repeat_policy_for({"mode": "read"}) == "read_only"
            assert tool.repeat_policy_for({"mode": "append"}) == "mutating"
            assert tool.repeat_policy_for({"mode": "write"}) == "mutating"
        assert SearchMemoryTool.repeat_policy == "read_only"
        assert SandboxSessionNoteTool.repeat_policy == "mutating"
        assert SandboxRecallNoteTool.repeat_policy == "read_only"

    def test_bash_text_is_never_promoted_to_read_only_by_prefix(self):
        tool = MockTool("bash")

        for command in (
            "find . -delete",
            "ls .; rm -rf ./target",
            "git status",
        ):
            assert _ToolLoopGuard._policy(tool, "bash", {"command": command}) == "standard"

    def test_uncertain_retry_policy_distinguishes_unknown_reads_and_polling(self):
        uncertain = ToolResult(
            success=False,
            error="timeout",
            outcome_uncertain=True,
        )

        standard_tool = _MarkerTool(name="remote_tool", repeat_policy="standard")
        standard_guard = _ToolLoopGuard()
        standard_fp, standard_policy = standard_guard._fingerprint(
            standard_tool,
            standard_tool.name,
            {"param1": "same"},
        )
        standard_guard.observe(
            fingerprint=standard_fp,
            policy=standard_policy,
            tool_name=standard_tool.name,
            result=uncertain,
            result_content="timeout",
        )
        assert standard_guard.check(
            tool=standard_tool,
            tool_name=standard_tool.name,
            arguments={"param1": "same"},
        )[2] is not None

        for repeat_policy in ("read_only", "polling"):
            tool = _MarkerTool(name=f"{repeat_policy}_tool", repeat_policy=repeat_policy)
            guard = _ToolLoopGuard()
            fingerprint, policy = guard._fingerprint(tool, tool.name, {"param1": "same"})
            guard.observe(
                fingerprint=fingerprint,
                policy=policy,
                tool_name=tool.name,
                result=uncertain,
                result_content="timeout",
            )
            assert guard.check(
                tool=tool,
                tool_name=tool.name,
                arguments={"param1": "same"},
            )[2] is None
            guard.observe(
                fingerprint=fingerprint,
                policy=policy,
                tool_name=tool.name,
                result=uncertain,
                result_content="timeout",
            )
            assert guard.check(
                tool=tool,
                tool_name=tool.name,
                arguments={"param1": "same"},
            )[2] is not None

    def test_uncertain_file_write_requires_same_path_read_before_retry(self):
        write_tool = _MarkerTool(
            name="write_file",
            marker="written",
            repeat_policy="mutating",
        )
        read_tool = _MarkerTool(
            name="read_file",
            marker="read",
            repeat_policy="read_only",
        )
        guard = _ToolLoopGuard(workspace_dir="/home/user/session")
        uncertain = ToolResult(
            success=False,
            error="lost response",
            outcome_uncertain=True,
        )
        write_args = {"path": "result.md", "content": "first"}
        write_fp, write_policy = guard._fingerprint(
            write_tool,
            write_tool.name,
            write_args,
        )
        guard.observe(
            fingerprint=write_fp,
            policy=write_policy,
            tool_name=write_tool.name,
            result=uncertain,
            result_content="uncertain",
            arguments=write_args,
        )

        changed_write_args = {"path": "result.md", "content": "second"}
        assert guard.check(
            tool=write_tool,
            tool_name=write_tool.name,
            arguments=changed_write_args,
        )[2] is not None

        other_read_args = {"path": "other.md"}
        other_read_fp, other_read_policy = guard._fingerprint(
            read_tool,
            read_tool.name,
            other_read_args,
        )
        guard.observe(
            fingerprint=other_read_fp,
            policy=other_read_policy,
            tool_name=read_tool.name,
            result=ToolResult(success=True, content="other"),
            result_content="other",
            arguments=other_read_args,
        )
        assert guard.check(
            tool=write_tool,
            tool_name=write_tool.name,
            arguments=changed_write_args,
        )[2] is not None

        same_read_args = {"path": "/home/user/session/result.md"}
        same_read_fp, same_read_policy = guard._fingerprint(
            read_tool,
            read_tool.name,
            same_read_args,
        )
        guard.observe(
            fingerprint=same_read_fp,
            policy=same_read_policy,
            tool_name=read_tool.name,
            result=ToolResult(success=True, content="verified"),
            result_content="verified",
            arguments=same_read_args,
        )
        assert guard.check(
            tool=write_tool,
            tool_name=write_tool.name,
            arguments=changed_write_args,
        )[2] is None

    def test_missing_file_read_verifies_uncertain_write_did_not_land(self):
        write_tool = _MarkerTool(
            name="write_file",
            marker="created",
            repeat_policy="mutating",
        )
        read_tool = _MarkerTool(
            name="read_file",
            marker="read",
            repeat_policy="read_only",
        )
        guard = _ToolLoopGuard(workspace_dir="/home/user/session")
        write_args = {"path": "result.md", "content": "first"}
        write_fp, write_policy = guard._fingerprint(
            write_tool,
            write_tool.name,
            write_args,
        )
        guard.observe(
            fingerprint=write_fp,
            policy=write_policy,
            tool_name=write_tool.name,
            result=ToolResult(
                success=False,
                error="lost response",
                outcome_uncertain=True,
            ),
            result_content="uncertain",
            arguments=write_args,
        )

        read_args = {"path": "/home/user/session/result.md"}
        read_fp, read_policy = guard._fingerprint(
            read_tool,
            read_tool.name,
            read_args,
        )
        guard.observe(
            fingerprint=read_fp,
            policy=read_policy,
            tool_name=read_tool.name,
            result=ToolResult(
                success=False,
                error="File not found: /home/user/session/result.md",
            ),
            result_content="File not found: /home/user/session/result.md",
            arguments=read_args,
        )

        assert guard.check(
            tool=write_tool,
            tool_name=write_tool.name,
            arguments=write_args,
        )[2] is None

    def test_missing_file_read_does_not_clear_unrelated_recoveries(self):
        write_tool = _MarkerTool(
            name="write_file",
            marker="written",
            repeat_policy="mutating",
        )
        read_tool = _MarkerTool(
            name="read_file",
            marker="read",
            repeat_policy="read_only",
        )
        guard = _ToolLoopGuard(workspace_dir="/home/user/session")
        uncertain = ToolResult(
            success=False,
            error="lost response",
            outcome_uncertain=True,
        )
        for path in ("alpha.md", "beta.md"):
            args = {"path": path, "content": "first"}
            fingerprint, policy = guard._fingerprint(write_tool, write_tool.name, args)
            guard.observe(
                fingerprint=fingerprint,
                policy=policy,
                tool_name=write_tool.name,
                result=uncertain,
                result_content="uncertain",
                arguments=args,
            )

        beta_retry = {"path": "beta.md", "content": "second"}
        assert guard.check(
            tool=write_tool,
            tool_name=write_tool.name,
            arguments=beta_retry,
        )[2] is not None

        alpha_read = {"path": "/home/user/session/alpha.md"}
        read_fp, read_policy = guard._fingerprint(read_tool, read_tool.name, alpha_read)
        guard.observe(
            fingerprint=read_fp,
            policy=read_policy,
            tool_name=read_tool.name,
            result=ToolResult(
                success=False,
                error="File not found: /home/user/session/alpha.md",
            ),
            result_content="File not found: /home/user/session/alpha.md",
            arguments=alpha_read,
        )

        assert guard.check(
            tool=write_tool,
            tool_name=write_tool.name,
            arguments={"path": "alpha.md", "content": "second"},
        )[2] is None
        assert guard.check(
            tool=write_tool,
            tool_name=write_tool.name,
            arguments=beta_retry,
        )[2] is not None

    def test_relative_and_absolute_workspace_paths_share_fingerprint(self):
        tool = _MarkerTool(
            name="write_file",
            marker="ok",
            repeat_policy="mutating",
        )
        guard = _ToolLoopGuard(workspace_dir="/home/user/sessions/s1")

        relative, _ = guard._fingerprint(
            tool,
            tool.name,
            {"path": "valuation.md", "content": "same"},
        )
        absolute, _ = guard._fingerprint(
            tool,
            tool.name,
            {"path": "/home/user/sessions/s1/valuation.md", "content": "same"},
        )
        backslash, _ = guard._fingerprint(
            tool,
            tool.name,
            {"path": "\\home\\user\\sessions\\s1\\valuation.md", "content": "same"},
        )

        assert relative == absolute == backslash

    @pytest.mark.asyncio
    async def test_identical_read_result_executes_twice_then_guard_terminates(self, tmp_path):
        tool = _MarkerTool(
            name="read_file",
            marker="READ_OK:WACC=9%",
            repeat_policy="read_only",
        )

        class RepeatingLLM:
            def __init__(self):
                self.calls = 0
                self.last_request_snapshot = None

            async def generate_stream(self, messages, tools, **_kwargs):
                self.calls += 1
                return LLMResponse(
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[ToolCall(
                        id=f"read-{self.calls}",
                        type="function",
                        function=FunctionCall(
                            name=tool.name,
                            arguments={"param1": "valuation.md"},
                        ),
                    )],
                )

        llm = RepeatingLLM()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[tool],
            workspace_dir=str(tmp_path / "ws"),
            max_steps=10,
        )
        agent.messages.append(Message(role="user", content="read valuation.md"))

        events = [event async for event in agent.run_agui("thread", "run")]

        assert tool.execute_count == 2
        assert llm.calls == 4
        assert events[-1].type.value == "RUN_ERROR"
        assert "tool_loop_detected" in str(getattr(events[-1], "message", ""))
        agent._validate_complete_tool_pairs(agent.messages)

    @pytest.mark.asyncio
    async def test_identical_mutation_is_exactly_once_with_recovery_then_error(self, tmp_path):
        tool = _MarkerTool(
            name="write_file",
            marker="WRITE_OK:/workspace/valuation.md",
            repeat_policy="mutating",
        )

        class RepeatingWriteLLM:
            def __init__(self):
                self.calls = 0
                self.last_request_snapshot = None

            async def generate_stream(self, messages, tools, **_kwargs):
                self.calls += 1
                return LLMResponse(
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[ToolCall(
                        id=f"write-{self.calls}",
                        type="function",
                        function=FunctionCall(
                            name=tool.name,
                            arguments={"param1": "same-content"},
                        ),
                    )],
                )

        llm = RepeatingWriteLLM()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[tool],
            workspace_dir=str(tmp_path / "ws"),
            max_steps=10,
        )
        agent.messages.append(Message(role="user", content="write once"))

        events = [event async for event in agent.run_agui("thread", "run")]

        assert tool.execute_count == 1
        assert llm.calls == 3
        assert events[-1].type.value == "RUN_ERROR"
        agent._validate_complete_tool_pairs(agent.messages)

    @pytest.mark.asyncio
    async def test_read_search_cycle_observes_both_results_then_blocks_next_member(self, tmp_path):
        read_tool = _MarkerTool(
            name="read_file",
            marker="READ_OK:same-state",
            repeat_policy="read_only",
        )
        find_tool = _MarkerTool(
            name="find_path",
            marker="FOUND:/workspace/valuation.md",
            repeat_policy="read_only",
        )

        class AlternatingLLM:
            def __init__(self):
                self.calls = 0
                self.last_request_snapshot = None

            async def generate_stream(self, messages, tools, **_kwargs):
                self.calls += 1
                selected = read_tool if self.calls % 2 else find_tool
                return LLMResponse(
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[ToolCall(
                        id=f"alternating-{self.calls}",
                        type="function",
                        function=FunctionCall(
                            name=selected.name,
                            arguments={"param1": "valuation.md"},
                        ),
                    )],
                )

        llm = AlternatingLLM()
        agent = Agent(
            llm_client=llm,
            system_prompt="test",
            tools=[read_tool, find_tool],
            workspace_dir=str(tmp_path / "ws"),
            max_steps=10,
        )
        agent.messages.append(Message(role="user", content="locate then read the file"))

        events = [event async for event in agent.run_agui("thread", "run")]

        assert read_tool.execute_count == 2
        assert find_tool.execute_count == 2
        assert llm.calls == 6
        assert events[-1].type.value == "RUN_ERROR"
        agent._validate_complete_tool_pairs(agent.messages)
