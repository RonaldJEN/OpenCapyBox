"""History Service 測試

測試 Round + AGUIEventLog 雙表結構
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
import json
import uuid
from sqlalchemy.exc import IntegrityError

from src.api.services.history_service import HistoryService
from src.api.models.round import Round
from src.agent.schema.agui_events import (
    RunFinishedEvent,
    TextMessageContentEvent,
    TextMessageStartEvent,
)
from tests.helpers import make_query_db


# ============== 模块级 Fixtures（所有 TestClass 共用） ==============

@pytest.fixture
def mock_db():
    """創建模擬數據庫"""
    return make_query_db()


@pytest.fixture
def history_service(mock_db):
    """創建 HistoryService"""
    return HistoryService(mock_db)


class TestHistoryServiceRound:
    """Round 相關方法測試"""

    def test_create_round(self, history_service, mock_db):
        """測試創建 Round"""
        result = history_service.create_round(
            session_id="session-123",
            round_id="round-456",
            user_message="Hello",
            preferred_skills=[
                {"key": "pdf", "display_name": "PDF 文档", "ignored": "drop"},
            ],
        )
        
        mock_db.add.assert_called_once()
        added_round = mock_db.add.call_args[0][0]
        
        assert added_round.id == "round-456"
        assert added_round.session_id == "session-123"
        assert added_round.user_message == "Hello"
        assert json.loads(added_round.preferred_skills) == [
            {"key": "pdf", "display_name": "PDF 文档"},
        ]
        assert added_round.status == "running"
        mock_db.refresh.assert_called_once_with(added_round)
        mock_db.expunge.assert_called_once_with(added_round)
        mock_db.rollback.assert_called_once()

    def test_create_round_with_parent_run_id(self, history_service, mock_db):
        """分支或子运行可以记录父 Round。"""
        history_service.create_round(
            session_id="session-123",
            round_id="round-resume",
            user_message="Q: Confirm?\nA: yes",
            parent_run_id="round-parent",
        )

        added_round = mock_db.add.call_args[0][0]
        assert added_round.parent_run_id == "round-parent"

    def test_idempotency_conflict_returns_original_preferred_skill_snapshot(
        self, history_service, mock_db
    ):
        existing = Round(
            id="round-existing",
            session_id="session-123",
            user_message="Original",
            preferred_skills=json.dumps([
                {"key": "pdf", "display_name": "Original PDF"},
            ]),
            idempotency_key="idem-1",
        )
        mock_db.commit.side_effect = IntegrityError(
            "INSERT",
            {},
            Exception("unique constraint"),
        )
        mock_db.query.return_value.filter.return_value.first.return_value = existing

        result = history_service.create_round(
            session_id="session-123",
            round_id="round-loser",
            user_message="Duplicate",
            preferred_skills=[
                {"key": "xlsx", "display_name": "New XLSX"},
            ],
            idempotency_key="idem-1",
        )

        assert result is existing
        assert json.loads(result.preferred_skills) == [
            {"key": "pdf", "display_name": "Original PDF"},
        ]

    def test_complete_round(self, history_service, mock_db):
        """測試完成 Round"""
        mock_round = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_round
        
        result = history_service.complete_round(
            round_id="round-456",
            final_response="Task completed",
            step_count=3,
            status="completed"
        )
        
        assert mock_round.final_response == "Task completed"
        assert mock_round.step_count == 3
        assert mock_round.status == "completed"
        mock_db.commit.assert_called()

    def test_complete_round_not_found(self, history_service, mock_db):
        """測試完成不存在的 Round"""
        mock_db.query.return_value.filter.return_value.first.return_value = None
        
        result = history_service.complete_round(
            round_id="nonexistent",
            final_response="Response",
            step_count=1
        )
        
        assert result is None

    def test_complete_round_skips_terminal_status(self, history_service, mock_db):
        """已處於終態的 round 不應被 complete_round 覆寫。

        回归：跨 worker 场景下 abort 将 round 标为 cancelled，但另一
        worker 上的 Agent 仍尝试 complete_round(status=completed)，
        导致状态矛盾。
        """
        for terminal in ("completed", "failed", "cancelled"):
            mock_round = MagicMock()
            mock_round.status = terminal
            mock_round.id = "round-terminal"
            mock_db.query.return_value.filter.return_value.first.return_value = mock_round

            result = history_service.complete_round(
                round_id="round-terminal",
                final_response="should not overwrite",
                step_count=99,
                status="completed",
            )

            assert result.status == terminal, f"终态 {terminal} 不应被覆写"
            mock_db.expunge.assert_called_once_with(mock_round)
            mock_db.rollback.assert_called_once()
            mock_db.commit.reset_mock()
            mock_db.expunge.reset_mock()
            mock_db.rollback.reset_mock()

    def test_get_round_status_releases_read_transaction(self, history_service, mock_db):
        """只读查询 round 状态后应立即结束事务，避免 PG idle-in-transaction。"""
        mock_db.query.return_value.filter.return_value.first.return_value = ("running",)

        result = history_service.get_round_status("round-456")

        assert result == "running"
        mock_db.rollback.assert_called_once()


class TestHistoryServiceGetSessionRounds:
    """獲取會話輪次測試"""

    def test_get_session_rounds_empty(self, history_service, mock_db):
        """測試獲取空會話輪次"""
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        
        rounds = history_service.get_session_rounds("session-123")
        
        assert rounds == []

    def test_get_session_rounds_with_data(self, history_service, mock_db):
        """測試獲取有數據的會話輪次（steps 從 AG-UI 事件重建）"""
        mock_round = MagicMock()
        mock_round.id = "round-1"
        mock_round.user_message = "Hello"
        mock_round.final_response = "Hi"
        mock_round.step_count = 1
        mock_round.status = "completed"
        mock_round.created_at = datetime.now()
        mock_round.completed_at = datetime.now()
        mock_round.user_attachments = None
        mock_round.preferred_skills = json.dumps([
            {
                "key": "pdf",
                "display_name": "PDF 文档",
                "ignored": "not returned",
            },
        ])
        mock_round.parent_run_id = "round-parent"
        
        # 模拟 AG-UI 事件（用于重建 steps）
        mock_event = MagicMock()
        mock_event.event_type = "STEP_STARTED"
        mock_event.payload = json.dumps({"type": "STEP_STARTED"})
        mock_event.created_at = datetime.now()
        mock_event.sequence = 1
        
        mock_event_end = MagicMock()
        mock_event_end.event_type = "STEP_FINISHED"
        mock_event_end.payload = json.dumps({"type": "STEP_FINISHED"})
        mock_event_end.created_at = datetime.now()
        mock_event_end.sequence = 2
        
        # 設置查詢返回：subagent child id 查詢、rounds 查詢 和 events 查詢
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.side_effect = [
            [mock_round],  # rounds 查詢
            [mock_event, mock_event_end],  # events 查詢（用於重建 steps）
        ]
        
        rounds = history_service.get_session_rounds("session-123")
        
        assert len(rounds) == 1
        assert rounds[0]["round_id"] == "round-1"
        assert rounds[0]["parent_run_id"] == "round-parent"
        assert rounds[0]["last_event_sequence"] == 2
        assert rounds[0]["user_message"] == "Hello"
        assert rounds[0]["preferred_skills"] == [
            {"key": "pdf", "display_name": "PDF 文档"},
        ]
        # steps 從事件重建
        assert "steps" in rounds[0]

    def test_get_session_rounds_old_or_corrupt_snapshot_defaults_to_empty(
        self, history_service, mock_db
    ):
        history_service._get_subagent_child_round_ids = MagicMock(return_value=set())
        history_service._rebuild_steps_from_events = MagicMock(return_value=([], 0))
        rows = []
        for round_id, preferred_skills in (
            ("old", None),
            ("corrupt", "not-json"),
            ("wrong-shape", '[{"key":"pdf"}]'),
        ):
            rows.append(MagicMock(
                id=round_id,
                user_message="Hello",
                final_response="Hi",
                step_count=0,
                status="completed",
                created_at=datetime.now(),
                completed_at=datetime.now(),
                user_attachments=None,
                preferred_skills=preferred_skills,
                parent_run_id=None,
                idempotency_key=None,
            ))
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = rows

        rounds = history_service.get_session_rounds("session-123")

        assert [round_data["preferred_skills"] for round_data in rounds] == [
            [],
            [],
            [],
        ]

class TestHistoryServiceRebuildSteps:
    """從 AG-UI 事件重建 steps 的測試"""

    def test_rebuild_steps_empty(self, history_service, mock_db):
        """測試無事件時返回空列表"""
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        
        steps = history_service._rebuild_steps_from_events("run-123")
        
        assert steps == []

    def test_rebuild_steps_with_thinking(self, history_service, mock_db):
        """測試從事件重建包含 thinking 的 steps（聚合模式）"""
        events = [
            MagicMock(
                event_type="STEP_STARTED",
                payload=json.dumps({"type": "STEP_STARTED"}),
                created_at=datetime.now(),
                id="e1"
            ),
            # 🔥 聚合後的 END 事件包含 fullContent
            MagicMock(
                event_type="THINKING_TEXT_MESSAGE_END",
                payload=json.dumps({"type": "THINKING_TEXT_MESSAGE_END", "fullContent": "正在思考..."}),
                created_at=datetime.now(),
                id="e2"
            ),
            MagicMock(
                event_type="TEXT_MESSAGE_END",
                payload=json.dumps({"type": "TEXT_MESSAGE_END", "fullContent": "回覆內容"}),
                created_at=datetime.now(),
                id="e3"
            ),
            MagicMock(
                event_type="STEP_FINISHED",
                payload=json.dumps({"type": "STEP_FINISHED"}),
                created_at=datetime.now(),
                id="e4"
            ),
        ]
        
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = events
        
        steps = history_service._rebuild_steps_from_events("run-123")
        
        assert len(steps) == 1
        assert steps[0]["thinking"] == "正在思考..."
        assert steps[0]["assistant_content"] == "回覆內容"
        assert steps[0]["status"] == "completed"

    def test_rebuild_steps_with_tool_calls(self, history_service, mock_db):
        """測試從事件重建包含工具調用的 steps"""
        events = [
            MagicMock(
                event_type="STEP_STARTED",
                payload=json.dumps({"type": "STEP_STARTED"}),
                created_at=datetime.now(),
                id="e1"
            ),
            MagicMock(
                event_type="TOOL_CALL_START",
                payload=json.dumps({
                    "type": "TOOL_CALL_START",
                    "toolCallId": "tc-1",
                    "toolCallName": "read_file"
                }),
                created_at=datetime.now(),
                id="e2"
            ),
            MagicMock(
                event_type="TOOL_CALL_ARGS",
                payload=json.dumps({
                    "type": "TOOL_CALL_ARGS",
                    "delta": '{"path": "test.txt"}'
                }),
                created_at=datetime.now(),
                id="e3"
            ),
            MagicMock(
                event_type="TOOL_CALL_END",
                payload=json.dumps({"type": "TOOL_CALL_END"}),
                created_at=datetime.now(),
                id="e4"
            ),
            MagicMock(
                event_type="TOOL_CALL_RESULT",
                payload=json.dumps({
                    "type": "TOOL_CALL_RESULT",
                    "toolCallId": "tc-1",
                    "result": "File content"
                }),
                created_at=datetime.now(),
                id="e5"
            ),
            MagicMock(
                event_type="STEP_FINISHED",
                payload=json.dumps({"type": "STEP_FINISHED"}),
                created_at=datetime.now(),
                id="e6"
            ),
        ]
        
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = events
        
        steps = history_service._rebuild_steps_from_events("run-123")
        
        assert len(steps) == 1
        assert len(steps[0]["tool_calls"]) == 1
        assert steps[0]["tool_calls"][0]["name"] == "read_file"
        assert steps[0]["tool_calls"][0]["input"] == {"path": "test.txt"}
        assert len(steps[0]["tool_results"]) == 1

    def test_rebuild_steps_preserves_agui_timing_metadata(self, history_service, mock_db):
        """history/v2 重建 steps 时应保留 AG-UI 时间戳和工具耗时。"""
        events = [
            MagicMock(
                event_type="STEP_STARTED",
                payload=json.dumps({"type": "STEP_STARTED"}),
                timestamp=1000,
                created_at=datetime.now(),
                id="e1",
            ),
            MagicMock(
                event_type="THINKING_TEXT_MESSAGE_START",
                payload=json.dumps({"type": "THINKING_TEXT_MESSAGE_START", "messageId": "think-1"}),
                timestamp=1100,
                created_at=datetime.now(),
                id="e2",
            ),
            MagicMock(
                event_type="THINKING_TEXT_MESSAGE_END",
                payload=json.dumps({"type": "THINKING_TEXT_MESSAGE_END", "fullContent": "正在思考"}),
                timestamp=1500,
                created_at=datetime.now(),
                id="e3",
            ),
            MagicMock(
                event_type="TOOL_CALL_START",
                payload=json.dumps({
                    "type": "TOOL_CALL_START",
                    "toolCallId": "tc-1",
                    "toolCallName": "search_web",
                }),
                timestamp=1600,
                created_at=datetime.now(),
                id="e4",
            ),
            MagicMock(
                event_type="TOOL_CALL_ARGS",
                payload=json.dumps({"type": "TOOL_CALL_ARGS", "delta": '{"query": "黄金价格"}'}),
                timestamp=1650,
                created_at=datetime.now(),
                id="e5",
            ),
            MagicMock(
                event_type="TOOL_CALL_END",
                payload=json.dumps({"type": "TOOL_CALL_END"}),
                timestamp=1700,
                created_at=datetime.now(),
                id="e6",
            ),
            MagicMock(
                event_type="TOOL_CALL_RESULT",
                payload=json.dumps({
                    "type": "TOOL_CALL_RESULT",
                    "toolCallId": "tc-1",
                    "messageId": "tool-msg-1",
                    "content": "搜索结果",
                    "executionTimeMs": 230,
                }),
                timestamp=1900,
                created_at=datetime.now(),
                id="e7",
            ),
            MagicMock(
                event_type="STEP_FINISHED",
                payload=json.dumps({"type": "STEP_FINISHED"}),
                timestamp=2000,
                created_at=datetime.now(),
                id="e8",
            ),
        ]

        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = events

        steps = history_service._rebuild_steps_from_events("run-123")

        assert len(steps) == 1
        step = steps[0]
        assert step["started_at_ts"] == 1000
        assert step["thinking_start_ts"] == 1100
        assert step["thinking_end_ts"] == 1500
        assert step["finished_at_ts"] == 2000
        assert step["thinking"] == "正在思考"
        assert step["tool_calls"][0]["id"] == "tc-1"
        assert step["tool_calls"][0]["started_at_ts"] == 1600
        assert step["tool_calls"][0]["ended_at_ts"] == 1700
        assert step["tool_results"][0]["tool_call_id"] == "tc-1"
        assert step["tool_results"][0]["received_at_ts"] == 1900
        assert step["tool_results"][0]["execution_time_ms"] == 230


class TestHistoryServiceIntegration:
    """整合測試"""

    def test_full_workflow(self, history_service, mock_db):
        """測試完整工作流程（Round + AG-UI Events）"""
        # 1. 創建 Round
        mock_db.query.return_value.filter.return_value.first.return_value = None
        history_service.create_round(
            session_id="session-123",
            round_id="round-1",
            user_message="Hello"
        )
        
        # 2. 完成 Round（steps 通過 AG-UI 事件自動記錄）
        mock_round = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_round
        
        history_service.complete_round(
            round_id="round-1",
            final_response="Done!",
            step_count=1,
            status="completed"
        )
        
        # 驗證操作執行
        assert mock_db.add.call_count >= 1  # round
        assert mock_db.commit.call_count >= 1


class TestHistoryServiceLLMCallRecord:
    """LLM 调用记录持久化测试。"""

    @pytest.mark.asyncio
    async def test_save_llm_call_record(self, history_service, mock_db):
        await history_service.save_llm_call_record(
            session_id="session-123",
            round_id="round-456",
            step_index=2,
            request_messages=[{"role": "user", "content": "hi"}],
            request_tools=["read_file"],
            response_content="hello",
            response_thinking=None,
            response_tool_calls=None,
            response_error=None,
            finish_reason="stop",
            usage_prompt_tokens=10,
            usage_completion_tokens=3,
            usage_total_tokens=13,
            first_token_latency_s=0.12,
            completion_latency_s=0.86,
            compaction_triggered=True,
            compaction_pre_tokens=81234,
            compaction_post_tokens=52345,
            compaction_tokens_saved=28889,
            compaction_microcompact_compacted_messages=4,
            compaction_summary_generated_count=2,
            compaction_summary_reused_count=1,
            compaction_summary_quality_repair_count=1,
            compaction_emergency_truncate_dropped_rounds=0,
        )

        mock_db.add.assert_called_once()
        row = mock_db.add.call_args.args[0]
        assert row.session_id == "session-123"
        assert row.round_id == "round-456"
        assert row.step_index == 2
        assert row.request_message_count == 1
        assert row.manual_review_status == "没问题"
        assert json.loads(row.request_messages) == [{"role": "user", "content": "hi"}]
        assert json.loads(row.request_tools) == ["read_file"]
        assert row.response_content == "hello"
        assert row.finish_reason == "stop"
        assert row.usage_total_tokens == 13
        assert row.first_token_latency_s == 0.12
        assert row.completion_latency_s == 0.86
        assert row.compaction_triggered is True
        assert row.compaction_pre_tokens == 81234
        assert row.compaction_post_tokens == 52345
        assert row.compaction_tokens_saved == 28889
        assert row.compaction_microcompact_compacted_messages == 4
        assert row.compaction_summary_generated_count == 2
        assert row.compaction_summary_reused_count == 1
        assert row.compaction_summary_quality_repair_count == 1
        assert row.compaction_emergency_truncate_dropped_rounds == 0
        mock_db.commit.assert_called_once()
        mock_db.refresh.assert_called_once_with(row)
        mock_db.expunge.assert_called_once_with(row)
        mock_db.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_llm_call_record_counts_provider_snapshot_messages(self, history_service, mock_db):
        await history_service.save_llm_call_record(
            session_id="session-123",
            round_id="round-456",
            step_index=3,
            request_messages=[
                {
                    "provider": "openai",
                    "model": "glm-5.1",
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": "u"},
                        {"role": "assistant", "content": "a"},
                    ],
                }
            ],
            request_tools=["read_file"],
            response_content="hello",
            response_thinking=None,
            response_tool_calls=None,
            response_error=None,
            finish_reason="stop",
            usage_prompt_tokens=10,
            usage_completion_tokens=3,
            usage_total_tokens=13,
            first_token_latency_s=0.055,
            completion_latency_s=0.4,
        )

        mock_db.add.assert_called_once()
        row = mock_db.add.call_args.args[0]
        assert row.request_message_count == 3
        assert row.manual_review_status == "没问题"
        assert row.first_token_latency_s == 0.055
        assert row.completion_latency_s == 0.4
        assert row.compaction_triggered is False


class TestHistoryServiceLateEventDrop:
    """終態 round 的遲到事件隔離測試。"""

    @pytest.mark.asyncio
    async def test_save_agui_event_commits_non_delta_event_immediately(self, history_service, mock_db):
        """非 delta 事件应立即提交，避免 SSE await 间隙持有 PG 事务。"""
        mock_db.query.return_value.filter.return_value.first.return_value = None
        event = TextMessageStartEvent(messageId="msg-1", role="assistant")

        result = await history_service.save_agui_event("run-1", event)

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_agui_event_commits_delta_status_read_transaction(self, history_service, mock_db):
        """delta 事件不写行，但终态读取产生的事务也要立即提交释放。"""
        mock_db.query.return_value.filter.return_value.first.return_value = None
        event = TextMessageContentEvent(messageId="msg-1", delta="hello")

        result = await history_service.save_agui_event("run-1", event)

        assert result is None
        mock_db.add.assert_not_called()
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_agui_event_drops_late_event_when_round_terminal(self, history_service, mock_db):
        """round 已是終態時，save_agui_event 應直接丟棄事件且不入庫。"""
        mock_db.query.return_value.filter.return_value.first.return_value = ("cancelled",)

        event = RunFinishedEvent(
            threadId="session-1",
            runId="run-1",
            outcome="interrupt",
            result={"reason": "user_cancelled"},
        )

        result = await history_service.save_agui_event("run-1", event)

        assert result is None
        mock_db.add.assert_not_called()
