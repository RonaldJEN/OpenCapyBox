"""Agent 服务 - 连接 OpenCapyBox 核心"""
import asyncio
import copy
import json
import logging
import uuid
from dataclasses import dataclass
from typing import List, Dict, Optional, AsyncIterator, Any

from opensandbox import Sandbox
from sqlalchemy.exc import SQLAlchemyError

from src.api.utils.timezone import now_naive
from src.agent.agent import Agent
from src.agent.llm import LLMClient
from src.agent.schema import Message as AgentMessage
from src.agent.schema.agui_events import AGUIEvent, EventType

from src.api.services.history_service import HistoryService
from src.api.services.agui_event_bus import SequencedAGUIEvent, StoredEvent, get_agui_event_bus
from src.api.services.run_completion_service import RunCompletionService
from src.api.services.sandbox_service import get_sandbox_service
from src.api.services.subagent_graph_service import get_subagent_graph_service
from src.api.services.tool_factory import create_agent_tools
from src.api.config import get_settings
from src.api.model_registry import get_model_registry
from src.agent.tools.base import ToolResult, ToolRuntimeContext
from pathlib import Path as PathlibPath

logger = logging.getLogger(__name__)


class DuplicateRoundError(Exception):
    """幂等衝突：另一個 Worker 已搶先創建了相同 idempotency_key 的 Round"""
    def __init__(self, existing_round_id: str):
        self.existing_round_id = existing_round_id
        super().__init__(f"Duplicate round: {existing_round_id}")


@dataclass(frozen=True)
class PreparedAgentRun:
    """A round that has been created and is ready to execute."""

    run_id: str
    user_message: str
    parent_run_id: str | None = None


