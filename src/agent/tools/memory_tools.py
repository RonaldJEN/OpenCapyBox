"""分层记忆工具 — 供 Agent 在对话中读写记忆

提供四个工具：
- UpdateLongTermMemoryTool: 更新 MEMORY.md（长期知识/共识）
- SearchMemoryTool: 语义/关键词检索记忆
- ReadUserProfileTool: 只读 USER.md（用户画像）
- UpdateUserProfileTool: 读写 USER.md（用户画像）
"""

import logging
from typing import Any, Awaitable, Callable

from opensandbox import Sandbox

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)

AgentConfigSync = Callable[[str, str], Awaitable[None]]
_CONTENT_MISSING = object()


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


async def _sandbox_append_text(sandbox: Sandbox, file_path: str, content: str) -> str:
    """Append text by read-modify-write via sandbox files API."""
    existing = await _sandbox_read_text(sandbox, file_path)
    merged = f"{existing}{content}" if existing else content
    await _sandbox_write_text(sandbox, file_path, merged)
    return merged


async def _sync_agent_config_after_write(
    sync: AgentConfigSync | None,
    file_path: str,
    content: str,
) -> None:
    if sync is None:
        return
    try:
        await sync(file_path, content)
    except Exception as exc:
        logger.warning("同步 Agent 配置文件到 DB 失败 (%s): %s", file_path, exc)


class UpdateLongTermMemoryTool(Tool):
    """更新 MEMORY.md（长期知识/共识）"""

    def repeat_policy_for(self, arguments: dict[str, Any]) -> str:
        return "read_only" if arguments.get("mode") == "read" else "mutating"

    def __init__(
        self,
        sandbox: Sandbox,
        workspace_dir: str = "/home/user",
        agent_config_sync: AgentConfigSync | None = None,
    ):
        self._sandbox = sandbox
        self._workspace_dir = workspace_dir
        self._agent_config_sync = agent_config_sync

    @property
    def name(self) -> str:
        return "update_long_term_memory"

    @property
    def description(self) -> str:
        return (
            "Update the long-term memory file (MEMORY.md) in the user's workspace. "
            "This file stores persistent knowledge, facts, and consensus for explicit "
            "read or search in future conversations; it is not automatically injected. "
            "Only write when the user explicitly asks to persist information across conversations. "
            "Use 'read' mode to check current content before updating. Use 'write' mode "
            "to replace the entire content."
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
                    "description": "Content to write/append (required for write and append; pass empty string explicitly in write mode to clear the file)",
                },
            },
            "required": ["mode"],
        }

    async def execute(self, mode: str, content: Any = _CONTENT_MISSING) -> ToolResult:
        try:
            file_path = f"{self._workspace_dir}/MEMORY.md"

            if mode == "read":
                try:
                    text = await _sandbox_read_text(self._sandbox, file_path)
                    return ToolResult(success=True, content=text or "(empty)")
                except Exception:
                    return ToolResult(success=True, content="(MEMORY.md does not exist yet)")

            elif mode == "write":
                if content is _CONTENT_MISSING:
                    return ToolResult(success=False, content="", error="content is required for write mode")
                if not isinstance(content, str):
                    return ToolResult(success=False, content="", error="content must be a string")
                await _sandbox_write_text(self._sandbox, file_path, content)
                await _sync_agent_config_after_write(self._agent_config_sync, file_path, content)
                return ToolResult(success=True, content=f"MEMORY.md updated ({len(content)} chars)")

            elif mode == "append":
                if content is _CONTENT_MISSING:
                    return ToolResult(success=False, content="", error="content is required for append mode")
                if not isinstance(content, str):
                    return ToolResult(success=False, content="", error="content must be a string")
                if not content:
                    return ToolResult(success=False, content="", error="content is required for append mode")
                merged = await _sandbox_append_text(self._sandbox, file_path, f"\n{content}\n")
                await _sync_agent_config_after_write(self._agent_config_sync, file_path, merged)
                return ToolResult(success=True, content=f"Appended to MEMORY.md ({len(content)} chars)")

            else:
                return ToolResult(success=False, content="", error=f"Unknown mode: {mode}")

        except Exception as e:
            return ToolResult(success=False, content="", error=f"MEMORY operation failed: {e}")


