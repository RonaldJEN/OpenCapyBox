"""共享工具工厂 — 统一创建 Agent 工具列表

聊天 Agent 和 Cron Agent 共享同一套工具集，
区别仅在于 Cron Agent 排除 AskUserQuestionTool（无人交互场景）。
"""

import logging
import posixpath
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
    RecordDailyLogTool,
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

from src.api.services.sandbox_service import get_sandbox_service
from src.api.config import get_settings

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


def _register_sandbox_skill_infos(skill_loader: SkillLoader, sandbox_skill_infos: list[dict]) -> None:
    for info in sandbox_skill_infos:
        user_skill = Skill(
            name=info["name"],
            description=info["description"],
            content="",
            source="user",
            sandbox_skill_dir=info["sandbox_skill_dir"],
        )
        skill_loader.register_sandbox_skill(user_skill)


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
) -> tuple[List, Optional[SkillLoader]]:
    """创建标准 Agent 工具列表。

    Parameters
    ----------
    exclude : set of tool class names to skip, e.g. {"AskUserQuestionTool"}

    Returns
    -------
    (tools, skill_loader)  skill_loader 为 None 当 Skills 未加载时
    """
    exclude = exclude or set()
    skill_loader_ref: Optional[SkillLoader] = None

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
        ("SandboxReadTool", lambda: SandboxReadTool(sandbox=sandbox, workspace_dir=workspace_dir)),
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
        ("RecordDailyLogTool", lambda: RecordDailyLogTool(
            sandbox=sandbox,
            workspace_dir=mount,
            agent_config_sync=agent_config_sync,
        )),
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
                sandbox_skill_infos = await sandbox_service.discover_sandbox_skills(
                    user_id, official_names,
                )
                _register_sandbox_skill_infos(skill_loader, sandbox_skill_infos)
                if sandbox_skill_infos:
                    logger.info(
                        "已发现 %d 个用户沙箱 Skills: %s",
                        len(sandbox_skill_infos),
                        [i["name"] for i in sandbox_skill_infos],
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
                sandbox_skill_infos = await svc.discover_sandbox_skills(
                    user_id, official_names,
                )
                before_names = set(skill_loader.sandbox_skills.keys())
                _register_sandbox_skill_infos(skill_loader, sandbox_skill_infos)
                new_names = set(skill_loader.sandbox_skills.keys()) - before_names
                if new_names:
                    logger.info("get_skill miss 后刷新发现用户沙箱 Skills: %s", sorted(new_names))

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

    return tools, skill_loader_ref
