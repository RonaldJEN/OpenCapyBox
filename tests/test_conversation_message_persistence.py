"""Agent 上下文恢复测试（去重方案）

验证 _restore_history 从 agui_events 重建完整 messages（含 tool 交互），
不依赖 conversation_messages 新增列。agui_events 是 Agent 输出的单一事实源。
"""

import json
import threading
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

    def test_same_round_interaction_replaces_synthetic_pending_tool_result(self):
        from src.api.services.agent_service import AgentService

        events = [
            self._evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "ask_user"}, 1),
            self._evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"questions": []}'}, 2),
            self._evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 3),
            self._evt("CUSTOM", {
                "name": "interaction_requested",
                "value": {"interactionId": "i1", "toolCallId": "tc1"},
            }, 4),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 5),
            self._evt("CUSTOM", {
                "name": "interaction_resolved",
                "value": {
                    "interactionId": "i1",
                    "toolCallId": "tc1",
                    "toolResultContent": "Continue?: Yes",
                },
            }, 6),
        ]

        msgs = AgentService._events_to_messages(events, round_id="round-1")

        assert len(msgs) == 2
        assert msgs[0].role == "assistant"
        assert msgs[0].tool_calls[0].id == "tc1"
        assert msgs[1].role == "tool"
        assert msgs[1].tool_call_id == "tc1"
        assert msgs[1].content == "Continue?: Yes"

    def test_same_round_approval_uses_executed_tool_result(self):
        from src.api.services.agent_service import AgentService

        events = [
            self._evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "protected"}, 1),
            self._evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"value": 1}'}, 2),
            self._evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 3),
            self._evt("CUSTOM", {
                "name": "interaction_requested",
                "value": {
                    "interactionId": "i1",
                    "toolCallId": "tc1",
                    "kind": "tool_approval",
                },
            }, 4),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 5),
            self._evt("CUSTOM", {
                "name": "interaction_resolved",
                "value": {
                    "interactionId": "i1",
                    "toolCallId": "tc1",
                    "toolResultContent": "[Tool approval execution pending]",
                },
            }, 6),
            self._evt("CUSTOM", {
                "name": "tool_approval_resume",
                "value": {"toolCallId": "tc1"},
            }, 7),
            self._evt("TOOL_CALL_RESULT", {
                "toolCallId": "tc1",
                "content": "executed result",
            }, 8),
        ]

        msgs = AgentService._events_to_messages(events, round_id="round-1")

        assert len(msgs) == 2
        assert msgs[1].role == "tool"
        assert msgs[1].content == "executed result"

    def test_synthetic_user_custom_flushes_pending_tool_step(self):
        """工具后的 synthetic user 必须恢复在 assistant/tool 之后。"""
        from src.api.services.agent_service import AgentService

        image_content = [
            {"type": "text", "text": "tool image context"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,Zm9v"}},
        ]
        events = [
            self._evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "read_image_file"}, 1),
            self._evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"paths":["chart.png"]}'}, 2),
            self._evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 3),
            self._evt("TOOL_CALL_RESULT", {"toolCallId": "tc1", "content": "image loaded", "messageId": "m1"}, 4),
            self._evt("CUSTOM", {
                "name": "synthetic_user_message",
                "value": {
                    "schema": "synthetic_user_message_ref.v1",
                    "contentRef": "conversation_messages",
                    "contentKind": "blocks",
                    "blockCount": 2,
                    "imageCount": 1,
                },
            }, 5),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 6),
            self._evt("TEXT_MESSAGE_CONTENT", {"delta": "I can see the chart."}, 7),
            self._evt("TEXT_MESSAGE_END", {"messageId": "m2"}, 8),
            self._evt("STEP_FINISHED", {"stepName": "step-2"}, 9),
        ]

        msgs = AgentService._events_to_messages(events, synthetic_user_contents=[image_content])

        assert [msg.role for msg in msgs] == ["assistant", "tool", "user", "assistant"]
        assert msgs[0].tool_calls[0].function.name == "read_image_file"
        assert msgs[1].tool_call_id == "tc1"
        assert msgs[2].is_synthetic is True
        assert msgs[2].content == image_content
        assert msgs[3].content == "I can see the chart."

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

    def _make_round(
        self,
        round_id,
        session_id,
        user_message,
        created_at_order=0,
        status="completed",
        final_response=None,
        parent_run_id=None,
    ):
        rnd = MagicMock()
        rnd.id = round_id
        rnd.session_id = session_id
        rnd.user_message = user_message
        rnd.created_at = created_at_order
        rnd.status = status
        rnd.final_response = final_response
        rnd.parent_run_id = parent_run_id
        return rnd

    def _make_conv_msg(self, role, content, round_id, sequence, is_summary=False, is_synthetic=False):
        m = MagicMock()
        m.role = role
        m.content = content if isinstance(content, str) else json.dumps(content)
        m.round_id = round_id
        m.sequence = sequence
        m.is_summary = is_summary
        m.is_synthetic = is_synthetic
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

    def test_synthetic_user_custom_restores_in_event_order_without_duplicate(self):
        """冷恢复顺序应与热运行一致：tool result 后插入 synthetic image context。"""
        image_content = [
            {"type": "text", "text": "tool image context"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,Zm9v"}},
        ]
        rounds = [self._make_round("r1", "s1", "inspect chart")]
        user_msgs = [
            self._make_conv_msg("user", "inspect chart", "r1", 1),
            self._make_conv_msg("user", image_content, "r1", 2, is_synthetic=True),
        ]
        events = {
            "r1": [
                self._make_evt("TOOL_CALL_START", {"toolCallId": "tc1", "toolCallName": "read_image_file"}, 1),
                self._make_evt("TOOL_CALL_ARGS", {"toolCallId": "tc1", "delta": '{"paths":["chart.png"]}'}, 2),
                self._make_evt("TOOL_CALL_END", {"toolCallId": "tc1"}, 3),
                self._make_evt("TOOL_CALL_RESULT", {"toolCallId": "tc1", "content": "image loaded", "messageId": "m1"}, 4),
                self._make_evt("CUSTOM", {
                    "name": "synthetic_user_message",
                    "value": {
                        "schema": "synthetic_user_message_ref.v1",
                        "contentRef": "conversation_messages",
                        "contentKind": "blocks",
                        "blockCount": 2,
                        "imageCount": 1,
                    },
                }, 5),
                self._make_evt("STEP_FINISHED", {"stepName": "step-1"}, 6),
                self._make_evt("TEXT_MESSAGE_CONTENT", {"delta": "The chart trends upward."}, 7),
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m2"}, 8),
                self._make_evt("STEP_FINISHED", {"stepName": "step-2"}, 9),
            ],
        }

        mock_db = self._setup_db(rounds, user_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")
        messages = service._rebuild_messages_from_events()

        assert [msg.role for msg in messages] == ["user", "assistant", "tool", "user", "assistant"]
        assert messages[0].content == "inspect chart"
        assert messages[1].tool_calls[0].function.name == "read_image_file"
        assert messages[2].content == "image loaded"
        assert messages[3].is_synthetic is True
        assert messages[3].content == image_content
        assert messages[4].content == "The chart trends upward."

    def test_synthetic_user_without_custom_marker_is_not_rebuilt(self):
        """没有 CUSTOM marker 时，不兜底插入无序 synthetic user 消息。"""
        rounds = [self._make_round("r1", "s1", "hello")]
        user_msgs = [
            self._make_conv_msg("user", "hello", "r1", 1),
            self._make_conv_msg("user", "synthetic nudge", "r1", 2, is_synthetic=True),
        ]
        events = {
            "r1": [
                self._make_evt("TEXT_MESSAGE_CONTENT", {"delta": "answer"}, 1),
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 2),
                self._make_evt("STEP_FINISHED", {"stepName": "step-1"}, 3),
            ],
        }

        mock_db = self._setup_db(rounds, user_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")
        messages = service._rebuild_messages_from_events()

        assert [msg.role for msg in messages] == ["user", "assistant"]
        assert messages[0].content == "hello"
        assert messages[1].content == "answer"

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

    def test_fallback_to_final_response_when_assistant_text_events_missing(self):
        """completed round 缺少可恢复 assistant 文本事件时，用 final_response 补回上一轮答复"""
        rounds = [self._make_round("r1", "s1", "continue previous task", final_response="Previous final answer")]
        user_msgs = [self._make_conv_msg("user", "continue previous task", "r1", 1)]
        events = {
            "r1": [
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 1),
                self._make_evt("STEP_FINISHED", {"stepName": "step-1"}, 2),
            ],
        }

        mock_db = self._setup_db(rounds, user_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")
        messages = service._rebuild_messages_from_events()

        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert messages[1].content == "Previous final answer"

    def test_final_response_fallback_skips_failed_round(self):
        """failed/cancelled round 的 final_response 不作为下一轮语义上下文"""
        rounds = [self._make_round("r1", "s1", "continue previous task", status="failed", final_response="Failed")]
        user_msgs = [self._make_conv_msg("user", "continue previous task", "r1", 1)]
        events = {}

        mock_db = self._setup_db(rounds, user_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")
        messages = service._rebuild_messages_from_events()

        assert len(messages) == 1
        assert messages[0].role == "user"

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

    def test_restore_history_replaces_runtime_messages_without_duplication(self):
        """重复刷新 runtime messages 不应把同一段 DB 历史追加多次。"""
        service = make_agent_service()
        service.agent = MagicMock()
        system_message = AgentMessage(role="system", content="system prompt")
        service.agent.messages = [
            system_message,
            AgentMessage(role="user", content="stale hot-cache message"),
        ]
        service.agent._cached_token_count = 123
        service.agent._cached_message_count = 2

        fake_messages = [
            AgentMessage(role="user", content="测试收件人，请新发邮件"),
            AgentMessage(role="assistant", content="附件sample-report.xlsx 已确认"),
        ]

        mock_settings = MagicMock()
        mock_settings.agent_max_history_messages = 100

        with patch.object(service, "_rebuild_messages_from_events", return_value=fake_messages):
            with patch("src.api.config.get_settings", return_value=mock_settings):
                service._restore_history()
                service._restore_history()

        assert service.agent.messages == [system_message] + fake_messages
        assert [msg.content for msg in service.agent.messages].count("测试收件人，请新发邮件") == 1
        assert service.agent._cached_token_count == 0
        assert service.agent._cached_message_count == 0
        assert service._last_saved_index == 3

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

    def test_restore_history_trim_fallbacks_to_nearest_real_user_boundary(self, caplog):
        """尾窗無 user 邊界時，應回退到最近真實 user，避免整段失憶"""
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
        with caplog.at_level(logging.WARNING):
            with patch.object(service, "_rebuild_messages_from_events", return_value=fake_messages):
                with patch("src.api.config.get_settings", return_value=mock_settings):
                    service._restore_history()

        # 應回退到最近真實 user(q1) 而非整段丟棄
        assert len(service.agent.messages) == len(fake_messages)
        assert service.agent.messages[0].role == "user"
        assert service.agent.messages[0].content == "q1"
        assert any("回退到最近真實 user" in r.message for r in caplog.records)

    def test_restore_history_trim_keeps_tail_when_no_real_user_anywhere(self, caplog):
        """極端情況：全歷史無真實 user，至少保留尾窗避免全空"""
        service = make_agent_service()
        service.agent = MagicMock()
        service.agent.messages = []

        fake_messages = [
            AgentMessage(role="assistant", content="a1"),
            AgentMessage(role="tool", content="r1", tool_call_id="tc1", name="bash"),
            AgentMessage(role="assistant", content="a2"),
            AgentMessage(role="tool", content="r2", tool_call_id="tc2", name="bash"),
        ]

        mock_settings = MagicMock()
        mock_settings.agent_max_history_messages = 2

        import logging
        with caplog.at_level(logging.ERROR):
            with patch.object(service, "_rebuild_messages_from_events", return_value=fake_messages):
                with patch("src.api.config.get_settings", return_value=mock_settings):
                    service._restore_history()

        assert len(service.agent.messages) == 2
        assert service.agent.messages[0].content == "a2"
        assert service.agent.messages[1].content == "r2"
        assert any("不存在真實 user 邊界" in r.message for r in caplog.records)

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
    def _make_round(round_id, session_id, user_message, created_at_order=0, parent_run_id=None):
        rnd = MagicMock()
        rnd.id = round_id
        rnd.session_id = session_id
        rnd.user_message = user_message
        rnd.created_at = created_at_order
        rnd.status = "completed"
        rnd.final_response = None
        rnd.parent_run_id = parent_run_id
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

    def test_synthetic_user_messages_rebuild_at_custom_marker(self):
        """合成 user 消息必须通过 CUSTOM marker 恢复到事件顺序位置。"""
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
                self._make_evt("CUSTOM", {
                    "name": "synthetic_user_message",
                    "value": {
                        "schema": "synthetic_user_message_ref.v1",
                        "contentRef": "conversation_messages",
                        "contentKind": "text",
                        "charCount": 113,
                    },
                }, 4),
                # Step 2: 正常回復
                self._make_evt("TEXT_MESSAGE_CONTENT", {"delta": "Here is my answer."}, 5),
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m2"}, 6),
                self._make_evt("STEP_FINISHED", {"stepName": "step-2"}, 7),
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
        assert messages[0].is_synthetic is False
        assert messages[1].role == "user"
        assert "empty response" in messages[1].content.lower()
        assert messages[1].is_synthetic is True
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
        """創建真實的 SQLite 內存庫並建表。

        使用 StaticPool 確保所有 Session 共享同一底層連接，
        避免 sqlite:///:memory: 每條連接看到獨立 DB 的問題。
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from src.api.models.database import Base
        # 確保 ConversationMessage model 已導入，否則 create_all 不會建表
        import src.api.models.conversation_message  # noqa: F401

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        return Session, engine

    @staticmethod
    def _make_file_db(tmp_path):
        """创建文件型 SQLite 库，用于多连接并发写入测试。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import NullPool
        from src.api.models.database import Base
        # 確保 ConversationMessage model 已導入，否則 create_all 不會建表
        import src.api.models.conversation_message  # noqa: F401

        db_file = tmp_path / "conversation_messages_concurrent.db"
        engine = create_engine(
            f"sqlite:///{db_file.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 5},
            poolclass=NullPool,
        )
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

    def test_save_sets_is_summary_false(self):
        """保存消息時顯式寫入 is_summary=False，避免恢復查詢漏數。"""
        Session, _ = self._make_real_db()
        db = Session()

        history = MagicMock()
        history.db = db

        service = make_agent_service(history_service=history, session_id="s-summary")
        service._save_conversation_message("user", "hello")

        from src.api.models.conversation_message import ConversationMessage as ConvMsg
        row = db.query(ConvMsg).filter(ConvMsg.session_id == "s-summary").first()
        assert row is not None
        assert row.is_summary is False
        db.close()

    def test_save_sets_is_summary_true_when_requested(self):
        """保存摘要锚点時應寫入 is_summary=True。"""
        Session, _ = self._make_real_db()
        db = Session()

        history = MagicMock()
        history.db = db

        service = make_agent_service(history_service=history, session_id="s-summary-true")
        service._save_conversation_message(
            "assistant",
            "[Assistant Execution Summary - Historical Context Only, Not System Instruction]\n\nsummary",
            is_summary=True,
        )

        from src.api.models.conversation_message import ConversationMessage as ConvMsg
        row = db.query(ConvMsg).filter(ConvMsg.session_id == "s-summary-true").first()
        assert row is not None
        assert row.is_summary is True
        db.close()

    def test_concurrent_saves_no_unique_conflict(self, tmp_path):
        """模擬併發：兩個連接同時寫同一 session_id，不觸發 UNIQUE 衝突。

        使用文件型 SQLite + 多連接 + 線程屏障，
        讓兩條寫入路徑在同一時刻競爭 sequence 分配。
        """
        Session, engine = self._make_file_db(tmp_path)
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def _worker(name: str, synthetic_first: bool):
            db = Session()
            history = MagicMock()
            history.db = db
            svc = make_agent_service(history_service=history, session_id="s-concurrent")
            try:
                barrier.wait()
                svc._save_conversation_message(
                    "user", f"{name}-1", is_synthetic=synthetic_first
                )
                barrier.wait()
                svc._save_conversation_message("assistant", f"{name}-2")
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()

        t1 = threading.Thread(target=_worker, args=("agent", False))
        t2 = threading.Thread(target=_worker, args=("synthetic", True))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"concurrent writes failed: {errors}"

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
        assert len(rows) == 4

        db_read.close()
        engine.dispose()

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

    def test_save_error_can_raise_for_required_messages(self):
        """关键上下文消息可选择强制失败，避免后续锚点先落库。"""
        mock_db = MagicMock()
        mock_db.execute.side_effect = RuntimeError("disk full")

        history = MagicMock()
        history.db = mock_db

        service = make_agent_service(history_service=history, session_id="s-err")
        with pytest.raises(RuntimeError, match="disk full"):
            service._save_conversation_message(
                "user",
                "boom",
                raise_on_error=True,
            )
        mock_db.rollback.assert_called_once()


# ============================================================
# _events_to_messages 诊断日志 + dict payload 兼容
# ============================================================

class TestEventsToMessagesDiagnostics:
    """验证 _events_to_messages 的诊断日志与 dict payload 支持"""

    @staticmethod
    def _evt(event_type: str, payload, sequence: int = 0):
        e = MagicMock()
        if isinstance(payload, dict):
            payload["type"] = event_type
        e.payload = payload
        e.sequence = sequence
        return e

    def test_malformed_payload_logs_warning(self, caplog):
        """畸形 payload 应记录 warning 而非静默跳过"""
        from src.api.services.agent_service import AgentService
        import logging

        bad_evt = MagicMock()
        bad_evt.payload = "NOT_JSON{{{"
        bad_evt.sequence = 1

        with caplog.at_level(logging.WARNING):
            msgs = AgentService._events_to_messages([bad_evt], round_id="r-bad")

        assert msgs == []
        assert any("payload 解析失敗" in r.message for r in caplog.records)

    def test_dict_payload_accepted(self):
        """payload 已是 dict 时应直接使用而非 json.loads"""
        from src.api.services.agent_service import AgentService

        events = [
            self._evt("TEXT_MESSAGE_CONTENT", {"delta": "dict works"}, 1),
            self._evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 2),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 3),
        ]
        # payload is already a dict
        for e in events:
            assert isinstance(e.payload, dict)

        msgs = AgentService._events_to_messages(events, round_id="r-dict")
        assert len(msgs) == 1
        assert msgs[0].content == "dict works"

    def test_skipped_count_in_log(self, caplog):
        """多条畸形事件时，summary 日志报告总跳过数"""
        from src.api.services.agent_service import AgentService
        import logging

        events = [
            self._evt("TEXT_MESSAGE_CONTENT", "BAD1{{{", 1),
            self._evt("TEXT_MESSAGE_CONTENT", "BAD2{{{", 2),
            self._evt("TEXT_MESSAGE_CONTENT", {"delta": "ok"}, 3),
            self._evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 4),
            self._evt("STEP_FINISHED", {"stepName": "step-1"}, 5),
        ]

        with caplog.at_level(logging.WARNING):
            msgs = AgentService._events_to_messages(events, round_id="r-multi-bad")

        assert len(msgs) == 1
        assert msgs[0].content == "ok"
        summary_logs = [r for r in caplog.records if "2/5" in r.message]
        assert len(summary_logs) == 1


