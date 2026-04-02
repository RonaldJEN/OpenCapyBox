"""Agent 服务 - 连接 OpenCapyBox 核心"""
import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import List, Dict, Optional, AsyncIterator, Any

from opensandbox import Sandbox

from src.agent.agent import Agent
from src.agent.llm import LLMClient
from src.agent.schema import LLMProvider, Message as AgentMessage
from src.agent.schema.agui_events import AGUIEvent, EventType
from src.agent.tools.sandbox_file_tools import SandboxReadTool, SandboxWriteTool, SandboxEditTool
from src.agent.tools.sandbox_bash_tool import SandboxBashTool, SandboxBashOutputTool, SandboxBashKillTool, _BackgroundCommandTracker
from src.agent.tools.sandbox_note_tool import SandboxSessionNoteTool, SandboxRecallNoteTool
from src.agent.tools.skill_loader import SkillLoader
from src.agent.tools.skill_tool import GetSkillTool
from src.agent.tools.memory_tools import (
    RecordDailyLogTool,
    UpdateLongTermMemoryTool,
    SearchMemoryTool,
    ReadUserProfileTool,
    UpdateUserProfileTool,
)
from src.agent.tools.cron_tool import ManageCronTool
from src.agent.tools.ask_user_tool import AskUserQuestionTool

from src.api.services.history_service import HistoryService
from src.api.services.sandbox_service import get_sandbox_service, get_sandbox_mount_path
from src.api.config import get_settings
from src.api.model_registry import get_model_registry
from pathlib import Path as PathlibPath

logger = logging.getLogger(__name__)
settings = get_settings()


