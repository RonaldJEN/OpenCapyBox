"""Controlled tools for the persistent per-user workspace.

The model never receives the physical ``/home/user/workdir`` path. Reads are
staged into the current Session/Cron execution root; mutations are delegated to
WorkspaceService so web, chat, and cron share revision and audit semantics.
"""

from __future__ import annotations

import json
import logging
import posixpath
from typing import Any, Callable

from .base import Tool, ToolResult, ToolRuntimeContext

logger = logging.getLogger(__name__)


def _entry_dict(entry: Any) -> dict[str, Any]:
    return {
        "entry_id": str(entry.entry_id),
        "parent_id": entry.parent_id,
        "name": str(entry.name),
        "kind": str(entry.kind),
        "path": str(entry.relative_path),
        "size_bytes": int(entry.size_bytes or 0),
        "mime_type": entry.mime_type,
        "sha256": entry.sha256,
        "revision": int(entry.revision or 0),
        "current_version_id": getattr(entry, "current_version_id", None),
        "tree_revision": int(getattr(entry, "tree_revision", 1) or 1),
        "status": str(entry.status),
    }


class _WorkspaceToolBase(Tool):
    def __init__(
        self,
        *,
        db_session_factory: Callable,
        user_id: str,
        execution_root: str,
        sandbox: Any | None = None,
        actor: str = "chat",
        fence: Callable[[Any], None] | None = None,
        change_recorder: Callable[[Any, dict[str, Any]], None] | None = None,
        base_context: dict[str, Any] | None = None,
    ) -> None:
        self._db_factory = db_session_factory
        self._user_id = user_id
        self._execution_root = posixpath.normpath(execution_root)
        self._sandbox = sandbox
        self._actor = actor
        self._fence = fence
        self._change_recorder = change_recorder
        self._base_context = dict(base_context or {})
        self._runtime_context: ToolRuntimeContext | None = None

    def set_runtime_context(self, context: ToolRuntimeContext) -> None:
        self._runtime_context = context

    def clear_runtime_context(self) -> None:
        self._runtime_context = None

    def _mutation_context(self) -> dict[str, str]:
        runtime = self._runtime_context
        context = dict(self._base_context)
        if runtime is None:
            return context
        context.update({
            "round_id": runtime.run_id,
            "tool_call_id": runtime.tool_call_id,
        })
        if self._actor == "cron":
            context["cron_run_id"] = runtime.thread_id
        else:
            context["session_id"] = runtime.thread_id
        return context

    def _idempotency_key(self) -> str | None:
        runtime = self._runtime_context
        if runtime is None:
            return None
        return f"workspace-tool:{runtime.thread_id}:{runtime.run_id}:{runtime.tool_call_id}"

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        from src.api.services.workspace_service import WorkspaceService

        db = self._db_factory()
        try:
            if self._fence is not None:
                self._fence(db)
            service = WorkspaceService(db, sandbox=self._sandbox)
            return await getattr(service, method)(*args, **kwargs)
        finally:
            db.close()

    @staticmethod
    def _failure(exc: Exception) -> ToolResult:
        code = getattr(exc, "code", type(exc).__name__)
        message = getattr(exc, "message", str(exc))
        return ToolResult(success=False, error=f"{code}: {message}")

    def _record_change(self, change: dict[str, Any]) -> None:
        if self._change_recorder is None:
            return
        db = self._db_factory()
        try:
            self._change_recorder(db, change)
        except Exception:
            # The WorkspaceMutation itself is already committed. Keep the
            # model-visible success truthful; Cron reconciliation can recover
            # its summary from the durable mutation journal.
            logger.warning("记录 Cron 工作区变更摘要失败，将由 reconciliation 补齐", exc_info=True)
        finally:
            db.close()

    def _mutation_result(self, result: Any) -> ToolResult:
        entry = _entry_dict(result.entry)
        change = {
            **entry,
            "operation": str(result.status),
            "mutation_id": str(result.mutation_id),
        }
        self._record_change(change)
        return ToolResult(
            success=True,
            content=(
                f"{result.status} 工作区/{entry['path']} | "
                f"entry_id={entry['entry_id']} | revision={entry['revision']}"
            ),
            resource_changes=[change],
        )

    def _change_set_result(self, result: Any) -> ToolResult:
        if not getattr(result, "change_set_id", None):
            return self._mutation_result(result)
        status = str(result.status).upper()
        if status == "APPLIED" and result.entry is not None and result.mutation_id:
            mutation_result = type("WorkspaceMutationView", (), {
                "status": getattr(result, "mutation_status", None) or "UPDATED",
                "entry": result.entry,
                "mutation_id": result.mutation_id,
            })()
            return self._mutation_result(mutation_result)
        if status == "FAILED":
            code = str(getattr(result, "error_code", None) or "CHANGE_SET_FAILED")
            message = str(getattr(result, "error_message", None) or "工作区发布失败")
            return ToolResult(
                success=False,
                error=(
                    f"{code}: {message}；正式文件和本次修改都已保留，"
                    "不要删除或重建目标文件"
                ),
            )
        if status == "APPLIED":
            message = "系统已自动收敛；正式文件保留人的当前内容，无需任何操作"
        elif status == "CONFLICT":
            message = "系统已保存本次修改并在后台重新合并；不要删除或重建目标文件"
        else:
            message = (
                "本次发布未改动正式文件，正式内容和本次修改都已保留；"
                "不要删除或重建目标文件"
            )
        return ToolResult(
            success=True,
            content=(
                f"{status} 工作区修改提案 | change_set_id={result.change_set_id} | "
                f"{message}"
            ),
        )


