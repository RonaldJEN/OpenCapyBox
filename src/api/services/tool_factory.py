"""共享工具工厂 — 统一创建 Agent 工具列表

聊天 Agent 和 Cron Agent 共享同一套工具集，
区别仅在于 Cron Agent 排除 AskUserQuestionTool（无人交互场景）。
"""

import logging
import posixpath
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Callable, Set

from opensandbox import Sandbox

from src.agent.tools.sandbox_file_tools import (
    SandboxReadTool,
    SandboxReadImageTool,
    SandboxWriteTool,
    SandboxEditTool,
)
from src.agent.tools.sandbox_bash_tool import (
    SandboxBashTool,
    SandboxBashOutputTool,
    SandboxBashKillTool,
    _BackgroundCommandTracker,
)
from src.agent.tools.sandbox_note_tool import SandboxSessionNoteTool, SandboxRecallNoteTool
from src.agent.tools.memory_tools import (
    UpdateLongTermMemoryTool,
    SearchMemoryTool,
    ReadUserProfileTool,
    UpdateUserProfileTool,
)
from src.agent.tools.cron_tool import ManageCronTool
from src.agent.tools.ask_user_tool import AskUserQuestionTool
from src.agent.tools.sub_agent_tool import SubAgentTool
from src.agent.tools.skill_loader import Skill, SkillLoader
from src.agent.tools.skill_tool import GetSkillTool
from src.agent.tools.mcp_tool import McpRemoteTool
from src.agent.tools.workspace_tools import (
    WorkspaceCreateDirectoryTool,
    WorkspaceListTool,
    WorkspaceMoveTool,
    WorkspacePublishTool,
    WorkspaceStageTool,
    WorkspaceDeleteTool,
)