class SearchMemoryTool(Tool):
    """语义/关键词检索记忆"""

    repeat_policy = "read_only"

    def __init__(self, db_session_factory, user_id: str):
        self._db_factory = db_session_factory
        self._user_id = user_id

    @property
    def name(self) -> str:
        return "search_memory"

    @property
    def description(self) -> str:
        return (
            "Search through the user's long-term memory and conversation history using "
            "semantic search (if embedding is configured) or keyword matching. Returns the "
            "most relevant memory snippets stored in the database. "
            "IMPORTANT: Results are retrieved from the database, NOT from sandbox files. "
            "Do NOT attempt to use read_file on paths returned by this tool. "
            "Use start_date/end_date to filter by time range (e.g. to find what was "
            "discussed during a specific week)."
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
                "start_date": {
                    "type": "string",
                    "description": "Filter results created on or after this date (ISO format YYYY-MM-DD, e.g. 2026-04-20)",
                },
                "end_date": {
                    "type": "string",
                    "description": "Filter results created on or before this date (ISO format YYYY-MM-DD, e.g. 2026-04-26)",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, top_k: int = 5, start_date: str | None = None, end_date: str | None = None) -> ToolResult:
        try:
            from src.api.services.memory_service import MemoryService

            db = self._db_factory()
            try:
                service = MemoryService(db)
                results = await service.search_memory(self._user_id, query, top_k, start_date=start_date, end_date=end_date)

                if not results:
                    return ToolResult(success=True, content="No matching memories found.")

                output_parts = []
                for i, r in enumerate(results, 1):
                    created = r.get("created_at")
                    time_str = ""
                    if created:
                        if hasattr(created, "strftime"):
                            time_str = f" | {created.strftime('%Y-%m-%d %H:%M')}"
                        else:
                            time_str = f" | {created}"
                    output_parts.append(
                        f"### [{i}] {r['file_path']} (score: {r['score']}{time_str})\n{r['text']}"
                    )
                return ToolResult(success=True, content="\n\n".join(output_parts))
            finally:
                db.close()

        except Exception as e:
            return ToolResult(success=False, content="", error=f"Memory search failed: {e}")


class ReadUserProfileTool(Tool):
    """只读 USER.md（用户画像）"""

    repeat_policy = "read_only"

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

    def repeat_policy_for(self, arguments: dict[str, Any]) -> str:
        return "read_only" if arguments.get("mode") == "read" else "mutating"

    def __init__(
        self,
        sandbox: Sandbox,
        workspace_dir: str = "/home/user",
        agent_config_sync: AgentConfigSync | None = None,
    ):
        self._sandbox = sandbox
        self._workspace_dir = workspace_dir
        self._agent_config_sync = agent_config_sync

    @property
    def name(self) -> str:
        return "update_user"

    @property
    def description(self) -> str:
        return (
            "Update the user's profile (USER.md) with personal info, background, "
            "or preferences learned during conversation. Use 'read' to check current "
            "content before updating, 'write' to replace, or 'append' to add a section. "
            "Only write when the user explicitly asks to persist profile information "
            "or change the agent configuration."
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
                    "description": "Content to write/append (required for write and append; pass empty string explicitly in write mode to clear the file)",
                },
            },
            "required": ["mode"],
        }

    async def execute(self, mode: str, content: Any = _CONTENT_MISSING) -> ToolResult:
        try:
            file_path = f"{self._workspace_dir}/USER.md"

            if mode == "read":
                try:
                    text = await _sandbox_read_text(self._sandbox, file_path)
                    return ToolResult(success=True, content=text or "(USER.md is empty)")
                except Exception:
                    return ToolResult(success=True, content="(USER.md does not exist yet)")

            elif mode == "write":
                if content is _CONTENT_MISSING:
                    return ToolResult(success=False, content="", error="content is required for write mode")
                if not isinstance(content, str):
                    return ToolResult(success=False, content="", error="content must be a string")
                await _sandbox_write_text(self._sandbox, file_path, content)
                await _sync_agent_config_after_write(self._agent_config_sync, file_path, content)
                return ToolResult(success=True, content=f"USER.md updated ({len(content)} chars)")

            elif mode == "append":
                if content is _CONTENT_MISSING:
                    return ToolResult(success=False, content="", error="content is required for append mode")
                if not isinstance(content, str):
                    return ToolResult(success=False, content="", error="content must be a string")
                if not content:
                    return ToolResult(success=False, content="", error="content is required for append mode")
                merged = await _sandbox_append_text(self._sandbox, file_path, f"\n{content}\n")
                await _sync_agent_config_after_write(self._agent_config_sync, file_path, merged)
                return ToolResult(success=True, content=f"Appended to USER.md ({len(content)} chars)")

            else:
                return ToolResult(success=False, content="", error=f"Unknown mode: {mode}")

        except Exception as e:
            return ToolResult(success=False, content="", error=f"USER.md operation failed: {e}")
