"""Sandbox-based file operation tools.

通過 OpenSandbox SDK 在遠端沙箱中進行文件操作，
取代原有的本地文件系統方式（file_tools.py）。

提供三個工具：
- SandboxReadTool: 讀取沙箱中的文件
- SandboxWriteTool: 在沙箱中寫入文件
- SandboxEditTool: 在沙箱中編輯文件（字串替換）

保留了原有的實用函式：
- BINARY_FORMAT_SKILLS: 二進位格式提示
"""

import asyncio
import base64
import hashlib
import json
import logging
import posixpath
import shlex
import uuid
from typing import Any, Awaitable, Callable


from opensandbox import Sandbox

from .base import Tool, ToolResult
from .session_file_references import stat_session_file_reference

logger = logging.getLogger(__name__)

AgentConfigSync = Callable[[str, str], Awaitable[None]]


class _SandboxWriteNotDispatchedError(RuntimeError):
    """The sandbox exposes no write API, so no remote mutation was attempted."""


class _SandboxWriteConflictError(RuntimeError):
    """The file changed after the caller read its edit base."""


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


def _normalize_read_only_paths(paths: set[str] | None) -> set[str]:
    return {posixpath.normpath(path) for path in (paths or set()) if path}


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

    設計原則：以帶長度及 SHA-256 校驗的 base64 命令為主路徑，確保
    空行、特殊字元完全保留。SDK files API 作為回退；遠端 stat 可用時
    必須通過 UTF-8 byte 長度校驗。

    層次:
      1. base64 命令（主路徑，保真且可檢出 stdout 截斷）
      2. SDK files API（回退路徑，盡可能做長度校驗）
    """
    last_error: Exception | None = None

    # ---------- 1) 主路徑：python3 base64（byte-exact） ----------
    py_cmd = (
        "python3 -c "
        + shlex.quote(
            "import base64,hashlib,json,sys; "
            f"data=open({path!r},'rb').read(); "
            "sys.stdout.write(json.dumps({"
            "'size':len(data),"
            "'sha256':hashlib.sha256(data).hexdigest(),"
            "'data':base64.b64encode(data).decode('ascii')"
            "},separators=(',',':')))"
        )
    )
    try:
        result = await sandbox.commands.run(py_cmd)
        if _extract_exit_code(result) == 0:
            payload = json.loads(_extract_stdout(result).strip())
            encoded = payload.get("data")
            expected_size = payload.get("size")
            expected_digest = payload.get("sha256")
            if not isinstance(encoded, str):
                raise ValueError("base64 read returned no data")
            if not isinstance(expected_size, int) or expected_size < 0:
                raise ValueError("base64 read returned an invalid size")
            if not isinstance(expected_digest, str):
                raise ValueError("base64 read returned no digest")
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) != expected_size:
                raise ValueError(
                    f"base64 read size mismatch: got {len(raw)} bytes, expected {expected_size}"
                )
            actual_digest = hashlib.sha256(raw).hexdigest()
            if actual_digest != expected_digest:
                raise ValueError("base64 read digest mismatch")
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
            # 校驗：取遠端 stat 長度，不一致就丟棄。stat 本身不可用時
            # 保留 SDK 回退能力，但不能吞掉已證實的長度不一致。
            try:
                stat_result = await sandbox.commands.run(f"stat -c '%s' {shlex.quote(path)}")
            except Exception as exc:
                logger.debug("SDK stat failed for %s: %s", path, exc)
                return text
            if _extract_exit_code(stat_result) != 0:
                return text
            try:
                expected_size = int(_extract_stdout(stat_result).strip().strip("'"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"SDK stat returned invalid size for {path}") from exc
            actual_size = len(text.encode("utf-8"))
            if actual_size != expected_size:
                logger.warning(
                    "SDK read size mismatch for %s: got %d bytes, expected %d",
                    path,
                    actual_size,
                    expected_size,
                )
                raise ValueError(
                    f"SDK read size mismatch for {path}: "
                    f"got {actual_size} bytes, expected {expected_size}"
                )
            return text
    except Exception as exc:
        last_error = exc
        logger.debug("SDK read failed for %s: %s", path, exc)

    if last_error:
        raise RuntimeError(f"File unreadable: {path} — {last_error}") from last_error

    raise FileNotFoundError(f"File not found or unreadable: {path}")


async def _sandbox_write_text(
    sandbox: Sandbox,
    path: str,
    content: str,
    *,
    workspace_dir: str | None = None,
    expected_sha256: str | None = None,
    must_not_exist: bool = False,
) -> None:
    if workspace_dir and '/sessions/' in workspace_dir and path.startswith(workspace_dir.rstrip('/') + '/'):
        # The Session editor uses the same per-path lock for its final CAS.
        # Root config files and Cron execution roots keep their existing behavior.
        edit_root = posixpath.join(workspace_dir, '.opencapybox-edit')
        temp = posixpath.join(edit_root, '.' + uuid.uuid4().hex + '.tmp')
        script = (
            'import fcntl,hashlib,os,sys\n'
            f'path={path!r}\ntemp={temp!r}\nroot={edit_root!r}\nexpected={expected_sha256!r}\nmust_not_exist={must_not_exist!r}\n'
            "lock=os.open(root+'/locks/'+hashlib.sha256(path.encode()).hexdigest(),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)\n"
            'try:\n'
            ' fcntl.flock(lock,fcntl.LOCK_EX)\n'
            ' if must_not_exist:\n'
            '  try: os.stat(path,follow_symlinks=False); sys.exit(4)\n'
            '  except FileNotFoundError: pass\n'
            ' if expected is not None:\n'
            '  current_fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)\n'
            '  try:\n'
            "   digest=hashlib.sha256(b''.join(iter(lambda:os.read(current_fd,65536),b''))).hexdigest()\n"
            '  finally:\n'
            '   os.close(current_fd)\n'
            '  if digest != expected: sys.exit(3)\n'
            ' os.replace(temp,path)\n'
            ' parent=os.open(os.path.dirname(path),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)\n'
            ' try: os.fsync(parent)\n'
            ' finally: os.close(parent)\n'
            'finally:\n'
            ' os.close(lock)\n'
            ' if os.path.exists(temp): os.unlink(temp)\n'
        )
        async def commit():
            try:
                await sandbox.commands.run('mkdir -p -- ' + shlex.quote(edit_root + '/locks'))
                await _sandbox_write_text(sandbox, temp, content)
                result = await sandbox.commands.run('python3 -c ' + shlex.quote(script))
                if _extract_exit_code(result) in {3, 4}:
                    raise _SandboxWriteConflictError('Session file changed after edit read')
                if _extract_exit_code(result) != 0:
                    raise RuntimeError('Session file commit failed')
            finally:
                try:
                    await sandbox.commands.run('rm -f -- ' + shlex.quote(temp))
                except Exception:
                    logger.warning('Session tool temp cleanup failed: %s', temp, exc_info=True)
        task = asyncio.create_task(commit())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise
        return
    write_file = getattr(sandbox.files, "write_file", None)
    if callable(write_file):
        await write_file(path, content)
        return
    write = getattr(sandbox.files, "write", None)
    if callable(write):
        await write(path, content.encode("utf-8"))
        return
    raise _SandboxWriteNotDispatchedError(
        "Sandbox files API does not provide write_file/write"
    )


def _is_missing_file_error(exc: BaseException) -> bool:
    """Recognize an explicit missing-file failure without trusting vague text."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, FileNotFoundError):
            return True
        if "no such file" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def _normalize_edit_line_endings(content: str) -> str:
    """Use LF for matching while leaving lone carriage returns untouched."""
    return content.replace("\r\n", "\n")


