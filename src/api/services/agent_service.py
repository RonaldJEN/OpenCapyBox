"""Agent 服务 - 连接 OpenCapyBox 核心"""
import asyncio
import json
import logging
import uuid
from typing import List, Dict, Optional, AsyncIterator, Any

from opensandbox import Sandbox

from src.api.utils.timezone import now_naive
from src.agent.agent import Agent
from src.agent.llm import LLMClient
from src.agent.schema import Message as AgentMessage
from src.agent.schema.agui_events import AGUIEvent, EventType

from src.api.services.history_service import HistoryService
from src.api.services.sandbox_service import get_sandbox_service, get_sandbox_mount_path
from src.api.services.tool_factory import create_agent_tools
from src.api.config import get_settings
from src.api.model_registry import get_model_registry
from pathlib import Path as PathlibPath

logger = logging.getLogger(__name__)


class DuplicateRoundError(Exception):
    """幂等衝突：另一個 Worker 已搶先創建了相同 idempotency_key 的 Round"""
    def __init__(self, existing_round_id: str):
        self.existing_round_id = existing_round_id
        super().__init__(f"Duplicate round: {existing_round_id}")


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

            self._token_limit = model_config.compute_token_limit()

            logger.info(
                "创建 LLM 客户端: model=%s, provider=%s, api_base=%s",
                model_config.model_name, model_config.provider, model_config.api_base,
            )

            # 收集 fallback 模型（排除當前主模型，按 YAML 順序）
            fallback_configs = [
                m for m in registry.list_models(enabled_only=True)
                if m.id != model_config.id
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

        # 加载 system prompt
        system_prompt = self._load_system_prompt()

        # 创建工具列表
        tools, self.skill_loader = await create_agent_tools(
            sandbox=self.sandbox,
            workspace_dir=self._workspace_dir,
            mount=get_sandbox_mount_path(),
            user_id=self.user_id,
            db_session_factory=self._get_db_session_factory(),
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
        )

        # 从数据库恢复历史
        self._restore_history()

    def _get_db_session_factory(self):
        """返回 DB session 工厂函数（供 memory_tools 延迟获取 DB session）"""
        from src.api.models.database import SessionLocal
        return SessionLocal

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
        包括：SOUL.md, AGENTS.md, MEMORY.md, USER.md(PROFILE)
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

    def _restore_history(self):
        """从 conversation_messages 表恢复对话历史

        从乾淨的 conversation_messages 表中讀取消息，用於 Agent 上下文恢復。
        若該表無記錄（首次運行或舊數據），fallback 到 HistoryService.get_minimal_history()。

        注意：為防止歷史消息過多導致模型 context 膨脹（特別是 tool calling 能力較弱的模型），
        會限制最多注入 agent_max_history_messages 條消息，超出時只保留最近的消息。
        """
        if not self.agent:
            return

        from src.api.config import get_settings

        # 從 agui_events 重建完整消息列表（含 tool_calls 和 tool results）
        messages = self._rebuild_messages_from_events()

        if not messages:
            # 沒有任何歷史事件，保持空
            self._last_saved_index = len(self.agent.messages)
            return

        # 限制歷史消息數量
        max_msgs = get_settings().agent_max_history_messages
        if len(messages) > max_msgs:
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

        self.agent.messages.extend(messages)

        logger.info(
            "從 agui_events 重建 %d 條消息 (session=%s)",
            len(messages), self.session_id,
        )
        self._last_saved_index = len(self.agent.messages)

    def _rebuild_messages_from_events(self) -> list[AgentMessage]:
        """從 agui_events + conversation_messages 重建完整的 LLM messages 數組。

        conversation_messages 提供 user 消息（含多模態內容），
        agui_events 提供 assistant + tool 交互（單一事實源，無數據重複）。

        Returns:
            按時序排列的 AgentMessage 列表
        """
        from src.api.models.agui_event import AGUIEventLog
        from src.api.models.conversation_message import ConversationMessage
        from src.api.models.round import Round

        db = self.history_service.db

        # 1. 獲取本 session 的所有 round（按時間排序）
        rounds = (
            db.query(Round)
            .filter(Round.session_id == self.session_id)
            .order_by(Round.created_at)
            .all()
        )
        if not rounds:
            return []

        # 2. 預載所有 user 消息（按 round_id 索引）
        conv_msgs = (
            db.query(ConversationMessage)
            .filter(
                ConversationMessage.session_id == self.session_id,
                ConversationMessage.role == "user",
                ConversationMessage.is_summary == False,  # noqa: E712
            )
            .order_by(ConversationMessage.sequence)
            .all()
        )
        user_msgs_by_round: dict[str, list] = {}
        for m in conv_msgs:
            user_msgs_by_round.setdefault(m.round_id, []).append(m)

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

        for rnd in rounds:
            # 4a. User 消息（從 conversation_messages 取，保留多模態塊）
            user_records = user_msgs_by_round.get(rnd.id, [])
            if user_records:
                for um in user_records:
                    try:
                        content = json.loads(um.content)
                    except (json.JSONDecodeError, TypeError):
                        content = um.content
                    messages.append(
                        AgentMessage(
                            role="user",
                            content=content,
                            is_synthetic=bool(getattr(um, "is_synthetic", False)),
                        )
                    )
            elif rnd.user_message:
                # Fallback：conversation_messages 無記錄（歷史數據遷移期），用 rounds.user_message
                logger.warning(
                    "Round %s 無 conversation_messages user 記錄，fallback 到 rounds.user_message (session=%s)",
                    rnd.id, self.session_id,
                )
                messages.append(AgentMessage(role="user", content=rnd.user_message))
            else:
                # 兩邊都無 user 消息（數據損壞），跳過該 round 的 agent 輸出以避免孤立 assistant 消息
                logger.warning(
                    "Round %s 既無 conversation_messages 也無 user_message，跳過整個 round (session=%s)",
                    rnd.id, self.session_id,
                )
                continue

            # 4b. Agent 輸出（從預載的 agui_events 重建 assistant + tool 消息）
            round_messages = self._events_to_messages(events_by_round.get(rnd.id, []))

            # interrupted round 被後續輪次解決後，避免冷恢復時仍看到過期占位內容
            if getattr(rnd, "status", None) == "resumed":
                for msg in round_messages:
                    if msg.role == "tool" and msg.content == "[Awaiting user response]":
                        msg.content = "[Interrupt resolved in subsequent round]"

            messages.extend(round_messages)

        return messages

    @staticmethod
    def _events_to_messages(events) -> list[AgentMessage]:
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

        for evt in events:
            try:
                payload = json.loads(evt.payload) if isinstance(evt.payload, str) else evt.payload
            except (json.JSONDecodeError, TypeError):
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

        # Flush 殘留（round 異常中斷無 STEP_FINISHED 時）
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
    ) -> None:
        """向 conversation_messages 表持久化一條消息。

        用於 Agent 上下文恢復，與 agui_events 互相獨立。

        使用原子 INSERT…SELECT 在單條 SQL 語句內完成
        MAX(sequence) 讀取 + 行寫入，SQLite 對單條寫語句持
        排他鎖，從結構上消除併發 UNIQUE 衝突。
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
                    "is_summary": False,
                    "is_synthetic": is_synthetic,
                    "created_at": now_naive(),
                },
            )
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning("保存 conversation_message 失敗: %s", e)

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
        _memory_filenames = {"USER.md", "MEMORY.md", "SOUL.md", "AGENTS.md"}
        _file_op_tracking: set[str] = set()
        _round_finished = False  # 追蹤 round 是否已正常完成
        _final_status: str | None = None  # except 路徑填充
        _final_response: str | None = None
        _externally_terminated = False
        # 固化本輪 cancel_token，避免後續新 run 覆蓋 self.cancel_token 導致判定串擾。
        run_cancel_token = self.cancel_token

        async def _record_llm_call(payload: dict[str, Any]) -> None:
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
                    break

                await self.history_service.save_agui_event(run_id, event)

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
                    if event.outcome == "success":
                        status = "completed"
                    elif event.outcome == "interrupt":
                        # 區分用戶主動取消和 ask_user 中斷：
                        # - user_cancelled → cancelled（終態）
                        # - ask_user 問答中斷 → interrupted（中間態，可恢復）
                        _result = event.result
                        _is_user_cancel = (
                            isinstance(_result, dict)
                            and _result.get("reason") == "user_cancelled"
                        )
                        if _is_user_cancel:
                            status = "cancelled"
                        else:
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
                elif event.type == EventType.CUSTOM:
                    # 合成 user message 持久化（truncation retry / empty nudge / step reminder）
                    if getattr(event, "name", "") == "synthetic_user_message":
                        syn_content = getattr(event, "value", {}).get("content", "")
                        if syn_content:
                            self._save_conversation_message(
                                "user", syn_content, round_id=run_id, is_synthetic=True,
                            )

                yield event

            if _externally_terminated:
                return

            self.history_service.complete_round(
                round_id=run_id,
                final_response=final_response,
                step_count=step_count,
                status=status,
                interrupt_payload=_interrupt_json,
            )
            _round_finished = True

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
                    self.history_service.complete_round(
                        round_id=run_id,
                        final_response=_final_response or accumulated_content or final_response or _fallback_response,
                        step_count=step_count,
                        status=_actual_status,
                    )
                    logger.warning(
                        "Round %s 異常退出（disconnect/cancel/error），已標記為 %s (steps=%d)",
                        run_id, _actual_status, step_count,
                    )
                except Exception:
                    logger.error("Round %s 異常退出後無法更新 DB", run_id, exc_info=True)
            self.agent.set_llm_call_hook(None)

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
                # 同步所有 agent 配置文件（USER/MEMORY/SOUL/AGENTS）
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
