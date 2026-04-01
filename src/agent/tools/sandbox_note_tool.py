"""Sandbox-based Session Note Tool.

通過 OpenSandbox SDK 在遠端沙箱中讀寫 session note，
取代原有的本地文件系統方式。

提供兩個工具：
- SandboxSessionNoteTool: 記錄筆記
- SandboxRecallNoteTool: 回憶筆記
"""

import json
import logging
from datetime import datetime
from typing import Any

from opensandbox import Sandbox

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)

# 沙箱中的筆記文件路徑
SANDBOX_MEMORY_FILE = "/home/user/.agent_memory.json"


def _extract_exit_code(execution: Any) -> int:
    exit_code = getattr(execution, "exit_code", None)
    if isinstance(exit_code, int):
        return exit_code
    return 1 if getattr(execution, "error", None) else 0


class SandboxSessionNoteTool(Tool):
    """在沙箱中記錄會話筆記"""

    def __init__(self, sandbox: Sandbox):
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "record_note"

    @property
    def description(self) -> str:
        return (
            "Record important information as session notes for future reference. "
            "Use this to record key facts, user preferences, decisions, or context "
            "that should be recalled later in the agent execution chain. Each note is timestamped."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to record as a note. Be concise but specific.",
                },
                "category": {
                    "type": "string",
                    "description": "Optional category/tag for this note (e.g., 'user_preference', 'project_info', 'decision')",
                },
            },
            "required": ["content"],
        }

    async def _load_notes(self) -> list:
        """從沙箱讀取筆記（SDK 失敗時回退到 cat 命令）"""
        try:
            read_file = getattr(self._sandbox.files, "read_file", None)
            if callable(read_file):
                content = await read_file(SANDBOX_MEMORY_FILE)
                return json.loads(content)
        except Exception:
            pass
        try:
            content = await self._sandbox.files.read(SANDBOX_MEMORY_FILE)
            return json.loads(content)
        except Exception:
            pass
        # 命令回退
        try:
            import shlex
            result = await self._sandbox.commands.run(f"cat {shlex.quote(SANDBOX_MEMORY_FILE)}")
            if _extract_exit_code(result) == 0:
                logs = getattr(result, "logs", None)
                stdout_lines = getattr(logs, "stdout", None)
                if stdout_lines:
                    text = "\n".join(getattr(l, "text", str(l)) for l in stdout_lines)
                else:
                    text = getattr(result, "stdout", "") or ""
                if text.strip():
                    return json.loads(text)
        except Exception:
            pass
        return []

    async def _save_notes(self, notes: list) -> None:
        """將筆記寫入沙箱"""
        content = json.dumps(notes, indent=2, ensure_ascii=False)
        write_file = getattr(self._sandbox.files, "write_file", None)
        if callable(write_file):
            await write_file(SANDBOX_MEMORY_FILE, content)
        else:
            await self._sandbox.files.write(SANDBOX_MEMORY_FILE, content.encode("utf-8"))

    async def execute(self, content: str, category: str = "general") -> ToolResult:
        """記錄筆記"""
        try:
            notes = await self._load_notes()
            note = {
                "timestamp": datetime.now().isoformat(),
                "category": category,
                "content": content,
            }
            notes.append(note)
            await self._save_notes(notes)

            return ToolResult(
                success=True,
                content=f"Recorded note: {content} (category: {category})",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                content="",
                error=f"Failed to record note: {str(e)}",
            )


class SandboxRecallNoteTool(Tool):
    """從沙箱中回憶筆記"""

    def __init__(self, sandbox: Sandbox):
        self._sandbox = sandbox

    async def _load_notes_recall(self) -> list:
        """從沙箱讀取筆記（SDK 失敗時回退到 cat 命令）"""
        try:
            read_file = getattr(self._sandbox.files, "read_file", None)
            if callable(read_file):
                content = await read_file(SANDBOX_MEMORY_FILE)
                return json.loads(content)
        except Exception:
            pass
        try:
            content = await self._sandbox.files.read(SANDBOX_MEMORY_FILE)
            return json.loads(content)
        except Exception:
            pass
        # 命令回退
        import shlex
        result = await self._sandbox.commands.run(f"cat {shlex.quote(SANDBOX_MEMORY_FILE)}")
        if _extract_exit_code(result) == 0:
            logs = getattr(result, "logs", None)
            stdout_lines = getattr(logs, "stdout", None)
            if stdout_lines:
                text = "\n".join(getattr(l, "text", str(l)) for l in stdout_lines)
            else:
                text = getattr(result, "stdout", "") or ""
            if text.strip():
                return json.loads(text)
        raise FileNotFoundError("No notes file found")

    @property
    def name(self) -> str:
        return "recall_notes"

    @property
    def description(self) -> str:
        return (
            "Recall all previously recorded session notes. "
            "Use this to retrieve important information, context, or decisions "
            "from earlier in the session or previous agent execution chains."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional: filter notes by category",
                },
            },
        }

    async def execute(self, category: str = None) -> ToolResult:
        """回憶筆記"""
        try:
            try:
                notes = await self._load_notes_recall()
            except Exception:
                return ToolResult(success=True, content="No notes recorded yet.")

            if not notes:
                return ToolResult(success=True, content="No notes recorded yet.")

            if category:
                notes = [n for n in notes if n.get("category") == category]
                if not notes:
                    return ToolResult(
                        success=True,
                        content=f"No notes found in category: {category}",
                    )

            formatted = []
            for idx, note in enumerate(notes, 1):
                timestamp = note.get("timestamp", "unknown time")
                cat = note.get("category", "general")
                note_content = note.get("content", "")
                formatted.append(f"{idx}. [{cat}] {note_content}\n   (recorded at {timestamp})")

            result = "Recorded Notes:\n" + "\n".join(formatted)
            return ToolResult(success=True, content=result)

        except Exception as e:
            return ToolResult(
                success=False,
                content="",
                error=f"Failed to recall notes: {str(e)}",
            )