def _detect_edit_line_ending(content: str) -> str:
    sample = content[:4096]
    crlf_count = sample.count("\r\n")
    lf_count = sample.count("\n") - crlf_count
    return "\r\n" if crlf_count > lf_count else "\n"


def _find_edit_matches(content: str, needle: str) -> list[int]:
    """Return non-overlapping literal match offsets."""
    matches: list[int] = []
    start = 0
    while True:
        found = content.find(needle, start)
        if found < 0:
            return matches
        matches.append(found)
        start = found + len(needle)


def _raw_offsets_for_normalized_indices(
    raw_content: str,
    indices: set[int],
) -> dict[int, int]:
    """Map LF-normalized character boundaries back to raw CRLF-aware offsets."""
    offsets: dict[int, int] = {}
    normalized_index = 0
    raw_index = 0
    if 0 in indices:
        offsets[0] = 0
    while raw_index < len(raw_content) and len(offsets) < len(indices):
        raw_index += 2 if raw_content.startswith("\r\n", raw_index) else 1
        normalized_index += 1
        if normalized_index in indices:
            offsets[normalized_index] = raw_index
    if len(offsets) != len(indices):
        raise ValueError("Could not map normalized edit offsets to raw content")
    return offsets


def _render_text_edit(
    raw_content: str,
    normalized_old_str: str,
    normalized_new_str: str,
    *,
    replace_all: bool,
) -> tuple[str, int]:
    line_ending = _detect_edit_line_ending(raw_content)
    content = _normalize_edit_line_endings(raw_content)
    match_positions = _find_edit_matches(content, normalized_old_str)
    match_count = len(match_positions)
    if match_count == 0:
        raise ValueError("TEXT_NOT_FOUND")
    if match_count > 1 and not replace_all:
        raise ValueError(f"MULTIPLE_MATCHES:{match_count}")
    replacements = match_count if replace_all else 1
    selected_positions = match_positions[:replacements]
    normalized_boundaries = {
        boundary
        for position in selected_positions
        for boundary in (position, position + len(normalized_old_str))
    }
    raw_offsets = _raw_offsets_for_normalized_indices(raw_content, normalized_boundaries)
    rendered_new_str = normalized_new_str.replace("\n", line_ending)
    parts: list[str] = []
    raw_cursor = 0
    for position in selected_positions:
        raw_start = raw_offsets[position]
        raw_end = raw_offsets[position + len(normalized_old_str)]
        parts.append(raw_content[raw_cursor:raw_start])
        parts.append(rendered_new_str)
        raw_cursor = raw_end
    parts.append(raw_content[raw_cursor:])
    return "".join(parts), replacements


