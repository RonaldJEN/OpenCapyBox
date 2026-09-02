"""Agent 服务 - 连接 OpenCapyBox 核心"""
import asyncio
import copy
import contextvars
import inspect
import json
import logging
import posixpath
import shlex
import uuid
from dataclasses import dataclass, field
from typing import List, Dict, Optional, AsyncIterator, Any, Callable

from opensandbox import Sandbox
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DBSession

from src.api.utils.timezone import now_naive
from src.agent.agent import Agent, ContinuationOwnershipLostError
from src.agent.llm import LLMClient
from src.agent.schema import Message as AgentMessage
from src.agent.schema.agui_events import AGUIEvent, Context, CustomEvent, EventType
from src.agent.schema.run_context import (
    AgentRunContext,
    LLMRequestContext,
    RequestedReasoningContext,
    RequestedTurnPreferencesContext,
    PendingFileDraftRef,
    ResolvedMcpConnectionRef,
    ResolvedReasoningContext,
    ResolvedSkillRef,
    ResolvedTurnPreferencesContext,
    current_run_context,
    parse_requested_reasoning_contexts,
    parse_pending_file_draft_contexts,
    parse_requested_turn_preferences_contexts,
    requested_turn_preferences_to_context,
    resolve_reasoning_selection,
)

from src.api.services.history_service import HistoryService
from src.api.services.agent_interaction_service import (
    AgentInteractionService,
    ContinuationWriteFence,
    DEFAULT_CONTINUATION_LEASE_SECONDS,
    InteractionConflictError,
)
from src.api.services.agui_event_bus import (
    RoundTerminalWriteSuppressed,
    SequencedAGUIEvent,
    StoredEvent,
    get_agui_event_bus,
)
from src.api.services.run_completion_service import RunCompletionService
from src.api.services.context_checkpoint_service import ContextCheckpointService
from src.api.services.sandbox_service import get_sandbox_service
from src.api.services.subagent_graph_service import get_subagent_graph_service
from src.api.services.tool_factory import create_agent_tools
from src.api.config import get_settings
from src.api.model_registry import get_model_registry
from src.agent.tools.base import ToolResult, ToolRuntimeContext
from pathlib import Path as PathlibPath

logger = logging.getLogger(__name__)

TURN_PREFERENCES_ORIGIN_USER_MESSAGE_ID_KEY = (
    "turn_preferences_origin_user_message_id"
)


class DuplicateRoundError(Exception):
    """幂等衝突：另一個 Worker 已搶先創建了相同 idempotency_key 的 Round"""
    def __init__(self, existing_round_id: str):
        self.existing_round_id = existing_round_id
        super().__init__(f"Duplicate round: {existing_round_id}")


class InvalidInteractionResponseError(ValueError):
    """A Human-in-the-Loop response does not match its durable interaction kind."""


class _InvalidCheckpointSourceError(RuntimeError):
    """The checkpoint cursor cannot be located in authoritative main history."""


class _RunLivenessLostError(RuntimeError):
    """The worker no longer owns the UserRunLock that authorizes production."""


class _CombinedRunStopToken:
    """Duck-typed asyncio.Event view over independent stop reasons."""

    def __init__(self, *tokens: asyncio.Event | None):
        self._tokens = tuple(token for token in tokens if token is not None)

    def is_set(self) -> bool:
        return any(token.is_set() for token in self._tokens)

    async def wait(self) -> bool:
        if self.is_set():
            return True
        waiters = [asyncio.create_task(token.wait()) for token in self._tokens]
        if not waiters:
            await asyncio.Future()
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            return True
        finally:
            for waiter in waiters:
                waiter.cancel()
            if waiters:
                await asyncio.gather(*waiters, return_exceptions=True)


@dataclass(frozen=True)
class PreparedAgentRun:
    """A round that has been created and is ready to execute."""

    run_id: str
    user_message: str
    user_message_id: str = ""
    context: AgentRunContext = field(default_factory=AgentRunContext)
    requested_context: RequestedTurnPreferencesContext | None = None
    parent_run_id: str | None = None
    is_continuation: bool = False
    initial_step: int = 0
    interaction_id: str | None = None
    interaction_tool_call_id: str | None = None
    interaction_tool_result_content: str | None = None
    interaction_kind: str | None = None
    tool_approval_resolution: str | None = None


@dataclass(frozen=True)
class _StagedWorkspaceAttachment:
    entry_id: str
    version_id: str | None
    destination_relative_path: str


@dataclass
class _WorkspaceAttachmentCapture:
    capture_id: str
    items: list[_StagedWorkspaceAttachment] = field(default_factory=list)


settings = get_settings()


def _compact_mcp_routing_text(value: object, *, limit: int) -> str:
    """Normalize user-visible connection metadata into one bounded prompt line."""

    text = " ".join(str(value or "").split())
    return text[:limit]


def _render_mcp_connections_runtime_prompt(connections: object) -> str:
    """Render compact connection labels without exposing remote tool schemas."""

    if not isinstance(connections, (list, tuple)):
        return ""

    entries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for connection in connections:
        if isinstance(connection, dict):
            raw_name = connection.get("server_name")
            raw_description = connection.get("server_description")
        else:
            raw_name = getattr(connection, "server_name", None)
            raw_description = getattr(connection, "server_description", None)
        name = _compact_mcp_routing_text(raw_name, limit=100)
        if not name:
            continue
        description = _compact_mcp_routing_text(raw_description, limit=500)
        key = (name, description)
        if key in seen:
            continue
        seen.add(key)
        entries.append(key)

    if not entries:
        return ""

    entries.sort(key=lambda item: (item[0].casefold(), item[1].casefold()))
    lines = ["## 数据连接"]
    lines.extend(
        f"- {name}：{description}" if description else f"- {name}"
        for name, description in entries
    )
    return "\n".join(lines)


def get_sandbox_mount_path(user_id: str | None = None) -> str:
    """Compatibility wrapper around the profile-aware sandbox service mount path."""
    return get_sandbox_service().get_mount_path(user_id)