class WorkspaceListTool(_WorkspaceToolBase):
    repeat_policy = "read_only"

    @property
    def name(self) -> str:
        return "workspace_list"

    @property
    def description(self) -> str:
        return (
            "List or search files in the user's persistent workspace. Returns stable entry_id, "
            "relative path, type, size, and revision for later workspace calls. "
            "If next_cursor is non-null and more results are needed, pass it as cursor "
            "with the same parent_id, query, and limit to fetch the next page."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "parent_id": {"type": "string", "description": "Directory entry_id; omit for root"},
                "query": {"type": "string", "description": "Optional filename/path search"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                "cursor": {"type": "string", "description": "Opaque next_cursor from the previous page; omit for the first page"},
            },
        }

    async def execute(
        self,
        parent_id: str | None = None,
        query: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ToolResult:
        try:
            page = await self._call(
                "list_entries",
                self._user_id,
                parent_id=parent_id or None,
                q=(query or "").strip() or None,
                limit=limit,
                cursor=cursor,
            )
            payload = {
                "workspace_revision": int(page.workspace_revision),
                "items": [_entry_dict(entry) for entry in page.items],
                "next_cursor": page.next_cursor,
            }
            return ToolResult(success=True, content=json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            return self._failure(exc)


class WorkspaceStageTool(_WorkspaceToolBase):
    repeat_policy = "read_only"

    @property
    def name(self) -> str:
        return "workspace_stage"

    @property
    def description(self) -> str:
        return (
            "Copy a workspace file or directory into the current execution Workspace. "
            "Omit revision to stage the current head; a stale observed revision automatically restages "
            "the latest head once so concurrent human edits do not block the Agent. Returns snapshot_path "
            "for file/bash tools and publish_source_path for workspace_publish."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "revision": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "version_id": {"type": "string", "description": "Optional immutable file version ID"},
                "tree_revision": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "destination_path": {
                    "type": "string",
                    "description": "Optional path relative to the current execution Workspace",
                },
            },
            "required": ["entry_id"],
        }

    async def execute(
        self,
        entry_id: str,
        revision: int | str | None = None,
        version_id: str | None = None,
        tree_revision: int | str | None = None,
        destination_path: str | None = None,
    ) -> ToolResult:
        try:
            normalized_version_id = (version_id or "").strip() or None
            normalized_destination = (destination_path or "").strip() or None
            try:
                staged = await self._call(
                    "stage_entry",
                    self._user_id,
                    entry_id,
                    expected_revision=revision,
                    version_id=normalized_version_id,
                    expected_tree_revision=tree_revision,
                    destination_root=self._execution_root,
                    destination_relative_path=normalized_destination,
                )
            except Exception as exc:
                if getattr(exc, "code", None) != "REVISION_CONFLICT" or normalized_version_id:
                    raise
                staged = await self._call(
                    "stage_entry",
                    self._user_id,
                    entry_id,
                    expected_revision=None,
                    version_id=None,
                    expected_tree_revision=None,
                    destination_root=self._execution_root,
                    destination_relative_path=normalized_destination,
                )
            entry = _entry_dict(staged.entry)
            payload = {
                "status": "STAGED",
                "workspace_path": entry["path"],
                "snapshot_path": staged.destination_path,
                "publish_source_path": staged.destination_relative_path,
                "entry_id": entry["entry_id"],
                "revision": staged.source_revision,
                "version_id": getattr(staged, "version_id", None),
                "base_version_id": getattr(staged, "version_id", None),
                "tree_revision": (
                    staged.tree_revision
                    if entry["kind"] == "directory"
                    else None
                ),
                "sha256": staged.sha256,
                "manifest_sha256": staged.sha256 if entry["kind"] == "directory" else None,
            }
            return ToolResult(
                success=True,
                content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception as exc:
            return self._failure(exc)


class WorkspacePublishTool(_WorkspaceToolBase):
    repeat_policy = "mutating"

    @property
    def name(self) -> str:
        return "workspace_publish"

    @property
    def description(self) -> str:
        return (
            "Publish a regular file from the current execution Workspace into the persistent "
            "workspace. Existing files require conflict_policy=overwrite plus the last observed "
            "destination revision/base version. The service preserves human edits and merges in "
            "the background; never delete or recreate a target to resolve a publish conflict."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path relative to the current execution Workspace"},
                "destination_parent_id": {"type": "string", "description": "Target directory entry_id; omit for root"},
                "destination_name": {"type": "string"},
                "conflict_policy": {"type": "string", "enum": ["fail", "overwrite"], "default": "fail"},
                "expected_destination_revision": {
                    "anyOf": [{"type": "integer"}, {"type": "string"}],
                },
                "base_version_id": {"type": "string"},
            },
            "required": ["source_path", "destination_name"],
        }

    async def execute(
        self,
        source_path: str,
        destination_name: str,
        destination_parent_id: str | None = None,
        conflict_policy: str = "fail",
        expected_destination_revision: int | str | None = None,
        base_version_id: str | None = None,
    ) -> ToolResult:
        try:
            normalized_source = posixpath.normpath(source_path.replace("\\", "/"))
            if (
                not normalized_source
                or normalized_source in {".", ".."}
                or posixpath.isabs(normalized_source)
                or normalized_source.startswith("../")
            ):
                return ToolResult(success=False, error="INVALID_SOURCE_PATH: 源文件必须位于当前执行目录")
            absolute_source = posixpath.join(self._execution_root, normalized_source)
            result = await self._call(
                "publish_sandbox_file",
                self._user_id,
                source_path=absolute_source,
                destination_parent_id=destination_parent_id or None,
                destination_name=destination_name,
                conflict_policy=conflict_policy,
                expected_destination_revision=expected_destination_revision,
                base_version_id=(base_version_id or "").strip() or None,
                actor=self._actor,
                context=self._mutation_context(),
                idempotency_key=self._idempotency_key(),
            )
            return self._change_set_result(result)
        except Exception as exc:
            return self._failure(exc)


class WorkspaceCreateDirectoryTool(_WorkspaceToolBase):
    repeat_policy = "mutating"

    @property
    def name(self) -> str:
        return "workspace_create_directory"

    @property
    def description(self) -> str:
        return "Create one persistent workspace directory under a stable parent entry_id."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "parent_id": {"type": "string", "description": "Parent directory entry_id; omit for root"},
            },
            "required": ["name"],
        }

    async def execute(self, name: str, parent_id: str | None = None) -> ToolResult:
        try:
            result = await self._call(
                "create_directory",
                self._user_id,
                parent_id or None,
                name,
                actor=self._actor,
                context=self._mutation_context(),
                idempotency_key=self._idempotency_key(),
            )
            return self._mutation_result(result)
        except Exception as exc:
            return self._failure(exc)


