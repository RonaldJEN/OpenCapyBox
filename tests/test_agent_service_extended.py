"""AgentService（Sandbox 版）測試"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace
from src.agent.schema.agui_events import (
    TextMessageContentEvent,
    TextMessageEndEvent,
    StepFinishedEvent,
    RunFinishedEvent,
    CustomEvent,
    ToolCallStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    EventType,
)
from src.agent.schema import Message as AgentHistoryMessage
from tests.helpers import (
    make_mock_sandbox, make_agent_service, MockLLMClient, MockRegistry,
    make_mock_agent, make_tool_call_agui_events,
)


class TestAgentServiceInit:
    def test_service_initialization(self):
        sandbox = make_mock_sandbox()
        service = make_agent_service(sandbox=sandbox)

        assert service.sandbox is sandbox
        assert service.session_id == "session-123"
        assert service.agent is None
        assert service._last_saved_index == 0


class TestAgentServiceCreateTools:
    @pytest.fixture
    def service(self):
        return make_agent_service()

    @pytest.mark.asyncio
    async def test_create_tools_basic(self, service):
        with patch("src.api.services.tool_factory.settings") as mock_settings:
            mock_settings.bocha_search_appcode = None
            mock_settings.skills_dir = ""
            from src.api.services.tool_factory import create_agent_tools
            from src.api.services.sandbox_service import get_sandbox_mount_path
            tools, _ = await create_agent_tools(
                sandbox=service.sandbox,
                workspace_dir=service._workspace_dir,
                mount=get_sandbox_mount_path(),
                user_id=service.user_id,
                db_session_factory=service._get_db_session_factory(),
            )

        tool_names = [t.name for t in tools]
        assert "read_file" in tool_names
        assert "read_image_file" in tool_names
        assert "write_file" in tool_names
        assert "edit_file" in tool_names
        assert "bash" in tool_names
        assert "bash_output" in tool_names
        assert "bash_kill" in tool_names
        assert "sub_agent" in tool_names
        assert "record_note" in tool_names

    @pytest.mark.asyncio
    async def test_create_tools_passes_image_capability(self, service):
        with patch("src.api.services.tool_factory.settings") as mock_settings:
            mock_settings.bocha_search_appcode = None
            mock_settings.skills_dir = ""
            from src.api.services.tool_factory import create_agent_tools
            from src.api.services.sandbox_service import get_sandbox_mount_path
            tools, _ = await create_agent_tools(
                sandbox=service.sandbox,
                workspace_dir=service._workspace_dir,
                mount=get_sandbox_mount_path(),
                user_id=service.user_id,
                db_session_factory=service._get_db_session_factory(),
                supports_image=True,
                max_images=3,
            )

        image_tool = next(t for t in tools if t.name == "read_image_file")
        assert image_tool._supports_image is True
        assert image_tool._model_max_images == 3

    @pytest.mark.asyncio
    async def test_partial_mcp_catalog_does_not_request_immediate_agent_retry(
        self,
        service,
    ):
        runtime = MagicMock()
        runtime.resolve_catalog = AsyncMock(return_value=SimpleNamespace(
            fingerprint="refresh-bucket-7",
            tools=(),
            errors=("optional server offline",),
        ))
        metadata = {}
        with (
            patch("src.api.services.tool_factory.settings") as mock_settings,
            patch(
                "src.api.services.tool_factory.get_mcp_runtime",
                return_value=runtime,
            ),
        ):
            mock_settings.bocha_search_appcode = None
            mock_settings.skills_dir = ""
            mock_settings.sandbox_background_command_timeout_seconds = 21600
            from src.api.services.tool_factory import create_agent_tools
            from src.api.services.sandbox_service import get_sandbox_mount_path

            await create_agent_tools(
                sandbox=service.sandbox,
                workspace_dir=service._workspace_dir,
                mount=get_sandbox_mount_path(),
                user_id=service.user_id,
                db_session_factory=service._get_db_session_factory(),
                build_metadata=metadata,
            )

        assert metadata["mcp_catalog_fingerprint"] == "refresh-bucket-7"
        assert metadata["mcp_catalog_retry_required"] is False

    @pytest.mark.asyncio
    async def test_skill_config_query_failure_keeps_skill_metadata_and_tool_available(
        self, service, tmp_path
    ):
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "pdf"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: PDF documents\n---\nUse PDF guidance.\n",
            encoding="utf-8",
        )
        failing_db = MagicMock()
        failing_db.query.side_effect = RuntimeError("skill config database unavailable")
        db_session_factory = MagicMock(return_value=failing_db)
        sandbox_service = MagicMock()
        sandbox_service.discover_sandbox_skills = AsyncMock(return_value=[])

        async def _push_skill(*_args, enabled_check=None, **_kwargs):
            assert enabled_check is not None
            assert enabled_check() is True
            return True

        sandbox_service.push_skill = AsyncMock(side_effect=_push_skill)

        with patch("src.api.services.tool_factory.settings") as mock_settings, patch(
            "src.api.services.tool_factory.get_sandbox_service",
            return_value=sandbox_service,
        ):
            mock_settings.bocha_search_appcode = None
            mock_settings.skills_dir = str(skills_dir)
            mock_settings.sandbox_background_command_timeout_seconds = 21600
            from src.api.services.tool_factory import create_agent_tools

            tools, loader = await create_agent_tools(
                sandbox=service.sandbox,
                workspace_dir=service._workspace_dir,
                mount="/home/user",
                user_id=service.user_id,
                db_session_factory=db_session_factory,
            )
            skill_tool = next(tool for tool in tools if tool.name == "get_skill")
            result = await skill_tool.execute(skill_name="pdf")
            unknown_result = await skill_tool.execute(skill_name="not-installed")

        assert loader is not None
        assert "`pdf`" in loader.get_skills_metadata_prompt()
        assert result.success is True
        assert "Use PDF guidance" in result.content
        assert unknown_result.success is False
        assert sandbox_service.push_skill.await_count == 1

    @pytest.mark.asyncio
    async def test_user_skill_disabled_during_read_is_not_cached_or_returned(
        self, service, tmp_path
    ):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        state = {"disabled": False}
        disabled_record = SimpleNamespace(skill_name="my-skill", enabled=False)

        def db_session_factory():
            db = MagicMock()
            db.query.return_value.filter.return_value.all.return_value = (
                [disabled_record] if state["disabled"] else []
            )
            return db

        sandbox_service = MagicMock()
        sandbox_service.discover_sandbox_skills = AsyncMock(return_value=[{
            "name": "my-skill",
            "description": "User skill",
            "sandbox_skill_dir": "/home/user/skills/my-skill",
        }])

        async def _read_and_disable(*_args, **_kwargs):
            state["disabled"] = True
            return "User-only guidance"

        sandbox_service.read_sandbox_skill_content = AsyncMock(
            side_effect=_read_and_disable
        )

        with patch("src.api.services.tool_factory.settings") as mock_settings, patch(
            "src.api.services.tool_factory.get_sandbox_service",
            return_value=sandbox_service,
        ):
            mock_settings.bocha_search_appcode = None
            mock_settings.skills_dir = str(skills_dir)
            mock_settings.sandbox_background_command_timeout_seconds = 21600
            from src.api.services.tool_factory import create_agent_tools

            tools, loader = await create_agent_tools(
                sandbox=service.sandbox,
                workspace_dir=service._workspace_dir,
                mount="/home/user",
                user_id=service.user_id,
                db_session_factory=db_session_factory,
            )
            skill_tool = next(tool for tool in tools if tool.name == "get_skill")
            result = await skill_tool.execute(skill_name="my-skill")

        assert loader is not None
        assert result.success is False
        assert "my-skill" in loader.disabled_skill_names
        assert loader.sandbox_skills["my-skill"].content == ""


class TestAgentServiceRestoreHistory:
    def test_restore_history_empty_gives_empty_messages(self):
        """conversation_messages 为空时 agent.messages 保持为空（无 fallback）"""
        history_service = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service)
        service.agent = MagicMock()
        service.agent.messages = []

        service._restore_history()

        assert len(service.agent.messages) == 0
        history_service.reset_session.assert_called_once()

    def test_restore_history_no_agent(self):
        service = make_agent_service()
        service._restore_history()  # should not raise

    def test_load_persisted_interrupt_releases_read_transaction(self):
        history_service = MagicMock()
        mock_db = MagicMock()
        round_row = MagicMock()
        round_row.id = "round-1"
        round_row.interrupt_payload = '{"id":"interrupt-1","payload":{"tool_call_id":"tc-1","questions":[{"text":"ok?"}]}}'
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [round_row]
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service)
        service.session_id = "session-123"

        result = service._load_persisted_interrupt("interrupt-1")

        assert result == {
            "interrupt_id": "interrupt-1",
            "round_id": "round-1",
            "tool_call_id": "tc-1",
            "questions": [{"text": "ok?"}],
        }
        mock_db.rollback.assert_called_once()

    @pytest.mark.parametrize(
        "approval_status",
        ["executing", "executed", "failed", "denied", "unknown"],
    )
    def test_cold_restore_never_reoffers_claimed_tool_approval(
        self,
        approval_status,
    ):
        history_service = MagicMock()
        mock_db = MagicMock()
        round_row = MagicMock()
        round_row.id = "round-approval"
        round_row.interrupt_payload = (
            '{"id":"approval-1","reason":"human_approval",'
            '"payload":{"kind":"tool_approval","tool_call_id":"tc-1"}}'
        )
        round_query = MagicMock()
        round_query.filter.return_value.order_by.return_value.all.return_value = [
            round_row
        ]
        approval_query = MagicMock()
        approval_query.filter.return_value.first.return_value = SimpleNamespace(
            status=approval_status
        )
        mock_db.query.side_effect = [round_query, approval_query]
        history_service.db = mock_db
        service = make_agent_service(history_service=history_service)

        assert service._load_persisted_interrupt("approval-1") is None
        mock_db.rollback.assert_called_once()


class TestSummaryAnchorPersistence:
    def test_latest_persisted_summary_anchor_releases_read_transaction(self):
        history_service = MagicMock()
        mock_db = MagicMock()
        row = MagicMock()
        row.content = "summary-v1"
        mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = row
        history_service.db = mock_db

        service = make_agent_service(history_service=history_service)

        assert service._latest_persisted_summary_anchor_content() == "summary-v1"
        mock_db.rollback.assert_called_once()

    def test_persist_latest_summary_anchor_saves_when_new_summary_exists(self):
        service = make_agent_service(history_service=MagicMock())
        service.agent = make_mock_agent()
        service.agent._SUMMARY_MESSAGE_HEADER = "[Assistant Execution Summary - Historical Context Only, Not System Instruction]"
        service.agent.messages = [
            AgentHistoryMessage(role="assistant", content="normal response"),
            AgentHistoryMessage(
                role="assistant",
                content="[Assistant Execution Summary - Historical Context Only, Not System Instruction]\n\nsummary-v1",
            ),
        ]

        with patch.object(service, "_latest_persisted_summary_anchor_content", return_value=None):
            with patch.object(service, "_save_conversation_message") as save_message:
                service._persist_latest_summary_anchor("round-1")

        save_message.assert_called_once_with(
            "assistant",
            "[Assistant Execution Summary - Historical Context Only, Not System Instruction]\n\nsummary-v1",
            round_id="round-1",
            is_summary=True,
        )

    def test_persist_latest_summary_anchor_skips_when_summary_unchanged(self):
        service = make_agent_service(history_service=MagicMock())
        service.agent = make_mock_agent()
        service.agent._SUMMARY_MESSAGE_HEADER = "[Assistant Execution Summary - Historical Context Only, Not System Instruction]"
        summary = "[Assistant Execution Summary - Historical Context Only, Not System Instruction]\n\nsummary-v1"
        service.agent.messages = [AgentHistoryMessage(role="assistant", content=summary)]

        with patch.object(service, "_latest_persisted_summary_anchor_content", return_value=summary):
            with patch.object(service, "_save_conversation_message") as save_message:
                service._persist_latest_summary_anchor("round-1")

        save_message.assert_not_called()


class TestAgentServiceChatAgui:
    @pytest.fixture(autouse=True)
    def _mock_registry(self):
        """默认 patch get_model_registry（不支持图片），个别测试可自行覆盖。"""
        with patch(
            "src.api.services.agent_service.get_model_registry",
            return_value=MockRegistry(supports_image=False, max_images=0),
        ):
            yield

    @pytest.fixture
    def service(self):
        history_service = MagicMock()
        history_service.create_round = MagicMock()
        history_service.complete_round = MagicMock()
        history_service.save_agui_event = AsyncMock()
        history_service.save_llm_call_record = AsyncMock()
        history_service.resolve_interrupted_rounds = MagicMock(return_value=1)

        service = make_agent_service(history_service=history_service)

        async def _run_agui(**kwargs):
            yield TextMessageContentEvent(messageId="m1", delta="Hello")
            yield TextMessageEndEvent(messageId="m1")
            yield StepFinishedEvent(stepName="step-1")
            yield RunFinishedEvent(threadId="session-123", runId=kwargs["run_id"], outcome="success")

        service.agent = make_mock_agent(run_agui_fn=_run_agui)
        service.model_id = "mock-model"
        service._build_restored_history_messages = MagicMock(return_value=[])
        return service

    @pytest.mark.asyncio
    async def test_chat_agui_clears_interrupted_rounds_when_skipping_pending_interrupt(self, service):
        service.agent._pending_interrupt = {
            "interrupt_id": "iid-1",
            "tool_call_id": "tc-1",
            "questions": [{"question": "Q?"}],
        }

        async for _ in service.chat_agui([
            {"type": "text", "text": "new request"},
        ]):
            pass

        service.agent.clear_pending_interrupt.assert_called_once()
        service.history_service.resolve_interrupted_rounds.assert_called_once_with("session-123")

    @pytest.mark.asyncio
    async def test_chat_agui_basic(self, service):
        events = []
        async for event in service.chat_agui([
            {"type": "text", "text": "hello"},
        ]):
            events.append(event)

        assert len(events) == 4
        service.history_service.create_round.assert_called_once()
        service.history_service.complete_round.assert_called_once()
        assert service.history_service.save_agui_event.await_count == 3
        complete_kwargs = service.history_service.complete_round.call_args.kwargs
        assert complete_kwargs["terminal_event"].type == EventType.RUN_FINISHED

    @pytest.mark.asyncio
    async def test_run_round_stream_synthetic_user_event_is_lightweight(self):
        data_url = "data:image/png;base64," + "A" * 1024
        image_content = [
            {"type": "text", "text": "tool image context"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        history_service = MagicMock()
        history_service.is_round_terminal = MagicMock(return_value=False)
        history_service.save_agui_event = AsyncMock(return_value=None)
        history_service.complete_round = MagicMock()
        history_service.reset_session = MagicMock()
        history_service.last_terminal_event = None

        service = make_agent_service(history_service=history_service)
        service._save_conversation_message = MagicMock()

        async def _run_agui(**kwargs):
            yield CustomEvent(
                name="synthetic_user_message",
                value={"content": image_content},
            )
            yield RunFinishedEvent(
                threadId="session-123",
                runId=kwargs["run_id"],
                outcome="interrupt",
                result={"reason": "max_steps_reached"},
            )

        service.agent = make_mock_agent(run_agui_fn=_run_agui)

        events = []
        async for event in service._run_round_stream(
            run_id="round-synthetic",
            user_message="inspect image",
        ):
            events.append(event)

        saved_custom_event = history_service.save_agui_event.await_args.args[1]
        assert saved_custom_event.type == EventType.CUSTOM
        assert saved_custom_event.value["contentRef"] == "conversation_messages"
        assert saved_custom_event.value["contentKind"] == "blocks"
        assert saved_custom_event.value["imageCount"] == 1
        assert "content" not in saved_custom_event.value
        assert data_url not in str(saved_custom_event.value)

        yielded_custom_event = events[0]
        assert yielded_custom_event.value == saved_custom_event.value
        service._save_conversation_message.assert_called_once_with(
            "user",
            image_content,
            round_id="round-synthetic",
            is_synthetic=True,
            raise_on_error=True,
        )

    @pytest.mark.asyncio
    async def test_run_round_stream_synthetic_content_failure_skips_marker(self):
        image_content = [
            {"type": "text", "text": "tool image context"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]
        history_service = MagicMock()
        history_service.is_round_terminal = MagicMock(return_value=False)
        history_service.save_agui_event = AsyncMock(return_value=None)
        history_service.complete_round = MagicMock()
        history_service.reset_session = MagicMock()
        history_service.last_terminal_event = None

        service = make_agent_service(history_service=history_service)
        service._save_conversation_message = MagicMock(
            side_effect=RuntimeError("conversation write failed"),
        )

        async def _run_agui(**kwargs):
            yield CustomEvent(
                name="synthetic_user_message",
                value={"content": image_content},
            )
            yield RunFinishedEvent(
                threadId="session-123",
                runId=kwargs["run_id"],
                outcome="interrupt",
                result={"reason": "max_steps_reached"},
            )

        service.agent = make_mock_agent(run_agui_fn=_run_agui)

        with pytest.raises(RuntimeError, match="conversation write failed"):
            async for _event in service._run_round_stream(
                run_id="round-synthetic-fail",
                user_message="inspect image",
            ):
                pass

        history_service.save_agui_event.assert_not_awaited()
        complete_kwargs = history_service.complete_round.call_args.kwargs
        assert complete_kwargs["status"] == "failed"

    @pytest.mark.asyncio
    async def test_chat_agui_max_steps_reached_is_terminal_status(self, service):
        final_text = "已达到最大步数限制（3 步），本轮执行被自动中止。"

        async def _run_agui(**kwargs):
            yield StepFinishedEvent(stepName="step-1")
            yield RunFinishedEvent(
                threadId="session-123",
                runId=kwargs["run_id"],
                outcome="interrupt",
                result={
                    "reason": "max_steps_reached",
                    "finalResponse": final_text,
                },
            )

        service.agent = make_mock_agent(run_agui_fn=_run_agui)

        async for _event in service.chat_agui([
            {"type": "text", "text": "loop forever"},
        ]):
            pass

        complete_kwargs = service.history_service.complete_round.call_args.kwargs
        assert complete_kwargs["status"] == "max_steps_reached"
        assert complete_kwargs["final_response"] == final_text
        assert complete_kwargs["terminal_event"].result["reason"] == "max_steps_reached"

    @pytest.mark.asyncio
    async def test_chat_agui_persists_llm_call_record(self, service):
        class HookAwareAgent:
            def __init__(self):
                self.messages = []
                self._pending_interrupt = None
                self._llm_call_hook = None
                self.last_llm_usage = None

            def has_pending_interrupt(self):
                return False

            def clear_pending_interrupt(self):
                return None

            def add_user_message(self, content):
                self.messages.append(AgentHistoryMessage(role="user", content=content))

            def set_llm_call_hook(self, hook):
                self._llm_call_hook = hook

            async def run_agui(self, **kwargs):
                await self._llm_call_hook(
                    {
                        "step_index": 1,
                        "request_messages": [{"role": "system", "content": "s"}],
                        "request_tools": ["read_file"],
                        "response_content": "Hello",
                        "response_thinking": None,
                        "response_tool_calls": None,
                        "response_error": None,
                        "finish_reason": "stop",
                        "usage_prompt_tokens": 10,
                        "usage_completion_tokens": 3,
                        "usage_total_tokens": 13,
                        "first_token_latency_s": 0.077,
                        "completion_latency_s": 0.333,
                        "compaction_triggered": True,
                        "compaction_pre_tokens": 81234,
                        "compaction_post_tokens": 52345,
                        "compaction_tokens_saved": 28889,
                        "compaction_microcompact_compacted_messages": 3,
                        "compaction_summary_generated_count": 2,
                        "compaction_summary_reused_count": 1,
                        "compaction_summary_quality_repair_count": 1,
                        "compaction_emergency_truncate_dropped_rounds": 0,
                    }
                )
                yield TextMessageContentEvent(messageId="m1", delta="Hello")
                yield TextMessageEndEvent(messageId="m1")
                yield StepFinishedEvent(stepName="step-1")
                yield RunFinishedEvent(threadId="session-123", runId=kwargs["run_id"], outcome="success")

        service.agent = HookAwareAgent()

        async for _ in service.chat_agui([
            {"type": "text", "text": "hello"},
        ]):
            pass

        created_round_id = service.history_service.create_round.call_args.kwargs["round_id"]
        save_kwargs = service.history_service.save_llm_call_record.await_args.kwargs
        assert save_kwargs["session_id"] == "session-123"
        assert save_kwargs["round_id"] == created_round_id
        assert save_kwargs["step_index"] == 1
        assert save_kwargs["response_content"] == "Hello"
        assert save_kwargs["usage_total_tokens"] == 13
        assert save_kwargs["first_token_latency_s"] == 0.077
        assert save_kwargs["completion_latency_s"] == 0.333
        assert save_kwargs["compaction_triggered"] is True
        assert save_kwargs["compaction_pre_tokens"] == 81234
        assert save_kwargs["compaction_post_tokens"] == 52345
        assert save_kwargs["compaction_tokens_saved"] == 28889
        assert save_kwargs["compaction_microcompact_compacted_messages"] == 3
        assert save_kwargs["compaction_summary_generated_count"] == 2
        assert save_kwargs["compaction_summary_reused_count"] == 1
        assert save_kwargs["compaction_summary_quality_repair_count"] == 1
        assert save_kwargs["compaction_emergency_truncate_dropped_rounds"] == 0

    @pytest.mark.asyncio
    async def test_chat_agui_refreshes_history_before_llm_request_snapshot(self, service):
        class HookAwareAgent:
            def __init__(self):
                self._SUMMARY_MESSAGE_HEADER = "[Assistant Execution Summary - Historical Context Only, Not System Instruction]"
                self.messages = [
                    AgentHistoryMessage(role="system", content="system prompt"),
                    AgentHistoryMessage(role="assistant", content="旧草稿：示例联系人，旧附件"),
                ]
                self._pending_interrupt = None
                self._llm_call_hook = None
                self.last_llm_usage = None

            def has_pending_interrupt(self):
                return False

            def clear_pending_interrupt(self):
                return None

            def add_user_message(self, content):
                self.messages.append(AgentHistoryMessage(role="user", content=content))

            def set_llm_call_hook(self, hook):
                self._llm_call_hook = hook

            async def run_agui(self, **kwargs):
                await self._llm_call_hook(
                    {
                        "step_index": 1,
                        "request_messages": [msg.model_dump(exclude_none=True) for msg in self.messages],
                        "request_tools": ["send_email"],
                        "response_content": "已发送",
                        "response_thinking": None,
                        "response_tool_calls": None,
                        "response_error": None,
                        "finish_reason": "stop",
                        "usage_prompt_tokens": 10,
                        "usage_completion_tokens": 3,
                        "usage_total_tokens": 13,
                        "first_token_latency_s": 0.077,
                        "completion_latency_s": 0.333,
                    }
                )
                yield RunFinishedEvent(threadId="session-123", runId=kwargs["run_id"], outcome="success")

        service.agent = HookAwareAgent()
        service._build_restored_history_messages = MagicMock(return_value=[
            AgentHistoryMessage(role="user", content="请改为新发邮件，正文称呼测试收件人"),
            AgentHistoryMessage(
                role="assistant",
                content="确认：附件sample-report.xlsx，称呼测试收件人，新发邮件",
            ),
        ])

        async for _ in service.chat_agui([
            {"type": "text", "text": "你发吧"},
        ]):
            pass

        request_messages = service.history_service.save_llm_call_record.await_args.kwargs["request_messages"]
        request_text = str(request_messages)
        assert "测试收件人" in request_text
        assert "sample-report.xlsx" in request_text
        assert "新发邮件" in request_text
        assert "你发吧" in request_text
        assert "旧草稿" not in request_text

    @pytest.mark.asyncio
    async def test_chat_agui_llm_call_record_failure_does_not_fail_run(self, service):
        """LLM 调用快照 DB 写失败属于 telemetry，不应中断主 Agent run。"""
        from sqlalchemy.exc import OperationalError

        class HookAwareAgent:
            def __init__(self):
                self.messages = []
                self._pending_interrupt = None
                self._llm_call_hook = None
                self.last_llm_usage = None

            def has_pending_interrupt(self):
                return False

            def clear_pending_interrupt(self):
                return None

            def add_user_message(self, content):
                self.messages.append(AgentHistoryMessage(role="user", content=content))

            def set_llm_call_hook(self, hook):
                self._llm_call_hook = hook

            async def run_agui(self, **kwargs):
                await self._llm_call_hook(
                    {
                        "step_index": 1,
                        "request_messages": [{"role": "system", "content": "s"}],
                        "request_tools": ["read_file"],
                        "response_content": "Hello",
                        "response_thinking": None,
                        "response_tool_calls": None,
                        "response_error": None,
                        "finish_reason": "stop",
                        "usage_prompt_tokens": 10,
                        "usage_completion_tokens": 3,
                        "usage_total_tokens": 13,
                        "first_token_latency_s": 0.077,
                        "completion_latency_s": 0.333,
                    }
                )
                yield TextMessageContentEvent(messageId="m1", delta="Hello")
                yield TextMessageEndEvent(messageId="m1")
                yield StepFinishedEvent(stepName="step-1")
                yield RunFinishedEvent(threadId="session-123", runId=kwargs["run_id"], outcome="success")

        service.agent = HookAwareAgent()
        service.history_service.save_llm_call_record.side_effect = OperationalError(
            "INSERT",
            {},
            Exception("server closed the connection unexpectedly"),
        )
        service.history_service.reset_session = MagicMock()

        events = []
        async for event in service.chat_agui([
            {"type": "text", "text": "hello"},
        ]):
            events.append(event)

        assert events[-1].type == "RUN_FINISHED"
        service.history_service.reset_session.assert_called_once()
        service.history_service.complete_round.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_agui_llm_call_record_non_db_error_fails_run(self, service):
        """非 DB 错误不能被 telemetry 降级吞掉。"""

        class HookAwareAgent:
            def __init__(self):
                self.messages = []
                self._pending_interrupt = None
                self._llm_call_hook = None

            def has_pending_interrupt(self):
                return False

            def clear_pending_interrupt(self):
                return None

            def add_user_message(self, content):
                self.messages.append(AgentHistoryMessage(role="user", content=content))

            def set_llm_call_hook(self, hook):
                self._llm_call_hook = hook

            async def run_agui(self, **kwargs):
                await self._llm_call_hook(
                    {
                        "step_index": 1,
                        "request_messages": [{"role": "system", "content": "s"}],
                        "request_tools": ["read_file"],
                        "response_content": "Hello",
                        "response_thinking": None,
                        "response_tool_calls": None,
                        "response_error": None,
                        "finish_reason": "stop",
                        "usage_prompt_tokens": 10,
                        "usage_completion_tokens": 3,
                        "usage_total_tokens": 13,
                        "first_token_latency_s": 0.077,
                        "completion_latency_s": 0.333,
                    }
                )
                yield RunFinishedEvent(threadId="session-123", runId=kwargs["run_id"], outcome="success")

        service.agent = HookAwareAgent()
        service.history_service.save_llm_call_record.side_effect = RuntimeError("schema drift")
        service.history_service.reset_session = MagicMock()

        with pytest.raises(RuntimeError, match="schema drift"):
            async for _event in service.chat_agui([
                {"type": "text", "text": "hello"},
            ]):
                pass

        service.history_service.reset_session.assert_called_once()
        service.history_service.complete_round.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_agui_with_attachments(self, service):
        events = []
        async for event in service.chat_agui([
            {"type": "text", "text": "read this"},
            {"type": "file", "file": {"path": "a.txt", "name": "a.txt"}},
            {"type": "file", "file": {"path": "b.pdf", "name": "b.pdf"}},
        ]):
            events.append(event)

        assert len(events) == 4
        service.agent.add_user_message.assert_called_once()
        sent_content = service.agent.add_user_message.call_args.args[0]
        assert isinstance(sent_content, list)
        assert any('"path":"a.txt"' in block.get("text", "") for block in sent_content)
        assert any('"path":"b.pdf"' in block.get("text", "") for block in sent_content)
        file_text_blocks = [
            block.get("text", "")
            for block in sent_content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        assert any("文件已就绪" in text for text in file_text_blocks)
        assert all("如需读取，请使用 read_file 工具" not in text for text in file_text_blocks)
        create_kwargs = service.history_service.create_round.call_args.kwargs
        assert create_kwargs["user_message"] == "read this"
        assert len(create_kwargs["user_attachments"]) == 2
        assert create_kwargs["user_attachments"][0]["path"] == "a.txt"

    @pytest.mark.asyncio
    async def test_chat_agui_attachment_prompt_preserves_exact_chinese_etf_path(self, service):
        filename = "xxxx.docx"

        async for _event in service.chat_agui([
            {"type": "file", "file": {"path": filename, "name": filename}},
        ]):
            pass

        sent_content = service.agent.add_user_message.call_args.args[0]
        file_text = next(
            block.get("text", "")
            for block in sent_content
            if isinstance(block, dict) and block.get("type") == "text"
        )

        assert f'"name":"{filename}"' in file_text
        assert f'"path":"{filename}"' in file_text
        assert "metadata.path" in file_text
        assert "xxxx .docx" not in file_text

        create_kwargs = service.history_service.create_round.call_args.kwargs
        assert create_kwargs["user_attachments"][0]["path"] == filename

    @pytest.mark.asyncio
    async def test_chat_agui_with_attachment_only_message_keeps_history_semantics(self, service):
        events = []
        async for event in service.chat_agui([
            {"type": "file", "file": {"path": "report.pdf", "name": "report.pdf"}},
        ]):
            events.append(event)

        assert len(events) == 4
        create_kwargs = service.history_service.create_round.call_args.kwargs
        assert create_kwargs["user_message"] == "[附件文件:report.pdf]"
        assert len(create_kwargs["user_attachments"]) == 1
        assert create_kwargs["user_attachments"][0]["path"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_chat_agui_with_image_not_supported(self, service):
        with pytest.raises(ValueError, match="不支持图片"):
            async for _ in service.chat_agui([
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            ]):
                pass

    @pytest.mark.asyncio
    async def test_chat_agui_with_image_supported(self, service):
        events = []
        with patch(
            "src.api.services.agent_service.get_model_registry",
            return_value=MockRegistry(supports_image=True, max_images=2),
        ):
            async for event in service.chat_agui([
                {"type": "text", "text": "请看图"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,Zm9v"},
                    "file": {"path": "images/a.png", "name": "a.png", "mime_type": "image/png"},
                },
            ]):
                events.append(event)

        assert len(events) == 4
        sent_content = service.agent.add_user_message.call_args.args[0]
        assert any(block.get("type") == "image_url" for block in sent_content)
        assert not any("file" in block for block in sent_content if block.get("type") == "image_url")
        create_kwargs = service.history_service.create_round.call_args.kwargs
        assert create_kwargs["user_message"] == "请看图"
        assert create_kwargs["user_attachments"][0]["path"] == "images/a.png"

    @pytest.mark.asyncio
    async def test_chat_agui_no_agent(self):
        service = make_agent_service()

        with pytest.raises(RuntimeError, match="Agent not initialized"):
            async for _ in service.chat_agui([{"type": "text", "text": "hello"}]):
                pass


class TestAgentServiceGenerateTitle:
    @pytest.fixture
    def service(self):
        service = make_agent_service()
        service.agent = MagicMock()
        service.agent.llm = MockLLMClient()
        return service

    @pytest.mark.asyncio
    async def test_generate_session_title(self, service):
        title = await service.generate_session_title("幫我寫一個 Python 腳本")
        assert isinstance(title, str)
        assert len(title) <= 30

    @pytest.mark.asyncio
    async def test_generate_session_title_truncate(self, service):
        async def generate_long(*args, **kwargs):
            from src.agent.schema import LLMResponse
            return LLMResponse(content="這是一個非常非常非常非常非常非常非常非常非常長的標題", finish_reason="stop")

        service.agent.llm.generate = generate_long
        title = await service.generate_session_title("Some message")
        assert len(title) <= 30

    @pytest.mark.asyncio
    async def test_generate_session_title_no_agent(self):
        service = make_agent_service()

        with pytest.raises(RuntimeError, match="Agent not initialized"):
            await service.generate_session_title("Hello")

    @pytest.mark.asyncio
    async def test_generate_session_title_error_returns_fallback(self, service):
        async def fail_generate(*args, **kwargs):
            raise Exception("LLM failed")

        service.agent.llm.generate = fail_generate
        title = await service.generate_session_title("Hello title")
        assert title == "Hello title"


class TestAgentServiceInitializeAgent:
    @pytest.fixture(autouse=True)
    def _mock_registry(self):
        model_config = SimpleNamespace(
            id="test-model",
            model_name="test-model",
            provider="openai",
            api_base="https://api.example.com",
            supports_image=False,
            max_images=0,
            supports_video=False,
            max_videos=0,
            context_window=128000,
            max_tokens=4096,
            compute_token_limit=lambda: 50000,
        )
        registry = MagicMock()
        registry.source = "yaml"
        registry.get_default.return_value = model_config
        registry.get_or_raise.return_value = model_config
        registry.list_models.return_value = [model_config]
        with patch("src.api.services.agent_service.get_model_registry", return_value=registry):
            yield

    @pytest.fixture
    def service(self):
        return make_agent_service()

    @pytest.mark.asyncio
    async def test_initialize_agent(self, service):
        with patch("src.api.services.agent_service.settings") as mock_settings:
            mock_settings.llm_provider = "openai"
            mock_settings.llm_api_key = "test-key"
            mock_settings.llm_api_base = "https://api.example.com"
            mock_settings.llm_model = "test-model"
            mock_settings.agent_max_steps = 10
            mock_settings.agent_token_limit = 50000
            mock_settings.bocha_search_appcode = None

            with patch("src.api.services.agent_service.LLMClient"):
                with patch("src.api.services.agent_service.Agent") as MockAgent:
                    MockAgent.return_value = MagicMock()
                    await service.initialize_agent()
                    assert service.agent is not None

    @pytest.mark.asyncio
    async def test_runtime_skill_metadata_survives_refresh_failure(self, service):
        loader = MagicMock()
        loader.refresh_disabled_skills.side_effect = RuntimeError("database unavailable")
        loader.get_skills_metadata_prompt.return_value = "- `pdf`: PDF documents"
        service._provision_default_files_if_needed = MagicMock()
        service._load_system_prompt = MagicMock(return_value="system")
        service._restore_history = MagicMock()

        with patch("src.api.services.agent_service.settings") as mock_settings, patch(
            "src.api.services.agent_service.create_agent_tools",
            new_callable=AsyncMock,
            return_value=([], loader),
        ), patch("src.api.services.agent_service.LLMClient") as MockLLM, patch(
            "src.api.services.agent_service.Agent"
        ) as MockAgent:
            mock_settings.agent_max_steps = 10
            mock_settings.agent_tool_timeout = 300
            mock_settings.agent_subagent_max_parallel = 1
            MockLLM.from_model_config.return_value = MagicMock()

            await service.initialize_agent()

        runtime_provider = MockAgent.call_args.kwargs["runtime_prompt_provider"]
        assert "`pdf`" in runtime_provider()

    @pytest.mark.asyncio
    async def test_initialize_agent_filters_multimodal_incompatible_fallbacks(self, service):
        def cfg(model_id: str, *, supports_image=False, max_images=0, supports_video=False, max_videos=0):
            return SimpleNamespace(
                id=model_id,
                model_name=model_id,
                provider="openai",
                api_base="https://api.example.com",
                supports_image=supports_image,
                max_images=max_images,
                supports_video=supports_video,
                max_videos=max_videos,
                context_window=128000,
                max_tokens=4096,
                compute_token_limit=lambda: 50000,
            )

        primary = cfg("primary-vision", supports_image=True, max_images=3)
        compatible = cfg("compatible-vision", supports_image=True, max_images=3)
        too_small = cfg("small-vision", supports_image=True, max_images=1)
        no_image = cfg("text-only", supports_image=False, max_images=0)

        registry = MagicMock()
        registry.get_or_raise.return_value = primary
        registry.get_default.return_value = primary
        registry.list_models.return_value = [primary, no_image, too_small, compatible]

        with patch("src.api.services.agent_service.get_model_registry", return_value=registry):
            with patch("src.api.services.agent_service.create_agent_tools", new_callable=AsyncMock, return_value=([], None)):
                with patch("src.api.services.agent_service.LLMClient") as MockLLM:
                    MockLLM.from_model_config.return_value = MagicMock()
                    with patch("src.api.services.agent_service.Agent") as MockAgent:
                        MockAgent.return_value = MagicMock()
                        service._provision_default_files_if_needed = MagicMock()
                        service._load_system_prompt = MagicMock(return_value="system")

                        await service.initialize_agent()

        fallback_configs = MockLLM.from_model_config.call_args.kwargs["fallback_configs"]
        assert [item.id for item in fallback_configs] == ["compatible-vision"]

    @pytest.mark.asyncio
    async def test_initialize_agent_fail_fast_when_registry_unavailable(self, service):
        with patch(
            "src.api.services.agent_service.get_model_registry",
            side_effect=FileNotFoundError("models.yaml missing"),
        ):
            with pytest.raises(RuntimeError, match="Model Registry 不可用"):
                await service.initialize_agent()


class TestValidateMultimodalBlocks:
    """_validate_multimodal_blocks 圖片大小校驗測試"""

    @pytest.fixture(autouse=True)
    def _setup_service(self):
        self.svc = make_agent_service(session_id="session-test")
        self.svc.model_id = "mock-model"

    def _patch_registry(self, **kwargs):
        return patch(
            "src.api.services.agent_service.get_model_registry",
            return_value=MockRegistry(**kwargs),
        )

    def test_small_image_passes(self):
        """正常大小的圖片應該通過校驗"""
        small_url = "data:image/jpeg;base64," + "A" * 1000
        blocks = [{"type": "image_url", "image_url": {"url": small_url}}]
        with self._patch_registry(supports_image=True, max_images=20):
            self.svc._validate_multimodal_blocks(blocks)

    def test_oversized_single_image_rejected(self):
        """超過 20MB 的單張圖片應被拒絕"""
        huge_url = "data:image/png;base64," + "A" * (25 * 1024 * 1024)
        blocks = [{"type": "image_url", "image_url": {"url": huge_url}}]
        with self._patch_registry(supports_image=True, max_images=20):
            with pytest.raises(ValueError, match="单张图片.*过大"):
                self.svc._validate_multimodal_blocks(blocks)

    def test_total_image_size_limit(self):
        """所有圖片總計超過 50MB 應被拒絕"""
        img_url = "data:image/jpeg;base64," + "A" * (18 * 1024 * 1024)
        blocks = [{"type": "image_url", "image_url": {"url": img_url}} for _ in range(3)]
        with self._patch_registry(supports_image=True, max_images=20):
            with pytest.raises(ValueError, match="总计过大"):
                self.svc._validate_multimodal_blocks(blocks)

    def test_model_not_support_image_rejected(self):
        """模型不支持圖片時應拒絕"""
        blocks = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}}]
        with self._patch_registry(supports_image=False, max_images=0):
            with pytest.raises(ValueError, match="不支持图片输入"):
                self.svc._validate_multimodal_blocks(blocks)


class TestWriteFileDirtyMemoryDetection:
    """write_file 写入记忆文件时应触发 _dirty_memory"""

    def _make_service(self):
        history_service = MagicMock()
        history_service.create_round = MagicMock()
        history_service.complete_round = MagicMock()
        history_service.save_agui_event = AsyncMock()

        service = make_agent_service(
            history_service=history_service,
            session_id="session-dirty",
        )
        service.model_id = "mock-model"
        return service

    async def _run_dirty_test(self, args_deltas, expected_sync):
        """执行 dirty memory 检测的通用测试逻辑。"""
        service = self._make_service()

        run_agui_fn = make_tool_call_agui_events(
            tool_name="write_file",
            args_deltas=args_deltas,
            thread_id="session-dirty",
        )
        service.agent = make_mock_agent(run_agui_fn=run_agui_fn)

        mock_post_round = AsyncMock()
        with patch.object(service, "_post_round_tasks", mock_post_round):
            with patch(
                "src.api.services.agent_service.get_model_registry",
                return_value=MockRegistry(supports_image=False, max_images=0),
            ):
                async for _ in service.chat_agui([{"type": "text", "text": "hello"}]):
                    pass

        mock_post_round.assert_called_once()
        assert mock_post_round.call_args.kwargs["sync_memory"] is expected_sync

    @pytest.mark.asyncio
    async def test_write_file_to_user_md_sets_dirty(self):
        """write_file 写入 USER.md 应触发同步"""
        await self._run_dirty_test(
            args_deltas=['{"path": "/home/user/USER.md", "content": "# Profile"}'],
            expected_sync=True,
        )

    @pytest.mark.asyncio
    async def test_write_file_to_non_memory_file_no_dirty(self):
        """write_file 写入普通文件不应触发同步"""
        await self._run_dirty_test(
            args_deltas=['{"path": "/home/user/app.py", "content": "print(1)"}'],
            expected_sync=False,
        )

    @pytest.mark.asyncio
    async def test_write_file_to_memory_md_sets_dirty(self):
        """write_file 写入 MEMORY.md 应触发同步（跨多个 TOOL_CALL_ARGS delta）"""
        await self._run_dirty_test(
            args_deltas=['{"path": "MEMORY.md"', ', "content": "# Mem"}'],
            expected_sync=True,
        )

    @pytest.mark.asyncio
    async def test_write_file_to_soul_md_sets_dirty(self):
        """write_file 写入 SOUL.md 应触发同步"""
        await self._run_dirty_test(
            args_deltas=['{"path": "/home/user/SOUL.md", "content": "# Updated"}'],
            expected_sync=True,
        )

    @pytest.mark.asyncio
    async def test_write_file_to_agents_md_does_not_set_dirty(self):
        """AGENTS.md 由平台模板管理，不从沙箱回写 DB"""
        await self._run_dirty_test(
            args_deltas=['{"path": "/home/user/AGENTS.md", "content": "# Updated"}'],
            expected_sync=False,
        )


class TestAgentServiceResumeAgui:
    @pytest.fixture
    def service(self):
        history_service = MagicMock()
        history_service.create_round = MagicMock()
        history_service.create_resume_round = MagicMock()
        history_service.complete_round = MagicMock()
        history_service.save_agui_event = AsyncMock()
        history_service.resolve_interrupted_rounds = MagicMock(return_value=1)

        service = make_agent_service(history_service=history_service)

        async def _run_agui(**kwargs):
            yield TextMessageContentEvent(messageId="m1", delta="resume done")
            yield TextMessageEndEvent(messageId="m1")
            yield StepFinishedEvent(stepName="step-1")
            yield RunFinishedEvent(threadId="session-123", runId=kwargs["run_id"], outcome="success")

        service.agent = make_mock_agent(run_agui_fn=_run_agui)
        service.agent.has_pending_interrupt.return_value = True
        service.model_id = "mock-model"
        service._build_restored_history_messages = MagicMock(return_value=[])
        return service

    @pytest.mark.asyncio
    async def test_resume_agui_persists_resume_user_message(self, service):
        answers = {"Which DB?": "PostgreSQL"}

        with patch.object(
            service,
            "_load_persisted_interrupt",
            return_value={
                "interrupt_id": "iid-1",
                "round_id": "round-interrupted",
                "tool_call_id": "tc-ask",
            },
        ):
            with patch.object(service, "_save_conversation_message") as save_message:
                async for _ in service.resume_agui("iid-1", answers):
                    pass

        service.agent.resume_from_interrupt.assert_called_once_with("iid-1", answers)
        service.history_service.create_resume_round.assert_called_once()
        create_kwargs = service.history_service.create_resume_round.call_args.kwargs
        assert create_kwargs["parent_run_id"] == "round-interrupted"
        assert create_kwargs["interrupt_id"] == "iid-1"
        assert create_kwargs["tool_call_id"] == "tc-ask"
        assert create_kwargs["answers"] == answers
        assert create_kwargs["tool_result_content"] == "User answered:\n- Which DB?: PostgreSQL"
        assert create_kwargs["restore_strategy"] == "hot_replace"
        service.history_service.create_round.assert_not_called()
        service.history_service.resolve_interrupted_rounds.assert_not_called()
        assert any(
            call.args and call.args[0] == "user" and "Q: Which DB?" in call.args[1]
            for call in save_message.call_args_list
        )
        assert any(
            call.args and call.args[0] == "assistant" and "resume done" in call.args[1]
            for call in save_message.call_args_list
        )

    def test_has_pending_interrupt_falls_back_to_persisted_interrupt(self, service):
        service.agent.has_pending_interrupt.return_value = False

        with patch.object(service, "_load_persisted_interrupt", return_value={"interrupt_id": "iid-1"}):
            assert service.has_pending_interrupt("iid-1") is True

    @pytest.mark.asyncio
    async def test_resume_agui_uses_cold_fallback_when_memory_interrupt_missing(self, service):
        answers = {"Which DB?": "PostgreSQL"}
        service.agent.has_pending_interrupt.return_value = False

        with patch.object(
            service,
            "_load_persisted_interrupt",
            return_value={"interrupt_id": "iid-1", "round_id": "round-interrupted"},
        ):
            with patch.object(service, "_save_conversation_message") as save_message:
                async for _ in service.resume_agui("iid-1", answers):
                    pass

        service.agent.resume_from_interrupt.assert_not_called()
        service.agent.add_user_message.assert_called_once()
        injected_user_message = service.agent.add_user_message.call_args.args[0]
        assert "Q: Which DB?" in injected_user_message
        assert "A: PostgreSQL" in injected_user_message
        service.history_service.create_resume_round.assert_called_once()
        create_kwargs = service.history_service.create_resume_round.call_args.kwargs
        assert create_kwargs["parent_run_id"] == "round-interrupted"
        assert create_kwargs["interrupt_id"] == "iid-1"
        assert create_kwargs["tool_call_id"] is None
        assert create_kwargs["tool_result_content"] == "User answered:\n- Which DB?: PostgreSQL"
        assert create_kwargs["restore_strategy"] == "cold_fallback_user_message"
        assert create_kwargs["fallback_reason"] == "tool_call_id missing"
        service.history_service.create_round.assert_not_called()
        service.history_service.resolve_interrupted_rounds.assert_not_called()
        assert any(
            call.args and call.args[0] == "user" and "Q: Which DB?" in call.args[1]
            for call in save_message.call_args_list
        )

    @pytest.mark.asyncio
    async def test_resume_agui_cold_path_replaces_restored_tool_placeholder(self, service):
        """冷 resume 有 tool_call_id 时应替换历史恢复出的 ask_user tool result。"""
        answers = {"Which DB?": "PostgreSQL"}
        service.agent.has_pending_interrupt.return_value = False
        service._build_restored_history_messages.return_value = [
            AgentHistoryMessage(
                role="tool",
                content="[Awaiting user response]",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]
        service.agent.messages = [
            AgentHistoryMessage(
                role="tool",
                content="[Awaiting user response]",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]

        with patch.object(
            service,
            "_load_persisted_interrupt",
            return_value={
                "interrupt_id": "iid-1",
                "round_id": "round-interrupted",
                "tool_call_id": "tc-ask",
            },
        ):
            with patch.object(service, "_save_conversation_message"):
                async for _ in service.resume_agui("iid-1", answers):
                    pass

        service.agent.resume_from_interrupt.assert_not_called()
        service.agent.add_user_message.assert_not_called()
        assert service.agent.messages[0].content == "User answered:\n- Which DB?: PostgreSQL"
        create_kwargs = service.history_service.create_resume_round.call_args.kwargs
        assert create_kwargs["interrupt_id"] == "iid-1"
        assert create_kwargs["tool_call_id"] == "tc-ask"
        assert create_kwargs["tool_result_content"] == "User answered:\n- Which DB?: PostgreSQL"
        assert create_kwargs["restore_strategy"] == "cold_replace"
        assert create_kwargs["fallback_reason"] is None

    @pytest.mark.asyncio
    async def test_resume_agui_cold_path_records_fallback_reason_when_replace_fails(self, service):
        """冷 resume 替换失败时应降级 user message，并把原因写入 resolution。"""
        answers = {"Which DB?": "PostgreSQL"}
        service.agent.has_pending_interrupt.return_value = False
        service._build_restored_history_messages.return_value = [
            AgentHistoryMessage(
                role="tool",
                content="already resolved",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]
        service.agent.messages = [
            AgentHistoryMessage(
                role="tool",
                content="already resolved",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]

        with patch.object(
            service,
            "_load_persisted_interrupt",
            return_value={
                "interrupt_id": "iid-1",
                "round_id": "round-interrupted",
                "tool_call_id": "tc-ask",
            },
        ):
            with patch.object(service, "_save_conversation_message"):
                async for _ in service.resume_agui("iid-1", answers):
                    pass

        service.agent.add_user_message.assert_called_once()
        create_kwargs = service.history_service.create_resume_round.call_args.kwargs
        assert create_kwargs["restore_strategy"] == "cold_fallback_user_message"
        assert create_kwargs["fallback_reason"] == "tool placeholder not found or already resolved"
        assert create_kwargs["interrupt_id"] == "iid-1"

    @pytest.mark.asyncio
    async def test_resume_agui_restores_hot_state_when_resume_round_create_fails(self, service):
        """DB 创建 resume round 失败时，热路径不应留下已恢复的内存状态。"""
        answers = {"Which DB?": "PostgreSQL"}
        pending = {
            "interrupt_id": "iid-1",
            "tool_call_id": "tc-ask",
            "questions": [{"question": "Which DB?"}],
        }
        service.agent._pending_interrupt = dict(pending)
        service._build_restored_history_messages.return_value = [
            AgentHistoryMessage(
                role="tool",
                content="[Awaiting user response]",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]
        service.agent.messages = [
            AgentHistoryMessage(
                role="tool",
                content="[Awaiting user response]",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]
        service.history_service.create_resume_round.side_effect = [
            ValueError("Round is not resumable: round-interrupted status=resumed"),
            None,
        ]

        def _resume_from_interrupt(_interrupt_id, _answers):
            service.agent.messages[0].content = "User answered:\n- Which DB?: PostgreSQL"
            service.agent._pending_interrupt = None

        service.agent.resume_from_interrupt.side_effect = _resume_from_interrupt

        with patch.object(
            service,
            "_load_persisted_interrupt",
            return_value={
                "interrupt_id": "iid-1",
                "round_id": "round-interrupted",
                "tool_call_id": "tc-ask",
            },
        ):
            with patch.object(service, "_save_conversation_message"):
                with pytest.raises(ValueError, match="Round is not resumable"):
                    async for _ in service.resume_agui("iid-1", answers):
                        pass

                assert service.agent.messages[0].content == "[Awaiting user response]"
                assert service.agent._pending_interrupt == pending

                async for _ in service.resume_agui("iid-1", answers):
                    pass

        assert service.agent.resume_from_interrupt.call_count == 2
        assert service.agent.messages[0].content == "User answered:\n- Which DB?: PostgreSQL"
        assert service.agent._pending_interrupt is None

    @pytest.mark.asyncio
    async def test_resume_agui_restores_cold_replace_when_resume_round_create_fails(self, service):
        """DB 创建 resume round 失败时，冷替换路径应还原 tool 占位。"""
        answers = {"Which DB?": "PostgreSQL"}
        service.agent.has_pending_interrupt.return_value = False
        service._build_restored_history_messages.return_value = [
            AgentHistoryMessage(
                role="tool",
                content="[Awaiting user response]",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]
        service.agent.messages = [
            AgentHistoryMessage(
                role="tool",
                content="[Awaiting user response]",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]
        service.history_service.create_resume_round.side_effect = ValueError(
            "Interrupt already resumed: iid-1"
        )

        with patch.object(
            service,
            "_load_persisted_interrupt",
            return_value={
                "interrupt_id": "iid-1",
                "round_id": "round-interrupted",
                "tool_call_id": "tc-ask",
            },
        ):
            with patch.object(service, "_save_conversation_message"):
                with pytest.raises(ValueError, match="Interrupt already resumed"):
                    async for _ in service.resume_agui("iid-1", answers):
                        pass

        assert service.agent.messages[0].content == "[Awaiting user response]"
        service.agent.add_user_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_agui_restores_cold_fallback_when_resume_round_create_fails(self, service):
        """DB 创建 resume round 失败时，冷 fallback 不应留下重复 Q/A user。"""
        answers = {"Which DB?": "PostgreSQL"}
        service.agent.has_pending_interrupt.return_value = False
        service._build_restored_history_messages.return_value = [
            AgentHistoryMessage(
                role="tool",
                content="already resolved",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]
        service.agent.messages = [
            AgentHistoryMessage(
                role="tool",
                content="already resolved",
                tool_call_id="tc-ask",
                name="ask_user",
            )
        ]
        service.history_service.create_resume_round.side_effect = ValueError(
            "Interrupt already resumed: iid-1"
        )

        def _add_user_message(content):
            service.agent.messages.append(AgentHistoryMessage(role="user", content=content))

        service.agent.add_user_message.side_effect = _add_user_message

        with patch.object(
            service,
            "_load_persisted_interrupt",
            return_value={
                "interrupt_id": "iid-1",
                "round_id": "round-interrupted",
                "tool_call_id": "tc-ask",
            },
        ):
            with patch.object(service, "_save_conversation_message"):
                with pytest.raises(ValueError, match="Interrupt already resumed"):
                    async for _ in service.resume_agui("iid-1", answers):
                        pass

        assert [msg.role for msg in service.agent.messages] == ["tool"]
        assert service.agent.messages[0].content == "already resolved"

    @pytest.mark.asyncio
    async def test_resume_agui_uses_hot_parent_cache_before_interrupt_commit(self, service):
        """热恢复可用内存态 parent 映射覆盖 RUN_FINISHED 刚发出但 DB 尚未标 interrupted 的窗口。"""
        answers = {"Which DB?": "PostgreSQL"}
        service._pending_interrupt_round_ids["iid-1"] = "round-interrupted"

        with patch.object(service, "_load_persisted_interrupt", return_value=None):
            with patch.object(service, "_save_conversation_message"):
                async for _ in service.resume_agui("iid-1", answers):
                    pass

        service.agent.resume_from_interrupt.assert_called_once_with("iid-1", answers)
        assert service.history_service.create_resume_round.call_args.kwargs["parent_run_id"] == "round-interrupted"
        service.history_service.create_round.assert_not_called()
        assert "iid-1" not in service._pending_interrupt_round_ids

    @pytest.mark.asyncio
    async def test_resume_agui_uses_hot_pending_snapshot_parent_without_side_map(self, service):
        """热恢复应能直接从 pending interrupt 快照拿到 parent round。"""
        answers = {"Which DB?": "PostgreSQL"}
        service.agent._pending_interrupt = {
            "interrupt_id": "iid-1",
            "round_id": "round-interrupted",
            "tool_call_id": "tc-ask",
            "questions": [{"question": "Which DB?"}],
        }

        with patch.object(service, "_load_persisted_interrupt", return_value=None):
            with patch.object(service, "_save_conversation_message"):
                async for _ in service.resume_agui("iid-1", answers):
                    pass

        service.agent.resume_from_interrupt.assert_called_once_with("iid-1", answers)
        create_kwargs = service.history_service.create_resume_round.call_args.kwargs
        assert create_kwargs["parent_run_id"] == "round-interrupted"
        assert create_kwargs["tool_call_id"] == "tc-ask"
        service.history_service.create_round.assert_not_called()
