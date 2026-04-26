"""Core Agent implementation."""

import json
import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
import os
from time import perf_counter
from typing import AsyncIterator, Optional, Any, Callable, Awaitable

import tiktoken

from .llm import LLMClient
from .logger import AgentLogger
from .schema import Message
from .tools.base import Tool, ToolResult
from .tools.ask_user_tool import ASK_USER_TOOL_NAME
from .utils import calculate_display_width
from .utils.token_utils import truncate_text_by_tokens
from .event_emitter import AGUIEventEmitter
from .schema.agui_events import (
    AGUIEvent, AgentState, CustomEvent, EventType, InterruptDetails,
)

logger = logging.getLogger(__name__)


# ANSI color codes
class Colors:
    """Terminal color definitions"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Foreground colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    # Bright colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


class Agent:
    """Single agent with basic tools and MCP support."""

    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 50,
        workspace_dir: str = "./workspace",
        token_limit: int = 80000,  # Summary triggered when tokens exceed this value
        context_window: int = 128000,  # 模型總上下文窗口大小
        max_output_tokens: int = 16384,  # 單次輸出上限（output tokens）
        tool_timeout: int = 300,  # 单次工具执行超时（秒），0 表示不限
    ):
        self.llm = llm_client
        self.tools = {tool.name: tool for tool in tools}

        # Level 2 microcompact: tool result 超過此字符數時壓縮為摘要佔位符
        self._MICROCOMPACT_CHAR_THRESHOLD = 4000
        self.max_steps = max_steps
        self.tool_timeout = tool_timeout
        self.token_limit = token_limit
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.workspace_dir = Path(workspace_dir)

        # workspace 目录由 agent_pool_service 在沙箱中远程创建，
        # 此处仅对本地路径（非沙箱路径）兜底创建，避免 Windows 上对
        # /home/user/... 执行 mkdir 报错或产生无效目录。
        if not str(workspace_dir).startswith("/"):
            self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # 🔥 将上下文信息注入到 system_prompt 的开头（而不是末尾）
        # 这样模型能第一时间看到这些关键信息
        context_info_parts = []

        # 注入时间信息（支持时区配置）- 使用更强调的格式
        timezone_str = os.getenv('TIMEZONE') or os.getenv('TZ') or 'UTC+0'
        current_time = datetime.now()
        year = current_time.year
        context_info_parts.append(f"- 🗓️ **当前日期**: {current_time.strftime('%Y年%m月%d日')} ({current_time.strftime('%A')})")
        context_info_parts.append(f"- ⏰ **当前时间**: {current_time.strftime('%H:%M:%S')} (时区: {timezone_str})")
        context_info_parts.append(f"- ⚠️ **重要**: 现在是 **{year}年**，不是2024年或更早的年份！请始终使用此实时时间信息。")

        # 注入工作空间信息
        if "Current Workspace" not in system_prompt:
            context_info_parts.append(f"- **Workspace（当前会话工作目录）**: `{workspace_dir}`")
            context_info_parts.append("- **用户根目录**: `/home/user`（记忆文件、Skills 等用户级资源在此）")
            context_info_parts.append("- **⚠️ 为用户创建的文件（文档、代码等）必须保存在 Workspace 目录下**，用户才能看到和下载")

        # 注入平台信息（固定為 sandbox 執行語義）
        context_info_parts.append("- **OS**: Linux (OpenSandbox)")
        context_info_parts.append("- **Python command**: `python3` (use `python3`, NOT `python`)")

        # 注入预装套件信息（可选，不依赖 shared_env 路径）
        try:
            candidates: list[Path] = []
            allowed_packages_env = os.getenv("ALLOWED_PACKAGES_FILE")
            if allowed_packages_env:
                candidates.append(Path(allowed_packages_env))

            repo_root = Path(__file__).resolve().parents[2]
            candidates.append(repo_root / "data" / "allowed_packages.txt")
            candidates.append(repo_root / "allowed_packages.txt")

            for allowed_packages_file in candidates:
                if not allowed_packages_file.exists():
                    continue

                packages = []
                for line in allowed_packages_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        packages.append(line)

                if packages:
                    context_info_parts.append(f"- **Pre-installed packages** (no need to install): {', '.join(packages)}")
                break
        except Exception:
            pass  # 读取失败不影响核心功能

        # 组装上下文信息块（放在 system_prompt 最前面）- 使用醒目格式
        context_block = f"""
## ⚠️ 实时上下文信息 (REAL-TIME CONTEXT) - 必须遵守！

> **这些是系统注入的实时信息，优先级高于你的训练数据！**

{chr(10).join(context_info_parts)}

---