class AgentService:
    """Agent 服务"""

    def __init__(
        self,
        sandbox: Sandbox,
        history_service: HistoryService,
        session_id: str,
        user_id: str,
        model_id: str | None = None,
        tool_exclude: set[str] | None = None,
        system_prompt_override: str | None = None,
        workspace_dir: str | None = None,
        allow_human_interrupts: bool = True,
        restore_history: bool = True,
        persist_context_checkpoint: bool = True,
        workspace_access: str = "manage",
        workspace_actor: str = "chat",
        workspace_fence: Callable[[DBSession], None] | None = None,
        workspace_change_recorder: Callable[[DBSession, dict[str, Any]], None] | None = None,
        workspace_context: dict[str, Any] | None = None,
    ):
        self.sandbox = sandbox
        self.history_service = history_service
        self.session_id = session_id
        self.user_id = user_id
        self.model_id = model_id
        self.tool_exclude = set(tool_exclude or set())
        self.system_prompt_override = system_prompt_override
        self.allow_human_interrupts = allow_human_interrupts
        self.restore_history = restore_history
        self.persist_context_checkpoint = persist_context_checkpoint
        if workspace_access not in {"none", "read", "edit", "manage"}:
            raise ValueError("workspace_access 必须为 none/read/edit/manage")
        self.workspace_access = workspace_access
        self.workspace_actor = workspace_actor
        self.workspace_fence = workspace_fence
        self.workspace_change_recorder = workspace_change_recorder
        self.workspace_context = dict(workspace_context or {})
        self.agent: Agent | None = None
        self._model_config = None
        self._last_saved_index = 0
        self._pending_interrupt_round_ids: dict[str, str] = {}
        self.skill_loader = None  # 保存 skill_loader 引用
        self.mcp_connections: tuple[object, ...] = ()
        self.mcp_catalog_fingerprint: str | None = None
        self.mcp_catalog_configuration_fingerprint: str | None = None
        self.mcp_catalog_retry_required = False
        self.cancel_token: asyncio.Event | None = None  # per-run 取消令牌
        self.liveness_token: asyncio.Event | None = None  # UserRunLock 所有权信号
        self._active_checkpoint_id: str | None = None
        self._active_checkpoint_sha256: str | None = None
        self._resume_lock = asyncio.Lock()  # 防止并发 resume 调用
        self._active_run_count = 0
        # 每個 session 使用沙箱內的隔離子目錄
        mount = get_sandbox_mount_path(user_id)
        self._workspace_dir = workspace_dir or (f"{mount}/sessions/{session_id}" if session_id else mount)

    @property
    def is_running(self) -> bool:
        """当前 AgentService 是否正在消费一个 round 流。"""
        return self._active_run_count > 0

    async def initialize_agent(self):
        """初始化 Agent（使用 Model Registry 驅動 LLM 配置）"""
        # === 從 Model Registry 創建 LLM 客戶端 ===
        try:
            registry = get_model_registry()
            if getattr(registry, "source", "") == "db":
                from src.api.models.database import SessionLocal
                from src.api.services.model_access_service import (
                    assert_user_can_access_model,
                    list_accessible_model_configs,
                    resolve_default_model_for_user,
                )

                with SessionLocal() as access_db:
                    if self.model_id:
                        model_config = assert_user_can_access_model(
                            access_db,
                            self.user_id,
                            self.model_id,
                            registry,
                        )
                    else:
                        model_config = resolve_default_model_for_user(
                            access_db,
                            self.user_id,
                            registry=registry,
                        )
                        self.model_id = model_config.id
                    registry_models = list_accessible_model_configs(
                        access_db,
                        self.user_id,
                        registry,
                    )
            elif self.model_id:
                model_config = registry.get_or_raise(self.model_id)
                registry_models = (
                    registry.list_models(enabled_only=True)
                    if hasattr(registry, "list_models")
                    else [model_config]
                )
            else:
                model_config = registry.get_default()
                self.model_id = model_config.id
                registry_models = (
                    registry.list_models(enabled_only=True)
                    if hasattr(registry, "list_models")
                    else [model_config]
                )

            self._token_limit = model_config.compute_token_limit()
            self._model_config = model_config

            logger.info(
                "创建 LLM 客户端: model=%s, provider=%s, api_base=%s",
                model_config.model_name, model_config.provider, model_config.api_base,
            )

            # 收集 fallback 模型（排除当前主模型，按目录顺序），并保持多模态能力不降级。
            fallback_configs = [
                m for m in registry_models
                if m.id != model_config.id
                and self._is_fallback_model_compatible(model_config, m)
            ]
            llm_client = LLMClient.from_model_config(
                model_config,
                fallback_configs=fallback_configs,
            )

        except FileNotFoundError as e:
            raise RuntimeError(
                f"Model Registry 不可用: {e}. "
                "請檢查数据库模型目录或首次 seed 的 models.yaml。"
            ) from e

        except ValueError as e:
            if self.model_id and ("不存在" in str(e) or "已停用" in str(e)):
                raise
            raise RuntimeError(
                f"Model Registry 配置異常: {e}. "
                "請修復数据库模型配置或环境变量后重试。"
            ) from e

        # === 新用户默认文件初始化 ===
        self._provision_default_files_if_needed()

        # 加载 system prompt：子 Agent 走 profile 精简提示（override），否则拼装父记忆
        system_prompt = self.system_prompt_override or self._load_system_prompt()

        # 创建工具列表
        tool_build_metadata: dict[str, object] = {}
        tools, self.skill_loader = await create_agent_tools(
            sandbox=self.sandbox,
            workspace_dir=self._workspace_dir,
            mount=get_sandbox_mount_path(self.user_id),
            user_id=self.user_id,
            db_session_factory=self._get_db_session_factory(),
            subagent_runner=self._run_subagent_invocation,
            exclude=self.tool_exclude,
            supports_image=model_config.supports_image,
            max_images=model_config.max_images,
            build_metadata=tool_build_metadata,
            workspace_access=self.workspace_access,
            workspace_actor=self.workspace_actor,
            workspace_fence=self.workspace_fence,
            workspace_change_recorder=self.workspace_change_recorder,
            workspace_context=self.workspace_context,
        )
        exact_mcp_fingerprint = tool_build_metadata.get("mcp_catalog_fingerprint")
        self.mcp_catalog_fingerprint = (
            exact_mcp_fingerprint
            if isinstance(exact_mcp_fingerprint, str) and exact_mcp_fingerprint
            else None
        )
        exact_mcp_configuration_fingerprint = tool_build_metadata.get(
            "mcp_catalog_configuration_fingerprint"
        )
        self.mcp_catalog_configuration_fingerprint = (
            exact_mcp_configuration_fingerprint
            if (
                isinstance(exact_mcp_configuration_fingerprint, str)
                and exact_mcp_configuration_fingerprint
            )
            else self.mcp_catalog_fingerprint
        )
        self.mcp_catalog_retry_required = (
            tool_build_metadata.get("mcp_catalog_retry_required") is True
        )
        raw_mcp_connections = tool_build_metadata.get("mcp_connections", ())
        self.mcp_connections = (
            tuple(raw_mcp_connections)
            if isinstance(raw_mcp_connections, (list, tuple))
            else ()
        )

        # 技能和数据连接元数据按 LLM 请求动态生成，不写入长期 system message。
        runtime_prompt_provider = None
        mcp_connections_prompt = _render_mcp_connections_runtime_prompt(
            self.mcp_connections
        )
        if self.skill_loader or mcp_connections_prompt:
            def _build_tools_runtime_prompt() -> str:
                parts: list[str] = []
                if self.skill_loader:
                    try:
                        self.skill_loader.refresh_disabled_skills()
                    except Exception as e:
                        # SkillLoader normally keeps the last-known snapshot itself;
                        # retain metadata even for alternate/test loaders that raise.
                        logger.warning("刷新请求级 Skill 启停配置失败，沿用上次状态: %s", e)
                    skills_metadata = self.skill_loader.get_skills_metadata_prompt()
                    if skills_metadata:
                        parts.append(f"## 已注册技能列表\n\n{skills_metadata}")
                if mcp_connections_prompt:
                    parts.append(mcp_connections_prompt)
                return "\n\n".join(parts)

            runtime_prompt_provider = _build_tools_runtime_prompt

        deferred_tool_retriever = None
        deferred_tool_catalog_is_current = None
        if any(tool.tool_ref.provider == "mcp" for tool in tools):
            from src.api.services.mcp_runtime import get_mcp_runtime
            from src.api.services.mcp_tool_search_service import (
                get_mcp_tool_search_service,
            )

            deferred_tool_retriever = get_mcp_tool_search_service()
            expected_mcp_configuration_fingerprint = (
                self.mcp_catalog_configuration_fingerprint
            )
            mcp_runtime = get_mcp_runtime()

            def _mcp_catalog_is_current() -> bool:
                if (
                    not self.user_id
                    or not expected_mcp_configuration_fingerprint
                ):
                    return False
                try:
                    return (
                        mcp_runtime.catalog_configuration_fingerprint(
                            self.user_id
                        )
                        == expected_mcp_configuration_fingerprint
                    )
                except Exception:
                    logger.warning(
                        "检查 MCP 工具检索目录版本失败，隐藏旧目录工具: user=%s",
                        self.user_id,
                        exc_info=True,
                    )
                    return False

            deferred_tool_catalog_is_current = _mcp_catalog_is_current

        # 创建 Agent
        self.agent = Agent(
            llm_client=llm_client,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=settings.agent_max_steps,
            workspace_dir=self._workspace_dir,  # 沙箱中的工作目錄
            token_limit=self._token_limit,
            context_window=model_config.context_window,
            max_output_tokens=model_config.max_tokens,  # output token limit, not context
            tool_timeout=settings.agent_tool_timeout,
            subagent_max_parallel=settings.agent_subagent_max_parallel,
            runtime_prompt_provider=runtime_prompt_provider,
            deferred_tool_retriever=deferred_tool_retriever,
            deferred_tool_catalog_is_current=deferred_tool_catalog_is_current,
            user_id=self.user_id,
            allow_human_interrupts=self.allow_human_interrupts,
            auto_compact_token_limit=getattr(model_config, "auto_compact_token_limit", None),
            tool_output_truncation_bytes=getattr(model_config, "tool_output_truncation_bytes", 42667),
            supports_image=bool(getattr(model_config, "supports_image", False)),
            supports_video=bool(getattr(model_config, "supports_video", False)),
        )

        if self.restore_history:
            self._restore_history()

    @staticmethod
    def _is_fallback_model_compatible(primary_config, fallback_config) -> bool:
        """Fallback must preserve multimodal capability promised by the primary model."""
        if getattr(primary_config, "supports_image", False):
            if not getattr(fallback_config, "supports_image", False):
                return False
            if int(getattr(fallback_config, "max_images", 0) or 0) < int(getattr(primary_config, "max_images", 0) or 0):
                return False
        if getattr(primary_config, "supports_video", False):
            if not getattr(fallback_config, "supports_video", False):
                return False
            if int(getattr(fallback_config, "max_videos", 0) or 0) < int(getattr(primary_config, "max_videos", 0) or 0):
                return False
        return True

    @staticmethod
    def _has_synthetic_user_content(content: Any) -> bool:
        return content not in (None, "", [])

    @staticmethod
    def _synthetic_user_content_marker(content: Any) -> dict[str, Any]:
        """Return a lightweight AG-UI marker for backend-only synthetic content."""
        marker: dict[str, Any] = {
            "schema": "synthetic_user_message_ref.v1",
            "contentRef": "conversation_messages",
        }
        if isinstance(content, list):
            image_count = sum(
                1
                for block in content
                if isinstance(block, dict)
                and (block.get("type") == "image_url" or "image_url" in block)
            )
            marker.update({
                "contentKind": "blocks",
                "blockCount": len(content),
                "imageCount": image_count,
            })
        elif isinstance(content, str):
            marker.update({
                "contentKind": "text",
                "charCount": len(content),
            })
        elif isinstance(content, dict):
            marker.update({
                "contentKind": "object",
                "fieldCount": len(content),
            })
        else:
            marker["contentKind"] = type(content).__name__
        return marker

    @staticmethod
    def _synthetic_user_content_from_event(event: AGUIEvent) -> Any | None:
        value = getattr(event, "value", None)
        if not isinstance(value, dict):
            return None
        return value.get("content")

    @staticmethod
    def _lightweight_synthetic_user_event(event: AGUIEvent, content: Any) -> AGUIEvent:
        marker = AgentService._synthetic_user_content_marker(content)
        if hasattr(event, "model_copy"):
            return event.model_copy(update={"value": marker})
        event_copy = copy.copy(event)
        setattr(event_copy, "value", marker)
        return event_copy

    def _get_db_session_factory(self):
        """返回 DB session 工厂函数（供 memory_tools 延迟获取 DB session）"""
        if self.history_service.session_factory is not None:
            return self.history_service.session_factory
        from src.api.models.database import SessionLocal
        return SessionLocal

    @staticmethod
    def _format_subagent_user_message(
        *,
        prompt: str,
        subagent_type: str,
        description: str,
        parent_run_id: str,
        tool_call_id: str,
    ) -> str:
        parts = [
            "You are a child agent run spawned by a parent OpenCapyBox agent.",
            "Complete only the delegated task. Use your available tools when useful.",
            "Do not ask the user questions; if information is missing, state the assumption and proceed.",
            "Report concise, concrete results back to the parent agent.",
            "",
            f"Sub-agent type: {subagent_type or 'general'}",
            f"Description: {description or '(none)'}",
            f"Parent run id: {parent_run_id}",
            f"Parent tool call id: {tool_call_id}",
            "",
            "Task:",
            prompt,
        ]
        return "\n".join(parts)

    @staticmethod
    def _subagent_tool_result_content(
        *,
        child_run_id: str,
        edge_id: str,
        status: str,
        agent_type: str,
        model_id: str | None,
        output: str,
    ) -> str:
        header = [
            "Sub-agent run finished.",
            f"child_run_id: {child_run_id}",
            f"edge_id: {edge_id}",
            f"status: {status}",
            f"agent_type: {agent_type or 'general'}",
        ]
        if model_id:
            header.append(f"model_id: {model_id}")
        header.append("")
        header.append("Result:")
        header.append(output or "(no output)")
        return "\n".join(header)

    async def _run_subagent_invocation(
        self,
        *,
        prompt: str,
        subagent_type: str,
        description: str,
        context: ToolRuntimeContext,
    ) -> ToolResult:
        """Run sub_agent as a real child Round and return its final output."""
        from src.api.models.round import Round
        from src.api.models.subagent_run import SubagentRun
        from src.agent.subagent_profiles import resolve_profile

        profile = resolve_profile(subagent_type)

        db_factory = self._get_db_session_factory()
        graph_db = db_factory()
        child_history = HistoryService(db_factory)
        child_service: AgentService | None = None
        edge_id: str | None = None
        child_run_id = str(uuid.uuid4())
        child_status = SubagentRun.FAILED
        child_output = ""
        child_error: str | None = None

        try:
            registry = get_model_registry()
            if getattr(registry, "source", "") == "db":
                from src.api.services.model_access_service import resolve_default_model_for_user

                subagent_model = resolve_default_model_for_user(
                    graph_db,
                    self.user_id,
                    kind="subagent",
                    registry=registry,
                )
            else:
                subagent_model = registry.get_subagent_default()
            model_id = subagent_model.id

            graph_service = get_subagent_graph_service()
            edge = graph_service.create_edge(
                graph_db,
                user_id=self.user_id,
                session_id=self.session_id,
                parent_run_id=context.run_id,
                tool_call_id=context.tool_call_id,
                agent_type=subagent_type or "general",
                agent_name=description or None,
                model_id=model_id,
                description=description or None,
                prompt=prompt,
                status=SubagentRun.REQUESTED,
                metadata={
                    "executor": "AgentService.sub_agent",
                    "mode": "sync_child_round",
                    "profile": profile.name,
                },
            )
            edge_id = edge.id

            child_service = AgentService(
                sandbox=self.sandbox,
                history_service=child_history,
                session_id=self.session_id,
                user_id=self.user_id,
                model_id=model_id,
                tool_exclude=set(profile.tool_exclude),
                system_prompt_override=profile.system_prompt,
                allow_human_interrupts=False,
                restore_history=False,
                persist_context_checkpoint=False,
                workspace_access={
                    "research": "read",
                    "write": "edit",
                    "general": "manage",
                }.get(profile.name, "none"),
                workspace_actor="chat",
            )
            await child_service.initialize_agent()
            child_service.cancel_token = context.cancel_token

            if not child_service.agent:
                raise RuntimeError("child agent failed to initialize")

            # Subagents are sidechains: they get their own task prompt, not the
            # full parent conversation replay. Their transcript is persisted via
            # the child Round and graph edge.
            child_user_message = self._format_subagent_user_message(
                prompt=prompt,
                subagent_type=subagent_type,
                description=description,
                parent_run_id=context.run_id,
                tool_call_id=context.tool_call_id,
            )

            child_service.history_service.create_round(
                session_id=self.session_id,
                round_id=child_run_id,
                user_message=child_user_message,
                user_attachments=[],
                parent_run_id=context.run_id,
            )
            child_service.agent.add_user_message(child_user_message)
            child_service._save_conversation_message(
                "user",
                child_user_message,
                round_id=child_run_id,
            )

            graph_service.attach_child_run(
                graph_db,
                edge_id=edge_id,
                user_id=self.user_id,
                session_id=self.session_id,
                child_run_id=child_run_id,
                status=SubagentRun.RUNNING,
            )

            async for _event in child_service.run_prepared_round(
                PreparedAgentRun(
                    run_id=child_run_id,
                    user_message=child_user_message,
                    parent_run_id=context.run_id,
                ),
                error_label="Sub-agent 执行失败",
            ):
                pass

            child_round = (
                child_history.db.query(Round)
                .filter(Round.id == child_run_id, Round.session_id == self.session_id)
                .first()
            )
            round_status = getattr(child_round, "status", None) or "failed"
            child_output = getattr(child_round, "final_response", None) or ""

            if round_status == "completed":
                child_status = SubagentRun.COMPLETED
            elif round_status == "cancelled":
                child_status = SubagentRun.CANCELLED
                child_error = "sub-agent was cancelled"
            elif round_status == "failed":
                child_status = SubagentRun.FAILED
                child_error = child_output or "sub-agent failed"
            elif round_status == "max_steps_reached":
                child_status = SubagentRun.FAILED
                child_error = child_output or "sub-agent reached max steps"
            else:
                child_status = SubagentRun.FAILED
                child_error = f"sub-agent ended in unsupported status: {round_status}"

            graph_service.mark_status(
                graph_db,
                edge_id=edge_id,
                user_id=self.user_id,
                session_id=self.session_id,
                status=child_status,
                output=child_output if child_status == SubagentRun.COMPLETED else None,
                error=child_error,
            )

            if child_status != SubagentRun.COMPLETED:
                return ToolResult(success=False, error=child_error or "sub-agent failed")

            child_file_references: list[dict[str, Any]] = []
            child_history._rebuild_steps_from_events(
                child_run_id,
                assistant_file_references=child_file_references,
            )
            deduplicated_child_references: dict[str, dict[str, Any]] = {}
            for reference in child_file_references:
                identity = (
                    f"workspace:{reference.get('entry_id')}"
                    if reference.get("source") == "workspace"
                    else (
                        f"session:{reference.get('session_id')}:"
                        f"{reference.get('path')}"
                    )
                )
                deduplicated_child_references[identity] = reference

            return ToolResult(
                success=True,
                content=self._subagent_tool_result_content(
                    child_run_id=child_run_id,
                    edge_id=edge_id,
                    status=child_status,
                    agent_type=subagent_type,
                    model_id=model_id,
                    output=child_output,
                ),
                assistant_file_references=list(
                    deduplicated_child_references.values()
                ) or None,
            )

        except Exception as exc:
            child_error = f"{type(exc).__name__}: {exc}"
            if edge_id:
                try:
                    get_subagent_graph_service().mark_status(
                        graph_db,
                        edge_id=edge_id,
                        user_id=self.user_id,
                        session_id=self.session_id,
                        status=SubagentRun.FAILED,
                        error=child_error,
                    )
                except Exception:
                    logger.warning("标记 subagent edge 失败: edge=%s", edge_id, exc_info=True)
            return ToolResult(success=False, error=f"sub-agent execution failed: {child_error}")
        finally:
            if child_service is not None:
                child_service.close()
            child_history.close()
            graph_db.close()

    def close(self) -> None:
        """释放 AgentService 持有的独立资源。"""
        self.history_service.close()

    def _load_system_prompt(self) -> str:
        """从 DB 用户配置和平台 AGENTS.md 模板组装 system prompt。

        MEMORY.md 在两阶段记忆管道完成前不注入。
        """
        memory_context = self._build_memory_context()
        if memory_context:
            return memory_context
        # fallback：DB 中无记忆文件（理论上新用户已通过 provision 注入）
        return "You are OpenCapyBox, a versatile AI assistant. Help the user with their tasks."

    def _provision_default_files_if_needed(self) -> None:
        """为新用户写入默认注入文件模板（幂等）

        检查 DB 中是否存在用户记忆文件，如果不存在则从 docs/ 模板写入默认值。
        初始化：SOUL.md, MEMORY.md, USER.md(PROFILE)。AGENTS.md 由平台模板直接注入。
        其中 MEMORY.md 仅持久化，不在止血阶段注入 system prompt。
        """
        try:
            from src.api.services.memory_service import MemoryService

            db = self.history_service.db
            mem_svc = MemoryService(db)
            count = mem_svc.provision_default_files(self.user_id)
            if count > 0:
                logger.info("新用户默认文件初始化完成: user=%s, count=%d", self.user_id, count)
        except Exception as e:
            logger.warning("默认文件初始化失败（非致命）: %s", e)
        finally:
            self.history_service.reset_session()

    def _build_memory_context(self) -> str:
        """组装 system prompt 前缀。

        SOUL/USER 来自用户 DB；AGENTS.md 始终来自平台模板。
        MEMORY.md 暂时仅保留在 DB/沙箱，不拼接到 system prompt。
        """
        try:
            from src.api.services.memory_service import MemoryService

            db = self.history_service.db
            mem_svc = MemoryService(db)
            all_files = mem_svc.get_all_memory_files(self.user_id)
            agents = mem_svc.get_agents_template_content()

            if not all_files and not agents.strip():
                return ""

            parts: list[str] = []

            soul = all_files.get("soul_md", "")
            if soul:
                parts.append(f"## Agent 人格\n{soul}\n")

            user = all_files.get("user_md", "")
            if user:
                parts.append(f"## 用户画像\n{user}\n")

            if agents:
                parts.append(f"## 行为规则\n{agents}\n")

            # TEMPORARY SAFETY STOP: canonical MEMORY.md is deliberately not
            # prompt-loaded. The future read path will inject only a bounded
            # consolidated summary and retrieve scoped memory blocks on demand.

            if not parts:
                return ""

            return "\n".join(parts) + "\n---\n\n"

        except Exception as e:
            logger.warning("构建记忆上下文失败: %s", e)
            return ""
        finally:
            self.history_service.reset_session()

    @staticmethod
    def _truncate_to_tokens(text: str, max_tokens: int, count_fn) -> str:
        """按 token 数截断文本"""
        if count_fn(text) <= max_tokens:
            return text
        # 二分截断
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if count_fn(text[:mid]) <= max_tokens:
                low = mid
            else:
                high = mid - 1
        return text[:low] + "\n...(truncated)"

    def _build_restored_history_messages(self) -> list[AgentMessage]:
        """从 DB 权威历史构建本轮 Agent runtime messages。

        默认优先恢复最新 v4 replacement，并从精确游标回放未覆盖 suffix；
        没有可用 v4 checkpoint 时从 rounds / conversation_messages /
        agui_events 重建完整权威历史。消息条数裁剪仅属于
        legacy_120 回滚策略。
        """
        from src.api.config import get_settings as _get_settings

        strategy = _get_settings().agent_history_strategy
        if strategy == "checkpoint_v1":
            checkpoint = ContextCheckpointService(self.history_service.db).load_latest(
                self.session_id,
            )
            if checkpoint is not None:
                try:
                    tail = self._rebuild_messages_after_checkpoint(checkpoint)
                except _InvalidCheckpointSourceError as exc:
                    logger.warning(
                        "累计上下文 checkpoint 游标无效，忽略整个 replacement 并从权威历史重建: "
                        "checkpoint=%s session=%s error=%s",
                        checkpoint.checkpoint_id,
                        self.session_id,
                        exc,
                    )
                else:
                    self._active_checkpoint_id = checkpoint.checkpoint_id
                    self._active_checkpoint_sha256 = None
                    logger.info(
                        "命中累计上下文 checkpoint: generation=%d replacement=%d tail=%d session=%s",
                        checkpoint.generation,
                        len(checkpoint.messages),
                        len(tail),
                        self.session_id,
                    )
                    return [
                        *[message.model_copy(deep=True) for message in checkpoint.messages],
                        *tail,
                    ]

        self._active_checkpoint_id = None
        self._active_checkpoint_sha256 = None
        messages = self._rebuild_messages_from_events()
        if strategy == "checkpoint_v1":
            # Migration path: rebuild the complete authoritative history once;
            # Agent token projection will compact it before the provider call
            # and persist the first cumulative replacement checkpoint.
            return messages
        if not messages:
            return []

        # legacy fallback only: the checkpoint strategy never slices by count.
        max_msgs = _get_settings().agent_max_history_messages
        if messages and len(messages) > max_msgs:
            start_idx = len(messages) - max_msgs
            trimmed = messages[start_idx:]
            # 確保從真實 user 消息邊界開始（跳過 synthetic）
            while trimmed and (trimmed[0].role != "user" or trimmed[0].is_synthetic):
                trimmed = trimmed[1:]
            if not trimmed:
                # 回退策略：從窗口起點向前回溯到最近真實 user 邊界，避免整段失憶
                fallback_idx = next(
                    (
                        i
                        for i in range(start_idx - 1, -1, -1)
                        if messages[i].role == "user" and not messages[i].is_synthetic
                    ),
                    None,
                )
                if fallback_idx is not None:
                    trimmed = messages[fallback_idx:]
                    logger.warning(
                        "歷史消息尾窗最近 %d 條無真實 user 邊界，已回退到最近真實 user@index=%d，"
                        "實際注入 %d 條 (session=%s)",
                        max_msgs,
                        fallback_idx,
                        len(trimmed),
                        self.session_id,
                    )
                else:
                    # 極端兜底：整體歷史不存在真實 user，保留尾窗避免全空。
                    logger.error(
                        "歷史消息中不存在真實 user 邊界，保留最近 %d 條作為兜底 (session=%s)",
                        max_msgs,
                        self.session_id,
                    )
                    trimmed = messages[start_idx:]
            logger.warning(
                "歷史消息 %d 條超過上限 %d，保留最近 %d 條 (session=%s)",
                len(messages), max_msgs, len(trimmed), self.session_id,
            )
            messages = trimmed

        return messages

    def _rebuild_messages_after_checkpoint(self, checkpoint) -> list[AgentMessage]:
        """Replay the uncovered suffix of a valid checkpoint source.

        An empty list is a valid result meaning that the checkpoint covers all
        authoritative history.  An invalid or unresolvable source raises
        ``_InvalidCheckpointSourceError`` so the caller can discard the whole
        replacement instead of accidentally appending a full-history fallback.
        """
        from src.api.models.agui_event import AGUIEventLog
        from src.api.models.conversation_message import ConversationMessage

        source_round_id = checkpoint.source_round_id
        if not source_round_id:
            raise _InvalidCheckpointSourceError("checkpoint has no source_round_id")

        user_rows = (
            self.history_service.db.query(ConversationMessage)
            .filter(
                ConversationMessage.session_id == self.session_id,
                ConversationMessage.round_id == source_round_id,
                ConversationMessage.sequence > checkpoint.source_message_sequence,
                ConversationMessage.role == "user",
            )
            .order_by(ConversationMessage.sequence)
            .all()
        )
        synthetic_contents: list[Any] = []
        source_suffix: list[AgentMessage] = []
        for row in user_rows:
            try:
                content = json.loads(row.content)
            except (TypeError, json.JSONDecodeError):
                content = row.content
            if bool(row.is_synthetic):
                synthetic_contents.append(content)
            else:
                source_suffix.append(AgentMessage(
                    role="user",
                    id=f"{source_round_id}:user:{row.sequence}",
                    run_id=source_round_id,
                    content=content,
                ))

        events = (
            self.history_service.db.query(AGUIEventLog)
            .filter(
                AGUIEventLog.run_id == source_round_id,
                AGUIEventLog.sequence > checkpoint.source_event_sequence,
            )
            .order_by(AGUIEventLog.sequence)
            .all()
        )
        source_suffix.extend(self._events_to_messages(
            events,
            round_id=source_round_id,
            synthetic_user_contents=synthetic_contents,
        ))
        later = self._rebuild_messages_from_events(after_round_id=source_round_id)
        return [*source_suffix, *later]

    def _refresh_runtime_messages_from_history(self) -> None:
        """用 DB 历史替换本进程 Agent runtime messages，保留 system prompt。"""
        if not self.agent:
            return

        restored_messages = self._build_restored_history_messages()
        system_messages = []
        if self.agent.messages and self.agent.messages[0].role == "system":
            system_messages = [self.agent.messages[0]]

        self.agent.messages = system_messages + restored_messages
        self.agent._cached_token_count = 0
        self.agent._cached_message_count = 0
        if hasattr(self.agent, "_active_context_tokens"):
            self.agent._active_context_tokens = self.agent._estimate_messages_tokens(
                self.agent.messages
            )

        logger.info(
            "已刷新 Agent runtime messages: history=%d total=%d (session=%s)",
            len(restored_messages), len(self.agent.messages), self.session_id,
        )
        self._last_saved_index = len(self.agent.messages)

    def _restore_history(self):
        """从 conversation_messages 表恢复对话历史。"""
        self._refresh_runtime_messages_from_history()

    def _rebuild_messages_from_events(self, *, after_round_id: str | None = None) -> list[AgentMessage]:
        """從 agui_events + conversation_messages 重建完整的 LLM messages 數組。

        conversation_messages 提供 user 消息（含多模態內容），
        agui_events 提供 assistant + tool 交互（單一事實源，無數據重複）。

        When ``after_round_id`` is provided it must resolve to an authoritative
        main-chat Round.  A missing cursor raises ``_InvalidCheckpointSourceError``;
        silently returning the full history would make the caller concatenate a
        checkpoint replacement with duplicate authoritative history.

        Returns:
            按時序排列的 AgentMessage 列表
        """
        from src.api.models.agui_event import AGUIEventLog
        from src.api.models.conversation_message import ConversationMessage
        from src.api.models.round import Round
        from src.api.models.subagent_run import SubagentRun

        db = self.history_service.db

        # 1. 獲取本 session 的所有 round（按時間排序）
        rounds = (
            db.query(Round)
            .filter(Round.session_id == self.session_id)
            .order_by(Round.created_at, Round.id)
            .all()
        )
        if not rounds:
            if after_round_id:
                raise _InvalidCheckpointSourceError(
                    f"source round {after_round_id!r} is absent from session history"
                )
            self.history_service.reset_session()
            return []
        subagent_child_round_ids = {
            row[0]
            for row in (
                db.query(SubagentRun.child_run_id)
                .filter(
                    SubagentRun.session_id == self.session_id,
                    SubagentRun.child_run_id.isnot(None),
                )
                .all()
            )
            if row[0]
        }
        if subagent_child_round_ids:
            rounds = [r for r in rounds if r.id not in subagent_child_round_ids]
            if not rounds:
                if after_round_id:
                    raise _InvalidCheckpointSourceError(
                        f"source round {after_round_id!r} is not authoritative main history"
                    )
                self.history_service.reset_session()
                return []

        if after_round_id:
            source_index = next(
                (index for index, round_obj in enumerate(rounds) if round_obj.id == after_round_id),
                None,
            )
            if source_index is None:
                raise _InvalidCheckpointSourceError(
                    f"source round {after_round_id!r} is absent from authoritative main history"
                )
            else:
                rounds = rounds[source_index + 1 :]
                if not rounds:
                    self.history_service.reset_session()
                    return []

        # 2. 預載所有 user + assistant 消息（按 round_id 索引）
        conv_msgs = (
            db.query(ConversationMessage)
            .filter(
                ConversationMessage.session_id == self.session_id,
                ConversationMessage.round_id.in_([round_obj.id for round_obj in rounds]),
                ConversationMessage.role.in_(["user", "assistant"]),
                ConversationMessage.is_summary == False,  # noqa: E712
            )
            .order_by(ConversationMessage.sequence)
            .all()
        )
        user_msgs_by_round: dict[str, list] = {}
        asst_msgs_by_round: dict[str, list] = {}
        for m in conv_msgs:
            if m.role == "user":
                user_msgs_by_round.setdefault(m.round_id, []).append(m)
            else:
                asst_msgs_by_round.setdefault(m.round_id, []).append(m)

        # 3. 批量加載所有 round 的 agui_events（避免 N+1 查詢）
        round_ids = [r.id for r in rounds]
        all_events = (
            db.query(AGUIEventLog)
            .filter(AGUIEventLog.run_id.in_(round_ids))
            .order_by(AGUIEventLog.run_id, AGUIEventLog.sequence)
            .all()
        )
        events_by_round: dict[str, list] = {}
        for evt in all_events:
            events_by_round.setdefault(evt.run_id, []).append(evt)

        # 4. 逐 round 重建
        messages: list[AgentMessage] = []

        def _round_has_synthetic_user_custom(round_events: list) -> bool:
            for evt in round_events:
                try:
                    payload = evt.payload if isinstance(evt.payload, dict) else json.loads(evt.payload)
                except (json.JSONDecodeError, TypeError):
                    continue
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "CUSTOM"
                    and payload.get("name") == "synthetic_user_message"
                ):
                    return True
            return False

        for rnd in rounds:
            round_id = rnd.id if isinstance(getattr(rnd, "id", None), str) else None
            round_events = events_by_round.get(rnd.id, [])
            has_synthetic_user_custom = _round_has_synthetic_user_custom(round_events)

            # 4a. User 消息（從 conversation_messages 取，保留多模態塊）
            user_records = user_msgs_by_round.get(rnd.id, [])
            synthetic_user_contents: list[Any] = []
            has_real_user_record = False
            if user_records:
                for um in user_records:
                    try:
                        content = json.loads(um.content)
                    except (json.JSONDecodeError, TypeError):
                        content = um.content
                    raw_is_synthetic = getattr(um, "is_synthetic", False)
                    is_synthetic = bool(raw_is_synthetic) if isinstance(raw_is_synthetic, (bool, int)) else False
                    if is_synthetic:
                        synthetic_user_contents.append(content)
                        continue
                    has_real_user_record = True
                    messages.append(
                        AgentMessage(
                            role="user",
                            id=f"{round_id}:user" if round_id else None,
                            run_id=round_id,
                            content=content,
                            is_synthetic=is_synthetic,
                        )
                    )
            if not has_real_user_record and rnd.user_message:
                logger.warning(
                    "Round %s 無 conversation_messages user 記錄，fallback 到 rounds.user_message (session=%s)",
                    rnd.id, self.session_id,
                )
                messages.append(AgentMessage(
                    role="user",
                    id=f"{round_id}:user" if round_id else None,
                    run_id=round_id,
                    content=rnd.user_message,
                ))
            elif not has_real_user_record:
                logger.warning(
                    "Round %s 既無 conversation_messages 也無 user_message，跳過整個 round (session=%s)",
                    rnd.id, self.session_id,
                )
                continue

            # 4b. Agent 輸出（從預載的 agui_events 重建 assistant + tool 消息）
            round_messages = self._events_to_messages(
                round_events,
                round_id=rnd.id,
                synthetic_user_contents=synthetic_user_contents if has_synthetic_user_custom else None,
            )

            _has_asst_text = any(
                msg.role == "assistant" and isinstance(msg.content, str) and msg.content
                for msg in round_messages
            )

            if getattr(rnd, "status", None) == "completed" and not _has_asst_text:
                # Level-1 fallback: Round.final_response
                if rnd.final_response:
                    round_messages.append(AgentMessage(role="assistant", content=rnd.final_response))
                    logger.warning(
                        "Round %s 事件重建無 assistant 文本 (%d 事件→%d 消息)，"
                        "fallback-L1 到 rounds.final_response (session=%s)",
                        rnd.id, len(round_events), len(round_messages), self.session_id,
                    )
                else:
                    # Level-2 fallback: conversation_messages 表的 assistant 記錄
                    asst_records = asst_msgs_by_round.get(rnd.id, [])
                    if asst_records:
                        for am in asst_records:
                            round_messages.append(AgentMessage(role="assistant", content=am.content))
                        logger.warning(
                            "Round %s 事件重建無 assistant 文本且無 final_response (%d 事件→%d 消息)，"
                            "fallback-L2 到 conversation_messages (%d 條) (session=%s)",
                            rnd.id, len(round_events), len(round_messages),
                            len(asst_records), self.session_id,
                        )
                    else:
                        logger.warning(
                            "Round %s completed 但無法恢復任何 assistant 內容 "
                            "(events=%d, final_response=None, conv_msgs=0) (session=%s)",
                            rnd.id, len(round_events), self.session_id,
                        )

            messages.extend(round_messages)

        logger.info(
            "歷史重建完成: %d rounds → %d messages (session=%s)",
            len(rounds), len(messages), self.session_id,
        )
        self.history_service.reset_session()
        return messages

    @staticmethod
    def _replace_interrupt_tool_result_in_messages(
        messages: list[Any],
        tool_call_id: str,
        content: str,
    ) -> bool:
        """在一组 AgentMessage 中替换 ask_user tool result 占位。"""
        placeholders = {
            "[Awaiting user response]",
            "[Awaiting tool approval]",
            "[Tool approval execution pending]",
        }
        for msg in messages:
            if (
                getattr(msg, "role", None) == "tool"
                and getattr(msg, "tool_call_id", None) == tool_call_id
                and getattr(msg, "content", None) == content
            ):
                # A previous worker persisted interaction_resolved before it
                # died. The durable tool result is already safe to continue.
                return True
            if (
                getattr(msg, "role", None) == "tool"
                and getattr(msg, "tool_call_id", None) == tool_call_id
                and getattr(msg, "content", None) in placeholders
            ):
                msg.content = content
                return True
        return False

    @staticmethod
    def _events_to_messages(
        events,
        round_id: str | None = None,
        synthetic_user_contents: list[Any] | None = None,
    ) -> list[AgentMessage]:
        """將一個 round 的 agui_events 序列轉換為 LLM messages。

        解析事件流，重建 assistant（含 tool_calls）和 tool result 消息。
        """
        from src.agent.schema import ToolCall, FunctionCall

        messages: list[AgentMessage] = []
        # Per-step 狀態
        step_text = ""
        step_tool_calls: list[ToolCall] = []
        step_tool_results: list[dict] = []
        tc_id_to_name: dict[str, str] = {}
        approval_result_ids: set[str] = set()
        _skipped = 0
        synthetic_content_iter = iter(synthetic_user_contents or [])

        def flush_step_messages() -> None:
            nonlocal step_text, step_tool_calls, step_tool_results
            if step_text or step_tool_calls:
                messages.append(AgentMessage(
                    role="assistant",
                    id=f"{round_id}:assistant:{len(messages) + 1}" if round_id else None,
                    run_id=round_id,
                    content=step_text,
                    tool_calls=step_tool_calls if step_tool_calls else None,
                ))
            for tr in step_tool_results:
                messages.append(AgentMessage(
                    role="tool",
                    id=f"{tr['tool_call_id']}:result",
                    run_id=round_id,
                    content=tr["content"],
                    tool_call_id=tr["tool_call_id"],
                    name=tr["name"],
                ))
            step_text = ""
            step_tool_calls = []
            step_tool_results = []

        for evt in events:
            if isinstance(evt.payload, dict):
                payload = evt.payload
            elif isinstance(evt.payload, str):
                try:
                    payload = json.loads(evt.payload)
                except (json.JSONDecodeError, TypeError):
                    _skipped += 1
                    logger.warning(
                        "事件 payload 解析失敗 (round=%s, seq=%s, preview=%.200s)",
                        round_id, getattr(evt, "sequence", "?"), evt.payload[:200] if evt.payload else "",
                    )
                    continue
            else:
                _skipped += 1
                continue

            evt_type = payload.get("type", "")

            if evt_type == "TEXT_MESSAGE_CONTENT":
                step_text += payload.get("delta", "")

            elif evt_type == "TOOL_CALL_START":
                tc_id = payload.get("toolCallId", "")
                tc_name = payload.get("toolCallName", "")
                tc_id_to_name[tc_id] = tc_name

            elif evt_type == "TOOL_CALL_ARGS":
                # DB 中已是聚合後的完整 args（save_agui_event 做了流式聚合）
                tc_id = payload.get("toolCallId", "")
                raw_args = payload.get("delta", "")
                try:
                    args_dict = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    args_dict = {"_raw": raw_args}
                tc_name = tc_id_to_name.get(tc_id, "")
                step_tool_calls.append(ToolCall(
                    id=tc_id,
                    type="function",
                    function=FunctionCall(name=tc_name, arguments=args_dict),
                ))

            elif evt_type == "TOOL_CALL_RESULT":
                tc_id = payload.get("toolCallId", "")
                if tc_id in approval_result_ids:
                    content = payload.get("content", "")
                    replaced = False
                    for pending_result in reversed(step_tool_results):
                        if pending_result.get("tool_call_id") == tc_id:
                            pending_result["content"] = content
                            replaced = True
                            break
                    if not replaced:
                        for message in reversed(messages):
                            if message.role == "tool" and message.tool_call_id == tc_id:
                                message.content = content
                                replaced = True
                                break
                    approval_result_ids.discard(tc_id)
                    if replaced:
                        continue
                tc_name = tc_id_to_name.get(tc_id, "")
                content = payload.get("content", "")
                step_tool_results.append({
                    "tool_call_id": tc_id,
                    "name": tc_name,
                    "content": content,
                })

            elif evt_type == "STEP_FINISHED":
                # Flush step：生成 assistant + tool messages
                flush_step_messages()

            elif evt_type == "CUSTOM" and payload.get("name") == "tool_approval_resume":
                value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
                tc_id = value.get("toolCallId")
                if isinstance(tc_id, str) and tc_id:
                    approval_result_ids.add(tc_id)

            elif evt_type == "CUSTOM" and payload.get("name") == "interaction_requested":
                value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
                tc_id = value.get("toolCallId")
                if isinstance(tc_id, str) and tc_id:
                    kind = value.get("kind")
                    step_tool_results.append({
                        "tool_call_id": tc_id,
                        "name": tc_id_to_name.get(
                            tc_id,
                            "ask_user" if kind != "tool_approval" else "",
                        ),
                        "content": (
                            "[Awaiting tool approval]"
                            if kind == "tool_approval"
                            else "[Awaiting user response]"
                        ),
                    })

            elif evt_type == "CUSTOM" and payload.get("name") == "interaction_resolved":
                value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
                tc_id = value.get("toolCallId")
                content = value.get("toolResultContent")
                if isinstance(tc_id, str) and tc_id and isinstance(content, str):
                    replaced = False
                    for pending_result in reversed(step_tool_results):
                        if pending_result.get("tool_call_id") == tc_id:
                            pending_result["content"] = content
                            replaced = True
                            break
                    if not replaced:
                        for message in reversed(messages):
                            if message.role == "tool" and message.tool_call_id == tc_id:
                                message.content = content
                                replaced = True
                                break
                    if not replaced:
                        logger.warning(
                            "interaction_resolved 未找到待替换 tool result: round=%s tool_call=%s",
                            round_id,
                            tc_id,
                        )

            elif evt_type == "CUSTOM" and payload.get("name") == "synthetic_user_message":
                content = next(synthetic_content_iter, None) if synthetic_user_contents is not None else None
                if AgentService._has_synthetic_user_content(content):
                    # Synthetic user messages are emitted after the assistant/tool
                    # messages they react to. Preserve that ordering on cold restore.
                    flush_step_messages()
                    messages.append(AgentMessage(role="user", content=content, is_synthetic=True))

        # Flush 殘留（round 異常中斷無 STEP_FINISHED 時）
        flush_step_messages()

        if _skipped:
            logger.warning(
                "事件解析：%d/%d 條事件因 payload 畸形被跳過 (round=%s)",
                _skipped, len(events), round_id,
            )

        return messages

    # =========================================================================
    # 已移除廢棄方法: chat()
    # 請使用 chat_agui() 方法獲取 AG-UI 協議兼容的事件流
    # =========================================================================

    @staticmethod
    def _blocks_to_plain_text(blocks: list[dict[str, Any]]) -> str:
        """將 blocks 轉為可展示文本（用於歷史 user_message）。"""
        text_parts: list[str] = []
        attachment_parts: list[str] = []

        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(str(text))
            elif block_type == "image_url":
                file_obj = block.get("file") or {}
                name = file_obj.get("name") or file_obj.get("path") or "image"
                attachment_parts.append(f"[附件图片:{name}]")
            elif block_type == "file":
                file_obj = block.get("file") or {}
                name = file_obj.get("name") or file_obj.get("path") or "file"
                label = "附件文件夹" if file_obj.get("kind") == "directory" else "附件文件"
                attachment_parts.append(f"[{label}:{name}]")
            elif block_type == "video_url":
                attachment_parts.append("[附件视频]")

        plain_text = "\n".join(part for part in text_parts if part).strip()
        if plain_text:
            return plain_text

        if attachment_parts:
            return "\n".join(attachment_parts)

        return ""

    @staticmethod
    def _extract_user_attachments(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """從內容塊提取可持久化的附件元數據（用於刷新後預覽）。"""
        attachments: list[dict[str, Any]] = []
        for block in blocks:
            block_type = block.get("type")
            if block_type == "file":
                file_obj = block.get("file") or {}
                path = file_obj.get("path")
                if path:
                    attachment = {
                        "path": path,
                        "name": file_obj.get("name") or PathlibPath(path).name,
                        "type": file_obj.get("mime_type") or "",
                        "size": AgentService._parse_file_size(file_obj.get("size")),
                    }
                    if file_obj.get("source") == "workspace":
                        attachment.update({
                            "source": "workspace",
                            "entry_id": file_obj.get("entry_id"),
                            "revision": file_obj.get("revision"),
                            "origin_path": file_obj.get("origin_path"),
                            "snapshot_path": file_obj.get("snapshot_path") or path,
                            "sha256": file_obj.get("sha256"),
                        })
                        if file_obj.get("version_id"):
                            attachment["version_id"] = file_obj.get("version_id")
                        if file_obj.get("version_sequence") is not None:
                            attachment["version_sequence"] = file_obj.get("version_sequence")
                        if file_obj.get("kind") == "directory":
                            attachment.update({
                                "kind": "directory",
                                "is_directory": True,
                                "tree_revision": file_obj.get("tree_revision"),
                                "manifest_sha256": file_obj.get("manifest_sha256"),
                            })
                    attachments.append(attachment)
            elif block_type == "image_url":
                file_obj = block.get("file") or {}
                path = file_obj.get("path")
                if path:
                    attachments.append(
                        {
                            "path": path,
                            "name": file_obj.get("name") or PathlibPath(path).name,
                            "type": file_obj.get("mime_type") or "image/*",
                            "size": AgentService._parse_file_size(file_obj.get("size")),
                        }
                    )
        return attachments

    @staticmethod
    def _parse_file_size(raw_size: Any) -> int | None:
        """安全解析文件大小為 int，無效值返回 None。"""
        if isinstance(raw_size, int):
            return raw_size
        if isinstance(raw_size, str) and raw_size.isdigit():
            return int(raw_size)
        return None

    @staticmethod
    def _normalize_content_blocks(user_content: list[Any]) -> list[dict[str, Any]]:
        """將 Pydantic 內容塊標準化為 dict。"""
        normalized: list[dict[str, Any]] = []
        for block in user_content:
            if hasattr(block, "model_dump"):
                normalized.append(block.model_dump(exclude_none=True))
            elif isinstance(block, dict):
                normalized.append(block)
            else:
                raise ValueError(f"不支持的 content block 类型: {type(block)}")
        return normalized

    async def _materialize_workspace_attachments(
        self,
        blocks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], _WorkspaceAttachmentCapture | None]:
        """Freeze workspace references into the current execution directory."""
        workspace_blocks = [
            block
            for block in blocks
            if block.get("type") == "file"
            and isinstance(block.get("file"), dict)
            and (block.get("file") or {}).get("source") == "workspace"
        ]
        if not workspace_blocks:
            return blocks, None

        from src.api.services.workspace_service import WorkspaceError, WorkspaceService

        db = self._get_db_session_factory()()
        capture = _WorkspaceAttachmentCapture(uuid.uuid4().hex)
        try:
            service = WorkspaceService(db, sandbox=self.sandbox)
            for block in workspace_blocks:
                file_obj = dict(block.get("file") or {})
                entry_id = file_obj.get("entry_id")
                version_id = file_obj.get("version_id")
                if not isinstance(entry_id, str) or not entry_id:
                    raise ValueError("workspace 附件缺少 entry_id")

                stage_kwargs: dict[str, Any] = {
                    "expected_revision": None,
                    "destination_root": self._workspace_dir,
                    "snapshot_id": capture.capture_id,
                }
                if isinstance(version_id, str) and version_id:
                    stage_kwargs["version_id"] = version_id
                try:
                    staged = await service.stage_entry(
                        self.user_id,
                        entry_id,
                        **stage_kwargs,
                    )
                except WorkspaceError as exc:
                    if version_id or exc.code != "REVISION_CONFLICT":
                        raise
                    staged = await service.stage_entry(
                        self.user_id,
                        entry_id,
                        **stage_kwargs,
                    )
                entry = staged.entry
                capture.items.append(_StagedWorkspaceAttachment(
                    entry_id=str(entry.entry_id),
                    version_id=(
                        str(getattr(staged, "version_id", ""))
                        if getattr(staged, "version_id", None) else None
                    ),
                    destination_relative_path=staged.destination_relative_path,
                ))
                file_obj.update({
                    "source": "workspace",
                    "entry_id": entry.entry_id,
                    "revision": str(staged.source_revision),
                    "kind": entry.kind,
                    "origin_path": entry.relative_path,
                    "path": staged.destination_relative_path,
                    "snapshot_path": staged.destination_relative_path,
                    "name": entry.name,
                    "mime_type": entry.mime_type or (
                        "inode/directory" if entry.kind == "directory" else None
                    ),
                    "size": int(staged.size_bytes),
                    "sha256": staged.sha256,
                })
                if getattr(staged, "version_id", None):
                    file_obj["version_id"] = staged.version_id
                if getattr(staged, "version_sequence", None) is not None:
                    file_obj["version_sequence"] = int(staged.version_sequence)
                if entry.kind == "directory":
                    file_obj["tree_revision"] = staged.tree_revision
                    file_obj["manifest_sha256"] = staged.sha256
                block["file"] = file_obj
            return blocks, capture
        except BaseException:
            await self._discard_workspace_attachment_capture(capture)
            raise
        finally:
            db.close()

    async def _discard_workspace_attachment_capture(
        self,
        capture: _WorkspaceAttachmentCapture,
    ) -> None:
        if not capture.items:
            return
        from src.api.models.user_sandbox import UserSandbox
        from src.api.services.sandbox_cleanup_service import enqueue_cleanup, run_cleanup_job
        from src.api.services.workspace_service import WorkspaceService

        cleanup_ids: list[str] = []
        db = self._get_db_session_factory()()
        try:
            service = WorkspaceService(db, sandbox=self.sandbox)
            sandbox_row = db.query(UserSandbox).filter(
                UserSandbox.user_id == self.user_id,
            ).one_or_none()
            for item in capture.items:
                parent_path = posixpath.dirname(item.destination_relative_path)
                service.release_content_references(
                    self.user_id,
                    reference_kind="stage_snapshot",
                    reference_key_prefix=f"{self._workspace_dir}:{parent_path}",
                    commit=False,
                )
                if sandbox_row is not None and sandbox_row.sandbox_id:
                    cleanup_ids.append(enqueue_cleanup(
                        db,
                        user_id=self.user_id,
                        owner_kind="attachment_capture",
                        owner_id=capture.capture_id,
                        sandbox_id=str(sandbox_row.sandbox_id),
                        profile_id=sandbox_row.active_profile_id,
                        profile_version=sandbox_row.active_profile_version,
                        mount_path=get_sandbox_service().get_mount_path(self.user_id),
                        relative_path=(
                            f"sessions/{self.session_id}/.workspace-snapshots/"
                            f"{item.entry_id}/{capture.capture_id}"
                        ),
                    ))
            db.commit()
        finally:
            db.close()
        for cleanup_id in cleanup_ids:
            await run_cleanup_job(cleanup_id)

    def _commit_workspace_attachment_capture(
        self,
        capture: _WorkspaceAttachmentCapture | None,
        *,
        run_id: str,
    ) -> None:
        if capture is None:
            return
        from src.api.services.workspace_service import WorkspaceService

        db = self._get_db_session_factory()()
        try:
            service = WorkspaceService(db, sandbox=self.sandbox)
            for item in capture.items:
                if item.version_id:
                    service.protect_version_reference(
                        self.user_id,
                        item.version_id,
                        reference_kind="round_attachment",
                        reference_key=(
                            f"{self.session_id}:{run_id}:"
                            f"{item.entry_id}:{item.version_id}"
                        ),
                        commit=False,
                        entry_id=item.entry_id,
                    )
                service.release_content_references(
                    self.user_id,
                    reference_kind="stage_snapshot",
                    reference_key_prefix=(
                        f"{self._workspace_dir}:"
                        f"{posixpath.dirname(item.destination_relative_path)}"
                    ),
                    commit=False,
                )
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _sandbox_command_stdout(execution: Any) -> str:
        logs = getattr(execution, "logs", None)
        lines = getattr(logs, "stdout", None) if logs is not None else None
        if not lines:
            return ""
        if isinstance(lines, str):
            return lines
        return "\n".join(
            str(getattr(line, "text", line))
            for line in lines
        )

    async def _capture_session_assistant_file(
        self,
        reference: dict[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Freeze one mutable Session file before its structured event commits."""

        relative_path = posixpath.normpath(str(reference.get("path") or "").replace("\\", "/"))
        revision = str(reference.get("revision") or "")
        if (
            not relative_path
            or relative_path in {".", ".."}
            or posixpath.isabs(relative_path)
            or relative_path.startswith("../")
            or any(segment.startswith(".") for segment in relative_path.split("/"))
        ):
            return None
        revision_parts = revision.split(":")
        if len(revision_parts) != 3 or revision_parts[0] != "v1":
            return None
        try:
            expected_size = int(revision_parts[1])
            expected_mtime_ns = int(revision_parts[2])
        except ValueError:
            return None
        if expected_size < 0 or expected_mtime_ns < 0:
            return None

        name = posixpath.basename(relative_path)
        capture_token = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{self.session_id}:{run_id}:{relative_path}:{revision}",
        ).hex
        snapshot_path = f".assistant-artifacts/{run_id}/{capture_token}/{name}"
        source_path = posixpath.join(self._workspace_dir, relative_path)
        destination_path = posixpath.join(self._workspace_dir, snapshot_path)
        script = (
            "import hashlib,json,os,stat,sys,uuid\n"
            f"source={source_path!r}\n"
            f"destination={destination_path!r}\n"
            f"expected_size={expected_size!r}\n"
            f"expected_mtime_ns={expected_mtime_ns!r}\n"
            "try:\n"
            " source_stat=os.lstat(source)\n"
            "except OSError:\n"
            " sys.exit(2)\n"
            "if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):\n"
            " sys.exit(3)\n"
            "if int(source_stat.st_size)!=expected_size or int(source_stat.st_mtime_ns)!=expected_mtime_ns:\n"
            " sys.exit(4)\n"
            "os.makedirs(os.path.dirname(destination),mode=0o700,exist_ok=True)\n"
            "source_flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)\n"
            "source_fd=os.open(source,source_flags)\n"
            "digest=hashlib.sha256()\n"
            "temp=destination+'.'+uuid.uuid4().hex+'.tmp'\n"
            "try:\n"
            " current=os.fstat(source_fd)\n"
            " if int(current.st_size)!=expected_size or int(current.st_mtime_ns)!=expected_mtime_ns:\n"
            "  sys.exit(4)\n"
            " src=os.fdopen(source_fd,'rb',closefd=True)\n"
            " source_fd=-1\n"
            " with src,open(temp,'xb') as dst:\n"
            "  while True:\n"
            "   chunk=src.read(1024*1024)\n"
            "   if not chunk:\n"
            "    break\n"
            "   digest.update(chunk)\n"
            "   dst.write(chunk)\n"
            "  dst.flush()\n"
            "  os.fsync(dst.fileno())\n"
            " if os.path.exists(destination):\n"
            "  existing=hashlib.sha256()\n"
            "  with open(destination,'rb') as handle:\n"
            "   for chunk in iter(lambda:handle.read(1024*1024),b''):\n"
            "    existing.update(chunk)\n"
            "  if existing.hexdigest()!=digest.hexdigest():\n"
            "   sys.exit(5)\n"
            "  os.unlink(temp)\n"
            " else:\n"
            "  os.replace(temp,destination)\n"
            " os.chmod(destination,0o400)\n"
            " print(json.dumps({'sha256':digest.hexdigest(),'size':expected_size},separators=(',',':')))\n"
            "finally:\n"
            " if source_fd>=0:\n"
            "  os.close(source_fd)\n"
            " try:\n"
            "  if os.path.exists(temp): os.unlink(temp)\n"
            " except OSError:\n"
            "  pass\n"
        )
        execution = await self.sandbox.commands.run(
            "python3 -c " + shlex.quote(script)
        )
        if getattr(execution, "error", None):
            return None
        stdout = self._sandbox_command_stdout(execution).strip()
        if not stdout:
            return None
        try:
            captured = json.loads(stdout.splitlines()[-1])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(captured, dict) or captured.get("size") != expected_size:
            return None
        return {
            "ref_id": f"session:{self.session_id}:{run_id}:{capture_token}",
            "source": "session",
            "session_id": self.session_id,
            "name": name,
            "path": relative_path,
            "snapshot_path": snapshot_path,
            "size": expected_size,
            "modified": str(reference.get("modified") or ""),
            "type": str(reference.get("type") or ""),
            "revision": revision,
            "sha256": str(captured.get("sha256") or ""),
            "operation": str(reference.get("operation") or "UPDATED"),
            "toolCallId": reference.get("toolCallId"),
        }

    def _protect_workspace_assistant_file(
        self,
        reference: dict[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Validate and pin one immutable Workspace version for Round history."""

        entry_id = str(reference.get("entry_id") or "")
        version_id = str(
            reference.get("version_id")
            or reference.get("current_version_id")
            or ""
        )
        path = str(reference.get("workspace_path") or reference.get("path") or "")
        name = str(reference.get("name") or posixpath.basename(path))
        operation = str(reference.get("operation") or "").upper()
        if (
            not entry_id
            or not version_id
            or not path
            or not name
            or (
                reference.get("kind") != "file"
                and not (reference.get("source") == "workspace" and reference.get("kind") is None)
            )
            or str(reference.get("status") or "active") != "active"
            or operation in {"NO_CHANGE", "DELETED"}
        ):
            return None
        from src.api.services.workspace_service import WorkspaceService

        WorkspaceService(self.history_service.db, sandbox=self.sandbox).protect_version_reference(
            self.user_id,
            version_id,
            reference_kind="round_attachment",
            reference_key=(
                f"{self.session_id}:{run_id}:assistant:{entry_id}:{version_id}"
            ),
            commit=False,
            entry_id=entry_id,
        )
        return {
            "ref_id": f"workspace:{entry_id}:{version_id}",
            "source": "workspace",
            "entry_id": entry_id,
            "name": name,
            "path": path,
            "workspace_path": path,
            "size": int(reference.get("size") or reference.get("size_bytes") or 0),
            "modified": str(reference.get("modified") or ""),
            "type": str(
                reference.get("type")
                or (name.rsplit(".", 1)[-1].lower() if "." in name else "")
            ),
            "revision": str(reference.get("revision") or ""),
            "version_id": version_id,
            "operation": operation or "UPDATED",
            "toolCallId": reference.get("toolCallId") or reference.get("tool_call_id"),
        }

    async def _materialize_assistant_file_reference(
        self,
        value: Any,
        *,
        run_id: str,
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        if value.get("source") == "session":
            if str(value.get("operation") or "").upper() == "DELETED":
                path = str(value.get("path") or "")
                name = str(value.get("name") or posixpath.basename(path))
                revision = str(value.get("revision") or "")
                if not path or not name or not revision:
                    return None
                tombstone_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{self.session_id}:{run_id}:deleted:{path}",
                ).hex
                return {
                    "ref_id": f"session:{self.session_id}:{run_id}:deleted:{tombstone_id}",
                    "source": "session",
                    "session_id": self.session_id,
                    "name": name,
                    "path": path,
                    "size": int(value.get("size") or 0),
                    "modified": str(value.get("modified") or ""),
                    "type": str(value.get("type") or ""),
                    "revision": revision,
                    "operation": "DELETED",
                    "toolCallId": value.get("toolCallId"),
                }
            existing_snapshot = str(value.get("snapshot_path") or "")
            if existing_snapshot:
                if (
                    value.get("session_id") != self.session_id
                    or not existing_snapshot.startswith(".assistant-artifacts/")
                    or not value.get("ref_id")
                    or not value.get("revision")
                ):
                    return None
                # Child-agent references already point at a platform-created,
                # read-only capture in this same Session. Preserve that exact
                # identity instead of re-reading the mutable source path.
                return {
                    "ref_id": str(value["ref_id"]),
                    "source": "session",
                    "session_id": self.session_id,
                    "name": str(value.get("name") or ""),
                    "path": str(value.get("path") or ""),
                    "snapshot_path": existing_snapshot,
                    "size": int(value.get("size") or 0),
                    "modified": str(value.get("modified") or ""),
                    "type": str(value.get("type") or ""),
                    "revision": str(value["revision"]),
                    "sha256": str(value.get("sha256") or ""),
                    "operation": str(value.get("operation") or "UPDATED"),
                    "toolCallId": value.get("toolCallId"),
                }
            return await self._capture_session_assistant_file(value, run_id=run_id)
        if value.get("source") == "workspace":
            return self._protect_workspace_assistant_file(value, run_id=run_id)
        return None

    def _validate_multimodal_blocks(self, blocks: list[dict[str, Any]]) -> None:
        """依照模型能力校驗多模態輸入。"""
        registry = get_model_registry()
        model_config = registry.get_or_raise(self.model_id) if self.model_id else registry.get_default()

        image_count = sum(1 for b in blocks if b.get("type") == "image_url")
        video_count = sum(1 for b in blocks if b.get("type") == "video_url")

        if image_count > 0 and not model_config.supports_image:
            raise ValueError(f"模型 '{model_config.id}' 不支持图片输入")
        if image_count > model_config.max_images:
            raise ValueError(
                f"模型 '{model_config.id}' 最多支持 {model_config.max_images} 张图片，当前 {image_count} 张"
            )

        if video_count > 0 and not model_config.supports_video:
            raise ValueError(f"模型 '{model_config.id}' 不支持视频输入")
        if video_count > model_config.max_videos:
            raise ValueError(
                f"模型 '{model_config.id}' 最多支持 {model_config.max_videos} 个视频，当前 {video_count} 个"
            )

        # --- 圖片大小守衛 ---
        MAX_SINGLE_IMAGE_MB = 20   # 單張圖片 Data URL 上限（MB）
        MAX_TOTAL_IMAGES_MB = 50   # 所有圖片 Data URL 總量上限（MB）
        total_image_bytes = 0
        for b in blocks:
            if b.get("type") == "image_url":
                url = (b.get("image_url") or {}).get("url", "")
                url_bytes = len(url) if url else 0  # base64 全是 ASCII，1 char = 1 byte
                if url_bytes > MAX_SINGLE_IMAGE_MB * 1024 * 1024:
                    size_mb = url_bytes / (1024 * 1024)
                    raise ValueError(
                        f"单张图片 Data URL 过大（{size_mb:.1f}MB），上限 {MAX_SINGLE_IMAGE_MB}MB。"
                        f"请压缩图片后重试。"
                    )
                total_image_bytes += url_bytes
        if total_image_bytes > MAX_TOTAL_IMAGES_MB * 1024 * 1024:
            total_mb = total_image_bytes / (1024 * 1024)
            raise ValueError(
                f"所有图片 Data URL 总计过大（{total_mb:.1f}MB），上限 {MAX_TOTAL_IMAGES_MB}MB。"
                f"请减少图片数量或压缩后重试。"
            )

    def _build_agent_user_content(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """構建發往 LLM 的用戶內容，將 file block 映射為 text block。"""
        agent_blocks: list[dict[str, Any]] = []
        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                agent_blocks.append(block)
            elif block_type == "image_url":
                image_url = block.get("image_url") or {}
                url = image_url.get("url", "")
                if not url:
                    raise ValueError("image_url.url 不能为空")
                agent_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": url},
                    }
                )
            elif block_type == "video_url":
                video_url = block.get("video_url") or {}
                url = video_url.get("url", "")
                if not url:
                    raise ValueError("video_url.url 不能为空")
                agent_blocks.append(
                    {
                        "type": "video_url",
                        "video_url": {"url": url},
                    }
                )
            elif block_type == "file":
                file_obj = block.get("file") or {}
                file_path = file_obj.get("path")
                if not file_path:
                    raise ValueError("file.path 不能为空")
                file_name = file_obj.get("name") or file_path
                metadata_payload = {
                    "name": str(file_name),
                    "path": str(file_path),
                }
                is_directory = file_obj.get("kind") == "directory"
                if is_directory:
                    metadata_payload["kind"] = "directory"
                if file_obj.get("source") == "workspace":
                    metadata_payload.update({
                        "source": "workspace",
                        "workspace_entry_id": str(file_obj.get("entry_id") or ""),
                        "workspace_path": str(file_obj.get("origin_path") or ""),
                        "workspace_revision": str(file_obj.get("revision") or ""),
                        "workspace_version_id": str(file_obj.get("version_id") or ""),
                        "workspace_version_sequence": file_obj.get("version_sequence"),
                    })
                metadata = json.dumps(
                    metadata_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                agent_blocks.append(
                    {
                        "type": "text",
                        "text": f"[{'附件文件夹' if is_directory else '附件文件'}] metadata={metadata}",
                    }
                )
            else:
                raise ValueError(f"未知 content block 类型: {block_type}")

        return agent_blocks

    def _save_conversation_message(
        self,
        role: str,
        content: Any,
        round_id: str | None = None,
        token_count: int | None = None,
        is_synthetic: bool = False,
        is_summary: bool = False,
        raise_on_error: bool = False,
        commit: bool = True,
    ) -> bool:
        """向 conversation_messages 表持久化一條消息。

        用於 Agent 上下文恢復，與 agui_events 互相獨立。

        使用原子 INSERT…SELECT 在單條 SQL 語句內完成
        MAX(sequence) 讀取 + 行寫入。同一 session 的寫入由 UserRunLock
        （uq_user_run_lock_user_session）串行化，單 worker 下不存在並發寫者；
        `uq_convmsg_session_seq` 唯一約束作為最終兜底。
        """
        from sqlalchemy import text

        db = self.history_service.db
        content_str = (
            json.dumps(content, ensure_ascii=False)
            if not isinstance(content, str)
            else content
        )

        try:
            db.execute(
                text(
                    """
                    INSERT INTO conversation_messages
                        (session_id, round_id, sequence, role,
                         content, token_count, is_summary,
                         is_synthetic, created_at)
                    SELECT
                        :session_id, :round_id,
                        COALESCE(MAX(sequence), 0) + 1,
                        :role, :content, :token_count,
                        :is_summary,
                        :is_synthetic, :created_at
                    FROM conversation_messages
                    WHERE session_id = :session_id
                    """
                ),
                {
                    "session_id": self.session_id,
                    "round_id": round_id,
                    "role": role,
                    "content": content_str,
                    "token_count": token_count,
                    "is_summary": is_summary,
                    "is_synthetic": is_synthetic,
                    "created_at": now_naive(),
                },
            )
            if commit:
                db.commit()
            else:
                db.flush()
            return True
        except Exception as e:
            db.rollback()
            logger.warning("保存 conversation_message 失敗: %s", e)
            if raise_on_error:
                raise
            return False

    async def chat_agui(
        self,
        user_content: list[Any],
        idempotency_key: str | None = None,
    ) -> AsyncIterator[AGUIEvent]:
        """執行對話並輸出 AG-UI 事件流
        
        這是新的主要 API 方法，直接透傳 Agent 的 AG-UI 事件流。
        
        Args:
            user_content: 用戶內容塊列表（V2 block-only）
            
        Yields:
            AGUIEvent: AG-UI 協議事件
            
        Example:
            async for event in agent_service.chat_agui(message):
                yield f"event: {event.type.value}\\ndata: {event.model_dump_json()}\\n\\n"
        """
        prepared = await self.prepare_chat_round(
            user_content=user_content,
            idempotency_key=idempotency_key,
        )

        async for event in self.run_prepared_round(
            prepared,
            error_label="Agent執行失敗",
        ):
            yield event

    async def prepare_chat_round(
        self,
        *,
        user_content: list[Any],
        idempotency_key: str | None = None,
        contexts: list[Context] | None = None,
    ) -> PreparedAgentRun:
        """Create the user round and update Agent memory before execution."""
        if not self.agent:
            raise RuntimeError("Agent not initialized")

        # Admit the key before any side effect: workspace staging below copies
        # files and allocates uuid-suffixed directory snapshots that a retry
        # would orphan, and a moved-on source revision would fail CAS instead
        # of reporting the already-created Round.
        if idempotency_key:
            admitted = self.history_service.find_round_by_idempotency_key(
                self.session_id, idempotency_key
            )
            if admitted is not None:
                logger.warning(
                    "幂等預檢：已存在 Round %s (status=%s)，跳過附件落盤與重複執行 (key=%s)",
                    admitted.id, admitted.status, idempotency_key,
                )
                raise DuplicateRoundError(admitted.id)

        persisted_interrupt = self._load_persisted_interrupt(None)
        if persisted_interrupt is not None:
            raise InteractionConflictError(
                "当前 Round 正在等待用户回答，请先处理待办问题或取消该 Round"
            )
        if self.agent.has_pending_interrupt():
            self.discard_pending_runtime_state()

        requested_context = parse_requested_turn_preferences_contexts(contexts or [])
        requested_reasoning = parse_requested_reasoning_contexts(contexts or [])
        pending_file_drafts = parse_pending_file_draft_contexts(contexts or [])
        run_context_options = (
            {"pending_file_drafts": pending_file_drafts}
            if pending_file_drafts else {}
        )
        run_context = await self._resolve_run_context(
            requested_context,
            requested_reasoning,
            **run_context_options,
        )

        # 正規化 + 校驗 + 構建輸入內容
        normalized_blocks = self._normalize_content_blocks(user_content)
        if not normalized_blocks:
            raise ValueError("消息 content 不能为空")

        self._validate_multimodal_blocks(normalized_blocks)
        normalized_blocks, attachment_capture = await self._materialize_workspace_attachments(
            normalized_blocks
        )
        try:
            agent_content = self._build_agent_user_content(normalized_blocks)
            user_message_for_history = self._blocks_to_plain_text(normalized_blocks)
            user_attachments = self._extract_user_attachments(normalized_blocks)
            self._refresh_runtime_messages_from_history()
            run_id = str(uuid.uuid4())
            created_round = self.history_service.create_round(
                session_id=self.session_id,
                round_id=run_id,
                user_message=user_message_for_history,
                user_attachments=user_attachments,
                preferred_skills=[
                    {"key": skill.key, "display_name": skill.display_name}
                    for skill in (
                        run_context.preferences.skills
                        if run_context.preferences is not None else ()
                    )
                ],
                preferred_mcp_connections=[
                    {
                        "server_id": connection.server_id,
                        "display_name": connection.display_name,
                    }
                    for connection in (
                        run_context.preferences.mcp_connections
                        if run_context.preferences is not None else ()
                    )
                ],
                thinking_mode=(
                    run_context.reasoning.mode if run_context.reasoning else None
                ),
                reasoning_effort=(
                    run_context.reasoning.effort if run_context.reasoning else None
                ),
                idempotency_key=idempotency_key,
            )
        except BaseException:
            if attachment_capture is not None:
                await self._discard_workspace_attachment_capture(attachment_capture)
            raise

        # 幂等衝突：另一個 Worker 已搶先創建了相同 idempotency_key 的 Round
        if idempotency_key and created_round.id != run_id:
            logger.warning(
                "幂等衝突：已存在 Round %s (status=%s)，跳過重複執行 (key=%s)",
                created_round.id, created_round.status, idempotency_key,
            )
            if attachment_capture is not None:
                await self._discard_workspace_attachment_capture(attachment_capture)
            raise DuplicateRoundError(created_round.id)

        self._commit_workspace_attachment_capture(
            attachment_capture,
            run_id=run_id,
        )
        
        # 添加到 agent
        user_message_id = f"{run_id}:user"
        add_user_parameters = inspect.signature(self.agent.add_user_message).parameters
        if "message_id" in add_user_parameters:
            self.agent.add_user_message(
                agent_content,
                message_id=user_message_id,
                run_id=run_id,
            )
        else:
            # Compatibility for small Agent doubles and third-party wrappers.
            self.agent.add_user_message(agent_content)
            last_message = (getattr(self.agent, "messages", None) or [None])[-1]
            if isinstance(last_message, AgentMessage):
                last_message.id = user_message_id
                last_message.run_id = run_id
        # 持久化用戶消息到 conversation_messages
        self._save_conversation_message("user", agent_content, round_id=run_id)

        return PreparedAgentRun(
            run_id=run_id,
            user_message=user_message_for_history,
            user_message_id=user_message_id,
            context=run_context,
            requested_context=requested_context,
        )

    async def run_prepared_round(
        self,
        prepared: PreparedAgentRun,
        *,
        error_label: str = "执行失败",
    ) -> AsyncIterator[AGUIEvent]:
        """Execute an already-created round."""
        preferred_skills = (
            [
                {
                    "key": skill.key,
                    "display_name": skill.display_name,
                }
                for skill in prepared.context.preferences.skills
            ]
            if not prepared.is_continuation
            and prepared.context.preferences is not None
            else []
        )
        preferred_mcp_connections = (
            [
                {
                    "server_id": connection.server_id,
                    "display_name": connection.display_name,
                }
                for connection in prepared.context.preferences.mcp_connections
            ]
            if not prepared.is_continuation
            and prepared.context.preferences is not None
            else []
        )
        async for event in self._run_round_stream(
            run_id=prepared.run_id,
            user_message=prepared.user_message,
            user_message_id=prepared.user_message_id,
            run_context=prepared.context,
            requested_context=prepared.requested_context,
            round_preferred_skills=preferred_skills,
            round_preferred_mcp_connections=preferred_mcp_connections,
            is_continuation=prepared.is_continuation,
            initial_step=prepared.initial_step,
            interaction_id=prepared.interaction_id,
            interaction_tool_call_id=prepared.interaction_tool_call_id,
            interaction_tool_result_content=prepared.interaction_tool_result_content,
            interaction_kind=prepared.interaction_kind,
            tool_approval_resolution=prepared.tool_approval_resolution,
            error_label=error_label,
        ):
            yield event

    async def _resolve_run_context(
        self,
        requested: RequestedTurnPreferencesContext | None,
        requested_reasoning: RequestedReasoningContext | None = None,
        pending_file_drafts: tuple[PendingFileDraftRef, ...] = (),
    ) -> AgentRunContext:
        """Resolve user-provided keys against the effective registry for this run."""
        resolved_skills: list[ResolvedSkillRef] = []
        if (
            requested is not None
            and requested.skill_keys
            and self.skill_loader is not None
        ):
            try:
                refresh_inventory = getattr(self.skill_loader, "refresh_inventory", None)
                if callable(refresh_inventory):
                    await refresh_inventory()
            except Exception:
                logger.warning("Preferred Skill Inventory 刷新失败，沿用当前 Registry", exc_info=True)
            try:
                self.skill_loader.refresh_disabled_skills(force=True)
            except Exception:
                logger.warning("Preferred Skill 状态刷新失败，按当前 Registry 解析", exc_info=True)
            for key in requested.skill_keys:
                skill = self.skill_loader.get_skill(key)
                if skill is None:
                    logger.info("忽略当前 Run 不可用的 preferred Skill: %s", key)
                    continue
                metadata = skill.metadata if isinstance(skill.metadata, dict) else {}
                display_name = str(metadata.get("display_name") or skill.name)
                resolved_skills.append(ResolvedSkillRef(
                    key=skill.name,
                    load_name=skill.name,
                    display_name=display_name,
                ))
        available_connections: dict[str, object] = {}
        for connection in getattr(self, "mcp_connections", ()):
            server_id = (
                connection.get("server_id")
                if isinstance(connection, dict)
                else getattr(connection, "server_id", None)
            )
            if isinstance(server_id, str) and server_id:
                available_connections[server_id] = connection
        resolved_mcp_connections: list[ResolvedMcpConnectionRef] = []
        for server_id in requested.mcp_server_ids if requested is not None else ():
            connection = available_connections.get(server_id)
            if connection is None:
                logger.info(
                    "忽略当前 Run 不可用的 preferred MCP connection: %r",
                    server_id,
                )
                continue
            display_name = (
                connection.get("server_name")
                if isinstance(connection, dict)
                else getattr(connection, "server_name", None)
            )
            normalized_display_name = " ".join(str(display_name or "").split())
            if not normalized_display_name:
                continue
            resolved_mcp_connections.append(
                ResolvedMcpConnectionRef(
                    server_id=server_id,
                    display_name=normalized_display_name,
                )
            )
        preferences = (
            ResolvedTurnPreferencesContext(
                skills=tuple(resolved_skills),
                mcp_connections=tuple(resolved_mcp_connections),
            )
            if resolved_skills or resolved_mcp_connections
            else None
        )
        reasoning = self._resolve_reasoning_context(requested_reasoning)
        return AgentRunContext(
            preferences=preferences,
            reasoning=reasoning,
            pending_file_drafts=pending_file_drafts,
        )

    def _resolve_reasoning_context(
        self,
        requested: RequestedReasoningContext | None,
    ) -> ResolvedReasoningContext:
        config = self._model_config
        if config is None:
            raise ValueError("当前模型不支持按轮设置推理等级")
        if requested is None:
            return ResolvedReasoningContext(
                mode=config.effective_thinking_mode,
                effort=config.reasoning_effort,
            )
        return resolve_reasoning_selection(
            requested,
            provider=config.provider,
            supports_reasoning_control=config.supports_reasoning_control,
            supported_reasoning_efforts=config.supported_reasoning_efforts,
        )

    def _reasoning_context_from_round(
        self,
        round_id: str,
    ) -> ResolvedReasoningContext | None:
        """Carry the frozen parent selection through interrupt continuations."""
        from src.api.models.round import Round

        db = self.history_service.db
        parent = (
            db.query(Round)
            .filter(Round.id == round_id, Round.session_id == self.session_id)
            .first()
        )
        if parent is None:
            raise ValueError(f"父 Round '{round_id}' 不存在或不属于当前会话")
        mode = parent.thinking_mode
        if mode is None:
            return None
        if mode not in {"provider_default", "enabled", "disabled"}:
            raise ValueError(f"父 Round '{round_id}' 的思考模式无效: {mode!r}")
        effort = parent.reasoning_effort
        if effort is not None and not isinstance(effort, str):
            raise ValueError(f"父 Round '{round_id}' 的推理等级无效")
        return ResolvedReasoningContext(
            mode=mode,
            effort=effort,
        )

    @staticmethod
    def _on_post_round_done(task: asyncio.Task) -> None:
        """后台任务完成回调：记录未被 await 的异常"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("后台 _post_round_tasks 异常: %s", exc, exc_info=exc)

    def _load_persisted_interrupt(self, interrupt_id: str | None) -> dict[str, Any] | None:
        """从数据库查找同 Round Interaction。

        该方法用于 Agent 内存状态丢失（例如 AgentPool TTL 回收）后的冷恢复。
        """
        from src.api.models.tool_permission import ToolApprovalRequest
        from src.api.services.tool_permission_service import (
            APPROVAL_CONTINUATION_RESUMABLE_STATUSES,
        )

        db = self.history_service.db
        try:
            interaction = AgentInteractionService.load_pending(
                db,
                session_id=self.session_id,
                interaction_id=interrupt_id,
            )
            if interaction is not None:
                request = AgentInteractionService.request_payload(interaction)
                nested_payload = (
                    request.get("payload")
                    if isinstance(request.get("payload"), dict)
                    else {}
                )
                interaction_kind = (
                    "tool_approval"
                    if interaction.kind == "tool_approval"
                    else "ask_user"
                )
                if interaction_kind == "tool_approval":
                    approval = (
                        db.query(ToolApprovalRequest)
                        .filter(
                            ToolApprovalRequest.id == interaction.id,
                            ToolApprovalRequest.user_id == self.user_id,
                            ToolApprovalRequest.session_id == self.session_id,
                            ToolApprovalRequest.status.in_(
                                APPROVAL_CONTINUATION_RESUMABLE_STATUSES
                            ),
                        )
                        .first()
                    )
                    if approval is None:
                        return None
                result = {
                    "interrupt_id": interaction.id,
                    "round_id": interaction.round_id,
                    "tool_call_id": interaction.tool_call_id,
                    "questions": (
                        nested_payload.get("questions")
                        if isinstance(nested_payload.get("questions"), list)
                        else []
                    ),
                    "kind": interaction_kind,
                    "payload": nested_payload,
                }
                if interaction_kind == "tool_approval":
                    result["reason"] = "human_approval"
                if isinstance(request.get("runtime_context"), dict):
                    result["runtime_context"] = request["runtime_context"]
                origin_user_message_id = request.get(
                    TURN_PREFERENCES_ORIGIN_USER_MESSAGE_ID_KEY
                )
                if isinstance(origin_user_message_id, str):
                    result[TURN_PREFERENCES_ORIGIN_USER_MESSAGE_ID_KEY] = (
                        origin_user_message_id
                    )
                return result
            return None
        finally:
            db.rollback()

    @staticmethod
    def _requested_context_from_interrupt(
        snapshot: dict[str, Any] | None,
    ) -> RequestedTurnPreferencesContext | None:
        raw = (snapshot or {}).get("runtime_context")
        if not isinstance(raw, dict):
            return None
        try:
            wire = Context.model_validate(raw)
        except Exception:
            logger.warning("忽略中断元数据中的非法 Runtime Context")
            return None
        return parse_requested_turn_preferences_contexts([wire])

    @staticmethod
    def _turn_preferences_origin_user_message_id_from_interrupt(
        snapshot: dict[str, Any] | None,
        *,
        parent_run_id: str,
    ) -> str:
        """Read the server-owned turn-preference anchor with a deterministic default."""
        raw = (snapshot or {}).get(TURN_PREFERENCES_ORIGIN_USER_MESSAGE_ID_KEY)
        if isinstance(raw, str):
            candidate = raw.strip()
            if candidate and candidate != ":user" and candidate.endswith(":user"):
                return candidate
        return f"{parent_run_id}:user"

    @staticmethod
    def _attach_turn_preferences_interaction_context(
        interaction_payload: dict[str, Any],
        pending_interrupt: dict[str, Any] | None,
        *,
        requested_context: RequestedTurnPreferencesContext | None,
        origin_user_message_id: str,
    ) -> None:
        """Persist requested keys and their original user-message anchor."""
        # The anchor is server-owned. Drop any value serialized by an interaction
        # producer before assigning the trusted value derived from PreparedAgentRun.
        interaction_payload.pop(TURN_PREFERENCES_ORIGIN_USER_MESSAGE_ID_KEY, None)
        interaction_payload.pop("runtime_context", None)
        if isinstance(pending_interrupt, dict):
            pending_interrupt.pop(TURN_PREFERENCES_ORIGIN_USER_MESSAGE_ID_KEY, None)
            pending_interrupt.pop("runtime_context", None)
        if requested_context is None:
            return

        runtime_wire = requested_turn_preferences_to_context(
            requested_context
        ).model_dump()
        interaction_payload["runtime_context"] = runtime_wire
        if isinstance(pending_interrupt, dict):
            pending_interrupt["runtime_context"] = runtime_wire

        trusted_anchor = origin_user_message_id.strip()
        if (
            not trusted_anchor
            or trusted_anchor == ":user"
            or not trusted_anchor.endswith(":user")
        ):
            logger.warning("Turn preference 原始用户消息锚点无效，未写入中断元数据")
            return
        interaction_payload[TURN_PREFERENCES_ORIGIN_USER_MESSAGE_ID_KEY] = trusted_anchor
        if isinstance(pending_interrupt, dict):
            pending_interrupt[TURN_PREFERENCES_ORIGIN_USER_MESSAGE_ID_KEY] = trusted_anchor

    def discard_pending_runtime_state(
        self,
        *,
        interrupt_id: str | None = None,
        owner_round_id: str | None = None,
    ) -> None:
        """清除已被数据库终态否决的 Agent 热缓存和本地 parent 映射。"""
        if self.agent:
            discard = getattr(type(self.agent), "discard_pending_runtime_state", None)
            if callable(discard):
                self.agent.discard_pending_runtime_state(
                    interrupt_id=interrupt_id,
                    owner_round_id=owner_round_id,
                )
            else:
                pending = getattr(self.agent, "_pending_interrupt", None)
                if isinstance(pending, dict):
                    matches_interrupt = (
                        interrupt_id is None
                        or pending.get("interrupt_id") == interrupt_id
                    )
                    pending_round_id = pending.get("round_id")
                    matches_round = (
                        owner_round_id is None
                        or pending_round_id is None
                        or pending_round_id == owner_round_id
                    )
                    if matches_interrupt and matches_round:
                        self.agent._pending_interrupt = None

                approved = getattr(self.agent, "_pending_approved_tool", None)
                if approved is not None:
                    matches_interrupt = (
                        interrupt_id is None
                        or getattr(approved, "request_id", None) == interrupt_id
                    )
                    approved_round_id = getattr(approved, "owner_round_id", None)
                    matches_round = (
                        owner_round_id is None
                        or approved_round_id is None
                        or approved_round_id == owner_round_id
                    )
                    if matches_interrupt and matches_round:
                        self.agent._pending_approved_tool = None

        pending_round_ids = getattr(self, "_pending_interrupt_round_ids", None)
        if not isinstance(pending_round_ids, dict):
            return
        if interrupt_id is not None:
            pending_round_ids.pop(interrupt_id, None)
        if owner_round_id is not None:
            stale_ids = [
                pending_id
                for pending_id, round_id in pending_round_ids.items()
                if round_id == owner_round_id
            ]
            for pending_id in stale_ids:
                pending_round_ids.pop(pending_id, None)

    def has_pending_interrupt(self, interrupt_id: str) -> bool:
        """检查匹配的待处理中断；同 Round 缓存必须由数据库状态背书。"""
        if self._load_persisted_interrupt(interrupt_id) is not None:
            return True
        snapshot = self._get_agent_pending_interrupt_snapshot(interrupt_id)
        if snapshot is not None:
            self.discard_pending_runtime_state(
                interrupt_id=interrupt_id,
                owner_round_id=snapshot.get("round_id"),
            )
        return False

    def _get_agent_pending_interrupt_snapshot(self, interrupt_id: str) -> dict[str, Any] | None:
        """尽量从 Agent 内存态读取 pending interrupt 快照。"""
        if not self.agent:
            return None

        getter = getattr(type(self.agent), "get_pending_interrupt", None)
        if callable(getter):
            snapshot = self.agent.get_pending_interrupt()
            if isinstance(snapshot, dict) and snapshot.get("interrupt_id") == interrupt_id:
                return snapshot

        pending = getattr(self.agent, "_pending_interrupt", None)
        if isinstance(pending, dict) and pending.get("interrupt_id") == interrupt_id:
            return dict(pending)
        return None

    def _attach_agent_pending_interrupt_round_id(self, interrupt_id: str, round_id: str) -> None:
        """将 pending interrupt 和触发它的 round 绑定到同一个内存快照。"""
        if not self.agent:
            return

        setter = getattr(type(self.agent), "set_pending_interrupt_round_id", None)
        if callable(setter) and self.agent.set_pending_interrupt_round_id(interrupt_id, round_id):
            return

        pending = getattr(self.agent, "_pending_interrupt", None)
        if isinstance(pending, dict) and pending.get("interrupt_id") == interrupt_id:
            pending["round_id"] = round_id

    def _replace_agent_interrupt_tool_result(self, tool_call_id: str, content: str) -> bool:
        """替换恢复出的 ask_user tool 占位，供冷 resume 路径使用。"""
        if not self.agent:
            return False
        messages = getattr(self.agent, "messages", None)
        if not isinstance(messages, list):
            return False

        return self._replace_interrupt_tool_result_in_messages(messages, tool_call_id, content)

    def _prepare_tool_approval_resume_locked(
        self,
        *,
        interrupt_id: str,
        answers: dict[str, str],
        parent_run_id: str,
        turn_preferences_origin_user_message_id: str,
        requested_context: RequestedTurnPreferencesContext | None,
        run_context: AgentRunContext,
    ) -> PreparedAgentRun:
        """Persist an approval answer and prepare its continuation.

        Same-Round execution is split into a durable decision and a later
        dispatch claim. External execution remains deferred to ``Agent.run_agui``
        so a crash before dispatch is recoverable and a crash after dispatch is
        never retried automatically.
        """
        if not self.agent:
            raise RuntimeError("Agent not initialized")

        from src.api.services.tool_permission_service import (
            APPROVAL_CONTINUATION_RESUMABLE_STATUSES,
            APPROVAL_RESOLUTIONS,
            prepare_approval_request,
        )

        resolution = str(answers.get("approval") or "").strip().lower()
        if resolution not in APPROVAL_RESOLUTIONS:
            raise InvalidInteractionResponseError(
                "tool approval requires answers.approval to be one of "
                "allow_once, allow_session, allow_always, deny"
            )
        # Approval has exactly one durable fact.  Ignore unrelated wire keys so
        # retries with the same normalized decision remain idempotent.
        canonical_answers = {"approval": resolution}

        marker = (
            "Tool execution denied by user."
            if resolution == "deny"
            else "[Tool approval execution pending]"
        )
        from src.api.models.tool_permission import ToolApprovalRequest

        messages_snapshot = copy.deepcopy(self.agent.messages)
        pending_interrupt_snapshot = copy.deepcopy(
            getattr(self.agent, "_pending_interrupt", None)
        )
        pending_round_ids_snapshot = dict(self._pending_interrupt_round_ids)
        db = self.history_service.db
        try:
            self._refresh_runtime_messages_from_history()
            round_row, _interaction = AgentInteractionService.lock_pending_for_update(
                db,
                session_id=self.session_id,
                interaction_id=interrupt_id,
            )
            if round_row.id != parent_run_id:
                raise InteractionConflictError(
                    f"Interaction Round ownership changed: {interrupt_id}"
                )
            if round_row.status != "waiting_interaction":
                raise InteractionConflictError(
                    f"Round is not waiting for interaction: {parent_run_id} "
                    f"status={round_row.status}"
                )
            approval = (
                db.query(ToolApprovalRequest)
                .filter(
                    ToolApprovalRequest.id == interrupt_id,
                    ToolApprovalRequest.user_id == self.user_id,
                    ToolApprovalRequest.session_id == self.session_id,
                    ToolApprovalRequest.run_id == parent_run_id,
                    ToolApprovalRequest.status.in_(
                        APPROVAL_CONTINUATION_RESUMABLE_STATUSES
                    ),
                )
                .with_for_update()
                .first()
            )
            if approval is None:
                raise InteractionConflictError(
                    f"Tool approval is not requestable: {interrupt_id}"
                )
            if approval.status != "requested" and approval.resolution != resolution:
                raise InteractionConflictError(
                    f"Tool approval already has a different resolution: {interrupt_id}"
                )
            initial_step = int(round_row.step_count or 0)
            original_user_message = round_row.user_message
            approval_tool_call_id = approval.tool_call_id
            AgentInteractionService.answer_pending(
                db,
                session_id=self.session_id,
                interaction_id=interrupt_id,
                answers=canonical_answers,
                tool_result_content=marker,
                commit=False,
            )
            prepare_approval_request(
                db,
                request_id=interrupt_id,
                user_id=self.user_id,
                resolution=resolution,
                commit=False,
            )
            db.commit()
        except Exception:
            db.rollback()
            self.agent.messages = messages_snapshot
            self.agent._pending_interrupt = pending_interrupt_snapshot
            self._pending_interrupt_round_ids = pending_round_ids_snapshot
            raise

        return PreparedAgentRun(
            run_id=parent_run_id,
            user_message=original_user_message,
            user_message_id=turn_preferences_origin_user_message_id,
            context=run_context,
            requested_context=requested_context,
            parent_run_id=None,
            is_continuation=True,
            initial_step=initial_step,
            interaction_id=interrupt_id,
            interaction_tool_call_id=approval_tool_call_id,
            interaction_tool_result_content=marker,
            interaction_kind="tool_approval",
            tool_approval_resolution=resolution,
        )

    def _claim_and_queue_tool_approval(
        self,
        *,
        interaction_id: str,
        interaction_claim_token: str,
        run_id: str,
        resolution: str,
    ) -> None:
        """Queue an unclaimed tool after a continuation worker owns the Round."""
        if not self.agent:
            raise RuntimeError("Agent not initialized")

        from src.api.models.tool_permission import ToolApprovalRequest
        from src.api.services.tool_permission_service import load_approval_arguments

        db = self.history_service.db
        pending_interrupt_snapshot = copy.deepcopy(
            getattr(self.agent, "_pending_interrupt", None)
        )
        pending_approved_snapshot = copy.deepcopy(
            getattr(self.agent, "_pending_approved_tool", None)
        )
        try:
            round_obj, interaction = AgentInteractionService.lock_pending_for_update(
                db,
                session_id=self.session_id,
                interaction_id=interaction_id,
            )
            if round_obj.id != run_id or interaction.claim_token != interaction_claim_token:
                raise InteractionConflictError(
                    f"Interaction continuation ownership changed: {interaction_id}"
                )
            if round_obj.status != "running":
                raise InteractionConflictError(
                    f"Round continuation is not running: {run_id} status={round_obj.status}"
                )
            request = (
                db.query(ToolApprovalRequest)
                .filter(
                    ToolApprovalRequest.id == interaction_id,
                    ToolApprovalRequest.user_id == self.user_id,
                    ToolApprovalRequest.session_id == self.session_id,
                    ToolApprovalRequest.run_id == run_id,
                    ToolApprovalRequest.status.in_(("approved", "denied")),
                    ToolApprovalRequest.resolution == resolution,
                )
                .with_for_update()
                .first()
            )
            if request is None:
                raise InteractionConflictError(
                    f"Tool approval is not prepared for continuation: {interaction_id}"
                )
            self.agent.queue_tool_approval_resume(
                request_id=request.id,
                tool_call_id=request.tool_call_id,
                function_name=request.model_tool_name,
                arguments=load_approval_arguments(request),
                provider=request.provider,
                tool_name=request.tool_name,
                server_id=request.server_id,
                installation_id=request.installation_id,
                schema_hash=request.schema_hash,
                connection_fingerprint=request.connection_fingerprint,
                resolution=resolution,
                should_execute=request.status == "approved",
                claim_token=None,
                owner_round_id=run_id,
                interaction_claim_token=interaction_claim_token,
            )
            db.rollback()
        except Exception:
            db.rollback()
            self.agent._pending_interrupt = pending_interrupt_snapshot
            self.agent._pending_approved_tool = pending_approved_snapshot
            raise

    async def resume_agui(
        self,
        interrupt_id: str,
        answers: dict[str, str],
    ) -> AsyncIterator[AGUIEvent]:
        """从 ask_user 中断中恢复 Agent 执行。

        使用 _resume_lock 防止并发 resume 调用。

        Args:
            interrupt_id: 中断 ID
            answers: 用户答案 {question_text: answer_label}

        Yields:
            AGUIEvent: AG-UI 协议事件
        """
        prepared = await self.prepare_resume_round(
            interrupt_id=interrupt_id,
            answers=answers,
        )

        async for event in self.run_prepared_round(
            prepared,
            error_label="Resume 执行失败",
        ):
            yield event

    async def prepare_resume_round(
        self,
        *,
        interrupt_id: str,
        answers: dict[str, str],
        contexts: list[Context] | None = None,
    ) -> PreparedAgentRun:
        """Persist an answer and prepare the original Round continuation."""
        if self._resume_lock.locked():
            raise RuntimeError("另一个 resume 操作正在进行中，请等待完成后重试")

        async with self._resume_lock:
            if not self.agent:
                raise RuntimeError("Agent not initialized")

            history_service = getattr(self, "history_service", None)
            history_db = getattr(history_service, "db", None)
            if isinstance(history_db, DBSession):
                history_service.recover_expired_interaction_continuations(
                    self.session_id,
                )
            persisted_interrupt = self._load_persisted_interrupt(interrupt_id)
            pending_interrupt = self._get_agent_pending_interrupt_snapshot(interrupt_id)
            if persisted_interrupt is None:
                if pending_interrupt is not None:
                    self.discard_pending_runtime_state(
                        interrupt_id=interrupt_id,
                        owner_round_id=pending_interrupt.get("round_id"),
                    )
                raise ValueError("No pending interaction to resume from")

            parent_run_id = persisted_interrupt.get("round_id")
            if not isinstance(parent_run_id, str) or not parent_run_id:
                self.discard_pending_runtime_state(
                    interrupt_id=interrupt_id,
                    owner_round_id=None,
                )
                raise ValueError("Pending interaction has no Round")

            interrupt_kind = persisted_interrupt.get("kind")
            interrupt_snapshot = persisted_interrupt
            requested_context = self._requested_context_from_interrupt(interrupt_snapshot)
            turn_preferences_origin_user_message_id = (
                self._turn_preferences_origin_user_message_id_from_interrupt(
                    interrupt_snapshot,
                    parent_run_id=parent_run_id,
                )
            )
            pending_file_drafts = parse_pending_file_draft_contexts(contexts or [])
            run_context = await self._resolve_run_context(
                requested_context,
                **(
                    {"pending_file_drafts": pending_file_drafts}
                    if pending_file_drafts else {}
                ),
            )
            parent_reasoning = self._reasoning_context_from_round(parent_run_id)
            if parent_reasoning is not None:
                run_context = AgentRunContext(
                    preferences=run_context.preferences,
                    reasoning=parent_reasoning,
                    pending_file_drafts=run_context.pending_file_drafts,
                )
            if interrupt_kind == "tool_approval":
                return self._prepare_tool_approval_resume_locked(
                    interrupt_id=interrupt_id,
                    answers=answers,
                    parent_run_id=parent_run_id,
                    turn_preferences_origin_user_message_id=(
                        turn_preferences_origin_user_message_id
                    ),
                    requested_context=requested_context,
                    run_context=run_context,
                )

            question_definitions = interrupt_snapshot.get("questions")
            ordered_answers = Agent.order_interrupt_answers(
                answers,
                (
                    question_definitions
                    if isinstance(question_definitions, list)
                    else None
                ),
            )
            tool_call_id = persisted_interrupt.get("tool_call_id")
            tool_result_content = Agent.format_interrupt_tool_result(
                ordered_answers,
                (
                    question_definitions
                    if isinstance(question_definitions, list)
                    else None
                ),
            )
            messages_snapshot = (
                copy.deepcopy(self.agent.messages)
                if isinstance(getattr(self.agent, "messages", None), list)
                else None
            )
            pending_interrupt_snapshot = copy.deepcopy(
                getattr(self.agent, "_pending_interrupt", None)
            )
            pending_round_ids_snapshot = dict(self._pending_interrupt_round_ids)
            db = self.history_service.db
            try:
                self._refresh_runtime_messages_from_history()
                if self.agent.has_pending_interrupt(interrupt_id):
                    self.agent.resume_from_interrupt(interrupt_id, ordered_answers)
                else:
                    replaced = (
                        self._replace_agent_interrupt_tool_result(
                            tool_call_id,
                            tool_result_content,
                        )
                        if tool_call_id
                        else False
                    )
                    if not replaced:
                        raise RuntimeError(
                            "Unable to restore pending ask_user tool result "
                            f"for interaction {interrupt_id}"
                        )

                from src.api.models.round import Round

                round_row = (
                    db.query(Round)
                    .filter(
                        Round.id == parent_run_id,
                        Round.session_id == self.session_id,
                    )
                    .first()
                )
                if round_row is None:
                    db.rollback()
                    raise ValueError(f"Round not found: {parent_run_id}")
                initial_step = int(round_row.step_count or 0)
                original_user_message = round_row.user_message
                db.rollback()

                answered_interaction = AgentInteractionService.answer_pending(
                    db,
                    session_id=self.session_id,
                    interaction_id=interrupt_id,
                    answers=ordered_answers,
                    tool_result_content=tool_result_content,
                )
                durable_tool_result_content = (
                    answered_interaction.tool_result_content
                    if isinstance(answered_interaction.tool_result_content, str)
                    else tool_result_content
                )
                durable_tool_message = next(
                    (
                        message
                        for message in getattr(self.agent, "messages", [])
                        if getattr(message, "role", None) == "tool"
                        and getattr(message, "tool_call_id", None) == tool_call_id
                    ),
                    None,
                )
                if durable_tool_message is not None:
                    durable_tool_message.content = durable_tool_result_content
                tool_result_content = durable_tool_result_content
            except Exception:
                if messages_snapshot is not None:
                    self.agent.messages = messages_snapshot
                if hasattr(self.agent, "_pending_interrupt"):
                    self.agent._pending_interrupt = pending_interrupt_snapshot
                self._pending_interrupt_round_ids = pending_round_ids_snapshot
                raise

            self._pending_interrupt_round_ids.pop(interrupt_id, None)
            return PreparedAgentRun(
                run_id=parent_run_id,
                user_message=original_user_message,
                user_message_id=turn_preferences_origin_user_message_id,
                context=run_context,
                requested_context=requested_context,
                parent_run_id=None,
                is_continuation=True,
                initial_step=initial_step,
                interaction_id=interrupt_id,
                interaction_tool_call_id=tool_call_id,
                interaction_tool_result_content=tool_result_content,
                interaction_kind="user_input",
            )

    async def _run_round_stream(
        self,
        run_id: str,
        user_message: str,
        user_message_id: str = "",
        run_context: AgentRunContext | None = None,
        requested_context: RequestedTurnPreferencesContext | None = None,
        round_preferred_skills: list[dict[str, str]] | None = None,
        round_preferred_mcp_connections: list[dict[str, str]] | None = None,
        is_continuation: bool = False,
        initial_step: int = 0,
        interaction_id: str | None = None,
        interaction_tool_call_id: str | None = None,
        interaction_tool_result_content: str | None = None,
        interaction_kind: str | None = None,
        tool_approval_resolution: str | None = None,
        error_label: str = "执行失败",
    ) -> AsyncIterator[AGUIEvent]:
        """共享的 round 事件流处理：追踪状态、持久化事件、完成 round。

        chat_agui 和 resume_agui 在创建 round 后都委托到此方法。

        Args:
            run_id: 本轮运行 ID
            user_message: 用户消息文本（用于后台任务）
            error_label: 失败时的错误前缀
        """
        run_context = run_context or AgentRunContext()
        final_response: Optional[str] = None
        step_count = max(int(initial_step or 0), 0)
        status = "running"
        accumulated_content = ""
        _dirty_memory = False
        _memory_write_tools = {"update_long_term_memory", "update_user"}
        _memory_filenames = {"USER.md", "MEMORY.md", "SOUL.md"}
        _file_op_tracking: set[str] = set()
        _round_finished = False  # 追蹤 round 是否已正常完成
        _round_suspended = False
        _final_status: str | None = None  # except 路徑填充
        _final_response: str | None = None
        _externally_terminated = False
        continuation_claim_token: str | None = None
        continuation_claim_completed = False
        continuation_start_committed = False
        continuation_claim_released = False
        continuation_claim_stop = asyncio.Event()
        continuation_claim_heartbeat: asyncio.Task | None = None
        continuation_completion_in_flight = False
        continuation_ownership_lost = asyncio.Event()
        waiting_step_finish_precounted = False
        # 固化本輪各类信号，避免後續新 run 覆蓋 service 屬性導致串擾。
        run_cancel_token = self.cancel_token
        run_liveness_token = getattr(self, "liveness_token", None)
        execution_stop_token = _CombinedRunStopToken(
            run_cancel_token,
            run_liveness_token,
            continuation_ownership_lost,
        )
        self._active_run_count += 1
        run_context_token = current_run_context.set(run_context)

        def _terminal_continuation_fence() -> ContinuationWriteFence | None:
            if (
                not interaction_id
                or not continuation_claim_token
                or continuation_claim_completed
                or continuation_claim_released
            ):
                return None
            return ContinuationWriteFence(
                session_id=self.session_id,
                interaction_id=interaction_id,
                claim_token=continuation_claim_token,
                transition="validate",
            )

        async def _renew_continuation_claim_until_stopped(
            interaction_id_value: str,
            claim_token_value: str,
        ) -> None:
            from src.api.models.database import SessionLocal

            interval = max(DEFAULT_CONTINUATION_LEASE_SECONDS / 3.0, 1.0)
            while True:
                try:
                    await asyncio.wait_for(
                        continuation_claim_stop.wait(),
                        timeout=interval,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    with SessionLocal() as claim_db:
                        AgentInteractionService.renew_continuation_claim(
                            claim_db,
                            session_id=self.session_id,
                            interaction_id=interaction_id_value,
                            claim_token=claim_token_value,
                        )
                except (InteractionConflictError, ValueError):
                    if (
                        continuation_claim_stop.is_set()
                        or continuation_claim_completed
                        or continuation_completion_in_flight
                    ):
                        return
                    logger.info(
                        "Interaction continuation ownership lost: interaction=%s",
                        interaction_id_value,
                        exc_info=True,
                    )
                    continuation_ownership_lost.set()
                    return
                except Exception:
                    logger.warning(
                        "Interaction continuation claim renewal failed; will retry: interaction=%s",
                        interaction_id_value,
                        exc_info=True,
                    )

        async def _release_uncommitted_continuation_start() -> None:
            """Stop lease renewal and release only an uncommitted start."""
            nonlocal continuation_claim_released
            nonlocal continuation_start_committed
            nonlocal _round_suspended

            if not interaction_id or not continuation_claim_token:
                return

            continuation_claim_stop.set()
            if continuation_claim_heartbeat is not None:
                try:
                    await continuation_claim_heartbeat
                except asyncio.CancelledError:
                    pass
            if (
                continuation_claim_completed
                or continuation_start_committed
                or continuation_claim_released
            ):
                return

            # publish() commits before its async fanout.  Cancellation in that
            # post-commit await means the caller never receives StoredEvent,
            # so confirm the authoritative Round state before deciding that
            # start was uncommitted.
            try:
                authoritative_status = self.history_service.get_round_status(run_id)
            except Exception:
                self.history_service.reset_session()
                _round_suspended = True
                return
            if authoritative_status == "running":
                continuation_start_committed = True
                return
            from src.api.models.round import Round

            if authoritative_status in Round.SUBSCRIBE_TERMINAL_STATUSES:
                _round_suspended = True
                return

            try:
                AgentInteractionService.release_continuation_claim(
                    self.history_service.db,
                    session_id=self.session_id,
                    interaction_id=interaction_id,
                    claim_token=continuation_claim_token,
                )
                continuation_claim_released = True
            except Exception:
                self.history_service.reset_session()
            finally:
                # A missing durable interaction_resolved event is still a
                # waiting continuation.  If release itself failed, lease
                # recovery owns convergence; never manufacture a failed Round.
                _round_suspended = True

        async def _record_llm_call(payload: dict[str, Any]) -> None:
            try:
                await self.history_service.save_llm_call_record(
                    session_id=self.session_id,
                    round_id=run_id,
                    step_index=payload["step_index"],
                    request_messages=payload["request_messages"],
                    request_tools=payload["request_tools"],
                    response_content=payload["response_content"],
                    response_thinking=payload["response_thinking"],
                    response_tool_calls=payload["response_tool_calls"],
                    response_error=payload["response_error"],
                    finish_reason=payload["finish_reason"],
                    usage_prompt_tokens=payload["usage_prompt_tokens"],
                    usage_completion_tokens=payload["usage_completion_tokens"],
                    usage_total_tokens=payload["usage_total_tokens"],
                    first_token_latency_s=payload["first_token_latency_s"],
                    completion_latency_s=payload["completion_latency_s"],
                    compaction_triggered=bool(payload.get("compaction_triggered", False)),
                    compaction_pre_tokens=payload.get("compaction_pre_tokens"),
                    compaction_post_tokens=payload.get("compaction_post_tokens"),
                    compaction_tokens_saved=payload.get("compaction_tokens_saved"),
                    compaction_microcompact_compacted_messages=payload.get("compaction_microcompact_compacted_messages"),
                    compaction_summary_generated_count=payload.get("compaction_summary_generated_count"),
                    compaction_summary_reused_count=payload.get("compaction_summary_reused_count"),
                    compaction_summary_quality_repair_count=payload.get("compaction_summary_quality_repair_count"),
                    compaction_emergency_truncate_dropped_rounds=payload.get("compaction_emergency_truncate_dropped_rounds"),
                    history_strategy=get_settings().agent_history_strategy,
                    checkpoint_id=payload.get("checkpoint_id") or self._active_checkpoint_id,
                    call_kind=str(payload.get("call_kind") or "agent_step"),
                )
            except SQLAlchemyError:
                self.history_service.reset_session()
                logger.warning(
                    "保存 LLM 调用快照失败，已跳过以避免中断 Agent run: session=%s round=%s",
                    self.session_id,
                    run_id,
                    exc_info=True,
                )

        async def _persist_compaction(payload: dict[str, Any]) -> str | None:
            if not self.persist_context_checkpoint:
                return None
            from src.api.models.agui_event import AGUIEventLog
            from src.api.models.conversation_message import ConversationMessage

            source_run_ids = [
                str(value)
                for value in payload.get("source_run_ids", [])
                if value
            ]
            source_round_id = source_run_ids[-1] if source_run_ids else None
            message_sequence = 0
            if source_run_ids:
                message_sequence = int(
                    self.history_service.db.query(
                        func.coalesce(func.max(ConversationMessage.sequence), 0)
                    )
                    .filter(
                        ConversationMessage.session_id == self.session_id,
                        ConversationMessage.round_id.in_(source_run_ids),
                    )
                    .scalar()
                    or 0
                )
            event_sequence = 0
            if source_round_id:
                event_sequence = int(
                    self.history_service.db.query(
                        func.coalesce(func.max(AGUIEventLog.sequence), 0)
                    )
                    .filter(AGUIEventLog.run_id == source_round_id)
                    .scalar()
                    or 0
                )
            loaded = ContextCheckpointService(self.history_service.db).save(
                session_id=self.session_id,
                source_round_id=source_round_id,
                source_message_sequence=message_sequence,
                source_event_sequence=event_sequence,
                trigger_phase=str(payload.get("phase") or "pre_turn"),
                summary=str(payload.get("summary") or ""),
                messages=list(payload["replacement_messages"]),
                source_token_count=payload.get("source_token_count"),
                replacement_token_count=payload.get("replacement_token_count"),
            )
            self._active_checkpoint_id = loaded.checkpoint_id
            self._active_checkpoint_sha256 = None
            logger.info(
                "Persisted Codex compacted history immediately: generation=%d phase=%s session=%s",
                loaded.generation,
                loaded.trigger_phase,
                self.session_id,
            )
            return loaded.checkpoint_id

        self.agent.set_llm_call_hook(_record_llm_call)
        compaction_hook_setter = getattr(self.agent, "set_compaction_persist_hook", None)
        if callable(compaction_hook_setter):
            compaction_hook_setter(_persist_compaction)

        try:
            run_agui_kwargs = {
                "thread_id": self.session_id,
                "run_id": run_id,
                "cancel_token": execution_stop_token,
            }
            run_agui_parameters = inspect.signature(self.agent.run_agui).parameters
            accepts_var_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in run_agui_parameters.values()
            )
            if any(
                parameter.name == "llm_request_context"
                or accepts_var_kwargs
                for parameter in run_agui_parameters.values()
            ):
                run_agui_kwargs["llm_request_context"] = LLMRequestContext(
                    purpose="agent_step",
                    run_context=run_context,
                    user_message_id=user_message_id,
                )
            if "emit_run_started" in run_agui_parameters or accepts_var_kwargs:
                run_agui_kwargs["emit_run_started"] = not is_continuation
            if "initial_step" in run_agui_parameters or accepts_var_kwargs:
                run_agui_kwargs["initial_step"] = step_count

            async def _events_with_interaction_prelude():
                nonlocal continuation_claim_token
                nonlocal continuation_claim_heartbeat
                if interaction_id and interaction_tool_call_id and interaction_tool_result_content is not None:
                    claimed = AgentInteractionService.claim_answered_continuation(
                        self.history_service.db,
                        session_id=self.session_id,
                        interaction_id=interaction_id,
                    )
                    continuation_claim_token = claimed.claim_token
                    if not continuation_claim_token:
                        raise RuntimeError(
                            f"Interaction continuation claim missing token: {interaction_id}"
                        )
                    continuation_claim_heartbeat = asyncio.create_task(
                        _renew_continuation_claim_until_stopped(
                            interaction_id,
                            continuation_claim_token,
                        )
                    )
                    try:
                        yield CustomEvent(
                            name="interaction_resolved",
                            value={
                                "interactionId": interaction_id,
                                "runId": run_id,
                                "toolCallId": interaction_tool_call_id,
                                "toolResultContent": interaction_tool_result_content,
                                "resolution": "answered",
                            },
                        )
                        if interaction_kind == "tool_approval":
                            if not tool_approval_resolution:
                                raise RuntimeError(
                                    f"Tool approval continuation missing resolution: {interaction_id}"
                                )
                            self._claim_and_queue_tool_approval(
                                interaction_id=interaction_id,
                                interaction_claim_token=continuation_claim_token,
                                run_id=run_id,
                                resolution=tool_approval_resolution,
                            )
                        async for agent_event in self.agent.run_agui(**run_agui_kwargs):
                            yield agent_event
                    finally:
                        continuation_claim_stop.set()
                        if continuation_claim_heartbeat is not None:
                            try:
                                await continuation_claim_heartbeat
                            except asyncio.CancelledError:
                                pass
                else:
                    async for agent_event in self.agent.run_agui(**run_agui_kwargs):
                        yield agent_event

            async for event in _events_with_interaction_prelude():
                # 本輪已被外部收斂為終態（常見於 abort 立即 cancelled）時，
                # 停止處理遲到事件，避免污染 conversation_messages 與 round 狀態。
                if self.history_service.is_round_terminal(run_id) is True:
                    current_status = self.history_service.get_round_status(run_id) or "cancelled"
                    logger.info(
                        "Round %s 已被外部收斂為 %s，停止處理遲到事件",
                        run_id,
                        current_status,
                    )
                    status = current_status
                    _round_finished = True
                    _externally_terminated = True
                    stored_terminal = RunCompletionService(
                        self.history_service.db
                    ).ensure_terminal_sync(run_id)
                    self.history_service.reset_session()
                    if isinstance(stored_terminal, StoredEvent):
                        yield stored_terminal.event
                    break

                if not bool(run_cancel_token and run_cancel_token.is_set()):
                    if continuation_ownership_lost.is_set():
                        raise ContinuationOwnershipLostError(
                            f"Interaction continuation ownership lost: {interaction_id}"
                        )
                    if run_liveness_token and run_liveness_token.is_set():
                        raise _RunLivenessLostError(
                            f"UserRunLock ownership lost for Round {run_id}"
                        )

                if (
                    event.type == EventType.RUN_STARTED
                    and (
                        round_preferred_skills is not None
                        or round_preferred_mcp_connections is not None
                    )
                ):
                    event = event.model_copy(update={
                        "preferred_skills": [
                            {
                                "key": item["key"],
                                "display_name": item["display_name"],
                            }
                            for item in (round_preferred_skills or [])
                        ],
                        "preferred_mcp_connections": [
                            {
                                "server_id": item["server_id"],
                                "display_name": item["display_name"],
                            }
                            for item in (round_preferred_mcp_connections or [])
                        ],
                    })

                if (
                    event.type == EventType.CUSTOM
                    and getattr(event, "name", "") == "assistant_file_referenced"
                ):
                    try:
                        materialized_reference = (
                            await self._materialize_assistant_file_reference(
                                getattr(event, "value", None),
                                run_id=run_id,
                            )
                        )
                    except Exception:
                        self.history_service.db.rollback()
                        logger.warning(
                            "助手文件引用冻结失败，已丢弃该展示引用: session=%s round=%s",
                            self.session_id,
                            run_id,
                            exc_info=True,
                        )
                        continue
                    if materialized_reference is None:
                        logger.info(
                            "助手文件引用在持久化前已失效，已跳过: session=%s round=%s",
                            self.session_id,
                            run_id,
                        )
                        continue
                    event = event.model_copy(update={"value": materialized_reference})

                if (
                    event.type == EventType.CUSTOM
                    and getattr(event, "name", "") == "workspace_resource_changed"
                    and isinstance(getattr(event, "value", None), dict)
                ):
                    workspace_event_value = dict(event.value)
                    try:
                        workspace_reference = self._protect_workspace_assistant_file(
                            workspace_event_value,
                            run_id=run_id,
                        )
                    except Exception:
                        self.history_service.db.rollback()
                        workspace_reference = None
                        logger.warning(
                            "Workspace 助手文件版本保护失败，仅保留内部变更事件: round=%s",
                            run_id,
                            exc_info=True,
                        )
                    if workspace_reference is not None:
                        workspace_event_value["assistant_file_reference"] = workspace_reference
                        event = event.model_copy(update={"value": workspace_event_value})

                event_to_store = event
                event_to_yield = event
                synthetic_user_content = None
                pending_interaction_to_commit: str | None = None
                pending_interaction_step_count: int | None = None
                if (
                    event.type == EventType.CUSTOM
                    and getattr(event, "name", "") == "interaction_requested"
                ):
                    value = dict(event.value) if isinstance(event.value, dict) else {}
                    interaction_id_value = value.get("interactionId")
                    tool_call_id_value = value.get("toolCallId")
                    kind = value.get("kind") or "user_input"
                    if not isinstance(interaction_id_value, str) or not interaction_id_value:
                        raise RuntimeError("interaction_requested is missing interactionId")
                    pending = getattr(self.agent, "_pending_interrupt", None)
                    self._attach_turn_preferences_interaction_context(
                        value,
                        pending if isinstance(pending, dict) else None,
                        requested_context=requested_context,
                        origin_user_message_id=user_message_id,
                    )
                    event = event.model_copy(update={"value": value})
                    event_to_store = event
                    event_to_yield = event
                    # interaction_requested is emitted only after the Agent has
                    # completed the current step. Persist that cumulative fact
                    # in the same transaction as the waiting interaction/event
                    # so a crash before the following STEP_FINISHED cannot
                    # repeat the step number or grant extra budget on resume.
                    pending_interaction_step_count = step_count + 1
                    AgentInteractionService.create_pending(
                        self.history_service.db,
                        interaction_id=interaction_id_value,
                        session_id=self.session_id,
                        round_id=run_id,
                        kind=str(kind),
                        tool_call_id=(
                            tool_call_id_value
                            if isinstance(tool_call_id_value, str)
                            else None
                        ),
                        request_payload=value,
                        step_count=pending_interaction_step_count,
                        commit=False,
                    )
                    pending_interaction_to_commit = interaction_id_value
                if (
                    event.type == EventType.CUSTOM
                    and getattr(event, "name", "") == "synthetic_user_message"
                ):
                    synthetic_user_content = self._synthetic_user_content_from_event(event)
                    if self._has_synthetic_user_content(synthetic_user_content):
                        self._save_conversation_message(
                            "user",
                            synthetic_user_content,
                            round_id=run_id,
                            is_synthetic=True,
                            raise_on_error=True,
                            commit=False,
                        )
                        event_to_store = self._lightweight_synthetic_user_event(
                            event,
                            synthetic_user_content,
                        )
                        event_to_yield = event_to_store

                is_interaction_prelude = bool(
                    interaction_id
                    and event.type == EventType.CUSTOM
                    and getattr(event, "name", "") == "interaction_resolved"
                    and isinstance(getattr(event, "value", None), dict)
                    and event.value.get("interactionId") == interaction_id
                )
                continuation_fence: ContinuationWriteFence | None = None
                if (
                    interaction_id
                    and continuation_claim_token
                    and not continuation_claim_completed
                ):
                    if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}:
                        transition = "validate"
                    elif is_interaction_prelude:
                        transition = "start"
                    elif (
                        interaction_kind == "tool_approval"
                        and not (
                            event.type == EventType.TOOL_CALL_RESULT
                            and getattr(event, "tool_call_id", None)
                            == interaction_tool_call_id
                        )
                    ):
                        transition = "validate"
                    else:
                        transition = "complete"
                    continuation_fence = ContinuationWriteFence(
                        session_id=self.session_id,
                        interaction_id=interaction_id,
                        claim_token=continuation_claim_token,
                        transition=transition,
                    )

                stored_event = None
                try:
                    if event_to_store.type not in {EventType.RUN_FINISHED, EventType.RUN_ERROR}:
                        continuation_completion_in_flight = bool(
                            continuation_fence is not None
                            and continuation_fence.transition == "complete"
                        )
                        stored_event = await self.history_service.save_agui_event(
                            run_id,
                            event_to_store,
                            continuation_fence=continuation_fence,
                        )
                except RoundTerminalWriteSuppressed as exc:
                    if pending_interaction_to_commit is not None:
                        self.history_service.db.rollback()
                    if continuation_fence is not None:
                        raise ContinuationOwnershipLostError(str(exc)) from exc
                    status = (
                        self.history_service.get_round_status(run_id)
                        or "cancelled"
                    )
                    _round_finished = True
                    _externally_terminated = True
                    stored_terminal = RunCompletionService(
                        self.history_service.db
                    ).ensure_terminal_sync(run_id)
                    self.history_service.reset_session()
                    if isinstance(stored_terminal, StoredEvent):
                        yield stored_terminal.event
                    break
                except InteractionConflictError as exc:
                    if pending_interaction_to_commit is not None:
                        self.history_service.db.rollback()
                    if continuation_fence is not None:
                        raise ContinuationOwnershipLostError(str(exc)) from exc
                    raise
                except Exception:
                    if pending_interaction_to_commit is not None:
                        self.history_service.db.rollback()
                    raise
                finally:
                    continuation_completion_in_flight = False

                if (
                    continuation_fence is not None
                    and continuation_fence.transition == "complete"
                    and stored_event is not None
                ):
                    continuation_claim_completed = True
                    continuation_claim_stop.set()
                elif (
                    is_interaction_prelude
                    and continuation_fence is not None
                    and continuation_fence.transition == "start"
                    and stored_event is not None
                ):
                    # From this commit onward the continuation is a running
                    # Round.  Startup failures must consume the still-owned
                    # fence into a durable failed terminal, never re-park it.
                    continuation_start_committed = True

                if pending_interaction_to_commit is not None:
                    interaction_id_value = pending_interaction_to_commit
                    step_count = max(
                        step_count,
                        int(pending_interaction_step_count or 0),
                    )
                    waiting_step_finish_precounted = True
                    self._attach_agent_pending_interrupt_round_id(
                        interaction_id_value,
                        run_id,
                    )
                    self._pending_interrupt_round_ids[interaction_id_value] = run_id
                    status = "waiting_interaction"
                    _round_suspended = True

                if event.type == EventType.TEXT_MESSAGE_CONTENT:
                    accumulated_content += event.delta
                elif event.type == EventType.TEXT_MESSAGE_END:
                    final_response = accumulated_content
                    if accumulated_content:
                        # Extract token count from LLM usage if available
                        tc = None
                        usage = getattr(self.agent, 'last_llm_usage', None)
                        if usage:
                            tc = usage.total_tokens or None
                        self._save_conversation_message("assistant", accumulated_content, round_id=run_id, token_count=tc)
                    accumulated_content = ""
                elif event.type == EventType.TOOL_CALL_START:
                    tool_name = getattr(event, "tool_call_name", "")
                    if tool_name in _memory_write_tools:
                        _dirty_memory = True
                    elif tool_name in ("write_file", "edit_file"):
                        tcid = getattr(event, "tool_call_id", "")
                        if tcid:
                            _file_op_tracking.add(tcid)
                elif event.type == EventType.TOOL_CALL_ARGS:
                    if not _dirty_memory and _file_op_tracking:
                        tcid = getattr(event, "tool_call_id", "")
                        if tcid in _file_op_tracking:
                            delta = getattr(event, "delta", "")
                            if any(fn in delta for fn in _memory_filenames):
                                _dirty_memory = True
                                _file_op_tracking.discard(tcid)
                elif event.type == EventType.TOOL_CALL_END:
                    tcid = getattr(event, "tool_call_id", "")
                    _file_op_tracking.discard(tcid)
                elif event.type == EventType.STEP_FINISHED:
                    if waiting_step_finish_precounted:
                        waiting_step_finish_precounted = False
                    else:
                        step_count += 1
                elif event.type == EventType.RUN_FINISHED:
                    _result = event.result
                    if isinstance(_result, dict):
                        terminal_text = (
                            _result.get("finalResponse")
                            or _result.get("final_response")
                        )
                        if terminal_text:
                            final_response = terminal_text

                    if event.outcome == "success":
                        status = "completed"
                    elif event.outcome == "interrupt":
                        reason = _result.get("reason") if isinstance(_result, dict) else None
                        if reason == "user_cancelled":
                            status = "cancelled"
                        elif reason == "max_steps_reached":
                            status = "max_steps_reached"
                        else:
                            raise RuntimeError(
                                f"Unsupported RUN_FINISHED interrupt reason: {reason!r}"
                            )
                    else:
                        status = "failed"
                elif event.type == EventType.RUN_ERROR:
                    status = "failed"
                    final_response = getattr(event, "message", None) or final_response

                if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}:
                    requested_terminal_payload = event_to_yield.model_dump(
                        by_alias=True,
                        exclude_none=True,
                        mode="json",
                    )
                    try:
                        completed_round = self.history_service.complete_round(
                            round_id=run_id,
                            final_response=final_response,
                            step_count=step_count,
                            status=status,
                            terminal_event=event,
                            continuation_fence=_terminal_continuation_fence(),
                        )
                    except InteractionConflictError as exc:
                        raise ContinuationOwnershipLostError(str(exc)) from exc
                    continuation_claim_stop.set()
                    stored_event = self.history_service.last_terminal_event
                    if not isinstance(stored_event, StoredEvent):
                        from src.api.models.round import Round

                        authoritative_status = getattr(completed_round, "status", None)
                        if authoritative_status in Round.SUBSCRIBE_TERMINAL_STATUSES:
                            stored_event = RunCompletionService(
                                self.history_service.db
                            ).ensure_terminal_sync(run_id)
                            self.history_service.reset_session()
                    if isinstance(stored_event, StoredEvent):
                        await get_agui_event_bus().publish_committed(run_id, stored_event.event)
                    _round_finished = True

                    if isinstance(stored_event, StoredEvent):
                        committed_payload = dict(stored_event.event)
                        committed_payload.pop("sequence", None)
                        if committed_payload != requested_terminal_payload:
                            status = (
                                getattr(completed_round, "status", None)
                                or status
                            )
                            _externally_terminated = True
                            yield stored_event.event
                            break

                yield SequencedAGUIEvent(event_to_yield, stored_event) if stored_event else event_to_yield

            if _externally_terminated:
                return

            if _round_suspended:
                self.history_service.save_waiting_round_progress(
                    run_id,
                    step_count=step_count,
                )
                return

            if not _round_finished and not _round_suspended:
                try:
                    self.history_service.complete_round(
                        round_id=run_id,
                        final_response=final_response,
                        step_count=step_count,
                        status=status,
                        continuation_fence=_terminal_continuation_fence(),
                    )
                except InteractionConflictError as exc:
                    raise ContinuationOwnershipLostError(str(exc)) from exc
                _round_finished = True
            if status == "completed" and not bool(run_cancel_token and run_cancel_token.is_set()):
                task = asyncio.create_task(
                    self._post_round_tasks(
                        sync_memory=_dirty_memory,
                        round_id=run_id,
                        user_message=user_message,
                        assistant_response=final_response,
                    ),
                    context=contextvars.Context(),
                )
                task.add_done_callback(self._on_post_round_done)

        except ContinuationOwnershipLostError:
            _round_suspended = True
            logger.info(
                "Round %s 的 continuation 已由其他 worker 接管，旧 worker 静默退出",
                run_id,
            )
            return
        except _RunLivenessLostError:
            _round_suspended = True
            logger.warning(
                "Round %s 的 UserRunLock 已丢失，旧 worker 静默退出等待恢复收敛",
                run_id,
            )
            return
        except Exception as e:
            _final_status = "failed"
            _final_response = f"{error_label}: {str(e)}"
            raise
        finally:
            await _release_uncommitted_continuation_start()
            # 統一處理 round 完成：正常路徑、異常、GeneratorExit、CancelledError
            if not _round_finished and not _round_suspended:
                try:
                    # 僅在可確認本地 cancel_token 已觸發時視為用戶取消。
                    # 其餘未知異常中斷（如框架級取消、進程退出）保守標記為 failed，
                    # 避免把系統級中斷混淆為 cancelled。
                    _is_user_cancel = bool(run_cancel_token and run_cancel_token.is_set())
                    _actual_status = "cancelled" if _is_user_cancel else (_final_status or "failed")
                    _fallback_response = "Cancelled" if _actual_status == "cancelled" else "Failed"
                    self.history_service.reset_session()
                    self.history_service.complete_round(
                        round_id=run_id,
                        final_response=_final_response or accumulated_content or final_response or _fallback_response,
                        step_count=step_count,
                        status=_actual_status,
                        continuation_fence=_terminal_continuation_fence(),
                    )
                    status = _actual_status
                    stored_event = self.history_service.last_terminal_event
                    if isinstance(stored_event, StoredEvent):
                        await get_agui_event_bus().publish_committed(run_id, stored_event.event)
                    _round_finished = True
                    logger.warning(
                        "Round %s 異常退出（disconnect/cancel/error），已標記為 %s (steps=%d)",
                        run_id, _actual_status, step_count,
                    )
                except InteractionConflictError:
                    _round_suspended = True
                    logger.info(
                        "Round %s 的 continuation 在异常收敛前已被接管，跳过旧 worker 终态",
                        run_id,
                    )
                except Exception:
                    logger.error("Round %s 異常退出後無法更新 DB", run_id, exc_info=True)
            if _round_finished:
                from src.api.models.round import Round

                if status in Round.COMPLETE_TERMINAL_STATUSES:
                    self.discard_pending_runtime_state(owner_round_id=run_id)
            self.agent.set_llm_call_hook(None)
            if callable(compaction_hook_setter):
                compaction_hook_setter(None)
            self._active_run_count = max(0, self._active_run_count - 1)
            current_run_context.reset(run_context_token)

    async def _post_round_tasks(
        self,
        sync_memory: bool = False,
        round_id: str = "",
        user_message: str = "",
        assistant_response: str | None = None,
    ):
        """Round 结束后的异步后台任务。

        仅同步本轮已明确发生的记忆文件写入。旧的 token
        阈值静默刷新已移除。后续候选提炼必须在 durable run lock
        释放后由持久化 idle job 执行，且不得直接写 canonical Memory。
        """
        if sync_memory:
            await self._sync_memory_to_db()

        # 自动索引对话内容到 memory_embeddings（确保 search_memory 可检索）
        if round_id and (user_message or assistant_response):
            await self._index_conversation_to_memory(
                round_id, user_message, assistant_response or ""
            )

    async def _sync_memory_to_db(self):
        """将沙箱记忆文件同步回 DB 并重建 embedding"""
        try:
            from src.api.services.memory_service import MemoryService, FILE_TYPE_TO_FILENAME
            from src.api.models.database import SessionLocal

            db = SessionLocal()
            try:
                mem_svc = MemoryService(db)
                # 同步用户 DB-backed 配置文件（USER/MEMORY/SOUL）。
                # AGENTS.md 由平台模板管理，不从沙箱回写 DB。
                for ft in ("user_md", "memory_md", "soul_md"):
                    sync_result = await mem_svc.sync_from_sandbox(
                        self.user_id, self.sandbox, ft
                    )
                    if sync_result is not None:
                        content, changed = sync_result
                        filename = FILE_TYPE_TO_FILENAME[ft]
                        # 仅对 USER 和 MEMORY 重建语义索引
                        if changed and ft in ("user_md", "memory_md"):
                            await mem_svc.rebuild_embeddings(self.user_id, filename, content)
                        logger.info(
                            "记忆同步完成: %s (%d chars, changed=%s)",
                            filename, len(content), changed,
                        )
            finally:
                db.close()
        except Exception as e:
            logger.warning("记忆同步回 DB 失败: %s", e)

    async def _index_conversation_to_memory(
        self, round_id: str, user_message: str, assistant_response: str
    ):
        """将对话内容索引到 memory_embeddings，使 search_memory 可跨会话检索"""
        try:
            from src.api.services.memory_service import MemoryService
            from src.api.models.database import SessionLocal

            db = SessionLocal()
            try:
                mem_svc = MemoryService(db)
                count = await mem_svc.index_conversation_round(
                    user_id=self.user_id,
                    session_id=self.session_id,
                    round_id=round_id,
                    user_message=user_message,
                    assistant_response=assistant_response,
                )
                if count:
                    logger.info(
                        "对话自动索引完成: session=%s, round=%s, chunks=%d",
                        self.session_id, round_id, count,
                    )
            finally:
                db.close()
        except Exception as e:
            logger.warning("对话自动索引失败: %s", e)

    async def generate_session_title(self, first_message: str) -> str:
        """根据用户的第一条消息生成会话标题

        Args:
            first_message: 用户的第一条消息

        Returns:
            生成的会话标题（不超过30个字符）
        """
        if not self.agent:
            raise RuntimeError("Agent not initialized")

        # 使用 LLM 生成简短标题
        title_prompt = f"""请根据用户的消息，生成一个简洁的会话标题。

要求：
- 长度不超过30个字符
- 准确概括用户意图
- 使用中文
- 只返回标题本身，不要任何额外的说明或标点

用户消息：
{first_message}

标题："""

        try:
            # 创建一个临时消息列表来调用 LLM
            temp_messages = [
                AgentMessage(role="user", content=title_prompt)
            ]

            # 调用 LLM
            response = await self.agent.llm.generate(
                messages=temp_messages,
            )

            # 提取标题并清理
            title = response.content.strip()

            # 确保不超过30个字符
            if len(title) > 30:
                title = title[:30]

            # 移除可能的引号
            title = title.strip('"\'')

            logger.info("生成会话标题: %s", title)
            return title

        except Exception as e:
            logger.warning("标题生成失败: %s", e)
            # 失败时返回默认标题
            return first_message[:30] if len(first_message) > 30 else first_message
