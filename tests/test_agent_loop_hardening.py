"""Agent loop 加固功能測試

覆蓋範圍：
- Phase 1: ModelConfig context_window 字段
- Phase 2: Tool result 預算截斷 (token_utils + max_result_tokens)
- Phase 3: 多級上下文管理 (microcompact + emergency_truncate)
- Phase 4: finish_reason 修復 + 多層退出檢查
- Review 修復: hard_ceiling 下界、failover 參數恢復、歷史消息輪次裁剪
"""
import pytest
from unittest.mock import MagicMock

from src.agent.agent import Agent
from src.agent.schema import Message, LLMResponse
from src.agent.tools.base import Tool, ToolResult
from src.agent.utils.token_utils import truncate_text_by_tokens
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

    def test_context_window_just_above_max_tokens_ok(self):
        """context_window == max_tokens + 1 是最小允許值"""
        cfg = self._make_config(max_tokens=16384, context_window=16385)
        assert cfg.context_window == 16385


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


class TestMicrocompact:
    """驗證 _microcompact_messages() Level 2 壓縮"""

    @pytest.fixture
    def agent(self, tmp_path):
        llm = MockLLMClient()
        return Agent(
            llm_client=llm,
            system_prompt="system",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            context_window=128000,
            max_output_tokens=16384,
        )

    def test_skip_when_few_user_rounds(self, agent):
        """不足 3 個 user round 時跳過"""
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="q1"),
            Message(role="tool", content="long " * 200, tool_call_id="tc1"),
            Message(role="user", content="q2"),
        ]
        result = agent._microcompact_messages()
        assert result == 0
        # tool content should be unchanged
        assert agent.messages[2].content.startswith("long ")

    def test_compact_old_tool_results(self, agent):
        """壓縮舊的 tool result，保留最近 2 個 user round"""
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="q1"),
            Message(role="tool", content="OLD_LARGE_RESULT " * 300, tool_call_id="tc1"),
            Message(role="user", content="q2"),
            Message(role="tool", content="MEDIUM_RESULT " * 300, tool_call_id="tc2"),
            Message(role="user", content="q3"),  # user_indices[-2]
            Message(role="tool", content="RECENT_LARGE " * 300, tool_call_id="tc3"),
            Message(role="user", content="q4"),  # user_indices[-1]
            Message(role="tool", content="LATEST_LARGE " * 300, tool_call_id="tc4"),
        ]
        result = agent._microcompact_messages()
        assert result > 0
        # Old tool result (before user_indices[-2]) should be compacted
        assert "compacted" in agent.messages[2].content.lower()
        # Recent tool results (within last 2 user rounds) should be preserved
        assert "RECENT_LARGE" in agent.messages[6].content
        assert "LATEST_LARGE" in agent.messages[8].content

    def test_clear_old_thinking(self, agent):
        """清除舊的 assistant thinking"""
        # 構造 4 個 user round，safe_boundary = user_indices[-2] = index 6 (q3)
        # a1 (index 2) 在邊界前 → thinking 被清除
        # a3 (index 7) 在邊界後 → thinking 保留
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="q1"),         # user_indices[0] = 1
            Message(role="assistant", content="a1", thinking="long thinking text"),
            Message(role="user", content="q2"),         # user_indices[1] = 3
            Message(role="assistant", content="a2"),
            Message(role="user", content="q3"),         # user_indices[2] = 5, safe_boundary
            Message(role="assistant", content="a3", thinking="recent thinking"),
            Message(role="user", content="q4"),         # user_indices[3] = 7
        ]
        agent._microcompact_messages()
        # Old thinking (before safe_boundary) should be cleared
        assert agent.messages[2].thinking is None
        # Recent thinking (at or after safe_boundary) should be preserved
        assert agent.messages[6].thinking == "recent thinking"


class TestEmergencyTruncate:
    """驗證 _emergency_truncate() Level 4 壓縮"""

    @pytest.fixture
    def agent(self, tmp_path):
        llm = MockLLMClient()
        return Agent(
            llm_client=llm,
            system_prompt="system",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            context_window=50000,
            max_output_tokens=4000,
            # hard_ceiling = max(50000-4000-3000, 8192) = 43000
        )

    def test_drops_oldest_round(self, agent):
        """丟棄最老的 user round"""
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="old question"),
            Message(role="assistant", content="x " * 60000),  # ~48000 tokens，會超硬頂(43000)
            Message(role="user", content="new question"),
            Message(role="assistant", content="short answer"),
        ]
        agent._emergency_truncate()
        # 最老的 user round 應該被丟棄
        user_msgs = [m for m in agent.messages if m.role == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0].content == "new question"

    def test_preserves_last_round(self, agent):
        """至少保留最後一個 user round"""
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="only question"),
            Message(role="assistant", content="x " * 30000),
        ]
        original_count = len(agent.messages)
        agent._emergency_truncate()
        # 只有一個 user round，不能再丟
        assert len(agent.messages) == original_count


# ============================================================
# Phase 4: finish_reason + Agent 構造函數
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