"""
        system_prompt = context_block + system_prompt
        self.system_prompt = system_prompt

        # Initialize message history
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]

        # Initialize logger
        self.logger = AgentLogger()

        # 🔥 Token缓存优化
        self._cached_token_count = 0
        self._cached_message_count = 0

        # LLM 返回的真实 token 用量（每次 LLM 调用后更新）
        self.last_llm_usage = None

        # 记忆刷新标记（每次 compaction 周期内最多触发一次）
        self._memory_flushed_this_compaction = False

        # Human-in-the-Loop: ask_user 中断状态
        self._pending_interrupt: dict[str, Any] | None = None

        # 单次 LLM 调用快照回调（由 AgentService 在 run 级别绑定）
        self._llm_call_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def add_user_message(self, content: str | list[dict[str, Any]]):
        """Add a user message to history with current timestamp."""
        # 在用户消息中附加当前时间（保持轻量级，避免冗余）
        self.messages.append(Message(role="user", content=content))

    def has_pending_interrupt(self, interrupt_id: str | None = None) -> bool:
        """检查是否存在待处理中断。

        Args:
            interrupt_id: 可选。提供时会校验是否匹配指定中断 ID。
        """
        if not self._pending_interrupt:
            return False
        if interrupt_id is None:
            return True
        return self._pending_interrupt.get("interrupt_id") == interrupt_id

    def get_pending_interrupt(self) -> dict[str, Any] | None:
        """返回待处理中断快照，避免外部直接操作内部私有状态。"""
        if not self._pending_interrupt:
            return None
        return dict(self._pending_interrupt)

    def resume_from_interrupt(self, interrupt_id: str, answers: dict[str, str]) -> None:
        """从 ask_user 中断中恢复，将用户答案注入对话历史。

        Args:
            interrupt_id: 中断 ID（必须匹配 _pending_interrupt）
            answers: 用户答案 {question_text: answer_label}

        Raises:
            ValueError: 无待处理中断或 ID 不匹配
        """
        if not self._pending_interrupt:
            raise ValueError("No pending interrupt to resume from")
        if self._pending_interrupt["interrupt_id"] != interrupt_id:
            raise ValueError(
                f"Interrupt ID mismatch: expected {self._pending_interrupt['interrupt_id']}, got {interrupt_id}"
            )

        tool_call_id = self._pending_interrupt["tool_call_id"]

        # 格式化答案为人类可读 + LLM 可理解的文本
        answer_lines = []
        for question_text, answer in answers.items():
            answer_lines.append(f"- {question_text}: {answer}")
        formatted_answers = "User answered:\n" + "\n".join(answer_lines) if answer_lines else "User provided no answers."

        # 找到占位 tool_result 消息并替换 content
        for msg in self.messages:
            if (
                msg.role == "tool"
                and msg.tool_call_id == tool_call_id
                and msg.content == "[Awaiting user response]"
            ):
                msg.content = formatted_answers
                break

        self._pending_interrupt = None

    def clear_pending_interrupt(self, replacement_content: str = "User chose not to answer and sent a new message instead.") -> None:
        """清除待处理的中断（用户发送了新消息而不是回答问题时调用）。"""
        if not self._pending_interrupt:
            return
        tool_call_id = self._pending_interrupt["tool_call_id"]
        for msg in self.messages:
            if (
                msg.role == "tool"
                and msg.tool_call_id == tool_call_id
                and msg.content == "[Awaiting user response]"
            ):
                msg.content = replacement_content
                break
        self._pending_interrupt = None

    def set_llm_call_hook(
        self,
        hook: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """设置/清空 step 级 LLM 调用快照回调。"""
        self._llm_call_hook = hook

    async def _emit_llm_call_record(self, payload: dict[str, Any]) -> None:
        """向上层发射 LLM 调用快照。"""
        if self._llm_call_hook is None:
            return
        await self._llm_call_hook(payload)

    def _required_tool_fields(self, tool: Tool) -> set[str]:
        """Extract required argument fields from tool schema."""
        schema = getattr(tool, "parameters", None)
        if not isinstance(schema, dict):
            return set()

        required = schema.get("required", [])
        if not isinstance(required, list):
            return set()

        return {field for field in required if isinstance(field, str)}

    def _validate_tool_arguments(self, tool_name: str, arguments: Any) -> str | None:
        """Validate tool arguments against tool schema before execution.

        Returns:
            Error message when invalid, otherwise None.
        """
        if tool_name not in self.tools:
            return f"Unknown tool: {tool_name}"

        if not isinstance(arguments, dict):
            return f"Invalid tool arguments: expected dict, got {type(arguments).__name__}"

        tool = self.tools[tool_name]
        required_fields = self._required_tool_fields(tool)
        missing_fields = sorted(field for field in required_fields if field not in arguments)
        if missing_fields:
            return f"Missing required tool arguments for '{tool_name}': {', '.join(missing_fields)}"

        return None

    def _estimate_tokens(self, force_recalculate: bool = False) -> int:
        """Accurately calculate token count for message history using tiktoken

        Uses cl100k_base encoder (GPT-4/Claude/M2 compatible)

        Args:
            force_recalculate: Force full recalculation instead of using cache

        Returns:
            Estimated token count
        """
        # 🔥 优化：使用缓存避免重复计算
        current_msg_count = len(self.messages)

        # 如果消息数量没变，直接返回缓存值
        if not force_recalculate and current_msg_count == self._cached_message_count and self._cached_token_count > 0:
            return self._cached_token_count

        try:
            # Use cl100k_base encoder (used by GPT-4 and most modern models)
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # Fallback: if tiktoken initialization fails, use simple estimation
            return self._estimate_tokens_fallback()

        total_tokens = 0

        for msg in self.messages:
            # Count text content
            if isinstance(msg.content, str):
                total_tokens += len(encoding.encode(msg.content))
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "")
                        if block_type == "image_url":
                            # 图片不能用 base64 字符串计算 token，
                            # LLM 提供商通常用固定值计费（如 OpenAI 85-1105 tokens/图）。
                            # 这里保守估算每张图 1000 tokens。
                            total_tokens += 1000
                        elif block_type == "video_url":
                            # 视频类似，保守估算 5000 tokens
                            total_tokens += 5000
                        else:
                            # 普通 block（text 等）正常计算
                            total_tokens += len(encoding.encode(str(block)))

            # Count thinking
            if msg.thinking:
                total_tokens += len(encoding.encode(msg.thinking))

            # Count tool_calls
            if msg.tool_calls:
                total_tokens += len(encoding.encode(str(msg.tool_calls)))

            # Metadata overhead per message (approximately 4 tokens)
            total_tokens += 4

        # 🔥 更新缓存
        self._cached_token_count = total_tokens
        self._cached_message_count = current_msg_count

        return total_tokens

    def _estimate_tokens_fallback(self) -> int:
        """Fallback token estimation method (when tiktoken is unavailable)"""
        total_chars = 0
        for msg in self.messages:
            if isinstance(msg.content, str):
                total_chars += len(msg.content)
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "")
                        if block_type == "image_url":
                            # 图片用固定值，不把 base64 当文本计算
                            total_chars += 2500  # ≈ 1000 tokens × 2.5 chars/token
                        elif block_type == "video_url":
                            total_chars += 12500  # ≈ 5000 tokens
                        else:
                            total_chars += len(str(block))

            if msg.thinking:
                total_chars += len(msg.thinking)

            if msg.tool_calls:
                total_chars += len(str(msg.tool_calls))

        # Rough estimation: average 2.5 characters = 1 token
        return int(total_chars / 2.5)

    def _microcompact_messages(self) -> int:
        """Level 2 壓縮：輕量清理舊輪次的 tool result 和 thinking，不調用 LLM。

        安全邊界：保留最近 2 個 user round 的全部內容不壓縮，
        只清理更早的舊消息，避免模型引用近期 tool result 時「失憶」。

        Returns:
            壓縮的消息數量
        """
        user_indices = [i for i, m in enumerate(self.messages) if m.role == "user" and i > 0 and not m.is_synthetic]
        if len(user_indices) < 3:
            return 0  # 不足 3 個 user round，跳過

        # 安全邊界：倒數第 2 個 user 消息的 index
        safe_boundary = user_indices[-2]
        compacted = 0

        for i in range(1, safe_boundary):  # 跳過 system prompt (index 0)
            msg = self.messages[i]
            if msg.role == "tool" and isinstance(msg.content, str) and len(msg.content) > self._MICROCOMPACT_CHAR_THRESHOLD:
                original_len = len(msg.content)
                msg.content = f"[Tool result compacted — {original_len} chars]"
                compacted += 1
            if msg.role == "assistant" and msg.thinking:
                msg.thinking = None
                compacted += 1

        if compacted:
            self._cached_token_count = 0  # 重置緩存（用 0 而非 None，與 _estimate_tokens 比較類型一致）
            print(f"{Colors.DIM}  Level 2 microcompact: cleared {compacted} old messages{Colors.RESET}")
        return compacted

    @property
    def _hard_ceiling(self) -> int:
        """Level 4/5 的硬頂：context_window 減去 output 預留和 buffer，下界 8192。"""
        return max(self.context_window - self.max_output_tokens - 3000, 8192)

    def _emergency_truncate(self):
        """Level 4 壓縮：緊急丟棄最老的 user round，最後手段。

        當 Level 3 摘要後仍超過硬頂（context_window - max_output_tokens - buffer）時觸發。
        每次丟棄最老的一個 user round（user 消息 + 後續的 assistant/tool 直到下一個 user）。
        """
        hard_ceiling = self._hard_ceiling
        dropped_rounds = 0
        max_drops = 3  # 安全閥：防止極端情況下無限循環

        while dropped_rounds < max_drops and self._estimate_tokens(force_recalculate=True) > hard_ceiling:
            user_indices = [i for i, m in enumerate(self.messages) if m.role == "user" and i > 0 and not m.is_synthetic]
            if len(user_indices) <= 1:
                break  # 至少保留最後一個 user round

            # 確定最老 user round 的範圍：從 user_indices[0] 到 user_indices[1]
            start = user_indices[0]
            end = user_indices[1]
            del self.messages[start:end]
            self._cached_token_count = 0
            dropped_rounds += 1

        if dropped_rounds:
            print(f"{Colors.BRIGHT_YELLOW}⚠️  Level 4 emergency truncate: dropped {dropped_rounds} oldest round(s){Colors.RESET}")

    async def _summarize_messages(self):
        """漸進式上下文管理流水線（Level 2 → Level 3 → Level 4）

        Level 2: Microcompact — 清除舊 tool result 和 thinking（不調 LLM）
        Level 3: LLM 摘要 — 將 user 之間的執行過程總結為一條消息
        Level 4: 緊急截斷 — 丟棄最老的 user round（最後手段）
        """
        estimated_tokens = self._estimate_tokens()

        if estimated_tokens <= self.token_limit:
            return

        print(f"\n{Colors.BRIGHT_YELLOW}📊 Token estimate: {estimated_tokens}/{self.token_limit}{Colors.RESET}")

        # Level 2: Microcompact（輕量，不調 LLM）
        self._microcompact_messages()
        estimated_tokens = self._estimate_tokens(force_recalculate=True)
        if estimated_tokens <= self.token_limit:
            print(f"{Colors.BRIGHT_GREEN}✓ Level 2 microcompact sufficient, tokens now: {estimated_tokens}{Colors.RESET}")
            return

        # Level 3: LLM 摘要（原有邏輯）
        print(f"{Colors.BRIGHT_YELLOW}🔄 Level 3: Triggering LLM summarization...{Colors.RESET}")
        await self._summarize_with_llm(estimated_tokens)

        # Level 4: 緊急截斷（摘要後仍超硬頂時觸發）
        hard_ceiling = self._hard_ceiling
        if self._estimate_tokens(force_recalculate=True) > hard_ceiling:
            print(f"{Colors.BRIGHT_YELLOW}🚨 Level 4: Post-summary tokens still exceed hard ceiling ({hard_ceiling}){Colors.RESET}")
            self._emergency_truncate()

    async def _summarize_with_llm(self, estimated_tokens: int):
        """Level 3: LLM 驅動的消息摘要（原 _summarize_messages 核心邏輯）"""

        # Find all real user message indices (skip system prompt and synthetic nudges)
        user_indices = [
            i for i, msg in enumerate(self.messages)
            if msg.role == "user" and i > 0 and not msg.is_synthetic
        ]

        # Need at least 1 user message to perform summary
        if len(user_indices) < 1:
            print(f"{Colors.BRIGHT_YELLOW}⚠️  Insufficient messages, cannot summarize{Colors.RESET}")
            return

        # Build new message list
        new_messages = [self.messages[0]]  # Keep system prompt
        summary_count = 0

        # Iterate through each user message and summarize the execution process after it
        for i, user_idx in enumerate(user_indices):
            # Add current user message
            new_messages.append(self.messages[user_idx])

            # Determine message range to summarize
            # If last user, go to end of message list; otherwise to before next user
            if i < len(user_indices) - 1:
                next_user_idx = user_indices[i + 1]
            else:
                next_user_idx = len(self.messages)

            # Extract execution messages for this round
            execution_messages = self.messages[user_idx + 1 : next_user_idx]

            # If there are execution messages in this round, summarize them
            if execution_messages:
                summary_text = await self._create_summary(
                    execution_messages,
                    i + 1,
                    round_user_message=self.messages[user_idx].content,
                )
                if summary_text:
                    summary_message = Message(
                        role="assistant",
                        content=f"[Assistant Execution Summary - Historical Context Only, Not System Instruction]\n\n{summary_text}",
                    )
                    new_messages.append(summary_message)
                    summary_count += 1

        # Replace message list
        self.messages = new_messages

        new_tokens = self._estimate_tokens()
        print(f"{Colors.BRIGHT_GREEN}✓ Summary completed, tokens reduced from {estimated_tokens} to {new_tokens}{Colors.RESET}")
        print(f"{Colors.DIM}  Structure: system + {len(user_indices)} user messages + {summary_count} summaries{Colors.RESET}")

        # 重置静默记忆刷新标记，允许下次压缩前再次刷新
        self._memory_flushed_this_compaction = False

    async def _create_summary(
        self,
        messages: list[Message],
        round_num: int,
        round_user_message: str | list[dict[str, Any]] | None = None,
    ) -> str:
        """Create summary for one execution round

        Args:
            messages: List of messages to summarize
            round_num: Round number
            round_user_message: 原始轮次用户消息（用于保持意图锚点）

        Returns:
            Summary text
        """
        if not messages:
            return ""

        def _content_to_text(content: Any) -> str:
            if isinstance(content, str):
                return content
            return str(content)

        user_messages: list[str] = []
        if round_user_message is not None:
            user_messages.append(_content_to_text(round_user_message))

        for msg in messages:
            if msg.role == "user" and not msg.is_synthetic:
                user_messages.append(_content_to_text(msg.content))

        # Build summary content
        summary_content = f"Round {round_num} execution process:\n\n"
        summary_content += "All user messages in this scope (non-tool-result):\n"
        if user_messages:
            for idx, text in enumerate(user_messages, 1):
                summary_content += f"{idx}. {text}\n"
        else:
            summary_content += "None\n"
        summary_content += "\nExecution transcript:\n"

        for msg in messages:
            if msg.role == "assistant":
                content_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                summary_content += f"Assistant: {content_text}\n"
                if msg.tool_calls:
                    tool_names = [tc.function.name for tc in msg.tool_calls]
                    summary_content += f"  → Called tools: {', '.join(tool_names)}\n"
            elif msg.role == "user" and not msg.is_synthetic:
                content_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                summary_content += f"User: {content_text}\n"
            elif msg.role == "tool":
                result_preview = msg.content if isinstance(msg.content, str) else str(msg.content)
                summary_content += f"  ← Tool returned: {result_preview}...\n"

        # Call LLM to generate concise summary
        try:
            summary_prompt = f"""You are summarizing one historical agent execution slice for context compaction.

