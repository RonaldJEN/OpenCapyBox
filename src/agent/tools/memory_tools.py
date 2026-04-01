"""分层记忆工具 — 供 Agent 在对话中读写记忆

提供五个工具：
- RecordDailyLogTool: 追加结构化记忆到 MEMORY.md（长期持久化）
- UpdateLongTermMemoryTool: 更新 MEMORY.md（长期知识/共识）
- SearchMemoryTool: 语义/关键词检索记忆
- ReadUserProfileTool: 只读 USER.md（用户画像）
- UpdateUserProfileTool: 读写 USER.md（用户画像）
"""

import logging
from datetime import datetime
from typing import Any

from opensandbox import Sandbox

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)


async def _sandbox_read_text(sandbox: Sandbox, file_path: str) -> str:
    """Read text file from sandbox, returning empty string when file does not exist."""
    read_fn = getattr(sandbox.files, "read_file", None)
    if callable(read_fn):
        try:
            text = await read_fn(file_path)
            return text or ""
        except Exception:
            return ""

    try:
        text = await sandbox.files.read(file_path)
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        return text or ""
    except Exception:
        return ""


async def _sandbox_write_text(sandbox: Sandbox, file_path: str, content: str) -> None:
    """Write text file to sandbox using files API only."""
    write_fn = getattr(sandbox.files, "write_file", None)
    if callable(write_fn):
        await write_fn(file_path, content)
    else:
        await sandbox.files.write(file_path, content.encode("utf-8"))


async def _sandbox_append_text(sandbox: Sandbox, file_path: str, content: str) -> None:
    """Append text by read-modify-write via sandbox files API."""
    existing = await _sandbox_read_text(sandbox, file_path)
    merged = f"{existing}{content}" if existing else content
    await _sandbox_write_text(sandbox, file_path, merged)


class RecordDailyLogTool(Tool):
    """追加结构化记忆到 MEMORY.md（长期持久化）"""

    def __init__(self, sandbox: Sandbox, workspace_dir: str = "/home/user"):
        self._sandbox = sandbox
        self._workspace_dir = workspace_dir

    @property
    def name(self) -> str:
        return "record_memory"

    @property
    def description(self) -> str:
        return (
            "Record important information to long-term memory (MEMORY.md). "
            "Use this to remember key facts, user preferences, decisions, or insights "
            "that should persist across conversations. Each entry is timestamped and appended."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to record. Be concise but specific.",
                },
                "category": {
                    "type": "string",
                    "description": "Category tag (e.g., 'preference', 'fact', 'decision', 'insight')",
                    "default": "general",
                },
            },
            "required": ["content"],
        }

    async def execute(self, content: str, category: str = "general") -> ToolResult:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            time_str = datetime.now().strftime("%H:%M:%S")
            file_path = f"{self._workspace_dir}/MEMORY.md"

            # 构建条目
            entry = (
                f"\n## [{today} {time_str}] ({category})\n\n"
                f"{content.strip()}\n"
            )

            # 追加到长期记忆文件
            await _sandbox_append_text(self._sandbox, file_path, entry)

            return ToolResult(
                success=True,
                content=f"Recorded to {file_path}: [{category}] {content[:100]}...",
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to record memory: {e}")


class UpdateLongTermMemoryTool(Tool):
    """更新 MEMORY.md（长期知识/共识）"""

    def __init__(self, sandbox: Sandbox, workspace_dir: str = "/home/user"):
        self._sandbox = sandbox
        self._workspace_dir = workspace_dir

    @property
    def name(self) -> str:
        return "update_long_term_memory"

    @property
    def description(self) -> str:
        return (
            "Update the long-term memory file (MEMORY.md) in the user's workspace. "
            "This file stores persistent knowledge, facts, and consensus that should "
            "be available across all future conversations. Use 'read' mode to check "
            "current content before updating. Use 'write' mode to replace the entire content."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["read", "write", "append"],
                    "description": "Operation mode: 'read' to view current content, "
                    "'write' to replace entire content, 'append' to add a section.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write/append (required for 'write' and 'append' modes)",
                },
            },
            "required": ["mode"],
        }

    async def execute(self, mode: str, content: str = "") -> ToolResult:
        try:
            file_path = f"{self._workspace_dir}/MEMORY.md"

            if mode == "read":
                try:
                    text = await _sandbox_read_text(self._sandbox, file_path)
                    return ToolResult(success=True, content=text or "(empty)")
                except Exception:
                    return ToolResult(success=True, content="(MEMORY.md does not exist yet)")

            elif mode == "write":
                if not content:
                    return ToolResult(success=False, content="", error="content is required for write mode")
                await _sandbox_write_text(self._sandbox, file_path, content)
                return ToolResult(success=True, content=f"MEMORY.md updated ({len(content)} chars)")

            elif mode == "append":
                if not content:
                    return ToolResult(success=False, content="", error="content is required for append mode")
                await _sandbox_append_text(self._sandbox, file_path, f"\n{content}\n")
                return ToolResult(success=True, content=f"Appended to MEMORY.md ({len(content)} chars)")

            else:
                return ToolResult(success=False, content="", error=f"Unknown mode: {mode}")

        except Exception as e:
            return ToolResult(success=False, content="", error=f"MEMORY operation failed: {e}")


