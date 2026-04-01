"""Sandbox-based file operation tools.

通過 OpenSandbox SDK 在遠端沙箱中進行文件操作，
取代原有的本地文件系統方式（file_tools.py）。

提供三個工具：
- SandboxReadTool: 讀取沙箱中的文件
- SandboxWriteTool: 在沙箱中寫入文件
- SandboxEditTool: 在沙箱中編輯文件（字串替換）

保留了原有的實用函式：
- truncate_text_by_tokens: Token 截斷
- BINARY_FORMAT_SKILLS: 二進位格式提示
"""

import base64
import difflib
import logging
import posixpath
import shlex
from typing import Any

import tiktoken
from opensandbox import Sandbox

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)


def _normalize_workspace_dir(workspace_dir: str) -> str:
    if not workspace_dir or not workspace_dir.startswith("/"):
        return "/home/user"
    normalized = posixpath.normpath(workspace_dir)
    return normalized if normalized.startswith("/") else "/home/user"


def _resolve_workspace_path(path: str, workspace_dir: str) -> str:
    if not path:
        return workspace_dir
    if path.startswith("/"):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(workspace_dir, path))


def _extract_exit_code(execution: Any) -> int:
    exit_code = getattr(execution, "exit_code", None)
    if isinstance(exit_code, int):
        return exit_code
    return 1 if getattr(execution, "error", None) else 0


def _extract_stdout(result: Any) -> str:
    """從 sandbox command result 中提取 stdout 文本。"""
    logs = getattr(result, "logs", None)
    stdout_lines = getattr(logs, "stdout", None)
    if stdout_lines:
        return "".join(getattr(line, "text", str(line)) for line in stdout_lines)
    direct = getattr(result, "stdout", None)
    return direct if isinstance(direct, str) else ""


async def _sandbox_read_text(sandbox: Sandbox, path: str) -> str:
    """讀取沙箱中的文本文件（byte-exact 保真）。

    設計原則：以 base64 命令為主路徑，確保空行、特殊字元完全保留。
    SDK GET 端點作為快速路徑，內含長度校驗；校驗不通過則回退到主路徑。

    層次:
      1. base64 命令（主路徑，保真）
      2. SDK files API（快速路徑，帶長度校驗）
    """
    last_error: Exception | None = None

    # ---------- 1) 主路徑：python3 base64（byte-exact） ----------
    py_cmd = (
        "python3 -c "
        + shlex.quote(
            "import base64,sys; "
            f"data=open({path!r},'rb').read(); "
            "sys.stdout.write(base64.b64encode(data).decode('ascii'))"
        )
    )
    try:
        result = await sandbox.commands.run(py_cmd)
        if _extract_exit_code(result) == 0:
            b64_text = _extract_stdout(result).strip()
            raw = base64.b64decode(b64_text, validate=False) if b64_text else b""
            return raw.decode("utf-8")
    except Exception as exc:
        last_error = exc
        logger.debug("base64 primary read failed for %s: %s", path, exc)

    # ---------- 2) 快速路徑：SDK files API（帶長度校驗） ----------
    try:
        read_file = getattr(sandbox.files, "read_file", None)
        if callable(read_file):
            content = await read_file(path)
            text = content if isinstance(content, str) else str(content)
            # 校驗：取遠端 stat 長度，不一致就丟棄
            try:
                stat_result = await sandbox.commands.run(f"stat -c '%s' {shlex.quote(path)}")
                if _extract_exit_code(stat_result) == 0:
                    expected_size = int(_extract_stdout(stat_result).strip().strip("'"))
                    if len(text.encode("utf-8")) == expected_size:
                        return text
                    logger.warning(
                        "SDK read size mismatch for %s: got %d bytes, expected %d",
                        path, len(text.encode("utf-8")), expected_size,
                    )
                    raise ValueError(
                        f"SDK read size mismatch for {path}: "
                        f"got {len(text.encode('utf-8'))} bytes, expected {expected_size}"
                    )
                else:
                    # stat 失敗就信任 SDK 結果
                    return text
            except Exception:
                return text
    except Exception as exc:
        last_error = exc
        logger.debug("SDK read failed for %s: %s", path, exc)

    if last_error:
        raise FileNotFoundError(f"File not found or unreadable: {path} — {last_error}") from last_error

    raise FileNotFoundError(f"File not found or unreadable: {path}")


async def _sandbox_write_text(sandbox: Sandbox, path: str, content: str) -> None:
    write_file = getattr(sandbox.files, "write_file", None)
    if callable(write_file):
        await write_file(path, content)
        return
    write = getattr(sandbox.files, "write", None)
    if callable(write):
        await write(path, content.encode("utf-8"))
        return
    raise AttributeError("Sandbox files API does not provide write_file/write")