async def _classify_text_write(
    sandbox: Sandbox,
    path: str,
    content: str,
) -> tuple[str, str | None]:
    """Return the write classification and observed base SHA when it exists.

    The primary path hashes the existing file inside the sandbox.  The full-read
    fallback preserves compatibility with sandbox backends that cannot execute
    the probe command, while still failing closed on errors other than a missing
    target.
    """
    raw = content.encode("utf-8")
    expected_size = len(raw)
    expected_digest = hashlib.sha256(raw).hexdigest()
    probe_script = (
        "import hashlib,json,os,sys\n"
        f"path={path!r}\n"
        "if not os.path.exists(path):\n"
        "    payload={'exists':False}\n"
        "else:\n"
        "    digest=hashlib.sha256()\n"
        "    size=0\n"
        "    with open(path,'rb') as handle:\n"
        "        while True:\n"
        "            chunk=handle.read(1024*1024)\n"
        "            if not chunk:\n"
        "                break\n"
        "            size += len(chunk)\n"
        "            digest.update(chunk)\n"
        "    payload={'exists':True,'size':size,'sha256':digest.hexdigest()}\n"
        "sys.stdout.write(json.dumps(payload,separators=(',',':')))\n"
    )
    try:
        result = await sandbox.commands.run(
            "python3 -c " + shlex.quote(probe_script)
        )
        if _extract_exit_code(result) == 0:
            payload = json.loads(_extract_stdout(result).strip())
            exists = payload.get("exists")
            if exists is False:
                return "CREATED", None
            if exists is True:
                size = payload.get("size")
                digest = payload.get("sha256")
                if isinstance(size, int) and size >= 0 and isinstance(digest, str):
                    if size == expected_size and digest == expected_digest:
                        return "NO CHANGE", digest
                    return "UPDATED", digest
            logger.debug(
                "sandbox write probe returned invalid metadata for %s; "
                "falling back to a full read",
                path,
            )
    except Exception as exc:
        logger.debug("sandbox write probe failed for %s: %s", path, exc)

    try:
        existing = await _sandbox_read_text(sandbox, path)
    except Exception as exc:
        if _is_missing_file_error(exc):
            return "CREATED", None
        raise RuntimeError(
            f"Unable to inspect existing file before write: {path} — {exc}"
        ) from exc
    existing_digest = hashlib.sha256(existing.encode("utf-8")).hexdigest()
    return ("NO CHANGE" if existing == content else "UPDATED"), existing_digest


def _uncertain_write_result(path: str, exc: Exception) -> ToolResult:
    return ToolResult(
        success=False,
        error=f"Write outcome uncertain for {path}: {exc}",
        content=(
            f"The write to {path} may have succeeded. Use read_file on this path "
            "to verify its content before retrying any file mutation."
        ),
        outcome_uncertain=True,
    )


async def _sync_agent_config_after_write(
    sync: AgentConfigSync | None,
    path: str,
    content: str,
) -> None:
    if sync is None:
        return
    try:
        await sync(path, content)
    except Exception as exc:
        logger.warning("同步 Agent 配置文件到 DB 失败 (%s): %s", path, exc)


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

IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Model-facing read windows are bounded before they leave the tool.  Keep these
# limits independent from the Agent's generic tool-output truncation so a file
# result is never silently changed from a complete range into a head/tail sample.
READ_DEFAULT_LIMIT = 2000
READ_MAX_LINE_LENGTH = 2000
READ_MAX_BYTES = 50 * 1024


def _truncate_read_line(line: str) -> tuple[str, bool]:
    if len(line) <= READ_MAX_LINE_LENGTH:
        return line, False
    return (
        f"{line[:READ_MAX_LINE_LENGTH]}... (line truncated to {READ_MAX_LINE_LENGTH} chars)",
        True,
    )


def _normalize_read_integer(value: Any, name: str) -> tuple[int | None, str | None]:
    """Normalize legacy numeric strings while rejecting non-integral windows."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, f"{name} must be a positive integer"
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None, f"{name} must be a positive integer"
    if not isinstance(value, int) or value < 1:
        return None, f"{name} must be a positive integer"
    return value, None


MAX_SINGLE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 50 * 1024 * 1024


def _quote_shell_arg(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _is_within_workspace(path: str, workspace_dir: str) -> bool:
    normalized_path = posixpath.normpath(path)
    normalized_workspace = _normalize_workspace_dir(workspace_dir)
    return normalized_path == normalized_workspace or normalized_path.startswith(normalized_workspace.rstrip("/") + "/")


async def _sandbox_read_image_bytes(sandbox: Sandbox, path: str, max_bytes: int) -> tuple[bytes, int]:
    """Read image bytes via sandbox command/base64 with a remote stat guard."""
    py_code = f"""
import base64, json, os, sys
p = {path!r}
limit = {max_bytes}
try:
    st = os.stat(p)
    if not os.path.isfile(p):
        print(json.dumps({{"error": "not a regular file"}}))
        sys.exit(3)
    if st.st_size > limit:
        print(json.dumps({{"error": "file too large", "size": int(st.st_size)}}))
        sys.exit(4)
    data = open(p, "rb").read()
    print(json.dumps({{"size": int(st.st_size), "data": base64.b64encode(data).decode("ascii")}}))
except FileNotFoundError:
    print(json.dumps({{"error": "file not found"}}))
    sys.exit(2)
except Exception as e:
    print(json.dumps({{"error": str(e)}}))
    sys.exit(1)
