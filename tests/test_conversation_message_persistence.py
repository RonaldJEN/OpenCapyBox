"""Agent 上下文恢复测试（去重方案）

验证 _restore_history 从 agui_events 重建完整 messages（含 tool 交互），
不依赖 conversation_messages 新增列。agui_events 是 Agent 输出的单一事实源。
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from src.agent.schema import Message as AgentMessage, ToolCall, FunctionCall
from tests.helpers import make_agent_service


# ============================================================
# _events_to_messages 单元测试
# ============================================================

class TestEventsToMessages:
    """测试 agui_events → Message[] 的转换逻辑"""

    @staticmethod
    def _evt(event_type: str, payload: dict, sequence: int = 0):
        """创建模拟的 AGUIEventLog 行"""
        payload["type"] = event_type
        e = MagicMock()
        e.payload = json.dumps(payload)
        e.sequence = sequence
        return e

    def test_text_only_step(self):
        """纯文本步骤 → 一条 assistant 消息"""
        from src.api.services.agent_service import AgentService

        events = [
            self._evt("TEXT_MESSAGE_CONTENT", {"delta": "Hello world"}, 1),
            self._evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 2),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 3),
        ]

        msgs = AgentService._events_to_messages(events)
        assert len(msgs) == 1
        assert msgs[0].role == "assistant"
        assert msgs[0].content == "Hello world"
        assert msgs[0].tool_calls is None

    def test_tool_call_step(self):
        """工具调用步骤 → assistant(tool_calls) + tool result"""
        from src.api.services.agent_service import AgentService

        events = [
            self._evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "bash"}, 1),
            self._evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"command": "ls"}'}, 2),
            self._evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 3),
            self._evt("TOOL_CALL_RESULT", {"toolCallId": "tc1", "content": "file1.txt", "messageId": "m1"}, 4),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 5),
        ]

        msgs = AgentService._events_to_messages(events)
        assert len(msgs) == 2

        # assistant with tool_calls
        assert msgs[0].role == "assistant"
        assert msgs[0].content == ""
        assert msgs[0].tool_calls is not None
        assert len(msgs[0].tool_calls) == 1
        assert msgs[0].tool_calls[0].id == "tc1"
        assert msgs[0].tool_calls[0].function.name == "bash"
        assert msgs[0].tool_calls[0].function.arguments == {"command": "ls"}

        # tool result
        assert msgs[1].role == "tool"
        assert msgs[1].content == "file1.txt"
        assert msgs[1].tool_call_id == "tc1"
        assert msgs[1].name == "bash"

    def test_text_plus_tool_call_step(self):
        """assistant 先输出文本再调工具"""
        from src.api.services.agent_service import AgentService

        events = [
            self._evt("TEXT_MESSAGE_CONTENT", {"delta": "Let me check."}, 1),
            self._evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 2),
            self._evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "read_file"}, 3),
            self._evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"path": "a.txt"}'}, 4),
            self._evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 5),
            self._evt("TOOL_CALL_RESULT", {"toolCallId": "tc1", "content": "data", "messageId": "m2"}, 6),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 7),
        ]

        msgs = AgentService._events_to_messages(events)
        assert len(msgs) == 2
        assert msgs[0].role == "assistant"
        assert msgs[0].content == "Let me check."
        assert msgs[0].tool_calls[0].function.name == "read_file"
        assert msgs[1].role == "tool"
        assert msgs[1].content == "data"

    def test_multiple_tool_calls_in_step(self):
        """单步多工具调用"""
        from src.api.services.agent_service import AgentService

        events = [
            self._evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "bash"}, 1),
            self._evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"command": "pwd"}'}, 2),
            self._evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 3),
            self._evt("TOOL_CALL_RESULT", {"toolCallId": "tc1", "content": "/home", "messageId": "m1"}, 4),
            self._evt("TOOL_CALL_START", {"toolCallId": "tc2", "toolCallName": "read_file"}, 5),
            self._evt("TOOL_CALL_ARGS", {"toolCallId": "tc2", "delta": '{"path": "x.py"}'}, 6),
            self._evt("TOOL_CALL_END", {"toolCallId": "tc2"}, 7),
            self._evt("TOOL_CALL_RESULT", {"toolCallId": "tc2", "content": "code", "messageId": "m2"}, 8),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 9),
        ]

        msgs = AgentService._events_to_messages(events)
        assert len(msgs) == 3  # 1 assistant + 2 tool
        assert len(msgs[0].tool_calls) == 2
        assert msgs[0].tool_calls[0].function.name == "bash"
        assert msgs[0].tool_calls[1].function.name == "read_file"
        assert msgs[1].tool_call_id == "tc1"
        assert msgs[2].tool_call_id == "tc2"

    def test_multi_step_round(self):
        """多步骤 round：工具→文本"""
        from src.api.services.agent_service import AgentService

        events = [
            # Step 1: tool call
            self._evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "bash"}, 1),
            self._evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"command": "ls"}'}, 2),
            self._evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 3),
            self._evt("TOOL_CALL_RESULT", {"toolCallId": "tc1", "content": "file1.txt", "messageId": "m1"}, 4),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 5),
            # Step 2: text reply
            self._evt("TEXT_MESSAGE_CONTENT", {"delta": "Found 1 file."}, 6),
            self._evt("TEXT_MESSAGE_END", {"messageId": "m2"}, 7),
            self._evt("STEP_FINISHED", {"stepName": "step-2"}, 8),
        ]

        msgs = AgentService._events_to_messages(events)
        assert len(msgs) == 3
        # Step 1: assistant + tool
        assert msgs[0].role == "assistant"
        assert msgs[0].tool_calls[0].function.name == "bash"
        assert msgs[1].role == "tool"
        assert msgs[1].content == "file1.txt"
        # Step 2: assistant text
        assert msgs[2].role == "assistant"
        assert msgs[2].content == "Found 1 file."
        assert msgs[2].tool_calls is None

    def test_aborted_round_no_step_finished(self):
        """round 中断（无 STEP_FINISHED）时也应 flush 残留"""
        from src.api.services.agent_service import AgentService

        events = [
            self._evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "bash"}, 1),
            self._evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"command": "fail"}'}, 2),
            self._evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 3),
            self._evt("TOOL_CALL_RESULT", {"toolCallId": "tc1", "content": "Error!", "messageId": "m1"}, 4),
            # No STEP_FINISHED
        ]

        msgs = AgentService._events_to_messages(events)
        assert len(msgs) == 2
        assert msgs[0].role == "assistant"
        assert msgs[0].tool_calls[0].function.name == "bash"
        assert msgs[1].role == "tool"
        assert msgs[1].content == "Error!"

    def test_empty_events(self):
        """空事件列表 → 无消息"""
        from src.api.services.agent_service import AgentService
        assert AgentService._events_to_messages([]) == []

    def test_malformed_payload_skipped(self):
        """畸形 payload 应跳过不崩溃"""
        from src.api.services.agent_service import AgentService

        bad_evt = MagicMock()
        bad_evt.payload = "NOT_JSON{{{"
        bad_evt.sequence = 1

        msgs = AgentService._events_to_messages([bad_evt])
        assert msgs == []


# ============================================================
# _rebuild_messages_from_events 集成测试
# ============================================================

class TestRebuildMessagesFromEvents:
    """测试从 DB 读取 rounds + agui_events + conversation_messages 重建完整消息"""

    def _make_round(self, round_id, session_id, user_message, created_at_order=0):
        rnd = MagicMock()
        rnd.id = round_id
        rnd.session_id = session_id
        rnd.user_message = user_message
        rnd.created_at = created_at_order
        return rnd

    def _make_conv_msg(self, role, content, round_id, sequence):
        m = MagicMock()
        m.role = role
        m.content = content if isinstance(content, str) else json.dumps(content)
        m.round_id = round_id
        m.sequence = sequence
        m.is_summary = False
        return m

    def _make_evt(self, event_type, payload_dict, sequence):
        e = MagicMock()
        payload_dict["type"] = event_type
        e.payload = json.dumps(payload_dict)
        e.sequence = sequence
        return e

    def _setup_db(self, rounds, user_msgs, events_by_round):
        """构建按模型分发的 mock db.query()"""
        from src.api.models.round import Round
        from src.api.models.conversation_message import ConversationMessage
        from src.api.models.agui_event import AGUIEventLog

        mock_db = MagicMock()

        rounds_query = MagicMock()
        rounds_query.filter.return_value.order_by.return_value.all.return_value = rounds

        conv_query = MagicMock()
        conv_query.filter.return_value.order_by.return_value.all.return_value = user_msgs

        # agui_events：批量查询模式 — filter(run_id.in_(...)).order_by(...).all()
        # 将 events_by_round 展平为按 (run_id, sequence) 排序的列表
        all_events_flat = []
        for rnd in rounds:
            all_events_flat.extend(events_by_round.get(rnd.id, []))

        # 为每个 mock event 添加 run_id 属性（_events_to_messages 不需要，
        # 但 _rebuild_messages_from_events 中的 setdefault(evt.run_id, ...) 需要）
        for rnd in rounds:
            for evt in events_by_round.get(rnd.id, []):
                evt.run_id = rnd.id

        events_query = MagicMock()
        events_query.filter.return_value.order_by.return_value.all.return_value = all_events_flat

        def _query(model):
            if model is Round:
                return rounds_query
            elif model is ConversationMessage:
                return conv_query
            elif model is AGUIEventLog:
                return events_query
            return MagicMock()

        mock_db.query = MagicMock(side_effect=_query)
        return mock_db

    def test_full_round_with_tool_interaction(self):
        """完整 round：user → tool call → text reply"""
        rounds = [self._make_round("r1", "s1", "list files")]
        user_msgs = [self._make_conv_msg("user", "list files", "r1", 1)]
        events = {
            "r1": [
                self._make_evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "bash"}, 1),
                self._make_evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"command": "ls"}'}, 2),
                self._make_evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 3),
                self._make_evt("TOOL_CALL_RESULT", {"toolCallId": "tc1", "content": "a.txt", "messageId": "m1"}, 4),
                self._make_evt("STEP_FINISHED", {"stepName": "step-1"}, 5),
                self._make_evt("TEXT_MESSAGE_CONTENT", {"delta": "Found a.txt"}, 6),
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m2"}, 7),
                self._make_evt("STEP_FINISHED", {"stepName": "step-2"}, 8),
            ],
        }

        mock_db = self._setup_db(rounds, user_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")
        messages = service._rebuild_messages_from_events()

        assert len(messages) == 4
        assert messages[0].role == "user"
        assert messages[0].content == "list files"
        assert messages[1].role == "assistant"
        assert messages[1].tool_calls[0].function.name == "bash"
        assert messages[2].role == "tool"
        assert messages[2].content == "a.txt"
        assert messages[3].role == "assistant"
        assert messages[3].content == "Found a.txt"

    def test_fallback_to_round_user_message(self):
        """conversation_messages 无 user 记录时 fallback 到 rounds.user_message"""
        rounds = [self._make_round("r1", "s1", "hello from round")]
        events = {
            "r1": [
                self._make_evt("TEXT_MESSAGE_CONTENT", {"delta": "Hi!"}, 1),
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 2),
                self._make_evt("STEP_FINISHED", {"stepName": "step-1"}, 3),
            ],
        }

        mock_db = self._setup_db(rounds, [], events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")
        messages = service._rebuild_messages_from_events()

        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[0].content == "hello from round"
        assert messages[1].role == "assistant"
        assert messages[1].content == "Hi!"

    def test_no_rounds_returns_empty(self):
        """无 rounds → 空列表"""
        mock_db = self._setup_db([], [], {})
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service)
        messages = service._rebuild_messages_from_events()

        assert messages == []


# ============================================================
# _restore_history 集成测试
# ============================================================

class TestRestoreHistory:
    """测试 _restore_history 正确调用 _rebuild_messages_from_events 并注入 agent"""

    def test_restore_history_injects_messages(self):
        service = make_agent_service()
        service.agent = MagicMock()
        service.agent.messages = []

        fake_messages = [
            AgentMessage(role="user", content="hi"),
            AgentMessage(role="assistant", content="hello"),
        ]

        mock_settings = MagicMock()
        mock_settings.agent_max_history_messages = 100

        with patch.object(service, "_rebuild_messages_from_events", return_value=fake_messages):
            with patch("src.api.config.get_settings", return_value=mock_settings):
                service._restore_history()

        assert len(service.agent.messages) == 2
        assert service.agent.messages[0].role == "user"
        assert service.agent.messages[1].role == "assistant"

    def test_restore_history_trims_to_max(self):
        service = make_agent_service()
        service.agent = MagicMock()
        service.agent.messages = []

        fake_messages = [
            AgentMessage(role="user", content="q1"),
            AgentMessage(role="assistant", content="a1"),
            AgentMessage(role="user", content="q2"),
            AgentMessage(role="assistant", content="a2"),
            AgentMessage(role="user", content="q3"),
            AgentMessage(role="assistant", content="a3"),
        ]

        mock_settings = MagicMock()
        mock_settings.agent_max_history_messages = 4

        with patch.object(service, "_rebuild_messages_from_events", return_value=fake_messages):
            with patch("src.api.config.get_settings", return_value=mock_settings):
                service._restore_history()

        assert len(service.agent.messages) == 4
        assert service.agent.messages[0].role == "user"
        assert service.agent.messages[0].content == "q2"

    def test_restore_history_trim_logs_error_when_no_user_boundary(self, caplog):
        """裁剪對齊到 user 邊界時全部被跳過，應記錄 error 並跳過注入"""
        service = make_agent_service()
        service.agent = MagicMock()
        service.agent.messages = []

        # 構造全部是 assistant/tool 的尾部（極端情況，正常不應發生）
        fake_messages = [
            AgentMessage(role="user", content="q1"),
            AgentMessage(role="assistant", content="a1"),
            AgentMessage(role="tool", content="r1", tool_call_id="tc1", name="bash"),
            AgentMessage(role="assistant", content="a2"),
            AgentMessage(role="tool", content="r2", tool_call_id="tc2", name="bash"),
            AgentMessage(role="assistant", content="a3"),
            AgentMessage(role="tool", content="r3", tool_call_id="tc3", name="bash"),
        ]

        mock_settings = MagicMock()
        mock_settings.agent_max_history_messages = 3  # 尾部 3 條都是 assistant/tool

        import logging
        with caplog.at_level(logging.ERROR):
            with patch.object(service, "_rebuild_messages_from_events", return_value=fake_messages):
                with patch("src.api.config.get_settings", return_value=mock_settings):
                    service._restore_history()

        # 應記錄 error 日誌並且不注入任何消息
        assert len(service.agent.messages) == 0
        assert any("_rebuild_messages_from_events" in r.message for r in caplog.records)

    def test_restore_history_no_agent(self):
        service = make_agent_service()
        service._restore_history()  # should not raise

    def test_restore_history_empty(self):
        service = make_agent_service()
        service.agent = MagicMock()
        service.agent.messages = []

        with patch.object(service, "_rebuild_messages_from_events", return_value=[]):
            service._restore_history()

        assert len(service.agent.messages) == 0

    def test_restore_history_trim_skips_synthetic_user(self):
        """裁剪對齊到 user 邊界時，跳過 is_synthetic 的 user 消息"""
        service = make_agent_service()
        service.agent = MagicMock()
        service.agent.messages = []

        fake_messages = [
            AgentMessage(role="user", content="q1"),
            AgentMessage(role="assistant", content="a1"),
            AgentMessage(role="user", content="nudge", is_synthetic=True),  # synthetic
            AgentMessage(role="assistant", content="a2"),
            AgentMessage(role="user", content="q2"),
            AgentMessage(role="assistant", content="a3"),
        ]

        mock_settings = MagicMock()
        mock_settings.agent_max_history_messages = 4  # 尾部 4 條: synthetic_user, a2, q2, a3

        with patch.object(service, "_rebuild_messages_from_events", return_value=fake_messages):
            with patch("src.api.config.get_settings", return_value=mock_settings):
                service._restore_history()

        # synthetic user 不算 round 邊界，向後跳到下一個真實 user "q2"
        assert service.agent.messages[0].role == "user"
        assert service.agent.messages[0].content == "q2"


# ============================================================
# 合成消息持久化 + 恢復測試
# ============================================================

class TestSyntheticMessagePersistenceOnRestore:
    """驗證合成 user message 在 _rebuild_messages_from_events 中被正確包含"""

    @staticmethod
    def _make_round(round_id, session_id, user_message, created_at_order=0):
        rnd = MagicMock()
        rnd.id = round_id
        rnd.session_id = session_id
        rnd.user_message = user_message
        rnd.created_at = created_at_order
        return rnd

    @staticmethod
    def _make_conv_msg(role, content, round_id, sequence, is_synthetic=False):
        m = MagicMock()
        m.role = role
        m.content = content if isinstance(content, str) else json.dumps(content)
        m.round_id = round_id
        m.sequence = sequence
        m.is_summary = False
        m.is_synthetic = is_synthetic
        return m

    @staticmethod
    def _make_evt(event_type, payload_dict, sequence):
        e = MagicMock()
        payload_dict["type"] = event_type
        e.payload = json.dumps(payload_dict)
        e.sequence = sequence
        return e

    def _setup_db(self, rounds, user_msgs, events_by_round):
        from src.api.models.round import Round
        from src.api.models.conversation_message import ConversationMessage
        from src.api.models.agui_event import AGUIEventLog

        mock_db = MagicMock()

        rounds_query = MagicMock()
        rounds_query.filter.return_value.order_by.return_value.all.return_value = rounds

        conv_query = MagicMock()
        conv_query.filter.return_value.order_by.return_value.all.return_value = user_msgs

        all_events_flat = []
        for rnd in rounds:
            for evt in events_by_round.get(rnd.id, []):
                evt.run_id = rnd.id
            all_events_flat.extend(events_by_round.get(rnd.id, []))

        events_query = MagicMock()
        events_query.filter.return_value.order_by.return_value.all.return_value = all_events_flat

        def _query(model):
            if model is Round:
                return rounds_query
            elif model is ConversationMessage:
                return conv_query
            elif model is AGUIEventLog:
                return events_query
            return MagicMock()

        mock_db.query = MagicMock(side_effect=_query)
        return mock_db

    def test_synthetic_user_messages_included_in_rebuild(self):
        """合成 user 消息（如 empty nudge）在重建時自動被包含"""
        rounds = [self._make_round("r1", "s1", "hello")]
        # 一條真實 user + 一條合成 user（empty nudge）
        user_msgs = [
            self._make_conv_msg("user", "hello", "r1", 1),
            self._make_conv_msg("user",
                "You returned an empty response with no tool calls. "
                "Please provide your answer or call a tool to continue working.",
                "r1", 3, is_synthetic=True),
        ]
        events = {
            "r1": [
                # Step 1: 空回復（觸發 nudge）
                self._make_evt("TEXT_MESSAGE_CONTENT", {"delta": ""}, 1),
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 2),
                self._make_evt("STEP_FINISHED", {"stepName": "step-1"}, 3),
                # Step 2: 正常回復
                self._make_evt("TEXT_MESSAGE_CONTENT", {"delta": "Here is my answer."}, 4),
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m2"}, 5),
                self._make_evt("STEP_FINISHED", {"stepName": "step-2"}, 6),
            ],
        }

        mock_db = self._setup_db(rounds, user_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")
        messages = service._rebuild_messages_from_events()

        # 應有 3 條：user(real) + user(synthetic nudge) + assistant(answer)
        # 注意：第一個 step 的空 assistant 消息（delta=""）不會被生成
        # 因為 _events_to_messages 中 `if step_text or step_tool_calls` 對空字符串為 False
        assert len(messages) == 3
        assert messages[0].role == "user"
        assert messages[0].content == "hello"
        assert messages[1].role == "user"
        assert "empty response" in messages[1].content.lower()
        assert messages[2].role == "assistant"
        assert messages[2].content == "Here is my answer."


# ============================================================
# _save_conversation_message 原子 INSERT 測試
# ============================================================

class TestSaveConversationMessageAtomicInsert:
    """驗證 _save_conversation_message 使用原子 INSERT…SELECT
    正確分配 sequence，不因併發寫入產生 UNIQUE 衝突。"""

    @staticmethod
    def _make_real_db():
        """創建真實的 SQLite 內存庫並建表。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from src.api.models.database import Base
        # 確保 ConversationMessage model 已導入，否則 create_all 不會建表
        import src.api.models.conversation_message  # noqa: F401

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        return Session, engine

    def test_sequential_saves_increment_sequence(self):
        """連續 save 產生遞增 sequence 1, 2, 3"""
        Session, _ = self._make_real_db()
        db = Session()

        history = MagicMock()
        history.db = db

        service = make_agent_service(history_service=history, session_id="s-seq")

        service._save_conversation_message("user", "msg1", round_id="r1")
        service._save_conversation_message("assistant", "msg2", round_id="r1")
        service._save_conversation_message("user", "msg3", round_id="r2")

        from src.api.models.conversation_message import ConversationMessage as ConvMsg
        rows = db.query(ConvMsg).filter(ConvMsg.session_id == "s-seq").order_by(ConvMsg.sequence).all()
        assert [r.sequence for r in rows] == [1, 2, 3]
        assert [r.role for r in rows] == ["user", "assistant", "user"]
        db.close()

    def test_interleaved_sessions_have_independent_sequences(self):
        """不同 session_id 之間 sequence 互相獨立"""
        Session, _ = self._make_real_db()
        db = Session()

        history = MagicMock()
        history.db = db

        svc_a = make_agent_service(history_service=history, session_id="s-a")
        svc_b = make_agent_service(history_service=history, session_id="s-b")

        svc_a._save_conversation_message("user", "a1")
        svc_b._save_conversation_message("user", "b1")
        svc_a._save_conversation_message("assistant", "a2")
        svc_b._save_conversation_message("assistant", "b2")

        from src.api.models.conversation_message import ConversationMessage as ConvMsg
        rows_a = db.query(ConvMsg).filter(ConvMsg.session_id == "s-a").order_by(ConvMsg.sequence).all()
        rows_b = db.query(ConvMsg).filter(ConvMsg.session_id == "s-b").order_by(ConvMsg.sequence).all()
        assert [r.sequence for r in rows_a] == [1, 2]
        assert [r.sequence for r in rows_b] == [1, 2]
        db.close()

    def test_concurrent_saves_no_unique_conflict(self):
        """模擬併發：兩個 session 物件同時寫同一 session_id，不觸發 UNIQUE 衝突。

        使用同一引擎的兩個 Session（模擬 agent 線程 + synthetic 線程），
        交替寫入，驗證 sequence 持續遞增且無重複。
        """
        Session, engine = self._make_real_db()
        db1 = Session()
        db2 = Session()

        history1 = MagicMock()
        history1.db = db1
        history2 = MagicMock()
        history2.db = db2

        svc1 = make_agent_service(history_service=history1, session_id="s-concurrent")
        svc2 = make_agent_service(history_service=history2, session_id="s-concurrent")

        # 交替寫入
        svc1._save_conversation_message("user", "from-agent-1")
        svc2._save_conversation_message("user", "from-synthetic-1", is_synthetic=True)
        svc1._save_conversation_message("assistant", "from-agent-2")
        svc2._save_conversation_message("user", "from-synthetic-2", is_synthetic=True)

        # 用一個乾淨的 session 讀取驗證
        db_read = Session()
        from src.api.models.conversation_message import ConversationMessage as ConvMsg
        rows = (
            db_read.query(ConvMsg)
            .filter(ConvMsg.session_id == "s-concurrent")
            .order_by(ConvMsg.sequence)
            .all()
        )
        seqs = [r.sequence for r in rows]
        assert seqs == [1, 2, 3, 4], f"expected [1,2,3,4], got {seqs}"
        assert len(set(seqs)) == 4, "sequence 有重複"

        db1.close()
        db2.close()
        db_read.close()

    def test_save_handles_json_content(self):
        """content 為 dict/list 時自動序列化為 JSON 字符串"""
        Session, _ = self._make_real_db()
        db = Session()

        history = MagicMock()
        history.db = db

        service = make_agent_service(history_service=history, session_id="s-json")
        content = [{"type": "text", "text": "hello"}]
        service._save_conversation_message("user", content, round_id="r1")

        from src.api.models.conversation_message import ConversationMessage as ConvMsg
        row = db.query(ConvMsg).filter(ConvMsg.session_id == "s-json").first()
        import json as _json
        assert _json.loads(row.content) == content
        db.close()

    def test_save_error_rolls_back(self):
        """DB 異常時 rollback，不崩潰"""
        mock_db = MagicMock()
        mock_db.execute.side_effect = RuntimeError("disk full")

        history = MagicMock()
        history.db = mock_db

        service = make_agent_service(history_service=history, session_id="s-err")
        # 不應拋異常
        service._save_conversation_message("user", "boom")
        mock_db.rollback.assert_called_once()