{summary_content}

Write a structured summary in English using the following 9 sections:
1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and fixes
5. Problem Solving
6. All user messages (exclude tool results)
7. Pending Tasks
8. Current Work
9. Optional Next Step

Requirements:
1. Keep facts grounded in the provided content only.
2. "All user messages" must include every user message in this slice.
3. "Current Work" must describe exactly what the agent was doing at the end of this slice.
4. If a section has no information, write "None".
5. Keep the response concise and scannable.
6. Output plain text only."""

            summary_msg = Message(role="user", content=summary_prompt)
            response = await self.llm.generate(
                messages=[
                    Message(
                        role="system",
                        content="You are an assistant skilled at structured summaries for Agent context compaction.",
                    ),
                    summary_msg,
                ]
            )

            summary_text = response.content
            print(f"{Colors.BRIGHT_GREEN}✓ Summary for round {round_num} generated successfully{Colors.RESET}")
            return summary_text

        except Exception as e:
            print(f"{Colors.BRIGHT_RED}✗ Summary generation failed for round {round_num}: {e}{Colors.RESET}")
            # Use simple text summary on failure
            return summary_content

    # =========================================================================
    # 已移除廢棄方法: run() 和 run_with_steps()
    # 請使用 run_agui() 方法獲取 AG-UI 協議兼容的事件流
    # =========================================================================

    async def run_agui(
        self, 
        thread_id: str,
        run_id: str,
        cancel_token: Optional[asyncio.Event] = None,
    ) -> AsyncIterator[AGUIEvent]:
        """執行 Agent 並輸出 AG-UI 事件流
        
        這是新的主要執行方法，直接輸出 AG-UI 協議兼容的事件流。
        
        Args:
            thread_id: 對話線程 ID（等同於 session_id）
            run_id: 運行 ID（等同於 round_id）
            cancel_token: 取消令牌，外部調用 .set() 可在下一個檢查點中斷執行
            
        Yields:
            AGUIEvent: AG-UI 協議事件
            
        Example:
            async for event in agent.run_agui(thread_id, run_id):
                print(f"Event: {event.type}")
        """
        # 初始化事件發射器
        emitter = AGUIEventEmitter(thread_id, run_id)
        
        # 開始日誌記錄
        self.logger.start_new_run()
        print(f"{Colors.DIM}📝 Log file: {self.logger.get_log_file_path()}{Colors.RESET}")
        
        step = 0
        final_response: Optional[str] = None

        # 多層退出檢查計數器
        output_truncation_retries = 0
        MAX_TRUNCATION_RETRIES = 1
        empty_response_nudged = False
        
        try:
            # RUN_STARTED
            yield emitter.run_started()
            
            # STATE_SNAPSHOT - 初始狀態
            yield emitter.state_snapshot(AgentState(
                current_step=0,
                total_steps=self.max_steps,
                status="running",
            ))
            
            while step < self.max_steps:
                # 🛑 取消檢查點 1: 每個 step 開始前
                if cancel_token and cancel_token.is_set():
                    print(f"\n{Colors.BRIGHT_YELLOW}⏹️  用戶取消了執行 (step {step + 1}){Colors.RESET}")
                    yield emitter.run_finished(outcome="interrupt", result={"reason": "user_cancelled"})
                    return

                # 檢查並摘要消息歷史以防止上下文溢出（Level 2-4 流水線）
                await self._summarize_messages()

                # 漸進式提醒（倒數第2步時提醒 LLM）
                if step == self.max_steps - 2:
                    print(f"\n{Colors.BRIGHT_YELLOW}💡 剩餘步驟不多，建議 LLM 考慮總結...{Colors.RESET}")
                    reminder_msg = Message(
                        role="user",
                        content="💡 系統提示：還剩 2 步就達到步驟上限。如果你已經收集了足夠信息，請在接下來的回復中給出答案；如果信息不足，請優先調用最關鍵的工具。",
                        is_synthetic=True,
                    )
                    self.messages.append(reminder_msg)
                    yield emitter.custom_event("synthetic_user_message", {"content": reminder_msg.content})
                
                step_name = f"step_{step + 1}"
                
                # STEP_STARTED
                yield emitter.step_started(step_name)
                
                # 打印步驟頭
                BOX_WIDTH = 58
                step_text = f"{Colors.BOLD}{Colors.BRIGHT_CYAN}💭 Step {step + 1}/{self.max_steps}{Colors.RESET}"
                step_display_width = calculate_display_width(step_text)
                padding = max(0, BOX_WIDTH - 1 - step_display_width)
                print(f"\n{Colors.DIM}╭{'─' * BOX_WIDTH}╮{Colors.RESET}")
                print(f"{Colors.DIM}│{Colors.RESET} {step_text}{' ' * padding}{Colors.DIM}│{Colors.RESET}")
                print(f"{Colors.DIM}╰{'─' * BOX_WIDTH}╯{Colors.RESET}")
                
                # 獲取工具列表
                tool_list = list(self.tools.values())
                self.logger.log_request(messages=self.messages, tools=tool_list)
                request_messages_snapshot = [msg.model_dump(exclude_none=True) for msg in self.messages]
                request_tools_snapshot = [tool.name for tool in tool_list]
                step_index = step + 1
                if hasattr(self.llm, "last_request_snapshot"):
                    self.llm.last_request_snapshot = None
                request_started_at = perf_counter()
                first_token_latency_s: float | None = None
                completion_latency_s: float | None = None

                def _mark_first_token() -> None:
                    nonlocal first_token_latency_s
                    if first_token_latency_s is None:
                        first_token_latency_s = round(perf_counter() - request_started_at, 3)
                
                # 調用 LLM 並處理流式響應
                # 真正流式 Streaming 实现 (Producer-Consumer 模式)
                event_queue = asyncio.Queue()
                SENTINEL = object()

                # 重置 usage，防止取消时读到上一轮的陈旧值
                self.last_llm_usage = None

                thinking_started = False
                message_started = False

                async def on_content_delta(delta: str):
                    _mark_first_token()
                    nonlocal message_started
                    if not message_started:
                        await event_queue.put(emitter.text_message_start(role="assistant"))
                        message_started = True
                    event = emitter.text_message_content(delta)
                    if event:
                        await event_queue.put(event)

                async def on_thinking_delta(delta: str):
                    _mark_first_token()
                    nonlocal thinking_started
                    if not thinking_started:
                        await event_queue.put(emitter.thinking_start())
                        thinking_started = True
                    event = emitter.thinking_content(delta)
                    if event:
                        await event_queue.put(event)

                async def on_tool_call_delta(*_: Any):
                    _mark_first_token()

                # 保存主模型參數，供 failover 恢復（try/finally 確保所有退出路徑都恢復）
                _primary_context_window = self.context_window
                _primary_max_output_tokens = self.max_output_tokens

                async def on_failover_reset(model_id: str, context_window: int = 0, max_output_tokens: int = 0):
                    """Failover 前重置流式狀態，並臨時同步 fallback 模型參數"""
                    nonlocal thinking_started, message_started
                    if thinking_started:
                        await event_queue.put(emitter.thinking_end())
                        thinking_started = False
                    if message_started:
                        await event_queue.put(emitter.text_message_end())
                        message_started = False
                    # 臨時使用 fallback 模型參數（本次 LLM 調用結束後恢復）
                    if context_window > 0:
                        self.context_window = context_window
                    if max_output_tokens > 0:
                        self.max_output_tokens = max_output_tokens
                    await event_queue.put(CustomEvent(
                        name="failover_reset",
                        value={"model": model_id},
                    ))
                    logger.info(
                        "Failover reset: next model=%s, context_window=%d, max_output_tokens=%d",
                        model_id, self.context_window, self.max_output_tokens,
                    )

                self.llm.failover_notify = on_failover_reset

                try:
                    async def producer():
                        try:
                            return await self.llm.generate_stream(
                                messages=self.messages,
                                tools=tool_list,
                                on_content=on_content_delta,
                                on_thinking=on_thinking_delta,
                                on_tool_call=on_tool_call_delta,
                            )
                        except Exception as e:
                            return e
                        finally:
                            await event_queue.put(SENTINEL)

                    # 启动生产者任务
                    producer_task = asyncio.create_task(producer())

                    # 消费循环（带 cancel_token 检查，可在 LLM 调用期间响应取消）
                    cancelled_during_llm = False

                    # 构建 cancel 等待 future（如果有 cancel_token）
                    cancel_future: asyncio.Future | None = None
                    if cancel_token:
                        cancel_future = asyncio.ensure_future(cancel_token.wait())

                    while True:
                        get_task = asyncio.ensure_future(event_queue.get())
                        wait_set: set[asyncio.Future] = {get_task}
                        if cancel_future and not cancel_future.done():
                            wait_set.add(cancel_future)

                        done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

                        if cancel_future in done:
                            # 用户取消
                            get_task.cancel()
                            cancelled_during_llm = True
                            producer_task.cancel()
                            try:
                                await producer_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            break

                        # queue.get() 完成
                        item = get_task.result()
                        if item is SENTINEL:
                            break
                        if isinstance(item, AGUIEvent):
                            yield item

                    # 清理未使用的 cancel_future
                    if cancel_future and not cancel_future.done():
                        cancel_future.cancel()

                    # 如果是 LLM 调用期间被用户取消
                    if cancelled_during_llm:
                        logger.info("⏹️  用戶取消了執行 (LLM 調用期間)")
                        if thinking_started:
                            yield emitter.thinking_end()
                        if message_started:
                            yield emitter.text_message_end()
                        yield emitter.step_finished(step_name)
                        yield emitter.run_finished(outcome="interrupt", result={"reason": "user_cancelled"})
                        return

                    # 获取最终结果
                    result = await producer_task
                    completion_latency_s = round(perf_counter() - request_started_at, 3)
                finally:
                    # Failover 是一次性的，無論成功/失敗/取消都恢復主模型參數
                    self.context_window = _primary_context_window
                    self.max_output_tokens = _primary_max_output_tokens

                llm_request_snapshot = getattr(self.llm, "last_request_snapshot", None)
                if isinstance(llm_request_snapshot, dict):
                    request_messages_snapshot = [llm_request_snapshot]

                # 错误处理
                if isinstance(result, Exception):
                    e = result
                    from .retry import RetryExhaustedError
                    if isinstance(e, RetryExhaustedError):
                        error_msg = f"LLM call failed after {e.attempts} retries\nLast error: {str(e.last_exception)}"
                    else:
                        error_msg = f"LLM call failed: {str(e)}"

                    await self._emit_llm_call_record(
                        {
                            "step_index": step_index,
                            "request_messages": request_messages_snapshot,
                            "request_tools": request_tools_snapshot,
                            "response_content": None,
                            "response_thinking": None,
                            "response_tool_calls": None,
                            "response_error": error_msg,
                            "finish_reason": None,
                            "usage_prompt_tokens": None,
                            "usage_completion_tokens": None,
                            "usage_total_tokens": None,
                            "first_token_latency_s": first_token_latency_s,
                            "completion_latency_s": completion_latency_s,
                        }
                    )

                    print(f"\n{Colors.BRIGHT_RED}❌ Error:{Colors.RESET} {error_msg}")

                    yield emitter.step_finished(step_name)
                    yield emitter.run_error(message=error_msg)
                    return

                response = result

                response_tool_calls_payload = (
                    [tc.model_dump() for tc in response.tool_calls]
                    if response.tool_calls
                    else None
                )
                usage = response.usage
                await self._emit_llm_call_record(
                    {
                        "step_index": step_index,
                        "request_messages": request_messages_snapshot,
                        "request_tools": request_tools_snapshot,
                        "response_content": response.content,
                        "response_thinking": response.thinking,
                        "response_tool_calls": response_tool_calls_payload,
                        "response_error": None,
                        "finish_reason": response.finish_reason,
                        "usage_prompt_tokens": usage.prompt_tokens if usage else None,
                        "usage_completion_tokens": usage.completion_tokens if usage else None,
                        "usage_total_tokens": usage.total_tokens if usage else None,
                        "first_token_latency_s": first_token_latency_s,
                        "completion_latency_s": completion_latency_s,
                    }
                )

                # 记录 LLM 返回的 token 用量
                self.last_llm_usage = response.usage

                # 記錄 LLM 響應
                self.logger.log_response(
                    content=response.content or "",
                    thinking=response.thinking,
                    tool_calls=response.tool_calls,
                    finish_reason=response.finish_reason,
                )

                # 添加助手消息
                assistant_msg = Message(
                    role="assistant",
                    content=response.content,
                    thinking=response.thinking,
                    tool_calls=response.tool_calls,
                )
                self.messages.append(assistant_msg)

                # 补发结束事件 (END)
                if thinking_started:
                    yield emitter.thinking_end()
                    print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 Thinking:{Colors.RESET}")
                    print(f"{Colors.DIM}{response.thinking}{Colors.RESET}")

                # 补发 text message 事件：
                # 如果流式 delta 触发了 message_started，正常发 END；
                # 如果流式未产生 delta（如模型一次性返回 content），补发完整 START/CONTENT/END
                if message_started:
                    yield emitter.text_message_end()
                    print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant:{Colors.RESET}")
                    print(f"{response.content}")
                elif response.content:
                    # LLM 返回了 content 但流式 delta 未触发，补发完整事件
                    yield emitter.text_message_start(role="assistant")
                    evt = emitter.text_message_content(response.content)
                    if evt:
                        yield evt
                    yield emitter.text_message_end()
                    print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant (non-stream):{Colors.RESET}")
                    print(f"{response.content}")

                # 多層退出檢查（借鑑 Claude Code 的 needsFollowUp 模式）
                if not response.tool_calls:
                    # 非空正常回覆時重置空響應標記
                    if response.content and response.content.strip():
                        empty_response_nudged = False

                    # CHECK 1: 輸出截斷恢復
                    # finish_reason == "length" 表示模型被 max_tokens 截斷，
                    # 注入恢復消息讓模型從中斷點繼續（最多重試 MAX_TRUNCATION_RETRIES 次）
                    if response.finish_reason == "length" and output_truncation_retries < MAX_TRUNCATION_RETRIES:
                        output_truncation_retries += 1
                        print(f"\n{Colors.BRIGHT_YELLOW}🔄 Output truncated (finish_reason=length), "
                              f"retry {output_truncation_retries}/{MAX_TRUNCATION_RETRIES}{Colors.RESET}")
                        truncation_content = (
                            "Your output was truncated before you could finish or call a tool. "
                            "Resume EXACTLY where you stopped — no repeat, no recap, no apology. "
                            "If you have remaining work, break it into smaller tool calls."
                        )
                        self.messages.append(Message(role="user", content=truncation_content, is_synthetic=True))
                        yield emitter.custom_event("synthetic_user_message", {"content": truncation_content})
                        yield emitter.step_finished(step_name)
                        step += 1
                        continue  # 重新進入 while 循環

                    # CHECK 2: 空響應兜底
                    # 模型返回空 content + 無 tool_calls 是異常行為，nudge 一次
                    if not (response.content and response.content.strip()) and not empty_response_nudged:
                        empty_response_nudged = True
                        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  Empty response with no tool calls, nudging model...{Colors.RESET}")
                        nudge_content = (
                            "You returned an empty response with no tool calls. "
                            "Please provide your answer or call a tool to continue working."
                        )
                        self.messages.append(Message(role="user", content=nudge_content, is_synthetic=True))
                        yield emitter.custom_event("synthetic_user_message", {"content": nudge_content})
                        yield emitter.step_finished(step_name)
                        step += 1
                        continue  # 再給一次機會

                    # CHECK 3: 連續空響應，視為異常退出
                    if not (response.content and response.content.strip()):
                        error_msg = "Model returned empty response twice with no tool calls. Ending run."
                        print(f"\n{Colors.BRIGHT_RED}🚫 {error_msg}{Colors.RESET}")
                        yield emitter.step_finished(step_name)
                        yield emitter.run_error(message=error_msg)
                        return

                    # 正常完成
                    final_response = response.content
                    yield emitter.step_finished(step_name)
                    break
                
                # 🛑 取消檢查點 2: LLM 回覆後、工具執行前
                if cancel_token and cancel_token.is_set():
                    print(f"\n{Colors.BRIGHT_YELLOW}⏹️  用戶取消了執行 (LLM 已回覆，跳過工具調用){Colors.RESET}")
                    yield emitter.step_finished(step_name)
                    yield emitter.run_finished(outcome="interrupt", result={"reason": "user_cancelled"})
                    return

                # 發射工具調用事件並執行工具
                for tool_call in response.tool_calls:
                    tool_call_id = tool_call.id
                    function_name = tool_call.function.name
                    arguments = tool_call.function.arguments

                    if not isinstance(function_name, str) or not function_name.strip():
                        function_name = ""
                    
                    # TOOL_CALL_START
                    yield emitter.tool_call_start(
                        tool_call_id=tool_call_id,
                        tool_name=function_name,
                    )
                    
                    # TOOL_CALL_ARGS
                    args_json = json.dumps(arguments, ensure_ascii=False)
                    event = emitter.tool_call_args(tool_call_id, args_json)
                    if event:
                        yield event
                    
                    # TOOL_CALL_END
                    yield emitter.tool_call_end(tool_call_id)
                    
                    # 打印工具調用
                    print(f"\n{Colors.BRIGHT_YELLOW}🔧 Tool Call:{Colors.RESET} {Colors.BOLD}{Colors.CYAN}{function_name}{Colors.RESET}")
                    print(f"{Colors.DIM}   Arguments:{Colors.RESET}")
                    truncated_args = {}
                    if isinstance(arguments, dict):
                        for key, value in arguments.items():
                            value_str = str(value)
                            if len(value_str) > 200:
                                truncated_args[key] = value_str[:200] + "..."
                            else:
                                truncated_args[key] = value
                    else:
                        truncated_args = {"_raw": str(arguments)}
                    args_display = json.dumps(truncated_args, indent=2, ensure_ascii=False)
                    for line in args_display.split("\n"):
                        print(f"   {Colors.DIM}{line}{Colors.RESET}")
                    
                    # 執行工具
                    # 🛑 取消檢查點 3: 每個工具執行前
                    if cancel_token and cancel_token.is_set():
                        print(f"\n{Colors.BRIGHT_YELLOW}⏹️  用戶取消了執行 (跳過工具 {function_name}){Colors.RESET}")
                        # 補充已跳過的工具結果（避免 tool_call 無 result 的協議不一致）
                        yield emitter.tool_call_result(
                            tool_call_id=tool_call_id,
                            content="Cancelled by user",
                            execution_time_ms=0,
                        )
                        tool_msg = Message(
                            role="tool",
                            content="Cancelled by user",
                            tool_call_id=tool_call_id,
                            name=function_name,
                        )
                        self.messages.append(tool_msg)
                        # 對剩餘的 tool_calls 也補充 cancelled result
                        remaining_idx = response.tool_calls.index(tool_call) + 1
                        for remaining_tc in response.tool_calls[remaining_idx:]:
                            yield emitter.tool_call_start(
                                tool_call_id=remaining_tc.id,
                                tool_name=remaining_tc.function.name,
                            )
                            yield emitter.tool_call_end(remaining_tc.id)
                            yield emitter.tool_call_result(
                                tool_call_id=remaining_tc.id,
                                content="Cancelled by user",
                                execution_time_ms=0,
                            )
                            cancel_msg = Message(
                                role="tool",
                                content="Cancelled by user",
                                tool_call_id=remaining_tc.id,
                                name=remaining_tc.function.name,
                            )
                            self.messages.append(cancel_msg)
                        yield emitter.step_finished(step_name)
                        yield emitter.run_finished(outcome="interrupt", result={"reason": "user_cancelled"})
                        return

                    # 🛑 Human-in-the-Loop: ask_user 拦截点
                    if function_name == ASK_USER_TOOL_NAME:
                        questions_payload = arguments.get("questions", []) if isinstance(arguments, dict) else []

                        # 防御性校验：空 questions 不应触发中断，返回错误结果继续执行
                        if not questions_payload:
                            error_msg = "ask_user called with empty questions list; skipping interrupt."
                            yield emitter.tool_call_result(
                                tool_call_id=tool_call_id,
                                content=error_msg,
                                execution_time_ms=0,
                            )
                            error_result_msg = Message(
                                role="tool",
                                content=error_msg,
                                tool_call_id=tool_call_id,
                                name=function_name,
                            )
                            self.messages.append(error_result_msg)
                            continue

                        interrupt_id = str(uuid.uuid4())

                        print(f"\n{Colors.BRIGHT_MAGENTA}❓ Ask User:{Colors.RESET} {len(questions_payload)} question(s) — interrupting for user input")

                        # 注入占位 tool_result（等待用户回答后替换）
                        yield emitter.tool_call_result(
                            tool_call_id=tool_call_id,
                            content="[Awaiting user response]",
                            execution_time_ms=0,
                        )
                        placeholder_msg = Message(
                            role="tool",
                            content="[Awaiting user response]",
                            tool_call_id=tool_call_id,
                            name=function_name,
                        )
                        self.messages.append(placeholder_msg)

                        # 为剩余未处理的 tool_calls 注入 skipped 结果
                        remaining_idx = response.tool_calls.index(tool_call) + 1
                        for remaining_tc in response.tool_calls[remaining_idx:]:
                            yield emitter.tool_call_start(
                                tool_call_id=remaining_tc.id,
                                tool_name=remaining_tc.function.name,
                            )
                            yield emitter.tool_call_end(remaining_tc.id)
                            yield emitter.tool_call_result(
                                tool_call_id=remaining_tc.id,
                                content="[Skipped: user question pending]",
                                execution_time_ms=0,
                            )
                            skip_msg = Message(
                                role="tool",
                                content="[Skipped: user question pending]",
                                tool_call_id=remaining_tc.id,
                                name=remaining_tc.function.name,
                            )
                            self.messages.append(skip_msg)

                        # 保存中断状态
                        self._pending_interrupt = {
                            "interrupt_id": interrupt_id,
                            "tool_call_id": tool_call_id,
                            "questions": questions_payload,
                        }

                        yield emitter.step_finished(step_name)
                        yield emitter.run_finished(
                            outcome="interrupt",
                            interrupt=InterruptDetails(
                                id=interrupt_id,
                                reason="input_required",
                                payload={
                                    "questions": questions_payload,
                                    "tool_call_id": tool_call_id,
                                },
                            ),
                        )
                        return

                    execution_time_ms = None
                    validation_error = self._validate_tool_arguments(function_name, arguments)
                    if validation_error:
                        result = ToolResult(
                            success=False,
                            content="",
                            error=validation_error,
                        )
                    else:
                        import time
                        start_time = time.time()
                        try:
                            tool = self.tools[function_name]
                            timeout = tool.execute_timeout or self.tool_timeout
                            if timeout > 0:
                                result = await asyncio.wait_for(
                                    tool.execute(**arguments),
                                    timeout=timeout,
                                )
                            else:
                                result = await tool.execute(**arguments)
                        except asyncio.TimeoutError:
                            timeout_used = tool.execute_timeout or self.tool_timeout
                            result = ToolResult(
                                success=False,
                                content="",
                                error=f"Tool execution timed out after {timeout_used}s",
                            )
                        except Exception as e:
                            import traceback
                            error_detail = f"{type(e).__name__}: {str(e)}"
                            error_trace = traceback.format_exc()
                            result = ToolResult(
                                success=False,
                                content="",
                                error=f"Tool execution failed: {error_detail}\n\nTraceback:\n{error_trace}",
                            )
                        finally:
                            end_time = time.time()
                            execution_time_ms = int((end_time - start_time) * 1000)
                    
                    # TOOL_CALL_RESULT
                    # Level 1 壓縮：截斷過大的 tool result，防止 context 膨脹
                    result_content = result.content if result.success else f"Error: {result.error}"
                    tool_obj = self.tools.get(function_name)
                    budget = getattr(tool_obj, 'max_result_tokens', 8000) if tool_obj else 8000
                    result_content = truncate_text_by_tokens(result_content, budget)

                    yield emitter.tool_call_result(
                        tool_call_id=tool_call_id,
                        content=result_content,
                        execution_time_ms=execution_time_ms,
                    )
                    
                    # 打印結果
                    if result.success:
                        result_text = result.content
                        if len(result_text) > 500:
                            result_text = result_text[:500] + f"{Colors.DIM}...{Colors.RESET}"
                        print(f"{Colors.BRIGHT_GREEN}✓ Result:{Colors.RESET} {result_text}")
                    else:
                        print(f"{Colors.BRIGHT_RED}✗ Error:{Colors.RESET} {Colors.RED}{result.error}{Colors.RESET}")
                    
                    # 記錄工具結果
                    self.logger.log_tool_result(
                        tool_name=function_name,
                        arguments=arguments,
                        result_success=result.success,
                        result_content=result.content if result.success else None,
                        result_error=result.error if not result.success else None,
                    )
                    
                    # 添加工具消息（使用截斷後的 result_content，與前端事件一致）
                    tool_msg = Message(
                        role="tool",
                        content=result_content,
                        tool_call_id=tool_call_id,
                        name=function_name,
                    )
                    self.messages.append(tool_msg)

                # Tool call 成功執行後重置空響應標記，允許下次空回覆時再 nudge
                empty_response_nudged = False

                # STEP_FINISHED
                yield emitter.step_finished(step_name)
                
                # STATE_DELTA
                yield emitter.state_delta([
                    {"op": "replace", "path": "/currentStep", "value": step + 1}
                ])
                
                step += 1
            
            # 運行結束
            if step >= self.max_steps:
                # 達到最大步數
                error_msg = f"任務在 {self.max_steps} 步後未能完成。"
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {error_msg}{Colors.RESET}")
                yield emitter.run_finished(outcome="interrupt", result={"reason": "max_steps_reached"})
            else:
                # 正常完成
                yield emitter.run_finished(outcome="success", result={"final_response": final_response})
                
        except Exception as e:
            import traceback
            error_detail = f"{type(e).__name__}: {str(e)}"
            print(f"\n{Colors.BRIGHT_RED}❌ Unexpected error:{Colors.RESET} {error_detail}")
            yield emitter.run_error(message=error_detail)

    async def maybe_flush_memory_silent(self) -> bool:
        """软阈值触发静默记忆刷新（在 run_agui() 外单独调用，不 yield SSE 事件）

        当 token 用量达到 75% 时，通过调用 LLM 将重要内容写入记忆工具。
        整个过程不影响 SSE 流。

        Returns:
            True: 本次确实触发并完成了静默记忆写入
            False: 未触发或触发失败
        """
        if self._memory_flushed_this_compaction:
            return False
        estimated = self._estimate_tokens()
        if estimated < self.token_limit * 0.75:
            return False

        # 检查是否有记忆工具可用
        memory_tools = {"record_memory", "update_long_term_memory", "update_user"}
        available_tools = memory_tools.intersection(self.tools.keys())
        if not available_tools:
            return False

        print(f"{Colors.DIM}📝 静默记忆刷新 (tokens: {estimated}/{self.token_limit})...{Colors.RESET}")

        try:
            await self._run_tool_call_only(
                "请把本次对话中需要长期记住的重要信息（用户偏好、关键决策、重要事实）写入记忆工具，然后回复 OK。",
                allowed_tools=list(available_tools),
            )
            self._memory_flushed_this_compaction = True
            print(f"{Colors.DIM}✓ 静默记忆刷新完成{Colors.RESET}")
            return True
        except Exception as e:
            print(f"{Colors.DIM}⚠️ 静默记忆刷新失败: {e}{Colors.RESET}")
            return False

    async def _run_tool_call_only(
        self,
        prompt: str,
        allowed_tools: list[str],
        max_steps: int = 3,
    ) -> None:
        """执行一次仅工具调用的 LLM 交互（静默，不 yield 事件）

        Args:
            prompt: 提示词
            allowed_tools: 允许使用的工具名列表
            max_steps: 最大步数
        """
        temp_messages = list(self.messages)
        temp_messages.append(Message(role="user", content=prompt))

        filtered_tools = [t for t in self.tools.values() if t.name in allowed_tools]
        if not filtered_tools:
            return

        for _ in range(max_steps):
            response = await self.llm.generate(
                messages=temp_messages,
                tools=filtered_tools,
            )

            if not response.tool_calls:
                break

            temp_messages.append(Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            ))

            for tc in response.tool_calls:
                if tc.function.name in allowed_tools and tc.function.name in self.tools:
                    try:
                        result = await self.tools[tc.function.name].execute(**tc.function.arguments)
                        result_text = result.content if result.success else f"Error: {result.error}"
                    except Exception as e:
                        result_text = f"Error: {e}"
                else:
                    result_text = f"Tool {tc.function.name} not allowed in this context"

                temp_messages.append(Message(
                    role="tool",
                    content=result_text,
                    tool_call_id=tc.id,
                    name=tc.function.name,
                ))

    def get_history(self) -> list[Message]:
        """Get message history."""
        return self.messages.copy()
