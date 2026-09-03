"""Resolve current files selected for presentation from a Session root."""

from __future__ import annotations

import json
import posixpath
import shlex
from datetime import datetime, timezone
from typing import Any


_IGNORED_DIRECTORIES = {
    ".git",
    ".opencapybox-edit",
    ".opencapybox-preview",
    ".workspace-snapshots",
    "__pycache__",
    "node_modules",
}


def _extract_stdout(execution: Any) -> str:
    logs = getattr(execution, "logs", None)
    lines = getattr(logs, "stdout", None) if logs is not None else None
    if not lines:
        return ""
    if isinstance(lines, str):
        return lines
    return "\n".join(
        str(getattr(line, "text", line))
        for line in lines
    )


def _normalize_root(workspace_dir: str) -> str:
    root = posixpath.normpath(workspace_dir)
    if not root.startswith("/"):
        raise ValueError("Session execution root must be absolute")
    return root


def _is_visible_relative_path(relative_path: str) -> bool:
    segments = [segment for segment in relative_path.split("/") if segment]
    return bool(segments) and all(
        not segment.startswith(".") and segment not in _IGNORED_DIRECTORIES
        for segment in segments
    )


def _reference_from_row(root: str, row: dict[str, Any]) -> dict[str, Any] | None:
    full_path = posixpath.normpath(str(row.get("path") or ""))
    if full_path == root or not full_path.startswith(root + "/"):
        return None
    relative_path = full_path[len(root) + 1 :]
    if not _is_visible_relative_path(relative_path):
        return None
    size = row.get("size")
    mtime_ns = row.get("mtime_ns")
    mtime = row.get("mtime")
    if not isinstance(size, int) or size < 0 or not isinstance(mtime_ns, int):
        return None
    try:
        modified = datetime.fromtimestamp(float(mtime), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None
    name = posixpath.basename(relative_path)
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "source": "session",
        "name": name,
        "path": relative_path,
        "size": size,
        "modified": modified,
        "type": extension,
        "revision": f"v1:{size}:{mtime_ns}",
    }


async def stat_session_file_reference(
    sandbox: Any,
    workspace_dir: str,
    full_path: str,
) -> dict[str, Any] | None:
    """Return one no-follow file identity below ``workspace_dir``."""

    root = _normalize_root(workspace_dir)
    normalized_path = posixpath.normpath(full_path)
    if normalized_path == root or not normalized_path.startswith(root + "/"):
        return None
    relative_path = normalized_path[len(root) + 1 :]
    if not _is_visible_relative_path(relative_path):
        return None
    script = (
        "import json,os,stat,sys\n"
        f"path={normalized_path!r}\n"
        "try:\n"
        " st=os.lstat(path)\n"
        "except (FileNotFoundError,OSError):\n"
        " sys.exit(2)\n"
        "if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):\n"
        " sys.exit(3)\n"
        "print(json.dumps({'path':path,'size':int(st.st_size),'mtime':float(st.st_mtime),'mtime_ns':int(st.st_mtime_ns)},separators=(',',':')))\n"
    )
    execution = await sandbox.commands.run("python3 -c " + shlex.quote(script))
    if getattr(execution, "error", None):
        return None
    stdout = _extract_stdout(execution).strip()
    if not stdout:
        return None
    try:
        row = json.loads(stdout.splitlines()[-1])
    except (json.JSONDecodeError, TypeError):
        return None
    return _reference_from_row(root, row) if isinstance(row, dict) else None