class WorkspaceMoveTool(_WorkspaceToolBase):
    repeat_policy = "mutating"

    @property
    def name(self) -> str:
        return "workspace_move"

    @property
    def description(self) -> str:
        return "Rename and/or move a workspace entry with optimistic revision checking."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "revision": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "destination_parent_id": {"type": "string", "description": "Omit to keep current parent"},
                "move_to_root": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true to move the entry to the persistent workspace root",
                },
                "name": {"type": "string", "description": "Omit to keep current name"},
            },
            "required": ["entry_id", "revision"],
        }

    async def execute(
        self,
        entry_id: str,
        revision: int | str,
        destination_parent_id: str | None = None,
        move_to_root: bool = False,
        name: str | None = None,
    ) -> ToolResult:
        try:
            if not isinstance(move_to_root, bool):
                return ToolResult(success=False, error="move_to_root must be a boolean")
            current = await self._call("get_entry", self._user_id, entry_id)
            target_parent_id = (
                None
                if move_to_root
                else destination_parent_id
                if destination_parent_id is not None
                else current.parent_id
            )
            result = await self._call(
                "move_entry",
                self._user_id,
                entry_id,
                parent_id=target_parent_id,
                name=(name or "").strip() or None,
                expected_revision=revision,
                actor=self._actor,
                context=self._mutation_context(),
                idempotency_key=self._idempotency_key(),
            )
            return self._mutation_result(result)
        except Exception as exc:
            return self._failure(exc)