class AgentService:
    """Agent 服务"""

    def __init__(
        self,
        sandbox: Sandbox,
        history_service: HistoryService,
        session_id: str,
        user_id: str,
        model_id: str | None = None,
    ):
        self.sandbox = sandbox
        self.history_service = history_service
        self.session_id = session_id
        self.user_id = user_id
        self.model_id = model_id
        self.agent: Agent | None = None
        self._last_saved_index = 0
        self._next_sequence = 0  # in-memory counter for conversation_messages.sequence
        self.skill_loader = None  # 保存 skill_loader 引用
        self.cancel_token: asyncio.Event | None = None  # per-run 取消令牌
        self._resume_lock = asyncio.Lock()  # 防止并发 resume 调用
        # 每個 session 使用沙箱內的隔離子目錄
        mount = get_sandbox_mount_path()
        self._workspace_dir = f"{mount}/sessions/{session_id}" if session_id else mount

    async def initialize_agent(self):
        """初始化 Agent（使用 Model Registry 驅動 LLM 配置）"""
        # === 從 Model Registry 創建 LLM 客戶端 ===
        try:
            registry = get_model_registry()
            if self.model_id:
                model_config = registry.get_or_raise(self.model_id)
            else:
                model_config = registry.get_default()
                self.model_id = model_config.id

            logger.info(
                "创建 LLM 客户端: model=%s, provider=%s, api_base=%s",
                model_config.model_name, model_config.provider, model_config.api_base,
            )
            llm_client = LLMClient.from_model_config(model_config)

        except FileNotFoundError as e:
            # Registry 本身加載失敗（找不到 models.yaml）→ 走 .env fallback
            logger.warning("Model Registry 不可用 (%s)，使用 .env 全局配置", e)
            llm_client = self._create_fallback_llm_client()

        except ValueError as e:
            # 模型不存在/已停用/API key 缺失
            # 先嘗試是否是 Registry 層面的結構性錯誤（如 YAML 格式壞了）
            if self.model_id and ("不存在" in str(e) or "已停用" in str(e)):
                # 指定模型不可用 → 直接報錯，不走 fallback
                raise
            # 其他 ValueError（如 YAML 解析問題）→ 走 fallback
            logger.warning("Model Registry 配置異常 (%s)，使用 .env 全局配置", e)
            llm_client = self._create_fallback_llm_client()

        # === 新用户默认文件初始化（Bootstrap）===
        self._provision_default_files_if_needed()

        # 加载 system prompt
        system_prompt = self._load_system_prompt()

        # 创建工具列表
        tools = await self._create_tools()

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
            token_limit=settings.agent_token_limit,
        )

        # 从数据库恢复历史
        self._restore_history()

    def _create_fallback_llm_client(self) -> LLMClient:
        """使用 .env 全局配置創建 LLM 客戶端（向後兼容 fallback）"""
        if settings.llm_provider.lower() == "openai":
            provider = LLMProvider.OPENAI
        else:
            provider = LLMProvider.ANTHROPIC

        logger.info(
            "创建 LLM 客户端 (fallback): api_base=%s, provider=%s, model=%s",
            settings.llm_api_base, provider, settings.llm_model,
        )
        return LLMClient(
            api_key=settings.llm_api_key,
            api_base=settings.llm_api_base,
            provider=provider,
            model=settings.llm_model,
        )

    @staticmethod
    def _auto_locate(setting_value: str, *relative_parts: str) -> Path:
        """若 setting_value 非空則直接使用，否則自動定位到 src/agent/ 下的相對路徑"""
        if setting_value:
            return Path(setting_value).resolve()
        return (Path(__file__).parent.parent.parent / "agent" / Path(*relative_parts)).resolve()

    def _get_db_session_factory(self):
        """返回 DB session 工厂函数（供 memory_tools 延迟获取 DB session）"""
        from src.api.models.database import SessionLocal
        return SessionLocal

    @staticmethod
    def _get_scheduler():
        """获取 APScheduler 实例（best-effort，避免工具层反向 import API 层）"""
        try:
            import src.api.main as _main_mod
            return getattr(getattr(_main_mod.app, "state", None), "scheduler", None)
        except Exception:
            return None

    def _load_system_prompt(self) -> str:
        """从 DB 记忆文件组装 
        SOUL.md / AGENTS.md 已包含全部指令（身份、工具规则、记忆管理等），
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
        包括：SOUL.md, AGENTS.md, MEMORY.md, HEARTBEAT.md, USER.md(PROFILE)
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

    def _build_memory_context(self) -> str:
        """从 DB 读取 SOUL/USER/AGENTS/MEMORY 并按优先级组装 system prompt 前缀"""
        try:
            from src.api.services.memory_service import MemoryService
            import tiktoken

            db = self.history_service.db
            mem_svc = MemoryService(db)
            all_files = mem_svc.get_all_memory_files(self.user_id)

            if not all_files:
                return ""

            try:
                encoding = tiktoken.get_encoding("cl100k_base")
                count_tokens = lambda t: len(encoding.encode(t))
            except Exception:
                count_tokens = lambda t: int(len(t) / 2.5)

            max_memory_tokens = int(settings.agent_token_limit * 0.15)

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

            agents = all_files.get("agents_md", "")
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

    async def _create_tools(self) -> List:
        """创建工具列表（基於 OpenSandbox）"""
        # 计算 skills 目录路径（位於 Agent Server 本地，用於 SkillLoader）
        skills_dir = self._auto_locate(settings.skills_dir, "skills")
        mount = get_sandbox_mount_path()

        # 共享後台命令追蹤器（按 session 隔離）
        bg_tracker = _BackgroundCommandTracker()

        tools = [
            # 沙箱文件工具
            SandboxReadTool(sandbox=self.sandbox, workspace_dir=self._workspace_dir),
            SandboxWriteTool(sandbox=self.sandbox, workspace_dir=self._workspace_dir),
            SandboxEditTool(sandbox=self.sandbox, workspace_dir=self._workspace_dir),
            # 沙箱 Bash 工具（共享 tracker）
            SandboxBashTool(sandbox=self.sandbox, workspace_dir=self._workspace_dir, tracker=bg_tracker),
            SandboxBashOutputTool(tracker=bg_tracker),
            SandboxBashKillTool(tracker=bg_tracker),
            # 沙箱會話筆記工具
            SandboxSessionNoteTool(sandbox=self.sandbox),
            SandboxRecallNoteTool(sandbox=self.sandbox),
            # 分层记忆工具
            RecordDailyLogTool(sandbox=self.sandbox, workspace_dir=mount),
            UpdateLongTermMemoryTool(sandbox=self.sandbox, workspace_dir=mount),
            SearchMemoryTool(
                db_session_factory=self._get_db_session_factory(),
                user_id=self.user_id,
            ),
            ReadUserProfileTool(sandbox=self.sandbox, workspace_dir=mount),
            UpdateUserProfileTool(sandbox=self.sandbox, workspace_dir=mount),
            # Cron 定时任务管理工具（通过依赖注入获取 scheduler，避免反向 import）
            ManageCronTool(
                db_session_factory=self._get_db_session_factory(),
                user_id=self.user_id,
                scheduler=self._get_scheduler(),
            ),
            # 用户交互工具（Human-in-the-Loop）
            AskUserQuestionTool(),
        ]

        # 添加搜索工具（如果配置了 Bocha AppCode）
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

        # 添加 Skills（复用前面计算的 skills_dir）
        try:
            if skills_dir.exists():
                skill_loader = SkillLoader(str(skills_dir))
                # 🔥 关键修复：必须先调用 discover_skills() 才能加载技能！
                skills = skill_loader.discover_skills()

                # 按用户 skill 配置过滤掉禁用的 skill
                try:
                    from src.api.models.user_memory import UserSkillConfig
                    db = self.history_service.db
                    disabled_skills = {
                        r.skill_name for r in
                        db.query(UserSkillConfig)
                        .filter(
                            UserSkillConfig.user_id == self.user_id,
                            UserSkillConfig.enabled == False,  # noqa: E712
                        )
                        .all()
                    }
                    if disabled_skills:
                        for name in disabled_skills:
                            skill_loader.loaded_skills.pop(name, None)
                        skills = [s for s in skills if s.name not in disabled_skills]
                        logger.info("已按用户配置禁用 %d 个 Skills: %s", len(disabled_skills), disabled_skills)
                except Exception as e:
                    logger.warning("查询 UserSkillConfig 失败，加载全部 Skills: %s", e)

                # --- 发现沙箱中用户自行安装的第三方 Skill ---
                try:
                    sandbox_service = get_sandbox_service()
                    official_names = set(skill_loader.loaded_skills.keys())
                    sandbox_skill_infos = await sandbox_service.discover_sandbox_skills(
                        self.user_id, official_names,
                    )
                    from src.agent.tools.skill_loader import Skill as _Skill
                    for info in sandbox_skill_infos:
                        user_skill = _Skill(
                            name=info["name"],
                            description=info["description"],
                            content="",  # 延迟加载
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
                    """确保 Skill 在沙箱中就绪。官方 Skill 需要推送，用户 Skill 已在沙箱中。"""
                    skill = skill_loader.get_skill(skill_name)
                    if skill and skill.source == "user":
                        return True  # 用户 Skill 已在沙箱中，无需推送
                    sandbox_service = get_sandbox_service()
                    return await sandbox_service.push_skill(
                        self.user_id,
                        str(skills_dir),
                        skill_name,
                    )

                async def _read_sandbox_skill(skill_name: str) -> str | None:
                    """从沙箱按需读取用户 Skill 的完整内容。"""
                    skill = skill_loader.get_skill(skill_name)
                    if not skill or skill.source != "user" or not skill.sandbox_skill_dir:
                        return None
                    sandbox_service = get_sandbox_service()
                    return await sandbox_service.read_sandbox_skill_content(
                        self.user_id,
                        skill.sandbox_skill_dir,
                    )

                tools.append(GetSkillTool(
                    skill_loader,
                    ensure_skill_ready=_ensure_skill_ready,
                    read_sandbox_skill=_read_sandbox_skill,
                ))
                skill_count = len(skill_loader.list_skills())
                # 🔥 保存 skill_loader 引用，用于后续注入元数据
                self.skill_loader = skill_loader
                logger.info("已加载 %d 个 Skills（官方 %d + 用户 %d）",
                            skill_count,
                            len(skill_loader.loaded_skills),
                            len(skill_loader.sandbox_skills))
            else:
                logger.warning("Skills 目录不存在: %s", skills_dir)
        except Exception as e:
            logger.warning("Skills 加载失败: %s", e)

        return tools

    def _restore_history(self):
        """从 conversation_messages 表恢复对话历史

        从乾淨的 conversation_messages 表中讀取消息，用於 Agent 上下文恢復。
        若該表無記錄（首次運行或舊數據），fallback 到 HistoryService.get_minimal_history()。
        """
        if not self.agent:
            return

        # 優先從 conversation_messages 表恢復
        from src.api.models.conversation_message import ConversationMessage
        db = self.history_service.db
        conv_msgs = (
            db.query(ConversationMessage)
            .filter(
                ConversationMessage.session_id == self.session_id,
                ConversationMessage.is_summary == False,  # noqa: E712
            )
            .order_by(ConversationMessage.sequence)
            .all()
        )

        if conv_msgs:
            for msg in conv_msgs:
                try:
                    content = json.loads(msg.content)
                except (json.JSONDecodeError, TypeError):
                    content = msg.content
                self.agent.messages.append(
                    AgentMessage(role=msg.role, content=content)
                )
            self._next_sequence = conv_msgs[-1].sequence
            logger.info(
                "從 conversation_messages 恢復 %d 條消息 (session=%s)",
                len(conv_msgs), self.session_id,
            )

        self._last_saved_index = len(self.agent.messages)

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
                        "text": f"[附件文件] name={file_name} path={file_path}。如需读取，请使用 read_file 工具。",
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
    ) -> None:
        """向 conversation_messages 表持久化一條消息。

        用於 Agent 上下文恢復，與 agui_events 互相獨立。
        """
        from src.api.models.conversation_message import ConversationMessage as ConvMsg
        db = self.history_service.db
        self._next_sequence += 1
        content_str = (
            json.dumps(content, ensure_ascii=False)
            if not isinstance(content, str)
            else content
        )
        msg = ConvMsg(
            session_id=self.session_id,
            round_id=round_id,
            sequence=self._next_sequence,
            role=role,
            content=content_str,
        )
        db.add(msg)
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning("保存 conversation_message 失敗: %s", e)

    async def chat_agui(
        self,
        user_content: list[Any],
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

        # 創建運行 ID
        run_id = str(uuid.uuid4())
        
        # 創建 Round
        self.history_service.create_round(
            session_id=self.session_id,
            round_id=run_id,
            user_message=user_message_for_history,
            user_attachments=user_attachments,
        )
        
        # 添加到 agent
        self.agent.add_user_message(agent_content)
        # 持久化用戶消息到 conversation_messages
        self._save_conversation_message("user", agent_content, round_id=run_id)

        async for event in self._run_round_stream(
            run_id=run_id,
            user_message=user_message_for_history,
            error_label="Agent執行失敗",
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
                "tool_call_id": details.get("tool_call_id"),
                "questions": questions,
            }

        return None

    def has_pending_interrupt(self, interrupt_id: str) -> bool:
        """检查是否存在匹配的待处理中断（内存态 + 持久化态）。"""
        if self.agent and self.agent.has_pending_interrupt(interrupt_id):
            return True
        return self._load_persisted_interrupt(interrupt_id) is not None

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
        if self._resume_lock.locked():
            raise RuntimeError("另一个 resume 操作正在进行中，请等待完成后重试")

        async with self._resume_lock:
            if not self.agent:
                raise RuntimeError("Agent not initialized")

            resume_user_message = self._format_resume_user_message(answers)

            if self.agent.has_pending_interrupt(interrupt_id):
                # 热恢复：直接替换 ask_user 占位 tool_result
                self.agent.resume_from_interrupt(interrupt_id, answers)
            else:
                # 冷恢复：内存中断状态已丢失，退化为注入用户回答继续对话
                persisted_interrupt = self._load_persisted_interrupt(interrupt_id)
                if not persisted_interrupt:
                    raise ValueError("No pending interrupt to resume from")

                logger.warning(
                    "resume 进入冷恢复路径: session=%s, interrupt_id=%s",
                    self.session_id,
                    interrupt_id,
                )
                self.agent.add_user_message(resume_user_message)

            # 将旧的 interrupted round 标记为 resumed，防止刷新后重复弹出
            self.history_service.resolve_interrupted_rounds(self.session_id)

            # 创建新的 run_id（恢复是一次新运行）
            run_id = str(uuid.uuid4())

            # 创建 Round（记录为 resume 操作）
            self.history_service.create_round(
                session_id=self.session_id,
                round_id=run_id,
                user_message=resume_user_message,
                user_attachments=[],
            )

            # 持久化用户 resume 消息到 conversation_messages（用于上下文恢复）
            self._save_conversation_message("user", resume_user_message, round_id=run_id)

            async for event in self._run_round_stream(
                run_id=run_id,
                user_message=resume_user_message,
                error_label="Resume 执行失败",
            ):
                yield event

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
        _memory_filenames = {"USER.md", "MEMORY.md", "SOUL.md", "AGENTS.md", "HEARTBEAT.md"}
        _file_op_tracking: set[str] = set()

        try:
            async for event in self.agent.run_agui(
                thread_id=self.session_id,
                run_id=run_id,
                cancel_token=self.cancel_token,
            ):
                await self.history_service.save_agui_event(run_id, event)

                if event.type == EventType.TEXT_MESSAGE_CONTENT:
                    accumulated_content += event.delta
                elif event.type == EventType.TEXT_MESSAGE_END:
                    final_response = accumulated_content
                    if accumulated_content:
                        self._save_conversation_message("assistant", accumulated_content, round_id=run_id)
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
                    if event.outcome == "success":
                        status = "completed"
                    elif event.outcome == "interrupt":
                        status = "interrupted"
                        if event.interrupt:
                            _interrupt_json = json.dumps(
                                event.interrupt.model_dump(exclude_none=True),
                                ensure_ascii=False,
                            )
                    else:
                        status = "failed"
                elif event.type == EventType.RUN_ERROR:
                    status = "failed"

                yield event

            self.history_service.complete_round(
                round_id=run_id,
                final_response=final_response,
                step_count=step_count,
                status=status,
                interrupt_payload=_interrupt_json,
            )

            task = asyncio.create_task(self._post_round_tasks(
                sync_memory=_dirty_memory,
                round_id=run_id,
                user_message=user_message,
                assistant_response=final_response,
            ))
            task.add_done_callback(self._on_post_round_done)

        except Exception as e:
            self.history_service.complete_round(
                round_id=run_id,
                final_response=f"{error_label}: {str(e)}",
                step_count=step_count,
                status="failed",
            )
            raise

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
                # 同步所有 agent 配置文件（USER/MEMORY/SOUL/AGENTS/HEARTBEAT）
                for ft in FILE_TYPE_TO_FILENAME:
                    content = await mem_svc.sync_from_sandbox(
                        self.user_id, self.sandbox, ft
                    )
                    if content:
                        filename = FILE_TYPE_TO_FILENAME[ft]
                        # 仅对 USER 和 MEMORY 重建语义索引
                        if ft in ("user_md", "memory_md"):
                            await mem_svc.rebuild_embeddings(self.user_id, filename, content)
                        logger.info("记忆同步完成: %s (%d chars)", filename, len(content))
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