# ============================================================
# _rebuild_messages_from_events Level-2 fallback 测试
# ============================================================

class TestRebuildFallbackToConversationMessages:
    """验证当 events 无法重建 assistant 且 final_response 为空时，
    fallback 到 conversation_messages 表的 assistant 记录"""

    @staticmethod
    def _make_round(round_id, session_id, user_message, status="completed", final_response=None, parent_run_id=None):
        rnd = MagicMock()
        rnd.id = round_id
        rnd.session_id = session_id
        rnd.user_message = user_message
        rnd.created_at = 0
        rnd.status = status
        rnd.final_response = final_response
        rnd.parent_run_id = parent_run_id
        return rnd

    @staticmethod
    def _make_conv_msg(role, content, round_id, sequence, is_summary=False):
        m = MagicMock()
        m.role = role
        m.content = content if isinstance(content, str) else json.dumps(content)
        m.round_id = round_id
        m.sequence = sequence
        m.is_summary = is_summary
        return m

    @staticmethod
    def _make_evt(event_type, payload_dict, sequence):
        e = MagicMock()
        payload_dict["type"] = event_type
        e.payload = json.dumps(payload_dict)
        e.sequence = sequence
        return e

    def _setup_db(self, rounds, conv_msgs, events_by_round):
        """构建 mock db，conv_msgs 包含 user + assistant"""
        from src.api.models.round import Round
        from src.api.models.conversation_message import ConversationMessage
        from src.api.models.agui_event import AGUIEventLog

        mock_db = MagicMock()

        rounds_query = MagicMock()
        rounds_query.filter.return_value.order_by.return_value.all.return_value = rounds

        conv_query = MagicMock()
        conv_query.filter.return_value.order_by.return_value.all.return_value = conv_msgs

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

    def test_fallback_l2_to_conversation_messages(self, caplog):
        """events 无 assistant 文本 + final_response=None → fallback 到 conv_msgs assistant"""
        import logging

        rounds = [self._make_round("r1", "s1", "hello")]
        conv_msgs = [
            self._make_conv_msg("user", "hello", "r1", 1),
            self._make_conv_msg("assistant", "world from conv_msgs", "r1", 2),
        ]
        # 空事件（或只有 END 事件，不产生文本）
        events = {"r1": []}

        mock_db = self._setup_db(rounds, conv_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")

        with caplog.at_level(logging.WARNING):
            messages = service._rebuild_messages_from_events()

        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[0].content == "hello"
        assert messages[1].role == "assistant"
        assert messages[1].content == "world from conv_msgs"
        assert any("fallback-L2" in r.message for r in caplog.records)

    def test_fallback_l1_preferred_over_l2(self, caplog):
        """final_response 存在时优先用 L1 fallback"""
        import logging

        rounds = [self._make_round("r1", "s1", "hello", final_response="from final_response")]
        conv_msgs = [
            self._make_conv_msg("user", "hello", "r1", 1),
            self._make_conv_msg("assistant", "from conv_msgs", "r1", 2),
        ]
        events = {"r1": []}

        mock_db = self._setup_db(rounds, conv_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")

        with caplog.at_level(logging.WARNING):
            messages = service._rebuild_messages_from_events()

        assert len(messages) == 2
        assert messages[1].role == "assistant"
        assert messages[1].content == "from final_response"
        assert any("fallback-L1" in r.message for r in caplog.records)
        assert not any("fallback-L2" in r.message for r in caplog.records)

    def test_no_fallback_when_events_have_text(self):
        """events 正常重建 assistant 文本时不触发任何 fallback"""
        rounds = [self._make_round("r1", "s1", "hello")]
        conv_msgs = [
            self._make_conv_msg("user", "hello", "r1", 1),
        ]
        events = {
            "r1": [
                self._make_evt("TEXT_MESSAGE_CONTENT", {"delta": "normal reply"}, 1),
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 2),
                self._make_evt("STEP_FINISHED", {"stepName": "step-1"}, 3),
            ],
        }

        mock_db = self._setup_db(rounds, conv_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")
        messages = service._rebuild_messages_from_events()

        assert len(messages) == 2
        assert messages[1].content == "normal reply"

    def test_multi_round_mixed_fallback(self, caplog):
        """多 round 场景：R1 正常，R2 需 L2 fallback"""
        import logging

        rounds = [
            self._make_round("r1", "s1", "q1"),
            self._make_round("r2", "s1", "q2"),
        ]
        conv_msgs = [
            self._make_conv_msg("user", "q1", "r1", 1),
            self._make_conv_msg("user", "q2", "r2", 3),
            self._make_conv_msg("assistant", "a2 from conv", "r2", 4),
        ]
        events = {
            "r1": [
                self._make_evt("TEXT_MESSAGE_CONTENT", {"delta": "a1 from events"}, 1),
                self._make_evt("TEXT_MESSAGE_END", {"messageId": "m1"}, 2),
                self._make_evt("STEP_FINISHED", {"stepName": "step-1"}, 3),
            ],
            "r2": [],  # 事件缺失
        }

        mock_db = self._setup_db(rounds, conv_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")

        with caplog.at_level(logging.WARNING):
            messages = service._rebuild_messages_from_events()

        assert len(messages) == 4
        assert messages[0].content == "q1"
        assert messages[1].content == "a1 from events"
        assert messages[2].content == "q2"
        assert messages[3].content == "a2 from conv"
        assert any("fallback-L2" in r.message for r in caplog.records)

    def test_failed_round_no_fallback(self):
        """failed round 不应触发 fallback（只有 completed round 需要）"""
        rounds = [self._make_round("r1", "s1", "q1", status="failed", final_response="error msg")]
        conv_msgs = [
            self._make_conv_msg("user", "q1", "r1", 1),
            self._make_conv_msg("assistant", "should not appear", "r1", 2),
        ]
        events = {"r1": []}

        mock_db = self._setup_db(rounds, conv_msgs, events)
        history_service = MagicMock()
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service, session_id="s1")
        messages = service._rebuild_messages_from_events()

        assert len(messages) == 1
        assert messages[0].role == "user"