def truncate_text_by_tokens(text: str, max_tokens: int) -> str:
    """Truncate text by token count if it exceeds the limit.

    When text exceeds the specified token limit, performs intelligent truncation
    by keeping the front and back parts while truncating the middle.
    """
    encoding = tiktoken.get_encoding("cl100k_base")
    token_count = len(encoding.encode(text))

    if token_count <= max_tokens:
        return text

    char_count = len(text)
    ratio = token_count / char_count
    chars_per_half = int((max_tokens / 2) / ratio * 0.95)

    head_part = text[:chars_per_half]
    last_newline_head = head_part.rfind("\n")
    if last_newline_head > 0:
        head_part = head_part[:last_newline_head]

    tail_part = text[-chars_per_half:]
    first_newline_tail = tail_part.find("\n")
    if first_newline_tail > 0:
        tail_part = tail_part[first_newline_tail + 1:]

    truncation_note = f"\n\n... [Content truncated: {token_count} tokens -> ~{max_tokens} tokens limit] ...\n\n"
    return head_part + truncation_note + tail_part


# 二進位文件格式到對應 skill 的映射
BINARY_FORMAT_SKILLS = {
    '.docx': ('docx', 'python skills/document-skills/docx/scripts/read_docx.py'),
    '.doc': ('docx', 'python skills/document-skills/docx/scripts/read_docx.py'),
    '.pdf': ('pdf', 'python skills/document-skills/pdf/scripts/read_pdf.py'),
    '.xlsx': ('xlsx', 'python skills/document-skills/xlsx/scripts/read_xlsx.py'),
    '.xls': ('xlsx', 'python skills/document-skills/xlsx/scripts/read_xlsx.py'),
    '.pptx': ('pptx', 'python skills/document-skills/pptx/scripts/read_pptx.py'),
    '.ppt': ('pptx', 'python skills/document-skills/pptx/scripts/read_pptx.py'),
}


class SandboxReadTool(Tool):
    """讀取沙箱中的文件。

    設計要點：
    1. 傳輸層：_sandbox_read_text 以 base64 命令為主路徑，byte-exact 保真。
    2. 邊界標記：輸出同時包含 HEADER（開頭）和 FOOTER（結尾），
       讓 LLM 明確辨認「內容已完整結束」。
    """

    def __init__(self, sandbox: Sandbox, workspace_dir: str = "/home/user"):
        self._sandbox = sandbox
        self._workspace_dir = _normalize_workspace_dir(workspace_dir)

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read text file contents from the sandbox filesystem (UTF-8). "
            "Cannot read binary files (.docx, .pdf, .xlsx) — use the corresponding skill. "
            "Output format:\n"
            "  === FILE: <path> | All N lines | COMPLETE ===\n"
            "  <numbered lines>\n"
            "  === END OF FILE ===\n"
            "When you see BOTH the COMPLETE header AND the END OF FILE footer, "
            "the content is fully included — do NOT re-read the same file."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file (relative to sandbox workspace root)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Starting line number (1-indexed). Use for large files to read from specific line",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read. Use with offset for large files to read in chunks",
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
        """讀取沙箱中的文件"""
        try:
            full_path = _resolve_workspace_path(path, self._workspace_dir)

            # 檢測二進位文件格式
            file_ext = posixpath.splitext(full_path)[1].lower()
            if file_ext in BINARY_FORMAT_SKILLS:
                skill_name, script_cmd = BINARY_FORMAT_SKILLS[file_ext]
                filename = posixpath.basename(full_path)
                safe_filename = f'"{filename}"' if ' ' in filename else filename
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Cannot read binary file '{path}'. This is a {file_ext} file.\n\n"
                          f"💡 Quick Fix: Run this command directly:\n"
                          f"   {script_cmd} {safe_filename}\n\n"
                          f"📚 For more options, use: get_skill('{skill_name}')",
                )

            # ---------- 從沙箱讀取 ----------
            content_str = await _sandbox_read_text(self._sandbox, full_path)

            # 按行處理
            lines = content_str.splitlines()

            # 應用 offset 和 limit
            if offset is not None:
                offset = int(offset) if isinstance(offset, str) else offset
            if limit is not None:
                limit = int(limit) if isinstance(limit, str) else limit

            start = (offset - 1) if offset else 0
            end = (start + limit) if limit else len(lines)
            if start < 0:
                start = 0
            if end > len(lines):
                end = len(lines)

            selected_lines = lines[start:end]

            # 格式化行號
            numbered_lines = []
            for i, line in enumerate(selected_lines, start=start + 1):
                line_content = line.rstrip("\n")
                numbered_lines.append(f"{i:6d}|{line_content}")

            body = "\n".join(numbered_lines)

            # ---------- HEADER + BODY + FOOTER ----------
            total_lines = len(lines)
            shown_start = start + 1
            shown_end = min(start + len(selected_lines), total_lines)
            is_complete = (shown_start == 1 and shown_end >= total_lines)

            if total_lines == 0:
                header = f"=== FILE: {path} | All 0 lines | COMPLETE ==="
            elif is_complete:
                header = f"=== FILE: {path} | All {total_lines} lines | COMPLETE ==="
            else:
                header = f"=== FILE: {path} | Lines {shown_start}-{shown_end} of {total_lines} total ==="

            footer = "=== END OF FILE ==="

            content = f"{header}\n{body}\n{footer}"

            # Token 截斷（仍保留 footer）
            content = truncate_text_by_tokens(content, 32000)

            return ToolResult(success=True, content=content)

        except Exception as e:
            error_msg = str(e)
            if "not found" in error_msg.lower() or "no such file" in error_msg.lower():
                return ToolResult(success=False, content="", error=f"File not found: {path}")
            return ToolResult(success=False, content="", error=error_msg)


