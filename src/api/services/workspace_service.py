"""Persistent workspace boundary shared by HTTP, chat, and cron callers.

The database owns stable identities and revisions.  File bytes stay inside the
user's OpenSandbox persistent mount and are installed with same-filesystem
temporary files plus ``os.replace``.  Every filesystem script rejects symlink
components before touching a workspace path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import posixpath
import re
import textwrap
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Any, Callable, Iterable, Literal

from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession, sessionmaker

from src.api.config import get_settings
from src.api.models.agui_event import AGUIEventLog
from src.api.models.database import SessionLocal
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.models.user_run_lock import UserRunLock
from src.api.models.user_sandbox import UserSandbox
from src.api.models.workspace import (
    UserWorkspace,
    WorkspaceChangeSet,
    WorkspaceClaim,
    WorkspaceContentObject,
    WorkspaceContentReference,
    WorkspaceEntry,
    WorkspaceFileVersion,
    WorkspaceMutation,
)
from src.api.services.sandbox_profile_service import resolve_sandbox_runtime_config
from src.api.services.sandbox_service import get_sandbox_mount_path, get_sandbox_service
from src.api.services.file_preview_service import office_preview_cache_keys
from src.api.services.spreadsheet_edit_validation import (
    validate_csv_edit_payload,
    validate_xlsx_edit_payload,
)
from src.api.services.workspace_mutation_coordinator import (
    WorkspaceClaimConflict,
    WorkspaceClaimLease,
    WorkspaceClaimLost,
    WorkspaceClaimSpec,
    WorkspaceDraining,
    WorkspaceMutationCoordinator,
    file_scope,
    keep_workspace_claims_alive,
    path_scope,
    tree_scope,
)
from src.api.services.workspace_auto_merge import AutoMergeResult, merge_workspace_bytes
from src.api.utils.sandbox_helpers import extract_command_stdout
from src.api.utils.timezone import now_naive


logger = logging.getLogger(__name__)


WORKSPACE_DIRECTORY = "workdir"
WORKSPACE_SYSTEM_DIRECTORY = ".opencapybox"
WORKSPACE_TEMP_DIRECTORY = f"{WORKSPACE_SYSTEM_DIRECTORY}/tmp"
WORKSPACE_OBJECT_DIRECTORY = f"{WORKSPACE_SYSTEM_DIRECTORY}/objects/sha256"
WORKSPACE_OFFICE_CACHE_DIRECTORY = f"{WORKSPACE_SYSTEM_DIRECTORY}/derived/office"
WORKSPACE_FENCE_DIRECTORY = f"{WORKSPACE_SYSTEM_DIRECTORY}/mutation-fences"
_ROOT_PARENT_KEY = ""
_SESSION_SOURCE_PREFIX = "sessions/"
_CRON_SOURCE_PREFIX = "cron/runs/"
_SOURCE_REVISION_RE = re.compile(r"^v1:(\d+):(\d+)$")
MAX_WORKSPACE_DIRECTORY_DEPTH = 2


async def _complete_snapshot_before_cancellation(awaitable: Any) -> Any:
    """Do not let an HTTP disconnect kill the remote script between link and replace."""
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def _workspace_path_depth(relative_path: str) -> int:
    return len([part for part in relative_path.split("/") if part])


class WorkspaceError(RuntimeError):
    """Domain failure carrying a stable API error code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        entry: WorkspaceEntry | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.entry = entry
        self.extra = extra or {}


@dataclass(frozen=True)
class WorkspaceEntryPage:
    items: list[WorkspaceEntry]
    next_cursor: str | None
    workspace_revision: int


@dataclass(frozen=True)
class WorkspaceMutationResult:
    status: str
    entry: WorkspaceEntry
    mutation_id: str
    auto_merged: bool = False


@dataclass(frozen=True)
class WorkspaceDeleteResult:
    mutation_id: str
    roots: tuple[dict[str, Any], ...]
    affected_entry_ids: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceChangeSetResult:
    status: str
    change_set_id: str
    entry: WorkspaceEntry | None = None
    mutation_id: str | None = None
    base_version_id: str | None = None
    proposed_version_id: str | None = None
    applied_version_id: str | None = None
    mutation_status: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    target_name: str | None = None
    target_path: str | None = None


@dataclass(frozen=True)
class WorkspaceChangeSetContent:
    change_set: WorkspaceChangeSet
    sandbox: Any
    sandbox_path: str
    filename: str
    size_bytes: int
    mime_type: str | None


@dataclass(frozen=True)
class WorkspacePreparedMutation:
    """Pure values safe to carry while no request DB transaction is open."""

    mutation_id: str
    user_id: str
    leases: tuple[WorkspaceClaimLease, ...]


@dataclass(frozen=True)
class WorkspaceVersionSnapshotPlan:
    version_row: dict[str, Any]
    source_relative_path: str


@dataclass(frozen=True)
class WorkspaceStageResult:
    entry: WorkspaceEntry
    destination_path: str
    destination_relative_path: str
    source_revision: int
    sha256: str | None
    size_bytes: int
    tree_revision: int | None = None
    version_id: str | None = None
    version_sequence: int | None = None


@dataclass(frozen=True)
class WorkspaceContent:
    entry: WorkspaceEntry
    sandbox: Any
    sandbox_path: str
    workspace_root: str


@dataclass(frozen=True)
class WorkspaceVersionContent:
    version: WorkspaceFileVersion
    sandbox: Any
    sandbox_path: str
    workspace_root: str
    name: str


@dataclass(frozen=True)
class WorkspaceHeadContent:
    version_id: str
    blob_id: str | None
    content_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class WorkspaceHistoryGcResult:
    versions_pruned: int
    objects_pruned: int
    bytes_reclaimed: int
    protected_versions: int


@dataclass(frozen=True)
class SandboxFileStat:
    size_bytes: int
    mtime_ns: int
    # None when the probe intentionally skips the O(n) content digest.
    sha256: str | None

    @property
    def source_revision(self) -> str:
        return f"v1:{self.size_bytes}:{self.mtime_ns}"


@dataclass(frozen=True)
class WorkspacePathState:
    kind: Literal["file", "directory"]
    sha256: str | None


class WorkspacePathPolicy:
    """Canonical path and filename validation for the workspace boundary."""

    @staticmethod
    def validate_name(name: str) -> str:
        if not isinstance(name, str):
            raise WorkspaceError(422, "INVALID_NAME", "文件名必须是字符串")
        normalized = unicodedata.normalize("NFC", name).strip()
        if not normalized or normalized in {".", ".."}:
            raise WorkspaceError(422, "INVALID_NAME", "文件名不能为空")
        if "\x00" in normalized or "/" in normalized or "\\" in normalized:
            raise WorkspaceError(422, "INVALID_NAME", "文件名不能包含路径分隔符")
        if len(normalized.encode("utf-8")) > 255:
            raise WorkspaceError(422, "INVALID_NAME", "文件名超过 255 字节")
        if normalized.startswith(WORKSPACE_SYSTEM_DIRECTORY):
            raise WorkspaceError(422, "RESERVED_NAME", "该名称由工作区系统保留")
        return normalized

    @classmethod
    def normalize_relative_path(
        cls,
        path: str,
        *,
        allow_empty: bool = False,
        allow_system: bool = False,
    ) -> str:
        if not isinstance(path, str) or "\x00" in path or "\\" in path:
            raise WorkspaceError(400, "INVALID_PATH", "文件路径不合法")
        if path.startswith("/"):
            raise WorkspaceError(400, "INVALID_PATH", "工作区路径必须是相对路径")
        raw_parts = path.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            if allow_empty and path == "":
                return ""
            raise WorkspaceError(400, "INVALID_PATH", "文件路径不合法")
        parts: list[str] = []
        for index, part in enumerate(raw_parts):
            if allow_system and index == 0 and part.startswith(WORKSPACE_SYSTEM_DIRECTORY):
                parts.append(part)
            else:
                parts.append(cls.validate_name(part))
        normalized = "/".join(parts)
        if not allow_system and (
            normalized == WORKSPACE_SYSTEM_DIRECTORY
            or normalized.startswith(WORKSPACE_SYSTEM_DIRECTORY + "/")
        ):
            raise WorkspaceError(403, "RESERVED_PATH", "工作区系统目录不可访问")
        if len(normalized.encode("utf-8")) > 2000:
            raise WorkspaceError(422, "PATH_TOO_LONG", "文件路径过长")
        return normalized

    @classmethod
    def join(cls, parent_path: str | None, name: str) -> str:
        safe_name = cls.validate_name(name)
        if not parent_path:
            return safe_name
        safe_parent = cls.normalize_relative_path(parent_path)
        return cls.normalize_relative_path(f"{safe_parent}/{safe_name}")

    @staticmethod
    def normalize_revision(value: int | str | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise WorkspaceError(422, "INVALID_REVISION", "文件版本无效")
        if isinstance(value, str):
            value = value.strip().strip('"')
        try:
            revision = int(value)
        except (TypeError, ValueError) as exc:
            raise WorkspaceError(422, "INVALID_REVISION", "文件版本无效") from exc
        if revision < 1:
            raise WorkspaceError(422, "INVALID_REVISION", "文件版本无效")
        return revision

    @staticmethod
    def normalize_external_relative_path(path: str) -> str:
        """Validate a path below the user's mount without reserving dot names."""
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise WorkspaceError(400, "INVALID_SOURCE_PATH", "源文件路径不合法")
        if "\x00" in path or "\\" in path:
            raise WorkspaceError(400, "INVALID_SOURCE_PATH", "源文件路径不合法")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise WorkspaceError(400, "INVALID_SOURCE_PATH", "源文件路径不合法")
        if any(len(part.encode("utf-8")) > 255 for part in parts):
            raise WorkspaceError(422, "PATH_TOO_LONG", "源文件路径过长")
        return "/".join(parts)


def _execution_exit_code(execution: Any) -> int:
    exit_code = getattr(execution, "exit_code", None)
    if isinstance(exit_code, int):
        return exit_code
    raise WorkspaceError(
        503,
        "SANDBOX_RESPONSE_INVALID",
        "沙箱命令未返回有效退出状态",
    )


class WorkspaceStore:
    """OpenSandbox-backed byte store with no-follow path traversal."""

    def __init__(self, sandbox: Any, workspace_root: str) -> None:
        if not workspace_root.startswith("/"):
            raise ValueError("workspace_root must be absolute")
        self.sandbox = sandbox
        self.workspace_root = posixpath.normpath(workspace_root)
        self._claim_fence: tuple[WorkspaceClaimLease, ...] = ()

    def absolute_path(self, relative_path: str) -> str:
        safe = WorkspacePathPolicy.normalize_relative_path(
            relative_path,
            allow_system=relative_path.startswith(WORKSPACE_SYSTEM_DIRECTORY + "/"),
        )
        return posixpath.join(self.workspace_root, safe)

    async def _run_json_script(self, body: str) -> dict[str, Any]:
        if self._claim_fence:
            fence_items = [
                {
                    "scope_key": lease.scope_key,
                    "owner_token": lease.owner_token,
                    "generation": int(lease.generation),
                }
                for lease in sorted(self._claim_fence, key=lambda item: item.scope_key)
            ]
            fence_root = posixpath.join(self.workspace_root, WORKSPACE_FENCE_DIRECTORY)
            body = f"""import fcntl, hashlib, json, os, stat, sys, uuid
fence_root = {fence_root!r}
fence_items = {fence_items!r}
fence_fds = []
def fence_fail():
    print(json.dumps({{"ok": False, "code": "MUTATION_FENCED", "message": "工作区修改所有权已失效"}}))
    raise SystemExit(0)
def publish_marker(marker_path, payload):
    temp_path = marker_path + '.' + uuid.uuid4().hex + '.tmp'
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(payload, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, marker_path)
    finally:
        try: os.unlink(temp_path)
        except FileNotFoundError: pass
try:
    if os.path.lexists(fence_root) and os.path.islink(fence_root):
        fence_fail()
    os.makedirs(fence_root, mode=0o700, exist_ok=True)
    for item in fence_items:
        scope_hash = hashlib.sha256(item['scope_key'].encode('utf-8')).hexdigest()
        lock_path = os.path.join(fence_root, scope_hash + '.lock')
        marker_path = os.path.join(fence_root, scope_hash + '.json')
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fence_fds.append(lock_fd)
        try:
            marker_fd = os.open(marker_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            marker = None
        else:
            with os.fdopen(marker_fd, 'r', encoding='utf-8') as source:
                marker = json.load(source)
        expected_generation = int(item['generation'])
        if marker is not None:
            current_generation = int(marker.get('generation') or 0)
            current_token = str(marker.get('owner_token') or '')
            if current_generation > expected_generation:
                fence_fail()
            if current_generation == expected_generation and current_token != item['owner_token']:
                fence_fail()
        if marker is None or int(marker.get('generation') or 0) < expected_generation:
            publish_marker(marker_path, {{
                'scope_key': item['scope_key'],
                'owner_token': item['owner_token'],
                'generation': expected_generation,
            }})
    try:
{textwrap.indent(body, '        ')}
    finally:
        pass
finally:
    for fence_fd in reversed(fence_fds):
        try: fcntl.flock(fence_fd, fcntl.LOCK_UN)
        finally: os.close(fence_fd)
"""
        command = "python3 - <<'PY'\n" + body + "\nPY"
        execution = await self.sandbox.commands.run(command)
        stdout = extract_command_stdout(execution).strip()
        if _execution_exit_code(execution) != 0:
            raise WorkspaceError(503, "SANDBOX_OPERATION_FAILED", "沙箱文件操作失败")
        try:
            payload = json.loads(stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(503, "SANDBOX_RESPONSE_INVALID", "沙箱文件操作响应无效") from exc
        if not isinstance(payload, dict):
            raise WorkspaceError(503, "SANDBOX_RESPONSE_INVALID", "沙箱文件操作响应无效")
        if payload.get("ok") is False:
            code = str(payload.get("code") or "SANDBOX_OPERATION_FAILED")
            status = {
                "NOT_FOUND": 404,
                "NAME_CONFLICT": 409,
                "SOURCE_REVISION_CONFLICT": 412,
                "DESTINATION_CHANGED": 409,
                "SYMLINK_REJECTED": 400,
                "NOT_DIRECTORY": 400,
                "NOT_FILE": 400,
                "OBJECT_COLLISION": 409,
                "STAGE_MANIFEST_MISMATCH": 409,
                "MUTATION_FENCED": 409,
            }.get(code, 503)
            raise WorkspaceError(status, code, str(payload.get("message") or "沙箱文件操作失败"), extra=payload)
        return payload

    @asynccontextmanager
    async def claim_fence(self, leases: Iterable[WorkspaceClaimLease]):
        normalized = tuple(sorted(leases, key=lambda item: item.scope_key))
        if self._claim_fence:
            raise RuntimeError("WorkspaceStore claim fence cannot be nested")
        self._claim_fence = normalized
        try:
            yield self
        finally:
            self._claim_fence = ()

    async def advance_claim_fences(
        self,
        previous_leases: Iterable[WorkspaceClaimLease],
        current_leases: Iterable[WorkspaceClaimLease],
    ) -> tuple[WorkspaceClaimLease, ...]:
        """Make a committed DB takeover authoritative at the filesystem boundary."""
        old_leases = tuple(sorted(previous_leases, key=lambda item: item.scope_key))
        new_leases = tuple(sorted(current_leases, key=lambda item: item.scope_key))
        if not old_leases:
            return ()
        if len(old_leases) != len(new_leases):
            raise WorkspaceError(409, "MUTATION_FENCED", "工作区 fence 接管范围无效")
        if self._claim_fence:
            raise RuntimeError("Cannot advance claim fences inside an active fence")
        current_by_claim = {lease.claim_id: lease for lease in new_leases}
        if {lease.claim_id for lease in old_leases} != set(current_by_claim):
            raise WorkspaceError(409, "MUTATION_FENCED", "工作区 fence 接管 claim 无效")
        items = [
            {
                "claim_id": lease.claim_id,
                "user_id": lease.user_id,
                "scope_kind": lease.scope_kind,
                "scope_key": lease.scope_key,
                "old_owner_token": lease.owner_token,
                "old_generation": int(lease.generation),
                "target_owner_token": current_by_claim[lease.claim_id].owner_token,
                "target_generation": int(current_by_claim[lease.claim_id].generation),
            }
            for lease in old_leases
        ]
        if any(
            current_by_claim[item.claim_id].scope_key != item.scope_key
            or int(current_by_claim[item.claim_id].generation) != int(item.generation) + 1
            for item in old_leases
        ):
            raise WorkspaceError(409, "MUTATION_FENCED", "工作区 fence 接管代际无效")
        fence_root = posixpath.join(self.workspace_root, WORKSPACE_FENCE_DIRECTORY)
        payload = await self._run_json_script(
            f"""import fcntl, hashlib, json, os, sys, uuid
fence_root = {fence_root!r}
items = {items!r}
lock_fds = []
markers = []
def fail(message):
    print(json.dumps({{"ok": False, "code": "MUTATION_FENCED", "message": message}}))
    raise SystemExit(0)
def publish(marker_path, value):
    temp_path = marker_path + '.' + uuid.uuid4().hex + '.tmp'
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, marker_path)
    finally:
        try: os.unlink(temp_path)
        except FileNotFoundError: pass
try:
    if os.path.lexists(fence_root) and os.path.islink(fence_root):
        fail('工作区 fence 目录无效')
    os.makedirs(fence_root, mode=0o700, exist_ok=True)
    for item in items:
        scope_hash = hashlib.sha256(item['scope_key'].encode('utf-8')).hexdigest()
        lock_path = os.path.join(fence_root, scope_hash + '.lock')
        marker_path = os.path.join(fence_root, scope_hash + '.json')
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_fds.append(lock_fd)
        try:
            marker_fd = os.open(marker_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            marker = None
        else:
            with os.fdopen(marker_fd, 'r', encoding='utf-8') as source:
                marker = json.load(source)
        markers.append((item, marker_path, marker))
    for item, _marker_path, marker in markers:
        if marker is None:
            continue
        generation = int(marker.get('generation') or 0)
        token = str(marker.get('owner_token') or '')
        if generation > item['target_generation']:
            fail('工作区 fence 已由更新 owner 推进')
        if generation == item['target_generation']:
            if token != item['target_owner_token']:
                fail('工作区 fence 已由其他 owner 接管')
        elif generation == item['old_generation'] and token != item['old_owner_token']:
            fail('工作区 fence owner 与数据库不一致')
    for item, marker_path, marker in markers:
        generation = int((marker or {{}}).get('generation') or 0)
        token = str((marker or {{}}).get('owner_token') or '')
        if generation != item['target_generation'] or token != item['target_owner_token']:
            publish(marker_path, {{
                'scope_key': item['scope_key'],
                'owner_token': item['target_owner_token'],
                'generation': item['target_generation'],
            }})
    print(json.dumps({{'ok': True}}, separators=(',', ':')))
finally:
    for lock_fd in reversed(lock_fds):
        try: fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally: os.close(lock_fd)
"""
        )
        return new_leases

    async def ensure_root(self) -> None:
        root = self.workspace_root
        system = posixpath.join(root, WORKSPACE_SYSTEM_DIRECTORY)
        temp = posixpath.join(root, WORKSPACE_TEMP_DIRECTORY)
        payload = await self._run_json_script(
            f"""import json, os, stat
root = {root!r}
paths = [{system!r}, {temp!r}, {posixpath.join(root, WORKSPACE_FENCE_DIRECTORY)!r}]
try:
    if os.path.lexists(root) and os.path.islink(root):
        print(json.dumps({{"ok": False, "code": "SYMLINK_REJECTED", "message": "工作区根目录不能是符号链接"}}))
    else:
        os.makedirs(root, mode=0o700, exist_ok=True)
        current = root
        rejected = False
        for path in paths:
            if os.path.lexists(path) and os.path.islink(path):
                rejected = True
                break
            os.makedirs(path, mode=0o700, exist_ok=True)
        if rejected:
            print(json.dumps({{"ok": False, "code": "SYMLINK_REJECTED", "message": "工作区系统目录不能是符号链接"}}))
        else:
            print(json.dumps({{"ok": True}}))
except OSError as exc:
    print(json.dumps({{"ok": False, "code": "IO_ERROR", "message": str(exc)}}))"""
        )
        if not payload.get("ok"):
            raise WorkspaceError(503, "WORKSPACE_INIT_FAILED", "无法初始化工作区")

    @staticmethod
    def _safe_walk_source() -> str:
        return """
def open_dir_chain(root, relative):
    if os.path.islink(root):
        raise RuntimeError('SYMLINK_REJECTED')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in ([p for p in relative.split('/') if p]):
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise
"""

    async def stat(self, relative_path: str) -> SandboxFileStat:
        safe = WorkspacePathPolicy.normalize_relative_path(relative_path, allow_system=True)
        parent, name = posixpath.split(safe)
        payload = await self._run_json_script(
            f"""import hashlib, json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
parent = {parent!r}
name = {name!r}
try:
    parent_fd = open_dir_chain(root, parent)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeError('NOT_FILE')
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(fd)
            if after.st_size != st.st_size or after.st_mtime_ns != st.st_mtime_ns:
                print(json.dumps({{"ok": False, "code": "SOURCE_REVISION_CONFLICT", "message": "读取期间文件发生变化"}}))
            else:
                print(json.dumps({{"ok": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest.hexdigest()}}))
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "文件不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "文件类型或路径不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return SandboxFileStat(
            size_bytes=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            sha256=str(payload["sha256"]),
        )

    async def stat_external(self, root: str, relative_path: str) -> SandboxFileStat:
        safe = WorkspacePathPolicy.normalize_external_relative_path(relative_path)
        parent, name = posixpath.split(safe)
        payload = await self._run_json_script(
            f"""import hashlib, json, os, stat
{self._safe_walk_source()}
root = {root!r}
parent = {parent!r}
name = {name!r}
try:
    parent_fd = open_dir_chain(root, parent)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeError('NOT_FILE')
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(fd)
            if after.st_size != st.st_size or after.st_mtime_ns != st.st_mtime_ns:
                print(json.dumps({{"ok": False, "code": "SOURCE_REVISION_CONFLICT", "message": "读取期间源文件发生变化"}}))
            else:
                print(json.dumps({{"ok": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest.hexdigest()}}))
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "源文件不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "源文件类型或路径不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return SandboxFileStat(
            size_bytes=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            sha256=str(payload["sha256"]),
        )

    async def read_bytes(self, relative_path: str, *, allow_system: bool = False) -> bytes:
        safe = WorkspacePathPolicy.normalize_relative_path(
            relative_path,
            allow_system=allow_system,
        )
        stream = await self.sandbox.files.read_bytes_stream(
            self.absolute_path(safe),
            chunk_size=64 * 1024,
        )
        if isinstance(stream, bytes):
            return stream
        content = bytearray()
        async for chunk in stream:
            content.extend(chunk)
        return bytes(content)

    async def inspect_path(
        self,
        relative_path: str,
        *,
        allow_system: bool = False,
    ) -> WorkspacePathState:
        safe = WorkspacePathPolicy.normalize_relative_path(
            relative_path,
            allow_system=allow_system,
        )
        parent, name = posixpath.split(safe)
        payload = await self._run_json_script(
            f"""import hashlib, json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
parent = {parent!r}
name = {name!r}
try:
    parent_fd = open_dir_chain(root, parent)
    try:
        item_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(item_stat.st_mode):
            raise RuntimeError('SYMLINK_REJECTED')
        if stat.S_ISDIR(item_stat.st_mode):
            print(json.dumps({{"ok": True, "kind": "directory", "sha256": None}}))
        elif stat.S_ISREG(item_stat.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                print(json.dumps({{"ok": True, "kind": "file", "sha256": digest.hexdigest()}}))
            finally:
                os.close(fd)
        else:
            raise RuntimeError('NOT_FILE')
    finally:
        os.close(parent_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "条目不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "条目类型或路径不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return WorkspacePathState(
            kind=str(payload["kind"]),
            sha256=payload.get("sha256"),
        )

    async def mkdir(self, relative_path: str) -> None:
        safe = WorkspacePathPolicy.normalize_relative_path(relative_path)
        parent, name = posixpath.split(safe)
        await self._run_json_script(
            f"""import json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
parent = {parent!r}
name = {name!r}
try:
    parent_fd = open_dir_chain(root, parent)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        print(json.dumps({{"ok": True}}))
    finally:
        os.close(parent_fd)
except FileExistsError:
    print(json.dumps({{"ok": False, "code": "NAME_CONFLICT", "message": "目标名称已存在"}}))
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "父目录不存在"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )

    async def _create_empty_file_atomic(
        self,
        relative_path: str,
        *,
        temp_token: str | None = None,
    ) -> SandboxFileStat:
        parent, name = posixpath.split(relative_path)
        temp_name = f".{temp_token or uuid.uuid4().hex}.empty"
        payload = await self._run_json_script(
            f"""import hashlib, json, os
{self._safe_walk_source()}
root = {self.workspace_root!r}
parent = {parent!r}
name = {name!r}
temp_name = {temp_name!r}
temp_created = False
try:
    parent_fd = open_dir_chain(root, parent)
    try:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            print(json.dumps({{"ok": False, "code": "NAME_CONFLICT", "message": "目标名称已存在"}}))
        except FileNotFoundError:
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            temp_created = True
            try:
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)
            os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temp_created = False
            os.fsync(parent_fd)
            installed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            print(json.dumps({{"ok": True, "size": installed.st_size, "mtime_ns": installed.st_mtime_ns, "sha256": hashlib.sha256(b'').hexdigest()}}))
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "目标父目录不存在"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return SandboxFileStat(
            size_bytes=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            sha256=str(payload["sha256"]),
        )

    async def write_bytes_atomic(
        self,
        relative_path: str,
        content: bytes,
        *,
        expected_sha256: str | None = None,
        must_not_exist: bool = False,
        temp_token: str | None = None,
    ) -> SandboxFileStat:
        safe = WorkspacePathPolicy.normalize_relative_path(relative_path)
        if must_not_exist and not content:
            return await self._create_empty_file_atomic(
                safe,
                temp_token=temp_token,
            )
        temp_name = (temp_token or uuid.uuid4().hex) + ".tmp"
        temp_relative = f"{WORKSPACE_TEMP_DIRECTORY}/{temp_name}"
        temp_absolute = self.absolute_path(temp_relative)
        await self.sandbox.files.write_file(temp_absolute, content)
        try:
            return await self._install_temp(
                temp_relative,
                safe,
                expected_sha256=expected_sha256,
                must_not_exist=must_not_exist,
            )
        except Exception:
            try:
                await self.sandbox.commands.run(f"rm -f -- {json.dumps(temp_absolute)}")
            except Exception:
                pass
            raise

    async def stage_bytes_for_install(
        self,
        content: bytes,
        *,
        temp_token: str,
    ) -> str:
        temp_relative = f"{WORKSPACE_TEMP_DIRECTORY}/{temp_token}.bytes-staged"
        await self.sandbox.files.write_file(self.absolute_path(temp_relative), content)
        return temp_relative

    async def stage_upload_stream(
        self,
        source: Any,
        *,
        max_bytes: int,
    ) -> tuple[str, SandboxFileStat]:
        """Spool an ``UploadFile`` into one verified same-filesystem temp file."""
        upload_id = uuid.uuid4().hex
        part_names: list[str] = []
        total = 0
        temp_name = upload_id + ".tmp"
        temp_relative = f"{WORKSPACE_TEMP_DIRECTORY}/{temp_name}"
        temp_absolute = self.absolute_path(temp_relative)
        staged_ready = False
        try:
            index = 0
            while True:
                chunk = await source.read(4 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise WorkspaceError(413, "FILE_TOO_LARGE", "文件超过工作区单文件大小限制")
                part_name = f"{upload_id}.{index:06d}.part"
                part_names.append(part_name)
                part_absolute = self.absolute_path(f"{WORKSPACE_TEMP_DIRECTORY}/{part_name}")
                await self.sandbox.files.write_file(part_absolute, chunk)
                index += 1

            payload = await self._run_json_script(
                f"""import hashlib, json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
temp_parent = {WORKSPACE_TEMP_DIRECTORY!r}
part_names = {part_names!r}
temp_name = {temp_name!r}
created = False
try:
    temp_fd = open_dir_chain(root, temp_parent)
    try:
        out_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=temp_fd)
        created = True
        digest = hashlib.sha256()
        try:
            for part_name in part_names:
                part_fd = os.open(part_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=temp_fd)
                try:
                    part_stat = os.fstat(part_fd)
                    if not stat.S_ISREG(part_stat.st_mode):
                        raise RuntimeError('NOT_FILE')
                    while True:
                        chunk = os.read(part_fd, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(out_fd, view)
                            view = view[written:]
                finally:
                    os.close(part_fd)
            os.fsync(out_fd)
        finally:
            os.close(out_fd)
        result = os.stat(temp_name, dir_fd=temp_fd, follow_symlinks=False)
        print(json.dumps({{"ok": True, "size": result.st_size, "mtime_ns": result.st_mtime_ns, "sha256": digest.hexdigest()}}))
    finally:
        for part_name in part_names:
            try:
                os.unlink(part_name, dir_fd=temp_fd)
            except OSError:
                pass
        os.close(temp_fd)
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "上传临时文件类型异常"}}))
except OSError as exc:
    print(json.dumps({{"ok": False, "code": "IO_ERROR", "message": str(exc)}}))"""
            )
            staged = SandboxFileStat(
                size_bytes=int(payload["size"]),
                mtime_ns=int(payload["mtime_ns"]),
                sha256=str(payload["sha256"]),
            )
            staged_ready = True
            return temp_relative, staged
        finally:
            cleanup_paths = [
                self.absolute_path(f"{WORKSPACE_TEMP_DIRECTORY}/{part_name}")
                for part_name in part_names
            ]
            if not staged_ready:
                cleanup_paths.append(temp_absolute)
            if cleanup_paths:
                try:
                    quoted = " ".join(json.dumps(path) for path in cleanup_paths)
                    await self.sandbox.commands.run(f"rm -f -- {quoted}")
                except Exception:
                    pass

    async def write_upload_stream_atomic(
        self,
        relative_path: str,
        source: Any,
        *,
        max_bytes: int,
        before_install: Any,
        must_not_exist: bool = False,
    ) -> SandboxFileStat:
        """Compatibility wrapper for callers that do not own a mutation claim."""
        destination = WorkspacePathPolicy.normalize_relative_path(relative_path)
        temp_relative, staged = await self.stage_upload_stream(
            source,
            max_bytes=max_bytes,
        )
        temp_absolute = self.absolute_path(temp_relative)
        try:
            before_install(staged, temp_relative)
            installed = await self._install_temp(
                temp_relative,
                destination,
                expected_sha256=None,
                must_not_exist=must_not_exist,
            )
            if installed.sha256 != staged.sha256:
                raise WorkspaceError(503, "UPLOAD_HASH_MISMATCH", "上传文件校验失败")
            return installed
        finally:
            try:
                await self.sandbox.commands.run(f"rm -f -- {json.dumps(temp_absolute)}")
            except Exception:
                pass

    async def _install_temp(
        self,
        temp_relative: str,
        destination_relative: str,
        *,
        expected_sha256: str | None,
        must_not_exist: bool,
    ) -> SandboxFileStat:
        destination = WorkspacePathPolicy.normalize_relative_path(destination_relative)
        dest_parent, dest_name = posixpath.split(destination)
        temp_parent, temp_name = posixpath.split(temp_relative)
        payload = await self._run_json_script(
            f"""import hashlib, json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