from src.api.services.sandbox_service import get_sandbox_service
from src.api.services.skill_inventory_service import (
    SkillInventoryIdentity,
    cached_sandbox_identity,
    inventory_view_is_current_winner,
    load_user_skill_inventory,
    normalize_user_skill_inventory,
    persist_user_skill_inventory,
)
from src.api.config import get_settings
from src.api.utils.timezone import now_naive
from src.api.services.mcp_runtime import (
    McpRequiredServerUnavailable,
    McpToolNameCollisionError,
    get_mcp_runtime,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _build_agent_config_sync(
    *,
    user_id: str,
    db_session_factory: Callable,
    mount: str,
):
    async def _sync(path: str, content: str) -> None:
        from src.api.services.memory_service import (
            MemoryService,
            get_agent_config_file_type_for_path,
        )

        file_type = get_agent_config_file_type_for_path(path, mount)
        if not file_type:
            return

        db = db_session_factory()
        try:
            svc = MemoryService(db)
            await svc.sync_agent_config_content(user_id, file_type, content)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()

    return _sync


def _auto_locate_skills_dir(setting_value: str) -> Path:
    if setting_value:
        return Path(setting_value).resolve()
    return (Path(__file__).parent.parent.parent / "agent" / "skills").resolve()


def _replace_sandbox_skill_infos(skill_loader: SkillLoader, sandbox_skill_infos: list[dict]) -> None:
    canonical_infos = normalize_user_skill_inventory(sandbox_skill_infos)
    replacement = [
        Skill(
            name=info["name"],
            description=info["description"],
            content="",
            metadata={"display_name": info["display_name"]},
            source="user",
            sandbox_skill_dir=info["sandbox_skill_dir"],
        )
        for info in canonical_infos
    ]
    skill_loader.replace_sandbox_skills(replacement)


def _load_matching_inventory_winner(
    db_session_factory: Callable,
    *,
    user_id: str,
    identity: SkillInventoryIdentity,
    observed_at: datetime,
) -> list[dict] | None:
    """Load the winning complete snapshot after a publish CAS loss."""

    db = db_session_factory()
    try:
        view = load_user_skill_inventory(db, user_id=user_id)
        if not inventory_view_is_current_winner(
            view,
            identity=identity,
            observed_at=observed_at,
        ):
            return None
        return view.skills
    finally:
        db.rollback()
        db.close()


async def create_agent_tools(
    *,
    sandbox: Sandbox,
    workspace_dir: str,
    mount: str,
    user_id: str,
    db_session_factory: Callable,
    subagent_runner: Callable | None = None,
    exclude: Optional[Set[str]] = None,
    supports_image: bool = False,
    max_images: int = 0,
    build_metadata: dict[str, object] | None = None,
    workspace_access: str = "manage",
    workspace_actor: str = "chat",
    workspace_fence: Callable[[object], None] | None = None,
    workspace_change_recorder: Callable[[object, dict], None] | None = None,
    workspace_context: dict | None = None,
) -> tuple[List, Optional[SkillLoader]]:
    """创建标准 Agent 工具列表。

    Parameters
    ----------
    exclude : set of tool class names to skip, e.g. {"AskUserQuestionTool"}

    Returns
    -------
    (tools, skill_loader)  skill_loader 为 None 当 Skills 未加载时
    """
    exclude = set(exclude or ())
    if workspace_access not in {"none", "read", "edit", "manage"}:
        raise ValueError("workspace_access 必须为 none/read/edit/manage")
    skill_loader_ref: Optional[SkillLoader] = None
    if build_metadata is not None:
        build_metadata["mcp_catalog_fingerprint"] = None
        build_metadata["mcp_catalog_configuration_fingerprint"] = None
        build_metadata["mcp_catalog_retry_required"] = False
        build_metadata["mcp_connections"] = ()

    bg_tracker = _BackgroundCommandTracker()
    agent_config_sync = _build_agent_config_sync(
        user_id=user_id,
        db_session_factory=db_session_factory,
        mount=mount,
    )
    read_only_paths = {posixpath.join(mount, "AGENTS.md")}

    # 全量候选工具（类名 -> 工厂函数），延迟构造：只有不在 exclude 中的才会被实例化
    _candidates: List[tuple[str, Callable[[], object]]] = [
        # 沙箱文件工具
        ("SandboxReadTool", lambda: SandboxReadTool(
            sandbox=sandbox,
            workspace_dir=workspace_dir,
        )),
        ("SandboxReadImageTool", lambda: SandboxReadImageTool(
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            supports_image=supports_image,
            max_images=max_images,
        )),
        ("SandboxWriteTool", lambda: SandboxWriteTool(
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            agent_config_sync=agent_config_sync,
            read_only_paths=read_only_paths,
        )),
        ("SandboxEditTool", lambda: SandboxEditTool(
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            agent_config_sync=agent_config_sync,
            read_only_paths=read_only_paths,
        )),
        # 沙箱 Bash 工具（共享 tracker）
        ("SandboxBashTool", lambda: SandboxBashTool(
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            tracker=bg_tracker,
            background_timeout_seconds=settings.sandbox_background_command_timeout_seconds,
        )),
        ("SandboxBashOutputTool", lambda: SandboxBashOutputTool(tracker=bg_tracker)),
        ("SandboxBashKillTool", lambda: SandboxBashKillTool(tracker=bg_tracker)),
        # 沙箱会话笔记工具
        ("SandboxSessionNoteTool", lambda: SandboxSessionNoteTool(sandbox=sandbox)),
        ("SandboxRecallNoteTool", lambda: SandboxRecallNoteTool(sandbox=sandbox)),
        # 分层记忆工具
        ("UpdateLongTermMemoryTool", lambda: UpdateLongTermMemoryTool(
            sandbox=sandbox,
            workspace_dir=mount,
            agent_config_sync=agent_config_sync,
        )),
        ("SearchMemoryTool", lambda: SearchMemoryTool(db_session_factory=db_session_factory, user_id=user_id)),
        ("ReadUserProfileTool", lambda: ReadUserProfileTool(sandbox=sandbox, workspace_dir=mount)),
        ("UpdateUserProfileTool", lambda: UpdateUserProfileTool(
            sandbox=sandbox,
            workspace_dir=mount,
            agent_config_sync=agent_config_sync,
        )),
        # Cron 定时任务管理工具
        ("ManageCronTool", lambda: ManageCronTool(
            db_session_factory=db_session_factory,
            user_id=user_id,
        )),
        # 用户交互工具（Human-in-the-Loop）
        ("AskUserQuestionTool", lambda: AskUserQuestionTool()),
        # 子 Agent 委托工具（服务层 runner 创建 child Round）
        ("SubAgentTool", lambda: SubAgentTool(runner=subagent_runner)),
    ]

    workspace_tool_kwargs = {
        "db_session_factory": db_session_factory,
        "user_id": user_id,
        "execution_root": workspace_dir,
        "sandbox": sandbox,
        "actor": workspace_actor,
        "fence": workspace_fence,
        "change_recorder": workspace_change_recorder,
        "base_context": workspace_context,
    }
    if workspace_access in {"read", "edit", "manage"}:
        _candidates.extend([
            ("WorkspaceListTool", lambda: WorkspaceListTool(**workspace_tool_kwargs)),
            ("WorkspaceStageTool", lambda: WorkspaceStageTool(**workspace_tool_kwargs)),
        ])
    if workspace_access in {"edit", "manage"}:
        _candidates.extend([
            ("WorkspacePublishTool", lambda: WorkspacePublishTool(**workspace_tool_kwargs)),
            (
                "WorkspaceCreateDirectoryTool",
                lambda: WorkspaceCreateDirectoryTool(**workspace_tool_kwargs),
            ),
        ])
    if workspace_access == "manage":
        _candidates.extend([
            ("WorkspaceMoveTool", lambda: WorkspaceMoveTool(**workspace_tool_kwargs)),
            ("WorkspaceDeleteTool", lambda: WorkspaceDeleteTool(**workspace_tool_kwargs)),
        ])

    tools: List = [factory() for cls_name, factory in _candidates if cls_name not in exclude]

    if exclude:
        known = {cls_name for cls_name, _ in _candidates}
        unknown = exclude - known
        if unknown:
            logger.warning("create_agent_tools: exclude 中有未知工具类: %s", unknown)

    # 搜索工具（条件加载）
    bocha_appcode = settings.bocha_search_appcode
    if bocha_appcode and bocha_appcode.strip():
        try:
            from src.agent.tools.glm_search_tool import GLMSearchTool, GLMBatchSearchTool
            tools.append(GLMSearchTool(api_key=bocha_appcode))
            tools.append(GLMBatchSearchTool(api_key=bocha_appcode))
            logger.info("已加载 Bocha 搜索工具")
        except Exception as e:
            logger.warning("Bocha 搜索工具加载失败: %s", e)
    else:
        logger.info("未配置 BOCHA_SEARCH_APPCODE，跳过搜索工具")

    # Skills（复杂加载流程）
    skills_dir = _auto_locate_skills_dir(settings.skills_dir)
    try:
        if skills_dir.exists():
            skill_loader = SkillLoader(
                str(skills_dir),
                disabled_cache_ttl_seconds=settings.skill_disabled_cache_ttl_seconds,
            )
            skill_loader.discover_skills()

            def _load_disabled_skills() -> set[str]:
                from src.api.models.user_memory import UserSkillConfig
                db = db_session_factory()
                try:
                    return {
                        r.skill_name for r in
                        db.query(UserSkillConfig)
                        .filter(
                            UserSkillConfig.user_id == user_id,
                            UserSkillConfig.enabled == False,  # noqa: E712
                        )
                        .all()
                    }
                finally:
                    db.close()

            # 保留完整技能清单，仅在运行时按 DB 配置过滤。
            try:
                skill_loader.set_disabled_skills_provider(_load_disabled_skills)
                disabled_skills = skill_loader.refresh_disabled_skills()
                if disabled_skills:
                    logger.info("已按用户配置禁用 %d 个 Skills: %s", len(disabled_skills), disabled_skills)
            except Exception as e:
                logger.warning("查询 UserSkillConfig 失败，加载全部 Skills: %s", e)

            # 发现沙箱中用户自行安装的第三方 Skill
            try:
                sandbox_service = get_sandbox_service()
                official_names = set(skill_loader.loaded_skills.keys())
                inventory_identity = cached_sandbox_identity(sandbox_service, user_id)
                if inventory_identity is None:
                    raise RuntimeError("沙箱缓存缺少完整代际指纹")
                inventory_observed_at = now_naive()
                discovery_result = await sandbox_service.discover_sandbox_skills(
                    user_id,
                    official_names,
                    strict=True,
                )
                sandbox_skill_infos = normalize_user_skill_inventory(discovery_result)
                if cached_sandbox_identity(sandbox_service, user_id) != inventory_identity:
                    raise RuntimeError("扫描期间沙箱代际发生变化")
                published = persist_user_skill_inventory(
                    db_session_factory,
                    user_id=user_id,
                    identity=inventory_identity,
                    skills=sandbox_skill_infos,
                    issues=getattr(discovery_result, "issues", []),
                    observed_at=inventory_observed_at,
                )
                registry_skill_infos = sandbox_skill_infos
                if not published:
                    registry_skill_infos = _load_matching_inventory_winner(
                        db_session_factory,
                        user_id=user_id,
                        identity=inventory_identity,
                        observed_at=inventory_observed_at,
                    )
                    if registry_skill_infos is None:
                        raise RuntimeError("扫描结果已过期，且无同代际胜出快照")
                _replace_sandbox_skill_infos(skill_loader, registry_skill_infos)
                if registry_skill_infos:
                    logger.info(
                        "已发现 %d 个用户沙箱 Skills: %s",
                        len(registry_skill_infos),
                        [i["name"] for i in registry_skill_infos],
                    )
            except Exception as e:
                logger.warning("沙箱 Skill 发现失败（不影响官方 Skills）: %s", e)

            async def _ensure_skill_ready(skill_name: str) -> bool:
                # On-demand skill loading needs strong consistency, so bypass the
                # metadata TTL cache and re-read the latest logical state here.
                try:
                    skill_loader.refresh_disabled_skills(force=True)
                except Exception as e:
                    logger.warning("刷新 Skill 启停配置失败: %s", e)
                if not skill_loader.is_skill_enabled(skill_name):
                    return False
                skill = skill_loader.get_skill(skill_name)
                if skill is None:
                    # Let GetSkillTool refresh the sandbox index before retrying;
                    # unknown names must not allocate permanent per-skill locks or
                    # scan the complete official skill tree.
                    return False
                if skill.source == "user":
                    return True
                svc = get_sandbox_service()

                def _is_still_enabled() -> bool:
                    try:
                        skill_loader.refresh_disabled_skills(force=True)
                    except Exception as e:
                        logger.warning("推送前刷新 Skill 启停配置失败: %s", e)
                        # Preserve the last-known snapshot. An initial failure
                        # therefore follows the documented load-all fallback.
                    return skill_loader.is_skill_enabled(skill_name)

                return await svc.push_skill(
                    user_id,
                    str(skills_dir),
                    skill_name,
                    enabled_check=_is_still_enabled,
                )

            async def _refresh_sandbox_skills() -> None:
                svc = get_sandbox_service()
                official_names = set(skill_loader.loaded_skills.keys())
                try:
                    inventory_identity = cached_sandbox_identity(svc, user_id)
                    if inventory_identity is None:
                        raise RuntimeError("沙箱缓存缺少完整代际指纹")
                    inventory_observed_at = now_naive()
                    discovery_result = await svc.discover_sandbox_skills(
                        user_id,
                        official_names,
                        strict=True,
                    )
                    sandbox_skill_infos = normalize_user_skill_inventory(discovery_result)
                    if cached_sandbox_identity(svc, user_id) != inventory_identity:
                        raise RuntimeError("扫描期间沙箱代际发生变化")
                    published = persist_user_skill_inventory(
                        db_session_factory,
                        user_id=user_id,
                        identity=inventory_identity,
                        skills=sandbox_skill_infos,
                        issues=getattr(discovery_result, "issues", []),
                        observed_at=inventory_observed_at,
                    )
                    registry_skill_infos = sandbox_skill_infos
                    if not published:
                        registry_skill_infos = _load_matching_inventory_winner(
                            db_session_factory,
                            user_id=user_id,
                            identity=inventory_identity,
                            observed_at=inventory_observed_at,
                        )
                        if registry_skill_infos is None:
                            raise RuntimeError("扫描结果已过期，且无同代际胜出快照")
                except Exception:
                    logger.warning(
                        "刷新用户沙箱 Skill 清单失败，保留现有 Registry "
                        "(user=%s)",
                        user_id,
                        exc_info=True,
                    )
                    return
                before_names = set(skill_loader.sandbox_skills.keys())
                _replace_sandbox_skill_infos(skill_loader, registry_skill_infos)
                current_names = set(skill_loader.sandbox_skills.keys())
                new_names = current_names - before_names
                removed_names = before_names - current_names
                if new_names:
                    logger.info("get_skill miss 后刷新发现用户沙箱 Skills: %s", sorted(new_names))
                if removed_names:
                    logger.info("刷新后移除已卸载用户沙箱 Skills: %s", sorted(removed_names))

            skill_loader.set_inventory_refresher(_refresh_sandbox_skills)

            async def _read_sandbox_skill(skill_name: str) -> str | None:
                # Reads must reflect the live enable state so a mid-read disable
                # discards content; force past the metadata TTL cache.
                try:
                    skill_loader.refresh_disabled_skills(force=True)
                except Exception as e:
                    logger.warning("读取前刷新 Skill 启停配置失败，沿用上次状态: %s", e)
                skill = skill_loader.get_skill(skill_name)
                if not skill or skill.source != "user" or not skill.sandbox_skill_dir:
                    return None
                svc = get_sandbox_service()
                content = await svc.read_sandbox_skill_content(
                    user_id,
                    skill.sandbox_skill_dir,
                )
                try:
                    skill_loader.refresh_disabled_skills(force=True)
                except Exception as e:
                    logger.warning("读取后刷新 Skill 启停配置失败，沿用上次状态: %s", e)
                if not skill_loader.is_skill_enabled(skill_name):
                    logger.info(
                        "用户 Skill 读取期间被禁用，丢弃内容 "
                        "(user=%s, skill=%s)",
                        user_id,
                        skill_name,
                    )
                    return None
                return content

            tools.append(GetSkillTool(
                skill_loader,
                ensure_skill_ready=_ensure_skill_ready,
                read_sandbox_skill=_read_sandbox_skill,
                refresh_sandbox_skills=_refresh_sandbox_skills,
            ))
            skill_loader_ref = skill_loader
            skill_count = len(skill_loader.list_skills())
            logger.info(
                "已加载 %d 个 Skills（官方 %d + 用户 %d）",
                skill_count,
                len(skill_loader.loaded_skills),
                len(skill_loader.sandbox_skills),
            )
        else:
            logger.warning("Skills 目录不存在: %s", skills_dir)
    except Exception as e:
        logger.warning("Skills 加载失败: %s", e)

    # Database-backed Streamable HTTP MCP tools. Each Agent receives an
    # immutable discovery snapshot, but McpRemoteTool marks these schemas as
    # DEFERRED; Agent injects the small mcp_tool_search gateway instead of
    # sending the full remote catalog on the initial model request. Every
    # actual call is still re-authorized against live connection and policy
    # state.
    mcp_runtime = get_mcp_runtime()
    try:
        mcp_catalog = await mcp_runtime.resolve_catalog(user_id)
    except McpRequiredServerUnavailable:
        # A required official integration is part of the Agent contract.
        raise
    except Exception as exc:
        # Catalog/database rollout must not make the existing built-in Agent
        # unavailable. Individual server failures are already isolated inside
        # resolve_catalog; this protects installations that predate MCP tables.
        logger.warning("MCP 目录加载失败，跳过远程工具: user=%s error=%s", user_id, exc)
        if build_metadata is not None:
            build_metadata["mcp_catalog_retry_required"] = True
    else:
        if build_metadata is not None:
            build_metadata["mcp_catalog_fingerprint"] = mcp_catalog.fingerprint
            build_metadata["mcp_catalog_configuration_fingerprint"] = (
                getattr(mcp_catalog, "configuration_fingerprint", None)
                or mcp_catalog.fingerprint
            )
            # Optional failures are cached as a partial catalog for this exact
            # config/refresh fingerprint. Rebuilding every chat would hammer an
            # offline server; the next refresh bucket or config version retries.
            build_metadata["mcp_catalog_retry_required"] = False
            build_metadata["mcp_connections"] = tuple(
                getattr(mcp_catalog, "connections", ()) or ()
            )
        existing_names = {tool.name for tool in tools}
        for snapshot in mcp_catalog.tools:
            if snapshot.model_name in existing_names:
                raise McpToolNameCollisionError(
                    f"MCP model tool name conflicts with an existing tool: {snapshot.model_name}"
                )
            tools.append(McpRemoteTool(
                user_id=user_id,
                snapshot=snapshot,
                runtime=mcp_runtime,
            ))
            existing_names.add(snapshot.model_name)
        for error in mcp_catalog.errors:
            logger.warning("MCP 服务发现失败（已隔离）: user=%s %s", user_id, error)

    return tools, skill_loader_ref