class SandboxWriteTool(Tool):
    """在沙箱中寫入文件"""

    def __init__(self, sandbox: Sandbox, workspace_dir: str = "/home/user"):
        self._sandbox = sandbox
        self._workspace_dir = _normalize_workspace_dir(workspace_dir)

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write text content to a file in the sandbox (UTF-8 encoding only). "
            "Cannot write binary files like .docx, .pdf, .xlsx - use appropriate scripts/tools for those formats. "
            "Will overwrite existing files completely. "
            "For existing files, you should read the file first using read_file. "
            "Prefer editing existing files over creating new ones unless explicitly needed."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file (relative to sandbox workspace root)",
                },
                "content": {
                    "type": "string",
                    "description": "Complete content to write (will replace existing content)",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str) -> ToolResult:
        """在沙箱中寫入文件"""
        try:
            full_path = _resolve_workspace_path(path, self._workspace_dir)

            # 確保父目錄存在（透過 bash 命令）
            import posixpath
            parent_dir = posixpath.dirname(full_path)
            if parent_dir:
                await self._sandbox.commands.run(f"mkdir -p {shlex.quote(parent_dir)}")

            # 寫入文件
            await _sandbox_write_text(self._sandbox, full_path, content)

            return ToolResult(success=True, content=f"Successfully wrote to {full_path}")

        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))


class SandboxEditTool(Tool):
    """在沙箱中編輯文件（字串替換）"""

    def __init__(self, sandbox: Sandbox, workspace_dir: str = "/home/user"):
        self._sandbox = sandbox
        self._workspace_dir = _normalize_workspace_dir(workspace_dir)

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Perform exact string replacement in a file in the sandbox. The old_str must match exactly "
            "and appear uniquely in the file, otherwise the operation will fail. "
            "You must read the file first before editing. Preserve exact indentation from the source."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file (relative to sandbox workspace root)",
                },
                "old_str": {
                    "type": "string",
                    "description": "Exact string to find and replace (must be unique in file)",
                },
                "new_str": {
                    "type": "string",
                    "description": "Replacement string",
                },
            },
            "required": ["path", "old_str", "new_str"],
        }

    async def execute(self, path: str, old_str: str, new_str: str) -> ToolResult:
        """在沙箱中編輯文件"""
        try:
            full_path = _resolve_workspace_path(path, self._workspace_dir)

            # 讀取當前內容
            content = await _sandbox_read_text(self._sandbox, full_path)

            if old_str not in content:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Text not found in file: {old_str}",
                )

            # 替換（只替換第一個匹配）
            new_content = content.replace(old_str, new_str, 1)

            # 寫回
            await _sandbox_write_text(self._sandbox, full_path, new_content)

            # 計算 diff 統計
            old_lines_list = old_str.splitlines(keepends=True)
            new_lines_list = new_str.splitlines(keepends=True)
            matcher = difflib.SequenceMatcher(None, old_lines_list, new_lines_list)
            lines_added = 0
            lines_removed = 0
            for op, i1, i2, j1, j2 in matcher.get_opcodes():
                if op == 'replace':
                    lines_removed += (i2 - i1)
                    lines_added += (j2 - j1)
                elif op == 'insert':
                    lines_added += (j2 - j1)
                elif op == 'delete':
                    lines_removed += (i2 - i1)

            diff_info = f" +{lines_added} -{lines_removed}" if (lines_added or lines_removed) else ""
            return ToolResult(success=True, content=f"Successfully edited {full_path}{diff_info}")

        except Exception as e:
            error_msg = str(e)
            if "not found" in error_msg.lower() or "no such file" in error_msg.lower():
                return ToolResult(success=False, content="", error=f"File not found: {path}")
            return ToolResult(success=False, content="", error=error_msg)