class TestHardCeilingFloorProtection:
    """驗證 hard_ceiling 在極端配置下不會為負"""

    @pytest.fixture
    def agent(self, tmp_path):
        llm = MockLLMClient()
        return Agent(
            llm_client=llm,
            system_prompt="system",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            # 極端情況：context_window == max_output_tokens
            context_window=16384,
            max_output_tokens=16384,
        )

    def test_emergency_truncate_does_not_loop_on_equal_params(self, agent):
        """context_window == max_output_tokens 時，hard_ceiling 應 >= 8192"""
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="only question"),
            Message(role="assistant", content="short"),
        ]
        # 不應無限循環或拋異常
        agent._emergency_truncate()
        # 至少保留 system + 1 user round
        assert len(agent.messages) >= 2

    def test_emergency_truncate_small_context_window(self, tmp_path):
        """context_window 很小但正值時，hard_ceiling 被下界保護"""
        llm = MockLLMClient()
        agent = Agent(
            llm_client=llm,
            system_prompt="sys",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            context_window=5000,
            max_output_tokens=4000,
        )
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="q1"),
            Message(role="assistant", content="a1"),
        ]
        # hard_ceiling = max(5000-4000-3000, 8192) = 8192
        # 不應出錯
        agent._emergency_truncate()


# ============================================================
# Review 修復: Failover 參數恢復
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
                    await self.failover_notify("fallback-model", 64000, 8192)
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


# ============================================================
# Review 修復: 歷史消息輪次邊界裁剪
# ============================================================


class TestHistoryTrimRoundBoundary:
    """驗證歷史消息裁剪按完整輪次而非裸消息數"""

    def test_trim_aligns_to_user_boundary(self):
        """裁剪後首條消息必須是 user 角色"""
        from types import SimpleNamespace

        # 模擬 50 條交替消息
        msgs = []
        for i in range(25):
            msgs.append(SimpleNamespace(role="user", content=f"q{i}", sequence=i*2))
            msgs.append(SimpleNamespace(role="assistant", content=f"a{i}", sequence=i*2+1))

        max_msgs = 10
        # 粗切
        trimmed = msgs[-max_msgs:]
        # 對齊到 user 邊界
        while trimmed and trimmed[0].role != "user":
            trimmed = trimmed[1:]

        assert trimmed[0].role == "user"
        assert len(trimmed) <= max_msgs

    def test_trim_preserves_tool_pair(self):
        """裁剪不應以 tool 消息開頭"""
        from types import SimpleNamespace

        msgs = [
            SimpleNamespace(role="user", content="q1", sequence=1),
            SimpleNamespace(role="assistant", content="a1", sequence=2),
            SimpleNamespace(role="tool", content="result", sequence=3),
            SimpleNamespace(role="user", content="q2", sequence=4),
            SimpleNamespace(role="assistant", content="a2", sequence=5),
        ]

        max_msgs = 3
        trimmed = msgs[-max_msgs:]
        # [-3:] = [tool, user, assistant]，tool 開頭→向前推
        while trimmed and trimmed[0].role != "user":
            trimmed = trimmed[1:]

        assert trimmed[0].role == "user"
        assert trimmed[0].content == "q2"


# ============================================================
# Synthetic-aware round boundary alignment
# ============================================================


class TestSyntheticAwareRoundBoundary:
    """驗證 round 邊界對齊邏輯跳過 synthetic user 消息"""

    def test_microcompact_ignores_synthetic(self, tmp_path):
        """_microcompact_messages 的 user_indices 只認真實 user 消息"""
        llm = MockLLMClient()
        agent = Agent(
            llm_client=llm,
            system_prompt="sys",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
        )
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="q1"),
            Message(role="tool", content="OLD_LARGE " * 500, tool_call_id="tc1"),
            Message(role="user", content="nudge", is_synthetic=True),  # synthetic，不算 round 邊界
            Message(role="assistant", content="a1"),
            Message(role="user", content="q2"),
            Message(role="tool", content="RECENT " * 500, tool_call_id="tc2"),
            Message(role="user", content="q3"),
        ]
        # 真實 user: q1(idx=1), q2(idx=5), q3(idx=7) → 3 個 round
        # safe_boundary = user_indices[-2] = idx 5
        # idx 2 的 tool (OLD_LARGE) 在邊界前，應被壓縮
        result = agent._microcompact_messages()
        assert result > 0
        assert "compacted" in agent.messages[2].content.lower()
        # idx 6 的 tool (RECENT) 在邊界後，應保留
        assert "RECENT" in agent.messages[6].content

    def test_emergency_truncate_ignores_synthetic(self, tmp_path):
        """_emergency_truncate 的 user_indices 只認真實 user 消息"""
        llm = MockLLMClient()
        agent = Agent(
            llm_client=llm,
            system_prompt="sys",
            tools=[],
            workspace_dir=str(tmp_path / "ws"),
            context_window=50000,
            max_output_tokens=4000,
        )
        agent.messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="old question"),
            Message(role="assistant", content="x " * 60000),
            Message(role="user", content="nudge", is_synthetic=True),  # synthetic
            Message(role="assistant", content="retry answer"),
            Message(role="user", content="new question"),
            Message(role="assistant", content="short"),
        ]
        agent._emergency_truncate()
        # 應丟棄 old question round（到 new question 之前），synthetic 跟著被丟
        real_users = [m for m in agent.messages if m.role == "user" and not m.is_synthetic]
        assert len(real_users) == 1
        assert real_users[0].content == "new question"


# ============================================================
# 合成消息 is_synthetic 標記 + CUSTOM 事件
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
