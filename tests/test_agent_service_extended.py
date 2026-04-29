"""AgentService（Sandbox 版）測試"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.agent.schema.agui_events import (
    TextMessageContentEvent,
    TextMessageEndEvent,
    StepFinishedEvent,
    RunFinishedEvent,
    ToolCallStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
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
        assert "write_file" in tool_names
        assert "edit_file" in tool_names
        assert "bash" in tool_names
        assert "bash_output" in tool_names
        assert "bash_kill" in tool_names
        assert "record_note" in tool_names


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

    def test_restore_history_no_agent(self):
        service = make_agent_service()
        service._restore_history()  # should not raise


class TestSummaryAnchorPersistence:
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
        assert service.history_service.save_agui_event.await_count == 4

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
        assert any("path=a.txt" in block.get("text", "") for block in sent_content)
        assert any("path=b.pdf" in block.get("text", "") for block in sent_content)
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
    @pytest.mark.parametrize("filename", ["SOUL.md", "AGENTS.md"])
    async def test_write_file_to_other_agent_files_sets_dirty(self, filename):
        """write_file 写入 SOUL.md/AGENTS.md 应触发同步"""
        await self._run_dirty_test(
            args_deltas=[f'{{"path": "/home/user/{filename}", "content": "# Updated"}}'],
            expected_sync=True,
        )


class TestAgentServiceResumeAgui:
    @pytest.fixture
    def service(self):
        history_service = MagicMock()
        history_service.create_round = MagicMock()
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
        return service

    @pytest.mark.asyncio
    async def test_resume_agui_persists_resume_user_message(self, service):
        answers = {"Which DB?": "PostgreSQL"}

        with patch.object(service, "_save_conversation_message") as save_message:
            async for _ in service.resume_agui("iid-1", answers):
                pass

        service.agent.resume_from_interrupt.assert_called_once_with("iid-1", answers)
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

        with patch.object(service, "_load_persisted_interrupt", return_value={"interrupt_id": "iid-1"}):
            with patch.object(service, "_save_conversation_message") as save_message:
                async for _ in service.resume_agui("iid-1", answers):
                    pass

        service.agent.resume_from_interrupt.assert_not_called()
        service.agent.add_user_message.assert_called_once()
        injected_user_message = service.agent.add_user_message.call_args.args[0]
        assert "Q: Which DB?" in injected_user_message
        assert "A: PostgreSQL" in injected_user_message
        service.history_service.resolve_interrupted_rounds.assert_called_once_with("session-123")
        assert any(
            call.args and call.args[0] == "user" and "Q: Which DB?" in call.args[1]
            for call in save_message.call_args_list
        )