class SearchMemoryTool(Tool):
    """语义/关键词检索记忆"""

    def __init__(self, db_session_factory, user_id: str):
        self._db_factory = db_session_factory
        self._user_id = user_id

    @property
    def name(self) -> str:
        return "search_memory"

    @property
    def description(self) -> str:
        return (
            "Search through the user's long-term memory and daily logs using semantic "
            "search (if embedding is configured) or keyword matching. Returns the most "
            "relevant memory snippets. Use this when you need to recall past conversations, "
            "decisions, or user preferences."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query describing what you want to find in memory",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, top_k: int = 5) -> ToolResult:
        try:
            from src.api.services.memory_service import MemoryService

            db = self._db_factory()
            try:
                service = MemoryService(db)
                results = await service.search_memory(self._user_id, query, top_k)

                if not results:
                    return ToolResult(success=True, content="No matching memories found.")

                output_parts = []
                for i, r in enumerate(results, 1):
                    output_parts.append(
                        f"### [{i}] {r['file_path']} (score: {r['score']})\n{r['text']}"
                    )
                return ToolResult(success=True, content="\n\n".join(output_parts))
            finally:
                db.close()

        except Exception as e:
            return ToolResult(success=False, content="", error=f"Memory search failed: {e}")


class ReadUserProfileTool(Tool):
    """只读 USER.md（用户画像）"""

    def __init__(self, sandbox: Sandbox, workspace_dir: str = "/home/user"):
        self._sandbox = sandbox
        self._workspace_dir = workspace_dir

    @property
    def name(self) -> str:
        return "read_user"

    @property
    def description(self) -> str:
        return (
            "Read the user's profile (USER.md) which contains their personal info, "
            "background, and preferences. Use update_user to modify it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self) -> ToolResult:
        try:
            file_path = f"{self._workspace_dir}/USER.md"
            try:
                read_fn = getattr(self._sandbox.files, "read_file", None)
                if callable(read_fn):
                    text = await read_fn(file_path)
                else:
                    text = await self._sandbox.files.read(file_path)
                    if isinstance(text, bytes):
                        text = text.decode("utf-8")
                return ToolResult(success=True, content=text or "(USER.md is empty)")
            except Exception:
                return ToolResult(success=True, content="(USER.md does not exist yet)")
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to read user profile: {e}")


class UpdateUserProfileTool(Tool):
    """读写 USER.md（用户画像）"""

    def __init__(self, sandbox: Sandbox, workspace_dir: str = "/home/user"):
        self._sandbox = sandbox
        self._workspace_dir = workspace_dir

    @property
    def name(self) -> str:
        return "update_user"

    @property
    def description(self) -> str:
        return (
            "Update the user's profile (USER.md) with personal info, background, "
            "or preferences learned during conversation. Use 'read' to check current "
            "content before updating, 'write' to replace, or 'append' to add a section. "
            "Call this proactively when you discover important user information."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["read", "write", "append"],
                    "description": "Operation mode: 'read' to view, 'write' to replace, 'append' to add.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write/append (required for 'write' and 'append' modes)",
                },
            },
            "required": ["mode"],
        }

    async def execute(self, mode: str, content: str = "") -> ToolResult:
        try:
            file_path = f"{self._workspace_dir}/USER.md"

            if mode == "read":
                try:
                    text = await _sandbox_read_text(self._sandbox, file_path)
                    return ToolResult(success=True, content=text or "(USER.md is empty)")
                except Exception:
                    return ToolResult(success=True, content="(USER.md does not exist yet)")

            elif mode == "write":
                if not content:
                    return ToolResult(success=False, content="", error="content is required for write mode")
                await _sandbox_write_text(self._sandbox, file_path, content)
                return ToolResult(success=True, content=f"USER.md updated ({len(content)} chars)")

            elif mode == "append":
                if not content:
                    return ToolResult(success=False, content="", error="content is required for append mode")
                await _sandbox_append_text(self._sandbox, file_path, f"\n{content}\n")
                return ToolResult(success=True, content=f"Appended to USER.md ({len(content)} chars)")

            else:
                return ToolResult(success=False, content="", error=f"Unknown mode: {mode}")

        except Exception as e:
            return ToolResult(success=False, content="", error=f"USER.md operation failed: {e}")
