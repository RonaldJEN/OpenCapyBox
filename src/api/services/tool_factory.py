"""共享工具工厂 — 统一创建 Agent 工具列表

聊天 Agent 和 Cron Agent 共享同一套工具集，
区别仅在于 Cron Agent 排除 AskUserQuestionTool（无人交互场景）。
"""

import logging
from pathlib import Path
from typing import List, Optional, Callable, Set

from opensandbox import Sandbox

from src.agent.tools.sandbox_file_tools import SandboxReadTool, SandboxWriteTool, SandboxEditTool
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
from src.agent.tools.skill_loader import SkillLoader
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


async def create_agent_tools(
    *,
    sandbox: Sandbox,
    workspace_dir: str,
    mount: str,
    user_id: str,
    db_session_factory: Callable,
    subagent_runner: Callable | None = None,
    exclude: Optional[Set[str]] = None,
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

    # 全量候选工具（类名 -> 工厂函数），延迟构造：只有不在 exclude 中的才会被实例化
    _candidates: List[tuple[str, Callable[[], object]]] = [
        # 沙箱文件工具
        ("SandboxReadTool", lambda: SandboxReadTool(sandbox=sandbox, workspace_dir=workspace_dir)),
        ("SandboxWriteTool", lambda: SandboxWriteTool(
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            agent_config_sync=agent_config_sync,
        )),
        ("SandboxEditTool", lambda: SandboxEditTool(
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            agent_config_sync=agent_config_sync,
        )),
        # 沙箱 Bash 工具（共享 tracker）
        ("SandboxBashTool", lambda: SandboxBashTool(sandbox=sandbox, workspace_dir=workspace_dir, tracker=bg_tracker)),
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
            skill_loader = SkillLoader(str(skills_dir))
            skills = skill_loader.discover_skills()

            # 按用户 skill 配置过滤掉禁用的 skill
            try:
                from src.api.models.user_memory import UserSkillConfig
                db = db_session_factory()
                try:
                    disabled_skills = {
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
                if disabled_skills:
                    for name in disabled_skills:
                        skill_loader.loaded_skills.pop(name, None)
                    skills = [s for s in skills if s.name not in disabled_skills]
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
                from src.agent.tools.skill_loader import Skill as _Skill
                for info in sandbox_skill_infos:
                    user_skill = _Skill(
                        name=info["name"],
                        description=info["description"],
                        content="",
                        source="user",
                        sandbox_skill_dir=info["sandbox_skill_dir"],
                    )
                    skill_loader.register_sandbox_skill(user_skill)
                if sandbox_skill_infos:
                    logger.info(
                        "已发现 %d 个用户沙箱 Skills: %s",
                        len(sandbox_skill_infos),
                        [i["name"] for i in sandbox_skill_infos],
                    )
            except Exception as e:
                logger.warning("沙箱 Skill 发现失败（不影响官方 Skills）: %s", e)

            async def _ensure_skill_ready(skill_name: str) -> bool:
                skill = skill_loader.get_skill(skill_name)
                if skill and skill.source == "user":
                    return True
                svc = get_sandbox_service()
                return await svc.push_skill(user_id, str(skills_dir), skill_name)

            async def _read_sandbox_skill(skill_name: str) -> str | None:
                skill = skill_loader.get_skill(skill_name)
                if not skill or skill.source != "user" or not skill.sandbox_skill_dir:
                    return None
                svc = get_sandbox_service()
                return await svc.read_sandbox_skill_content(user_id, skill.sandbox_skill_dir)

            tools.append(GetSkillTool(
                skill_loader,
                ensure_skill_ready=_ensure_skill_ready,
                read_sandbox_skill=_read_sandbox_skill,
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