settings = get_settings()


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
    ):
        self.sandbox = sandbox
        self.history_service = history_service
        self.session_id = session_id
        self.user_id = user_id
        self.model_id = model_id
        self.tool_exclude = set(tool_exclude or set())
        self.system_prompt_override = system_prompt_override
        self.agent: Agent | None = None
        self._last_saved_index = 0
        self._pending_interrupt_round_ids: dict[str, str] = {}
        self.skill_loader = None  # 保存 skill_loader 引用
        self.cancel_token: asyncio.Event | None = None  # per-run 取消令牌
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

            logger.info(
                "创建 LLM 客户端: model=%s, provider=%s, api_base=%s",
                model_config.model_name, model_config.provider, model_config.api_base,
            )

            # 收集 fallback 模型（排除當前主模型，按 YAML 順序），并保持多模态能力不降级。
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
                "請修復 models.yaml 配置後重試。"
            ) from e

        except ValueError as e:
            if self.model_id and ("不存在" in str(e) or "已停用" in str(e)):
                raise
            raise RuntimeError(
                f"Model Registry 配置異常: {e}. "
                "請修復 models.yaml 或環境變數後重試。"
            ) from e

        # === 新用户默认文件初始化 ===
        self._provision_default_files_if_needed()

        # 加载 system prompt：子 Agent 走 profile 精简提示（override），否则拼装父记忆
        system_prompt = self.system_prompt_override or self._load_system_prompt()

        # 创建工具列表
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
        )

        # 注入技能元数据到系统提示符（Progressive Disclosure - Level 1）
        if self.skill_loader:
            skills_metadata = self.skill_loader.get_skills_metadata_prompt()
            if skills_metadata:
                system_prompt += f"\n\n## 已注册技能列表\n\n{skills_metadata}\n"
                total = len(self.skill_loader.loaded_skills) + len(self.skill_loader.sandbox_skills)
                logger.info("已注入 %d 个技能元数据到系统提示符", total)

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
        )

        # 从数据库恢复历史
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
            )
            await child_service.initialize_agent()
            child_service.cancel_token = context.cancel_token

            if not child_service.agent:
                raise RuntimeError("child agent failed to initialize")

            # Subagents are sidechains: they get their own task prompt, not the
            # full parent conversation replay. Their transcript is persisted via
            # the child Round and graph edge.
            child_service.agent.messages = [child_service.agent.messages[0]]
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
        """从 DB 记忆文件组装 
        SOUL.md / 平台 AGENTS.md 模板已包含全部指令（身份、工具规则、记忆管理等），
        仅当 DB 中无任何记忆文件时，使用极简 fallback。
        """
        memory_context = self._build_memory_context()
        if memory_context:
            return memory_context
        # fallback：DB 中无记忆文件（理论上新用户已通过 provision 注入）
        return "You are OpenCapyBox, a versatile AI assistant. Help the user with their tasks."

    def _provision_default_files_if_needed(self) -> None:
        """为新用户写入默认注入文件模板（幂等）

        检查 DB 中是否存在用户记忆文件，如果不存在则从 docs/ 模板写入默认值。
        包括：SOUL.md, MEMORY.md, USER.md(PROFILE)。AGENTS.md 由平台模板直接注入。
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

        SOUL/USER/MEMORY 来自用户 DB；AGENTS.md 始终来自平台模板。
        """
        try:
            from src.api.services.memory_service import MemoryService
            import tiktoken

            db = self.history_service.db
            mem_svc = MemoryService(db)
            all_files = mem_svc.get_all_memory_files(self.user_id)
            agents = mem_svc.get_agents_template_content()

            if not all_files and not agents.strip():
                return ""

            try:
                encoding = tiktoken.get_encoding("cl100k_base")
                count_tokens = lambda t: len(encoding.encode(t))
            except Exception:
                count_tokens = lambda t: int(len(t) / 2.5)

            max_memory_tokens = int(self._token_limit * 0.15)

            parts: list[str] = []
            used_tokens = 0

            # 高优先级（必须注入）
            soul = all_files.get("soul_md", "")
            if soul:
                parts.append(f"## Agent 人格\n{soul}\n")
                used_tokens += count_tokens(soul)

            user = all_files.get("user_md", "")
            if user:
                parts.append(f"## 用户画像\n{user}\n")
                used_tokens += count_tokens(user)

            if agents:
                parts.append(f"## 行为规则\n{agents}\n")
                used_tokens += count_tokens(agents)

            # 低优先级（按剩余 budget 截断）
            memory_budget = max(0, max_memory_tokens - used_tokens)

            memory = all_files.get("memory_md", "")
            if memory and memory_budget > 0:
                half_budget = memory_budget // 2
                truncated = self._truncate_to_tokens(memory, half_budget, count_tokens)
                if truncated:
                    parts.append(f"## 长期记忆\n{truncated}\n")
                    memory_budget -= count_tokens(truncated)

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

        从 rounds / conversation_messages / agui_events / interrupt_resolutions 重建
        LLM 消息数组，并在存在摘要锚点时按裁剪规则注入。

        注意：為防止歷史消息過多導致模型 context 膨脹（特別是 tool calling 能力較弱的模型），
        會限制最多注入 agent_max_history_messages 條消息，超出時只保留最近的消息。
        """
        from src.api.config import get_settings as _get_settings

        # 從 agui_events 重建完整消息列表（含 tool_calls 和 tool results）
        messages = self._rebuild_messages_from_events()
        summary_anchor = self._load_latest_summary_anchor()

        if not messages and not summary_anchor:
            return []

        # 限制歷史消息數量
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

        if summary_anchor and not self._has_summary_anchor(messages, summary_anchor.content):
            messages = [summary_anchor] + messages
            logger.info("歷史恢復注入摘要錨點 (session=%s)", self.session_id)

        return messages

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

        logger.info(
            "已刷新 Agent runtime messages: history=%d total=%d (session=%s)",
            len(restored_messages), len(self.agent.messages), self.session_id,
        )
        self._last_saved_index = len(self.agent.messages)

    def _restore_history(self):
        """从 conversation_messages 表恢复对话历史。"""
        self._refresh_runtime_messages_from_history()

    def _rebuild_messages_from_events(self) -> list[AgentMessage]:
        """從 agui_events + conversation_messages 重建完整的 LLM messages 數組。

        conversation_messages 提供 user 消息（含多模態內容），
        agui_events 提供 assistant + tool 交互（單一事實源，無數據重複）。

        Returns:
            按時序排列的 AgentMessage 列表
        """
        from src.api.models.agui_event import AGUIEventLog
        from src.api.models.conversation_message import ConversationMessage
        from src.api.models.interrupt_resolution import InterruptResolution
        from src.api.models.round import Round
        from src.api.models.subagent_run import SubagentRun

        db = self.history_service.db

        # 1. 獲取本 session 的所有 round（按時間排序）
        rounds = (
            db.query(Round)
            .filter(Round.session_id == self.session_id)
            .order_by(Round.created_at)
            .all()
        )
        if not rounds:
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
                self.history_service.reset_session()
                return []

        # 2. 預載所有 user + assistant 消息（按 round_id 索引）
        conv_msgs = (
            db.query(ConversationMessage)
            .filter(
                ConversationMessage.session_id == self.session_id,
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

        # 4. 预载 ask_user resume resolution，用于让冷恢复重建出热 resume 等价结构。
        resolution_rows = (
            db.query(InterruptResolution)
            .filter(InterruptResolution.session_id == self.session_id)
            .order_by(InterruptResolution.created_at)
            .all()
        )
        round_by_id = {rnd.id: rnd for rnd in rounds}
        resolution_by_parent_round_id = {}
        resolution_by_resume_round_id = {}
        for row in resolution_rows:
            child_round = round_by_id.get(row.resume_round_id)
            if not child_round or getattr(child_round, "parent_run_id", None) != row.parent_round_id:
                fallback_reason = (
                    "history stitch child round not found"
                    if not child_round
                    else "history stitch parent_run_id mismatch"
                )
                self.history_service.update_interrupt_resolution_fallback(
                    row.interrupt_id,
                    fallback_reason,
                )
                logger.warning(
                    "忽略无法按 parent_run_id 对齐的 interrupt resolution: "
                    "session=%s parent_round=%s resume_round=%s reason=%s",
                    self.session_id,
                    row.parent_round_id,
                    row.resume_round_id,
                    fallback_reason,
                )
                continue
            resolution_by_parent_round_id[row.parent_round_id] = row
            resolution_by_resume_round_id[row.resume_round_id] = row
        stitched_resume_round_ids: set[str] = set()

        # 5. 逐 round 重建
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
            round_events = events_by_round.get(rnd.id, [])
            has_synthetic_user_custom = _round_has_synthetic_user_custom(round_events)

            # 4a. User 消息（從 conversation_messages 取，保留多模態塊）
            user_records = user_msgs_by_round.get(rnd.id, [])
            resume_resolution = resolution_by_resume_round_id.get(rnd.id)
            skip_resume_user = rnd.id in stitched_resume_round_ids
            skipped_resume_user = False
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
                    if (
                        skip_resume_user
                        and not skipped_resume_user
                        and resume_resolution
                        and content == resume_resolution.resume_user_message
                    ):
                        skipped_resume_user = True
                        continue
                    messages.append(
                        AgentMessage(
                            role="user",
                            content=content,
                            is_synthetic=is_synthetic,
                        )
                    )
            if not has_real_user_record and rnd.user_message:
                if (
                    skip_resume_user
                    and resume_resolution
                    and rnd.user_message == resume_resolution.resume_user_message
                ):
                    skipped_resume_user = True
                else:
                    # Fallback：conversation_messages 無記錄（歷史數據遷移期），用 rounds.user_message
                    logger.warning(
                        "Round %s 無 conversation_messages user 記錄，fallback 到 rounds.user_message (session=%s)",
                        rnd.id, self.session_id,
                    )
                    messages.append(AgentMessage(role="user", content=rnd.user_message))
            elif not has_real_user_record:
                if skip_resume_user:
                    skipped_resume_user = True
                else:
                    # 兩邊都無 user 消息（數據損壞），跳過該 round 的 agent 輸出以避免孤立 assistant 消息
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

            # interrupted round 被後續輪次解決後，避免冷恢復時仍看到過期占位內容
            if getattr(rnd, "status", None) == "resumed":
                resolution = resolution_by_parent_round_id.get(rnd.id)
                if resolution:
                    replaced = False
                    if resolution.tool_call_id:
                        replaced = self._replace_interrupt_tool_result_in_messages(
                            round_messages,
                            resolution.tool_call_id,
                            resolution.tool_result_content,
                        )
                    if replaced:
                        stitched_resume_round_ids.add(resolution.resume_round_id)
                    else:
                        fallback_reason = (
                            "history stitch tool_call_id missing"
                            if not resolution.tool_call_id
                            else "history stitch tool placeholder not found or already resolved"
                        )
                        self.history_service.update_interrupt_resolution_fallback(
                            resolution.interrupt_id,
                            fallback_reason,
                        )
                        logger.warning(
                            "未能按 interrupt resolution 回填 ask_user tool result: "
                            "session=%s parent_round=%s resume_round=%s tool_call_id=%s reason=%s",
                            self.session_id,
                            rnd.id,
                            resolution.resume_round_id,
                            resolution.tool_call_id,
                            fallback_reason,
                        )
                for msg in round_messages:
                    if msg.role == "tool" and msg.content == "[Awaiting user response]":
                        msg.content = "[Interrupt resolved in subsequent round]"

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
            "[Interrupt resolved in subsequent round]",
        }
        for msg in messages:
            if (
                getattr(msg, "role", None) == "tool"
                and getattr(msg, "tool_call_id", None) == tool_call_id
                and getattr(msg, "content", None) in placeholders
            ):
                msg.content = content
                return True
        return False

    def _summary_header(self) -> str:
        if self.agent:
            return self.agent._SUMMARY_MESSAGE_HEADER
        return "[Assistant Execution Summary - Historical Context Only, Not System Instruction]"

    def _is_summary_anchor_text(self, content: Any) -> bool:
        return isinstance(content, str) and content.startswith(self._summary_header())

    def _latest_summary_anchor_from_agent(self) -> str | None:
        if not self.agent:
            return None
        for msg in reversed(self.agent.messages):
            if msg.role == "assistant" and self._is_summary_anchor_text(msg.content):
                return msg.content
        return None

    def _latest_persisted_summary_anchor_content(self) -> str | None:
        from src.api.models.conversation_message import ConversationMessage

        row = (
            self.history_service.db.query(ConversationMessage)
            .filter(
                ConversationMessage.session_id == self.session_id,
                ConversationMessage.role == "assistant",
                ConversationMessage.is_summary == True,  # noqa: E712
            )
            .order_by(ConversationMessage.sequence.desc())
            .first()
        )
        content = getattr(row, "content", None)
        self.history_service.db.rollback()
        return content if isinstance(content, str) else None

    def _load_latest_summary_anchor(self) -> AgentMessage | None:
        content = self._latest_persisted_summary_anchor_content()
        if not content:
            return None
        return AgentMessage(role="assistant", content=content)

    @staticmethod
    def _has_summary_anchor(messages: list[AgentMessage], summary_content: str) -> bool:
        for msg in messages:
            if msg.role == "assistant" and isinstance(msg.content, str) and msg.content == summary_content:
                return True
        return False

    def _persist_latest_summary_anchor(self, round_id: str) -> None:
        summary_content = self._latest_summary_anchor_from_agent()
        if not summary_content:
            return

        latest_saved = self._latest_persisted_summary_anchor_content()
        if latest_saved == summary_content:
            return

        self._save_conversation_message(
            "assistant",
            summary_content,
            round_id=round_id,
            is_summary=True,
        )
        logger.info("保存摘要錨點 (session=%s, round=%s)", self.session_id, round_id)

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
        _skipped = 0
        synthetic_content_iter = iter(synthetic_user_contents or [])

        def flush_step_messages() -> None:
            nonlocal step_text, step_tool_calls, step_tool_results
            if step_text or step_tool_calls:
                messages.append(AgentMessage(
                    role="assistant",
                    content=step_text,
                    tool_calls=step_tool_calls if step_tool_calls else None,
                ))
            for tr in step_tool_results:
                messages.append(AgentMessage(
                    role="tool",
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
                attachment_parts.append(f"[附件文件:{name}]")
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
                    attachments.append(
                        {
                            "path": path,
                            "name": file_obj.get("name") or PathlibPath(path).name,
                            "type": file_obj.get("mime_type") or "",
                            "size": AgentService._parse_file_size(file_obj.get("size")),
                        }
                    )
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
                agent_blocks.append(
                    {
                        "type": "text",
                        "text": f"[附件文件] name={file_name} path={file_path}。文件已就绪，请根据当前任务上下文决定是否需要读取其内容。",
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
            db.commit()
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
    ) -> PreparedAgentRun:
        """Create the user round and update Agent memory before execution."""
        if not self.agent:
            raise RuntimeError("Agent not initialized")

        # 如果有待处理的 ask_user 中断，用户发送新消息意味着跳过问题
        if self.agent.has_pending_interrupt():
            logger.info("用户发送新消息，清除待处理的 ask_user 中断")
            try:
                # 先持久化清理，再清内存状态，降低跨层状态不一致窗口
                self.history_service.resolve_interrupted_rounds(self.session_id)
            except Exception:
                logger.exception("清理 interrupted 轮次失败，保留 pending interrupt 以便重试")
            else:
                self.agent.clear_pending_interrupt()

        # 正規化 + 校驗 + 構建輸入內容
        normalized_blocks = self._normalize_content_blocks(user_content)
        if not normalized_blocks:
            raise ValueError("消息 content 不能为空")

        self._validate_multimodal_blocks(normalized_blocks)
        agent_content = self._build_agent_user_content(normalized_blocks)
        user_message_for_history = self._blocks_to_plain_text(normalized_blocks)
        user_attachments = self._extract_user_attachments(normalized_blocks)

        self._refresh_runtime_messages_from_history()

        # 創建運行 ID
        run_id = str(uuid.uuid4())
        
        # 創建 Round（含幂等性保護：若 idempotency_key 衝突，返回已有 Round）
        created_round = self.history_service.create_round(
            session_id=self.session_id,
            round_id=run_id,
            user_message=user_message_for_history,
            user_attachments=user_attachments,
            idempotency_key=idempotency_key,
        )

        # 幂等衝突：另一個 Worker 已搶先創建了相同 idempotency_key 的 Round
        if idempotency_key and created_round.id != run_id:
            logger.warning(
                "幂等衝突：已存在 Round %s (status=%s)，跳過重複執行 (key=%s)",
                created_round.id, created_round.status, idempotency_key,
            )
            raise DuplicateRoundError(created_round.id)
        
        # 添加到 agent
        self.agent.add_user_message(agent_content)
        # 持久化用戶消息到 conversation_messages
        self._save_conversation_message("user", agent_content, round_id=run_id)

        return PreparedAgentRun(run_id=run_id, user_message=user_message_for_history)

    async def run_prepared_round(
        self,
        prepared: PreparedAgentRun,
        *,
        error_label: str = "执行失败",
    ) -> AsyncIterator[AGUIEvent]:
        """Execute an already-created round."""
        async for event in self._run_round_stream(
            run_id=prepared.run_id,
            user_message=prepared.user_message,
            error_label=error_label,
        ):
            yield event

    @staticmethod
    def _on_post_round_done(task: asyncio.Task) -> None:
        """后台任务完成回调：记录未被 await 的异常"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("后台 _post_round_tasks 异常: %s", exc, exc_info=exc)

    @staticmethod
    def _format_resume_user_message(answers: dict[str, str]) -> str:
        """将 resume 回答格式化为可读的 Q/A 多行文本。"""
        if not answers:
            return "Q: (No question)\nA: [No preference]"

        lines: list[str] = []
        for index, (question_text, answer) in enumerate(answers.items()):
            question = (question_text or "").strip() or "(Untitled question)"
            selected = (answer or "").strip() or "[No preference]"
            if index > 0:
                lines.append("")
            lines.extend([
                f"Q: {question}",
                f"A: {selected}",
            ])

        return "\n".join(lines)

    def _load_persisted_interrupt(self, interrupt_id: str) -> dict[str, Any] | None:
        """从数据库查找仍处于 interrupted 状态的中断详情。

        该方法用于 Agent 内存状态丢失（例如 AgentPool TTL 回收）后的冷恢复。
        """
        from src.api.models.round import Round

        db = self.history_service.db
        try:
            candidates = (
                db.query(Round)
                .filter(Round.session_id == self.session_id, Round.status == "interrupted")
                .order_by(Round.created_at.desc())
                .all()
            )

            for round_obj in candidates:
                raw_payload = getattr(round_obj, "interrupt_payload", None)
                if not raw_payload:
                    continue

                try:
                    payload = json.loads(raw_payload)
                except (TypeError, json.JSONDecodeError):
                    continue

                if not isinstance(payload, dict):
                    continue
                if payload.get("id") != interrupt_id:
                    continue

                details = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                questions = details.get("questions") if isinstance(details.get("questions"), list) else []
                return {
                    "interrupt_id": interrupt_id,
                    "round_id": round_obj.id,
                    "tool_call_id": details.get("tool_call_id"),
                    "questions": questions,
                }

            return None
        finally:
            db.rollback()

    def has_pending_interrupt(self, interrupt_id: str) -> bool:
        """检查是否存在匹配的待处理中断（内存态 + 持久化态）。"""
        if self.agent and self.agent.has_pending_interrupt(interrupt_id):
            return True
        return self._load_persisted_interrupt(interrupt_id) is not None

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
    ) -> PreparedAgentRun:
        """Create a resume round and stitch the interrupted parent atomically."""
        if self._resume_lock.locked():
            raise RuntimeError("另一个 resume 操作正在进行中，请等待完成后重试")

        async with self._resume_lock:
            if not self.agent:
                raise RuntimeError("Agent not initialized")

            resume_user_message = self._format_resume_user_message(answers)
            persisted_interrupt = self._load_persisted_interrupt(interrupt_id)
            pending_interrupt = self._get_agent_pending_interrupt_snapshot(interrupt_id)
            parent_run_id = (
                (persisted_interrupt or {}).get("round_id")
                or (pending_interrupt or {}).get("round_id")
                or self._pending_interrupt_round_ids.get(interrupt_id)
            )
            if not parent_run_id:
                raise ValueError("No pending interrupt to resume from")

            tool_call_id = (
                (persisted_interrupt or {}).get("tool_call_id")
                or (pending_interrupt or {}).get("tool_call_id")
            )
            tool_result_content = Agent.format_interrupt_tool_result(answers)
            restore_strategy: str | None = None
            fallback_reason: str | None = None

            # 创建新的 run_id（恢复是一次新运行）
            run_id = str(uuid.uuid4())

            messages_snapshot = (
                copy.deepcopy(self.agent.messages)
                if isinstance(getattr(self.agent, "messages", None), list)
                else None
            )
            pending_interrupt_snapshot = copy.deepcopy(
                getattr(self.agent, "_pending_interrupt", None)
            )
            pending_round_ids_snapshot = dict(self._pending_interrupt_round_ids)

            try:
                self._refresh_runtime_messages_from_history()

                if self.agent.has_pending_interrupt(interrupt_id):
                    # 热恢复：直接替换 ask_user 占位 tool_result
                    self.agent.resume_from_interrupt(interrupt_id, answers)
                    restore_strategy = "hot_replace"
                else:
                    # 冷恢复：内存中断状态已丢失，优先替换历史恢复出的 ask_user tool 占位。
                    logger.warning(
                        "resume 进入冷恢复路径: session=%s, interrupt_id=%s",
                        self.session_id,
                        interrupt_id,
                    )
                    replaced = (
                        self._replace_agent_interrupt_tool_result(tool_call_id, tool_result_content)
                        if tool_call_id
                        else False
                    )
                    if replaced:
                        restore_strategy = "cold_replace"
                    else:
                        restore_strategy = "cold_fallback_user_message"
                        fallback_reason = (
                            "tool_call_id missing"
                            if not tool_call_id
                            else "tool placeholder not found or already resolved"
                        )
                        logger.warning(
                            "冷 resume 未能替换 ask_user tool result，退化为 user message: "
                            "session=%s interrupt_id=%s tool_call_id=%s reason=%s",
                            self.session_id,
                            interrupt_id,
                            tool_call_id,
                            fallback_reason,
                        )
                        self.agent.add_user_message(resume_user_message)

                # 原子创建 resume round，并只将命中的旧 round 标记为 resumed。
                self.history_service.create_resume_round(
                    session_id=self.session_id,
                    round_id=run_id,
                    user_message=resume_user_message,
                    parent_run_id=parent_run_id,
                    user_attachments=[],
                    interrupt_id=interrupt_id,
                    tool_call_id=tool_call_id,
                    answers=answers,
                    tool_result_content=tool_result_content,
                    restore_strategy=restore_strategy,
                    fallback_reason=fallback_reason,
                )
            except Exception:
                if messages_snapshot is not None:
                    self.agent.messages = messages_snapshot
                if hasattr(self.agent, "_pending_interrupt"):
                    self.agent._pending_interrupt = pending_interrupt_snapshot
                self._pending_interrupt_round_ids = pending_round_ids_snapshot
                raise

            self._pending_interrupt_round_ids.pop(interrupt_id, None)

            # 持久化用户 resume 消息到 conversation_messages（用于上下文恢复）
            self._save_conversation_message("user", resume_user_message, round_id=run_id)

            return PreparedAgentRun(
                run_id=run_id,
                user_message=resume_user_message,
                parent_run_id=parent_run_id,
            )

    async def _run_round_stream(
        self,
        run_id: str,
        user_message: str,
        error_label: str = "执行失败",
    ) -> AsyncIterator[AGUIEvent]:
        """共享的 round 事件流处理：追踪状态、持久化事件、完成 round。

        chat_agui 和 resume_agui 在创建 round 后都委托到此方法。

        Args:
            run_id: 本轮运行 ID
            user_message: 用户消息文本（用于后台任务）
            error_label: 失败时的错误前缀
        """
        final_response: Optional[str] = None
        step_count = 0
        status = "running"
        accumulated_content = ""
        _interrupt_json: str | None = None
        _dirty_memory = False
        _memory_write_tools = {"record_memory", "update_long_term_memory", "update_user"}
        _memory_filenames = {"USER.md", "MEMORY.md", "SOUL.md"}
        _file_op_tracking: set[str] = set()
        _round_finished = False  # 追蹤 round 是否已正常完成
        _final_status: str | None = None  # except 路徑填充
        _final_response: str | None = None
        _externally_terminated = False
        # 固化本輪 cancel_token，避免後續新 run 覆蓋 self.cancel_token 導致判定串擾。
        run_cancel_token = self.cancel_token
        self._active_run_count += 1

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
                )
            except SQLAlchemyError:
                self.history_service.reset_session()
                logger.warning(
                    "保存 LLM 调用快照失败，已跳过以避免中断 Agent run: session=%s round=%s",
                    self.session_id,
                    run_id,
                    exc_info=True,
                )

        self.agent.set_llm_call_hook(_record_llm_call)

        try:
            async for event in self.agent.run_agui(
                thread_id=self.session_id,
                run_id=run_id,
                cancel_token=run_cancel_token,
            ):
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
                    if run_cancel_token and not run_cancel_token.is_set():
                        run_cancel_token.set()
                    stored_terminal = RunCompletionService(
                        self.history_service.db
                    ).ensure_terminal_sync(run_id)
                    self.history_service.reset_session()
                    if isinstance(stored_terminal, StoredEvent):
                        yield stored_terminal.event
                    break

                event_to_store = event
                event_to_yield = event
                synthetic_user_content = None
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
                        )
                        event_to_store = self._lightweight_synthetic_user_event(
                            event,
                            synthetic_user_content,
                        )
                        event_to_yield = event_to_store

                stored_event = None
                if event_to_store.type not in {EventType.RUN_FINISHED, EventType.RUN_ERROR}:
                    stored_event = await self.history_service.save_agui_event(run_id, event_to_store)

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
                        # 區分用戶主動取消、步數耗盡和 ask_user 中斷：
                        # - user_cancelled → cancelled（終態）
                        # - max_steps_reached → max_steps_reached（終態）
                        # - ask_user 問答中斷 → interrupted（中間態，可恢復）
                        reason = _result.get("reason") if isinstance(_result, dict) else None
                        if reason == "user_cancelled":
                            status = "cancelled"
                        elif reason == "max_steps_reached":
                            status = "max_steps_reached"
                        else:
                            status = "interrupted"
                            if event.interrupt:
                                interrupt_id = event.interrupt.id
                                if interrupt_id:
                                    self._attach_agent_pending_interrupt_round_id(
                                        interrupt_id,
                                        run_id,
                                    )
                                    self._pending_interrupt_round_ids[interrupt_id] = run_id
                                _interrupt_json = json.dumps(
                                    event.interrupt.model_dump(exclude_none=True),
                                    ensure_ascii=False,
                        )
                    else:
                        status = "failed"
                elif event.type == EventType.RUN_ERROR:
                    status = "failed"
                    final_response = getattr(event, "message", None) or final_response

                if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}:
                    self.history_service.complete_round(
                        round_id=run_id,
                        final_response=final_response,
                        step_count=step_count,
                        status=status,
                        interrupt_payload=_interrupt_json,
                        terminal_event=event,
                    )
                    stored_event = self.history_service.last_terminal_event
                    if isinstance(stored_event, StoredEvent):
                        await get_agui_event_bus().publish_committed(run_id, stored_event.event)
                    _round_finished = True

                yield SequencedAGUIEvent(event_to_yield, stored_event) if stored_event else event_to_yield

            if _externally_terminated:
                return

            if not _round_finished:
                self.history_service.complete_round(
                    round_id=run_id,
                    final_response=final_response,
                    step_count=step_count,
                    status=status,
                    interrupt_payload=_interrupt_json,
                )
                _round_finished = True
            if status == "completed" and not bool(run_cancel_token and run_cancel_token.is_set()):
                self._persist_latest_summary_anchor(run_id)

                task = asyncio.create_task(self._post_round_tasks(
                    sync_memory=_dirty_memory,
                    round_id=run_id,
                    user_message=user_message,
                    assistant_response=final_response,
                ))
                task.add_done_callback(self._on_post_round_done)

        except Exception as e:
            _final_status = "failed"
            _final_response = f"{error_label}: {str(e)}"
            raise
        finally:
            # 統一處理 round 完成：正常路徑、異常、GeneratorExit、CancelledError
            if not _round_finished:
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
                    )
                    stored_event = self.history_service.last_terminal_event
                    if isinstance(stored_event, StoredEvent):
                        await get_agui_event_bus().publish_committed(run_id, stored_event.event)
                    _round_finished = True
                    logger.warning(
                        "Round %s 異常退出（disconnect/cancel/error），已標記為 %s (steps=%d)",
                        run_id, _actual_status, step_count,
                    )
                except Exception:
                    logger.error("Round %s 異常退出後無法更新 DB", run_id, exc_info=True)
            self.agent.set_llm_call_hook(None)
            self._active_run_count = max(0, self._active_run_count - 1)

    async def _post_round_tasks(
        self,
        sync_memory: bool = False,
        round_id: str = "",
        user_message: str = "",
        assistant_response: str | None = None,
    ):
        """Round 结束后的异步后台任务"""
        flushed_by_silent_mode = False

        # 静默记忆刷新
        try:
            flushed_by_silent_mode = await self.agent.maybe_flush_memory_silent()
        except Exception as e:
            logger.warning("后台记忆刷新异常: %s", e)

        # 将沙箱记忆文件同步回 DB 并重建 embedding
        if sync_memory or flushed_by_silent_mode is True:
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