class WorkspaceDeleteTool(_WorkspaceToolBase):
    repeat_policy = "mutating"

    @property
    def name(self) -> str:
        return "workspace_delete"

    @property
    def description(self) -> str:
        return (
            "Permanently delete a workspace file or directory, including its history, only when "
            "the user explicitly asks. There is no recycle bin or restore. Never delete a target "
            "to work around a failed publish; its stable entry_id must be preserved."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
                "revision": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            },
            "required": ["entry_id", "revision"],
        }

    async def execute(self, entry_id: str, revision: int | str) -> ToolResult:
        try:
            result = await self._call(
                "delete_entry",
                self._user_id,
                entry_id,
                expected_revision=revision,
                actor=self._actor,
                context=self._mutation_context(),
                idempotency_key=self._idempotency_key(),
            )
            changes = [{
                "entry_id": root["entry_id"],
                "path": root["relative_path"],
                "name": root["name"],
                "kind": root["kind"],
                "revision": int(root["revision"]) + 1,
                "status": "deleted",
                "operation": "DELETED",
                "mutation_id": result.mutation_id,
                "affected_entry_ids": list(result.affected_entry_ids),
            } for root in result.roots]
            for change in changes:
                self._record_change(change)
            return ToolResult(
                success=True,
                content=f"DELETED {len(result.affected_entry_ids)} 个工作区条目；无法恢复",
                resource_changes=changes,
            )
        except Exception as exc:
            return self._failure(exc)


__all__ = [
    "WorkspaceCreateDirectoryTool",
    "WorkspaceListTool",
    "WorkspaceMoveTool",
    "WorkspacePublishTool",
    "WorkspaceStageTool",
    "WorkspaceDeleteTool",
]
