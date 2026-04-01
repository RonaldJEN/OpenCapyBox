"""History Service 測試

測試 Round + AGUIEventLog 雙表結構
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
import json
import uuid

from src.api.services.history_service import HistoryService
from src.api.models.round import Round
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
            user_message="Hello"
        )
        
        mock_db.add.assert_called_once()
        added_round = mock_db.add.call_args[0][0]
        
        assert added_round.id == "round-456"
        assert added_round.session_id == "session-123"
        assert added_round.user_message == "Hello"
        assert added_round.status == "running"

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
        
        # 模拟 AG-UI 事件（用于重建 steps）
        mock_event = MagicMock()
        mock_event.event_type = "STEP_STARTED"
        mock_event.payload = json.dumps({"type": "STEP_STARTED"})
        mock_event.created_at = datetime.now()
        
        mock_event_end = MagicMock()
        mock_event_end.event_type = "STEP_FINISHED"
        mock_event_end.payload = json.dumps({"type": "STEP_FINISHED"})
        mock_event_end.created_at = datetime.now()
        
        # 設置查詢返回：rounds 查詢 和 events 查詢
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.side_effect = [
            [mock_round],  # rounds 查詢
            [mock_event, mock_event_end],  # events 查詢（用於重建 steps）
        ]
        
        rounds = history_service.get_session_rounds("session-123")
        
        assert len(rounds) == 1
        assert rounds[0]["round_id"] == "round-1"
        assert rounds[0]["user_message"] == "Hello"
        # steps 從事件重建
        assert "steps" in rounds[0]


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


class TestInjectSystemRound:
    """inject_system_round 测试（Cron 结果注入等）"""

    def test_inject_creates_round_and_events(self, history_service, mock_db):
        """inject_system_round 应创建 Round + 7 条 AGUI 事件"""
        round_id = history_service.inject_system_round(
            session_id="session-123",
            content="⏰ 定时任务完成",
            source="cron:daily_report",
        )

        assert round_id  # 返回非空 round_id
        # 1 Round + 7 AGUIEventLog = 8 次 add
        assert mock_db.add.call_count == 8
        mock_db.commit.assert_called_once()

    def test_inject_round_fields(self, history_service, mock_db):
        """验证注入的 Round 字段"""
        round_id = history_service.inject_system_round(
            session_id="session-123",
            content="测试消息",
            source="cron:test",
        )

        # 第一次 add 应该是 Round 对象
        first_add_call = mock_db.add.call_args_list[0]
        round_obj = first_add_call[0][0]
        assert round_obj.session_id == "session-123"
        assert round_obj.user_message == "[cron:test]"
        assert round_obj.final_response == "测试消息"
        assert round_obj.status == "completed"
        assert round_obj.step_count == 1

    def test_inject_event_types(self, history_service, mock_db):
        """验证注入的 AG-UI 事件类型序列"""
        history_service.inject_system_round(
            session_id="session-123",
            content="hello",
            source="system",
        )

        # 提取所有 add 的对象（第一个是 Round，后面 7 个是事件）
        all_added = [call[0][0] for call in mock_db.add.call_args_list]
        events = all_added[1:]  # 跳过 Round

        event_types = [e.event_type for e in events]
        assert event_types == [
            "RUN_STARTED",
            "STEP_STARTED",
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
            "STEP_FINISHED",
            "RUN_FINISHED",
        ]

    def test_inject_event_content(self, history_service, mock_db):
        """验证 TEXT_MESSAGE_CONTENT 事件包含正确内容"""
        history_service.inject_system_round(
            session_id="session-123",
            content="任务执行结果",
            source="cron:test",
        )

        all_added = [call[0][0] for call in mock_db.add.call_args_list]
        # 第4个 add（index 3）是 TEXT_MESSAGE_CONTENT
        content_event = all_added[4]  # index 0=Round, 1-7=events, content at index 4
        payload = json.loads(content_event.payload)
        assert payload["type"] == "TEXT_MESSAGE_CONTENT"
        assert payload["delta"] == "任务执行结果"