dest_parent = {dest_parent!r}
dest_name = {dest_name!r}
temp_parent = {temp_parent!r}
temp_name = {temp_name!r}
expected_sha = {expected_sha256!r}
must_not_exist = {must_not_exist!r}
try:
    dest_fd = open_dir_chain(root, dest_parent)
    temp_fd = open_dir_chain(root, temp_parent)
    try:
        temp_file_fd = os.open(temp_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=temp_fd)
        try:
            temp_st = os.fstat(temp_file_fd)
            if not stat.S_ISREG(temp_st.st_mode):
                raise RuntimeError('NOT_FILE')
            temp_hash = hashlib.sha256()
            while True:
                chunk = os.read(temp_file_fd, 1024 * 1024)
                if not chunk:
                    break
                temp_hash.update(chunk)
            temp_sha = temp_hash.hexdigest()
        finally:
            os.close(temp_file_fd)
        try:
            current_fd = os.open(dest_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dest_fd)
        except FileNotFoundError:
            current_fd = None
        if current_fd is not None:
            try:
                current_st = os.fstat(current_fd)
                if not stat.S_ISREG(current_st.st_mode):
                    raise RuntimeError('NOT_FILE')
                current_hash = hashlib.sha256()
                while True:
                    chunk = os.read(current_fd, 1024 * 1024)
                    if not chunk:
                        break
                    current_hash.update(chunk)
                current_sha = current_hash.hexdigest()
            finally:
                os.close(current_fd)
            if must_not_exist:
                print(json.dumps({{"ok": False, "code": "NAME_CONFLICT", "message": "目标名称已存在", "sha256": current_sha}}))
            elif expected_sha is not None and current_sha != expected_sha:
                print(json.dumps({{"ok": False, "code": "DESTINATION_CHANGED", "message": "目标文件已被其他操作修改", "sha256": current_sha}}))
            else:
                os.replace(temp_name, dest_name, src_dir_fd=temp_fd, dst_dir_fd=dest_fd)
                os.fsync(dest_fd)
                st = os.stat(dest_name, dir_fd=dest_fd, follow_symlinks=False)
                print(json.dumps({{"ok": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": temp_sha}}))
        else:
            if expected_sha is not None:
                print(json.dumps({{"ok": False, "code": "DESTINATION_CHANGED", "message": "目标文件已被删除"}}))
            else:
                os.replace(temp_name, dest_name, src_dir_fd=temp_fd, dst_dir_fd=dest_fd)
                os.fsync(dest_fd)
                st = os.stat(dest_name, dir_fd=dest_fd, follow_symlinks=False)
                print(json.dumps({{"ok": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": temp_sha}}))
    finally:
        os.close(temp_fd)
        os.close(dest_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "目标父目录不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "目标文件类型不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return SandboxFileStat(
            size_bytes=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            sha256=str(payload["sha256"]),
        )

    async def install_staged_file(
        self,
        *,
        staged_relative_path: str,
        destination_relative_path: str,
        expected_destination_sha256: str | None,
        must_not_exist: bool,
    ) -> SandboxFileStat:
        return await self._install_temp(
            staged_relative_path,
            destination_relative_path,
            expected_sha256=expected_destination_sha256,
            must_not_exist=must_not_exist,
        )

    async def copy_external_atomic(
        self,
        *,
        source_root: str,
        source_relative_path: str,
        expected_source_revision: str,
        destination_relative_path: str,
        expected_destination_sha256: str | None = None,
        must_not_exist: bool = False,
        temp_token: str | None = None,
        allow_system_destination: bool = False,
    ) -> SandboxFileStat:
        """Copy a stable external snapshot into the workspace entirely in-sandbox."""
        source = WorkspacePathPolicy.normalize_external_relative_path(source_relative_path)
        destination = WorkspacePathPolicy.normalize_relative_path(
            destination_relative_path,
            allow_system=allow_system_destination,
        )
        source_parent, source_name = posixpath.split(source)
        dest_parent, dest_name = posixpath.split(destination)
        temp_name = (temp_token or uuid.uuid4().hex) + ".tmp"
        payload = await self._run_json_script(
            f"""import hashlib, json, os, stat
{self._safe_walk_source()}
source_root = {source_root!r}
source_parent = {source_parent!r}
source_name = {source_name!r}
workspace_root = {self.workspace_root!r}
dest_parent = {dest_parent!r}
dest_name = {dest_name!r}
temp_parent = {WORKSPACE_TEMP_DIRECTORY!r}
temp_name = {temp_name!r}
expected_source_revision = {expected_source_revision!r}
expected_destination_sha = {expected_destination_sha256!r}
must_not_exist = {must_not_exist!r}
temp_created = False
try:
    source_parent_fd = open_dir_chain(source_root, source_parent)
    dest_fd = open_dir_chain(workspace_root, dest_parent)
    temp_fd = open_dir_chain(workspace_root, temp_parent)
    try:
        source_fd = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent_fd)
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError('NOT_FILE')
            current_revision = f'v1:{{before.st_size}}:{{before.st_mtime_ns}}'
            if current_revision != expected_source_revision:
                print(json.dumps({{"ok": False, "code": "SOURCE_REVISION_CONFLICT", "message": "源文件已被其他操作修改", "current_revision": current_revision}}))
            else:
                out_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=temp_fd)
                temp_created = True
                digest = hashlib.sha256()
                try:
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(out_fd, view)
                            view = view[written:]
                    os.fsync(out_fd)
                finally:
                    os.close(out_fd)
                after = os.fstat(source_fd)
                after_revision = f'v1:{{after.st_size}}:{{after.st_mtime_ns}}'
                if after_revision != expected_source_revision:
                    print(json.dumps({{"ok": False, "code": "SOURCE_REVISION_CONFLICT", "message": "复制期间源文件发生变化", "current_revision": after_revision}}))
                else:
                    try:
                        current_fd = os.open(dest_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dest_fd)
                    except FileNotFoundError:
                        current_fd = None
                    current_sha = None
                    if current_fd is not None:
                        try:
                            current_st = os.fstat(current_fd)
                            if not stat.S_ISREG(current_st.st_mode):
                                raise RuntimeError('NOT_FILE')
                            current_hash = hashlib.sha256()
                            while True:
                                chunk = os.read(current_fd, 1024 * 1024)
                                if not chunk:
                                    break
                                current_hash.update(chunk)
                            current_sha = current_hash.hexdigest()
                        finally:
                            os.close(current_fd)
                    if current_sha is not None and must_not_exist:
                        print(json.dumps({{"ok": False, "code": "NAME_CONFLICT", "message": "目标名称已存在", "sha256": current_sha}}))
                    elif current_sha != expected_destination_sha:
                        print(json.dumps({{"ok": False, "code": "DESTINATION_CHANGED", "message": "目标文件已被其他操作修改", "sha256": current_sha}}))
                    else:
                        os.replace(temp_name, dest_name, src_dir_fd=temp_fd, dst_dir_fd=dest_fd)
                        temp_created = False
                        os.fsync(dest_fd)
                        installed = os.stat(dest_name, dir_fd=dest_fd, follow_symlinks=False)
                        print(json.dumps({{"ok": True, "size": installed.st_size, "mtime_ns": installed.st_mtime_ns, "sha256": digest.hexdigest()}}))
        finally:
            os.close(source_fd)
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=temp_fd)
            except OSError:
                pass
        os.close(temp_fd)
        os.close(dest_fd)
        os.close(source_parent_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "源文件或目标目录不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "源或目标文件类型不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return SandboxFileStat(
            size_bytes=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            sha256=str(payload["sha256"]),
        )

    async def copy_to_external_atomic(
        self,
        *,
        source_relative_path: str,
        expected_source_sha256: str | None,
        destination_root: str,
        destination_relative_path: str,
        no_clobber: bool = False,
    ) -> SandboxFileStat:
        """Copy one workspace file into a controlled Session/Cron directory."""
        source = WorkspacePathPolicy.normalize_relative_path(
            source_relative_path,
            allow_system=source_relative_path.startswith(WORKSPACE_SYSTEM_DIRECTORY + "/"),
        )
        destination = WorkspacePathPolicy.normalize_external_relative_path(destination_relative_path)
        source_parent, source_name = posixpath.split(source)
        dest_parent, dest_name = posixpath.split(destination)
        temp_name = f".{uuid.uuid4().hex}.tmp"
        payload = await self._run_json_script(
            f"""import hashlib, json, os, stat
{self._safe_walk_source()}
def open_or_create_dir_chain(root, relative):
    if os.path.islink(root):
        raise RuntimeError('SYMLINK_REJECTED')
    os.makedirs(root, mode=0o700, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in ([p for p in relative.split('/') if p]):
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=fd)
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise
workspace_root = {self.workspace_root!r}
source_parent = {source_parent!r}
source_name = {source_name!r}
destination_root = {destination_root!r}
dest_parent = {dest_parent!r}
dest_name = {dest_name!r}
temp_name = {temp_name!r}
expected_sha = {expected_source_sha256!r}
no_clobber = {no_clobber!r}
temp_created = False
try:
    source_parent_fd = open_dir_chain(workspace_root, source_parent)
    dest_fd = open_or_create_dir_chain(destination_root, dest_parent)
    try:
        source_fd = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent_fd)
        try:
            st = os.fstat(source_fd)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeError('NOT_FILE')
            out_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dest_fd)
            temp_created = True
            digest = hashlib.sha256()
            try:
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(out_fd, view)
                        view = view[written:]
                os.fsync(out_fd)
            finally:
                os.close(out_fd)
            source_sha = digest.hexdigest()
            if expected_sha is not None and source_sha != expected_sha:
                print(json.dumps({{"ok": False, "code": "SOURCE_REVISION_CONFLICT", "message": "工作区文件已在外部被修改", "sha256": source_sha}}))
            else:
                if no_clobber:
                    try:
                        os.link(
                            temp_name,
                            dest_name,
                            src_dir_fd=dest_fd,
                            dst_dir_fd=dest_fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        existing_fd = os.open(dest_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dest_fd)
                        try:
                            existing_digest = hashlib.sha256()
                            while True:
                                existing_chunk = os.read(existing_fd, 1024 * 1024)
                                if not existing_chunk:
                                    break
                                existing_digest.update(existing_chunk)
                            existing_stat = os.fstat(existing_fd)
                        finally:
                            os.close(existing_fd)
                        if existing_digest.hexdigest() != source_sha or existing_stat.st_size != st.st_size:
                            raise RuntimeError('OBJECT_COLLISION')
                    os.unlink(temp_name, dir_fd=dest_fd)
                    temp_created = False
                    os.chmod(
                        dest_name,
                        0o400,
                        dir_fd=dest_fd,
                        follow_symlinks=False,
                    )
                else:
                    os.replace(temp_name, dest_name, src_dir_fd=dest_fd, dst_dir_fd=dest_fd)
                    temp_created = False
                os.fsync(dest_fd)
                installed = os.stat(dest_name, dir_fd=dest_fd, follow_symlinks=False)
                print(json.dumps({{"ok": True, "size": installed.st_size, "mtime_ns": installed.st_mtime_ns, "sha256": source_sha}}))
        finally:
            os.close(source_fd)
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=dest_fd)
            except OSError:
                pass
        os.close(dest_fd)
        os.close(source_parent_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "工作区源文件不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "源或目标路径不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return SandboxFileStat(
            size_bytes=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            sha256=str(payload["sha256"]),
        )

    async def ensure_content_object(
        self,
        *,
        source_relative_path: str,
        destination_relative_path: str,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> SandboxFileStat:
        """Publish one immutable SHA object without replacing an existing object."""

        copied = await self.copy_to_external_atomic(
            source_relative_path=source_relative_path,
            expected_source_sha256=expected_sha256,
            destination_root=self.workspace_root,
            destination_relative_path=destination_relative_path,
            no_clobber=True,
        )
        if copied.sha256 != expected_sha256 or int(copied.size_bytes) != int(expected_size_bytes):
            raise WorkspaceError(409, "CONTENT_OBJECT_CHANGED", "内容对象校验失败")
        return copied

    async def ensure_external_directory(
        self,
        *,
        destination_root: str,
        destination_relative_path: str,
        must_not_exist: bool = False,
    ) -> None:
        """Create one no-follow directory chain in a Session/Cron root."""
        destination = WorkspacePathPolicy.normalize_external_relative_path(
            destination_relative_path
        )
        dest_parent, dest_name = posixpath.split(destination)
        await self._run_json_script(
            f"""import json, os
def open_or_create_dir_chain(root, relative):
    if os.path.islink(root):
        raise RuntimeError('SYMLINK_REJECTED')
    os.makedirs(root, mode=0o700, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in ([p for p in relative.split('/') if p]):
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=fd)
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise
root = {destination_root!r}
dest_parent = {dest_parent!r}
dest_name = {dest_name!r}
must_not_exist = {must_not_exist!r}
try:
    parent_fd = open_or_create_dir_chain(root, dest_parent)
    try:
        if must_not_exist:
            os.mkdir(dest_name, mode=0o700, dir_fd=parent_fd)
            target_fd = os.open(dest_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        else:
            try:
                target_fd = os.open(dest_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                os.mkdir(dest_name, mode=0o700, dir_fd=parent_fd)
                target_fd = os.open(dest_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        os.close(target_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    print(json.dumps({{"ok": True}}))
except FileExistsError:
    print(json.dumps({{"ok": False, "code": "NAME_CONFLICT", "message": "目标目录已存在"}}))
except NotADirectoryError:
    print(json.dumps({{"ok": False, "code": "NOT_DIRECTORY", "message": "目标路径不是目录"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "目标目录路径不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )

    async def inspect_external_directory_manifest(
        self,
        *,
        destination_root: str,
        directory_relative_path: str,
    ) -> list[dict[str, Any]]:
        """Hash a staged external tree before its single atomic publish."""
        directory = WorkspacePathPolicy.normalize_external_relative_path(
            directory_relative_path
        )
        payload = await self._run_json_script(
            f"""import hashlib, json, os, stat
{self._safe_walk_source()}
root = {destination_root!r}
directory = {directory!r}
manifest = []
def walk(directory_fd, prefix):
    for name in sorted(os.listdir(directory_fd)):
        item_path = f'{{prefix}}/{{name}}' if prefix else name
        item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(item_stat.st_mode):
            manifest.append({{"path": item_path, "kind": "directory"}})
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                walk(child_fd, item_path)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(item_stat.st_mode):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                before = os.fstat(file_fd)
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(file_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                after = os.fstat(file_fd)
                if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                    raise RuntimeError('SOURCE_REVISION_CONFLICT')
                manifest.append({{
                    "path": item_path,
                    "kind": "file",
                    "sha256": digest.hexdigest(),
                    "size_bytes": before.st_size,
                }})
            finally:
                os.close(file_fd)
        else:
            raise RuntimeError('SYMLINK_REJECTED')
try:
    directory_fd = open_dir_chain(root, directory)
    try:
        walk(directory_fd, '')
    finally:
        os.close(directory_fd)
    print(json.dumps({{"ok": True, "manifest": manifest}}))
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "暂存目录不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "暂存目录校验失败"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        manifest = payload.get("manifest")
        if not isinstance(manifest, list) or not all(
            isinstance(item, dict) for item in manifest
        ):
            raise WorkspaceError(503, "SANDBOX_RESPONSE_INVALID", "暂存目录校验响应无效")
        return manifest

    async def install_external_directory_atomic(
        self,
        *,
        destination_root: str,
        staged_relative_path: str,
        destination_relative_path: str,
        expected_manifest: list[dict[str, Any]],
    ) -> None:
        """Verify and no-clobber publish one complete incoming directory."""
        staged = WorkspacePathPolicy.normalize_external_relative_path(staged_relative_path)
        destination = WorkspacePathPolicy.normalize_external_relative_path(
            destination_relative_path
        )
        staged_parent, staged_name = posixpath.split(staged)
        destination_parent, destination_name = posixpath.split(destination)
        if staged_parent != destination_parent or not staged_name.startswith(".incoming-"):
            raise WorkspaceError(400, "INVALID_STAGE_PATH", "暂存目录必须位于目标同级 incoming 路径")
        normalized_manifest = sorted(
            (dict(item) for item in expected_manifest),
            key=lambda item: str(item.get("path") or ""),
        )
        await self._run_json_script(
            f"""import ctypes, errno, hashlib, json, os, stat
{self._safe_walk_source()}
root = {destination_root!r}
parent = {destination_parent!r}
staged_name = {staged_name!r}
destination_name = {destination_name!r}
expected_manifest = {normalized_manifest!r}
manifest = []
def walk(directory_fd, prefix):
    for name in sorted(os.listdir(directory_fd)):
        item_path = f'{{prefix}}/{{name}}' if prefix else name
        item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(item_stat.st_mode):
            manifest.append({{"path": item_path, "kind": "directory"}})
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                walk(child_fd, item_path)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(item_stat.st_mode):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                before = os.fstat(file_fd)
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(file_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                after = os.fstat(file_fd)
                if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                    raise RuntimeError('SOURCE_REVISION_CONFLICT')
                manifest.append({{
                    "path": item_path,
                    "kind": "file",
                    "sha256": digest.hexdigest(),
                    "size_bytes": before.st_size,
                }})
            finally:
                os.close(file_fd)
        else:
            raise RuntimeError('SYMLINK_REJECTED')
def rename_noreplace(parent_fd, source_name, target_name):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, 'renameat2', None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            parent_fd,
            os.fsencode(source_name),
            parent_fd,
            os.fsencode(target_name),
            1,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError(error_number, os.strerror(error_number), target_name)
        if error_number not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(error_number, os.strerror(error_number), target_name)
    placeholder_created = False
    try:
        os.mkdir(target_name, mode=0o700, dir_fd=parent_fd)
        placeholder_created = True
        os.rename(source_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except Exception:
        if placeholder_created:
            try:
                os.rmdir(target_name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
try:
    parent_fd = open_dir_chain(root, parent)
    try:
        staged_stat = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(staged_stat.st_mode):
            raise RuntimeError('NOT_DIRECTORY')
        staged_fd = os.open(staged_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            walk(staged_fd, '')
        finally:
            os.close(staged_fd)
        manifest.sort(key=lambda item: item.get('path', ''))
        if manifest != expected_manifest:
            raise RuntimeError('STAGE_MANIFEST_MISMATCH')
        rename_noreplace(parent_fd, staged_name, destination_name)
        os.fsync(parent_fd)
        print(json.dumps({{"ok": True, "manifest": manifest}}))
    finally:
        os.close(parent_fd)
except FileExistsError:
    print(json.dumps({{"ok": False, "code": "NAME_CONFLICT", "message": "目标目录已存在"}}))
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "暂存目录或目标父目录不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "暂存目录校验或发布失败"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )

    async def cleanup_external_incoming_directory(
        self,
        *,
        destination_root: str,
        incoming_relative_path: str,
    ) -> bool:
        """Delete only a uniquely named unpublished incoming tree."""
        incoming = WorkspacePathPolicy.normalize_external_relative_path(
            incoming_relative_path
        )
        parent, name = posixpath.split(incoming)
        if not name.startswith(".incoming-"):
            raise WorkspaceError(400, "INVALID_STAGE_PATH", "清理目标不是 incoming 暂存目录")
        payload = await self._run_json_script(
            f"""import json, os, stat
{self._safe_walk_source()}
root = {destination_root!r}
parent = {parent!r}
name = {name!r}
def remove_tree(parent_fd, child_name):
    item_stat = os.stat(child_name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(item_stat.st_mode):
        raise RuntimeError('NOT_DIRECTORY')
    child_fd = os.open(child_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        for candidate in os.listdir(child_fd):
            candidate_stat = os.stat(candidate, dir_fd=child_fd, follow_symlinks=False)
            if stat.S_ISDIR(candidate_stat.st_mode):
                remove_tree(child_fd, candidate)
            elif stat.S_ISREG(candidate_stat.st_mode):
                os.unlink(candidate, dir_fd=child_fd)
            else:
                raise RuntimeError('SYMLINK_REJECTED')
    finally:
        os.close(child_fd)
    os.rmdir(child_name, dir_fd=parent_fd)
try:
    parent_fd = open_dir_chain(root, parent)
    try:
        remove_tree(parent_fd, name)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    print(json.dumps({{"ok": True, "removed": True}}))
except FileNotFoundError:
    print(json.dumps({{"ok": True, "removed": False}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "incoming 暂存目录清理失败"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return bool(payload.get("removed"))

    async def snapshot_for_read(
        self,
        *,
        source_relative_path: str,
        destination_relative_path: str,
    ) -> SandboxFileStat:
        """Create an O(1) stable read snapshot inside the workspace volume.

        Returns the physical stat of the linked source so callers can detect
        drift against metadata. ``sha256`` stays ``None`` to keep reads O(1).
        """
        source = WorkspacePathPolicy.normalize_relative_path(
            source_relative_path,
            allow_system=source_relative_path.startswith(WORKSPACE_SYSTEM_DIRECTORY + "/"),
        )
        destination = WorkspacePathPolicy.normalize_relative_path(
            destination_relative_path,
            allow_system=True,
        )
        source_parent, source_name = posixpath.split(source)
        dest_parent, dest_name = posixpath.split(destination)
        temp_name = f".{uuid.uuid4().hex}.link"
        payload = await self._run_json_script(
            f"""import fcntl, json, os, stat
{self._safe_walk_source()}
def open_or_create_dir_chain(root, relative):
    if os.path.islink(root):
        raise RuntimeError('SYMLINK_REJECTED')
    os.makedirs(root, mode=0o700, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in ([p for p in relative.split('/') if p]):
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=fd)
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise
root = {self.workspace_root!r}
source_parent = {source_parent!r}
source_name = {source_name!r}
dest_parent = {dest_parent!r}
dest_name = {dest_name!r}
temp_name = {temp_name!r}
temp_created = False
lock_fd = None
try:
    source_parent_fd = open_dir_chain(root, source_parent)
    dest_fd = open_or_create_dir_chain(root, dest_parent)
    try:
        lock_fd = os.open('.snapshot.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dest_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        for candidate in os.listdir(dest_fd):
            token = candidate[1:-5] if candidate.startswith('.') and candidate.endswith('.link') else ''
            if len(token) == 32 and all(char in '0123456789abcdef' for char in token):
                try:
                    candidate_stat = os.stat(candidate, dir_fd=dest_fd, follow_symlinks=False)
                    if stat.S_ISREG(candidate_stat.st_mode):
                        os.unlink(candidate, dir_fd=dest_fd)
                except FileNotFoundError:
                    pass
        source_stat = os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(source_stat.st_mode):
            raise RuntimeError('NOT_FILE')
        os.link(
            source_name,
            temp_name,
            src_dir_fd=source_parent_fd,
            dst_dir_fd=dest_fd,
            follow_symlinks=False,
        )
        temp_created = True
        linked_stat = os.stat(temp_name, dir_fd=dest_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(linked_stat.st_mode)
            or linked_stat.st_dev != source_stat.st_dev
            or linked_stat.st_ino != source_stat.st_ino
        ):
            raise RuntimeError('SOURCE_REVISION_CONFLICT')
        os.replace(temp_name, dest_name, src_dir_fd=dest_fd, dst_dir_fd=dest_fd)
        temp_created = False
        print(json.dumps({{"ok": True, "size": source_stat.st_size, "mtime_ns": source_stat.st_mtime_ns}}))
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=dest_fd)
            except OSError:
                pass
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(dest_fd)
        os.close(source_parent_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "工作区源文件不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "源或目标路径不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return SandboxFileStat(
            size_bytes=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            sha256=None,
        )

    async def copy_version_snapshot(
        self,
        *,
        source_relative_path: str,
        destination_relative_path: str,
        expected_sha256: str | None,
        expected_size_bytes: int,
    ) -> SandboxFileStat:
        """Ensure one content-addressed immutable object, never a working-file hard link."""
        if not isinstance(expected_sha256, str):
            raise WorkspaceError(409, "CONTENT_OBJECT_INVALID", "文件版本缺少内容摘要")
        return await self.ensure_content_object(
            source_relative_path=source_relative_path,
            destination_relative_path=destination_relative_path,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )

    async def restore_version_atomic(
        self,
        *,
        version_relative_path: str,
        destination_relative_path: str,
        expected_version_sha256: str,
        expected_version_size_bytes: int,
        expected_destination_sha256: str | None,
        temp_token: str,
    ) -> SandboxFileStat:
        temp_relative = f"{WORKSPACE_TEMP_DIRECTORY}/{temp_token}.restore"
        copied = await self.copy_to_external_atomic(
            source_relative_path=version_relative_path,
            expected_source_sha256=expected_version_sha256,
            destination_root=self.workspace_root,
            destination_relative_path=temp_relative,
        )
        if (
            copied.sha256 != expected_version_sha256
            or int(copied.size_bytes) != int(expected_version_size_bytes)
        ):
            try:
                await self.remove(temp_relative)
            except WorkspaceError as cleanup_error:
                if cleanup_error.code != "NOT_FOUND":
                    raise
            raise WorkspaceError(409, "VERSION_SNAPSHOT_CHANGED", "历史版本内容校验失败")
        try:
            return await self._install_temp(
                temp_relative,
                destination_relative_path,
                expected_sha256=expected_destination_sha256,
                must_not_exist=False,
            )
        except (Exception, asyncio.CancelledError):
            try:
                await self.remove(temp_relative)
            except WorkspaceError as cleanup_error:
                if cleanup_error.code != "NOT_FOUND":
                    logger.warning("版本恢复临时副本清理失败 path=%s", temp_relative)
            except Exception:
                logger.warning(
                    "版本恢复临时副本清理失败 path=%s",
                    temp_relative,
                    exc_info=True,
                )
            raise

    async def cleanup_read_snapshot_links(self, destination_directory: str) -> int:
        """Remove only interrupted temporary hard links under one read snapshot directory."""
        directory = WorkspacePathPolicy.normalize_relative_path(
            destination_directory,
            allow_system=True,
        )
        expected_prefix = f"{WORKSPACE_SYSTEM_DIRECTORY}/read/"
        if not directory.startswith(expected_prefix):
            raise WorkspaceError(400, "INVALID_SOURCE_PATH", "清理目标不是工作区读取快照目录")
        payload = await self._run_json_script(
            f"""import fcntl, json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
directory = {directory!r}
removed = 0
lock_fd = None
try:
    directory_fd = open_dir_chain(root, directory)
    try:
        lock_fd = os.open('.snapshot.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        for candidate in os.listdir(directory_fd):
            token = candidate[1:-5] if candidate.startswith('.') and candidate.endswith('.link') else ''
            if len(token) == 32 and all(char in '0123456789abcdef' for char in token):
                try:
                    candidate_stat = os.stat(candidate, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISREG(candidate_stat.st_mode):
                        os.unlink(candidate, dir_fd=directory_fd)
                        removed += 1
                except FileNotFoundError:
                    pass
        print(json.dumps({{"ok": True, "removed": removed}}))
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(directory_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": True, "removed": 0}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )
        return int(payload.get("removed") or 0)

    async def move(
        self,
        source_relative_path: str,
        destination_relative_path: str,
        *,
        allow_system_destination: bool = False,
    ) -> None:
        source = WorkspacePathPolicy.normalize_relative_path(source_relative_path, allow_system=True)
        destination = WorkspacePathPolicy.normalize_relative_path(
            destination_relative_path,
            allow_system=allow_system_destination,
        )
        source_parent, source_name = posixpath.split(source)
        dest_parent, dest_name = posixpath.split(destination)
        await self._run_json_script(
            f"""import json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
source_parent = {source_parent!r}
source_name = {source_name!r}
dest_parent = {dest_parent!r}
dest_name = {dest_name!r}
try:
    source_fd = open_dir_chain(root, source_parent)
    dest_fd = open_dir_chain(root, dest_parent)
    try:
        source_stat = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISLNK(source_stat.st_mode) or not (
            stat.S_ISREG(source_stat.st_mode) or stat.S_ISDIR(source_stat.st_mode)
        ):
            raise RuntimeError('SYMLINK_REJECTED')
        try:
            os.stat(dest_name, dir_fd=dest_fd, follow_symlinks=False)
            print(json.dumps({{"ok": False, "code": "NAME_CONFLICT", "message": "目标名称已存在"}}))
        except FileNotFoundError:
            os.rename(source_name, dest_name, src_dir_fd=source_fd, dst_dir_fd=dest_fd)
            os.fsync(source_fd)
            if dest_fd != source_fd:
                os.fsync(dest_fd)
            print(json.dumps({{"ok": True}}))
    finally:
        os.close(dest_fd)
        os.close(source_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "源条目或目标目录不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "源条目类型或路径不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )


    async def delete_entries(self, paths: list[str], cleanup_paths: list[str]) -> None:
        """Delete confirmed roots in place; retrying a partial delete is safe under its claims."""
        roots = [WorkspacePathPolicy.normalize_relative_path(path) for path in paths]
        cleanup = [WorkspacePathPolicy.normalize_relative_path(path, allow_system=True) for path in cleanup_paths]
        if any(not path.startswith(f"{WORKSPACE_SYSTEM_DIRECTORY}/read/") for path in cleanup):
            raise WorkspaceError(400, "INVALID_PATH", "删除清理范围无效")
        await self._run_json_script(
            f"""import json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
paths = {roots!r}
cleanup_paths = {cleanup!r}
def check_tree(parent_fd, name):
    st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError('SYMLINK_REJECTED')
    if stat.S_ISDIR(st.st_mode):
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            for child in os.listdir(child_fd):
                check_tree(child_fd, child)
        finally:
            os.close(child_fd)
    elif not stat.S_ISREG(st.st_mode):
        raise RuntimeError('NOT_FILE')
def remove_tree(parent_fd, name):
    st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISDIR(st.st_mode):
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            for child in os.listdir(child_fd):
                remove_tree(child_fd, child)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    elif stat.S_ISREG(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)
    else:
        raise RuntimeError('SYMLINK_REJECTED')
opened = []
try:
    # Validate every root before the first destructive operation.
    for path in paths + cleanup_paths:
        parent, name = os.path.split(path)
        try:
            fd = open_dir_chain(root, parent)
        except FileNotFoundError:
            continue
        opened.append((fd, name))
        try:
            check_tree(fd, name)
        except FileNotFoundError:
            pass
    # Platform-owned snapshot directories are named by stable Workspace ID.
    # Independent ordinary Session outputs/imported copies are not touched.
    mount = os.path.dirname(root)
    for execution_parent in ('sessions', 'cron/runs'):
        try:
            runs_fd = open_dir_chain(mount, execution_parent)
        except FileNotFoundError:
            continue
        try:
            for run_name in os.listdir(runs_fd):
                try:
                    run_fd = os.open(run_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=runs_fd)
                except OSError:
                    continue
                try:
                    try:
                        snapshots_fd = os.open('.workspace-snapshots', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=run_fd)
                    except FileNotFoundError:
                        continue
                    try:
                        for path in cleanup_paths:
                            entry_id = os.path.basename(path)
                            try:
                                check_tree(snapshots_fd, entry_id)
                                opened.append((os.dup(snapshots_fd), entry_id))
                            except FileNotFoundError:
                                pass
                    finally:
                        os.close(snapshots_fd)
                finally:
                    os.close(run_fd)
        finally:
            os.close(runs_fd)
    for fd, name in opened:
        try:
            remove_tree(fd, name)
            os.fsync(fd)
        except FileNotFoundError:
            pass
    print(json.dumps({{"ok": True}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "删除路径无效"}}))
except OSError as exc:
    print(json.dumps({{"ok": False, "code": "IO_ERROR", "message": str(exc)}}))
finally:
    for fd, _name in opened:
        os.close(fd)
"""
        )

    async def remove(self, relative_path: str) -> None:
        safe = WorkspacePathPolicy.normalize_relative_path(relative_path, allow_system=True)
        parent, name = posixpath.split(safe)
        await self._run_json_script(
            f"""import json, os, stat
{self._safe_walk_source()}
def remove_entry(parent_fd, name):
    st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError('SYMLINK_REJECTED')
    if stat.S_ISDIR(st.st_mode):
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            for child in os.listdir(child_fd):
                remove_entry(child_fd, child)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    elif stat.S_ISREG(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)
    else:
        raise RuntimeError('NOT_FILE')
root = {self.workspace_root!r}
parent = {parent!r}
name = {name!r}
try:
    parent_fd = open_dir_chain(root, parent)
    try:
        remove_entry(parent_fd, name)
        os.fsync(parent_fd)
        print(json.dumps({{"ok": True}}))
    finally:
        os.close(parent_fd)
except FileNotFoundError:
    print(json.dumps({{"ok": False, "code": "NOT_FOUND", "message": "条目不存在"}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "条目类型或路径不合法"}}))
except OSError as exc:
    code = 'SYMLINK_REJECTED' if getattr(exc, 'errno', None) == 40 else 'IO_ERROR'
    print(json.dumps({{"ok": False, "code": code, "message": str(exc)}}))"""
        )

    async def remove_content_object(self, relative_path: str) -> None:
        await self.remove_content_objects([relative_path])

    async def remove_content_objects(self, relative_paths: Iterable[str]) -> None:
        paths = sorted(set(relative_paths))
        if not paths:
            return
        pattern = re.compile(
            rf"^{re.escape(WORKSPACE_OBJECT_DIRECTORY)}/([0-9a-f]{{2}})/([0-9a-f]{{64}})/content$"
        )
        for path in paths:
            match = pattern.fullmatch(path)
            if match is None or not match.group(2).startswith(match.group(1)):
                raise WorkspaceError(400, "CONTENT_OBJECT_INVALID", "内容对象路径无效")
        await self._run_json_script(
            f"""import json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
paths = {paths!r}
opened = []
try:
    for path in paths:
        parent, name = os.path.split(path)
        try:
            fd = open_dir_chain(root, parent)
        except FileNotFoundError:
            continue
        opened.append((fd, name))
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError('SYMLINK_REJECTED' if stat.S_ISLNK(st.st_mode) else 'NOT_FILE')
    for fd, name in opened:
        try:
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)
        except FileNotFoundError:
            pass
    directories = {{directory for path in paths for directory in (os.path.dirname(path), os.path.dirname(os.path.dirname(path)))}}
    for directory in sorted(directories, key=lambda path: (-path.count('/'), path)):
        parent, name = os.path.split(directory)
        try:
            fd = open_dir_chain(root, parent)
            try:
                os.rmdir(name, dir_fd=fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    print(json.dumps({{"ok": True}}))
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "内容对象路径无效"}}))
except OSError as exc:
    print(json.dumps({{"ok": False, "code": "IO_ERROR", "message": str(exc)}}))
finally:
    for fd, _name in opened:
        os.close(fd)
"""
        )

    async def remove_office_preview_caches(self, cache_keys: Iterable[str]) -> None:
        keys = sorted(set(cache_keys))
        if not keys:
            return
        if any(re.fullmatch(r"[0-9a-f]{64}", key) is None for key in keys):
            raise WorkspaceError(400, "PREVIEW_CACHE_KEY_INVALID", "预览缓存键无效")
        await self._run_json_script(
            f"""import json, os, stat
{self._safe_walk_source()}
root = {self.workspace_root!r}
cache_root = {WORKSPACE_OFFICE_CACHE_DIRECTORY!r}
keys = {keys!r}
def remove_tree(parent_fd, name):
    st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError('SYMLINK_REJECTED')
    if stat.S_ISDIR(st.st_mode):
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            for child in os.listdir(child_fd):
                remove_tree(child_fd, child)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    elif stat.S_ISREG(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)
    else:
        raise RuntimeError('NOT_FILE')
try:
    try:
        cache_fd = open_dir_chain(root, cache_root)
    except FileNotFoundError:
        print(json.dumps({{"ok": True, "removed": 0}}))
    else:
        removed = 0
        try:
            for key in keys:
                try:
                    remove_tree(cache_fd, key)
                    removed += 1
                except FileNotFoundError:
                    pass
            os.fsync(cache_fd)
            print(json.dumps({{"ok": True, "removed": removed}}))
        finally:
            os.close(cache_fd)
except RuntimeError as exc:
    print(json.dumps({{"ok": False, "code": str(exc), "message": "预览缓存路径无效"}}))
except OSError as exc:
    print(json.dumps({{"ok": False, "code": "IO_ERROR", "message": str(exc)}}))"""
        )

    async def cleanup_empty_legacy_version_directories(self) -> int:
        legacy_root = posixpath.join(self.workspace_root, WORKSPACE_SYSTEM_DIRECTORY, "versions")
        payload = await self._run_json_script(
            f"""import json, os, stat
root = {legacy_root!r}
removed = 0
if os.path.lexists(root):
    root_stat = os.lstat(root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        print(json.dumps({{"ok": False, "code": "SYMLINK_REJECTED", "message": "旧版本目录无效"}}))
    else:
        for current, _dirs, _files in os.walk(root, topdown=False, followlinks=False):
            try:
                os.rmdir(current)
                removed += 1
            except OSError:
                pass
        print(json.dumps({{"ok": True, "removed": removed}}))
else:
    print(json.dumps({{"ok": True, "removed": 0}}))"""
        )
        return int(payload.get("removed") or 0)

    async def read(self, relative_path: str) -> bytes | Any:
        safe = WorkspacePathPolicy.normalize_relative_path(relative_path)
        absolute = self.absolute_path(safe)
        return await self.sandbox.files.read_bytes_stream(
            absolute,
            chunk_size=64 * 1024,
        )


def _blank_xlsx_bytes() -> bytes:
    """Build a minimal standards-compliant workbook containing ``Sheet1``."""
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        "xl/worksheets/sheet1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1"/><sheetData/></worksheet>""",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


async def _validate_edit_content(name: str, content: bytes) -> None:
    extension = posixpath.splitext(name)[1].lower()
    if extension in {".md", ".markdown", ".txt"}:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(422, "INVALID_UTF8", "文本文件必须使用 UTF-8") from exc
        return
    if extension == ".csv":
        try:
            await asyncio.to_thread(validate_csv_edit_payload, content)
        except ValueError as exc:
            raise WorkspaceError(422, "INVALID_CSV", str(exc)) from exc
        return
    if extension == ".xlsx":
        try:
            await asyncio.to_thread(validate_xlsx_edit_payload, content)
        except ValueError as exc:
            raise WorkspaceError(422, "INVALID_XLSX", str(exc)) from exc
        return
    raise WorkspaceError(415, "UNSUPPORTED_EDIT_TYPE", "当前文件类型不支持在线编辑")


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise WorkspaceError(400, "INVALID_CURSOR", "分页游标无效") from exc
    if value < 0:
        raise WorkspaceError(400, "INVALID_CURSOR", "分页游标无效")
    return value


class WorkspaceService:
    """Authoritative workspace metadata, byte operations, and mutation journal."""

    # A healthy cached sandbox can be reused without entering the per-user
    # lifecycle lock. Revalidate occasionally so a remotely deleted container
    # heals without adding a control-plane round trip to every file operation.
    _sandbox_validation_deadlines: dict[tuple[str, str], float] = {}
    _sandbox_validation_ttl_seconds = 10.0

    def __init__(
        self,
        db: DBSession,
        sandbox_service: Any | None = None,
        *,
        sandbox: Any | None = None,
        db_session_factory: Callable[[], DBSession] | None = None,
    ) -> None:
        self.db = db
        self.sandbox_service = sandbox_service or get_sandbox_service()
        self.sandbox = sandbox
        self.settings = get_settings()
        self.db_session_factory = db_session_factory or SessionLocal

    def _independent_db_session_factory(self) -> Callable[[], DBSession]:
        configured = getattr(self, "db_session_factory", None)
        if callable(configured):
            return configured
        bind = self.db.get_bind()
        return sessionmaker(bind=bind, expire_on_commit=False)

    @asynccontextmanager
    async def _guard_workspace_claims(
        self,
        store: WorkspaceStore,
        leases: Iterable[WorkspaceClaimLease],
    ):
        frozen_leases = tuple(leases)
        async with keep_workspace_claims_alive(
            self._independent_db_session_factory(),
            frozen_leases,
            lease_seconds=int(self.settings.workspace_mutation_lease_seconds),
        ) as heartbeat:
            async with store.claim_fence(frozen_leases):
                yield heartbeat

    def _ancestor_entry_ids(
        self,
        user_id: str,
        parent_id: str | None,
    ) -> list[str]:
        ancestors: list[str] = []
        current_id = parent_id
        while current_id:
            if current_id in ancestors:
                raise WorkspaceError(409, "DIRECTORY_CYCLE", "工作区目录关系存在循环")
            current = self.db.query(
                WorkspaceEntry.entry_id,
                WorkspaceEntry.parent_id,
                WorkspaceEntry.kind,
                WorkspaceEntry.status,
            ).filter(
                WorkspaceEntry.user_id == user_id,
                WorkspaceEntry.entry_id == current_id,
            ).first()
            if current is None or current.status != "active" or current.kind != "directory":
                raise WorkspaceError(404, "ENTRY_NOT_FOUND", "工作区父目录不存在")
            ancestors.append(str(current.entry_id))
            current_id = current.parent_id
        return ancestors

    @staticmethod
    def _tree_scope_keys(entry_ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(tree_scope(entry_id) for entry_id in entry_ids)

    def _acquire_claims(
        self,
        *,
        user_id: str,
        operation: str,
        specs: Iterable[WorkspaceClaimSpec],
        mutation_id: str | None = None,
        commit: bool = True,
    ) -> list[WorkspaceClaimLease]:
        try:
            return WorkspaceMutationCoordinator(self.db).acquire_claims(
                user_id=user_id,
                operation=operation,
                specs=specs,
                mutation_id=mutation_id,
                commit=commit,
            )
        except WorkspaceClaimConflict as exc:
            raise WorkspaceError(
                409,
                "WORKSPACE_MUTATION_IN_PROGRESS",
                "目标文件或目录正在被其他操作修改，请稍后重试",
                extra={"conflicting_scopes": list(exc.scope_keys)},
            ) from exc
        except WorkspaceDraining as exc:
            raise WorkspaceError(
                409,
                "WORKSPACE_DRAINING",
                str(exc),
            ) from exc

    def _release_unattached_claims(
        self,
        leases: Iterable[WorkspaceClaimLease],
    ) -> None:
        try:
            WorkspaceMutationCoordinator(self.db).release_claims(leases)
        except Exception:
            self.db.rollback()
            logger.warning("释放未附着的工作区 claim 失败", exc_info=True)

    def _release_mutation_claims(
        self,
        mutation_id: str,
        owner_token: str | None,
        *,
        final_state: str = "released",
    ) -> None:
        if not owner_token:
            return
        query = self.db.query(WorkspaceClaim).filter(
            WorkspaceClaim.mutation_id == mutation_id,
            WorkspaceClaim.state == "active",
            WorkspaceClaim.owner_token == owner_token,
        )
        query.update(
            {"state": final_state, "released_at": now_naive()},
            synchronize_session=False,
        )

    def _lock_and_assert_mutation_claims(
        self,
        mutation: WorkspaceMutation,
        journal: dict[str, Any],
        expected_leases: Iterable[WorkspaceClaimLease] | None = None,
    ) -> None:
        claim_ids = journal.get("claim_ids") or []
        if not claim_ids:
            return
        if not isinstance(claim_ids, list) or not mutation.owner_token:
            raise WorkspaceError(409, "MUTATION_FENCED", "工作区修改凭证无效")
        leases = tuple(expected_leases or ())
        if leases:
            expected_ids = {lease.claim_id for lease in leases}
            if set(str(item) for item in claim_ids) != expected_ids:
                raise WorkspaceError(409, "MUTATION_FENCED", "工作区修改凭证范围已变化")
            primary = leases[0]
            if (
                mutation.owner_token != primary.owner_token
                or int(mutation.claim_generation or 0) != int(primary.generation)
            ):
                raise WorkspaceError(409, "MUTATION_FENCED", "工作区修改所有权已被接管")
        rows = (
            self.db.query(WorkspaceClaim)
            .filter(
                WorkspaceClaim.claim_id.in_(tuple(str(item) for item in claim_ids)),
                WorkspaceClaim.mutation_id == mutation.mutation_id,
                WorkspaceClaim.owner_token == mutation.owner_token,
                WorkspaceClaim.state == "active",
            )
            .with_for_update()
            .all()
        )
        if len(rows) != len(set(str(item) for item in claim_ids)):
            raise WorkspaceError(409, "MUTATION_FENCED", "工作区修改所有权已失效")
        if leases:
            rows_by_id = {row.claim_id: row for row in rows}
            for lease in leases:
                row = rows_by_id.get(lease.claim_id)
                if (
                    row is None
                    or row.owner_token != lease.owner_token
                    or int(row.generation) != int(lease.generation)
                ):
                    raise WorkspaceError(409, "MUTATION_FENCED", "工作区修改所有权已被接管")

    def _owned_prepared_mutation_query(
        self,
        prepared: WorkspacePreparedMutation,
    ):
        query = self.db.query(WorkspaceMutation).filter(
            WorkspaceMutation.mutation_id == prepared.mutation_id,
            WorkspaceMutation.state == "prepared",
        )
        if prepared.leases:
            primary = prepared.leases[0]
            query = query.filter(
                WorkspaceMutation.owner_token == primary.owner_token,
                WorkspaceMutation.claim_generation == primary.generation,
            )
        return query

    async def _sandbox_for_user(
        self,
        user_id: str,
        *,
        persisted_id: str | None = None,
        runtime: Any | None = None,
        binding_loaded: bool = False,
        active_run: bool = False,
    ) -> Any:
        # Chat/Cron tools are bound to the exact Sandbox generation acquired
        # for their run. A concurrent file-panel recovery must not make an
        # in-flight Agent jump to a newly-created user binding mid-round.
        if self.sandbox is not None:
            return self.sandbox
        if not binding_loaded:
            row = self.db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
            persisted_id = row.sandbox_id if row and row.sandbox_id else None
            self.db.commit()
        cached = self.sandbox_service.get_cached(user_id)
        if cached is not None:
            cached_id_value = getattr(cached, "id", None)
            cache_matches = bool(persisted_id and cached_id_value == persisted_id)
            if cache_matches:
                if runtime is None:
                    runtime = self._runtime(user_id)
                cache_matches = self.sandbox_service.get_cached_profile_fingerprint(user_id) == (
                    runtime.profile_id,
                    runtime.profile_version,
                )
            if cache_matches:
                cached_id = str(cached_id_value or persisted_id)
                validation_key = (user_id, cached_id)
                if self._sandbox_validation_deadlines.get(validation_key, 0.0) > time.monotonic():
                    return cached
                is_healthy = getattr(cached, "is_healthy", None)
                healthy = True
                if callable(is_healthy):
                    try:
                        healthy = bool(await is_healthy())
                    except Exception:
                        healthy = False
                if healthy:
                    self._sandbox_validation_deadlines[validation_key] = (
                        time.monotonic() + self._sandbox_validation_ttl_seconds
                    )
                    return cached
            self.sandbox_service.invalidate_cache(user_id)
            for validation_key in tuple(self._sandbox_validation_deadlines):
                if validation_key[0] == user_id:
                    self._sandbox_validation_deadlines.pop(validation_key, None)
        if active_run:
            if not persisted_id:
                raise WorkspaceError(
                    503,
                    "ACTIVE_RUN_SANDBOX_UNAVAILABLE",
                    "Agent 运行期间 Sandbox 绑定尚未就绪",
                )
            try:
                sandbox = await self.sandbox_service.get_existing(user_id, persisted_id)
            except Exception as exc:
                raise WorkspaceError(
                    503,
                    "ACTIVE_RUN_SANDBOX_UNAVAILABLE",
                    "Agent 运行期间当前 Sandbox 暂时不可用",
                ) from exc
            resolved_id = str(getattr(sandbox, "id", None) or persisted_id)
            self._sandbox_validation_deadlines[(user_id, resolved_id)] = (
                time.monotonic() + self._sandbox_validation_ttl_seconds
            )
            return sandbox
        sandbox, sandbox_id = await self.sandbox_service.get_or_resume_with_persisted_id(
            user_id,
            persisted_id,
        )
        resolved_id = str(getattr(sandbox, "id", None) or sandbox_id or persisted_id or "cached")
        self._sandbox_validation_deadlines[(user_id, resolved_id)] = (
            time.monotonic() + self._sandbox_validation_ttl_seconds
        )
        return sandbox

    def _runtime(self, user_id: str):
        # Workspace profile fencing must use desired DB state, never a stale
        # runtime cache that could authorize rebuilding against the wrong
        # persistent backend.
        return resolve_sandbox_runtime_config(self.db, user_id)

    async def _prepare(
        self,
        user_id: str,
        *,
        for_update: bool,
        reconcile_prepared: bool = True,
        require_filesystem: bool = True,
    ) -> tuple[UserWorkspace, WorkspaceStore | None]:
        if not self.settings.sandbox_persistent_storage_enabled:
            raise WorkspaceError(
                503,
                "WORKSPACE_PERSISTENCE_DISABLED",
                "当前 Sandbox 未启用持久存储，不能使用工作区",
            )
        runtime = self._runtime(user_id)
        mount_path = get_sandbox_mount_path(runtime.mount_path)
        root_path = posixpath.join(mount_path, WORKSPACE_DIRECTORY)
        workspace = self.db.query(UserWorkspace).filter(
            UserWorkspace.user_id == user_id
        ).first()
        created = workspace is None
        metadata_changed = created
        if workspace is None:
            workspace = UserWorkspace(
                user_id=user_id,
                root_path=root_path,
                active_profile_id=runtime.profile_id,
                active_profile_version=runtime.profile_version,
                quota_bytes=int(self.settings.workspace_quota_bytes),
                history_quota_bytes=int(self.settings.workspace_history_quota_bytes),
            )
            self.db.add(workspace)
            self.db.flush()
        elif (
            workspace.active_profile_id != runtime.profile_id
            or int(workspace.active_profile_version or 0) != int(runtime.profile_version)
            or workspace.root_path != root_path
        ):
            if int(workspace.entry_count or 0) > 0:
                raise WorkspaceError(
                    409,
                    "WORKSPACE_PROFILE_MISMATCH",
                    "工作区包含持久文件，不能切换 Sandbox Profile",
                )
            workspace.root_path = root_path
            workspace.active_profile_id = runtime.profile_id
            workspace.active_profile_version = runtime.profile_version
            workspace.updated_at = now_naive()
            metadata_changed = True
        # Directory/search projections are authoritative DB metadata and must
        # not resume a user's Sandbox. Resuming here lets an unavailable
        # OpenSandbox control plane hold several request-scoped DB sessions,
        # exhaust the pool, and make unrelated Session APIs appear frozen.
        if not require_filesystem:
            if for_update:
                raise RuntimeError("metadata-only workspace preparation cannot prepare mutations")
            if metadata_changed:
                self.db.commit()
                self.db.refresh(workspace)
            if workspace in self.db:
                self.db.expunge(workspace)
            self.db.commit()
            return workspace, None
        needs_root_ensure = (
            created
            or metadata_changed
            or int(workspace.entry_count or 0) == 0
        )
        workspace_root_path = workspace.root_path
        sandbox_row = self.db.query(UserSandbox).filter(
            UserSandbox.user_id == user_id
        ).first()
        persisted_sandbox_id = (
            sandbox_row.sandbox_id
            if sandbox_row and sandbox_row.sandbox_id
            else None
        )
        stale_cutoff = now_naive() - timedelta(
            seconds=max(int(self.settings.sse_subscribe_timeout), 1)
        )
        active_run = (
            self.db.query(UserRunLock.lock_id)
            .filter(
                UserRunLock.user_id == user_id,
                UserRunLock.updated_at >= stale_cutoff,
            )
            .first()
            is not None
        )
        # Do not hold a request-scoped DB transaction while waiting for the
        # Sandbox control plane. Entry CAS and the brief capacity reservation
        # below provide the mutation locks at the points that actually need them.
        self.db.commit()
        # Resolve/rebuild only after the durable workspace/Profile fence. A
        # non-empty mismatch must fail before SandboxService can recreate a
        # container against another backend.
        sandbox = await self._sandbox_for_user(
            user_id,
            persisted_id=persisted_sandbox_id,
            runtime=runtime,
            binding_loaded=True,
            active_run=active_run,
        )
        store = WorkspaceStore(sandbox, workspace_root_path)
        if needs_root_ensure:
            await store.ensure_root()
        workspace = self.db.query(UserWorkspace).filter(
            UserWorkspace.user_id == user_id
        ).one()
        if for_update and reconcile_prepared:
            await self._reconcile_user_prepared(workspace, store)
            # Reconciliation commits even when there is nothing to repair.
            # Production SessionLocal expires ORM attributes on that commit,
            # so reload an attached projection before returning a detached
            # workspace to upload/capacity callers.
            workspace = (
                self.db.query(UserWorkspace)
                .populate_existing()
                .filter(UserWorkspace.user_id == user_id)
                .one()
            )
        if workspace in self.db:
            self.db.expunge(workspace)
        self.db.commit()
        return workspace, store

    async def ensure_workspace(self, user_id: str) -> UserWorkspace:
        workspace, _store = await self._prepare(user_id, for_update=False)
        return workspace

    def _entry_query(self, user_id: str, entry_id: str, *, for_update: bool = False):
        query = self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.entry_id == entry_id,
        )
        return query.with_for_update() if for_update else query

    def _entry(
        self,
        user_id: str,
        entry_id: str,
        *,
        for_update: bool = False,
    ) -> WorkspaceEntry:
        entry = self._entry_query(user_id, entry_id, for_update=for_update).first()
        if entry is None or entry.status != "active":
            raise WorkspaceError(404, "ENTRY_NOT_FOUND", "工作区条目不存在")
        return entry

    def _parent(self, user_id: str, parent_id: str | None, *, for_update: bool = False) -> WorkspaceEntry | None:
        if parent_id is None:
            return None
        parent = self._entry(user_id, parent_id, for_update=for_update)
        if parent.kind != "directory":
            raise WorkspaceError(400, "NOT_DIRECTORY", "目标父级不是文件夹")
        return parent

    def _sibling(
        self,
        user_id: str,
        parent_id: str | None,
        name: str,
        *,
        exclude_entry_id: str | None = None,
        for_update: bool = True,
    ) -> WorkspaceEntry | None:
        query = self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.parent_key == (parent_id or _ROOT_PARENT_KEY),
            WorkspaceEntry.name == name,
            WorkspaceEntry.status == "active",
        )
        if exclude_entry_id:
            query = query.filter(WorkspaceEntry.entry_id != exclude_entry_id)
        return (query.with_for_update() if for_update else query).first()

    def _descendants_by_path_prefix(
        self,
        user_id: str,
        root_path: str,
        *,
        status: Literal["active"],
        for_update: bool = True,
    ) -> list[WorkspaceEntry]:
        """Lock descendants below one exact path.

        Workspace names may legally contain SQL LIKE metacharacters. Escape
        them before appending the descendant wildcard so ``%``/``_`` in one
        directory cannot capture an unrelated sibling subtree.
        """
        escaped_root = (
            root_path.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        query = (
            self.db.query(WorkspaceEntry)
            .filter(
                WorkspaceEntry.user_id == user_id,
                WorkspaceEntry.status == status,
                WorkspaceEntry.relative_path.like(
                    escaped_root + "/%",
                    escape="\\",
                ),
            )
        )
        return (query.with_for_update() if for_update else query).all()

    def _active_capacity_reservations(self, user_id: str) -> tuple[int, int]:
        rows = self.db.query(WorkspaceMutation.details_json).filter(
            WorkspaceMutation.user_id == user_id,
            WorkspaceMutation.state == "prepared",
        ).all()
        reserved_bytes = 0
        reserved_entries = 0
        for (details_json,) in rows:
            try:
                details = json.loads(details_json or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            journal = details.get("journal") if isinstance(details, dict) else None
            if not isinstance(journal, dict):
                continue
            # Only positive deltas reserve capacity. A pending shrink/delete
            # must not make space available until it actually commits.
            reserved_bytes += max(0, int(journal.get("bytes_delta") or 0))
            reserved_entries += max(0, int(journal.get("entries_delta") or 0))
        return reserved_bytes, reserved_entries

    def _check_capacity(self, workspace: UserWorkspace, *, bytes_delta: int = 0, entries_delta: int = 0) -> None:
        reserved_bytes, reserved_entries = self._active_capacity_reservations(workspace.user_id)
        next_bytes = int(workspace.used_bytes or 0) + reserved_bytes + int(bytes_delta)
        next_entries = int(workspace.entry_count or 0) + reserved_entries + int(entries_delta)
        max_entries = int(self.settings.workspace_max_entries)
        if next_bytes < 0 or next_entries < 0:
            raise WorkspaceError(500, "WORKSPACE_COUNTER_INVALID", "工作区计数异常")
        if next_bytes > int(workspace.quota_bytes or 0):
            raise WorkspaceError(
                413,
                "QUOTA_EXCEEDED",
                "工作区容量不足",
                extra={"quota_bytes": int(workspace.quota_bytes or 0), "used_bytes": int(workspace.used_bytes or 0)},
            )
        if next_entries > max_entries:
            raise WorkspaceError(413, "ENTRY_LIMIT_EXCEEDED", "工作区文件数量已达上限")

    @staticmethod
    def _context_fields(context: dict[str, Any] | None) -> dict[str, Any]:
        context = context or {}
        return {
            "session_id": context.get("session_id"),
            "round_id": context.get("round_id"),
            "tool_call_id": context.get("tool_call_id"),
            "cron_job_id": context.get("cron_job_id"),
            "cron_run_id": context.get("cron_run_id"),
        }

    def _idempotent_result(
        self,
        user_id: str,
        idempotency_key: str | None,
        operation: str,
    ) -> WorkspaceMutationResult | None:
        if not idempotency_key:
            return None
        mutation = self.db.query(WorkspaceMutation).filter(
            WorkspaceMutation.user_id == user_id,
            WorkspaceMutation.idempotency_key == idempotency_key,
        ).first()
        if mutation is None:
            return None
        if mutation.operation != operation:
            raise WorkspaceError(409, "IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他工作区操作")
        if mutation.state == "failed":
            self._raise_failed_mutation(mutation)
        if mutation.state != "completed" or not mutation.entry_id or not mutation.result_status:
            raise WorkspaceError(
                409,
                "MUTATION_IN_PROGRESS",
                "相同工作区操作仍在处理中",
                extra={
                    "mutation_id": mutation.mutation_id,
                    "mutation_state": mutation.state,
                    "outcome": "pending",
                },
            )
        auto_merged = False
        projection = None
        try:
            stored_details = json.loads(mutation.details_json or "{}")
            result_details = stored_details.get("result", stored_details)
            auto_merged = bool(result_details.get("auto_merged")) if isinstance(result_details, dict) else False
            journal = stored_details.get("journal") if isinstance(stored_details, dict) else None
            projection = (
                journal.get("entry_projection")
                if isinstance(journal, dict)
                else stored_details.get("entry_projection")
                if isinstance(stored_details, dict)
                else None
            )
        except (AttributeError, json.JSONDecodeError):
            pass
        current = self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.entry_id == mutation.entry_id,
        ).one_or_none()
        if isinstance(projection, dict):
            values = {
                field: projection.get(field)
                for field in (
                    "entry_id", "user_id", "parent_id", "parent_key", "name", "kind",
                    "relative_path", "size_bytes", "mime_type", "sha256", "revision",
                    "current_version_id", "head_blob_id", "tree_revision", "status",
                )
            }
            if values["entry_id"] != mutation.entry_id or values["user_id"] != user_id:
                raise WorkspaceError(409, "MUTATION_JOURNAL_INVALID", "工作区幂等回执归属无效")
            entry = WorkspaceEntry(
                **values,
                created_at=(current.created_at if current is not None else mutation.created_at),
                updated_at=mutation.completed_at or mutation.created_at,
            )
        elif current is not None:
            # Legacy mutation rows did not freeze their response projection.
            entry = current
        else:
            raise WorkspaceError(404, "ENTRY_NOT_FOUND", "工作区文件不存在")
        return WorkspaceMutationResult(
            mutation.result_status,
            entry,
            mutation.mutation_id,
            auto_merged=auto_merged,
        )

    def _begin_prepared_mutation(
        self,
        *,
        workspace: UserWorkspace,
        workspace_user_id: str | None = None,
        entry_id: str | None,
        actor: str,
        operation: str,
        result_status: str,
        idempotency_key: str | None,
        context: dict[str, Any] | None,
        before_revision: int | None,
        before_sha256: str | None,
        after_revision: int,
        after_sha256: str | None,
        journal: dict[str, Any],
        claim_specs: Iterable[WorkspaceClaimSpec] = (),
        before_version_id: str | None = None,
        after_version_id: str | None = None,
        change_set_id: str | None = None,
        mutation_id: str | None = None,
    ) -> WorkspacePreparedMutation:
        if actor not in {"web", "chat", "cron", "admin"}:
            raise WorkspaceError(422, "INVALID_ACTOR", "工作区调用来源无效")
        user_id = workspace_user_id or str(workspace.user_id)
        mutation_id = mutation_id or str(uuid.uuid4())
        normalized_claim_specs = tuple(claim_specs)
        leases = tuple(
            self._acquire_claims(
                user_id=user_id,
                operation=operation,
                specs=normalized_claim_specs,
                mutation_id=mutation_id,
                commit=False,
            )
            if normalized_claim_specs
            else ()
        )
        frozen_projections: list[dict[str, Any]] = []
        before_projection = journal.get("before_entry_projection")
        if isinstance(before_projection, dict):
            frozen_projections.append(before_projection)
        for projection_key in ("before_entry_projections", "base_entry_projections"):
            projection_items = journal.get(projection_key)
            if isinstance(projection_items, list):
                frozen_projections.extend(
                    item for item in projection_items if isinstance(item, dict)
                )
        for frozen_projection in sorted(
            frozen_projections,
            key=lambda item: str(item.get("entry_id") or ""),
        ):
            current = (
                self.db.query(WorkspaceEntry.entry_id)
                .filter(
                    WorkspaceEntry.user_id == user_id,
                    WorkspaceEntry.entry_id == str(frozen_projection.get("entry_id") or ""),
                    WorkspaceEntry.revision == int(frozen_projection.get("revision") or 0),
                    WorkspaceEntry.relative_path == frozen_projection.get("relative_path"),
                    WorkspaceEntry.status == frozen_projection.get("status"),
                    WorkspaceEntry.sha256 == frozen_projection.get("sha256"),
                    WorkspaceEntry.current_version_id == frozen_projection.get("current_version_id"),
                    WorkspaceEntry.head_blob_id == frozen_projection.get("head_blob_id"),
                    WorkspaceEntry.tree_revision == int(frozen_projection.get("tree_revision") or 1),
                )
                .with_for_update()
                .one_or_none()
            )
            if current is None:
                self.db.rollback()
                raise WorkspaceError(409, "REVISION_CONFLICT", "文件已被其他操作修改")
        destination_expectation = journal.get("destination_expectation")
        if isinstance(destination_expectation, dict):
            destination_query = self.db.query(WorkspaceEntry.entry_id).filter(
                WorkspaceEntry.user_id == user_id,
                WorkspaceEntry.parent_key == destination_expectation.get("parent_key"),
                WorkspaceEntry.name == destination_expectation.get("name"),
                WorkspaceEntry.status == "active",
            )
            excluded_entry_id = destination_expectation.get("exclude_entry_id")
            if excluded_entry_id:
                destination_query = destination_query.filter(
                    WorkspaceEntry.entry_id != str(excluded_entry_id)
                )
            if destination_query.with_for_update().first() is not None:
                self.db.rollback()
                raise WorkspaceError(409, "NAME_CONFLICT", "目标名称已存在")
        if journal.get("create_entry") is True:
            create_projection = journal.get("entry_projection")
            if not isinstance(create_projection, dict):
                self.db.rollback()
                raise WorkspaceError(500, "MUTATION_JOURNAL_INVALID", "工作区创建记录无效")
            existing = (
                self.db.query(WorkspaceEntry.entry_id)
                .filter(
                    WorkspaceEntry.user_id == user_id,
                    or_(
                        WorkspaceEntry.entry_id == str(create_projection.get("entry_id") or ""),
                        (
                            (WorkspaceEntry.parent_key == create_projection.get("parent_key"))
                            & (WorkspaceEntry.name == create_projection.get("name"))
                            & (WorkspaceEntry.status == "active")
                        ),
                    ),
                )
                .with_for_update()
                .first()
            )
            if existing is not None:
                self.db.rollback()
                raise WorkspaceError(409, "NAME_CONFLICT", "目标名称已存在")
        reservation_bytes = max(0, int(journal.get("bytes_delta") or 0))
        reservation_entries = max(0, int(journal.get("entries_delta") or 0))
        if reservation_bytes or reservation_entries:
            # Serialize only the tiny capacity-reservation transaction.
            # Move/rename/delete have no positive delta and skip this lock.
            authoritative_workspace = self.db.query(UserWorkspace).filter(
                UserWorkspace.user_id == user_id
            ).with_for_update().one()
            self._check_capacity(
                authoritative_workspace,
                bytes_delta=reservation_bytes,
                entries_delta=reservation_entries,
            )
        journal_payload = {
            **journal,
            "result_status": result_status,
            "claim_ids": [lease.claim_id for lease in leases],
            "temp_path": journal.get("temp_path")
            or f"{WORKSPACE_TEMP_DIRECTORY}/{mutation_id}.tmp",
        }
        primary_claim = leases[0] if leases else None
        mutation = WorkspaceMutation(
            mutation_id=mutation_id,
            user_id=user_id,
            entry_id=entry_id,
            actor=actor,
            operation=operation,
            state="prepared",
            result_status=result_status,
            idempotency_key=idempotency_key,
            claim_id=primary_claim.claim_id if primary_claim else None,
            claim_generation=primary_claim.generation if primary_claim else None,
            owner_token=primary_claim.owner_token if primary_claim else None,
            change_set_id=change_set_id,
            before_revision=before_revision,
            after_revision=after_revision,
            before_version_id=before_version_id,
            after_version_id=after_version_id,
            before_sha256=before_sha256,
            after_sha256=after_sha256,
            details_json=json.dumps(
                {"journal": journal_payload},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            lease_expires_at=now_naive()
            + timedelta(
                seconds=max(
                    int(self.settings.workspace_mutation_lease_seconds),
                    10,
                )
            ),
            heartbeat_at=now_naive(),
            completed_at=None,
            **self._context_fields(context),
        )
        self.db.add(mutation)
        version_rows = journal.get("version_rows")
        mutation_blob_ids: set[str] = set()
        if isinstance(version_rows, list):
            for version_row in version_rows:
                if not isinstance(version_row, dict) or not version_row.get("blob_id"):
                    continue
                mutation_blob_ids.add(str(version_row["blob_id"]))
                self._upsert_content_reference(
                    user_id=user_id,
                    blob_id=str(version_row["blob_id"]),
                    version_id=str(version_row.get("version_id") or "") or None,
                    reference_kind="mutation_object",
                    reference_key=f"{mutation_id}:{version_row['blob_id']}",
                )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            winner = self._idempotent_result(user_id, idempotency_key, operation)
            if winner is not None:
                raise WorkspaceError(
                    409,
                    "IDEMPOTENT_OPERATION_COMPLETED",
                    "相同工作区操作已经完成",
                    entry=winner.entry,
                    extra={"mutation_id": winner.mutation_id},
                ) from exc
            raise WorkspaceError(409, "MUTATION_CONFLICT", "工作区操作准备失败") from exc
        prepared = WorkspacePreparedMutation(
            mutation_id=mutation_id,
            user_id=user_id,
            leases=leases,
        )
        if mutation_blob_ids:
            pruning_object = self.db.query(WorkspaceContentObject.blob_id).filter(
                WorkspaceContentObject.user_id == user_id,
                WorkspaceContentObject.blob_id.in_(tuple(sorted(mutation_blob_ids))),
                WorkspaceContentObject.state == "pruning",
            ).with_for_update().first()
            self.db.rollback()
            if pruning_object is not None:
                self._fail_prepared_mutation(
                    prepared,
                    code="CONTENT_OBJECT_PRUNING",
                    message="内容对象正在回收，请重试",
                    recoverable=True,
                )
                raise WorkspaceError(409, "CONTENT_OBJECT_PRUNING", "内容对象正在回收，请重试")
        return prepared

    def _release_mutation_object_references(self, mutation_id: str) -> None:
        self.db.query(WorkspaceContentReference).filter(
            WorkspaceContentReference.reference_kind == "mutation_object",
            WorkspaceContentReference.reference_key.like(f"{mutation_id}:%"),
        ).delete(synchronize_session=False)

    @staticmethod
    def _freeze_mutation_failure(
        row: WorkspaceMutation,
        *,
        status_code: int,
        code: str,
        message: str,
        recoverable: bool,
        error_extra: dict[str, Any] | None = None,
    ) -> None:
        try:
            details = json.loads(row.details_json or "{}")
        except (TypeError, json.JSONDecodeError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        details["failure"] = {
            "status_code": int(status_code),
            "code": str(code),
            "message": str(message),
            "extra": dict(error_extra or {}),
        }
        row.details_json = json.dumps(
            details,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        row.state = "failed"
        row.error_code = code
        row.error_message = message
        row.recoverable = recoverable
        row.lease_expires_at = None
        row.completed_at = now_naive()

    def _raise_failed_mutation(self, mutation: WorkspaceMutation) -> None:
        try:
            details = json.loads(mutation.details_json or "{}")
        except (TypeError, json.JSONDecodeError):
            details = {}
        failure = details.get("failure") if isinstance(details, dict) else None
        if not isinstance(failure, dict):
            failure = {}
        try:
            status_code = int(failure.get("status_code") or 409)
        except (TypeError, ValueError):
            status_code = 409
        code = str(failure.get("code") or mutation.error_code or "MUTATION_FAILED")
        message = str(
            failure.get("message")
            or mutation.error_message
            or "相同工作区操作已经失败"
        )
        failure_extra = failure.get("extra")
        extra = dict(failure_extra) if isinstance(failure_extra, dict) else {}
        extra.update({
            "mutation_id": mutation.mutation_id,
            "mutation_state": "failed",
            "outcome": "not_applied" if mutation.recoverable else "unknown",
        })
        raise WorkspaceError(status_code, code, message, extra=extra)

    def _fail_prepared_mutation(
        self,
        mutation: WorkspacePreparedMutation,
        *,
        code: str,
        message: str,
        recoverable: bool,
        status_code: int = 409,
        error_extra: dict[str, Any] | None = None,
    ) -> None:
        self.db.rollback()
        row = self._owned_prepared_mutation_query(mutation).with_for_update().first()
        if row is None:
            return
        self._freeze_mutation_failure(
            row,
            status_code=status_code,
            code=code,
            message=message,
            recoverable=recoverable,
            error_extra=error_extra,
        )
        self._release_mutation_object_references(row.mutation_id)
        self._release_mutation_claims(row.mutation_id, row.owner_token)
        self.db.commit()

    @staticmethod
    def _journal_projection(entry: WorkspaceEntry) -> dict[str, Any]:
        return {
            "entry_id": entry.entry_id,
            "user_id": entry.user_id,
            "parent_id": entry.parent_id,
            "parent_key": entry.parent_key,
            "name": entry.name,
            "kind": entry.kind,
            "relative_path": entry.relative_path,
            "size_bytes": int(entry.size_bytes or 0),
            "mime_type": entry.mime_type,
            "sha256": entry.sha256,
            "revision": int(entry.revision),
            "current_version_id": entry.current_version_id,
            "head_blob_id": entry.head_blob_id,
            "tree_revision": int(entry.tree_revision or 1),
            "status": entry.status,
        }

    @staticmethod
    def _content_object_path(sha256: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise WorkspaceError(409, "CONTENT_OBJECT_INVALID", "内容对象摘要无效")
        return f"{WORKSPACE_OBJECT_DIRECTORY}/{sha256[:2]}/{sha256}/content"

    @staticmethod
    def _content_object_id(user_id: str, sha256: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"opencapybox://{user_id}/sha256/{sha256}"))

    @staticmethod
    def _default_checkpoint_kind(actor: str, *, initial: bool) -> str | None:
        if initial:
            return "initial" if actor == "web" else f"{actor}_publish"
        if actor in {"chat", "cron", "admin"}:
            return f"{actor}_publish"
        return None

    def _version_row(
        self,
        *,
        version_id: str,
        entry_id: str,
        user_id: str,
        sequence: int,
        parent_version_id: str | None,
        sha256: str | None,
        size_bytes: int,
        mime_type: str | None,
        actor: str,
        context: dict[str, Any] | None,
        checkpoint_kind: str | None,
    ) -> dict[str, Any]:
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise WorkspaceError(409, "CONTENT_OBJECT_INVALID", "文件缺少有效内容摘要")
        context_fields = self._context_fields(context)
        blob_id = self._content_object_id(user_id, sha256)
        retained_until = None
        if checkpoint_kind is None:
            retained_until = now_naive() + timedelta(
                days=int(self.settings.workspace_draft_base_retention_days)
            )
        return {
            "version_id": version_id,
            "user_id": user_id,
            "entry_id": entry_id,
            "sequence": sequence,
            "parent_version_id": parent_version_id,
            "restored_from_version_id": None,
            "blob_id": blob_id,
            "content_path": self._content_object_path(sha256),
            "sha256": sha256,
            "size_bytes": int(size_bytes),
            "mime_type": mime_type,
            "actor": actor,
            "session_id": context_fields.get("session_id"),
            "round_id": context_fields.get("round_id"),
            "cron_run_id": context_fields.get("cron_run_id"),
            "state": "materialized",
            "pinned": False,
            "checkpoint_kind": checkpoint_kind,
            "retained_until": retained_until.isoformat() if retained_until else None,
            "pruned_at": None,
        }

    def _plan_initial_file_version(
        self,
        entry: WorkspaceEntry,
        *,
        actor: str,
        context: dict[str, Any] | None,
        checkpoint_kind: str | None = None,
    ) -> WorkspaceVersionSnapshotPlan:
        effective_checkpoint = checkpoint_kind or self._default_checkpoint_kind(actor, initial=True)
        version_id = str(uuid.uuid4())
        row = self._version_row(
            version_id=version_id,
            entry_id=str(entry.entry_id),
            user_id=str(entry.user_id),
            sequence=1,
            parent_version_id=None,
            sha256=entry.sha256,
            size_bytes=int(entry.size_bytes or 0),
            mime_type=entry.mime_type,
            actor=actor,
            context=context,
            checkpoint_kind=effective_checkpoint,
        )
        return WorkspaceVersionSnapshotPlan(row, str(entry.relative_path))

    def _plan_file_version_update(
        self,
        entry: WorkspaceEntry,
        *,
        new_sha256: str,
        new_size_bytes: int,
        new_mime_type: str | None,
        actor: str,
        context: dict[str, Any] | None,
        checkpoint_kind: str | None = None,
    ) -> tuple[WorkspaceVersionSnapshotPlan | None, WorkspaceVersionSnapshotPlan]:
        entry_id = str(entry.entry_id)
        user_id = str(entry.user_id)
        source_path = str(entry.relative_path)
        base_plan: WorkspaceVersionSnapshotPlan | None = None
        parent_version_id = entry.current_version_id
        if parent_version_id:
            current = self.db.query(WorkspaceFileVersion).filter(
                WorkspaceFileVersion.version_id == parent_version_id,
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.entry_id == entry_id,
            ).one_or_none()
            if current is None:
                raise WorkspaceError(409, "VERSION_HISTORY_INVALID", "文件当前版本记录不存在")
            next_sequence = int(current.sequence) + 1
        else:
            base_version_id = str(uuid.uuid4())
            base_row = self._version_row(
                version_id=base_version_id,
                entry_id=entry_id,
                user_id=user_id,
                sequence=1,
                parent_version_id=None,
                sha256=entry.sha256,
                size_bytes=int(entry.size_bytes or 0),
                mime_type=entry.mime_type,
                actor=actor,
                context=context,
                checkpoint_kind="legacy_head",
            )
            base_plan = WorkspaceVersionSnapshotPlan(base_row, source_path)
            parent_version_id = base_version_id
            next_sequence = 2
        next_version_id = str(uuid.uuid4())
        next_row = self._version_row(
            version_id=next_version_id,
            entry_id=entry_id,
            user_id=user_id,
            sequence=next_sequence,
            parent_version_id=parent_version_id,
            sha256=new_sha256,
            size_bytes=new_size_bytes,
            mime_type=new_mime_type,
            actor=actor,
            context=context,
            checkpoint_kind=(
                checkpoint_kind
                if checkpoint_kind is not None
                else self._default_checkpoint_kind(actor, initial=False)
            ),
        )
        return base_plan, WorkspaceVersionSnapshotPlan(next_row, source_path)

    async def _snapshot_version(
        self,
        store: WorkspaceStore,
        plan: WorkspaceVersionSnapshotPlan,
    ) -> None:
        snapshot = await store.copy_version_snapshot(
            source_relative_path=plan.source_relative_path,
            destination_relative_path=str(plan.version_row["content_path"]),
            expected_sha256=plan.version_row.get("sha256"),
            expected_size_bytes=int(plan.version_row["size_bytes"]),
        )
        if (
            int(snapshot.size_bytes) != int(plan.version_row["size_bytes"])
            or snapshot.sha256 != plan.version_row.get("sha256")
        ):
            raise WorkspaceError(409, "VERSION_SNAPSHOT_CHANGED", "文件在保存版本快照时发生变化")

    def _persist_content_object_record(
        self,
        *,
        user_id: str,
        blob_id: str,
        sha256: str,
        size_bytes: int,
        content_path: str,
    ) -> int:
        if not blob_id or not content_path or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise WorkspaceError(409, "CONTENT_OBJECT_INVALID", "内容对象记录无效")
        content_object = self.db.get(WorkspaceContentObject, blob_id)
        history_bytes_delta = 0
        if content_object is None:
            try:
                with self.db.begin_nested():
                    content_object = WorkspaceContentObject(
                        blob_id=blob_id,
                        user_id=user_id,
                        sha256=sha256,
                        size_bytes=int(size_bytes),
                        content_path=content_path,
                        state="materialized",
                        last_accessed_at=now_naive(),
                    )
                    self.db.add(content_object)
                    self.db.flush()
                history_bytes_delta = int(size_bytes)
            except IntegrityError:
                content_object = self.db.query(WorkspaceContentObject).filter(
                    WorkspaceContentObject.user_id == user_id,
                    WorkspaceContentObject.sha256 == sha256,
                ).one_or_none()
        if (
            content_object is None
            or content_object.user_id != user_id
            or content_object.sha256 != sha256
            or int(content_object.size_bytes) != int(size_bytes)
            or content_object.content_path != content_path
        ):
            raise WorkspaceError(409, "CONTENT_OBJECT_COLLISION", "内容对象记录冲突")
        if content_object.state == "pruning":
            raise WorkspaceError(409, "CONTENT_OBJECT_PRUNING", "内容对象正在回收，请重试")
        if content_object.state == "pruned":
            content_object.state = "materialized"
            content_object.pruned_at = None
            history_bytes_delta = int(size_bytes)
        elif content_object.state != "materialized":
            raise WorkspaceError(409, "CONTENT_OBJECT_COLLISION", "内容对象状态无效")
        content_object.last_accessed_at = now_naive()
        return history_bytes_delta

    def _register_prepared_content_object(
        self,
        user_id: str,
        version_row: dict[str, Any],
    ) -> None:
        history_delta = self._persist_content_object_record(
            user_id=user_id,
            blob_id=str(version_row.get("blob_id") or ""),
            sha256=str(version_row.get("sha256") or ""),
            size_bytes=int(version_row.get("size_bytes") or 0),
            content_path=str(version_row.get("content_path") or ""),
        )
        workspace = self.db.query(UserWorkspace).filter(
            UserWorkspace.user_id == user_id,
        ).with_for_update().one()
        workspace.history_used_bytes = int(workspace.history_used_bytes or 0) + history_delta
        self.db.commit()

    def _persist_version_rows(self, version_rows: Any) -> int:
        if version_rows is None:
            return 0
        if not isinstance(version_rows, list) or not all(isinstance(item, dict) for item in version_rows):
            raise WorkspaceError(409, "MUTATION_FENCED", "文件版本记录无效")
        history_bytes_delta = 0
        for payload in version_rows:
            normalized = dict(payload)
            retained_until = normalized.get("retained_until")
            if isinstance(retained_until, str):
                normalized["retained_until"] = datetime.fromisoformat(retained_until)
            version_id = str(payload.get("version_id") or "")
            existing = self.db.get(WorkspaceFileVersion, version_id)
            if existing is not None:
                if existing.sha256 != payload.get("sha256") or existing.entry_id != payload.get("entry_id"):
                    raise WorkspaceError(409, "MUTATION_FENCED", "文件版本记录已被占用")
                continue
            blob_id = str(normalized.get("blob_id") or "")
            blob_sha = str(normalized.get("sha256") or "")
            blob_path = str(normalized.get("content_path") or "")
            blob_size = int(normalized.get("size_bytes") or 0)
            history_bytes_delta += self._persist_content_object_record(
                user_id=str(normalized["user_id"]),
                blob_id=blob_id,
                sha256=blob_sha,
                size_bytes=blob_size,
                content_path=blob_path,
            )
            self.db.add(WorkspaceFileVersion(**normalized))
        return history_bytes_delta

    def _upsert_content_reference(
        self,
        *,
        user_id: str,
        blob_id: str,
        version_id: str | None,
        reference_kind: str,
        reference_key: str,
        retained_until: datetime | None = None,
    ) -> WorkspaceContentReference:
        reference = self.db.query(WorkspaceContentReference).filter(
            WorkspaceContentReference.user_id == user_id,
            WorkspaceContentReference.reference_kind == reference_kind,
            WorkspaceContentReference.reference_key == reference_key,
        ).with_for_update().one_or_none()
        if reference is None:
            reference = WorkspaceContentReference(
                reference_id=str(uuid.uuid4()),
                user_id=user_id,
                blob_id=blob_id,
                version_id=version_id,
                reference_kind=reference_kind,
                reference_key=reference_key,
                retained_until=retained_until,
            )
            self.db.add(reference)
        else:
            reference.blob_id = blob_id
            reference.version_id = version_id
            reference.retained_until = retained_until
            reference.updated_at = now_naive()
        return reference

    def _sync_entry_head_reference(self, entry: WorkspaceEntry) -> None:
        if entry.kind != "file" or not entry.head_blob_id or not entry.current_version_id:
            return
        self._upsert_content_reference(
            user_id=str(entry.user_id),
            blob_id=str(entry.head_blob_id),
            version_id=str(entry.current_version_id),
            reference_kind="entry_head",
            reference_key=str(entry.entry_id),
        )

    @staticmethod
    def _content_object_claim_specs(
        version_rows: Iterable[dict[str, Any]],
    ) -> tuple[WorkspaceClaimSpec, ...]:
        blob_ids = sorted(
            {str(row.get("blob_id") or "") for row in version_rows if row.get("blob_id")}
        )
        return tuple(
            WorkspaceClaimSpec("path", f"object:{blob_id}")
            for blob_id in blob_ids
        )

    def _increment_tree_revisions(self, user_id: str, journal: dict[str, Any]) -> None:
        raw_ids = journal.get("tree_revision_entry_ids") or []
        if not isinstance(raw_ids, list):
            raise WorkspaceError(409, "MUTATION_FENCED", "目录版本更新范围无效")
        entry_ids = tuple(sorted({str(item) for item in raw_ids if item}))
        if not entry_ids:
            return
        self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.entry_id.in_(entry_ids),
            WorkspaceEntry.kind == "directory",
        ).update(
            {WorkspaceEntry.tree_revision: WorkspaceEntry.tree_revision + 1},
            synchronize_session=False,
        )

    def _apply_journal_projection(
        self,
        user_id: str,
        projection: dict[str, Any],
    ) -> WorkspaceEntry:
        values = dict(projection)
        entry = self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.entry_id == values.get("entry_id"),
        ).with_for_update().first()
        if entry is None:
            entry = WorkspaceEntry(**values)
            self.db.add(entry)
        else:
            for field_name, value in values.items():
                if field_name not in {"entry_id", "user_id"}:
                    setattr(entry, field_name, value)
            entry.updated_at = now_naive()
        return entry

    def _apply_journal_projection_cas(
        self,
        user_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> WorkspaceEntry:
        """Apply one prepared entry projection only from its frozen base."""

        values = dict(after)
        entry_id = str(before.get("entry_id") or "")
        query = self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.entry_id == entry_id,
            WorkspaceEntry.revision == int(before.get("revision") or 0),
            WorkspaceEntry.relative_path == before.get("relative_path"),
            WorkspaceEntry.status == before.get("status"),
            WorkspaceEntry.sha256 == before.get("sha256"),
            WorkspaceEntry.current_version_id == before.get("current_version_id"),
            WorkspaceEntry.head_blob_id == before.get("head_blob_id"),
            WorkspaceEntry.tree_revision == int(before.get("tree_revision") or 1),
        )
        update_values = {
            key: value
            for key, value in values.items()
            if key not in {"entry_id", "user_id"}
        }
        update_values["updated_at"] = now_naive()
        updated = query.update(update_values, synchronize_session=False)
        if updated != 1:
            raise WorkspaceError(
                409,
                "MUTATION_FENCED",
                "文件在本次保存完成前已被其他操作修改",
            )
        return (
            self.db.query(WorkspaceEntry)
            .populate_existing()
            .filter(
                WorkspaceEntry.user_id == user_id,
                WorkspaceEntry.entry_id == entry_id,
            )
            .one()
        )

    def _create_journal_projection(
        self,
        user_id: str,
        projection: dict[str, Any],
    ) -> WorkspaceEntry:
        values = dict(projection)
        if values.get("user_id") != user_id:
            raise WorkspaceError(409, "MUTATION_FENCED", "工作区创建记录归属无效")
        existing = self.db.query(WorkspaceEntry.entry_id).filter(
            WorkspaceEntry.user_id == user_id,
            or_(
                WorkspaceEntry.entry_id == values.get("entry_id"),
                (
                    (WorkspaceEntry.parent_key == values.get("parent_key"))
                    & (WorkspaceEntry.name == values.get("name"))
                    & (WorkspaceEntry.status == "active")
                ),
            ),
        ).first()
        if existing is not None:
            raise WorkspaceError(409, "MUTATION_FENCED", "目标名称已被其他操作占用")
        entry = WorkspaceEntry(**values)
        self.db.add(entry)
        self.db.flush()
        return entry

    async def _reconcile_user_prepared(
        self,
        workspace: UserWorkspace,
        store: WorkspaceStore,
        *,
        force: bool = False,
    ) -> int:
        user_id = str(workspace.user_id)
        query = self.db.query(WorkspaceMutation.mutation_id).filter(
            WorkspaceMutation.user_id == user_id,
            WorkspaceMutation.state == "prepared",
        )
        if not force:
            query = query.filter(
                or_(
                    WorkspaceMutation.lease_expires_at.is_(None),
                    WorkspaceMutation.lease_expires_at <= now_naive(),
                )
            )
        pending_ids = [str(row[0]) for row in query.order_by(WorkspaceMutation.created_at.asc()).all()]
        self.db.commit()
        reconciled = 0
        for mutation_id in pending_ids:
            reconcile_leases: tuple[WorkspaceClaimLease, ...] = ()
            has_claims = self.db.query(WorkspaceClaim.claim_id).filter(
                WorkspaceClaim.user_id == user_id,
                WorkspaceClaim.mutation_id == mutation_id,
                WorkspaceClaim.state == "active",
            ).first() is not None
            self.db.rollback()
            if has_claims:
                try:
                    takeover = WorkspaceMutationCoordinator(
                        self.db
                    ).takeover_expired_mutation_claims(
                        user_id=user_id,
                        mutation_id=mutation_id,
                    )
                    await store.advance_claim_fences(
                        takeover.previous,
                        takeover.current,
                    )
                    reconcile_leases = takeover.current
                except WorkspaceClaimConflict:
                    continue
                except WorkspaceError:
                    self.db.rollback()
                    logger.warning(
                        "工作区 claim 文件系统 fence 接管失败 user=%s mutation=%s",
                        user_id,
                        mutation_id,
                        exc_info=True,
                    )
                    continue
            mutation = self.db.query(WorkspaceMutation).filter(
                WorkspaceMutation.mutation_id == mutation_id,
                WorkspaceMutation.state == "prepared",
            ).first()
            if mutation is None:
                continue
            try:
                details = json.loads(mutation.details_json or "{}")
                journal = details.get("journal") if isinstance(details, dict) else None
                if not isinstance(journal, dict):
                    raise ValueError("missing journal")
                action = str(journal.get("action") or "replace")
                if action == "delete_many":
                    prepared = WorkspacePreparedMutation(mutation_id, user_id, reconcile_leases)
                    self.db.commit()
                    async with self._guard_workspace_claims(
                        store, reconcile_leases,
                    ) as heartbeat:
                        heartbeat.raise_if_lost()
                        await _complete_snapshot_before_cancellation(
                            store.delete_entries(journal["delete_paths"], journal.get("cleanup_paths", []))
                        )
                        heartbeat.raise_if_lost()
                        self._complete_prepared_delete(prepared)
                    await self._cleanup_deleted_objects(user_id, journal.get("released_blob_ids", []), store)
                    reconciled += 1
                    continue
                self.db.commit()

                async def inspect_optional(path_key: str, allow_system_key: str):
                    raw_path = journal.get(path_key)
                    if not isinstance(raw_path, str) or not raw_path:
                        return None
                    safe_path = WorkspacePathPolicy.normalize_relative_path(
                        raw_path,
                        allow_system=bool(journal.get(allow_system_key, False)),
                    )
                    try:
                        return await store.inspect_path(
                            safe_path,
                            allow_system=bool(journal.get(allow_system_key, False)),
                        )
                    except WorkspaceError as exc:
                        if exc.code == "NOT_FOUND":
                            return None
                        raise

                async def inspect_filesystem_state() -> tuple[bool, bool]:
                    target_state = await inspect_optional("target_path", "allow_system_target")
                    applied_state = False
                    not_applied_state = False
                    if action == "replace":
                        new_sha = journal.get("new_sha256")
                        old_sha = journal.get("old_sha256")
                        physical_sha = target_state.sha256 if target_state and target_state.kind == "file" else None
                        applied_state = new_sha is not None and physical_sha == new_sha
                        not_applied_state = physical_sha == old_sha or (old_sha is None and target_state is None)
                    elif action == "mkdir":
                        applied_state = target_state is not None and target_state.kind == "directory"
                        not_applied_state = target_state is None
                    elif action == "move":
                        source_state = await inspect_optional("source_path", "allow_system_source")
                        applied_state = target_state is not None and source_state is None
                        not_applied_state = source_state is not None and target_state is None
                    else:
                        raise ValueError(f"unsupported journal action: {action}")
                    version_rows = journal.get("version_rows") or []
                    if applied_state and version_rows:
                        if not isinstance(version_rows, list):
                            raise ValueError("invalid version rows")
                        target_path = str(journal.get("target_path") or "")
                        for version_row in version_rows:
                            if not isinstance(version_row, dict):
                                raise ValueError("invalid version row")
                            content_path = str(version_row.get("content_path") or "")
                            if not content_path:
                                raise ValueError("missing version content path")
                            try:
                                version_state = await store.inspect_path(
                                    content_path,
                                    allow_system=True,
                                )
                            except WorkspaceError as exc:
                                if exc.code != "NOT_FOUND":
                                    raise
                                version_state = None
                            expected_version_sha = version_row.get("sha256")
                            if version_state is None:
                                if expected_version_sha != journal.get("new_sha256"):
                                    raise WorkspaceError(
                                        409,
                                        "RECONCILIATION_CONFLICT",
                                        "历史基线版本快照缺失，不能自动补写",
                                    )
                                await store.copy_version_snapshot(
                                    source_relative_path=target_path,
                                    destination_relative_path=content_path,
                                    expected_sha256=expected_version_sha,
                                    expected_size_bytes=int(version_row.get("size_bytes") or 0),
                                )
                            elif (
                                version_state.kind != "file"
                                or version_state.sha256 != expected_version_sha
                            ):
                                raise WorkspaceError(
                                    409,
                                    "RECONCILIATION_CONFLICT",
                                    "历史版本快照内容不匹配",
                                )
                    return applied_state, not_applied_state

                async with self._guard_workspace_claims(store, reconcile_leases):
                    applied, not_applied = await inspect_filesystem_state()

                mutation = self.db.query(WorkspaceMutation).filter(
                    WorkspaceMutation.mutation_id == mutation_id,
                    WorkspaceMutation.state == "prepared",
                ).with_for_update().one_or_none()
                if mutation is None:
                    continue
                if reconcile_leases:
                    self._lock_and_assert_mutation_claims(mutation, journal)
                workspace = self.db.query(UserWorkspace).filter(
                    UserWorkspace.user_id == user_id,
                ).with_for_update().one()
                history_bytes_delta = 0

                if applied:
                    projections = journal.get("entry_projections")
                    if projections is None:
                        projection = journal.get("entry_projection")
                        projections = [projection] if isinstance(projection, dict) else []
                    if not isinstance(projections, list) or not projections:
                        raise ValueError("missing entry projection")
                    before_projections = journal.get("before_entry_projections")
                    before_projection = journal.get("before_entry_projection")
                    if journal.get("create_entry") is True:
                        projection = projections[0]
                        if not isinstance(projection, dict):
                            raise ValueError("invalid entry projection")
                        self._create_journal_projection(workspace.user_id, projection)
                    elif isinstance(before_projections, list):
                        if len(before_projections) != len(projections):
                            raise ValueError("incomplete entry projections")
                        for before, projection in zip(before_projections, projections):
                            if not isinstance(before, dict) or not isinstance(projection, dict):
                                raise ValueError("invalid entry projection")
                            self._apply_journal_projection_cas(
                                workspace.user_id,
                                before,
                                projection,
                            )
                    elif isinstance(before_projection, dict):
                        projection = projections[0]
                        if not isinstance(projection, dict):
                            raise ValueError("invalid entry projection")
                        self._apply_journal_projection_cas(
                            workspace.user_id,
                            before_projection,
                            projection,
                        )
                    else:
                        for projection in projections:
                            if not isinstance(projection, dict):
                                raise ValueError("invalid entry projection")
                            self._apply_journal_projection(workspace.user_id, projection)
                    history_bytes_delta = self._persist_version_rows(journal.get("version_rows"))
                    self._increment_tree_revisions(workspace.user_id, journal)
                    finalized_entry = self.db.query(WorkspaceEntry).filter(
                        WorkspaceEntry.user_id == workspace.user_id,
                        WorkspaceEntry.entry_id == mutation.entry_id,
                    ).one_or_none()
                    if finalized_entry is not None:
                        self._sync_entry_head_reference(finalized_entry)
                    workspace = self.db.query(UserWorkspace).filter(
                        UserWorkspace.user_id == workspace.user_id
                    ).with_for_update().one()
                    workspace.used_bytes = int(workspace.used_bytes or 0) + int(
                        journal.get("bytes_delta") or 0
                    )
                    workspace.entry_count = int(workspace.entry_count or 0) + int(
                        journal.get("entries_delta") or 0
                    )
                    workspace.history_used_bytes = int(workspace.history_used_bytes or 0) + int(
                        history_bytes_delta or 0
                    )
                    workspace.revision = int(workspace.revision or 0) + 1
                    workspace.updated_at = now_naive()
                    mutation.state = "completed"
                    mutation.result_status = str(journal.get("result_status") or mutation.result_status or "UPDATED")
                    mutation.lease_expires_at = None
                    mutation.error_code = None
                    mutation.error_message = None
                    mutation.recoverable = False
                    mutation.completed_at = now_naive()
                elif not_applied:
                    self._freeze_mutation_failure(
                        mutation,
                        status_code=409,
                        code="NOT_APPLIED",
                        message="文件系统操作未发生，已安全终止准备中的操作",
                        recoverable=True,
                    )
                else:
                    self._freeze_mutation_failure(
                        mutation,
                        status_code=409,
                        code="RECONCILIATION_CONFLICT",
                        message="文件系统状态既不匹配操作前也不匹配操作后",
                        recoverable=False,
                    )
                self._release_mutation_object_references(mutation.mutation_id)
                self._release_mutation_claims(
                    mutation.mutation_id,
                    mutation.owner_token,
                    final_state="released" if applied or not_applied else "fenced",
                )
                self.db.commit()
                reconciled += 1
            except Exception as exc:
                self.db.rollback()
                row = self.db.query(WorkspaceMutation).filter(
                    WorkspaceMutation.mutation_id == mutation_id,
                    WorkspaceMutation.state == "prepared",
                ).first()
                if row is not None:
                    row.error_code = "RECONCILIATION_RETRY"
                    row.error_message = str(exc)[:1000]
                    row.recoverable = True
                    row.lease_expires_at = now_naive() + timedelta(seconds=30)
                    self.db.commit()
        return reconciled

    async def reconcile_prepared_mutations(self, user_id: str, *, force: bool = False) -> int:
        workspace, store = await self._prepare(
            user_id,
            for_update=False,
            reconcile_prepared=False,
        )
        return await self._reconcile_user_prepared(workspace, store, force=force)

    def _record_mutation(
        self,
        *,
        workspace: UserWorkspace,
        entry: WorkspaceEntry,
        actor: str,
        operation: str,
        result_status: str,
        idempotency_key: str | None,
        context: dict[str, Any] | None,
        before_revision: int | None,
        before_sha256: str | None,
        details: dict[str, Any] | None = None,
        prepared_mutation: WorkspacePreparedMutation | None = None,
    ) -> WorkspaceMutationResult:
        if actor not in {"web", "chat", "cron", "admin"}:
            raise WorkspaceError(422, "INVALID_ACTOR", "工作区调用来源无效")
        user_id = (
            prepared_mutation.user_id
            if prepared_mutation is not None
            else str(entry.user_id)
        )
        journal_payload: dict[str, Any] | None = None
        history_bytes_delta = 0
        if prepared_mutation is None:
            frozen_details = {
                "result": details or {},
                "entry_projection": self._journal_projection(entry),
            }
            mutation = WorkspaceMutation(
                mutation_id=str(uuid.uuid4()),
                user_id=user_id,
                entry_id=entry.entry_id,
                actor=actor,
                operation=operation,
                state="completed",
                result_status=result_status,
                idempotency_key=idempotency_key,
                before_revision=before_revision,
                after_revision=int(entry.revision),
                before_sha256=before_sha256,
                after_sha256=entry.sha256,
                details_json=json.dumps(frozen_details, ensure_ascii=False, separators=(",", ":")),
                completed_at=now_naive(),
                **self._context_fields(context),
            )
            self.db.add(mutation)
        else:
            # Do not flush stale per-request workspace counters before the
            # authoritative row is locked below. Different files may finish
            # concurrently after their prepared journals are committed.
            with self.db.no_autoflush:
                mutation = self._owned_prepared_mutation_query(
                    prepared_mutation
                ).with_for_update().first()
            if mutation is None:
                raise WorkspaceError(409, "MUTATION_FENCED", "工作区操作的准备记录已失效")
            try:
                prepared_details = json.loads(mutation.details_json or "{}")
            except json.JSONDecodeError:
                prepared_details = {}
            if not isinstance(prepared_details, dict):
                prepared_details = {}
            raw_journal = prepared_details.get("journal")
            journal_payload = raw_journal if isinstance(raw_journal, dict) else {}
            self._lock_and_assert_mutation_claims(
                mutation,
                journal_payload,
                prepared_mutation.leases,
            )
            after_projection = journal_payload.get("entry_projection")
            if journal_payload.get("create_entry") is True and isinstance(after_projection, dict):
                entry = self._create_journal_projection(user_id, after_projection)
            before_projection = journal_payload.get("before_entry_projection")
            if isinstance(before_projection, dict) and isinstance(after_projection, dict):
                entry = self._apply_journal_projection_cas(
                    user_id,
                    before_projection,
                    after_projection,
                )
            before_projections = journal_payload.get("before_entry_projections")
            after_projections = journal_payload.get("entry_projections")
            if isinstance(before_projections, list) and isinstance(after_projections, list):
                if len(before_projections) != len(after_projections):
                    raise WorkspaceError(409, "MUTATION_FENCED", "工作区目录修改记录不完整")
                finalized_entries = [
                    self._apply_journal_projection_cas(user_id, before_item, after_item)
                    for before_item, after_item in zip(before_projections, after_projections)
                    if isinstance(before_item, dict) and isinstance(after_item, dict)
                ]
                if len(finalized_entries) != len(before_projections):
                    raise WorkspaceError(409, "MUTATION_FENCED", "工作区目录修改记录无效")
                entry = next(
                    (item for item in finalized_entries if item.entry_id == mutation.entry_id),
                    entry,
                )
            history_bytes_delta = self._persist_version_rows(journal_payload.get("version_rows"))
            self._sync_entry_head_reference(entry)
            self._increment_tree_revisions(user_id, journal_payload)
            prepared_details["result"] = details or {}
            mutation.details_json = json.dumps(
                prepared_details,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            mutation.state = "completed"
            mutation.result_status = result_status
            mutation.after_revision = int(entry.revision)
            mutation.after_sha256 = entry.sha256
            mutation.lease_expires_at = None
            mutation.error_code = None
            mutation.error_message = None
            mutation.completed_at = now_naive()
            self._release_mutation_object_references(mutation.mutation_id)
            self._release_mutation_claims(mutation.mutation_id, mutation.owner_token)
        if prepared_mutation is not None:
            with self.db.no_autoflush:
                workspace = (
                    self.db.query(UserWorkspace)
                    .populate_existing()
                    .filter(UserWorkspace.user_id == user_id)
                    .with_for_update()
                    .one()
                )
            workspace.used_bytes = int(workspace.used_bytes or 0) + int(
                (journal_payload or {}).get("bytes_delta") or 0
            )
            workspace.entry_count = int(workspace.entry_count or 0) + int(
                (journal_payload or {}).get("entries_delta") or 0
            )
            workspace.history_used_bytes = int(workspace.history_used_bytes or 0) + int(
                history_bytes_delta
            )
        else:
            with self.db.no_autoflush:
                workspace = (
                    self.db.query(UserWorkspace)
                    .populate_existing()
                    .filter(UserWorkspace.user_id == user_id)
                    .with_for_update()
                    .one()
                )
        workspace.revision = int(workspace.revision or 0) + 1
        workspace.updated_at = now_naive()
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            winner = self._idempotent_result(user_id, idempotency_key, operation)
            if winner is not None:
                return winner
            raise WorkspaceError(409, "NAME_CONFLICT", "工作区目标名称已存在") from exc
        self.db.refresh(entry)
        return WorkspaceMutationResult(result_status, entry, mutation.mutation_id)

    async def get_entry(
        self,
        user_id: str,
        entry_id: str,
    ) -> WorkspaceEntry:
        await self._prepare(
            user_id,
            for_update=False,
            require_filesystem=False,
        )
        return self._entry(user_id, entry_id)

    async def get_entry_by_path(self, user_id: str, relative_path: str) -> WorkspaceEntry:
        await self._prepare(
            user_id,
            for_update=False,
            require_filesystem=False,
        )
        safe_path = WorkspacePathPolicy.normalize_relative_path(relative_path)
        entry = self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.relative_path == safe_path,
            WorkspaceEntry.status == "active",
        ).first()
        if entry is None:
            raise WorkspaceError(404, "ENTRY_NOT_FOUND", "工作区文件不存在")
        return entry

    async def list_entries(
        self,
        user_id: str,
        *,
        parent_id: str | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> WorkspaceEntryPage:
        workspace, _store = await self._prepare(
            user_id,
            for_update=False,
            require_filesystem=False,
        )
        limit = max(1, min(int(limit), 200))
        offset = _decode_cursor(cursor)
        query = self.db.query(WorkspaceEntry).filter(WorkspaceEntry.user_id == user_id)
        query = query.filter(WorkspaceEntry.status == "active")
        normalized_q = (q or "").strip()
        if normalized_q:
            escaped = normalized_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.filter(WorkspaceEntry.relative_path.ilike(f"%{escaped}%", escape="\\"))
        else:
            query = query.filter(WorkspaceEntry.parent_key == (parent_id or _ROOT_PARENT_KEY))
        rows = (
            query.order_by(
                case((WorkspaceEntry.kind == "directory", 0), else_=1).asc(),
                func.lower(WorkspaceEntry.name).asc(),
                WorkspaceEntry.entry_id.asc(),
            )
            .offset(offset)
            .limit(limit + 1)
            .all()
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        return WorkspaceEntryPage(
            items=items,
            next_cursor=_encode_cursor(offset + limit) if has_more else None,
            workspace_revision=int(workspace.revision or 0),
        )

    async def create_directory(
        self,
        user_id: str,
        parent_id: str | None,
        name: str,
        *,
        actor: str = "web",
        idempotency_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> WorkspaceMutationResult:
        safe_name = WorkspacePathPolicy.validate_name(name)
        workspace, store = await self._prepare(user_id, for_update=True)
        previous = self._idempotent_result(user_id, idempotency_key, "create_directory")
        if previous:
            return previous
        parent = self._parent(user_id, parent_id)
        if parent and _workspace_path_depth(parent.relative_path) >= MAX_WORKSPACE_DIRECTORY_DEPTH:
            raise WorkspaceError(422, "DIRECTORY_DEPTH_LIMIT", "文件夹最多支持两层")
        if self._sibling(user_id, parent_id, safe_name, for_update=False):
            raise WorkspaceError(409, "NAME_CONFLICT", "目标名称已存在")
        relative_path = WorkspacePathPolicy.join(parent.relative_path if parent else None, safe_name)
        entry = WorkspaceEntry(
            entry_id=str(uuid.uuid4()),
            user_id=user_id,
            parent_id=parent_id,
            parent_key=parent_id or _ROOT_PARENT_KEY,
            name=safe_name,
            kind="directory",
            relative_path=relative_path,
            revision=1,
            status="active",
        )
        entry_projection = self._journal_projection(entry)
        parent_projection = self._journal_projection(parent) if parent else None
        ancestor_ids = self._ancestor_entry_ids(user_id, parent_id)
        path_claim = WorkspaceClaimSpec(
            "path",
            path_scope(parent_id, safe_name),
            parent_id,
            conflict_scope_keys=self._tree_scope_keys(ancestor_ids),
        )
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace,
            workspace_user_id=user_id,
            entry_id=entry.entry_id,
            actor=actor,
            operation="create_directory",
            result_status="CREATED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=None,
            before_sha256=None,
            after_revision=1,
            after_sha256=None,
            journal={
                "action": "mkdir",
                "target_path": relative_path,
                "bytes_delta": 0,
                "entries_delta": 1,
                "tree_revision_entry_ids": ancestor_ids,
                "create_entry": True,
                "entry_projection": entry_projection,
                "base_entry_projections": [parent_projection] if parent_projection else [],
                "destination_expectation": {
                    "parent_key": parent_id or _ROOT_PARENT_KEY,
                    "name": safe_name,
                },
            },
            claim_specs=(path_claim,),
        )
        try:
            async with self._guard_workspace_claims(store, prepared.leases):
                await store.mkdir(relative_path)
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(
                409,
                "MUTATION_FENCED",
                "文件夹创建所有权已失效，请刷新后重试",
            ) from exc
        except WorkspaceError as exc:
            if exc.code in {"NAME_CONFLICT", "NOT_FOUND", "SYMLINK_REJECTED", "NOT_DIRECTORY"}:
                self._fail_prepared_mutation(
                    prepared,
                    code=exc.code,
                    message=exc.message,
                    recoverable=True,
                    status_code=exc.status_code,
                    error_extra=exc.extra,
                )
            raise
        return self._record_mutation(
            workspace=workspace,
            entry=entry,
            actor=actor,
            operation="create_directory",
            result_status="CREATED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=None,
            before_sha256=None,
            details={"parent_id": parent_id, "name": safe_name},
            prepared_mutation=prepared,
        )

    async def create_file(
        self,
        user_id: str,
        parent_id: str | None,
        name: str,
        *,
        file_type: Literal["markdown", "xlsx"],
        actor: str = "web",
        idempotency_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> WorkspaceMutationResult:
        safe_name = WorkspacePathPolicy.validate_name(name)
        if file_type == "markdown" and posixpath.splitext(safe_name)[1].lower() not in {".md", ".markdown"}:
            safe_name += ".md"
        elif file_type == "xlsx" and not safe_name.lower().endswith(".xlsx"):
            safe_name += ".xlsx"
        content = b"" if file_type == "markdown" else _blank_xlsx_bytes()
        return await self._create_or_upload_file(
            user_id,
            parent_id,
            safe_name,
            content,
            actor=actor,
            idempotency_key=idempotency_key,
            context=context,
            operation="create_file",
        )

    async def upload_file(
        self,
        user_id: str,
        parent_id: str | None,
        name: str,
        content: bytes,
        *,
        actor: str = "web",
        idempotency_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> WorkspaceMutationResult:
        return await self._create_or_upload_file(
            user_id,
            parent_id,
            name,
            content,
            actor=actor,
            idempotency_key=idempotency_key,
            context=context,
            operation="upload_file",
        )

    async def upload_file_stream(
        self,
        user_id: str,
        parent_id: str | None,
        name: str,
        source: Any,
        *,
        declared_size: int | None = None,
        actor: str = "web",
        idempotency_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> WorkspaceMutationResult:
        safe_name = WorkspacePathPolicy.validate_name(name)
        max_file_bytes = int(self.settings.workspace_max_file_bytes)
        if declared_size is not None and (declared_size < 0 or declared_size > max_file_bytes):
            raise WorkspaceError(413, "FILE_TOO_LARGE", "文件超过工作区单文件大小限制")
        workspace, store = await self._prepare(user_id, for_update=True)
        previous = self._idempotent_result(user_id, idempotency_key, "upload_file")
        if previous:
            return previous
        parent = self._parent(user_id, parent_id)
        if self._sibling(user_id, parent_id, safe_name, for_update=False):
            raise WorkspaceError(409, "NAME_CONFLICT", "目标名称已存在")
        self._check_capacity(
            workspace,
            bytes_delta=int(declared_size or 0),
            entries_delta=1,
        )
        relative_path = WorkspacePathPolicy.join(parent.relative_path if parent else None, safe_name)
        mime_type, _ = mimetypes.guess_type(safe_name)
        entry_id = str(uuid.uuid4())
        parent_projection = self._journal_projection(parent) if parent else None
        ancestor_ids = self._ancestor_entry_ids(user_id, parent_id)
        path_claim = WorkspaceClaimSpec(
            "path",
            path_scope(parent_id, safe_name),
            parent_id,
            conflict_scope_keys=self._tree_scope_keys(ancestor_ids),
        )
        self.db.rollback()
        temp_relative, staged = await store.stage_upload_stream(
            source,
            max_bytes=max_file_bytes,
        )
        planned_entry = WorkspaceEntry(
            entry_id=entry_id,
            user_id=user_id,
            parent_id=parent_id,
            parent_key=parent_id or _ROOT_PARENT_KEY,
            name=safe_name,
            kind="file",
            relative_path=relative_path,
            size_bytes=staged.size_bytes,
            mime_type=mime_type or "application/octet-stream",
            sha256=staged.sha256,
            revision=1,
            status="active",
        )
        try:
            version_plan = self._plan_initial_file_version(
                planned_entry,
                actor=actor,
                context=context,
            )
        except (Exception, asyncio.CancelledError):
            try:
                await store.remove(temp_relative)
            except WorkspaceError as cleanup_error:
                if cleanup_error.code != "NOT_FOUND":
                    logger.warning("上传版本规划失败后的临时文件清理失败 path=%s", temp_relative)
            except Exception:
                logger.warning(
                    "上传版本规划失败后的临时文件清理失败 path=%s",
                    temp_relative,
                    exc_info=True,
                )
            raise
        entry_projection = self._journal_projection(planned_entry)
        entry_projection["current_version_id"] = version_plan.version_row["version_id"]
        entry_projection["head_blob_id"] = version_plan.version_row["blob_id"]
        prepared: WorkspacePreparedMutation | None = None
        try:
            prepared = self._begin_prepared_mutation(
                workspace=workspace,
                workspace_user_id=user_id,
                entry_id=entry_id,
                actor=actor,
                operation="upload_file",
                result_status="CREATED",
                idempotency_key=idempotency_key,
                context=context,
                before_revision=None,
                before_sha256=None,
                after_revision=1,
                after_sha256=staged.sha256,
                after_version_id=str(version_plan.version_row["version_id"]),
                journal={
                    "target_path": relative_path,
                    "temp_path": temp_relative,
                    "old_sha256": None,
                    "new_sha256": staged.sha256,
                    "bytes_delta": staged.size_bytes,
                    "entries_delta": 1,
                    "tree_revision_entry_ids": ancestor_ids,
                    "create_entry": True,
                    "entry_projection": entry_projection,
                    "version_rows": [version_plan.version_row],
                    "base_entry_projections": [parent_projection] if parent_projection else [],
                    "destination_expectation": {
                        "parent_key": parent_id or _ROOT_PARENT_KEY,
                        "name": safe_name,
                    },
                },
                claim_specs=(path_claim,),
            )
        except (Exception, asyncio.CancelledError):
            try:
                await store.remove(temp_relative)
            except WorkspaceError as cleanup_error:
                if cleanup_error.code != "NOT_FOUND":
                    logger.warning("未取得 claim 的上传临时文件清理失败 path=%s", temp_relative)
            except Exception:
                logger.warning(
                    "未取得 claim 的上传临时文件清理失败 path=%s",
                    temp_relative,
                    exc_info=True,
                )
            raise

        mutation_result: WorkspaceMutationResult | None = None
        installed_to_workspace = False
        try:
            async with self._guard_workspace_claims(
                store, prepared.leases,
            ) as heartbeat:
                heartbeat.raise_if_lost()
                stat_result = await _complete_snapshot_before_cancellation(
                    store.install_staged_file(
                        staged_relative_path=temp_relative,
                        destination_relative_path=relative_path,
                        expected_destination_sha256=None,
                        must_not_exist=True,
                    )
                )
                installed_to_workspace = True
                if (
                    stat_result.sha256 != staged.sha256
                    or int(stat_result.size_bytes) != int(staged.size_bytes)
                ):
                    raise WorkspaceError(503, "UPLOAD_HASH_MISMATCH", "上传文件校验失败")
                heartbeat.raise_if_lost()
                await _complete_snapshot_before_cancellation(
                    self._snapshot_version(store, version_plan)
                )
                heartbeat.raise_if_lost()
                mutation_result = self._record_mutation(
                    workspace=workspace,
                    entry=planned_entry,
                    actor=actor,
                    operation="upload_file",
                    result_status="CREATED",
                    idempotency_key=idempotency_key,
                    context=context,
                    before_revision=None,
                    before_sha256=None,
                    details={
                        "parent_id": parent_id,
                        "name": safe_name,
                        "size_bytes": stat_result.size_bytes,
                    },
                    prepared_mutation=prepared,
                )
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(
                409,
                "MUTATION_FENCED",
                "上传操作所有权已失效，请刷新后重试",
            ) from exc
        except WorkspaceError as exc:
            if not installed_to_workspace and exc.code in {
                "NAME_CONFLICT", "DESTINATION_CHANGED", "NOT_FOUND", "SYMLINK_REJECTED", "NOT_FILE"
            }:
                self._fail_prepared_mutation(
                    prepared,
                    code=exc.code,
                    message=exc.message,
                    recoverable=True,
                    status_code=exc.status_code,
                    error_extra=exc.extra,
                )
            raise
        if mutation_result is None:
            raise WorkspaceError(503, "UPLOAD_FINALIZATION_FAILED", "上传文件落库失败")
        return mutation_result

    async def _create_or_upload_file(
        self,
        user_id: str,
        parent_id: str | None,
        name: str,
        content: bytes,
        *,
        actor: str,
        idempotency_key: str | None,
        context: dict[str, Any] | None,
        operation: str,
    ) -> WorkspaceMutationResult:
        safe_name = WorkspacePathPolicy.validate_name(name)
        max_file_bytes = int(self.settings.workspace_max_file_bytes)
        if len(content) > max_file_bytes:
            raise WorkspaceError(413, "FILE_TOO_LARGE", "文件超过工作区单文件大小限制")
        workspace, store = await self._prepare(user_id, for_update=True)
        previous = self._idempotent_result(user_id, idempotency_key, operation)
        if previous:
            return previous
        parent = self._parent(user_id, parent_id)
        if self._sibling(user_id, parent_id, safe_name, for_update=False):
            raise WorkspaceError(409, "NAME_CONFLICT", "目标名称已存在")
        relative_path = WorkspacePathPolicy.join(parent.relative_path if parent else None, safe_name)
        mime_type, _ = mimetypes.guess_type(safe_name)
        entry = WorkspaceEntry(
            entry_id=str(uuid.uuid4()),
            user_id=user_id,
            parent_id=parent_id,
            parent_key=parent_id or _ROOT_PARENT_KEY,
            name=safe_name,
            kind="file",
            relative_path=relative_path,
            size_bytes=len(content),
            mime_type=mime_type or "application/octet-stream",
            sha256=hashlib.sha256(content).hexdigest(),
            revision=1,
            status="active",
        )
        entry_id = str(entry.entry_id)
        version_plan = self._plan_initial_file_version(entry, actor=actor, context=context)
        entry_projection = self._journal_projection(entry)
        entry_projection["current_version_id"] = version_plan.version_row["version_id"]
        entry_projection["head_blob_id"] = version_plan.version_row["blob_id"]
        parent_projection = self._journal_projection(parent) if parent else None
        ancestor_ids = self._ancestor_entry_ids(user_id, parent_id)
        path_claim = WorkspaceClaimSpec(
            "path",
            path_scope(parent_id, safe_name),
            parent_id,
            conflict_scope_keys=self._tree_scope_keys(ancestor_ids),
        )
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace,
            workspace_user_id=user_id,
            entry_id=entry_id,
            actor=actor,
            operation=operation,
            result_status="CREATED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=None,
            before_sha256=None,
            after_revision=1,
            after_sha256=entry.sha256,
            after_version_id=str(version_plan.version_row["version_id"]),
            journal={
                "target_path": relative_path,
                "old_sha256": None,
                "new_sha256": entry.sha256,
                "bytes_delta": len(content),
                "entries_delta": 1,
                "tree_revision_entry_ids": ancestor_ids,
                "create_entry": True,
                "entry_projection": entry_projection,
                "version_rows": [version_plan.version_row],
                "base_entry_projections": [parent_projection] if parent_projection else [],
                "destination_expectation": {
                    "parent_key": parent_id or _ROOT_PARENT_KEY,
                    "name": safe_name,
                },
            },
            claim_specs=(path_claim,),
        )
        installed_to_workspace = False
        try:
            async with self._guard_workspace_claims(store, prepared.leases):
                if content:
                    staged_path = await store.stage_bytes_for_install(
                        content,
                        temp_token=prepared.mutation_id,
                    )
                    WorkspaceMutationCoordinator(self.db).renew_claims(
                        prepared.leases,
                        lease_seconds=int(self.settings.workspace_mutation_lease_seconds),
                    )
                    stat_result = await store.install_staged_file(
                        staged_relative_path=staged_path,
                        destination_relative_path=relative_path,
                        expected_destination_sha256=None,
                        must_not_exist=True,
                    )
                else:
                    stat_result = await store.write_bytes_atomic(
                        relative_path,
                        content,
                        must_not_exist=True,
                        temp_token=prepared.mutation_id,
                    )
                installed_to_workspace = True
                await self._snapshot_version(store, version_plan)
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(
                409,
                "MUTATION_FENCED",
                "文件创建所有权已失效，请刷新后重试",
            ) from exc
        except WorkspaceError as exc:
            if not installed_to_workspace and exc.code in {"NAME_CONFLICT", "DESTINATION_CHANGED", "NOT_FOUND", "SYMLINK_REJECTED", "NOT_FILE"}:
                self._fail_prepared_mutation(
                    prepared,
                    code=exc.code,
                    message=exc.message,
                    recoverable=True,
                    status_code=exc.status_code,
                    error_extra=exc.extra,
                )
            raise
        return self._record_mutation(
            workspace=workspace,
            entry=entry,
            actor=actor,
            operation=operation,
            result_status="CREATED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=None,
            before_sha256=None,
            details={"parent_id": parent_id, "name": safe_name, "size_bytes": stat_result.size_bytes},
            prepared_mutation=prepared,
        )

    async def write_content(
        self,
        user_id: str,
        entry_id: str,
        content: bytes,
        expected_revision: int | str,
        *,
        actor: str = "web",
        context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        _operation: str = "write_content",
        _change_set_id: str | None = None,
        _auto_merged: bool = False,
        _content_validated: bool = False,
    ) -> WorkspaceMutationResult:
        expected = WorkspacePathPolicy.normalize_revision(expected_revision)
        workspace, store = await self._prepare(user_id, for_update=True)
        previous = self._idempotent_result(user_id, idempotency_key, _operation)
        if previous:
            return previous
        entry = self._entry(user_id, entry_id)
        if entry.kind != "file":
            raise WorkspaceError(400, "NOT_FILE", "目标不是文件")
        if int(entry.revision) != expected:
            raise WorkspaceError(409, "REVISION_CONFLICT", "文件已被其他操作修改", entry=entry)
        max_file_bytes = int(self.settings.workspace_max_file_bytes)
        if len(content) > max_file_bytes:
            raise WorkspaceError(413, "FILE_TOO_LARGE", "文件超过工作区单文件大小限制")
        if not _content_validated:
            await _validate_edit_content(entry.name, content)
        new_sha256 = hashlib.sha256(content).hexdigest()
        if entry.sha256 == new_sha256 and int(entry.size_bytes or 0) == len(content):
            return self._record_mutation(
                workspace=workspace,
                entry=entry,
                actor=actor,
                operation=_operation,
                result_status="NO_CHANGE",
                idempotency_key=idempotency_key,
                context=context,
                before_revision=int(entry.revision),
                before_sha256=entry.sha256,
                details={"size_bytes": len(content), "auto_merged": _auto_merged},
            )
        delta = len(content) - int(entry.size_bytes or 0)
        before_revision = int(entry.revision)
        before_sha = entry.sha256
        target_path = str(entry.relative_path)
        base_version_plan, next_version_plan = self._plan_file_version_update(
            entry,
            new_sha256=new_sha256,
            new_size_bytes=len(content),
            new_mime_type=entry.mime_type,
            actor=actor,
            context=context,
        )
        before_projection = self._journal_projection(entry)
        planned_projection = dict(before_projection)
        planned_projection.update(
            {
                "size_bytes": len(content),
                "sha256": new_sha256,
                "revision": before_revision + 1,
                "current_version_id": next_version_plan.version_row["version_id"],
                "head_blob_id": next_version_plan.version_row["blob_id"],
            }
        )
        version_rows = [
            *( [base_version_plan.version_row] if base_version_plan else [] ),
            next_version_plan.version_row,
        ]
        ancestor_ids = self._ancestor_entry_ids(user_id, entry.parent_id)
        claim_specs = (
            WorkspaceClaimSpec(
                "file",
                file_scope(entry_id),
                entry_id,
                conflict_scope_keys=self._tree_scope_keys(ancestor_ids),
            ),
        )
        # From this point until the Sandbox operation finishes, carry only
        # frozen values. Production sessions expire ORM rows on commit.
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace,
            workspace_user_id=user_id,
            entry_id=entry_id,
            actor=actor,
            operation=_operation,
            result_status="UPDATED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=before_revision,
            before_sha256=before_sha,
            after_revision=before_revision + 1,
            after_sha256=planned_projection["sha256"],
            before_version_id=before_projection.get("current_version_id"),
            after_version_id=str(next_version_plan.version_row["version_id"]),
            change_set_id=_change_set_id,
            journal={
                "target_path": target_path,
                "old_sha256": before_sha,
                "new_sha256": planned_projection["sha256"],
                "bytes_delta": delta,
                "entries_delta": 0,
                "tree_revision_entry_ids": ancestor_ids,
                "before_entry_projection": before_projection,
                "entry_projection": planned_projection,
                "version_rows": version_rows,
            },
            claim_specs=claim_specs,
        )
        installed_to_workspace = False
        try:
            async with self._guard_workspace_claims(store, prepared.leases):
                if base_version_plan is not None:
                    await self._snapshot_version(store, base_version_plan)
                if content:
                    staged_path = await store.stage_bytes_for_install(
                        content,
                        temp_token=prepared.mutation_id,
                    )
                    WorkspaceMutationCoordinator(self.db).renew_claims(
                        prepared.leases,
                        lease_seconds=int(self.settings.workspace_mutation_lease_seconds),
                    )
                    stat_result = await store.install_staged_file(
                        staged_relative_path=staged_path,
                        destination_relative_path=target_path,
                        expected_destination_sha256=before_sha,
                        must_not_exist=False,
                    )
                else:
                    stat_result = await store.write_bytes_atomic(
                        target_path,
                        content,
                        expected_sha256=before_sha,
                        temp_token=prepared.mutation_id,
                    )
                installed_to_workspace = True
                await self._snapshot_version(store, next_version_plan)
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(
                409,
                "MUTATION_FENCED",
                "文件保存所有权已失效，请刷新后重试",
            ) from exc
        except WorkspaceError as exc:
            if not installed_to_workspace and exc.code in {"NAME_CONFLICT", "DESTINATION_CHANGED", "NOT_FOUND", "SYMLINK_REJECTED", "NOT_FILE"}:
                self._fail_prepared_mutation(
                    prepared,
                    code=exc.code,
                    message=exc.message,
                    recoverable=True,
                    status_code=exc.status_code,
                    error_extra=exc.extra,
                )
            raise
        return self._record_mutation(
            workspace=workspace,
            entry=entry,
            actor=actor,
            operation=_operation,
            result_status="UPDATED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=before_revision,
            before_sha256=before_sha,
            details={"size_bytes": stat_result.size_bytes, "auto_merged": _auto_merged},
            prepared_mutation=prepared,
        )

    async def write_content_auto_merge(
        self,
        user_id: str,
        entry_id: str,
        content: bytes,
        expected_revision: int | str,
        *,
        base_version_id: str | None,
        actor: str = "web",
        context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> WorkspaceMutationResult:
        """Save an online-editor draft without exposing version conflicts.

        The browser draft is the current side of the three-way merge, so it
        wins same-location overlaps.  Server changes since ``base_version_id``
        are folded into untouched lines/cells before the normal fenced write.
        """
        expected = WorkspacePathPolicy.normalize_revision(expected_revision)
        workspace, store = await self._prepare(user_id, for_update=True)
        previous = self._idempotent_result(user_id, idempotency_key, "write_content")
        if previous:
            return previous
        entry = self._entry(user_id, entry_id)
        if entry.kind != "file":
            raise WorkspaceError(400, "NOT_FILE", "目标不是文件")
        if entry.current_version_id and not base_version_id:
            raise WorkspaceError(
                428,
                "BASE_VERSION_REQUIRED",
                "已有版本的文件保存必须携带编辑基线",
                entry=entry,
            )
        if len(content) > int(self.settings.workspace_max_file_bytes):
            raise WorkspaceError(413, "FILE_TOO_LARGE", "文件超过工作区单文件大小限制")
        await _validate_edit_content(entry.name, content)
        entry, current_head = await self._synchronize_physical_head(
            user_id,
            workspace=workspace,
            store=store,
            entry=entry,
        )
        current_revision = int(entry.revision)
        current_version_id = str(entry.current_version_id or "") or None
        base_version = None
        base_was_pruned = False
        if base_version_id:
            base_version = self.db.query(WorkspaceFileVersion).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.version_id == base_version_id,
            ).one_or_none()
            if base_version is None or str(base_version.entry_id) != entry_id:
                raise WorkspaceError(
                    409,
                    "BASE_VERSION_INVALID",
                    "编辑基线不存在或不属于当前文件",
                    entry=entry,
                )
            if base_version.state == "pruned":
                base_was_pruned = True
            elif (
                base_version.state != "materialized"
                or not base_version.content_path
                or not base_version.sha256
            ):
                raise WorkspaceError(
                    409,
                    "BASE_VERSION_UNAVAILABLE",
                    "编辑基线当前不可用于自动合并，请稍后重试",
                    entry=entry,
                )
        if current_revision == expected:
            self.db.rollback()
            return await self.write_content(
                user_id,
                entry_id,
                content,
                expected,
                actor=actor,
                context=context,
                idempotency_key=idempotency_key,
                _content_validated=True,
            )
        if base_version_id and base_version_id == current_version_id:
            # Rename/move advances entry revision without changing file bytes.
            self.db.rollback()
            return await self.write_content(
                user_id,
                entry_id,
                content,
                current_revision,
                actor=actor,
                context=context,
                idempotency_key=idempotency_key,
                _content_validated=True,
            )

        target_name = str(entry.name)
        base_path = str(base_version.content_path) if base_version is not None and base_version.content_path else None
        base_sha = str(base_version.sha256 or "") if base_version is not None else None
        base_size = int(base_version.size_bytes or 0) if base_version is not None else None
        self.db.commit()

        merged_content = content
        if base_path:
            server_content = await self._read_verified_change_set_bytes(
                store,
                current_head.content_path,
                expected_sha256=current_head.sha256,
                expected_size=current_head.size_bytes,
                allow_system=True,
            )
            try:
                base_content = await self._read_verified_change_set_bytes(
                    store,
                    base_path,
                    expected_sha256=base_sha,
                    expected_size=base_size,
                    allow_system=True,
                )
                merge_result = await asyncio.to_thread(
                    merge_workspace_bytes,
                    target_name,
                    base=base_content,
                    current=content,
                    proposal=server_content,
                )
                if merge_result is not None:
                    merged_content = merge_result.content
            except WorkspaceError as exc:
                raise WorkspaceError(
                    409,
                    "BASE_VERSION_UNAVAILABLE",
                    "编辑基线内容不可用，无法安全自动合并",
                    entry=entry,
                ) from exc
        elif not base_was_pruned:
            raise WorkspaceError(
                409,
                "BASE_VERSION_UNAVAILABLE",
                "编辑基线内容不可用，无法安全自动合并",
                entry=entry,
            )

        saved = await self.write_content(
            user_id,
            entry_id,
            merged_content,
            current_revision,
            actor=actor,
            context=context,
            idempotency_key=idempotency_key,
            _auto_merged=True,
            _content_validated=merged_content is content,
        )
        return WorkspaceMutationResult(
            saved.status,
            saved.entry,
            saved.mutation_id,
            auto_merged=True,
        )

    async def list_versions(
        self,
        user_id: str,
        entry_id: str,
    ) -> list[WorkspaceFileVersion]:
        await self._prepare(
            user_id,
            for_update=False,
            require_filesystem=False,
        )
        entry = self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.entry_id == entry_id,
        ).first()
        if entry is None:
            raise WorkspaceError(404, "ENTRY_NOT_FOUND", "工作区条目不存在")
        return (
            self.db.query(WorkspaceFileVersion)
            .filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.entry_id == entry_id,
                WorkspaceFileVersion.state == "materialized",
                or_(
                    WorkspaceFileVersion.checkpoint_kind.isnot(None),
                    WorkspaceFileVersion.pinned.is_(True),
                    WorkspaceFileVersion.version_id == entry.current_version_id,
                ),
            )
            .order_by(WorkspaceFileVersion.sequence.desc())
            .all()
        )

    async def checkpoint_entry(
        self,
        user_id: str,
        entry_id: str,
        *,
        expected_revision: int | str,
        version_id: str,
        checkpoint_kind: Literal["web_idle", "web_close", "web_periodic"],
    ) -> WorkspaceFileVersion:
        await self._prepare(
            user_id,
            for_update=False,
            require_filesystem=False,
        )
        expected = WorkspacePathPolicy.normalize_revision(expected_revision)
        entry = self._entry(user_id, entry_id)
        if entry.kind != "file":
            raise WorkspaceError(400, "NOT_FILE", "目标不是文件")
        if int(entry.revision) != expected or str(entry.current_version_id or "") != version_id:
            raise WorkspaceError(409, "REVISION_CONFLICT", "文件已产生更新版本", entry=entry)
        version = self.db.query(WorkspaceFileVersion).filter(
            WorkspaceFileVersion.user_id == user_id,
            WorkspaceFileVersion.entry_id == entry_id,
            WorkspaceFileVersion.version_id == version_id,
            WorkspaceFileVersion.state == "materialized",
        ).with_for_update().one_or_none()
        if version is None or not version.blob_id or not version.content_path:
            raise WorkspaceError(404, "VERSION_NOT_FOUND", "文件版本不存在")
        if version.checkpoint_kind is None:
            version.checkpoint_kind = checkpoint_kind
            version.retained_until = None
            self.db.commit()
            self.db.refresh(version)
        else:
            self.db.rollback()
        return version

    def protect_version_reference(
        self,
        user_id: str,
        version_id: str,
        *,
        reference_kind: Literal["round_attachment", "checkpoint_pin"],
        reference_key: str,
        commit: bool = True,
        entry_id: str | None = None,
    ) -> WorkspaceContentReference | None:
        query = self.db.query(WorkspaceFileVersion).filter(
            WorkspaceFileVersion.user_id == user_id,
            WorkspaceFileVersion.version_id == version_id,
            WorkspaceFileVersion.state == "materialized",
        )
        if entry_id is not None:
            query = query.filter(WorkspaceFileVersion.entry_id == entry_id)
        version = query.with_for_update().one_or_none()
        if version is None:
            raise WorkspaceError(404, "VERSION_NOT_FOUND", "文件版本不存在")
        if not version.blob_id:
            version.pinned = True
            if commit:
                self.db.commit()
            return None
        reference = self._upsert_content_reference(
            user_id=user_id,
            blob_id=str(version.blob_id),
            version_id=version_id,
            reference_kind=reference_kind,
            reference_key=reference_key,
        )
        version.pinned = True
        if commit:
            self.db.commit()
            self.db.refresh(reference)
        return reference

    def release_content_references(
        self,
        user_id: str,
        *,
        reference_kind: str,
        reference_key_prefix: str,
        commit: bool = True,
    ) -> int:
        references = self.db.query(WorkspaceContentReference).filter(
            WorkspaceContentReference.user_id == user_id,
            WorkspaceContentReference.reference_kind == reference_kind,
            WorkspaceContentReference.reference_key.like(reference_key_prefix + "%"),
        ).with_for_update().all()
        version_ids = {str(item.version_id) for item in references if item.version_id}
        for reference in references:
            self.db.delete(reference)
        if references:
            self.db.flush()
        if version_ids:
            still_referenced = {
                str(row[0])
                for row in self.db.query(WorkspaceContentReference.version_id).filter(
                    WorkspaceContentReference.user_id == user_id,
                    WorkspaceContentReference.version_id.in_(tuple(version_ids)),
                    WorkspaceContentReference.reference_kind.in_(("round_attachment", "checkpoint_pin")),
                ).all()
                if row[0]
            }
            releasable = tuple(sorted(version_ids - still_referenced))
            if releasable:
                self.db.query(WorkspaceFileVersion).filter(
                    WorkspaceFileVersion.user_id == user_id,
                    WorkspaceFileVersion.version_id.in_(releasable),
                ).update({WorkspaceFileVersion.pinned: False}, synchronize_session="fetch")
        if commit:
            self.db.commit()
        return len(references)

    def backfill_legacy_round_file_references(self, user_id: str) -> int:
        """Pin materialized Workspace versions named by pre-contract Round events.

        This migration runs before history GC.  It trusts only durable
        workspace_resource_changed events carrying both stable entry_id and an
        immutable version ID; assistant prose and same-name paths are ignored.
        """

        rows = (
            self.db.query(AGUIEventLog.run_id, AGUIEventLog.payload, Session.id)
            .join(Round, Round.id == AGUIEventLog.run_id)
            .join(
                Session,
                or_(Round.session_id == Session.id, Round.thread_id == Session.id),
            )
            .filter(
                Session.user_id == user_id,
                AGUIEventLog.event_type == "CUSTOM",
                AGUIEventLog.payload.contains("workspace_resource_changed"),
            )
            .order_by(AGUIEventLog.created_at.asc(), AGUIEventLog.id.asc())
            .all()
        )
        candidates: dict[tuple[str, str, str], tuple[str, str, str, str]] = {}
        for run_id, raw_payload, session_id in rows:
            try:
                payload = json.loads(raw_payload or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("name") != "workspace_resource_changed":
                continue
            value = payload.get("value")
            if not isinstance(value, dict):
                continue
            nested = value.get("assistant_file_reference")
            source = nested if isinstance(nested, dict) else value
            entry_id = str(source.get("entry_id") or "")
            version_id = str(
                source.get("version_id")
                or source.get("current_version_id")
                or ""
            )
            operation = str(source.get("operation") or value.get("operation") or "").upper()
            status = str(source.get("status") or value.get("status") or "active")
            kind = source.get("kind") or value.get("kind")
            identity = (str(session_id), str(run_id), entry_id)
            if operation in {"DELETED"} or status != "active":
                candidates.pop(identity, None)
                continue
            if (
                not entry_id
                or not version_id
                or kind != "file"
                or operation == "NO_CHANGE"
            ):
                continue
            reference_key = (
                f"{session_id}:{run_id}:assistant:{entry_id}:{version_id}"
            )
            candidates[identity] = (
                entry_id,
                version_id,
                reference_key,
                operation,
            )

        created = 0
        for entry_id, version_id, reference_key, _operation in candidates.values():
            existing = self.db.query(WorkspaceContentReference.reference_id).filter(
                WorkspaceContentReference.user_id == user_id,
                WorkspaceContentReference.reference_kind == "round_attachment",
                WorkspaceContentReference.reference_key == reference_key,
            ).first()
            if existing is not None:
                continue
            version = self.db.query(WorkspaceFileVersion).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.entry_id == entry_id,
                WorkspaceFileVersion.version_id == version_id,
                WorkspaceFileVersion.state == "materialized",
            ).one_or_none()
            if version is None or not version.blob_id:
                continue
            self._upsert_content_reference(
                user_id=user_id,
                blob_id=str(version.blob_id),
                version_id=version_id,
                reference_kind="round_attachment",
                reference_key=reference_key,
            )
            version.pinned = True
            created += 1
        self.db.commit()
        return created

    async def migrate_legacy_versions(self, user_id: str, *, limit: int = 100) -> int:
        """Move pre-object-store version copies into per-user SHA objects."""

        _workspace, store = await self._prepare(user_id, for_update=False)
        batch_size = max(1, min(int(limit), 1000))
        cleanup_rows = self.db.query(WorkspaceFileVersion).filter(
            WorkspaceFileVersion.user_id == user_id,
            WorkspaceFileVersion.legacy_content_path.isnot(None),
        ).limit(batch_size).all()
        cleaned = 0
        for cleanup_row in cleanup_rows:
            legacy_path = str(cleanup_row.legacy_content_path or "")
            parts = legacy_path.split("/")
            if parts[:2] != [WORKSPACE_SYSTEM_DIRECTORY, "versions"] or len(parts) != 5 or parts[-1] != "content":
                cleanup_row.legacy_content_path = None
                self.db.commit()
                continue
            try:
                await store.remove(posixpath.dirname(legacy_path))
            except WorkspaceError as exc:
                if exc.code != "NOT_FOUND":
                    self.db.rollback()
                    continue
            row = self.db.get(WorkspaceFileVersion, cleanup_row.version_id)
            if row is not None:
                row.legacy_content_path = None
                self.db.commit()
                cleaned += 1
        remaining = max(batch_size - cleaned, 0)
        if remaining == 0:
            await store.cleanup_empty_legacy_version_directories()
            return cleaned
        candidates = self.db.query(WorkspaceFileVersion).filter(
            WorkspaceFileVersion.user_id == user_id,
            WorkspaceFileVersion.state == "materialized",
            WorkspaceFileVersion.blob_id.is_(None),
            WorkspaceFileVersion.content_path.isnot(None),
        ).order_by(WorkspaceFileVersion.created_at.asc()).limit(remaining).all()
        candidate_values = [
            {
                "version_id": str(row.version_id),
                "entry_id": str(row.entry_id),
                "content_path": str(row.content_path),
                "sha256": str(row.sha256 or ""),
                "size_bytes": int(row.size_bytes or 0),
            }
            for row in candidates
        ]
        self.db.rollback()
        migrated = 0
        for candidate in candidate_values:
            old_path = candidate["content_path"]
            parts = old_path.split("/")
            if (
                parts[:2] != [WORKSPACE_SYSTEM_DIRECTORY, "versions"]
                or len(parts) != 5
                or parts[-1] != "content"
                or not re.fullmatch(r"[0-9a-f]{64}", candidate["sha256"])
            ):
                continue
            blob_id = self._content_object_id(user_id, candidate["sha256"])
            object_path = self._content_object_path(candidate["sha256"])
            reference_key = f"legacy:{candidate['version_id']}"
            try:
                self._upsert_content_reference(
                    user_id=user_id,
                    blob_id=blob_id,
                    version_id=candidate["version_id"],
                    reference_kind="legacy_migration",
                    reference_key=reference_key,
                )
                existing_object = self.db.get(WorkspaceContentObject, blob_id)
                if existing_object is not None and existing_object.state == "pruning":
                    self.db.rollback()
                    continue
                self.db.commit()
                await store.ensure_content_object(
                    source_relative_path=old_path,
                    destination_relative_path=object_path,
                    expected_sha256=candidate["sha256"],
                    expected_size_bytes=candidate["size_bytes"],
                )
                version = self.db.query(WorkspaceFileVersion).filter(
                    WorkspaceFileVersion.user_id == user_id,
                    WorkspaceFileVersion.version_id == candidate["version_id"],
                    WorkspaceFileVersion.state == "materialized",
                    WorkspaceFileVersion.blob_id.is_(None),
                ).with_for_update().one_or_none()
                if version is None:
                    self.db.rollback()
                    continue
                history_delta = self._persist_content_object_record(
                    user_id=user_id,
                    blob_id=blob_id,
                    sha256=candidate["sha256"],
                    size_bytes=candidate["size_bytes"],
                    content_path=object_path,
                )
                version.blob_id = blob_id
                version.content_path = object_path
                version.legacy_content_path = old_path
                version.checkpoint_kind = version.checkpoint_kind or "legacy"
                entry = self.db.query(WorkspaceEntry).filter(
                    WorkspaceEntry.user_id == user_id,
                    WorkspaceEntry.entry_id == candidate["entry_id"],
                    WorkspaceEntry.current_version_id == candidate["version_id"],
                ).with_for_update().one_or_none()
                if entry is not None:
                    entry.head_blob_id = blob_id
                    self._sync_entry_head_reference(entry)
                self.db.query(WorkspaceContentReference).filter(
                    WorkspaceContentReference.user_id == user_id,
                    WorkspaceContentReference.reference_kind == "legacy_migration",
                    WorkspaceContentReference.reference_key == reference_key,
                ).delete(synchronize_session=False)
                workspace = self.db.query(UserWorkspace).filter(
                    UserWorkspace.user_id == user_id,
                ).with_for_update().one()
                workspace.history_used_bytes = int(workspace.history_used_bytes or 0) + history_delta
                self.db.commit()
                try:
                    await store.remove(posixpath.dirname(old_path))
                except WorkspaceError as exc:
                    if exc.code != "NOT_FOUND":
                        migrated += 1
                        continue
                version = self.db.get(WorkspaceFileVersion, candidate["version_id"])
                if version is not None:
                    version.legacy_content_path = None
                    self.db.commit()
                migrated += 1
            except WorkspaceError:
                self.db.rollback()
                logger.warning(
                    "Legacy workspace version migration deferred user=%s version=%s",
                    user_id,
                    candidate["version_id"],
                    exc_info=True,
                )
                continue
        await store.cleanup_empty_legacy_version_directories()
        return cleaned + migrated

    def _version_is_gc_protected(
        self,
        version: WorkspaceFileVersion,
        *,
        at: datetime,
    ) -> bool:
        user_id = str(version.user_id)
        version_id = str(version.version_id)
        blob_id = str(version.blob_id or "")
        entry = self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.entry_id == version.entry_id,
        ).one_or_none()
        if entry is not None and str(entry.current_version_id or "") == version_id:
            return True
        if bool(version.pinned):
            return True
        reference_exists = self.db.query(WorkspaceContentReference.reference_id).filter(
            WorkspaceContentReference.user_id == user_id,
            or_(
                WorkspaceContentReference.version_id == version_id,
                WorkspaceContentReference.blob_id == blob_id,
            ),
        ).first()
        if reference_exists is not None:
            return True
        active_change_set = self.db.query(WorkspaceChangeSet.change_set_id).filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.status.in_(("preparing", "proposed", "conflict", "needs_review", "applying")),
            or_(
                WorkspaceChangeSet.base_version_id == version_id,
                WorkspaceChangeSet.proposed_version_id == version_id,
            ),
        ).first()
        if active_change_set is not None:
            return True
        prepared_mutation = self.db.query(WorkspaceMutation.mutation_id).filter(
            WorkspaceMutation.user_id == user_id,
            WorkspaceMutation.state == "prepared",
            or_(
                WorkspaceMutation.before_version_id == version_id,
                WorkspaceMutation.after_version_id == version_id,
            ),
        ).first()
        if prepared_mutation is not None:
            return True
        if entry is None:
            return False
        if version.checkpoint_kind is None:
            if version.retained_until is None or version.retained_until <= at:
                return False
            newer_draft_count = self.db.query(func.count(WorkspaceFileVersion.version_id)).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.entry_id == version.entry_id,
                WorkspaceFileVersion.state == "materialized",
                WorkspaceFileVersion.checkpoint_kind.is_(None),
                WorkspaceFileVersion.sequence > version.sequence,
            ).scalar()
            return int(newer_draft_count or 0) < int(
                self.settings.workspace_draft_revision_retention_count
            )
        retention_cutoff = at - timedelta(days=int(self.settings.workspace_version_retention_days))
        if version.created_at >= retention_cutoff:
            return True
        newer_checkpoint_count = self.db.query(func.count(WorkspaceFileVersion.version_id)).filter(
            WorkspaceFileVersion.user_id == user_id,
            WorkspaceFileVersion.entry_id == version.entry_id,
            WorkspaceFileVersion.state == "materialized",
            WorkspaceFileVersion.checkpoint_kind.isnot(None),
            WorkspaceFileVersion.sequence > version.sequence,
        ).scalar()
        return int(newer_checkpoint_count or 0) < int(self.settings.workspace_version_retention_count)

    async def _prune_version_content(
        self,
        user_id: str,
        version_id: str,
        store: WorkspaceStore,
        *,
        at: datetime,
    ) -> tuple[bool, bool, int]:
        version = self.db.query(WorkspaceFileVersion).filter(
            WorkspaceFileVersion.user_id == user_id,
            WorkspaceFileVersion.version_id == version_id,
            WorkspaceFileVersion.state.in_(("materialized", "pruning")),
        ).one_or_none()
        if version is None or not version.blob_id:
            self.db.rollback()
            return False, False, 0
        blob_id = str(version.blob_id)
        try:
            leases = tuple(self._acquire_claims(
                user_id=user_id,
                operation="history_gc",
                specs=(WorkspaceClaimSpec("path", f"object:{blob_id}"),),
            ))
        except WorkspaceError as exc:
            if exc.code == "WORKSPACE_MUTATION_IN_PROGRESS":
                return False, False, 0
            raise
        try:
            version = self.db.query(WorkspaceFileVersion).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.version_id == version_id,
                WorkspaceFileVersion.state.in_(("materialized", "pruning")),
            ).with_for_update().one_or_none()
            if version is None or not version.blob_id:
                self.db.rollback()
                return False, False, 0
            if self._version_is_gc_protected(version, at=at):
                if version.state == "pruning":
                    version.state = "materialized"
                self.db.commit()
                return False, False, 0
            content_object = self.db.query(WorkspaceContentObject).filter(
                WorkspaceContentObject.user_id == user_id,
                WorkspaceContentObject.blob_id == blob_id,
            ).with_for_update().one_or_none()
            if content_object is None:
                version.state = "pruned"
                version.blob_id = None
                version.content_path = None
                version.pruned_at = at
                self.db.commit()
                return True, False, 0
            other_version_exists = self.db.query(WorkspaceFileVersion.version_id).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.blob_id == blob_id,
                WorkspaceFileVersion.version_id != version_id,
                WorkspaceFileVersion.state == "materialized",
            ).first()
            object_reference_exists = self.db.query(WorkspaceContentReference.reference_id).filter(
                WorkspaceContentReference.user_id == user_id,
                WorkspaceContentReference.blob_id == blob_id,
            ).first()
            head_exists = self.db.query(WorkspaceEntry.entry_id).filter(
                WorkspaceEntry.user_id == user_id,
                WorkspaceEntry.head_blob_id == blob_id,
            ).first()
            if other_version_exists is not None or object_reference_exists is not None or head_exists is not None:
                version.state = "pruned"
                version.blob_id = None
                version.content_path = None
                version.pruned_at = at
                self.db.commit()
                return True, False, 0
            version.state = "pruning"
            content_object.state = "pruning"
            content_path = str(content_object.content_path)
            object_sha256 = str(content_object.sha256)
            object_size = int(content_object.size_bytes or 0)
            self.db.commit()

            try:
                await store.remove_content_object(content_path)
            except WorkspaceError as exc:
                if exc.code != "NOT_FOUND":
                    raise
            await store.remove_office_preview_caches(
                office_preview_cache_keys(object_sha256)
            )

            version = self.db.query(WorkspaceFileVersion).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.version_id == version_id,
                WorkspaceFileVersion.state == "pruning",
            ).with_for_update().one_or_none()
            content_object = self.db.query(WorkspaceContentObject).filter(
                WorkspaceContentObject.user_id == user_id,
                WorkspaceContentObject.blob_id == blob_id,
                WorkspaceContentObject.state == "pruning",
            ).with_for_update().one_or_none()
            if version is None or content_object is None:
                self.db.rollback()
                raise WorkspaceError(409, "HISTORY_GC_FENCED", "历史对象回收状态已变化")
            version.state = "pruned"
            version.blob_id = None
            version.content_path = None
            version.pruned_at = at
            content_object.state = "pruned"
            content_object.pruned_at = at
            workspace = self.db.query(UserWorkspace).filter(
                UserWorkspace.user_id == user_id,
            ).with_for_update().one()
            workspace.history_used_bytes = max(
                0,
                int(workspace.history_used_bytes or 0) - object_size,
            )
            workspace.last_history_gc_at = at
            self.db.commit()
            return True, True, object_size
        finally:
            self._release_unattached_claims(leases)

    async def _prune_unreferenced_object(
        self,
        user_id: str,
        blob_id: str,
        store: WorkspaceStore,
        *,
        at: datetime,
    ) -> tuple[bool, int]:
        count, reclaimed = await self._prune_unreferenced_objects(user_id, [blob_id], store, at=at)
        return bool(count), reclaimed

    async def _prune_unreferenced_objects(
        self, user_id: str, blob_ids: Iterable[str], store: WorkspaceStore, *, at: datetime,
    ) -> tuple[int, int]:
        candidates = tuple(sorted(set(blob_ids)))
        if not candidates:
            return 0, 0
        try:
            leases = tuple(self._acquire_claims(
                user_id=user_id,
                operation="history_gc",
                specs=tuple(WorkspaceClaimSpec("path", f"object:{blob_id}") for blob_id in candidates),
            ))
        except WorkspaceError as exc:
            if exc.code == "WORKSPACE_MUTATION_IN_PROGRESS":
                return 0, 0
            raise
        try:
            objects = self.db.query(WorkspaceContentObject).filter(
                WorkspaceContentObject.user_id == user_id,
                WorkspaceContentObject.blob_id.in_(candidates),
                WorkspaceContentObject.state.in_(("materialized", "pruning")),
            ).order_by(WorkspaceContentObject.blob_id).with_for_update().all()
            if not objects:
                self.db.rollback()
                return 0, 0
            protected_query = (
                self.db.query(WorkspaceContentReference.blob_id).filter(
                    WorkspaceContentReference.user_id == user_id,
                    WorkspaceContentReference.blob_id.in_(candidates),
                ).union(
                    self.db.query(WorkspaceEntry.head_blob_id).filter(
                        WorkspaceEntry.user_id == user_id,
                        WorkspaceEntry.head_blob_id.in_(candidates),
                    ),
                    self.db.query(WorkspaceFileVersion.blob_id).filter(
                        WorkspaceFileVersion.user_id == user_id,
                        WorkspaceFileVersion.blob_id.in_(candidates),
                        WorkspaceFileVersion.state.in_(("materialized", "pruning")),
                    ),
                    self.db.query(WorkspaceChangeSet.proposal_blob_id).filter(
                        WorkspaceChangeSet.user_id == user_id,
                        WorkspaceChangeSet.proposal_blob_id.in_(candidates),
                        WorkspaceChangeSet.status.in_(("preparing", "proposed", "conflict", "needs_review", "applying")),
                    ),
                )
            )
            protected = {row[0] for row in protected_query.all()}
            targets = {}
            target_cache_keys: set[str] = set()
            for content_object in objects:
                if content_object.blob_id in protected:
                    content_object.state = "materialized"
                else:
                    targets[content_object.blob_id] = str(content_object.content_path)
                    target_cache_keys.update(
                        office_preview_cache_keys(str(content_object.sha256))
                    )
                    content_object.state = "pruning"
            self.db.commit()
            if not targets:
                return 0, 0
            await store.remove_content_objects(targets.values())
            await store.remove_office_preview_caches(target_cache_keys)
            pruned_objects = self.db.query(WorkspaceContentObject).filter(
                WorkspaceContentObject.user_id == user_id,
                WorkspaceContentObject.blob_id.in_(tuple(targets)),
                WorkspaceContentObject.state == "pruning",
            ).order_by(WorkspaceContentObject.blob_id).with_for_update().all()
            if len(pruned_objects) != len(targets):
                self.db.rollback()
                raise WorkspaceError(409, "HISTORY_GC_FENCED", "历史对象回收状态已变化")
            object_size = sum(int(item.size_bytes or 0) for item in pruned_objects)
            for content_object in pruned_objects:
                content_object.state = "pruned"
                content_object.pruned_at = at
            workspace = self.db.query(UserWorkspace).filter(
                UserWorkspace.user_id == user_id,
            ).with_for_update().one()
            workspace.history_used_bytes = max(
                0,
                int(workspace.history_used_bytes or 0) - object_size,
            )
            workspace.last_history_gc_at = at
            self.db.commit()
            return len(pruned_objects), object_size
        finally:
            self._release_unattached_claims(leases)

    async def run_history_gc(
        self,
        user_id: str,
        *,
        at: datetime | None = None,
        limit: int | None = None,
    ) -> WorkspaceHistoryGcResult:
        workspace, store = await self._prepare(user_id, for_update=False)
        current_time = at or now_naive()
        batch_size = min(
            max(int(limit or self.settings.workspace_history_gc_batch_size), 1),
            1000,
        )
        self.db.query(WorkspaceContentReference).filter(
            WorkspaceContentReference.user_id == user_id,
            WorkspaceContentReference.retained_until.isnot(None),
            WorkspaceContentReference.retained_until <= current_time,
        ).delete(synchronize_session=False)
        self.db.commit()
        versions = (
            self.db.query(WorkspaceFileVersion)
            .filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.state.in_(("materialized", "pruning")),
                WorkspaceFileVersion.blob_id.isnot(None),
            )
            .order_by(WorkspaceFileVersion.created_at.asc(), WorkspaceFileVersion.version_id.asc())
            .all()
        )
        candidate_ids: list[str] = []
        protected = 0
        for version in versions:
            if self._version_is_gc_protected(version, at=current_time):
                protected += 1
                continue
            candidate_ids.append(str(version.version_id))
            if len(candidate_ids) >= batch_size:
                break
        self.db.rollback()
        versions_pruned = 0
        objects_pruned = 0
        bytes_reclaimed = 0
        for candidate_id in candidate_ids:
            version_pruned, object_pruned, reclaimed = await self._prune_version_content(
                user_id,
                candidate_id,
                store,
                at=current_time,
            )
            versions_pruned += int(version_pruned)
            objects_pruned += int(object_pruned)
            bytes_reclaimed += int(reclaimed)
        remaining = max(batch_size - versions_pruned, 0)
        if remaining:
            object_ids = [
                str(row[0])
                for row in (
                    self.db.query(WorkspaceContentObject.blob_id)
                    .filter(
                        WorkspaceContentObject.user_id == user_id,
                        WorkspaceContentObject.state.in_(("materialized", "pruning")),
                        ~WorkspaceContentObject.blob_id.in_(
                            self.db.query(WorkspaceContentReference.blob_id).filter(
                                WorkspaceContentReference.user_id == user_id,
                            )
                        ),
                        ~WorkspaceContentObject.blob_id.in_(
                            self.db.query(WorkspaceEntry.head_blob_id).filter(
                                WorkspaceEntry.user_id == user_id,
                                WorkspaceEntry.head_blob_id.isnot(None),
                            )
                        ),
                        ~WorkspaceContentObject.blob_id.in_(
                            self.db.query(WorkspaceFileVersion.blob_id).filter(
                                WorkspaceFileVersion.user_id == user_id,
                                WorkspaceFileVersion.state.in_(("materialized", "pruning")),
                                WorkspaceFileVersion.blob_id.isnot(None),
                            )
                        ),
                        ~WorkspaceContentObject.blob_id.in_(
                            self.db.query(WorkspaceChangeSet.proposal_blob_id).filter(
                                WorkspaceChangeSet.user_id == user_id,
                                WorkspaceChangeSet.status.in_(("preparing", "proposed", "conflict", "needs_review", "applying")),
                                WorkspaceChangeSet.proposal_blob_id.isnot(None),
                            )
                        ),
                    )
                    .order_by(WorkspaceContentObject.last_accessed_at.asc())
                    .limit(remaining)
                    .all()
                )
            ]
            self.db.rollback()
            for object_id in object_ids:
                object_pruned, reclaimed = await self._prune_unreferenced_object(
                    user_id,
                    object_id,
                    store,
                    at=current_time,
                )
                objects_pruned += int(object_pruned)
                bytes_reclaimed += int(reclaimed)
        workspace_row = self.db.query(UserWorkspace).filter(
            UserWorkspace.user_id == user_id,
        ).with_for_update().one()
        authoritative_history_bytes = self.db.query(
            func.coalesce(func.sum(WorkspaceContentObject.size_bytes), 0)
        ).filter(
            WorkspaceContentObject.user_id == user_id,
            WorkspaceContentObject.state.in_(("materialized", "pruning")),
        ).scalar()
        workspace_row.history_used_bytes = int(authoritative_history_bytes or 0)
        workspace_row.last_history_gc_at = current_time
        if int(workspace_row.history_used_bytes) > int(workspace_row.history_quota_bytes or 0):
            logger.warning(
                "Workspace protected history exceeds soft quota user=%s used=%s quota=%s",
                user_id,
                workspace_row.history_used_bytes,
                workspace_row.history_quota_bytes,
            )
        self.db.commit()
        return WorkspaceHistoryGcResult(
            versions_pruned=versions_pruned,
            objects_pruned=objects_pruned,
            bytes_reclaimed=bytes_reclaimed,
            protected_versions=protected,
        )

    async def open_version_content(
        self,
        user_id: str,
        version_id: str,
    ) -> WorkspaceVersionContent:
        _workspace, store = await self._prepare(user_id, for_update=False)
        version = (
            self.db.query(WorkspaceFileVersion)
            .join(
                WorkspaceContentObject,
                WorkspaceContentObject.blob_id == WorkspaceFileVersion.blob_id,
            )
            .filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.version_id == version_id,
                WorkspaceFileVersion.state == "materialized",
                WorkspaceContentObject.user_id == user_id,
                WorkspaceContentObject.state == "materialized",
                WorkspaceContentObject.content_path == WorkspaceFileVersion.content_path,
                WorkspaceContentObject.sha256 == WorkspaceFileVersion.sha256,
                WorkspaceContentObject.size_bytes == WorkspaceFileVersion.size_bytes,
            )
            .one_or_none()
        )
        if version is None or not version.content_path:
            raise WorkspaceError(404, "VERSION_NOT_FOUND", "文件版本不存在")
        entry_name = self.db.query(WorkspaceEntry.name).filter(
            WorkspaceEntry.user_id == user_id,
            WorkspaceEntry.entry_id == version.entry_id,
        ).scalar()
        fallback_extension = mimetypes.guess_extension(version.mime_type or "") or ""
        display_name = str(
            entry_name
            or f"workspace-version-{int(version.sequence)}{fallback_extension}"
        )
        sandbox_path = store.absolute_path(str(version.content_path))
        self.db.expunge(version)
        self.db.commit()
        return WorkspaceVersionContent(
            version=version,
            sandbox=store.sandbox,
            sandbox_path=sandbox_path,
            workspace_root=store.workspace_root,
            name=display_name,
        )

    async def restore_version(
        self,
        user_id: str,
        entry_id: str,
        version_id: str,
        *,
        expected_revision: int | str,
        idempotency_key: str,
        actor: str = "web",
        context: dict[str, Any] | None = None,
    ) -> WorkspaceMutationResult:
        expected = WorkspacePathPolicy.normalize_revision(expected_revision)
        workspace, store = await self._prepare(user_id, for_update=True)
        previous = self._idempotent_result(user_id, idempotency_key, "restore_version")
        if previous:
            return previous
        entry = self._entry(user_id, entry_id)
        if entry.kind != "file":
            raise WorkspaceError(400, "NOT_FILE", "目标不是文件")
        if int(entry.revision) != expected:
            raise WorkspaceError(409, "REVISION_CONFLICT", "文件已被其他操作修改", entry=entry)
        version = self.db.query(WorkspaceFileVersion).filter(
            WorkspaceFileVersion.user_id == user_id,
            WorkspaceFileVersion.entry_id == entry_id,
            WorkspaceFileVersion.version_id == version_id,
            WorkspaceFileVersion.state == "materialized",
        ).one_or_none()
        if version is None or not version.content_path or not version.sha256:
            raise WorkspaceError(404, "VERSION_NOT_FOUND", "文件版本不存在")
        if entry.current_version_id == version_id:
            return self._record_mutation(
                workspace=workspace,
                entry=entry,
                actor=actor,
                operation="restore_version",
                result_status="NO_CHANGE",
                idempotency_key=idempotency_key,
                context=context,
                before_revision=int(entry.revision),
                before_sha256=entry.sha256,
                details={"restored_from_version_id": version_id},
            )

        before_revision = int(entry.revision)
        before_sha = entry.sha256
        target_path = str(entry.relative_path)
        version_path = str(version.content_path)
        version_sha = str(version.sha256)
        version_size = int(version.size_bytes or 0)
        version_mime = version.mime_type or entry.mime_type
        base_version_plan, next_version_plan = self._plan_file_version_update(
            entry,
            new_sha256=version_sha,
            new_size_bytes=version_size,
            new_mime_type=version_mime,
            actor=actor,
            context=context,
            checkpoint_kind="restore",
        )
        next_version_plan.version_row["restored_from_version_id"] = version_id
        before_projection = self._journal_projection(entry)
        after_projection = dict(before_projection)
        after_projection.update({
            "size_bytes": version_size,
            "sha256": version_sha,
            "mime_type": version_mime,
            "revision": before_revision + 1,
            "current_version_id": next_version_plan.version_row["version_id"],
            "head_blob_id": next_version_plan.version_row["blob_id"],
        })
        version_rows = [
            *([base_version_plan.version_row] if base_version_plan else []),
            next_version_plan.version_row,
        ]
        ancestor_ids = self._ancestor_entry_ids(user_id, entry.parent_id)
        claim = WorkspaceClaimSpec(
            "file",
            file_scope(entry_id),
            entry_id,
            conflict_scope_keys=self._tree_scope_keys(ancestor_ids),
        )
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace,
            workspace_user_id=user_id,
            entry_id=entry_id,
            actor=actor,
            operation="restore_version",
            result_status="UPDATED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=before_revision,
            before_sha256=before_sha,
            after_revision=before_revision + 1,
            after_sha256=version_sha,
            before_version_id=before_projection.get("current_version_id"),
            after_version_id=str(next_version_plan.version_row["version_id"]),
            journal={
                "target_path": target_path,
                "old_sha256": before_sha,
                "new_sha256": version_sha,
                "bytes_delta": version_size - int(before_projection.get("size_bytes") or 0),
                "entries_delta": 0,
                "tree_revision_entry_ids": ancestor_ids,
                "before_entry_projection": before_projection,
                "entry_projection": after_projection,
                "version_rows": version_rows,
            },
            claim_specs=(claim,),
        )
        installed_to_workspace = False
        try:
            async with self._guard_workspace_claims(store, prepared.leases):
                if base_version_plan is not None:
                    await self._snapshot_version(store, base_version_plan)
                restored = await store.restore_version_atomic(
                    version_relative_path=version_path,
                    destination_relative_path=target_path,
                    expected_version_sha256=version_sha,
                    expected_version_size_bytes=version_size,
                    expected_destination_sha256=before_sha,
                    temp_token=prepared.mutation_id,
                )
                installed_to_workspace = True
                if restored.sha256 != version_sha or int(restored.size_bytes) != version_size:
                    raise WorkspaceError(409, "VERSION_SNAPSHOT_CHANGED", "历史版本内容校验失败")
                await self._snapshot_version(store, next_version_plan)
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(
                409,
                "MUTATION_FENCED",
                "版本恢复所有权已失效，请刷新后重试",
            ) from exc
        except WorkspaceError as exc:
            if not installed_to_workspace and exc.code in {
                "DESTINATION_CHANGED",
                "NOT_FOUND",
                "SYMLINK_REJECTED",
                "NOT_FILE",
            }:
                self._fail_prepared_mutation(
                    prepared,
                    code=exc.code,
                    message=exc.message,
                    recoverable=True,
                    status_code=exc.status_code,
                    error_extra=exc.extra,
                )
            raise
        return self._record_mutation(
            workspace=workspace,
            entry=entry,
            actor=actor,
            operation="restore_version",
            result_status="UPDATED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=before_revision,
            before_sha256=before_sha,
            details={"restored_from_version_id": version_id},
            prepared_mutation=prepared,
        )

    async def open_content(
        self,
        user_id: str,
        entry_id: str,
    ) -> WorkspaceContent:
        _workspace, store = await self._prepare(user_id, for_update=False)
        entry = self._entry(user_id, entry_id)
        if entry.kind != "file":
            raise WorkspaceError(400, "NOT_FILE", "目标不是文件")
        head = self._resolve_entry_head_content(user_id, entry)
        # Metadata and bytes refer to the same immutable object. A later writer
        # may advance the entry, but cannot replace this request's content path.
        sandbox_path = store.absolute_path(head.content_path)
        self.db.expunge(entry)
        self.db.commit()
        return WorkspaceContent(
            entry,
            store.sandbox,
            sandbox_path,
            store.workspace_root,
        )

    async def move_entry(
        self,
        user_id: str,
        entry_id: str,
        *,
        parent_id: str | None,
        name: str | None = None,
        expected_revision: int | str,
        actor: str = "web",
        context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> WorkspaceMutationResult:
        expected = WorkspacePathPolicy.normalize_revision(expected_revision)
        workspace, store = await self._prepare(user_id, for_update=True)
        previous = self._idempotent_result(user_id, idempotency_key, "move_entry")
        if previous:
            return previous
        entry = self._entry(user_id, entry_id)
        if int(entry.revision) != expected:
            raise WorkspaceError(409, "REVISION_CONFLICT", "条目已被其他操作修改", entry=entry)
        self._assert_agent_relocation_preserves_stable_identity(
            user_id=user_id,
            entry_id=entry_id,
            actor=actor,
            context=context,
        )
        parent = self._parent(user_id, parent_id)
        safe_name = WorkspacePathPolicy.validate_name(name if name is not None else entry.name)
        if parent and entry.kind == "directory" and (
            parent.entry_id == entry.entry_id
            or parent.relative_path.startswith(entry.relative_path + "/")
        ):
            raise WorkspaceError(409, "DIRECTORY_CYCLE", "文件夹不能移动到自身或其子目录")
        conflict = self._sibling(
            user_id,
            parent_id,
            safe_name,
            exclude_entry_id=entry.entry_id,
            for_update=False,
        )
        if conflict:
            raise WorkspaceError(409, "NAME_CONFLICT", "目标名称已存在", entry=conflict)
        next_path = WorkspacePathPolicy.join(parent.relative_path if parent else None, safe_name)
        if next_path == entry.relative_path:
            return self._record_mutation(
                workspace=workspace,
                entry=entry,
                actor=actor,
                operation="move_entry",
                result_status="NO_CHANGE",
                idempotency_key=idempotency_key,
                context=context,
                before_revision=int(entry.revision),
                before_sha256=entry.sha256,
                details={"from": entry.relative_path, "to": entry.relative_path},
            )
        old_path = entry.relative_path
        before_revision = int(entry.revision)
        descendants = self._descendants_by_path_prefix(
            user_id,
            old_path,
            status="active",
            for_update=False,
        )
        if entry.kind == "directory":
            entry_depth = _workspace_path_depth(entry.relative_path)
            subtree_levels = max(
                [1] + [
                    _workspace_path_depth(descendant.relative_path) - entry_depth + 1
                    for descendant in descendants
                    if descendant.kind == "directory"
                ]
            )
            parent_depth = _workspace_path_depth(parent.relative_path) if parent else 0
            if parent_depth + subtree_levels > MAX_WORKSPACE_DIRECTORY_DEPTH:
                raise WorkspaceError(422, "DIRECTORY_DEPTH_LIMIT", "文件夹最多支持两层")
        before_sha = entry.sha256
        before_entry_projection = self._journal_projection(entry)
        entry_projection = dict(before_entry_projection)
        entry_projection.update(
            {
                "parent_id": parent_id,
                "parent_key": parent_id or _ROOT_PARENT_KEY,
                "name": safe_name,
                "relative_path": next_path,
                "revision": before_revision + 1,
            }
        )
        descendant_projections: list[dict[str, Any]] = []
        before_descendant_projections: list[dict[str, Any]] = []
        for descendant in descendants:
            before_descendant = self._journal_projection(descendant)
            before_descendant_projections.append(before_descendant)
            projection = dict(before_descendant)
            projection.update(
                {
                    "relative_path": next_path + descendant.relative_path[len(old_path):],
                    "revision": int(descendant.revision or 0) + 1,
                }
            )
            descendant_projections.append(projection)
        source_ancestor_ids = self._ancestor_entry_ids(user_id, before_entry_projection.get("parent_id"))
        destination_ancestor_ids = self._ancestor_entry_ids(user_id, parent_id)
        subtree_entry_ids = tuple(
            [entry_id, *(str(item.entry_id) for item in descendants)]
        )
        if entry.kind == "directory":
            source_claim = WorkspaceClaimSpec(
                "tree",
                tree_scope(entry_id),
                entry_id,
                conflict_scope_keys=self._tree_scope_keys(source_ancestor_ids),
                conflict_entry_ids=subtree_entry_ids,
            )
        else:
            source_claim = WorkspaceClaimSpec(
                "file",
                file_scope(entry_id),
                entry_id,
                conflict_scope_keys=self._tree_scope_keys(source_ancestor_ids),
            )
        destination_claim = WorkspaceClaimSpec(
            "path",
            path_scope(parent_id, safe_name),
            parent_id,
            conflict_scope_keys=self._tree_scope_keys(destination_ancestor_ids),
        )
        parent_projection = self._journal_projection(parent) if parent else None
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace,
            workspace_user_id=user_id,
            entry_id=entry_id,
            actor=actor,
            operation="move_entry",
            result_status="MOVED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=before_revision,
            before_sha256=before_sha,
            after_revision=before_revision + 1,
            after_sha256=before_sha,
            journal={
                "action": "move",
                "source_path": old_path,
                "target_path": next_path,
                "bytes_delta": 0,
                "entries_delta": 0,
                "tree_revision_entry_ids": sorted(set(
                    [*source_ancestor_ids, *destination_ancestor_ids]
                    + ([entry_id] if before_entry_projection.get("kind") == "directory" else [])
                )),
                "before_entry_projections": [
                    before_entry_projection,
                    *before_descendant_projections,
                ],
                "entry_projections": [entry_projection, *descendant_projections],
                "base_entry_projections": [parent_projection] if parent_projection else [],
                "destination_expectation": {
                    "parent_key": parent_id or _ROOT_PARENT_KEY,
                    "name": safe_name,
                    "exclude_entry_id": entry_id,
                },
            },
            claim_specs=(source_claim, destination_claim),
        )
        try:
            async with self._guard_workspace_claims(store, prepared.leases):
                await store.move(old_path, next_path)
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(
                409,
                "MUTATION_FENCED",
                "移动操作所有权已失效，请刷新后重试",
            ) from exc
        except WorkspaceError as exc:
            if exc.code in {"NAME_CONFLICT", "NOT_FOUND", "SYMLINK_REJECTED", "NOT_FILE"}:
                self._fail_prepared_mutation(
                    prepared,
                    code=exc.code,
                    message=exc.message,
                    recoverable=True,
                    status_code=exc.status_code,
                    error_extra=exc.extra,
                )
            raise
        return self._record_mutation(
            workspace=workspace,
            entry=entry,
            actor=actor,
            operation="move_entry",
            result_status="MOVED",
            idempotency_key=idempotency_key,
            context=context,
            before_revision=before_revision,
            before_sha256=before_sha,
            details={"from": old_path, "to": next_path},
            prepared_mutation=prepared,
        )

    def _assert_agent_relocation_preserves_stable_identity(
        self,
        *,
        user_id: str,
        entry_id: str,
        actor: str,
        context: dict[str, Any] | None,
    ) -> None:
        """Do not relocate/delete a target to evade the publish result."""

        round_id = str((context or {}).get("round_id") or "")
        if actor not in {"chat", "cron"} or not round_id:
            return
        related_change = self.db.query(WorkspaceChangeSet.change_set_id).filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.round_id == round_id,
            WorkspaceChangeSet.entry_id == entry_id,
        ).first()
        if related_change is not None:
            raise WorkspaceError(
                409,
                "STABLE_ENTRY_ID_REQUIRED",
                "本轮已针对该文件发起覆盖发布，禁止通过移动或删除再同名重建；请保留原 entry_id 并处理覆盖结果",
            )

    def _assert_agent_does_not_recreate_deleted_path(
        self,
        *,
        user_id: str,
        destination_path: str,
        actor: str,
        context: dict[str, Any] | None,
    ) -> None:
        """Reject deletion/relocation followed by same-name recreation in one Round."""

        round_id = str((context or {}).get("round_id") or "")
        if actor not in {"chat", "cron"} or not round_id:
            return
        relocation_rows = self.db.query(
            WorkspaceMutation.operation,
            WorkspaceMutation.details_json,
        ).filter(
            WorkspaceMutation.user_id == user_id,
            WorkspaceMutation.round_id == round_id,
            WorkspaceMutation.operation.in_(("delete_entries", "move_entry")),
            WorkspaceMutation.state == "completed",
        ).all()
        for operation, raw_details in relocation_rows:
            try:
                details = json.loads(raw_details or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if operation == "delete_entries":
                deleted_roots = (details.get("journal") or {}).get("root_projections") or []
                if any(item.get("relative_path") == destination_path for item in deleted_roots):
                    raise WorkspaceError(409, "STABLE_ENTRY_REPLACEMENT_FORBIDDEN", "本轮已删除该路径的稳定实体，禁止同名重建")
                continue
            result_details = (
                details.get("result")
                if isinstance(details, dict) and isinstance(details.get("result"), dict)
                else details
            )
            vacated_path = None
            if isinstance(result_details, dict):
                vacated_path = (
                    result_details.get("from")
                )
            if vacated_path == destination_path:
                raise WorkspaceError(
                    409,
                    "STABLE_ENTRY_REPLACEMENT_FORBIDDEN",
                    "本轮已移动或删除该路径的稳定实体，禁止同名重建；如需继续修改必须保留原实体",
                )


    def _delete_result(self, mutation: WorkspaceMutation) -> WorkspaceDeleteResult:
        journal = json.loads(mutation.details_json)["journal"]
        return WorkspaceDeleteResult(
            mutation_id=str(mutation.mutation_id),
            roots=tuple(dict(item) for item in journal["root_projections"]),
            affected_entry_ids=tuple(journal["delete_entry_ids"]),
        )

    def _complete_prepared_delete(self, prepared: WorkspacePreparedMutation) -> WorkspaceDeleteResult:
        row = self._owned_prepared_mutation_query(prepared).with_for_update().one_or_none()
        if row is None:
            raise WorkspaceError(409, "MUTATION_FENCED", "删除操作所有权已失效")
        journal = json.loads(row.details_json)["journal"]
        self._lock_and_assert_mutation_claims(row, journal, prepared.leases)
        entry_ids = tuple(journal["delete_entry_ids"])
        for before in journal["before_entry_projections"]:
            current = self.db.query(WorkspaceEntry.entry_id).filter(
                WorkspaceEntry.user_id == row.user_id,
                WorkspaceEntry.entry_id == before["entry_id"],
                WorkspaceEntry.revision == before["revision"],
                WorkspaceEntry.relative_path == before["relative_path"],
                WorkspaceEntry.status == before["status"],
            ).with_for_update().one_or_none()
            if current is None:
                raise WorkspaceError(409, "MUTATION_FENCED", "删除范围已被其他操作修改")
        versions = self.db.query(WorkspaceFileVersion.version_id).filter(
            WorkspaceFileVersion.user_id == row.user_id,
            WorkspaceFileVersion.entry_id.in_(entry_ids),
        ).all()
        version_ids = tuple(item[0] for item in versions)
        changes = self.db.query(WorkspaceChangeSet.change_set_id).filter(
            WorkspaceChangeSet.user_id == row.user_id,
            WorkspaceChangeSet.entry_id.in_(entry_ids),
        ).all()
        change_keys = tuple(
            key for (change_id,) in changes
            for key in (f"{change_id}:base", f"{change_id}:proposal")
        )
        self.db.query(WorkspaceContentReference).filter(
            WorkspaceContentReference.user_id == row.user_id,
            or_(
                WorkspaceContentReference.version_id.in_(version_ids),
                (
                    WorkspaceContentReference.reference_kind == "entry_head"
                ) & WorkspaceContentReference.reference_key.in_(entry_ids),
                WorkspaceContentReference.reference_key.in_(change_keys),
            ),
        ).delete(synchronize_session=False)
        self.db.query(WorkspaceChangeSet).filter(
            WorkspaceChangeSet.user_id == row.user_id,
            WorkspaceChangeSet.entry_id.in_(entry_ids),
        ).delete(synchronize_session=False)
        self.db.query(WorkspaceFileVersion).filter(
            WorkspaceFileVersion.user_id == row.user_id,
            WorkspaceFileVersion.entry_id.in_(entry_ids),
        ).delete(synchronize_session=False)
        self.db.query(WorkspaceEntry).filter(
            WorkspaceEntry.user_id == row.user_id,
            WorkspaceEntry.entry_id.in_(entry_ids),
        ).delete(synchronize_session=False)
        self._increment_tree_revisions(row.user_id, journal)
        workspace = self.db.query(UserWorkspace).filter(
            UserWorkspace.user_id == row.user_id,
        ).with_for_update().one()
        workspace.used_bytes = int(workspace.used_bytes or 0) + int(journal["bytes_delta"])
        workspace.entry_count = int(workspace.entry_count or 0) + int(journal["entries_delta"])
        workspace.revision = int(workspace.revision or 0) + 1
        workspace.updated_at = now_naive()
        row.state = "completed"
        row.result_status = "DELETED"
        row.lease_expires_at = None
        row.error_code = None
        row.error_message = None
        row.recoverable = False
        row.completed_at = now_naive()
        self._release_mutation_claims(row.mutation_id, row.owner_token)
        result = self._delete_result(row)
        self.db.commit()
        return result

    async def delete_entry(
        self, user_id: str, entry_id: str, *, expected_revision: int | str,
        actor: str = "web", context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> WorkspaceDeleteResult:
        return await self.delete_entries_batch(
            user_id, ((entry_id, expected_revision),),
            actor=actor, context=context,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )

    async def delete_entries_batch(
        self, user_id: str, items: Iterable[tuple[str, int | str]], *,
        idempotency_key: str, actor: str = "web",
        context: dict[str, Any] | None = None,
    ) -> WorkspaceDeleteResult:
        requested: dict[str, int] = {}
        for entry_id, revision in items:
            expected = WorkspacePathPolicy.normalize_revision(revision)
            if expected is None:
                raise WorkspaceError(422, "INVALID_REVISION", "删除必须携带文件版本")
            if entry_id in requested and requested[entry_id] != expected:
                raise WorkspaceError(422, "DUPLICATE_ENTRY", "同一条目提交了不同版本")
            requested[str(entry_id)] = expected
        if not requested or len(requested) > 200:
            raise WorkspaceError(422, "INVALID_BATCH_SIZE", "每次删除必须包含 1 到 200 个条目")
        fingerprint = hashlib.sha256(json.dumps(
            sorted(requested.items()), separators=(",", ":"),
        ).encode()).hexdigest()
        workspace, store = await self._prepare(user_id, for_update=True)
        existing = self.db.query(WorkspaceMutation).filter(
            WorkspaceMutation.user_id == user_id,
            WorkspaceMutation.idempotency_key == idempotency_key,
        ).one_or_none()
        if existing is not None:
            journal = json.loads(existing.details_json or "{}").get("journal", {})
            if existing.operation != "delete_entries" or journal.get("request_fingerprint") != fingerprint:
                raise WorkspaceError(409, "IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同的删除请求")
            if existing.state == "failed":
                self._raise_failed_mutation(existing)
            if existing.state != "completed":
                raise WorkspaceError(
                    409,
                    "MUTATION_IN_PROGRESS",
                    "删除操作仍在处理中",
                    extra={
                        "mutation_id": existing.mutation_id,
                        "mutation_state": existing.state,
                        "outcome": "pending",
                    },
                )
            return self._delete_result(existing)
        entries = [self._entry(user_id, entry_id) for entry_id in requested]
        for entry in entries:
            if int(entry.revision) != requested[entry.entry_id]:
                raise WorkspaceError(409, "REVISION_CONFLICT", "条目已被其他操作修改", entry=entry)
        roots: list[WorkspaceEntry] = []
        for entry in sorted(entries, key=lambda item: (item.relative_path.count("/"), item.relative_path)):
            if not any(root.kind == "directory" and entry.relative_path.startswith(root.relative_path + "/") for root in roots):
                roots.append(entry)
        projections: list[dict[str, Any]] = []
        claims: list[WorkspaceClaimSpec] = []
        ancestor_ids: set[str] = set()
        for root in roots:
            descendants = self._descendants_by_path_prefix(
                user_id, root.relative_path, status="active", for_update=False,
            ) if root.kind == "directory" else []
            subtree = [root, *descendants]
            ancestors = self._ancestor_entry_ids(user_id, root.parent_id)
            ancestor_ids.update(ancestors)
            for entry in subtree:
                self._assert_agent_relocation_preserves_stable_identity(
                    user_id=user_id, entry_id=entry.entry_id, actor=actor, context=context,
                )
            projections.extend(self._journal_projection(item) for item in subtree)
            claims.append(WorkspaceClaimSpec(
                "tree" if root.kind == "directory" else "file",
                tree_scope(root.entry_id) if root.kind == "directory" else file_scope(root.entry_id),
                root.entry_id, conflict_scope_keys=self._tree_scope_keys(ancestors),
                conflict_entry_ids=tuple(item.entry_id for item in subtree) if root.kind == "directory" else (),
            ))
        root_projections = [self._journal_projection(item) for item in roots]
        entry_ids = [item["entry_id"] for item in projections]
        blob_ids = {item[0] for item in self.db.query(WorkspaceFileVersion.blob_id).filter(
            WorkspaceFileVersion.user_id == user_id, WorkspaceFileVersion.entry_id.in_(entry_ids),
            WorkspaceFileVersion.blob_id.isnot(None),
        ).all()}
        blob_ids.update(item[0] for item in self.db.query(WorkspaceChangeSet.proposal_blob_id).filter(
            WorkspaceChangeSet.user_id == user_id, WorkspaceChangeSet.entry_id.in_(entry_ids),
            WorkspaceChangeSet.proposal_blob_id.isnot(None),
        ).all())
        paths = [item["relative_path"] for item in root_projections]
        cleanup_paths = [f"{WORKSPACE_SYSTEM_DIRECTORY}/read/{entry_id}" for entry_id in entry_ids]
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace, workspace_user_id=user_id,
            entry_id=entry_ids[0] if len(roots) == 1 else None,
            actor=actor, operation="delete_entries", result_status="DELETED",
            idempotency_key=idempotency_key, context=context,
            before_revision=None, before_sha256=None, after_revision=0, after_sha256=None,
            journal={
                "action": "delete_many", "request_fingerprint": fingerprint,
                "delete_paths": paths, "cleanup_paths": cleanup_paths,
                "delete_entry_ids": entry_ids, "root_projections": root_projections,
                "before_entry_projections": projections,
                "tree_revision_entry_ids": sorted(ancestor_ids - set(entry_ids)),
                "bytes_delta": -sum(int(item["size_bytes"]) for item in projections if item["kind"] == "file"),
                "entries_delta": -len(projections), "released_blob_ids": sorted(blob_ids),
            }, claim_specs=claims,
        )
        async with self._guard_workspace_claims(
            store, prepared.leases,
        ) as heartbeat:
            heartbeat.raise_if_lost()
            await _complete_snapshot_before_cancellation(store.delete_entries(paths, cleanup_paths))
            heartbeat.raise_if_lost()
            result = self._complete_prepared_delete(prepared)
        await self._cleanup_deleted_objects(user_id, blob_ids, store)
        return result

    async def _cleanup_deleted_objects(self, user_id: str, blob_ids: Iterable[str], store: WorkspaceStore) -> None:
        # No retention for deleted files. Shared objects stay only when another
        # live entry/version/proposal references them; failed physical GC is
        # already recoverable through the ordinary orphan-object maintenance.
        try:
            await self._prune_unreferenced_objects(user_id, blob_ids, store, at=now_naive())
        except Exception:
            self.db.rollback()
            logger.warning("删除文件的零引用对象批量清理待重试", exc_info=True)

    async def import_session_file(
        self,
        user_id: str,
        *,
        session_id: str,
        source_path: str,
        source_revision: str,
        destination_parent_id: str | None,
        destination_name: str,
        conflict_policy: Literal["fail", "overwrite"] = "fail",
        expected_destination_revision: int | str | None = None,
        idempotency_key: str,
        actor: str = "web",
        context: dict[str, Any] | None = None,
    ) -> WorkspaceMutationResult:
        session = self.db.query(Session).filter(Session.id == session_id, Session.user_id == user_id).first()
        if session is None:
            raise WorkspaceError(404, "SESSION_NOT_FOUND", "会话不存在")
        source_relative = WorkspacePathPolicy.normalize_external_relative_path(source_path)
        if any(
            component.startswith(WORKSPACE_SYSTEM_DIRECTORY)
            or component == ".workspace-snapshots"
            for component in source_relative.split("/")
        ):
            raise WorkspaceError(403, "SESSION_SYSTEM_FILE_DENIED", "Session 系统文件不能存入工作区")
        mount_path = self.sandbox_service.get_mount_path(user_id)
        session_root = posixpath.join(mount_path, "sessions", session_id)
        merged_context = {**(context or {}), "session_id": session_id}
        return await self._publish_external(
            user_id,
            source_root=session_root,
            source_relative_path=source_relative,
            expected_source_revision=source_revision,
            destination_parent_id=destination_parent_id,
            destination_name=destination_name,
            conflict_policy=conflict_policy,
            expected_destination_revision=expected_destination_revision,
            idempotency_key=idempotency_key,
            actor=actor,
            context=merged_context,
            operation="import_session_file",
        )

    async def publish_sandbox_file(
        self,
        user_id: str,
        *,
        source_path: str,
        destination_parent_id: str | None,
        destination_name: str,
        conflict_policy: Literal["fail", "overwrite"] = "fail",
        expected_destination_revision: int | str | None = None,
        base_version_id: str | None = None,
        actor: str = "chat",
        context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> WorkspaceChangeSetResult:
        mount_path = posixpath.normpath(self.sandbox_service.get_mount_path(user_id))
        source_absolute = posixpath.normpath(source_path)
        if source_absolute == mount_path or not source_absolute.startswith(mount_path + "/"):
            raise WorkspaceError(403, "SOURCE_OUTSIDE_USER_MOUNT", "发布源文件不在用户沙箱挂载目录内")
        source_relative = WorkspacePathPolicy.normalize_external_relative_path(
            source_absolute[len(mount_path) + 1:]
        )
        if not source_relative.startswith((_SESSION_SOURCE_PREFIX, _CRON_SOURCE_PREFIX)):
            raise WorkspaceError(403, "SOURCE_SCOPE_DENIED", "只能发布 Session 或 Cron 运行目录中的文件")
        _workspace, store = await self._prepare(user_id, for_update=False)
        source_stat = await store.stat_external(mount_path, source_relative)
        return await self._propose_external_change(
            user_id,
            store=store,
            source_stat=source_stat,
            source_root=mount_path,
            source_relative_path=source_relative,
            expected_source_revision=source_stat.source_revision,
            destination_parent_id=destination_parent_id,
            destination_name=destination_name,
            conflict_policy=conflict_policy,
            expected_destination_revision=expected_destination_revision,
            base_version_id=base_version_id,
            idempotency_key=idempotency_key,
            actor=actor,
            context=context,
            operation="publish_sandbox_file",
            require_explicit_base=True,
        )

    def _finalize_change_set_references(self, row: WorkspaceChangeSet) -> None:
        if row.status not in {"applied", "rejected", "failed"}:
            return
        self.db.query(WorkspaceContentReference).filter(
            WorkspaceContentReference.user_id == row.user_id,
            WorkspaceContentReference.reference_kind == "change_set_base",
            WorkspaceContentReference.reference_key == f"{row.change_set_id}:base",
        ).delete(synchronize_session=False)
        proposal_reference = self.db.query(WorkspaceContentReference).filter(
            WorkspaceContentReference.user_id == row.user_id,
            WorkspaceContentReference.reference_kind == "change_set_proposal",
            WorkspaceContentReference.reference_key == f"{row.change_set_id}:proposal",
        ).one_or_none()
        if proposal_reference is not None and proposal_reference.retained_until is None:
            proposal_reference.retained_until = now_naive() + timedelta(
                days=int(self.settings.workspace_version_retention_days)
            )
            proposal_reference.updated_at = now_naive()

    def _change_set_result(self, row: WorkspaceChangeSet) -> WorkspaceChangeSetResult:
        row = (
            self.db.query(WorkspaceChangeSet)
            .populate_existing()
            .filter(WorkspaceChangeSet.change_set_id == row.change_set_id)
            .one()
        )
        entry = None
        if row.entry_id:
            entry = self.db.query(WorkspaceEntry).filter(
                WorkspaceEntry.user_id == row.user_id,
                WorkspaceEntry.entry_id == row.entry_id,
            ).one_or_none()
        mutation = self.db.query(WorkspaceMutation).filter(
            WorkspaceMutation.change_set_id == row.change_set_id,
            WorkspaceMutation.state == "completed",
        ).order_by(WorkspaceMutation.completed_at.desc()).first()
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        result_values = {
            "status": str(row.status).upper(),
            "change_set_id": row.change_set_id,
            "mutation_id": mutation.mutation_id if mutation else None,
            "base_version_id": row.base_version_id,
            "proposed_version_id": row.proposed_version_id,
            "applied_version_id": row.applied_version_id,
            "mutation_status": mutation.result_status if mutation else None,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "target_name": details.get("destination_name"),
            "target_path": details.get("destination_path"),
        }
        if entry is not None and entry in self.db:
            # Tool execution closes its short-lived DB session before rendering
            # the result. Detach a fully loaded projection before the terminal
            # commit can expire it.
            self.db.expunge(entry)
        if row.status in {"applied", "rejected", "failed"}:
            self._finalize_change_set_references(row)
            self.db.commit()
        return WorkspaceChangeSetResult(
            status=result_values["status"],
            change_set_id=result_values["change_set_id"],
            entry=entry,
            mutation_id=result_values["mutation_id"],
            base_version_id=result_values["base_version_id"],
            proposed_version_id=result_values["proposed_version_id"],
            applied_version_id=result_values["applied_version_id"],
            mutation_status=result_values["mutation_status"],
            error_code=result_values["error_code"],
            error_message=result_values["error_message"],
            target_name=result_values["target_name"],
            target_path=result_values["target_path"],
        )

    def _bind_change_set_proposal(
        self,
        *,
        user_id: str,
        change_set_id: str,
        blob_id: str,
        sha256: str,
        size_bytes: int,
        content_path: str,
    ) -> bool:
        """Atomically bind a verified proposal object and its durable reference."""
        row = self.db.query(WorkspaceChangeSet).filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.change_set_id == change_set_id,
        ).with_for_update().one_or_none()
        if row is None:
            self.db.rollback()
            raise WorkspaceError(404, "CHANGE_SET_NOT_FOUND", "工作区修改提案不存在")
        if row.proposal_blob_id and str(row.proposal_blob_id) != blob_id:
            self.db.rollback()
            raise WorkspaceError(409, "CONTENT_OBJECT_COLLISION", "工作区修改提案对象引用冲突")
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError as exc:
            self.db.rollback()
            raise WorkspaceError(409, "CHANGE_SET_INVALID", "工作区修改提案记录无效") from exc
        if not isinstance(details, dict):
            self.db.rollback()
            raise WorkspaceError(409, "CHANGE_SET_INVALID", "工作区修改提案记录无效")
        reference = self.db.query(WorkspaceContentReference).filter(
            WorkspaceContentReference.user_id == user_id,
            WorkspaceContentReference.reference_kind == "change_set_proposal",
            WorkspaceContentReference.reference_key == f"{change_set_id}:proposal",
        ).one_or_none()
        already_bound = (
            str(row.proposal_blob_id or "") == blob_id
            and details.get("proposal_path") == content_path
            and reference is not None
            and str(reference.blob_id) == blob_id
            and row.status != "preparing"
        )
        history_delta = self._persist_content_object_record(
            user_id=user_id,
            blob_id=blob_id,
            sha256=sha256,
            size_bytes=size_bytes,
            content_path=content_path,
        )
        self._upsert_content_reference(
            user_id=user_id,
            blob_id=blob_id,
            version_id=None,
            reference_kind="change_set_proposal",
            reference_key=f"{change_set_id}:proposal",
        )
        if row.base_version_id:
            base_version = self.db.query(WorkspaceFileVersion).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.version_id == row.base_version_id,
                WorkspaceFileVersion.state == "materialized",
            ).one_or_none()
            if base_version is not None and base_version.blob_id:
                self._upsert_content_reference(
                    user_id=user_id,
                    blob_id=str(base_version.blob_id),
                    version_id=str(base_version.version_id),
                    reference_kind="change_set_base",
                    reference_key=f"{change_set_id}:base",
                )
        workspace_row = self.db.query(UserWorkspace).filter(
            UserWorkspace.user_id == user_id,
        ).with_for_update().one()
        workspace_row.history_used_bytes = int(workspace_row.history_used_bytes or 0) + history_delta
        row.proposal_blob_id = blob_id
        legacy_proposal_path = str(details.get("proposal_path") or "")
        if (
            legacy_proposal_path.startswith(WORKSPACE_TEMP_DIRECTORY + "/")
            and not details.get("proposal_temp_path")
        ):
            details["proposal_temp_path"] = legacy_proposal_path
        details["proposal_path"] = content_path
        details.setdefault("proposal_bound_at", now_naive().isoformat())
        row.details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        if row.status == "preparing":
            row.status = "conflict" if row.error_code == "BASE_VERSION_CONFLICT" else "proposed"
        # Production SessionLocal uses autoflush=False.  Flush the newly
        # inserted proposal reference before terminal retention queries it;
        # otherwise migrated applied/rejected rows commit a NULL retention.
        self.db.flush()
        self._finalize_change_set_references(row)
        self.db.commit()
        return not already_bound

    def _clear_change_set_proposal_temp_path(
        self,
        *,
        user_id: str,
        change_set_id: str,
        cleared_paths: Iterable[str],
    ) -> None:
        cleared = {str(path) for path in cleared_paths if path}
        if not cleared:
            return
        row = self.db.query(WorkspaceChangeSet).filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.change_set_id == change_set_id,
        ).with_for_update().one_or_none()
        if row is None:
            self.db.rollback()
            return
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError:
            self.db.rollback()
            return
        if not isinstance(details, dict):
            self.db.rollback()
            return
        temp_path = str(details.get("proposal_temp_path") or "")
        if temp_path not in cleared:
            self.db.rollback()
            return
        details.pop("proposal_temp_path", None)
        row.details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        self.db.commit()

    def _finish_change_set_preparation_failed(
        self,
        *,
        user_id: str,
        change_set_id: str,
        code: str,
        message: str,
    ) -> None:
        row = self.db.query(WorkspaceChangeSet).filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.change_set_id == change_set_id,
            WorkspaceChangeSet.status.in_(("preparing", "proposed", "conflict", "needs_review", "applying")),
        ).with_for_update().one_or_none()
        if row is None:
            self.db.rollback()
            return
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        if not isinstance(details, dict):
            details = {}
        details["failure"] = {
            "code": code,
            "message": message,
            "proposal_preserved": True,
        }
        row.status = "failed"
        row.error_code = code
        row.error_message = message[:1000]
        row.details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        self._finalize_change_set_references(row)
        self.db.commit()

    async def _migrate_change_set_proposal(
        self,
        user_id: str,
        change_set_id: str,
        store: WorkspaceStore,
    ) -> bool:
        """Recover a legacy or interrupted proposal temp into the object store."""
        row = self.db.query(WorkspaceChangeSet).populate_existing().filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.change_set_id == change_set_id,
        ).one_or_none()
        if row is None:
            self.db.rollback()
            return False
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError:
            self._finish_change_set_preparation_failed(
                user_id=user_id,
                change_set_id=change_set_id,
                code="CHANGE_SET_INVALID",
                message="工作区修改提案记录无效",
            )
            return False
        if not isinstance(details, dict):
            self._finish_change_set_preparation_failed(
                user_id=user_id,
                change_set_id=change_set_id,
                code="CHANGE_SET_INVALID",
                message="工作区修改提案记录无效",
            )
            return False
        expected_sha = str(details.get("source_sha256") or "")
        try:
            expected_size = int(details.get("source_size_bytes") or 0)
        except (TypeError, ValueError):
            expected_size = -1
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or expected_size < 0:
            self._finish_change_set_preparation_failed(
                user_id=user_id,
                change_set_id=change_set_id,
                code="CHANGE_SET_INVALID",
                message="工作区修改提案摘要或大小无效",
            )
            return False
        blob_id = self._content_object_id(user_id, expected_sha)
        content_path = self._content_object_path(expected_sha)
        if row.proposal_blob_id and str(row.proposal_blob_id) != blob_id:
            self._finish_change_set_preparation_failed(
                user_id=user_id,
                change_set_id=change_set_id,
                code="CONTENT_OBJECT_COLLISION",
                message="工作区修改提案对象引用冲突",
            )
            return False
        legacy_proposal_path = str(details.get("proposal_path") or "")
        raw_temp_candidates = [
            str(details.get("proposal_temp_path") or ""),
            legacy_proposal_path if legacy_proposal_path != content_path else "",
        ]
        temp_candidates: list[str] = []
        for raw_path in raw_temp_candidates:
            if not raw_path:
                continue
            try:
                normalized_temp = WorkspacePathPolicy.normalize_relative_path(
                    raw_path,
                    allow_system=True,
                )
            except WorkspaceError:
                continue
            if normalized_temp.startswith(WORKSPACE_TEMP_DIRECTORY + "/"):
                temp_candidates.append(normalized_temp)
        temp_candidates = list(dict.fromkeys(temp_candidates))
        self.db.rollback()
        leases = tuple(self._acquire_claims(
            user_id=user_id,
            operation="migrate_change_set_proposal",
            specs=(WorkspaceClaimSpec("path", f"object:{blob_id}"),),
        ))
        migrated = False
        binding_committed = False
        verified_temp_candidates: list[str] = []
        unresolved_temp = False
        try:
            async with self._guard_workspace_claims(store, leases) as heartbeat:
                for candidate in temp_candidates:
                    try:
                        candidate_stat = await store.stat(candidate)
                    except WorkspaceError as exc:
                        if exc.code == "NOT_FOUND":
                            continue
                        raise
                    if (
                        candidate_stat.sha256 == expected_sha
                        and int(candidate_stat.size_bytes) == expected_size
                    ):
                        verified_temp_candidates.append(candidate)
                    else:
                        unresolved_temp = True
                        logger.warning(
                            "Change set legacy temp validation failed; preserving original change_set=%s path=%s",
                            change_set_id,
                            candidate,
                        )
                object_ready = False
                try:
                    object_stat = await store.stat(content_path)
                    object_ready = (
                        object_stat.sha256 == expected_sha
                        and int(object_stat.size_bytes) == expected_size
                    )
                    if not object_ready:
                        self._finish_change_set_preparation_failed(
                            user_id=user_id,
                            change_set_id=change_set_id,
                            code="CHANGE_SET_CONTENT_CHANGED",
                            message="工作区修改提案对象校验失败",
                        )
                        return False
                except WorkspaceError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                if not object_ready:
                    source_path = (
                        verified_temp_candidates[0]
                        if verified_temp_candidates
                        else None
                    )
                    if source_path is None:
                        self._finish_change_set_preparation_failed(
                            user_id=user_id,
                            change_set_id=change_set_id,
                            code="CHANGE_SET_PREPARATION_LOST",
                            message="工作区修改提案准备中断且未找到可恢复内容",
                        )
                        return False
                    await _complete_snapshot_before_cancellation(
                        store.ensure_content_object(
                            source_relative_path=source_path,
                            destination_relative_path=content_path,
                            expected_sha256=expected_sha,
                            expected_size_bytes=expected_size,
                        )
                    )
                heartbeat.raise_if_lost()
                migrated = self._bind_change_set_proposal(
                    user_id=user_id,
                    change_set_id=change_set_id,
                    blob_id=blob_id,
                    sha256=expected_sha,
                    size_bytes=expected_size,
                    content_path=content_path,
                )
                binding_committed = True
        finally:
            self._release_unattached_claims(leases)
        if binding_committed:
            for candidate in verified_temp_candidates:
                try:
                    await store.remove(candidate)
                except WorkspaceError as cleanup_error:
                    if cleanup_error.code != "NOT_FOUND":
                        unresolved_temp = True
                        logger.warning(
                            "Change set legacy temp cleanup failed change_set=%s path=%s",
                            change_set_id,
                            candidate,
                        )
            if not unresolved_temp:
                self._clear_change_set_proposal_temp_path(
                    user_id=user_id,
                    change_set_id=change_set_id,
                    cleared_paths=temp_candidates,
                )
        return migrated

    async def migrate_legacy_change_set_proposals(
        self,
        user_id: str,
        *,
        limit: int = 100,
    ) -> int:
        """Idempotently bind recoverable proposal temps before deleting originals."""
        _workspace, store = await self._prepare(user_id, for_update=False)
        proposal_reference_exists = self.db.query(
            WorkspaceContentReference.reference_id
        ).filter(
            WorkspaceContentReference.user_id == user_id,
            WorkspaceContentReference.reference_kind == "change_set_proposal",
            WorkspaceContentReference.reference_key
            == WorkspaceChangeSet.change_set_id.concat(":proposal"),
        ).exists()
        proposal_reference_missing_retention = self.db.query(
            WorkspaceContentReference.reference_id
        ).filter(
            WorkspaceContentReference.user_id == user_id,
            WorkspaceContentReference.reference_kind == "change_set_proposal",
            WorkspaceContentReference.reference_key
            == WorkspaceChangeSet.change_set_id.concat(":proposal"),
            WorkspaceContentReference.retained_until.is_(None),
        ).exists()
        change_set_ids = [
            str(item[0])
            for item in (
                self.db.query(WorkspaceChangeSet.change_set_id)
                .filter(
                    WorkspaceChangeSet.user_id == user_id,
                    or_(
                        WorkspaceChangeSet.proposal_blob_id.is_(None),
                        WorkspaceChangeSet.details_json.like(
                            f'%"proposal_path":"{WORKSPACE_TEMP_DIRECTORY}/%'
                        ),
                        WorkspaceChangeSet.details_json.like(
                            f'%"proposal_path": "{WORKSPACE_TEMP_DIRECTORY}/%'
                        ),
                        WorkspaceChangeSet.details_json.like(
                            f'%"proposal_temp_path":"{WORKSPACE_TEMP_DIRECTORY}/%'
                        ),
                        WorkspaceChangeSet.details_json.like(
                            f'%"proposal_temp_path": "{WORKSPACE_TEMP_DIRECTORY}/%'
                        ),
                        ~proposal_reference_exists,
                        proposal_reference_missing_retention,
                    ),
                )
                .order_by(WorkspaceChangeSet.created_at.asc())
                .limit(max(1, min(int(limit), 1000)))
                .all()
            )
        ]
        self.db.rollback()
        migrated = 0
        for candidate_id in change_set_ids:
            try:
                migrated += int(
                    await self._migrate_change_set_proposal(
                        user_id,
                        candidate_id,
                        store,
                    )
                )
            except WorkspaceError as exc:
                self.db.rollback()
                if exc.code == "WORKSPACE_MUTATION_IN_PROGRESS":
                    continue
                logger.warning(
                    "Workspace change set proposal migration deferred user=%s change_set=%s code=%s",
                    user_id,
                    candidate_id,
                    exc.code,
                )
            except asyncio.CancelledError:
                self.db.rollback()
                raise
            except Exception:
                self.db.rollback()
                logger.warning(
                    "Workspace change set proposal migration deferred user=%s change_set=%s",
                    user_id,
                    candidate_id,
                    exc_info=True,
                )
        return migrated

    async def _propose_external_change(
        self,
        user_id: str,
        *,
        store: WorkspaceStore,
        source_stat: SandboxFileStat,
        source_root: str,
        source_relative_path: str,
        expected_source_revision: str,
        destination_parent_id: str | None,
        destination_name: str,
        conflict_policy: Literal["fail", "overwrite"],
        expected_destination_revision: int | str | None,
        base_version_id: str | None,
        idempotency_key: str | None,
        actor: str,
        context: dict[str, Any] | None,
        operation: str,
        require_explicit_base: bool = False,
    ) -> WorkspaceChangeSetResult:
        safe_name = WorkspacePathPolicy.validate_name(destination_name)
        stable_key = idempotency_key or f"workspace-change-set:{uuid.uuid4()}"
        fingerprint_payload = {
            "operation": operation,
            "source_root": source_root,
            "source_relative_path": source_relative_path,
            "source_revision": expected_source_revision,
            "destination_parent_id": destination_parent_id,
            "destination_name": safe_name,
            "conflict_policy": conflict_policy,
            "expected_destination_revision": expected_destination_revision,
            "base_version_id": base_version_id,
            "actor": actor,
        }
        request_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = self.db.query(WorkspaceChangeSet).filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.idempotency_key == stable_key,
        ).one_or_none()
        if existing is not None:
            try:
                existing_details = json.loads(existing.details_json or "{}")
            except json.JSONDecodeError:
                existing_details = {}
            if existing_details.get("request_fingerprint") != request_fingerprint:
                raise WorkspaceError(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "幂等键已用于其他工作区修改提案",
                )
            if existing.status in {"proposed", "conflict", "needs_review", "applying"}:
                return await self.apply_change_set(user_id, existing.change_set_id)
            if existing.status == "preparing":
                raise WorkspaceError(409, "CHANGE_SET_NOT_READY", "工作区修改提案仍在准备")
            return self._change_set_result(existing)

        parent = self._parent(user_id, destination_parent_id)
        destination_path = WorkspacePathPolicy.join(
            parent.relative_path if parent else None,
            safe_name,
        )
        target = self._sibling(
            user_id,
            destination_parent_id,
            safe_name,
            for_update=False,
        )
        if target is None:
            self._assert_agent_does_not_recreate_deleted_path(
                user_id=user_id,
                destination_path=destination_path,
                actor=actor,
                context=context,
            )
        normalized_expected = WorkspacePathPolicy.normalize_revision(expected_destination_revision)
        target_revision = int(target.revision) if target is not None else None
        target_version_id = target.current_version_id if target is not None else None
        if target is not None and require_explicit_base and not base_version_id:
            raise WorkspaceError(
                428,
                "BASE_VERSION_REQUIRED",
                "发布到已有文件必须携带生成提案时的基线版本",
                entry=target,
            )
        requested_base = base_version_id or target_version_id
        base_matches = True
        conflict_reason: str | None = None
        if target is not None and target.kind != "file":
            base_matches = False
            conflict_reason = "目标名称已被文件夹占用"
        elif target is not None and conflict_policy != "overwrite":
            base_matches = False
            conflict_reason = "目标文件已存在，需要人工确认"
        elif target is not None and base_version_id is not None:
            base_matches = target_version_id == base_version_id
            if not base_matches:
                conflict_reason = "目标文件版本已变化"
        elif target is not None:
            base_matches = normalized_expected == target_revision
            if not base_matches:
                conflict_reason = "目标文件版本已变化"
        elif normalized_expected is not None or base_version_id is not None:
            base_matches = False
            conflict_reason = "目标文件已不存在"

        if source_stat.source_revision != expected_source_revision:
            raise WorkspaceError(412, "SOURCE_REVISION_CONFLICT", "发布源文件已变化")
        if not isinstance(source_stat.sha256, str):
            raise WorkspaceError(409, "MISSING_CONTENT_HASH", "发布源文件缺少内容摘要")
        change_set_id = str(uuid.uuid4())
        proposal_blob_id = self._content_object_id(user_id, source_stat.sha256)
        proposal_path = self._content_object_path(source_stat.sha256)
        proposal_temp_path = f"{WORKSPACE_TEMP_DIRECTORY}/change-set-{change_set_id}.proposal"
        context_fields = self._context_fields(context)
        details = {
            "request_fingerprint": request_fingerprint,
            "source_root": source_root,
            "source_relative_path": source_relative_path,
            "source_revision": expected_source_revision,
            "source_sha256": source_stat.sha256,
            "source_size_bytes": int(source_stat.size_bytes),
            "planned_proposal_blob_id": proposal_blob_id,
            "planned_proposal_path": proposal_path,
            "proposal_temp_path": proposal_temp_path,
            "destination_parent_id": destination_parent_id,
            "destination_name": safe_name,
            "destination_path": destination_path,
            "target_existed": target is not None,
            "base_revision": target_revision,
            "conflict_policy": conflict_policy,
        }
        row = WorkspaceChangeSet(
            change_set_id=change_set_id,
            user_id=user_id,
            entry_id=str(target.entry_id) if target is not None else None,
            operation=operation,
            status="preparing",
            actor=actor,
            base_version_id=requested_base,
            proposed_version_id=None,
            applied_version_id=None,
            proposal_blob_id=None,
            idempotency_key=stable_key,
            session_id=context_fields.get("session_id"),
            round_id=context_fields.get("round_id"),
            tool_call_id=context_fields.get("tool_call_id"),
            cron_run_id=context_fields.get("cron_run_id"),
            details_json=json.dumps(details, ensure_ascii=False, separators=(",", ":")),
            error_code="BASE_VERSION_CONFLICT" if not base_matches else None,
            error_message=conflict_reason,
        )
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            winner = self.db.query(WorkspaceChangeSet).filter(
                WorkspaceChangeSet.user_id == user_id,
                WorkspaceChangeSet.idempotency_key == stable_key,
            ).one()
            return self._change_set_result(winner)

        leases = tuple(self._acquire_claims(
            user_id=user_id,
            operation="prepare_change_set",
            specs=(WorkspaceClaimSpec("path", f"object:{proposal_blob_id}"),),
        ))
        proposal_bound = False
        try:
            async with self._guard_workspace_claims(store, leases) as heartbeat:
                heartbeat.raise_if_lost()
                staged = await _complete_snapshot_before_cancellation(
                    store.copy_external_atomic(
                        source_root=source_root,
                        source_relative_path=source_relative_path,
                        expected_source_revision=expected_source_revision,
                        destination_relative_path=proposal_temp_path,
                        expected_destination_sha256=None,
                        must_not_exist=True,
                        temp_token=change_set_id,
                        allow_system_destination=True,
                    )
                )
                if (
                    staged.sha256 != source_stat.sha256
                    or int(staged.size_bytes) != int(source_stat.size_bytes)
                ):
                    raise WorkspaceError(409, "CHANGE_SET_CONTENT_CHANGED", "工作区修改提案临时文件校验失败")
                heartbeat.raise_if_lost()
                await _complete_snapshot_before_cancellation(
                    store.ensure_content_object(
                        source_relative_path=proposal_temp_path,
                        destination_relative_path=proposal_path,
                        expected_sha256=source_stat.sha256,
                        expected_size_bytes=int(source_stat.size_bytes),
                    )
                )
                heartbeat.raise_if_lost()
                self._bind_change_set_proposal(
                    user_id=user_id,
                    change_set_id=change_set_id,
                    blob_id=proposal_blob_id,
                    sha256=source_stat.sha256,
                    size_bytes=int(source_stat.size_bytes),
                    content_path=proposal_path,
                )
                proposal_bound = True
                cleanup_complete = True
                try:
                    await store.remove(proposal_temp_path)
                except WorkspaceError as cleanup_error:
                    if cleanup_error.code != "NOT_FOUND":
                        cleanup_complete = False
                        logger.warning(
                            "Change set temp cleanup failed change_set=%s path=%s",
                            change_set_id,
                            proposal_temp_path,
                        )
                if cleanup_complete:
                    self._clear_change_set_proposal_temp_path(
                        user_id=user_id,
                        change_set_id=change_set_id,
                        cleared_paths=(proposal_temp_path,),
                    )
        except asyncio.CancelledError:
            # The durable preparing row plus temp/object path is the recovery
            # journal. Maintenance validates and binds it after ownership expires.
            self.db.rollback()
            raise
        except Exception as exc:
            self.db.rollback()
            if not proposal_bound:
                self._finish_change_set_preparation_failed(
                    user_id=user_id,
                    change_set_id=change_set_id,
                    code=str(getattr(exc, "code", type(exc).__name__)),
                    message=str(exc) or "工作区修改提案准备失败",
                )
            raise
        finally:
            self._release_unattached_claims(leases)
        return await self.apply_change_set(
            user_id,
            change_set_id,
            expected_current_version_id=target_version_id if not base_matches else requested_base,
        )

    def list_change_sets(
        self,
        user_id: str,
        *,
        status: str | None = None,
    ) -> list[WorkspaceChangeSet]:
        query = self.db.query(WorkspaceChangeSet).filter(
            WorkspaceChangeSet.user_id == user_id,
        )
        if status:
            query = query.filter(WorkspaceChangeSet.status == status)
        return query.order_by(WorkspaceChangeSet.created_at.desc()).all()

    async def reconcile_change_sets(self, user_id: str, *, limit: int = 100) -> int:
        """Resume automatic convergence after caller cancellation or worker loss."""

        await self.migrate_legacy_change_set_proposals(user_id, limit=limit)
        change_set_ids = [
            str(row[0])
            for row in (
                self.db.query(WorkspaceChangeSet.change_set_id)
                .filter(
                    WorkspaceChangeSet.user_id == user_id,
                    WorkspaceChangeSet.status.in_(("preparing", "proposed", "conflict", "needs_review", "applying")),
                )
                .order_by(WorkspaceChangeSet.created_at.asc())
                .limit(max(1, min(int(limit), 1000)))
                .all()
            )
        ]
        self.db.rollback()
        converged = 0
        for change_set_id in change_set_ids:
            try:
                result = await self.apply_change_set(user_id, change_set_id)
            except WorkspaceError as exc:
                if exc.code in {"CHANGE_SET_NOT_READY", "WORKSPACE_MUTATION_IN_PROGRESS"}:
                    self.db.rollback()
                    continue
                raise
            if result.status in {"APPLIED", "REJECTED"}:
                converged += 1
        return converged

    async def open_change_set_content(
        self,
        user_id: str,
        change_set_id: str,
    ) -> WorkspaceChangeSetContent:
        _workspace, store = await self._prepare(user_id, for_update=False)
        row = self.db.query(WorkspaceChangeSet).populate_existing().filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.change_set_id == change_set_id,
        ).one_or_none()
        if row is None:
            raise WorkspaceError(404, "CHANGE_SET_NOT_FOUND", "工作区修改提案不存在")
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError as exc:
            raise WorkspaceError(409, "CHANGE_SET_INVALID", "工作区修改提案记录无效") from exc
        proposal_path = str(details.get("proposal_path") or "")
        if not proposal_path:
            raise WorkspaceError(404, "CHANGE_SET_CONTENT_NOT_FOUND", "工作区修改提案内容不存在")
        filename = str(details.get("destination_name") or "workspace-proposal")
        mime_type, _ = mimetypes.guess_type(filename)
        result = WorkspaceChangeSetContent(
            change_set=row,
            sandbox=store.sandbox,
            sandbox_path=store.absolute_path(proposal_path),
            filename=filename,
            size_bytes=int(details.get("source_size_bytes") or 0),
            mime_type=mime_type,
        )
        expected_sha = details.get("source_sha256")
        self.db.expunge(row)
        self.db.commit()
        state = await store.inspect_path(proposal_path, allow_system=True)
        if state.kind != "file" or state.sha256 != expected_sha:
            raise WorkspaceError(409, "CHANGE_SET_CONTENT_CHANGED", "工作区修改提案内容校验失败")
        return result

    def reject_change_set(self, user_id: str, change_set_id: str) -> WorkspaceChangeSet:
        row = self.db.query(WorkspaceChangeSet).filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.change_set_id == change_set_id,
        ).with_for_update().one_or_none()
        if row is None:
            raise WorkspaceError(404, "CHANGE_SET_NOT_FOUND", "工作区修改提案不存在")
        if row.status == "applied":
            raise WorkspaceError(409, "CHANGE_SET_ALREADY_APPLIED", "工作区修改提案已经发布")
        if row.status not in {"proposed", "conflict", "needs_review"}:
            raise WorkspaceError(409, "CHANGE_SET_NOT_READY", "工作区修改提案正在准备或发布")
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError as exc:
            raise WorkspaceError(409, "CHANGE_SET_INVALID", "工作区修改提案记录无效") from exc
        if isinstance(details, dict) and details.get("apply_started_at"):
            raise WorkspaceError(409, "CHANGE_SET_NOT_READY", "工作区修改提案已经进入发布流程")
        row.status = "rejected"
        row.error_code = None
        row.error_message = None
        self._finalize_change_set_references(row)
        self.db.commit()
        return row

    async def _read_verified_change_set_bytes(
        self,
        store: WorkspaceStore,
        path: str,
        *,
        expected_sha256: str | None,
        expected_size: int | None,
        allow_system: bool,
    ) -> bytes:
        try:
            content = await store.read_bytes(path, allow_system=allow_system)
        except WorkspaceError:
            raise
        except Exception as exc:
            raise WorkspaceError(
                503,
                "SANDBOX_READ_FAILED",
                "工作区内部版本读取失败",
            ) from exc
        if expected_size is not None and len(content) != expected_size:
            raise WorkspaceError(409, "CHANGE_SET_CONTENT_CHANGED", "工作区修改内容大小校验失败")
        if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
            raise WorkspaceError(409, "CHANGE_SET_CONTENT_CHANGED", "工作区修改内容校验失败")
        return content

    def _resolve_entry_head_content(
        self,
        user_id: str,
        entry: WorkspaceEntry,
    ) -> WorkspaceHeadContent:
        version_id = str(entry.current_version_id or "")
        if not version_id:
            raise WorkspaceError(409, "CURRENT_HEAD_UNAVAILABLE", "文件缺少当前内部版本")
        version = self.db.query(WorkspaceFileVersion).filter(
            WorkspaceFileVersion.user_id == user_id,
            WorkspaceFileVersion.entry_id == entry.entry_id,
            WorkspaceFileVersion.version_id == version_id,
            WorkspaceFileVersion.state == "materialized",
        ).one_or_none()
        if (
            version is None
            or not version.content_path
            or not version.sha256
            or version.sha256 != entry.sha256
            or int(version.size_bytes or 0) != int(entry.size_bytes or 0)
        ):
            raise WorkspaceError(409, "CURRENT_HEAD_INVALID", "文件当前内部版本与工作区记录不一致")

        content_path = WorkspacePathPolicy.normalize_relative_path(
            str(version.content_path),
            allow_system=True,
        )
        if not content_path.startswith(WORKSPACE_SYSTEM_DIRECTORY + "/"):
            raise WorkspaceError(409, "CURRENT_HEAD_INVALID", "文件当前内部版本路径无效")
        blob_id = str(version.blob_id or "") or None
        entry_blob_id = str(entry.head_blob_id or "") or None
        if blob_id is None or entry_blob_id is None or blob_id != entry_blob_id:
            raise WorkspaceError(409, "CURRENT_HEAD_INVALID", "文件当前内容对象引用不一致")
        content_object = self.db.query(WorkspaceContentObject).filter(
            WorkspaceContentObject.user_id == user_id,
            WorkspaceContentObject.blob_id == blob_id,
            WorkspaceContentObject.state == "materialized",
        ).one_or_none()
        if (
            content_object is None
            or content_object.sha256 != version.sha256
            or int(content_object.size_bytes or 0) != int(version.size_bytes or 0)
            or str(content_object.content_path) != content_path
        ):
            raise WorkspaceError(409, "CURRENT_HEAD_INVALID", "文件当前内容对象不可用")
        return WorkspaceHeadContent(
            version_id=version_id,
            blob_id=blob_id,
            content_path=content_path,
            sha256=str(version.sha256),
            size_bytes=int(version.size_bytes or 0),
        )

    def _lock_owned_applying_change_set(
        self,
        change_set_id: str,
        apply_lease: WorkspaceClaimLease,
    ) -> tuple[WorkspaceChangeSet, dict[str, Any]]:
        row = self.db.query(WorkspaceChangeSet).filter(
            WorkspaceChangeSet.change_set_id == change_set_id,
            WorkspaceChangeSet.status == "applying",
        ).with_for_update().one_or_none()
        if row is None:
            self.db.rollback()
            raise WorkspaceError(409, "CHANGE_SET_APPLY_FENCED", "工作区修改提案发布所有权已失效")
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        owner = details.get("apply_owner") if isinstance(details, dict) else None
        if (
            not isinstance(owner, dict)
            or owner.get("owner_token") != apply_lease.owner_token
            or int(owner.get("generation") or 0) != int(apply_lease.generation)
        ):
            self.db.rollback()
            raise WorkspaceError(409, "CHANGE_SET_APPLY_FENCED", "工作区修改提案发布所有权已失效")
        return row, details

    def _finish_change_set_failed(
        self,
        change_set_id: str,
        *,
        apply_lease: WorkspaceClaimLease,
        details: dict[str, Any],
        code: str,
        message: str,
    ) -> WorkspaceChangeSetResult:
        details["failure"] = {
            "code": code,
            "message": message,
            "proposal_preserved": True,
        }
        row, _owned_details = self._lock_owned_applying_change_set(change_set_id, apply_lease)
        row.status = "failed"
        row.applied_at = None
        row.details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        row.error_code = code
        row.error_message = message[:1000]
        self.db.commit()
        return self._change_set_result(self.db.get(WorkspaceChangeSet, change_set_id))

    def _defer_change_set_retry(
        self,
        change_set_id: str,
        *,
        apply_lease: WorkspaceClaimLease,
        details: dict[str, Any],
        code: str,
        message: str,
    ) -> WorkspaceChangeSetResult:
        details["auto_merge"] = {
            "algorithm": "workspace-three-way-v1",
            "outcome": "retrying",
            "reason": code,
            "proposal_preserved": True,
        }
        row, _owned_details = self._lock_owned_applying_change_set(change_set_id, apply_lease)
        row.status = "conflict"
        row.details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        row.error_code = code
        row.error_message = message[:1000]
        self.db.commit()
        return self._change_set_result(self.db.get(WorkspaceChangeSet, change_set_id))

    def _finish_change_set_for_workspace_error(
        self,
        change_set_id: str,
        *,
        apply_lease: WorkspaceClaimLease,
        details: dict[str, Any],
        error: WorkspaceError,
        retry_message: str,
    ) -> WorkspaceChangeSetResult:
        self.db.rollback()
        prepared_exists = self.db.query(WorkspaceMutation.mutation_id).filter(
            WorkspaceMutation.change_set_id == change_set_id,
            WorkspaceMutation.state == "prepared",
        ).first() is not None
        retryable_codes = {
            "CONTENT_OBJECT_PRUNING",
            "DESTINATION_CHANGED",
            "IDEMPOTENT_OPERATION_COMPLETED",
            "MUTATION_CONFLICT",
            "MUTATION_FENCED",
            "MUTATION_IN_PROGRESS",
            "NAME_CONFLICT",
            "REVISION_CONFLICT",
            "SOURCE_REVISION_CONFLICT",
            "VERSION_SNAPSHOT_CHANGED",
            "WORKSPACE_MUTATION_IN_PROGRESS",
        }
        if prepared_exists or error.code in retryable_codes:
            return self._defer_change_set_retry(
                change_set_id,
                apply_lease=apply_lease,
                details=details,
                code=error.code,
                message=retry_message,
            )
        return self._finish_change_set_failed(
            change_set_id,
            apply_lease=apply_lease,
            details=details,
            code=error.code,
            message=error.message,
        )

    async def _materialize_legacy_head(
        self,
        user_id: str,
        *,
        workspace: UserWorkspace,
        store: WorkspaceStore,
        entry: WorkspaceEntry,
        physical: SandboxFileStat,
    ) -> WorkspaceEntry:
        target_path = str(entry.relative_path)
        before_sha = str(entry.sha256 or "") or None
        if (
            physical.sha256 != before_sha
            or int(physical.size_bytes) != int(entry.size_bytes or 0)
        ):
            raise WorkspaceError(
                409,
                "CURRENT_HEAD_UNAVAILABLE",
                "旧工作区文件缺少内部基线且实体内容已经变化",
            )
        current_version = None
        if entry.current_version_id:
            current_version = self.db.query(WorkspaceFileVersion).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.entry_id == entry.entry_id,
                WorkspaceFileVersion.version_id == entry.current_version_id,
                WorkspaceFileVersion.state == "materialized",
            ).one_or_none()
            if current_version is None:
                raise WorkspaceError(409, "CURRENT_HEAD_UNAVAILABLE", "旧工作区当前版本记录不存在")
            if current_version.blob_id or entry.head_blob_id:
                raise WorkspaceError(409, "CURRENT_HEAD_INVALID", "旧工作区内容对象引用不完整")
            if (
                current_version.sha256 != entry.sha256
                or int(current_version.size_bytes or 0) != int(entry.size_bytes or 0)
            ):
                raise WorkspaceError(409, "CURRENT_HEAD_INVALID", "旧工作区当前版本与条目不一致")
        latest_version = current_version or (
            self.db.query(WorkspaceFileVersion)
            .filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.entry_id == entry.entry_id,
                WorkspaceFileVersion.state == "materialized",
            )
            .order_by(WorkspaceFileVersion.sequence.desc())
            .first()
        )
        next_version_id = str(uuid.uuid4())
        next_row = self._version_row(
            version_id=next_version_id,
            entry_id=str(entry.entry_id),
            user_id=user_id,
            sequence=int(latest_version.sequence) + 1 if latest_version is not None else 1,
            parent_version_id=(
                str(latest_version.version_id) if latest_version is not None else None
            ),
            sha256=str(entry.sha256 or "") or None,
            size_bytes=int(entry.size_bytes or 0),
            mime_type=entry.mime_type,
            actor="admin",
            context=None,
            checkpoint_kind="legacy_head",
        )
        next_plan = WorkspaceVersionSnapshotPlan(next_row, target_path)
        before_projection = self._journal_projection(entry)
        after_projection = dict(before_projection)
        after_projection.update({
            "current_version_id": next_version_id,
            "head_blob_id": next_row["blob_id"],
        })
        version_rows = [next_row]
        entry_id = str(entry.entry_id)
        before_revision = int(entry.revision)
        ancestor_ids = self._ancestor_entry_ids(user_id, entry.parent_id)
        claim = WorkspaceClaimSpec(
            "file",
            file_scope(entry_id),
            entry_id,
            conflict_scope_keys=self._tree_scope_keys(ancestor_ids),
        )
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace,
            workspace_user_id=user_id,
            entry_id=entry_id,
            actor="admin",
            operation="materialize_legacy_head",
            result_status="NO_CHANGE",
            idempotency_key=None,
            context=None,
            before_revision=before_revision,
            before_sha256=before_sha,
            after_revision=before_revision,
            after_sha256=entry.sha256,
            before_version_id=(
                str(before_projection.get("current_version_id") or "") or None
            ),
            after_version_id=next_version_id,
            journal={
                "target_path": target_path,
                "old_sha256": before_sha,
                "new_sha256": before_sha,
                "bytes_delta": 0,
                "entries_delta": 0,
                "tree_revision_entry_ids": [],
                "before_entry_projection": before_projection,
                "entry_projection": after_projection,
                "version_rows": version_rows,
            },
            claim_specs=(claim, *self._content_object_claim_specs(version_rows)),
        )
        try:
            async with self._guard_workspace_claims(store, prepared.leases):
                current = await store.stat(target_path)
                if (
                    current.source_revision != physical.source_revision
                    or current.sha256 != physical.sha256
                ):
                    raise WorkspaceError(
                        409,
                        "SOURCE_REVISION_CONFLICT",
                        "旧工作区实体文件在建立内部版本时发生变化",
                    )
                await self._snapshot_version(store, next_plan)
                self._register_prepared_content_object(user_id, next_plan.version_row)
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(409, "MUTATION_FENCED", "旧工作区版本迁移所有权已失效") from exc
        except WorkspaceError:
            # Reconciliation owns the prepared version row and can safely
            # finish it only while the physical SHA still matches.
            raise
        return self._record_mutation(
            workspace=workspace,
            entry=entry,
            actor="admin",
            operation="materialize_legacy_head",
            result_status="NO_CHANGE",
            idempotency_key=None,
            context=None,
            before_revision=before_revision,
            before_sha256=before_sha,
            details={"legacy_head_materialized": True},
            prepared_mutation=prepared,
        ).entry

    async def _repair_physical_head_projection(
        self,
        user_id: str,
        *,
        workspace: UserWorkspace,
        store: WorkspaceStore,
        entry: WorkspaceEntry,
        head: WorkspaceHeadContent,
        physical: SandboxFileStat | None,
    ) -> WorkspaceEntry:
        operation = "repair_physical_head"

        before_projection = self._journal_projection(entry)
        target_path = str(entry.relative_path)
        entry_id = str(entry.entry_id)
        before_revision = int(entry.revision)
        before_sha = str(entry.sha256 or "") or None
        ancestor_ids = self._ancestor_entry_ids(user_id, entry.parent_id)
        claim = WorkspaceClaimSpec(
            "file",
            file_scope(entry_id),
            entry_id,
            conflict_scope_keys=self._tree_scope_keys(ancestor_ids),
        )
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace,
            workspace_user_id=user_id,
            entry_id=entry_id,
            actor="admin",
            operation=operation,
            result_status="NO_CHANGE",
            idempotency_key=None,
            context=None,
            before_revision=before_revision,
            before_sha256=before_sha,
            after_revision=before_revision,
            after_sha256=before_sha,
            before_version_id=head.version_id,
            after_version_id=head.version_id,
            journal={
                "target_path": target_path,
                "old_sha256": physical.sha256 if physical is not None else None,
                "new_sha256": head.sha256,
                "bytes_delta": 0,
                "entries_delta": 0,
                "tree_revision_entry_ids": [],
                "before_entry_projection": before_projection,
                "entry_projection": before_projection,
                "version_rows": [],
            },
            claim_specs=(claim,),
        )
        installed = False
        try:
            async with self._guard_workspace_claims(store, prepared.leases):
                current: SandboxFileStat | None
                try:
                    current = await store.stat(target_path)
                except WorkspaceError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                    current = None
                if physical is None:
                    if current is not None:
                        raise WorkspaceError(
                            409,
                            "SOURCE_REVISION_CONFLICT",
                            "工作区实体文件在修复前再次变化",
                        )
                elif (
                    current is None
                    or current.source_revision != physical.source_revision
                    or current.sha256 != physical.sha256
                ):
                    raise WorkspaceError(
                        409,
                        "SOURCE_REVISION_CONFLICT",
                        "工作区实体文件在修复前再次变化",
                    )
                head_stat = await store.stat(head.content_path)
                if (
                    head_stat.sha256 != head.sha256
                    or int(head_stat.size_bytes) != head.size_bytes
                ):
                    raise WorkspaceError(409, "CURRENT_HEAD_INVALID", "工作区内部当前版本校验失败")
                restored = await store.copy_external_atomic(
                    source_root=store.workspace_root,
                    source_relative_path=head.content_path,
                    expected_source_revision=head_stat.source_revision,
                    destination_relative_path=target_path,
                    expected_destination_sha256=physical.sha256 if physical is not None else None,
                    must_not_exist=physical is None,
                    temp_token=f"{prepared.mutation_id}-repair",
                    allow_system_destination=False,
                )
                installed = True
                if restored.sha256 != head.sha256 or int(restored.size_bytes) != head.size_bytes:
                    raise WorkspaceError(409, "CURRENT_HEAD_INVALID", "工作区实体文件修复校验失败")
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(409, "MUTATION_FENCED", "工作区实体文件修复所有权已失效") from exc
        except WorkspaceError as exc:
            if not installed:
                self._fail_prepared_mutation(
                    prepared,
                    code=exc.code,
                    message=exc.message,
                    recoverable=True,
                    status_code=exc.status_code,
                    error_extra=exc.extra,
                )
            raise
        return self._record_mutation(
            workspace=workspace,
            entry=entry,
            actor="admin",
            operation=operation,
            result_status="NO_CHANGE",
            idempotency_key=None,
            context=None,
            before_revision=before_revision,
            before_sha256=before_sha,
            details={"physical_head_repaired": True},
            prepared_mutation=prepared,
        ).entry

    async def _absorb_physical_head(
        self,
        user_id: str,
        *,
        workspace: UserWorkspace,
        store: WorkspaceStore,
        entry: WorkspaceEntry,
        physical: SandboxFileStat,
    ) -> WorkspaceEntry:
        operation = "absorb_physical_head"
        if physical.sha256 is None:
            raise WorkspaceError(409, "CURRENT_HEAD_INVALID", "工作区实体文件缺少内容摘要")
        if int(physical.size_bytes) > int(self.settings.workspace_max_file_bytes):
            raise WorkspaceError(413, "FILE_TOO_LARGE", "工作区实体文件超过单文件大小限制")
        if not entry.current_version_id:
            raise WorkspaceError(409, "CURRENT_HEAD_UNAVAILABLE", "旧工作区文件缺少可合并的内部基线")

        before_projection = self._journal_projection(entry)
        target_path = str(entry.relative_path)
        entry_id = str(entry.entry_id)
        before_revision = int(entry.revision)
        before_sha = str(entry.sha256 or "") or None
        base_plan, next_plan = self._plan_file_version_update(
            entry,
            new_sha256=physical.sha256,
            new_size_bytes=int(physical.size_bytes),
            new_mime_type=entry.mime_type,
            actor="web",
            context=None,
            checkpoint_kind="web_external",
        )
        if base_plan is not None:
            raise WorkspaceError(409, "CURRENT_HEAD_UNAVAILABLE", "旧工作区文件缺少可合并的内部基线")
        after_projection = dict(before_projection)
        after_projection.update({
            "size_bytes": int(physical.size_bytes),
            "sha256": physical.sha256,
            "revision": before_revision + 1,
            "current_version_id": next_plan.version_row["version_id"],
            "head_blob_id": next_plan.version_row["blob_id"],
        })
        version_rows = [next_plan.version_row]
        ancestor_ids = self._ancestor_entry_ids(user_id, entry.parent_id)
        claim = WorkspaceClaimSpec(
            "file",
            file_scope(entry_id),
            entry_id,
            conflict_scope_keys=self._tree_scope_keys(ancestor_ids),
        )
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace,
            workspace_user_id=user_id,
            entry_id=entry_id,
            actor="web",
            operation=operation,
            result_status="UPDATED",
            idempotency_key=None,
            context=None,
            before_revision=before_revision,
            before_sha256=before_sha,
            after_revision=before_revision + 1,
            after_sha256=physical.sha256,
            before_version_id=str(before_projection.get("current_version_id") or "") or None,
            after_version_id=str(next_plan.version_row["version_id"]),
            journal={
                "target_path": target_path,
                "old_sha256": before_sha,
                "new_sha256": physical.sha256,
                "bytes_delta": int(physical.size_bytes) - int(entry.size_bytes or 0),
                "entries_delta": 0,
                "tree_revision_entry_ids": ancestor_ids,
                "before_entry_projection": before_projection,
                "entry_projection": after_projection,
                "version_rows": version_rows,
            },
            claim_specs=(claim, *self._content_object_claim_specs(version_rows)),
        )
        try:
            async with self._guard_workspace_claims(store, prepared.leases):
                current = await store.stat(target_path)
                if (
                    current.source_revision != physical.source_revision
                    or current.sha256 != physical.sha256
                ):
                    raise WorkspaceError(
                        409,
                        "SOURCE_REVISION_CONFLICT",
                        "工作区实体文件在保存内部版本前再次变化",
                    )
                await self._snapshot_version(store, next_plan)
                self._register_prepared_content_object(user_id, next_plan.version_row)
                after_snapshot = await store.stat(target_path)
                if (
                    after_snapshot.source_revision != physical.source_revision
                    or after_snapshot.sha256 != physical.sha256
                ):
                    raise WorkspaceError(
                        409,
                        "SOURCE_REVISION_CONFLICT",
                        "工作区实体文件在保存内部版本时再次变化",
                    )
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(409, "MUTATION_FENCED", "工作区实体文件保存所有权已失效") from exc
        except WorkspaceError:
            # The physical file already contains the candidate bytes. Keep the
            # prepared journal so reconciliation can either finalize that exact
            # snapshot or fence it if the file moved again.
            raise
        return self._record_mutation(
            workspace=workspace,
            entry=entry,
            actor="web",
            operation=operation,
            result_status="UPDATED",
            idempotency_key=None,
            context=None,
            before_revision=before_revision,
            before_sha256=before_sha,
            details={"physical_head_absorbed": True},
            prepared_mutation=prepared,
        ).entry

    async def _synchronize_physical_head(
        self,
        user_id: str,
        *,
        workspace: UserWorkspace,
        store: WorkspaceStore,
        entry: WorkspaceEntry,
    ) -> tuple[WorkspaceEntry, WorkspaceHeadContent]:
        entry_id = str(entry.entry_id)
        target_path = str(entry.relative_path)
        try:
            self.db.commit()
            physical = await store.stat(target_path)
        except WorkspaceError as exc:
            if exc.code != "NOT_FOUND":
                raise
            physical = None
        entry = self._entry(user_id, entry_id)
        try:
            head = self._resolve_entry_head_content(user_id, entry)
        except WorkspaceError as exc:
            if exc.code not in {"CURRENT_HEAD_UNAVAILABLE", "CURRENT_HEAD_INVALID"}:
                raise
            if physical is None:
                raise
            entry = await self._materialize_legacy_head(
                user_id,
                workspace=workspace,
                store=store,
                entry=entry,
                physical=physical,
            )
            head = self._resolve_entry_head_content(user_id, entry)
        if (
            physical is not None
            and physical.sha256 == head.sha256
            and int(physical.size_bytes) == head.size_bytes
        ):
            refreshed = self._entry(user_id, entry_id)
            return refreshed, self._resolve_entry_head_content(user_id, refreshed)

        known_version = None
        if physical is not None and physical.sha256 is not None:
            known_version = self.db.query(WorkspaceFileVersion.version_id).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.entry_id == entry_id,
                WorkspaceFileVersion.sha256 == physical.sha256,
                WorkspaceFileVersion.size_bytes == int(physical.size_bytes),
                WorkspaceFileVersion.state == "materialized",
            ).first()
        if physical is None or known_version is not None:
            synchronized = await self._repair_physical_head_projection(
                user_id,
                workspace=workspace,
                store=store,
                entry=entry,
                head=head,
                physical=physical,
            )
        else:
            synchronized = await self._absorb_physical_head(
                user_id,
                workspace=workspace,
                store=store,
                entry=entry,
                physical=physical,
            )
        refreshed = self._entry(user_id, str(synchronized.entry_id))
        return refreshed, self._resolve_entry_head_content(user_id, refreshed)

    def _finish_change_set_with_current(
        self,
        change_set_id: str,
        *,
        apply_lease: WorkspaceClaimLease,
        details: dict[str, Any],
        entry_id: str | None,
        current_version_id: str | None,
        reason: str,
        merge_result: AutoMergeResult | None = None,
    ) -> WorkspaceChangeSetResult:
        details["auto_merge"] = {
            "algorithm": "workspace-three-way-v1",
            "outcome": "current_wins",
            "reason": reason,
            "strategy": merge_result.strategy if merge_result else None,
            "applied_changes": merge_result.applied_changes if merge_result else 0,
            "preserved_conflicts": merge_result.preserved_conflicts if merge_result else None,
            "proposal_preserved": True,
        }
        row, _owned_details = self._lock_owned_applying_change_set(change_set_id, apply_lease)
        row.status = "applied"
        row.entry_id = entry_id
        row.applied_version_id = current_version_id
        row.applied_at = now_naive()
        row.details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        row.error_code = None
        row.error_message = None
        self.db.commit()
        return self._change_set_result(self.db.get(WorkspaceChangeSet, change_set_id))

    def _finish_change_set_applied(
        self,
        change_set_id: str,
        *,
        apply_lease: WorkspaceClaimLease,
        entry_id: str | None,
        applied_version_id: str | None,
        applied_at: datetime | None = None,
        details: dict[str, Any] | None = None,
    ) -> WorkspaceChangeSetResult:
        row, owned_details = self._lock_owned_applying_change_set(change_set_id, apply_lease)
        row.status = "applied"
        row.entry_id = entry_id
        row.applied_version_id = applied_version_id
        row.applied_at = applied_at or now_naive()
        row.error_code = None
        row.error_message = None
        if details is not None:
            row.details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        elif not isinstance(owned_details, dict):
            row.details_json = "{}"
        self.db.commit()
        return self._change_set_result(self.db.get(WorkspaceChangeSet, change_set_id))

    async def apply_change_set(
        self,
        user_id: str,
        change_set_id: str,
        *,
        expected_current_version_id: str | None = None,
    ) -> WorkspaceChangeSetResult:
        leases = tuple(self._acquire_claims(
            user_id=user_id,
            operation="apply_change_set",
            specs=(WorkspaceClaimSpec("path", f"change-set:{change_set_id}"),),
        ))
        try:
            async with keep_workspace_claims_alive(
                self._independent_db_session_factory(),
                leases,
                lease_seconds=int(self.settings.workspace_mutation_lease_seconds),
            ) as heartbeat:
                heartbeat.raise_if_lost()
                return await self._apply_change_set_owned(
                    user_id,
                    change_set_id,
                    expected_current_version_id=expected_current_version_id,
                    apply_lease=leases[0],
                    apply_heartbeat=heartbeat,
                )
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(
                409,
                "CHANGE_SET_APPLY_FENCED",
                "工作区修改提案发布所有权已失效",
            ) from exc
        finally:
            self._release_unattached_claims(leases)

    async def _apply_change_set_owned(
        self,
        user_id: str,
        change_set_id: str,
        *,
        expected_current_version_id: str | None = None,
        apply_lease: WorkspaceClaimLease,
        apply_heartbeat: Any,
    ) -> WorkspaceChangeSetResult:
        workspace, store = await self._prepare(user_id, for_update=True)
        row = self.db.query(WorkspaceChangeSet).populate_existing().filter(
            WorkspaceChangeSet.user_id == user_id,
            WorkspaceChangeSet.change_set_id == change_set_id,
        ).with_for_update().one_or_none()
        if row is None:
            raise WorkspaceError(404, "CHANGE_SET_NOT_FOUND", "工作区修改提案不存在")
        if row.status == "applied":
            self.db.rollback()
            return self._change_set_result(row)
        if row.status == "rejected":
            self.db.rollback()
            raise WorkspaceError(409, "CHANGE_SET_REJECTED", "工作区修改提案已拒绝")
        if row.status == "preparing":
            self.db.rollback()
            raise WorkspaceError(409, "CHANGE_SET_NOT_READY", "工作区修改提案仍在准备")
        if row.status not in {"proposed", "conflict", "needs_review", "applying"}:
            self.db.rollback()
            raise WorkspaceError(409, "CHANGE_SET_NOT_READY", "工作区修改提案状态不允许发布")
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError as exc:
            self.db.rollback()
            raise WorkspaceError(409, "CHANGE_SET_INVALID", "工作区修改提案记录无效") from exc
        if not isinstance(details, dict):
            self.db.rollback()
            raise WorkspaceError(409, "CHANGE_SET_INVALID", "工作区修改提案记录无效")
        details["apply_owner"] = {
            "owner_token": apply_lease.owner_token,
            "generation": int(apply_lease.generation),
        }
        details.setdefault("apply_started_at", now_naive().isoformat())
        row.status = "applying"
        row.details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        self.db.commit()
        apply_heartbeat.raise_if_lost()

        completed_mutation = self.db.query(WorkspaceMutation).filter(
            WorkspaceMutation.change_set_id == change_set_id,
            WorkspaceMutation.state == "completed",
        ).order_by(WorkspaceMutation.completed_at.desc()).first()
        if completed_mutation is not None:
            return self._finish_change_set_applied(
                change_set_id,
                apply_lease=apply_lease,
                entry_id=completed_mutation.entry_id,
                applied_version_id=completed_mutation.after_version_id,
                applied_at=completed_mutation.completed_at or now_naive(),
            )

        base_version = None
        if row.base_version_id:
            base_version = self.db.query(WorkspaceFileVersion).filter(
                WorkspaceFileVersion.user_id == user_id,
                WorkspaceFileVersion.version_id == row.base_version_id,
                WorkspaceFileVersion.state == "materialized",
            ).one_or_none()
            if base_version is not None and row.entry_id and str(base_version.entry_id) != str(row.entry_id):
                base_version = None
        if base_version is not None:
            target = self.db.query(WorkspaceEntry).filter(
                WorkspaceEntry.user_id == user_id,
                WorkspaceEntry.entry_id == base_version.entry_id,
                WorkspaceEntry.status == "active",
            ).one_or_none()
        else:
            target = self._sibling(
                user_id,
                details.get("destination_parent_id"),
                str(details.get("destination_name") or ""),
                for_update=False,
            )
        if target is not None and not details.get("target_existed"):
            return self._finish_change_set_with_current(
                change_set_id,
                apply_lease=apply_lease,
                details=details,
                entry_id=str(target.entry_id),
                current_version_id=(
                    str(target.current_version_id) if target.current_version_id else None
                ),
                reason="target_created_after_proposal",
            )

        proposal_path = str(details.get("proposal_path") or "")
        proposal_sha = str(details.get("source_sha256") or "") or None
        proposal_size = int(details.get("source_size_bytes") or 0)
        actor = str(row.actor)
        operation = str(row.operation)
        context = {
            "session_id": row.session_id,
            "round_id": row.round_id,
            "tool_call_id": row.tool_call_id,
            "cron_run_id": row.cron_run_id,
        }

        current_head: WorkspaceHeadContent | None = None
        if target is not None and target.kind == "file":
            try:
                target, current_head = await self._synchronize_physical_head(
                    user_id,
                    workspace=workspace,
                    store=store,
                    entry=target,
                )
            except WorkspaceError as exc:
                return self._finish_change_set_for_workspace_error(
                    change_set_id,
                    apply_lease=apply_lease,
                    details=details,
                    error=exc,
                    retry_message="工作区实体文件正在收敛，后台重新处理",
                )
            except Exception as exc:
                return self._defer_change_set_retry(
                    change_set_id,
                    apply_lease=apply_lease,
                    details=details,
                    code="HEAD_SYNCHRONIZATION_RETRY",
                    message=str(exc) or "工作区实体文件收敛中断，后台重试",
                )

        target_entry_id = str(target.entry_id) if target is not None else None
        current_version_id = str(target.current_version_id) if target is not None and target.current_version_id else None
        current_revision = int(target.revision) if target is not None else None
        if expected_current_version_id is not None and current_version_id != expected_current_version_id:
            details["head_advanced_during_freeze"] = {
                "expected_current_version_id": expected_current_version_id,
                "current_version_id": current_version_id,
            }

        mergeable = target is not None and posixpath.splitext(str(target.name))[1].lower() in {".md", ".markdown", ".txt", ".csv", ".xlsx"}
        base_unchanged = (
            base_version is not None
            and bool(base_version.content_path)
            and row.base_version_id == current_version_id
        )
        # 无分叉时直接发布冻结提案，避免三方合并器丢弃新增行或工作表。
        if mergeable and not base_unchanged:
            target_name = str(target.name)
            base_path = str(base_version.content_path) if base_version is not None and base_version.content_path else None
            base_sha = str(base_version.sha256 or "") if base_version is not None else None
            base_size = int(base_version.size_bytes or 0) if base_version is not None else None
            if row.base_version_id and (base_version is None or not base_path):
                return self._finish_change_set_failed(
                    change_set_id,
                    apply_lease=apply_lease,
                    details=details,
                    code="BASE_VERSION_UNAVAILABLE",
                    message="自动合并所需的历史基线已经不可用",
                )
            if current_head is None:
                return self._finish_change_set_failed(
                    change_set_id,
                    apply_lease=apply_lease,
                    details=details,
                    code="CURRENT_HEAD_UNAVAILABLE",
                    message="正式文件缺少可读取的内部当前版本",
                )
            self.db.commit()
            try:
                current_content = await self._read_verified_change_set_bytes(
                    store,
                    current_head.content_path,
                    expected_sha256=current_head.sha256,
                    expected_size=current_head.size_bytes,
                    allow_system=True,
                )
                proposal_content = await self._read_verified_change_set_bytes(
                    store,
                    proposal_path,
                    expected_sha256=proposal_sha,
                    expected_size=proposal_size,
                    allow_system=True,
                )
                if base_path and base_version is not None:
                    base_content = await self._read_verified_change_set_bytes(
                        store,
                        base_path,
                        expected_sha256=base_sha,
                        expected_size=base_size,
                        allow_system=True,
                    )
                else:
                    base_content = current_content
                merge_result = await asyncio.to_thread(
                    merge_workspace_bytes,
                    target_name,
                    base=base_content,
                    current=current_content,
                    proposal=proposal_content,
                )
            except WorkspaceError as exc:
                return self._finish_change_set_failed(
                    change_set_id,
                    apply_lease=apply_lease,
                    details=details,
                    code=exc.code,
                    message=exc.message,
                )
            if merge_result is None:
                return self._finish_change_set_with_current(
                    change_set_id,
                    apply_lease=apply_lease,
                    details=details,
                    entry_id=target_entry_id,
                    current_version_id=current_version_id,
                    reason="unsupported_merge_shape",
                )
            details["auto_merge"] = {
                "algorithm": "workspace-three-way-v1",
                "outcome": "merged" if merge_result.content != current_content else "current_wins",
                "strategy": merge_result.strategy,
                "applied_changes": merge_result.applied_changes,
                "preserved_conflicts": merge_result.preserved_conflicts,
                "proposal_preserved": True,
                "base_version_id": row.base_version_id,
                "current_version_id": current_version_id,
            }
            if merge_result.content == current_content:
                return self._finish_change_set_with_current(
                    change_set_id,
                    apply_lease=apply_lease,
                    details=details,
                    entry_id=target_entry_id,
                    current_version_id=current_version_id,
                    reason="all_overlaps_preserved_current",
                    merge_result=merge_result,
                )
            applying_row, _owned_details = self._lock_owned_applying_change_set(
                change_set_id,
                apply_lease,
            )
            applying_row.details_json = json.dumps(
                details,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self.db.commit()
            apply_heartbeat.raise_if_lost()
            try:
                mutation = await self.write_content(
                    user_id,
                    target_entry_id,
                    merge_result.content,
                    current_revision,
                    actor=actor,
                    context=context,
                    idempotency_key=f"workspace-change-set:{change_set_id}:merge",
                    _operation=operation,
                    _change_set_id=change_set_id,
                )
            except WorkspaceError as exc:
                return self._finish_change_set_for_workspace_error(
                    change_set_id,
                    apply_lease=apply_lease,
                    details=details,
                    error=exc,
                    retry_message="正式文件在合并写回阶段发生变化，后台重新合并",
                )
            except Exception as exc:
                return self._defer_change_set_retry(
                    change_set_id,
                    apply_lease=apply_lease,
                    details=details,
                    code="CHANGE_SET_APPLY_RETRY",
                    message=str(exc) or "合并写回中断，后台重试",
                )
            apply_heartbeat.raise_if_lost()
            return self._finish_change_set_applied(
                change_set_id,
                apply_lease=apply_lease,
                entry_id=str(mutation.entry.entry_id),
                applied_version_id=(
                    str(mutation.entry.current_version_id)
                    if mutation.entry.current_version_id
                    else None
                ),
                details=details,
            )

        if target is not None and base_version is not None and current_version_id != row.base_version_id:
            return self._finish_change_set_with_current(
                change_set_id,
                apply_lease=apply_lease,
                details=details,
                entry_id=target_entry_id,
                current_version_id=current_version_id,
                reason="read_only_format_current_wins",
            )
        if target is None and details.get("target_existed"):
            return self._finish_change_set_with_current(
                change_set_id,
                apply_lease=apply_lease,
                details=details,
                entry_id=None,
                current_version_id=None,
                reason="target_deleted",
            )

        apply_heartbeat.raise_if_lost()
        try:
            # The proposal already has immutable bytes and a durable content
            # reference. Publish that object directly; a second copy in the
            # execution root would escape Workspace deletion and history GC.
            frozen = await store.stat(proposal_path)
            if frozen.sha256 != proposal_sha or frozen.size_bytes != proposal_size:
                raise WorkspaceError(409, "CHANGE_SET_CONTENT_CHANGED", "工作区修改提案内容校验失败")
            mutation = await self._publish_external(
                user_id,
                source_root=store.workspace_root,
                source_relative_path=proposal_path,
                expected_source_revision=frozen.source_revision,
                destination_parent_id=target.parent_id if target is not None else details.get("destination_parent_id"),
                destination_name=target.name if target is not None else str(details.get("destination_name") or ""),
                conflict_policy="overwrite" if target is not None else "fail",
                expected_destination_revision=current_revision,
                idempotency_key=f"workspace-change-set:{change_set_id}:apply",
                actor=actor,
                context=context,
                operation=operation,
                change_set_id=change_set_id,
            )
        except WorkspaceError as exc:
            return self._finish_change_set_for_workspace_error(
                change_set_id,
                apply_lease=apply_lease,
                details=details,
                error=exc,
                retry_message="正式文件在发布写回阶段发生变化，后台重新处理",
            )
        except Exception as exc:
            return self._defer_change_set_retry(
                change_set_id,
                apply_lease=apply_lease,
                details=details,
                code="CHANGE_SET_APPLY_RETRY",
                message=str(exc) or "发布写回中断，后台重试",
            )
        apply_heartbeat.raise_if_lost()
        return self._finish_change_set_applied(
            change_set_id,
            apply_lease=apply_lease,
            entry_id=str(mutation.entry.entry_id),
            applied_version_id=(
                str(mutation.entry.current_version_id)
                if mutation.entry.current_version_id
                else None
            ),
            details=details,
        )

    async def _publish_external(
        self,
        user_id: str,
        *,
        source_root: str,
        source_relative_path: str,
        expected_source_revision: str,
        destination_parent_id: str | None,
        destination_name: str,
        conflict_policy: Literal["fail", "overwrite"],
        expected_destination_revision: int | str | None,
        idempotency_key: str | None,
        actor: str,
        context: dict[str, Any] | None,
        operation: str,
        change_set_id: str | None = None,
    ) -> WorkspaceMutationResult:
        if not _SOURCE_REVISION_RE.fullmatch(expected_source_revision):
            raise WorkspaceError(422, "INVALID_SOURCE_REVISION", "源文件版本无效")
        safe_name = WorkspacePathPolicy.validate_name(destination_name)
        workspace, store = await self._prepare(user_id, for_update=True)
        previous = self._idempotent_result(user_id, idempotency_key, operation)
        if previous:
            return previous
        source_stat = await store.stat_external(source_root, source_relative_path)
        if source_stat.source_revision != expected_source_revision:
            raise WorkspaceError(
                412,
                "SOURCE_REVISION_CONFLICT",
                "源文件已被其他操作修改，请刷新后重试",
                extra={"current_revision": source_stat.source_revision},
            )
        max_file_bytes = int(self.settings.workspace_max_file_bytes)
        if source_stat.size_bytes > max_file_bytes:
            raise WorkspaceError(413, "FILE_TOO_LARGE", "文件超过工作区单文件大小限制")
        parent = self._parent(user_id, destination_parent_id)
        target = self._sibling(
            user_id,
            destination_parent_id,
            safe_name,
            for_update=False,
        )
        if target and target.kind != "file":
            raise WorkspaceError(409, "NAME_CONFLICT", "目标名称已被文件夹占用", entry=target)
        if target and target.sha256 == source_stat.sha256:
            return self._record_mutation(
                workspace=workspace,
                entry=target,
                actor=actor,
                operation=operation,
                result_status="NO_CHANGE",
                idempotency_key=idempotency_key,
                context=context,
                before_revision=int(target.revision),
                before_sha256=target.sha256,
                details={"source_path": source_relative_path, "source_revision": expected_source_revision},
            )
        if target and conflict_policy == "fail":
            raise WorkspaceError(409, "NAME_CONFLICT", "目标名称已存在", entry=target)
        normalized_expected_destination = WorkspacePathPolicy.normalize_revision(expected_destination_revision)
        if target and normalized_expected_destination != int(target.revision):
            raise WorkspaceError(409, "REVISION_CONFLICT", "目标文件已被其他操作修改", entry=target)
        if not target and normalized_expected_destination is not None:
            raise WorkspaceError(409, "REVISION_CONFLICT", "目标文件已不存在")
        bytes_delta = source_stat.size_bytes - (int(target.size_bytes or 0) if target else 0)
        destination_path = WorkspacePathPolicy.join(parent.relative_path if parent else None, safe_name)
        mime_type, _ = mimetypes.guess_type(safe_name)
        if target:
            before_revision: int | None = int(target.revision)
            before_sha: str | None = target.sha256
            result_status = "UPDATED"
            before_projection = self._journal_projection(target)
            base_version_plan, next_version_plan = self._plan_file_version_update(
                target,
                new_sha256=str(source_stat.sha256),
                new_size_bytes=int(source_stat.size_bytes),
                new_mime_type=mime_type or "application/octet-stream",
                actor=actor,
                context=context,
            )
            projection = dict(before_projection)
            projection.update(
                {
                    "size_bytes": source_stat.size_bytes,
                    "sha256": source_stat.sha256,
                    "mime_type": mime_type or "application/octet-stream",
                    "revision": before_revision + 1,
                    "current_version_id": next_version_plan.version_row["version_id"],
                    "head_blob_id": next_version_plan.version_row["blob_id"],
                }
            )
            entry_id = target.entry_id
        else:
            before_revision = None
            before_sha = None
            result_status = "CREATED"
            entry_id = str(uuid.uuid4())
            planned_entry = WorkspaceEntry(
                entry_id=entry_id,
                user_id=user_id,
                parent_id=destination_parent_id,
                parent_key=destination_parent_id or _ROOT_PARENT_KEY,
                name=safe_name,
                kind="file",
                relative_path=destination_path,
                size_bytes=source_stat.size_bytes,
                mime_type=mime_type or "application/octet-stream",
                sha256=source_stat.sha256,
                revision=1,
                status="active",
            )
            next_version_plan = self._plan_initial_file_version(
                planned_entry,
                actor=actor,
                context=context,
            )
            base_version_plan = None
            projection = self._journal_projection(planned_entry)
            projection["current_version_id"] = next_version_plan.version_row["version_id"]
            projection["head_blob_id"] = next_version_plan.version_row["blob_id"]
            before_projection = None
        version_rows = [
            *([base_version_plan.version_row] if base_version_plan else []),
            next_version_plan.version_row,
        ]
        creating = target is None
        parent_projection = self._journal_projection(parent) if parent else None
        destination_ancestor_ids = self._ancestor_entry_ids(user_id, destination_parent_id)
        if target is not None:
            target_claim = WorkspaceClaimSpec(
                "file",
                file_scope(str(entry_id)),
                str(entry_id),
                conflict_scope_keys=self._tree_scope_keys(destination_ancestor_ids),
            )
        else:
            target_claim = WorkspaceClaimSpec(
                "path",
                path_scope(destination_parent_id, safe_name),
                destination_parent_id,
                conflict_scope_keys=self._tree_scope_keys(destination_ancestor_ids),
            )
        entry_for_result = target if target is not None else planned_entry
        self.db.rollback()
        prepared = self._begin_prepared_mutation(
            workspace=workspace,
            workspace_user_id=user_id,
            entry_id=entry_id,
            actor=actor,
            operation=operation,
            result_status=result_status,
            idempotency_key=idempotency_key,
            context=context,
            before_revision=before_revision,
            before_sha256=before_sha,
            after_revision=int(projection["revision"]),
            after_sha256=source_stat.sha256,
            before_version_id=(
                str(before_projection.get("current_version_id"))
                if before_projection and before_projection.get("current_version_id")
                else None
            ),
            after_version_id=str(next_version_plan.version_row["version_id"]),
            change_set_id=change_set_id,
            journal={
                "target_path": destination_path,
                "old_sha256": before_sha,
                "new_sha256": source_stat.sha256,
                "bytes_delta": bytes_delta,
                "entries_delta": 1 if creating else 0,
                "tree_revision_entry_ids": destination_ancestor_ids,
                "create_entry": creating,
                "before_entry_projection": before_projection,
                "entry_projection": projection,
                "version_rows": version_rows,
                "base_entry_projections": [parent_projection] if parent_projection else [],
                "destination_expectation": {
                    "parent_key": destination_parent_id or _ROOT_PARENT_KEY,
                    "name": safe_name,
                    "exclude_entry_id": None if creating else str(entry_id),
                },
            },
            claim_specs=(target_claim,),
        )
        installed_to_workspace = False
        try:
            async with self._guard_workspace_claims(store, prepared.leases):
                if base_version_plan is not None:
                    await self._snapshot_version(store, base_version_plan)
                staged_publish_path = (
                    f"{WORKSPACE_TEMP_DIRECTORY}/{prepared.mutation_id}.publish-staged"
                )
                await store.copy_external_atomic(
                    source_root=source_root,
                    source_relative_path=source_relative_path,
                    expected_source_revision=expected_source_revision,
                    destination_relative_path=staged_publish_path,
                    expected_destination_sha256=None,
                    must_not_exist=True,
                    temp_token=f"{prepared.mutation_id}-copy",
                    allow_system_destination=True,
                )
                WorkspaceMutationCoordinator(self.db).renew_claims(
                    prepared.leases,
                    lease_seconds=int(self.settings.workspace_mutation_lease_seconds),
                )
                installed = await store.install_staged_file(
                    staged_relative_path=staged_publish_path,
                    destination_relative_path=destination_path,
                    expected_destination_sha256=before_sha,
                    must_not_exist=creating,
                )
                installed_to_workspace = True
                await self._snapshot_version(store, next_version_plan)
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(
                409,
                "MUTATION_FENCED",
                "发布操作所有权已失效，请刷新后重试",
            ) from exc
        except WorkspaceError as exc:
            if not installed_to_workspace and exc.code in {
                "NAME_CONFLICT", "DESTINATION_CHANGED", "NOT_FOUND", "SYMLINK_REJECTED", "NOT_FILE", "SOURCE_REVISION_CONFLICT"
            }:
                self._fail_prepared_mutation(
                    prepared,
                    code=exc.code,
                    message=exc.message,
                    recoverable=True,
                    status_code=exc.status_code,
                    error_extra=exc.extra,
                )
            raise
        return self._record_mutation(
            workspace=workspace,
            entry=entry_for_result,
            actor=actor,
            operation=operation,
            result_status=result_status,
            idempotency_key=idempotency_key,
            context=context,
            before_revision=before_revision,
            before_sha256=before_sha,
            details={
                "source_path": source_relative_path,
                "source_revision": expected_source_revision,
                "size_bytes": installed.size_bytes,
            },
            prepared_mutation=prepared,
        )

    async def stage_entry(
        self,
        user_id: str,
        entry_id: str,
        *,
        expected_revision: int | str | None,
        version_id: str | None = None,
        expected_tree_revision: int | str | None = None,
        destination_root: str,
        destination_relative_path: str | None = None,
        snapshot_id: str | None = None,
    ) -> WorkspaceStageResult:
        _workspace, store = await self._prepare(user_id, for_update=False)
        entry = self._entry(user_id, entry_id)
        if entry.kind == "directory" and version_id is not None:
            raise WorkspaceError(400, "DIRECTORY_VERSION_UNSUPPORTED", "工作区文件夹不支持历史版本引用")
        expected = WorkspacePathPolicy.normalize_revision(expected_revision)
        expected_tree = WorkspacePathPolicy.normalize_revision(expected_tree_revision)
        if version_id is None and expected is not None and int(entry.revision) != expected:
            raise WorkspaceError(409, "REVISION_CONFLICT", "工作区条目已被修改", entry=entry)
        if (
            entry.kind == "directory"
            and expected_tree is not None
            and int(entry.tree_revision or 1) != expected_tree
        ):
            raise WorkspaceError(409, "REVISION_CONFLICT", "工作区文件夹内容已被修改", entry=entry)
        mount_path = posixpath.normpath(self.sandbox_service.get_mount_path(user_id))
        normalized_root = posixpath.normpath(destination_root)
        if normalized_root == mount_path or not normalized_root.startswith(mount_path + "/"):
            raise WorkspaceError(403, "DESTINATION_OUTSIDE_USER_MOUNT", "引用目标不在用户沙箱挂载目录内")
        root_relative = normalized_root[len(mount_path) + 1:]
        if not root_relative.startswith((_SESSION_SOURCE_PREFIX, _CRON_SOURCE_PREFIX)):
            raise WorkspaceError(403, "DESTINATION_SCOPE_DENIED", "只能引用到 Session 或 Cron 运行目录")
        if destination_relative_path is None:
            if snapshot_id is not None and not re.fullmatch(r"[0-9a-f]{32}", snapshot_id):
                raise WorkspaceError(422, "INVALID_SNAPSHOT_ID", "附件快照标识无效")
            snapshot_version = snapshot_id or (
                str(version_id or entry.current_version_id or int(entry.revision))
                if entry.kind == "file"
                else f"{int(entry.revision)}-{uuid.uuid4().hex}"
            )
            destination_relative_path = f".workspace-snapshots/{entry.entry_id}/{snapshot_version}/{entry.name}"
        safe_destination = WorkspacePathPolicy.normalize_external_relative_path(destination_relative_path)
        if entry.kind == "file":
            selected_version_id = version_id or entry.current_version_id
            selected_version = None
            if selected_version_id:
                selected_version = self.db.query(WorkspaceFileVersion).filter(
                    WorkspaceFileVersion.user_id == user_id,
                    WorkspaceFileVersion.entry_id == entry_id,
                    WorkspaceFileVersion.version_id == selected_version_id,
                    WorkspaceFileVersion.state == "materialized",
                ).one_or_none()
                if selected_version is None or not selected_version.content_path:
                    raise WorkspaceError(404, "VERSION_NOT_FOUND", "文件版本不存在")
            source_relative_path = (
                str(selected_version.content_path)
                if selected_version is not None
                else str(entry.relative_path)
            )
            source_sha256 = (
                selected_version.sha256
                if selected_version is not None
                else entry.sha256
            )
            selected_blob_id = (
                str(selected_version.blob_id)
                if selected_version is not None and selected_version.blob_id
                else None
            )
            if entry in self.db:
                self.db.expunge(entry)
            if selected_blob_id:
                self.db.rollback()
                self._upsert_content_reference(
                    user_id=user_id,
                    blob_id=selected_blob_id,
                    version_id=str(selected_version_id),
                    reference_kind="stage_snapshot",
                    reference_key=f"{normalized_root}:{safe_destination}",
                    retained_until=now_naive() + timedelta(hours=1),
                )
                content_object = self.db.query(WorkspaceContentObject).filter(
                    WorkspaceContentObject.user_id == user_id,
                    WorkspaceContentObject.blob_id == selected_blob_id,
                ).with_for_update().one_or_none()
                if content_object is None or content_object.state != "materialized":
                    self.db.rollback()
                    raise WorkspaceError(409, "CONTENT_OBJECT_PRUNING", "文件版本正在回收，请重试")
                self.db.commit()
            else:
                self.db.commit()
            copied = await store.copy_to_external_atomic(
                source_relative_path=source_relative_path,
                expected_source_sha256=source_sha256,
                destination_root=normalized_root,
                destination_relative_path=safe_destination,
            )
            return WorkspaceStageResult(
                entry=entry,
                destination_path=posixpath.join(normalized_root, safe_destination),
                destination_relative_path=safe_destination,
                source_revision=int(entry.revision),
                sha256=copied.sha256,
                size_bytes=copied.size_bytes,
                version_id=str(selected_version_id) if selected_version_id else None,
                version_sequence=(
                    int(selected_version.sequence)
                    if selected_version is not None
                    else None
                ),
            )

        descendants = sorted(
            self._descendants_by_path_prefix(
                user_id,
                entry.relative_path,
                status="active",
                for_update=False,
            ),
            key=lambda item: (item.relative_path.count("/"), item.relative_path),
        )
        root_projection = self._journal_projection(entry)
        descendant_projections = [self._journal_projection(item) for item in descendants]
        ancestor_ids = self._ancestor_entry_ids(user_id, entry.parent_id)
        subtree_ids = tuple(
            [entry_id, *(str(item.entry_id) for item in descendants)]
        )
        self.db.rollback()
        leases = tuple(self._acquire_claims(
            user_id=user_id,
            operation="stage_directory",
            specs=(WorkspaceClaimSpec(
                "tree",
                tree_scope(entry_id),
                entry_id,
                conflict_scope_keys=self._tree_scope_keys(ancestor_ids),
                conflict_entry_ids=subtree_ids,
            ),),
        ))
        frozen_projections = [root_projection, *descendant_projections]
        destination_parent, _destination_name = posixpath.split(safe_destination)
        incoming_name = f".incoming-{uuid.uuid4().hex}"
        incoming_path = posixpath.join(destination_parent, incoming_name)
        incoming_created = False
        incoming_installed = False
        manifest: list[dict[str, Any]] = []
        total_size = 0

        def assert_frozen_projections() -> None:
            for projection in sorted(
                frozen_projections,
                key=lambda item: str(item.get("entry_id") or ""),
            ):
                current = self.db.query(WorkspaceEntry.entry_id).filter(
                    WorkspaceEntry.user_id == user_id,
                    WorkspaceEntry.entry_id == str(projection.get("entry_id") or ""),
                    WorkspaceEntry.revision == int(projection.get("revision") or 0),
                    WorkspaceEntry.relative_path == projection.get("relative_path"),
                    WorkspaceEntry.status == projection.get("status"),
                    WorkspaceEntry.sha256 == projection.get("sha256"),
                    WorkspaceEntry.tree_revision == int(projection.get("tree_revision") or 1),
                ).one_or_none()
                if current is None:
                    raise WorkspaceError(409, "REVISION_CONFLICT", "工作区文件夹已被修改")
            self.db.commit()

        try:
            assert_frozen_projections()
            root_path = str(root_projection["relative_path"])
            prefix_length = len(root_path) + 1
            async with self._guard_workspace_claims(store, leases) as heartbeat:
                heartbeat.raise_if_lost()
                source_state = await store.inspect_path(root_path)
                if source_state.kind != "directory":
                    raise WorkspaceError(409, "SOURCE_TYPE_CHANGED", "工作区文件夹状态已改变")
                await store.ensure_external_directory(
                    destination_root=normalized_root,
                    destination_relative_path=incoming_path,
                    must_not_exist=True,
                )
                incoming_created = True
                heartbeat.raise_if_lost()
                expected_physical_manifest: list[dict[str, Any]] = []
                for projection in descendant_projections:
                    relative_path = str(projection["relative_path"])
                    relative_child_path = WorkspacePathPolicy.normalize_external_relative_path(
                        relative_path[prefix_length:]
                    )
                    destination_child_path = posixpath.join(incoming_path, relative_child_path)
                    if projection.get("kind") == "directory":
                        child_state = await store.inspect_path(relative_path)
                        if child_state.kind != "directory":
                            raise WorkspaceError(
                                409,
                                "SOURCE_TYPE_CHANGED",
                                "工作区文件夹状态已改变",
                            )
                        await store.ensure_external_directory(
                            destination_root=normalized_root,
                            destination_relative_path=destination_child_path,
                        )
                        heartbeat.raise_if_lost()
                        manifest.append({
                            "path": relative_child_path,
                            "kind": "directory",
                            "revision": int(projection["revision"]),
                        })
                        expected_physical_manifest.append({
                            "path": relative_child_path,
                            "kind": "directory",
                        })
                        continue
                    copied = await store.copy_to_external_atomic(
                        source_relative_path=relative_path,
                        expected_source_sha256=projection.get("sha256"),
                        destination_root=normalized_root,
                        destination_relative_path=destination_child_path,
                    )
                    if (
                        copied.sha256 != projection.get("sha256")
                        or int(copied.size_bytes) != int(projection.get("size_bytes") or 0)
                    ):
                        raise WorkspaceError(409, "SOURCE_REVISION_CONFLICT", "工作区文件夹内容已变化")
                    heartbeat.raise_if_lost()
                    total_size += int(copied.size_bytes)
                    manifest.append({
                        "path": relative_child_path,
                        "kind": "file",
                        "revision": int(projection["revision"]),
                        "sha256": copied.sha256,
                        "size_bytes": int(copied.size_bytes),
                    })
                    expected_physical_manifest.append({
                        "path": relative_child_path,
                        "kind": "file",
                        "sha256": copied.sha256,
                        "size_bytes": int(copied.size_bytes),
                    })
                assert_frozen_projections()
                heartbeat.raise_if_lost()
                await _complete_snapshot_before_cancellation(
                    store.install_external_directory_atomic(
                        destination_root=normalized_root,
                        staged_relative_path=incoming_path,
                        destination_relative_path=safe_destination,
                        expected_manifest=expected_physical_manifest,
                    )
                )
                incoming_installed = True
        except WorkspaceClaimLost as exc:
            raise WorkspaceError(
                409,
                "MUTATION_FENCED",
                "文件夹快照所有权已失效，请重试",
            ) from exc
        finally:
            if incoming_created and not incoming_installed:
                try:
                    await store.cleanup_external_incoming_directory(
                        destination_root=normalized_root,
                        incoming_relative_path=incoming_path,
                    )
                except Exception:
                    logger.warning(
                        "文件夹 stage incoming 清理失败 root=%s path=%s",
                        normalized_root,
                        incoming_path,
                        exc_info=True,
                    )
            self._release_unattached_claims(leases)
        manifest_sha256 = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        refreshed_entry = self._entry(user_id, entry_id)
        return WorkspaceStageResult(
            entry=refreshed_entry,
            destination_path=posixpath.join(normalized_root, safe_destination),
            destination_relative_path=safe_destination,
            source_revision=int(root_projection["revision"]),
            sha256=manifest_sha256,
            size_bytes=total_size,
            tree_revision=int(
                root_projection.get("tree_revision")
                or root_projection["revision"]
            ),
        )


def ensure_workspace_profile_switch_allowed(
    db: DBSession,
    *,
    user_id: str,
    desired_profile_id: str | None,
) -> None:
    """Enter a short drain state before a destructive Profile reassignment."""
    coordinator = WorkspaceMutationCoordinator(db)
    try:
        coordinator.begin_workspace_drain(user_id)
    except WorkspaceClaimConflict as exc:
        raise WorkspaceError(
            409,
            "WORKSPACE_MUTATION_IN_PROGRESS",
            "工作区仍有文件操作正在进行，暂时不能切换 Sandbox Profile",
            extra={"conflicting_scopes": list(exc.scope_keys)},
        ) from exc
    except WorkspaceDraining as exc:
        raise WorkspaceError(
            409,
            "WORKSPACE_DRAINING",
            str(exc),
        ) from exc
    workspace = db.query(UserWorkspace).filter(UserWorkspace.user_id == user_id).one()
    if int(workspace.entry_count or 0) == 0:
        return
    if desired_profile_id is None:
        from src.api.services.sandbox_profile_service import get_existing_default_sandbox_profile

        default_profile = get_existing_default_sandbox_profile(db)
        effective_desired_id = default_profile.id if default_profile else None
    else:
        effective_desired_id = desired_profile_id
    # The current admin rebuild path kills the container *and clears the whole
    # mount*.  Therefore even a same-profile force rebuild/version refresh is
    # destructive for workdir and must be blocked until that lifecycle path is
    # changed to preserve storage.
    extra = {
        "entry_count": int(workspace.entry_count or 0),
        "used_bytes": int(workspace.used_bytes or 0),
        "active_profile_id": workspace.active_profile_id,
        "desired_profile_id": effective_desired_id,
    }
    coordinator.finish_workspace_drain(user_id)
    raise WorkspaceError(
        409,
        "WORKSPACE_PROFILE_SWITCH_BLOCKED",
        "工作区包含持久文件；迁移或清空工作区后才能重建或切换 Sandbox Profile",
        extra=extra,
    )


def finish_workspace_profile_switch(db: DBSession, *, user_id: str) -> None:
    WorkspaceMutationCoordinator(db).finish_workspace_drain(user_id)


def ensure_workspace_profile_runtime_update_allowed(
    db: DBSession,
    *,
    profile_id: str,
) -> None:
    affected = db.query(UserWorkspace).filter(
        UserWorkspace.active_profile_id == profile_id,
        UserWorkspace.entry_count > 0,
    ).count()
    if affected:
        raise WorkspaceError(
            409,
            "WORKSPACE_PROFILE_UPDATE_BLOCKED",
            "该 Sandbox Profile 仍承载非空工作区，不能修改运行时连接配置",
            extra={"affected_workspaces": int(affected)},
        )


def ensure_default_workspace_profile_switch_allowed(
    db: DBSession,
    *,
    desired_profile_id: str,
) -> None:
    from src.api.models.user_sandbox_config import UserSandboxConfig

    explicitly_bound_users = db.query(UserSandboxConfig.user_id).filter(
        UserSandboxConfig.sandbox_profile_id.isnot(None)
    )
    affected = db.query(UserWorkspace).filter(
        UserWorkspace.entry_count > 0,
        UserWorkspace.active_profile_id != desired_profile_id,
        ~UserWorkspace.user_id.in_(explicitly_bound_users),
    ).count()
    if affected:
        raise WorkspaceError(
            409,
            "WORKSPACE_DEFAULT_PROFILE_SWITCH_BLOCKED",
            "仍有使用默认 Profile 的非空工作区，不能切换全局默认 Sandbox Profile",
            extra={"affected_workspaces": int(affected)},
        )


async def reconcile_workspace_mutations(
    db: DBSession,
    *,
    sandbox_service: Any | None = None,
    force: bool = False,
) -> int:
    """Reconcile expired prepared mutations from a previous process/crash."""
    query = db.query(WorkspaceMutation.user_id).filter(
        WorkspaceMutation.state == "prepared"
    )
    if not force:
        query = query.filter(
            or_(
                WorkspaceMutation.lease_expires_at.is_(None),
                WorkspaceMutation.lease_expires_at <= now_naive(),
            )
        )
    prepared_user_ids = {str(row[0]) for row in query.distinct().all() if row[0]}
    db.commit()
    reconciled = 0
    for user_id in sorted(prepared_user_ids):
        try:
            service = WorkspaceService(
                db,
                sandbox_service=sandbox_service,
            )
            if user_id in prepared_user_ids:
                reconciled += await service.reconcile_prepared_mutations(
                    user_id,
                    force=force,
                )
        except Exception:
            db.rollback()
            # Leave the prepared row durable for the next startup/first write.
            logger.warning(
                "工作区 prepared mutation 对账失败，保留到下次重试 user=%s",
                user_id,
                exc_info=True,
            )
            continue
    return reconciled


__all__ = [
    "SandboxFileStat",
    "WorkspaceContent",
    "WorkspaceChangeSetContent",
    "WorkspaceChangeSetResult",
    "WorkspaceEntryPage",
    "WorkspaceError",
    "WorkspaceHistoryGcResult",
    "WorkspaceMutationResult",
    "WorkspacePathState",
    "WorkspacePathPolicy",
    "WorkspaceService",
    "WorkspaceStageResult",
    "WorkspaceStore",
    "WorkspaceDeleteResult",
    "WorkspaceVersionContent",
    "ensure_workspace_profile_switch_allowed",
    "finish_workspace_profile_switch",
    "ensure_workspace_profile_runtime_update_allowed",
    "ensure_default_workspace_profile_switch_allowed",
    "reconcile_workspace_mutations",
]
