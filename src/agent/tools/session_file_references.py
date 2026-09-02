"""Structured identities for mutable files in one Session execution root."""

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
_MAX_SCANNED_FILES = 400
_MAX_SCAN_DEPTH = 6


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


async def snapshot_session_files(
    sandbox: Any,
    workspace_dir: str,
) -> dict[str, dict[str, Any]] | None:
    """Capture bounded metadata for visible regular files below one Session root."""

    root = _normalize_root(workspace_dir)
    script = (
        "import json,os,stat\n"
        f"root={root!r}\n"
        f"ignored={sorted(_IGNORED_DIRECTORIES)!r}\n"
        f"max_depth={_MAX_SCAN_DEPTH}\n"
        f"limit={_MAX_SCANNED_FILES}\n"
        "rows=[]\n"
        "for current,dirs,files in os.walk(root,topdown=True,followlinks=False):\n"
        " rel_dir=os.path.relpath(current,root)\n"
        " depth=0 if rel_dir=='.' else len(rel_dir.split(os.sep))\n"
        " dirs[:]=[name for name in dirs if not name.startswith('.') and name not in ignored and depth < max_depth and not os.path.islink(os.path.join(current,name))]\n"
        " dirs.sort()\n"
        " for name in sorted(files):\n"
        "  if name.startswith('.'):\n"
        "   continue\n"
        "  path=os.path.join(current,name)\n"
        "  try:\n"
        "   st=os.lstat(path)\n"
        "  except OSError:\n"
        "   continue\n"
        "  if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):\n"
        "   continue\n"
        "  rows.append({'path':path,'size':int(st.st_size),'mtime':float(st.st_mtime),'mtime_ns':int(st.st_mtime_ns)})\n"
        "  if len(rows)>=limit:\n"
        "   break\n"
        " if len(rows)>=limit:\n"
        "  break\n"
        "print(json.dumps({'rows':rows,'truncated':len(rows)>=limit},separators=(',',':')))\n"
    )
    execution = await sandbox.commands.run("python3 -c " + shlex.quote(script))
    if getattr(execution, "error", None):
        return None
    stdout = _extract_stdout(execution).strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("truncated") is True:
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None
    references: dict[str, dict[str, Any]] = {}
    for row in rows:
        reference = _reference_from_row(root, row) if isinstance(row, dict) else None
        if reference is not None:
            references[str(reference["path"])] = reference
    return references


def changed_session_file_references(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return created or version-changed files, sorted by relative path."""

    changed: list[dict[str, Any]] = []
    for path, reference in sorted(after.items()):
        previous = before.get(path)
        if previous is not None and previous.get("revision") == reference.get("revision"):
            continue
        changed.append({
            **reference,
            "operation": "CREATED" if previous is None else "UPDATED",
        })
    for path, reference in sorted(before.items()):
        if path not in after:
            changed.append({**reference, "operation": "DELETED"})
    return changed