"""
    result = await sandbox.commands.run("python3 -c " + shlex.quote(py_code))
    stdout_text = _extract_stdout(result).strip()
    try:
        payload = json.loads(stdout_text) if stdout_text else {}
    except Exception as exc:
        raise RuntimeError(f"image read returned invalid JSON for {path}") from exc

    if _extract_exit_code(result) != 0 or payload.get("error"):
        error = payload.get("error") or "image read command failed"
        size = payload.get("size")
        if size is not None:
            error = f"{error} ({size} bytes)"
        raise RuntimeError(error)

    encoded = payload.get("data")
    if not isinstance(encoded, str):
        raise RuntimeError(f"image read returned no data for {path}")
    raw = base64.b64decode(encoded, validate=False)
    size = int(payload.get("size", len(raw)) or len(raw))
    if len(raw) != size:
        logger.warning("image read size mismatch for %s: stat=%d actual=%d", path, size, len(raw))
    return raw, size


class SandboxReadImageTool(Tool):
    """Read sandbox image files and return image_url content blocks for visual models."""

    repeat_policy = "read_only"

    def __init__(
        self,
        sandbox: Sandbox,
        workspace_dir: str = "/home/user",
        *,
        supports_image: bool = False,
        max_images: int = 0,
        max_single_image_bytes: int = MAX_SINGLE_IMAGE_BYTES,
        max_total_image_bytes: int = MAX_TOTAL_IMAGE_BYTES,
    ):
        self._sandbox = sandbox
        self._workspace_dir = _normalize_workspace_dir(workspace_dir)
        self._supports_image = bool(supports_image)
        self._model_max_images = int(max_images or 0)
        self._max_single_image_bytes = max_single_image_bytes
        self._max_total_image_bytes = max_total_image_bytes

    @property
    def name(self) -> str:
        return "read_image_file"

    @property
    def description(self) -> str:
        return (
            "Read one or more sandbox image files and attach them to the next model request as visual context. "
            "Supports .png, .jpg, .jpeg, and .webp files in the current Session directory."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Image paths, absolute or relative to the current Session directory shown in the system context",
                },
                "max_images": {
                    "type": "integer",
                    "description": "Maximum number of images to read from paths (default 10).",
                    "default": 10,
                },
            },
            "required": ["paths"],
        }

    async def execute(self, paths: list[str], max_images: int = 10) -> ToolResult:
        if not self._supports_image or self._model_max_images <= 0:
            return ToolResult(
                success=False,
                content="",
                error="当前模型不支持图片输入，请切换 qwen3.6-plus/kimi-2.5 等支持图片的模型。",
            )

        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            return ToolResult(success=False, content="", error="paths must be a list of image paths")

        clean_paths = [p for p in paths if isinstance(p, str) and p.strip()]
        if not clean_paths:
            return ToolResult(success=False, content="", error="paths cannot be empty")

        try:
            requested_max = int(max_images or 10)
        except Exception:
            requested_max = 10
        if requested_max <= 0:
            return ToolResult(success=False, content="", error="max_images must be greater than 0")

        allowed_count = min(requested_max, self._model_max_images)
        if len(clean_paths) > allowed_count:
            return ToolResult(
                success=False,
                content="",
                error=f"一次最多读取 {allowed_count} 张图片，当前请求 {len(clean_paths)} 张。",
            )

        content_blocks: list[dict[str, Any]] = []
        read_summaries: list[str] = []
        total_bytes = 0

        for raw_path in clean_paths:
            full_path = _resolve_workspace_path(raw_path, self._workspace_dir)
            if not _is_within_workspace(full_path, self._workspace_dir):
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Path outside workspace is not allowed: {raw_path}",
                )

            ext = posixpath.splitext(full_path)[1].lower()
            mime_type = IMAGE_MIME_TYPES.get(ext)
            if not mime_type:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Unsupported image format for {raw_path}; supported: png, jpg, jpeg, webp",
                )

            try:
                raw_single_limit = max(1, int((self._max_single_image_bytes - 64) * 3 / 4))
                raw_bytes, size = await _sandbox_read_image_bytes(
                    self._sandbox,
                    full_path,
                    raw_single_limit,
                )
            except Exception as exc:
                return ToolResult(success=False, content="", error=f"Failed to read image {raw_path}: {exc}")

            encoded = base64.b64encode(raw_bytes).decode("ascii")
            data_url = f"data:{mime_type};base64,{encoded}"
            data_url_bytes = len(data_url.encode("ascii"))
            if data_url_bytes > self._max_single_image_bytes:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"单张图片 Data URL 超过上限 {self._max_single_image_bytes // (1024 * 1024)}MB。",
                )

            total_bytes += data_url_bytes
            if total_bytes > self._max_total_image_bytes:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"图片总大小超过上限 {self._max_total_image_bytes // (1024 * 1024)}MB。",
                )

            content_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                    "file": {
                        "path": raw_path,
                        "name": posixpath.basename(full_path),
                        "mime_type": mime_type,
                        "size": size,
                    },
                }
            )
            read_summaries.append(f"- {raw_path} ({mime_type}, {size} bytes)")

        content = "已读取图片并加入下一轮模型视觉上下文：\n" + "\n".join(read_summaries)
        return ToolResult(success=True, content=content, content_blocks=content_blocks)


class SandboxReadTool(Tool):
    """讀取沙箱中的文件。

    設計要點：
    1. 傳輸層：_sandbox_read_text 以 base64 命令為主路徑，byte-exact 保真。
    2. 呈現層：按完整行建立有界窗口，並提供明確的續讀 offset。
    3. 邊界標記：只有從首行讀到 EOF 且沒有行內截斷才標記 COMPLETE。
    """

    repeat_policy = "read_only"
    manages_model_result_size = True

    def __init__(
        self,
        sandbox: Sandbox,
        workspace_dir: str = "/home/user",
    ):
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
            f"Each call returns at most {READ_DEFAULT_LIMIT} complete lines and about "
            f"{READ_MAX_BYTES // 1024} KiB; individual lines are capped at "
            f"{READ_MAX_LINE_LENGTH} characters. "
            "Output format:\n"
            "  === FILE: <path> | All N lines | COMPLETE ===\n"
            "  <numbered lines>\n"
            "  === END OF FILE ===\n"
            "PARTIAL results always end with an exact `use offset=N to continue` marker. "
            "When you see BOTH the COMPLETE header AND the END OF FILE footer, "
            "the entire file is included — do NOT re-read it. EOF + OMITTED means "
            "the physical end was reached, but the call started after line 1 and/or a long "
            "line was shortened. A LINE LIMIT marker is a per-line preview limit; re-reading "
            "the same offset will not reveal the omitted tail."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path, or a path relative to the current Session directory shown in the system context",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Starting line number (1-indexed). Defaults to 1",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": READ_DEFAULT_LIMIT,
                    "description": (
                        f"Maximum number of lines to read. Defaults to and cannot exceed {READ_DEFAULT_LIMIT}"
                    ),
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
        """讀取沙箱中的文件"""
        try:
            # Validate before any sandbox I/O. Numeric strings remain accepted for
            # compatibility with previously persisted/provider-generated calls.
            normalized_offset, offset_error = _normalize_read_integer(offset, "offset")
            if offset_error:
                return ToolResult(success=False, error=offset_error)
            normalized_limit, limit_error = _normalize_read_integer(limit, "limit")
            if limit_error:
                return ToolResult(success=False, error=limit_error)
            if normalized_limit is not None and normalized_limit > READ_DEFAULT_LIMIT:
                return ToolResult(
                    success=False,
                    error=f"limit must be less than or equal to {READ_DEFAULT_LIMIT}",
                )

            requested_offset = normalized_offset or 1
            requested_limit = normalized_limit or READ_DEFAULT_LIMIT
            full_path = _resolve_workspace_path(path, self._workspace_dir)

            # 檢測二進位文件格式
            file_ext = posixpath.splitext(full_path)[1].lower()
            if file_ext in BINARY_FORMAT_SKILLS:
                skill_name, script_cmd = BINARY_FORMAT_SKILLS[file_ext]
                safe_path = _quote_shell_arg(full_path)
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Cannot read binary file '{path}'. This is a {file_ext} file.\n\n"
                          f"💡 Quick Fix: Run this command directly:\n"
                          f"   {script_cmd} {safe_path}\n\n"
                          f"📚 For more options, use: get_skill('{skill_name}')",
                )

            # ---------- 從沙箱讀取 ----------
            content_str = await _sandbox_read_text(self._sandbox, full_path)

            # 按行處理。splitlines() 保持既有語義：檔尾換行不額外算一行。
            lines = content_str.splitlines()

            total_lines = len(lines)
            if requested_offset > total_lines and not (total_lines == 0 and requested_offset == 1):
                return ToolResult(
                    success=False,
                    error=(
                        f"offset {requested_offset} is out of range for '{path}' "
                        f"({total_lines} lines)"
                    ),
                )

            start = requested_offset - 1
            requested_end = min(start + requested_limit, total_lines)

            # 只加入可完整容納的渲染行；不做會丟失中間內容的 head+tail 截斷。
            numbered_lines: list[str] = []
            body_bytes = 0
            truncated_by_bytes = False
            truncated_line = False
            for line_index in range(start, requested_end):
                line_content, line_was_truncated = _truncate_read_line(lines[line_index])
                rendered_line = f"{line_index + 1:6d}|{line_content}"
                rendered_bytes = len(rendered_line.encode("utf-8"))
                separator_bytes = 1 if numbered_lines else 0
                if body_bytes + separator_bytes + rendered_bytes > READ_MAX_BYTES:
                    truncated_by_bytes = True
                    break
                numbered_lines.append(rendered_line)
                body_bytes += separator_bytes + rendered_bytes
                truncated_line = truncated_line or line_was_truncated

            body = "\n".join(numbered_lines)

            # ---------- HEADER + BODY + FOOTER ----------
            shown_start = requested_offset
            shown_end = start + len(numbered_lines)
            reached_eof = shown_end >= total_lines
            has_more = not reached_eof
            is_complete = requested_offset == 1 and reached_eof and not truncated_line

            if total_lines == 0:
                header = f"=== FILE: {path} | All 0 lines | COMPLETE ==="
            elif is_complete:
                header = f"=== FILE: {path} | All {total_lines} lines | COMPLETE ==="
            elif has_more:
                reasons = []
                if truncated_by_bytes:
                    reasons.append("BYTE LIMIT")
                if truncated_line:
                    reasons.append("LINE LIMIT")
                reason = f" | {', '.join(reasons)}" if reasons else ""
                header = (
                    f"=== FILE: {path} | Lines {shown_start}-{shown_end} "
                    f"of {total_lines} total | PARTIAL{reason} ==="
                )
            else:
                reasons = []
                if requested_offset > 1:
                    reasons.append("STARTED AT OFFSET")
                if truncated_line:
                    reasons.append("LINE LIMIT")
                reason = f" | {' | '.join(reasons)}" if reasons else ""
                header = (
                    f"=== FILE: {path} | Lines {shown_start}-{shown_end} "
                    f"of {total_lines} total | EOF | OMITTED{reason} ==="
                )

            if has_more:
                footer = f"=== MORE: use offset={shown_end + 1} to continue ==="
            else:
                footer = "=== END OF FILE ==="

            content = f"{header}\n{body}\n{footer}"

            return ToolResult(success=True, content=content)

        except Exception as e:
            error_msg = str(e)
            if "not found" in error_msg.lower() or "no such file" in error_msg.lower():
                return ToolResult(success=False, content="", error=f"File not found: {path}")
            return ToolResult(success=False, content="", error=error_msg)


class SandboxWriteTool(Tool):
    """在沙箱中寫入文件"""

    repeat_policy = "mutating"

    def __init__(
        self,
        sandbox: Sandbox,
        workspace_dir: str = "/home/user",
        agent_config_sync: AgentConfigSync | None = None,
        read_only_paths: set[str] | None = None,
    ):
        self._sandbox = sandbox
        self._workspace_dir = _normalize_workspace_dir(workspace_dir)
        self._agent_config_sync = agent_config_sync
        self._read_only_paths = _normalize_read_only_paths(read_only_paths)

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write text content to a file in the sandbox (UTF-8 encoding only). "
            "Both 'path' and 'content' parameters are REQUIRED — always specify the file path. "
            "Cannot write binary files like .docx, .pdf, .xlsx - use appropriate scripts/tools for those formats. "
            "Will overwrite existing files completely. "
            "For existing files, you should read the file first using read_file. "
            "Prefer editing existing files over creating new ones unless explicitly needed. "
            "Returns CREATED, UPDATED, or NO CHANGE. If the write outcome is uncertain, "
            "read the file to verify it before retrying."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path, or a path relative to the current Session directory shown in the system context",
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
            if full_path in self._read_only_paths:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"{full_path} is managed by the platform template and cannot be edited.",
                )

            write_status, observed_sha256 = await _classify_text_write(
                self._sandbox,
                full_path,
                content,
            )

            if write_status == "NO CHANGE":
                await _sync_agent_config_after_write(
                    self._agent_config_sync,
                    full_path,
                    content,
                )
                reference = await stat_session_file_reference(
                    self._sandbox,
                    self._workspace_dir,
                    full_path,
                )
                if reference:
                    reference = {**reference, "operation": "NO_CHANGE"}
                return ToolResult(
                    success=True,
                    content=f"NO CHANGE {full_path}",
                    assistant_file_references=[reference] if reference else None,
                )

            # 確保父目錄存在（透過 bash 命令）
            parent_dir = posixpath.dirname(full_path)
            if parent_dir:
                await self._sandbox.commands.run(f"mkdir -p {shlex.quote(parent_dir)}")

            # 寫入文件
            try:
                write_options: dict[str, Any] = {"workspace_dir": self._workspace_dir}
                if (
                    '/sessions/' in self._workspace_dir
                    and full_path.startswith(self._workspace_dir.rstrip('/') + '/')
                ):
                    if observed_sha256 is not None:
                        write_options["expected_sha256"] = observed_sha256
                    else:
                        write_options["must_not_exist"] = True
                await _sandbox_write_text(self._sandbox, full_path, content, **write_options)
            except _SandboxWriteConflictError:
                return ToolResult(
                    success=False,
                    content="",
                    error="File changed while applying full write; read the latest content and retry.",
                )
            except _SandboxWriteNotDispatchedError as exc:
                return ToolResult(success=False, content="", error=str(exc))
            except Exception as exc:
                return _uncertain_write_result(full_path, exc)
            await _sync_agent_config_after_write(self._agent_config_sync, full_path, content)
            reference = await stat_session_file_reference(
                self._sandbox,
                self._workspace_dir,
                full_path,
            )
            if reference:
                reference = {**reference, "operation": write_status}

            return ToolResult(
                success=True,
                content=f"{write_status} {full_path}",
                assistant_file_references=[reference] if reference else None,
            )

        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))


class SandboxEditTool(Tool):
    """在沙箱中編輯文件（字串替換）"""

    repeat_policy = "mutating"

    def __init__(
        self,
        sandbox: Sandbox,
        workspace_dir: str = "/home/user",
        agent_config_sync: AgentConfigSync | None = None,
        read_only_paths: set[str] | None = None,
    ):
        self._sandbox = sandbox
        self._workspace_dir = _normalize_workspace_dir(workspace_dir)
        self._agent_config_sync = agent_config_sync
        self._read_only_paths = _normalize_read_only_paths(read_only_paths)

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Perform exact string replacement in a file in the sandbox. The old_str must match exactly "
            "and appear uniquely in the file by default. If it appears more than once, provide a more "
            "specific old_str or explicitly set replace_all=true. Empty old_str and identical old_str/new_str "
            "are rejected. "
            "You must read the file first before editing. Preserve exact indentation from the source."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path, or a path relative to the current Session directory shown in the system context",
                },
                "old_str": {
                    "type": "string",
                    "description": "Exact string to find and replace (must be unique in file)",
                },
                "new_str": {
                    "type": "string",
                    "description": "Replacement string",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every match; defaults to false, which requires exactly one match",
                    "default": False,
                },
            },
            "required": ["path", "old_str", "new_str"],
        }

    async def execute(
        self,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
    ) -> ToolResult:
        """在沙箱中編輯文件"""
        try:
            normalized_old_str = _normalize_edit_line_endings(old_str)
            normalized_new_str = _normalize_edit_line_endings(new_str)
            if normalized_old_str == "":
                return ToolResult(
                    success=False,
                    content="",
                    error="old_str must not be empty",
                )
            if normalized_old_str == normalized_new_str:
                return ToolResult(
                    success=False,
                    content="",
                    error="old_str and new_str must be different",
                )
            if not isinstance(replace_all, bool):
                return ToolResult(
                    success=False,
                    content="",
                    error="replace_all must be a boolean",
                )

            full_path = _resolve_workspace_path(path, self._workspace_dir)
            if full_path in self._read_only_paths:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"{full_path} is managed by the platform template and cannot be edited.",
                )

            use_session_cas = (
                '/sessions/' in self._workspace_dir
                and full_path.startswith(self._workspace_dir.rstrip('/') + '/')
            )
            max_attempts = 2 if use_session_cas else 1
            replacements = 0
            new_content = ""
            for attempt in range(max_attempts):
                raw_content = await _sandbox_read_text(self._sandbox, full_path)
                try:
                    new_content, replacements = _render_text_edit(
                        raw_content,
                        normalized_old_str,
                        normalized_new_str,
                        replace_all=replace_all,
                    )
                except ValueError as exc:
                    code = str(exc)
                    if code == "TEXT_NOT_FOUND":
                        return ToolResult(success=False, content="", error=f"Text not found in file: {old_str}")
                    if code.startswith("MULTIPLE_MATCHES:"):
                        match_count = int(code.split(":", 1)[1])
                        return ToolResult(
                            success=False,
                            content="",
                            error=(
                                f"Found {match_count} matches for old_str in {full_path}. "
                                "Provide a more specific old_str or set replace_all=true."
                            ),
                        )
                    raise
                try:
                    write_options: dict[str, Any] = {"workspace_dir": self._workspace_dir}
                    if use_session_cas:
                        write_options["expected_sha256"] = hashlib.sha256(
                            raw_content.encode("utf-8")
                        ).hexdigest()
                    await _sandbox_write_text(
                        self._sandbox,
                        full_path,
                        new_content,
                        **write_options,
                    )
                    break
                except _SandboxWriteConflictError:
                    if attempt + 1 >= max_attempts:
                        return ToolResult(
                            success=False,
                            content="",
                            error="File changed while applying edit; read the latest content and retry.",
                        )
                except _SandboxWriteNotDispatchedError as exc:
                    return ToolResult(success=False, content="", error=str(exc))
                except Exception as exc:
                    return _uncertain_write_result(full_path, exc)
            await _sync_agent_config_after_write(self._agent_config_sync, full_path, new_content)
            reference = await stat_session_file_reference(
                self._sandbox,
                self._workspace_dir,
                full_path,
            )
            if reference:
                reference = {**reference, "operation": "UPDATED"}

            return ToolResult(
                success=True,
                content=f"EDITED {full_path} | replacements={replacements}",
                assistant_file_references=[reference] if reference else None,
            )

        except Exception as e:
            error_msg = str(e)
            if "not found" in error_msg.lower() or "no such file" in error_msg.lower():
                return ToolResult(success=False, content="", error=f"File not found: {path}")
            return ToolResult(success=False, content="", error=error_msg)
