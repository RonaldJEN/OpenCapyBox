"""AgentService（Sandbox 版）測試"""

from datetime import datetime, timedelta

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace
from src.agent.schema.agui_events import (
    TextMessageContentEvent,
    TextMessageEndEvent,
    StepFinishedEvent,
    RunFinishedEvent,
    RunErrorEvent,
    CustomEvent,
    ToolCallStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    EventType,
)
from src.agent.schema import Message as AgentHistoryMessage
from src.api.services.agui_event_bus import RoundTerminalWriteSuppressed, StoredEvent
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
        assert "apply_patch" in tool_names
        assert "present_files" in tool_names
        assert "write_file" not in tool_names
        assert "edit_file" not in tool_names
        assert "bash" in tool_names
        assert "bash_output" in tool_names
        assert "bash_kill" in tool_names
        assert "sub_agent" in tool_names
        assert "record_note" in tool_names
        assert {
            "workspace_list",
            "workspace_stage",
            "workspace_publish",
            "workspace_create_directory",
            "workspace_move",
            "workspace_delete",
        }.issubset(tool_names)
        workspace_list = next(tool for tool in tools if tool.name == "workspace_list")
        assert workspace_list._sandbox is service.sandbox

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("access", "expected"),
        [
            ("none", set()),
            ("read", {"workspace_list", "workspace_stage"}),
            (
                "edit",
                {
                    "workspace_list",
                    "workspace_stage",
                    "workspace_publish",
                    "workspace_create_directory",
                },
            ),
            (
                "manage",
                {
                    "workspace_list",
                    "workspace_stage",
                    "workspace_publish",
                    "workspace_create_directory",
                    "workspace_move",
                    "workspace_delete",
                },
            ),
        ],
    )
    async def test_workspace_access_controls_exposed_tools(self, service, access, expected):
        with patch("src.api.services.tool_factory.settings") as mock_settings:
            mock_settings.bocha_search_appcode = None
            mock_settings.skills_dir = ""
            mock_settings.sandbox_background_command_timeout_seconds = 21600
            from src.api.services.tool_factory import create_agent_tools
            from src.api.services.sandbox_service import get_sandbox_mount_path

            tools, _ = await create_agent_tools(
                sandbox=service.sandbox,
                workspace_dir=service._workspace_dir,
                mount=get_sandbox_mount_path(),
                user_id=service.user_id,
                db_session_factory=service._get_db_session_factory(),
                workspace_access=access,
            )

        workspace_names = {tool.name for tool in tools if tool.name.startswith("workspace_")}
        assert workspace_names == expected

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
            configuration_fingerprint="configuration-3",
            tools=(),
            errors=("optional server offline",),
            connections=("available-connection",),
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
        assert (
            metadata["mcp_catalog_configuration_fingerprint"]
            == "configuration-3"
        )
        assert metadata["mcp_catalog_retry_required"] is False
        assert metadata["mcp_connections"] == ("available-connection",)

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
        sandbox_service.get_sandbox_id.return_value = "sandbox-1"
        sandbox_service.get_cached_profile_fingerprint.return_value = ("profile-1", 1)
        sandbox_service.discover_sandbox_skills = AsyncMock(return_value=[{
            "name": "my-skill",
            "display_name": "My Skill Display",
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
        ), patch(
            "src.api.services.tool_factory.persist_user_skill_inventory",
            return_value=True,
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
        assert loader.sandbox_skills["my-skill"].metadata == {
            "display_name": "My Skill Display"
        }

    @pytest.mark.asyncio
    async def test_user_skill_refresh_failure_preserves_existing_registry(
        self, service, tmp_path
    ):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        db_session_factory = MagicMock(return_value=db)
        sandbox_service = MagicMock()
        sandbox_service.get_sandbox_id.return_value = "sandbox-1"
        sandbox_service.get_cached_profile_fingerprint.return_value = ("profile-1", 1)
        sandbox_service.discover_sandbox_skills = AsyncMock(side_effect=[
            [{
                "name": "my-skill",
                "description": "User skill",
                "sandbox_skill_dir": "/home/user/skills/my-skill",
            }],
            RuntimeError("sandbox scan unavailable"),
        ])

        with patch("src.api.services.tool_factory.settings") as mock_settings, patch(
            "src.api.services.tool_factory.get_sandbox_service",
            return_value=sandbox_service,
        ), patch(
            "src.api.services.tool_factory.persist_user_skill_inventory",
            return_value=True,
        ):
            mock_settings.bocha_search_appcode = None
            mock_settings.skills_dir = str(skills_dir)
            mock_settings.sandbox_background_command_timeout_seconds = 21600
            from src.api.services.tool_factory import create_agent_tools

            _, loader = await create_agent_tools(
                sandbox=service.sandbox,
                workspace_dir=service._workspace_dir,
                mount="/home/user",
                user_id=service.user_id,
                db_session_factory=db_session_factory,
            )
            assert loader is not None
            assert "my-skill" in loader.sandbox_skills

            await loader.refresh_inventory()

        assert "my-skill" in loader.sandbox_skills
        refresh_call = sandbox_service.discover_sandbox_skills.await_args_list[1]
        assert refresh_call.kwargs["strict"] is True

    @pytest.mark.asyncio
    async def test_user_skill_publish_cas_loser_uses_matching_winner_snapshot(
        self, service, tmp_path
    ):
        from src.api.services.skill_inventory_service import (
            SkillInventoryIdentity,
            UserSkillInventoryView,
        )

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        db_session_factory = MagicMock(return_value=db)
        identity = SkillInventoryIdentity("sandbox-1", "profile-1", 1)
        winner = [{
            "name": "winner-skill",
            "description": "Newer complete scan",
            "sandbox_skill_dir": "/home/user/skills/winner-skill",
        }]
        sandbox_service = MagicMock()
        sandbox_service.get_sandbox_id.return_value = "sandbox-1"
        sandbox_service.get_cached_profile_fingerprint.return_value = ("profile-1", 1)
        sandbox_service.discover_sandbox_skills = AsyncMock(return_value=[{
            "name": "loser-skill",
            "description": "Older complete scan",
            "sandbox_skill_dir": "/home/user/skills/loser-skill",
        }])
        scan_started_at = datetime(2026, 7, 17, 1, 0, 0)

        with patch("src.api.services.tool_factory.settings") as mock_settings, patch(
            "src.api.services.tool_factory.get_sandbox_service",
            return_value=sandbox_service,
        ), patch(
            "src.api.services.tool_factory.persist_user_skill_inventory",
            return_value=False,
        ), patch(
            "src.api.services.tool_factory.load_user_skill_inventory",
            return_value=UserSkillInventoryView(
                identity,
                winner,
                scan_started_at + timedelta(seconds=1),
            ),
        ), patch(
            "src.api.services.tool_factory.now_naive",
            return_value=scan_started_at,
        ):
            mock_settings.bocha_search_appcode = None
            mock_settings.skills_dir = str(skills_dir)
            mock_settings.sandbox_background_command_timeout_seconds = 21600
            from src.api.services.tool_factory import create_agent_tools

            _, loader = await create_agent_tools(
                sandbox=service.sandbox,
                workspace_dir=service._workspace_dir,
                mount="/home/user",
                user_id=service.user_id,
                db_session_factory=db_session_factory,
            )

        assert loader is not None
        assert set(loader.sandbox_skills) == {"winner-skill"}


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
    async def test_new_message_does_not_abandon_same_round_interaction(self, service):
        from src.api.services.agent_interaction_service import InteractionConflictError

        service.agent._pending_interrupt = {
            "interrupt_id": "iid-1",
            "tool_call_id": "tc-1",
            "kind": "ask_user",
        }

        with patch.object(
            service,
            "_load_persisted_interrupt",
            return_value={
                "interrupt_id": "iid-1",
                "round_id": "round-original",
                "tool_call_id": "tc-1",
                "kind": "ask_user",
            },
        ):
            with pytest.raises(InteractionConflictError, match="等待用户回答"):
                await service.prepare_chat_round(
                    user_content=[{"type": "text", "text": "new request"}],
                )

        service.agent.clear_pending_interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_message_discards_cancelled_same_round_hot_interrupt(self, service):
        service.agent._pending_interrupt = {
            "interrupt_id": "iid-cancelled",
            "round_id": "round-cancelled",
            "tool_call_id": "tc-cancelled",
            "kind": "ask_user",
        }

        with patch.object(service, "_load_persisted_interrupt", return_value=None):
            prepared = await service.prepare_chat_round(
                user_content=[{"type": "text", "text": "new request"}],
            )

        assert prepared.run_id
        assert service.agent._pending_interrupt is None

    @pytest.mark.asyncio
    async def test_admitted_idempotency_key_skips_attachment_staging(self, service):
        from src.api.services.agent_service import DuplicateRoundError

        service.history_service.find_round_by_idempotency_key = MagicMock(
            return_value=SimpleNamespace(id="round-existing", status="running")
        )
        service._materialize_workspace_attachments = AsyncMock()

        with pytest.raises(DuplicateRoundError) as excinfo:
            await service.prepare_chat_round(
                user_content=[{"type": "text", "text": "retry"}],
                idempotency_key="idem-retry",
            )

        assert excinfo.value.existing_round_id == "round-existing"
        # Staging copies files and allocates uuid-suffixed directory snapshots;
        # a retry must not reach it.
        service._materialize_workspace_attachments.assert_not_awaited()
        service.history_service.create_round.assert_not_called()


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
    async def test_abort_race_suppresses_ephemeral_delta_and_raw_yield(self):
        history_service = MagicMock()
        history_service.db = MagicMock()
        history_service.is_round_terminal = MagicMock(return_value=False)
        history_service.get_round_status = MagicMock(return_value="cancelled")
        history_service.save_agui_event = AsyncMock(
            side_effect=RoundTerminalWriteSuppressed("round-delta-race")
        )
        history_service.complete_round = MagicMock()
        history_service.reset_session = MagicMock()
        history_service.last_terminal_event = None
        service = make_agent_service(history_service=history_service)
        service._save_conversation_message = MagicMock()

        async def _run_agui(**_kwargs):
            yield TextMessageContentEvent(messageId="m-race", delta="late")

        service.agent = make_mock_agent(run_agui_fn=_run_agui)
        terminal = StoredEvent(
            "round-delta-race",
            4,
            {"type": "RUN_FINISHED", "sequence": 4, "outcome": "interrupt"},
        )
        with patch(
            "src.api.services.agent_service.RunCompletionService"
        ) as completion_cls:
            completion_cls.return_value.ensure_terminal_sync.return_value = terminal
            events = [
                event
                async for event in service._run_round_stream(
                    run_id="round-delta-race",
                    user_message="hello",
                )
            ]

        assert events == [terminal.event]
        service._save_conversation_message.assert_not_called()
        history_service.complete_round.assert_not_called()

    @pytest.mark.asyncio
    async def test_abort_race_suppresses_durable_text_end_side_effects(self):
        history_service = MagicMock()
        history_service.db = MagicMock()
        history_service.is_round_terminal = MagicMock(return_value=False)
        history_service.get_round_status = MagicMock(return_value="cancelled")
        history_service.save_agui_event = AsyncMock(
            side_effect=[
                None,
                RoundTerminalWriteSuppressed("round-end-race"),
            ]
        )
        history_service.complete_round = MagicMock()
        history_service.reset_session = MagicMock()
        history_service.last_terminal_event = None
        service = make_agent_service(history_service=history_service)
        service._save_conversation_message = MagicMock()

        async def _run_agui(**_kwargs):
            yield TextMessageContentEvent(messageId="m-race", delta="accepted")
            yield TextMessageEndEvent(messageId="m-race")

        service.agent = make_mock_agent(run_agui_fn=_run_agui)
        terminal = StoredEvent(
            "round-end-race",
            8,
            {"type": "RUN_FINISHED", "sequence": 8, "outcome": "interrupt"},
        )
        with patch(
            "src.api.services.agent_service.RunCompletionService"
        ) as completion_cls:
            completion_cls.return_value.ensure_terminal_sync.return_value = terminal
            events = [
                event
                async for event in service._run_round_stream(
                    run_id="round-end-race",
                    user_message="hello",
                )
            ]

        assert events[0].type == EventType.TEXT_MESSAGE_CONTENT
        assert events[1:] == [terminal.event]
        service._save_conversation_message.assert_not_called()
        history_service.complete_round.assert_not_called()

    @pytest.mark.parametrize("late_terminal_kind", ["finished", "error"])
    @pytest.mark.asyncio
    async def test_abort_between_terminal_precheck_and_completion_yields_committed_terminal(
        self,
        late_terminal_kind,
    ):
        history_service = MagicMock()
        history_service.db = MagicMock()
        # The Agent-side precheck wins first; abort commits before
        # complete_round takes the authoritative Round lock.
        history_service.is_round_terminal = MagicMock(return_value=False)
        history_service.complete_round = MagicMock(
            return_value=SimpleNamespace(status="cancelled"),
        )
        history_service.last_terminal_event = None
        history_service.reset_session = MagicMock()
        service = make_agent_service(history_service=history_service)

        async def _run_agui(**kwargs):
            if late_terminal_kind == "finished":
                yield RunFinishedEvent(
                    threadId="session-123",
                    runId=kwargs["run_id"],
                    outcome="success",
                    result={"finalResponse": "late success"},
                )
            else:
                yield RunErrorEvent(
                    message="late failure",
                    code="LATE_AGENT_FAILURE",
                )

        service.agent = make_mock_agent(run_agui_fn=_run_agui)
        terminal = StoredEvent(
            "round-terminal-race",
            7,
            {
                "type": "RUN_FINISHED",
                "sequence": 7,
                "outcome": "interrupt",
                "result": {"reason": "user_cancelled"},
            },
        )
        with patch(
            "src.api.services.agent_service.RunCompletionService"
        ) as completion_cls:
            completion_cls.return_value.ensure_terminal_sync.return_value = terminal
            events = [
                event
                async for event in service._run_round_stream(
                    run_id="round-terminal-race",
                    user_message="hello",
                )
            ]

        assert events == [terminal.event]
        completion_cls.return_value.ensure_terminal_sync.assert_called_once_with(
            "round-terminal-race"
        )

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
            commit=False,
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
    async def test_run_round_stream_parks_interaction_without_completion(self):
        history_service = MagicMock()
        history_service.db = MagicMock()
        history_service.is_round_terminal = MagicMock(return_value=False)
        history_service.save_agui_event = AsyncMock(return_value=None)
        history_service.complete_round = MagicMock()
        history_service.save_waiting_round_progress = MagicMock()
        history_service.reset_session = MagicMock()
        history_service.last_terminal_event = None
        service = make_agent_service(history_service=history_service)

        async def _run_agui(**kwargs):
            yield CustomEvent(
                name="interaction_requested",
                value={
                    "interactionId": "interaction-1",
                    "runId": kwargs["run_id"],
                    "kind": "user_input",
                    "toolCallId": "tool-1",
                    "payload": {"questions": [{"question": "Continue?"}]},
                },
            )
            yield StepFinishedEvent(stepName="step-1")

        service.agent = make_mock_agent(run_agui_fn=_run_agui)
        service.agent._pending_interrupt = {
            "interrupt_id": "interaction-1",
            "tool_call_id": "tool-1",
            "kind": "ask_user",
        }

        with patch(
            "src.api.services.agent_service.AgentInteractionService.create_pending"
        ) as create_pending:
            events = []
            async for event in service._run_round_stream(
                run_id="round-1",
                user_message="hello",
            ):
                events.append(event)

        assert [event.type for event in events] == [EventType.CUSTOM, EventType.STEP_FINISHED]
        create_pending.assert_called_once()
        assert create_pending.call_args.kwargs["step_count"] == 1
        history_service.complete_round.assert_not_called()
        history_service.save_waiting_round_progress.assert_called_once_with(
            "round-1",
            step_count=1,
        )

    @pytest.mark.asyncio
    async def test_step_finished_write_failure_keeps_atomic_waiting_step_count(self):
        history_service = MagicMock()
        history_service.db = MagicMock()
        history_service.is_round_terminal = MagicMock(return_value=False)
        history_service.save_agui_event = AsyncMock(
            side_effect=[None, RuntimeError("step persistence failed")],
        )
        history_service.complete_round = MagicMock()
        history_service.save_waiting_round_progress = MagicMock()
        history_service.reset_session = MagicMock()
        history_service.last_terminal_event = None
        service = make_agent_service(history_service=history_service)

        async def _run_agui(**kwargs):
            yield CustomEvent(
                name="interaction_requested",
                value={
                    "interactionId": "interaction-step-crash",
                    "runId": kwargs["run_id"],
                    "kind": "user_input",
                    "toolCallId": "tool-step-crash",
                    "payload": {"questions": [{"question": "Continue?"}]},
                },
            )
            yield StepFinishedEvent(stepName="step-1")

        service.agent = make_mock_agent(run_agui_fn=_run_agui)
        service.agent._pending_interrupt = {
            "interrupt_id": "interaction-step-crash",
            "tool_call_id": "tool-step-crash",
            "kind": "ask_user",
        }

        with patch(
            "src.api.services.agent_service.AgentInteractionService.create_pending"
        ) as create_pending:
            events = []
            with pytest.raises(RuntimeError, match="step persistence failed"):
                async for event in service._run_round_stream(
                    run_id="round-step-crash",
                    user_message="hello",
                ):
                    events.append(event)

        assert [event.type for event in events] == [EventType.CUSTOM]
        assert create_pending.call_args.kwargs["step_count"] == 1
        history_service.complete_round.assert_not_called()
        history_service.save_waiting_round_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_interaction_and_requested_event_share_one_commit_boundary(self):
        history_service = MagicMock()
        history_service.db = MagicMock()
        history_service.is_round_terminal = MagicMock(return_value=False)
        history_service.save_agui_event = AsyncMock(
            side_effect=RuntimeError("event persistence failed"),
        )
        history_service.complete_round = MagicMock()
        history_service.reset_session = MagicMock()
        history_service.last_terminal_event = None
        service = make_agent_service(history_service=history_service)

        async def _run_agui(**kwargs):
            yield CustomEvent(
                name="interaction_requested",
                value={
                    "interactionId": "interaction-atomic",
                    "runId": kwargs["run_id"],
                    "kind": "user_input",
                    "toolCallId": "tool-atomic",
                    "payload": {"questions": [{"question": "Continue?"}]},
                },
            )

        service.agent = make_mock_agent(run_agui_fn=_run_agui)
        service.agent._pending_interrupt = {
            "interrupt_id": "interaction-atomic",
            "tool_call_id": "tool-atomic",
            "kind": "ask_user",
        }

        with patch(
            "src.api.services.agent_service.AgentInteractionService.create_pending"
        ) as create_pending:
            with pytest.raises(RuntimeError, match="event persistence failed"):
                async for _ in service._run_round_stream(
                    run_id="round-atomic",
                    user_message="hello",
                ):
                    pass

        assert create_pending.call_args.kwargs["commit"] is False
        assert create_pending.call_args.kwargs["step_count"] == 1
        history_service.db.rollback.assert_called()
        assert history_service.complete_round.call_args.kwargs["status"] == "failed"

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
        assert all("文件已就绪" not in text for text in file_text_blocks)
        assert all("如需读取，请使用 read_file 工具" not in text for text in file_text_blocks)
        create_kwargs = service.history_service.create_round.call_args.kwargs
        assert create_kwargs["user_message"] == "read this"
        assert len(create_kwargs["user_attachments"]) == 2
        assert create_kwargs["user_attachments"][0]["path"] == "a.txt"

    @pytest.mark.asyncio
    async def test_workspace_attachment_is_frozen_before_round_creation(self, service):
        staged_entry = SimpleNamespace(
            entry_id="entry-1",
            relative_path="reports/report.xlsx",
            name="report.xlsx",
            kind="file",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        staged_result = SimpleNamespace(
            entry=staged_entry,
            destination_relative_path=".workspace-snapshots/entry-1/version-8/report.xlsx",
            source_revision=7,
            sha256="a" * 64,
            size_bytes=42,
            version_id="version-8",
            version_sequence=8,
        )
        workspace_service = MagicMock()
        workspace_service.get_entry = AsyncMock(return_value=staged_entry)
        workspace_service.stage_entry = AsyncMock(return_value=staged_result)

        with patch(
            "src.api.services.workspace_service.WorkspaceService",
            return_value=workspace_service,
            create=True,
        ):
            async for _event in service.chat_agui([{
                "type": "file",
                "file": {
                    "source": "workspace",
                    "entry_id": "entry-1",
                    "name": "forged-name.xlsx",
                    "size": 999,
                },
            }]):
                pass

        stage_kwargs = workspace_service.stage_entry.await_args.kwargs
        assert stage_kwargs["expected_revision"] is None
        assert stage_kwargs["destination_root"] == "/home/user/sessions/session-123"
        assert len(stage_kwargs["snapshot_id"]) == 32
        sent_content = service.agent.add_user_message.call_args.args[0]
        file_text = next(block["text"] for block in sent_content if block.get("type") == "text")
        assert '"path":".workspace-snapshots/entry-1/version-8/report.xlsx"' in file_text
        assert '"workspace_entry_id":"entry-1"' in file_text
        assert '"workspace_version_sequence":8' in file_text
        assert "逐字使用 metadata.path" not in file_text

        attachment = service.history_service.create_round.call_args.kwargs["user_attachments"][0]
        assert attachment == {
            "path": ".workspace-snapshots/entry-1/version-8/report.xlsx",
            "name": "report.xlsx",
            "type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size": 42,
            "source": "workspace",
            "entry_id": "entry-1",
            "revision": "7",
            "origin_path": "reports/report.xlsx",
            "snapshot_path": ".workspace-snapshots/entry-1/version-8/report.xlsx",
            "sha256": "a" * 64,
            "version_id": "version-8",
            "version_sequence": 8,
        }

    @pytest.mark.asyncio
    async def test_workspace_directory_attachment_is_live_reference(self, service):
        directory_entry = SimpleNamespace(
            entry_id="folder-1",
            relative_path="research",
            name="research",
            kind="directory",
            mime_type=None,
            revision=1,
            tree_revision=3,
            size_bytes=128,
        )
        workspace_service = MagicMock()
        workspace_service.get_entry = AsyncMock(return_value=directory_entry)
        workspace_service.stage_entry = AsyncMock()

        with patch(
            "src.api.services.workspace_service.WorkspaceService",
            return_value=workspace_service,
            create=True,
        ):
            async for _event in service.chat_agui([{
                "type": "file",
                "file": {
                    "source": "workspace",
                    "entry_id": "folder-1",
                    "kind": "directory",
                    "name": "forged-name",
                },
            }]):
                pass

        workspace_service.stage_entry.assert_not_awaited()
        sent_content = service.agent.add_user_message.call_args.args[0]
        folder_text = next(block["text"] for block in sent_content if block.get("type") == "text")
        assert folder_text.startswith("[附件文件夹] metadata=")
        assert '"path":"workspace://entry/folder-1"' in folder_text
        assert '"kind":"directory"' in folder_text
        assert '"workspace_reference_mode":"live"' in folder_text

        attachment = service.history_service.create_round.call_args.kwargs["user_attachments"][0]
        assert attachment == {
            "path": "workspace://entry/folder-1",
            "name": "research",
            "type": "inode/directory",
            "size": 128,
            "source": "workspace",
            "entry_id": "folder-1",
            "revision": "1",
            "origin_path": "research",
            "kind": "directory",
            "is_directory": True,
            "reference_mode": "live",
            "tree_revision": 3,
        }

    @pytest.mark.asyncio
    async def test_workspace_attachment_partial_stage_failure_discards_capture(self, service):
        from src.api.services.workspace_service import WorkspaceError

        staged = SimpleNamespace(
            entry=SimpleNamespace(
                entry_id="entry-1",
                relative_path="a.md",
                name="a.md",
                kind="file",
                mime_type="text/markdown",
            ),
            destination_relative_path=".workspace-snapshots/entry-1/capture/a.md",
            source_revision=1,
            sha256="a" * 64,
            size_bytes=1,
            version_id="version-1",
            version_sequence=1,
        )
        workspace_service = MagicMock()
        workspace_service.get_entry = AsyncMock(side_effect=[
            staged.entry,
            SimpleNamespace(
                entry_id="entry-2",
                relative_path="b.md",
                name="b.md",
                kind="file",
                mime_type="text/markdown",
            ),
        ])
        workspace_service.stage_entry = AsyncMock(side_effect=[
            staged,
            WorkspaceError(404, "ENTRY_NOT_FOUND", "missing"),
        ])
        service._discard_workspace_attachment_capture = AsyncMock()

        with patch(
            "src.api.services.workspace_service.WorkspaceService",
            return_value=workspace_service,
            create=True,
        ):
            with pytest.raises(WorkspaceError, match="missing"):
                await service._materialize_workspace_attachments([
                    {"type": "file", "file": {"source": "workspace", "entry_id": "entry-1", "name": "a.md"}},
                    {"type": "file", "file": {"source": "workspace", "entry_id": "entry-2", "name": "b.md"}},
                ])

        capture = service._discard_workspace_attachment_capture.await_args.args[0]
        assert [item.entry_id for item in capture.items] == ["entry-1"]

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
        assert file_text == f'[附件文件] metadata={{"name":"{filename}","path":"{filename}"}}'
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
        image_block = next(block for block in sent_content if block.get("type") == "image_url")
        assert "file" not in image_block
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
        assert "Load a skill's full content" not in runtime_provider()

    @pytest.mark.asyncio
    async def test_runtime_mcp_connections_are_compact_request_only_metadata(
        self,
        service,
    ):
        from src.api.services.mcp_runtime import McpConnectionSummary

        connection = McpConnectionSummary(
            server_id="server-1",
            server_name="同花顺股票 MCP",
            server_description="A 股实时行情、个股资料、财务和公告",
        )

        async def _create_tools(**kwargs):
            kwargs["build_metadata"]["mcp_connections"] = (connection, connection)
            return [], None

        service._provision_default_files_if_needed = MagicMock()
        service._load_system_prompt = MagicMock(return_value="stable system")
        service._restore_history = MagicMock()

        with patch("src.api.services.agent_service.settings") as mock_settings, patch(
            "src.api.services.agent_service.create_agent_tools",
            side_effect=_create_tools,
        ), patch("src.api.services.agent_service.LLMClient") as MockLLM, patch(
            "src.api.services.agent_service.Agent"
        ) as MockAgent:
            mock_settings.agent_max_steps = 10
            mock_settings.agent_tool_timeout = 300
            mock_settings.agent_subagent_max_parallel = 1
            MockLLM.from_model_config.return_value = MagicMock()

            await service.initialize_agent()

        agent_kwargs = MockAgent.call_args.kwargs
        runtime_provider = agent_kwargs["runtime_prompt_provider"]
        assert runtime_provider() == (
            "## 数据连接\n"
            "- 同花顺股票 MCP：A 股实时行情、个股资料、财务和公告"
        )
        assert "同花顺股票 MCP" not in agent_kwargs["system_prompt"]
        assert "mcp_tool_search" not in runtime_provider()

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
        history_service.complete_round = MagicMock()
        history_service.save_agui_event = AsyncMock()

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
        service._reasoning_context_from_round = MagicMock(return_value=None)
        return service

    @pytest.mark.asyncio
    async def test_resume_reuses_original_round(self, service):
        answers = {"Which DB?": "PostgreSQL"}
        service.history_service.db.query.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(step_count=1, user_message="original request")
        )
        service.history_service.is_round_terminal = MagicMock(return_value=False)
        service.history_service.save_agui_event = AsyncMock(side_effect=[
            StoredEvent(
                "round-original",
                1,
                {"type": "CUSTOM", "name": "interaction_resolved", "sequence": 1},
            ),
            None,
            StoredEvent(
                "round-original",
                2,
                {"type": "TEXT_MESSAGE_END", "messageId": "m1", "sequence": 2},
            ),
            StoredEvent(
                "round-original",
                3,
                {"type": "STEP_FINISHED", "stepName": "step-1", "sequence": 3},
            ),
        ])
        service.history_service.last_terminal_event = None

        with (
            patch.object(
                service,
                "_load_persisted_interrupt",
                return_value={
                    "interrupt_id": "iid-1",
                    "round_id": "round-original",
                    "tool_call_id": "tc-ask",
                    "kind": "ask_user",
                },
            ),
            patch.object(service, "_save_conversation_message") as save_message,
            patch(
                "src.api.services.agent_service.AgentInteractionService.answer_pending"
            ) as answer_pending,
            patch(
                "src.api.services.agent_service.AgentInteractionService.claim_answered_continuation",
                return_value=SimpleNamespace(claim_token="interaction-claim"),
            ) as claim_continuation,
            patch(
                "src.api.services.agent_service.AgentInteractionService.release_continuation_claim"
            ),
        ):
            events = []
            async for event in service.resume_agui("iid-1", answers):
                events.append(event)

        service.agent.resume_from_interrupt.assert_called_once_with("iid-1", answers)
        answer_pending.assert_called_once()
        claim_continuation.assert_called_once()
        persisted_calls = service.history_service.save_agui_event.await_args_list
        assert persisted_calls[0].kwargs["continuation_fence"].transition == "start"
        assert persisted_calls[1].kwargs["continuation_fence"].transition == "complete"
        assert persisted_calls[2].kwargs["continuation_fence"].transition == "complete"
        assert persisted_calls[3].kwargs["continuation_fence"] is None
        complete_kwargs = service.history_service.complete_round.call_args.kwargs
        assert complete_kwargs["round_id"] == "round-original"
        assert any(
            event.type == EventType.CUSTOM
            and event.name == "interaction_resolved"
            and event.value["runId"] == "round-original"
            for event in events
        )
        assert not any(
            call.args and call.args[0] == "user"
            for call in save_message.call_args_list
        )

    @pytest.mark.asyncio
    async def test_retry_reuses_first_durable_tool_result_order(
        self,
        service,
    ):
        incoming = "User answered:\n- First?: Yes\n- Second?: No"
        # Retries must reuse the first durable rendering even if wire key order differs.
        frozen = "User answered:\n- Second?: No\n- First?: Yes"
        tool_message = AgentHistoryMessage(
            role="tool",
            content="[Awaiting user response]",
            tool_call_id="tc-ask",
            name="ask_user",
        )
        service.agent.messages = [tool_message]
        service.agent._pending_interrupt = {
            "interrupt_id": "iid-order",
            "round_id": "round-original",
            "tool_call_id": "tc-ask",
            "kind": "ask_user",
        }
        service.history_service.db.query.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(step_count=1, user_message="original request")
        )

        def _resume(_interrupt_id, _answers):
            tool_message.content = incoming
            service.agent._pending_interrupt = None

        service.agent.resume_from_interrupt.side_effect = _resume
        with (
            patch.object(service, "_refresh_runtime_messages_from_history"),
            patch.object(
                service,
                "_load_persisted_interrupt",
                return_value={
                    "interrupt_id": "iid-order",
                    "round_id": "round-original",
                    "tool_call_id": "tc-ask",
                    "kind": "ask_user",
                },
            ),
            patch(
                "src.api.services.agent_service.AgentInteractionService.answer_pending",
                return_value=SimpleNamespace(tool_result_content=frozen),
            ),
        ):
            prepared = await service.prepare_resume_round(
                interrupt_id="iid-order",
                answers={"Second?": "No", "First?": "Yes"},
            )

        assert prepared.interaction_tool_result_content == frozen
        assert tool_message.content == frozen

    @pytest.mark.asyncio
    async def test_same_round_continuation_completes_on_first_durable_agent_event(
        self,
        service,
    ):
        service.history_service.is_round_terminal = MagicMock(return_value=False)
        service.history_service.last_terminal_event = None
        service.history_service.save_agui_event = AsyncMock(side_effect=[
            StoredEvent(
                "round-original",
                1,
                {"type": "CUSTOM", "name": "interaction_resolved", "sequence": 1},
            ),
            None,
            StoredEvent(
                "round-original",
                2,
                {"type": "TEXT_MESSAGE_END", "messageId": "m1", "sequence": 2},
            ),
        ])

        with (
            patch.object(service, "_save_conversation_message"),
            patch(
                "src.api.services.agent_service.AgentInteractionService.claim_answered_continuation",
                return_value=SimpleNamespace(claim_token="interaction-claim"),
            ),
            patch(
                "src.api.services.agent_service.AgentInteractionService.release_continuation_claim"
            ) as release_continuation,
        ):
            stream = service._run_round_stream(
                run_id="round-original",
                user_message="original request",
                is_continuation=True,
                interaction_id="iid-1",
                interaction_tool_call_id="tc-ask",
                interaction_tool_result_content="User answered:\n- Which DB?: PostgreSQL",
                interaction_kind="user_input",
            )
            prelude = await stream.__anext__()
            assert prelude.type == EventType.CUSTOM

            live_delta = await stream.__anext__()
            assert live_delta.type == EventType.TEXT_MESSAGE_CONTENT
            assert (
                service.history_service.save_agui_event.await_args_list[1]
                .kwargs["continuation_fence"]
                .transition
                == "complete"
            )

            durable_event = await stream.__anext__()
            assert durable_event.type == EventType.TEXT_MESSAGE_END
            assert (
                service.history_service.save_agui_event.await_args_list[2]
                .kwargs["continuation_fence"]
                .transition
                == "complete"
            )
            await stream.aclose()

        release_continuation.assert_not_called()

    @pytest.mark.asyncio
    async def test_uncommitted_continuation_start_releases_claim_and_stays_waiting(
        self,
        service,
    ):
        service.history_service.is_round_terminal = MagicMock(return_value=False)
        service.history_service.save_agui_event = AsyncMock(
            side_effect=RuntimeError("interaction_resolved persistence failed"),
        )
        service.history_service.get_round_status = MagicMock(
            return_value="waiting_interaction",
        )
        service.history_service.last_terminal_event = None

        with (
            patch(
                "src.api.services.agent_service.AgentInteractionService.claim_answered_continuation",
                return_value=SimpleNamespace(claim_token="interaction-claim"),
            ),
            patch(
                "src.api.services.agent_service.AgentInteractionService.release_continuation_claim"
            ) as release_continuation,
        ):
            with pytest.raises(
                RuntimeError,
                match="interaction_resolved persistence failed",
            ):
                async for _event in service._run_round_stream(
                    run_id="round-original",
                    user_message="original request",
                    is_continuation=True,
                    interaction_id="iid-uncommitted",
                    interaction_tool_call_id="tc-ask",
                    interaction_tool_result_content="User answered:\n- Continue?: Yes",
                    interaction_kind="user_input",
                ):
                    pass

        release_continuation.assert_called_once_with(
            service.history_service.db,
            session_id=service.session_id,
            interaction_id="iid-uncommitted",
            claim_token="interaction-claim",
        )
        service.history_service.complete_round.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_commit_fanout_failure_never_releases_started_continuation(
        self,
        service,
    ):
        service.history_service.is_round_terminal = MagicMock(return_value=False)
        # AguiEventBus commits before awaiting fanout.  The missing return
        # value therefore cannot by itself prove that start was uncommitted.
        service.history_service.save_agui_event = AsyncMock(
            side_effect=RuntimeError("committed fanout failed"),
        )
        service.history_service.get_round_status = MagicMock(return_value="running")
        service.history_service.last_terminal_event = None

        with (
            patch(
                "src.api.services.agent_service.AgentInteractionService.claim_answered_continuation",
                return_value=SimpleNamespace(claim_token="interaction-claim"),
            ),
            patch(
                "src.api.services.agent_service.AgentInteractionService.release_continuation_claim"
            ) as release_continuation,
        ):
            with pytest.raises(RuntimeError, match="committed fanout failed"):
                async for _event in service._run_round_stream(
                    run_id="round-original",
                    user_message="original request",
                    is_continuation=True,
                    interaction_id="iid-post-commit",
                    interaction_tool_call_id="tc-ask",
                    interaction_tool_result_content="User answered:\n- Continue?: Yes",
                    interaction_kind="user_input",
                ):
                    pass

        release_continuation.assert_not_called()
        complete_kwargs = service.history_service.complete_round.call_args.kwargs
        assert complete_kwargs["status"] == "failed"
        assert complete_kwargs["continuation_fence"].transition == "validate"
        assert complete_kwargs["continuation_fence"].claim_token == "interaction-claim"

    @pytest.mark.asyncio
    async def test_committed_continuation_startup_failure_uses_owned_terminal_fence(
        self,
        service,
    ):
        async def _failed_start(**_kwargs):
            raise RuntimeError("agent startup failed")
            yield  # pragma: no cover - keeps this an async generator

        service.agent.run_agui = _failed_start
        service.history_service.is_round_terminal = MagicMock(return_value=False)
        service.history_service.last_terminal_event = None
        service.history_service.save_agui_event = AsyncMock(return_value=StoredEvent(
            "round-original",
            1,
            {"type": "CUSTOM", "name": "interaction_resolved", "sequence": 1},
        ))

        with (
            patch(
                "src.api.services.agent_service.AgentInteractionService.claim_answered_continuation",
                return_value=SimpleNamespace(claim_token="interaction-claim"),
            ),
            patch(
                "src.api.services.agent_service.AgentInteractionService.release_continuation_claim"
            ) as release_continuation,
        ):
            stream = service._run_round_stream(
                run_id="round-original",
                user_message="original request",
                is_continuation=True,
                interaction_id="iid-startup",
                interaction_tool_call_id="tc-ask",
                interaction_tool_result_content="User answered:\n- Continue?: Yes",
                interaction_kind="user_input",
            )
            prelude = await stream.__anext__()
            assert prelude.type == EventType.CUSTOM
            with pytest.raises(RuntimeError, match="agent startup failed"):
                await stream.__anext__()

        release_continuation.assert_not_called()
        complete_kwargs = service.history_service.complete_round.call_args.kwargs
        assert complete_kwargs["status"] == "failed"
        assert complete_kwargs["continuation_fence"].transition == "validate"
        assert complete_kwargs["continuation_fence"].claim_token == "interaction-claim"

    @pytest.mark.asyncio
    async def test_committed_tool_approval_queue_failure_uses_owned_terminal_fence(
        self,
        service,
    ):
        service.history_service.is_round_terminal = MagicMock(return_value=False)
        service.history_service.last_terminal_event = None
        service.history_service.save_agui_event = AsyncMock(return_value=StoredEvent(
            "round-original",
            1,
            {"type": "CUSTOM", "name": "interaction_resolved", "sequence": 1},
        ))

        with (
            patch(
                "src.api.services.agent_service.AgentInteractionService.claim_answered_continuation",
                return_value=SimpleNamespace(claim_token="interaction-claim"),
            ),
            patch.object(
                service,
                "_claim_and_queue_tool_approval",
                side_effect=RuntimeError("approval queue failed"),
            ),
            patch(
                "src.api.services.agent_service.AgentInteractionService.release_continuation_claim"
            ) as release_continuation,
        ):
            stream = service._run_round_stream(
                run_id="round-original",
                user_message="original request",
                is_continuation=True,
                interaction_id="approval-startup",
                interaction_tool_call_id="tool-1",
                interaction_tool_result_content="[Tool approval execution pending]",
                interaction_kind="tool_approval",
                tool_approval_resolution="allow_once",
            )
            prelude = await stream.__anext__()
            assert prelude.type == EventType.CUSTOM
            with pytest.raises(RuntimeError, match="approval queue failed"):
                await stream.__anext__()

        release_continuation.assert_not_called()
        complete_kwargs = service.history_service.complete_round.call_args.kwargs
        assert complete_kwargs["status"] == "failed"
        assert complete_kwargs["continuation_fence"].transition == "validate"
        assert complete_kwargs["continuation_fence"].claim_token == "interaction-claim"

    @pytest.mark.asyncio
    async def test_tool_approval_continuation_waits_for_durable_tool_result(
        self,
        service,
    ):
        async def _approval_events(**_kwargs):
            yield CustomEvent(
                name="tool_approval_resume",
                value={"toolCallId": "tool-1"},
            )
            yield ToolCallResultEvent(
                messageId="tool-1:result",
                toolCallId="tool-1",
                content="tool result",
            )

        service.agent.run_agui = _approval_events
        service.history_service.is_round_terminal = MagicMock(return_value=False)
        service.history_service.last_terminal_event = None
        service.history_service.save_agui_event = AsyncMock(side_effect=[
            StoredEvent(
                "round-original",
                1,
                {"type": "CUSTOM", "name": "interaction_resolved", "sequence": 1},
            ),
            StoredEvent(
                "round-original",
                2,
                {"type": "CUSTOM", "name": "tool_approval_resume", "sequence": 2},
            ),
            StoredEvent(
                "round-original",
                3,
                {"type": "TOOL_CALL_RESULT", "toolCallId": "tool-1", "sequence": 3},
            ),
        ])

        with (
            patch.object(service, "_claim_and_queue_tool_approval"),
            patch(
                "src.api.services.agent_service.AgentInteractionService.claim_answered_continuation",
                return_value=SimpleNamespace(claim_token="interaction-claim"),
            ),
            patch(
                "src.api.services.agent_service.AgentInteractionService.release_continuation_claim"
            ) as release_continuation,
        ):
            stream = service._run_round_stream(
                run_id="round-original",
                user_message="original request",
                is_continuation=True,
                interaction_id="approval-1",
                interaction_tool_call_id="tool-1",
                interaction_tool_result_content="[Tool approval execution pending]",
                interaction_kind="tool_approval",
                tool_approval_resolution="allow_once",
            )
            await stream.__anext__()  # interaction_resolved
            approval_marker = await stream.__anext__()
            assert approval_marker.type == EventType.CUSTOM
            assert (
                service.history_service.save_agui_event.await_args_list[1]
                .kwargs["continuation_fence"]
                .transition
                == "validate"
            )

            tool_result = await stream.__anext__()
            assert tool_result.type == EventType.TOOL_CALL_RESULT
            assert (
                service.history_service.save_agui_event.await_args_list[2]
                .kwargs["continuation_fence"]
                .transition
                == "complete"
            )
            await stream.aclose()

        release_continuation.assert_not_called()

    def test_tool_approval_reuses_original_round(self, service):
        from src.agent.schema.run_context import AgentRunContext

        request = SimpleNamespace(
            id="approval-1",
            tool_call_id="tool-1",
            model_tool_name="protected_tool",
            provider="builtin",
            tool_name="protected_tool",
            server_id=None,
            installation_id=None,
            schema_hash=None,
            connection_fingerprint=None,
            status="requested",
            resolution=None,
        )
        service.history_service.db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = request
        round_row = SimpleNamespace(
            id="round-original",
            status="waiting_interaction",
            step_count=2,
            user_message="original request",
        )

        with (
            patch.object(service, "_refresh_runtime_messages_from_history"),
            patch(
                "src.api.services.agent_service.AgentInteractionService.lock_pending_for_update",
                return_value=(round_row, SimpleNamespace(id="approval-1")),
            ),
            patch(
                "src.api.services.agent_service.AgentInteractionService.answer_pending"
            ) as answer_pending,
            patch(
                "src.api.services.tool_permission_service.prepare_approval_request"
            ) as prepare_approval,
        ):
            prepared = service._prepare_tool_approval_resume_locked(
                interrupt_id="approval-1",
                answers={"approval": "allow_once"},
                parent_run_id="round-original",
                turn_preferences_origin_user_message_id="round-original:user",
                requested_context=None,
                run_context=AgentRunContext(),
            )

        assert prepared.run_id == "round-original"
        assert prepared.is_continuation is True
        assert prepared.initial_step == 2
        assert prepared.interaction_id == "approval-1"
        assert prepared.interaction_kind == "tool_approval"
        assert prepared.tool_approval_resolution == "allow_once"
        service.agent.queue_tool_approval_resume.assert_not_called()
        answer_pending.assert_called_once()
        prepare_approval.assert_called_once_with(
            service.history_service.db,
            request_id="approval-1",
            user_id=service.user_id,
            resolution="allow_once",
            commit=False,
        )

    def test_has_pending_interrupt_falls_back_to_persisted_interrupt(self, service):
        service.agent.has_pending_interrupt.return_value = False

        with patch.object(service, "_load_persisted_interrupt", return_value={"interrupt_id": "iid-1"}):
            assert service.has_pending_interrupt("iid-1") is True

    def test_has_pending_interrupt_discards_cancelled_same_round_hot_cache(self, service):
        service.agent._pending_interrupt = {
            "interrupt_id": "iid-cancelled",
            "round_id": "round-cancelled",
            "kind": "ask_user",
        }

        with patch.object(service, "_load_persisted_interrupt", return_value=None):
            assert service.has_pending_interrupt("iid-cancelled") is False

        assert service.agent._pending_interrupt is None
        assert "iid-cancelled" not in service._pending_interrupt_round_ids
