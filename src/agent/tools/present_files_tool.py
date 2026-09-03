"""Explicitly present selected current Session files to the user."""

from typing import Any

from opensandbox import Sandbox

from .base import Tool, ToolResult
from .sandbox_file_tools import _normalize_workspace_dir, _resolve_workspace_path
from .session_file_references import stat_session_file_reference


class SandboxPresentFilesTool(Tool):
    """Declare which existing Session files are user-facing deliverables."""

    repeat_policy = "mutating"

    def __init__(self, sandbox: Sandbox, workspace_dir: str = "/home/user"):
        self._sandbox = sandbox
        self._workspace_dir = _normalize_workspace_dir(workspace_dir)

    @property
    def name(self) -> str:
        return "present_files"

    @property
    def description(self) -> str:
        return (
            "Present completed files to the user below the final response. "
            "Call this once near the end with only the final deliverables, never "
            "temporary, intermediate, cache, validation, or source files. Paths "
            "must be files that currently exist in the current Session directory."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "uniqueItems": True,
                    "description": "Session-relative paths of completed deliverables.",
                }
            },
            "required": ["paths"],
            "additionalProperties": False,
        }

    async def execute(self, paths: list[str]) -> ToolResult:
        if not isinstance(paths, list) or not paths:
            return ToolResult(success=False, error="paths must contain at least one file")

        normalized: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if not isinstance(path, str) or not path.strip():
                return ToolResult(success=False, error="each path must be a non-empty string")
            full_path = _resolve_workspace_path(path.strip(), self._workspace_dir)
            if full_path in seen:
                continue
            seen.add(full_path)
            normalized.append(full_path)

        references: list[dict[str, Any]] = []
        for full_path in normalized:
            reference = await stat_session_file_reference(
                self._sandbox,
                self._workspace_dir,
                full_path,
            )
            if reference is None:
                return ToolResult(
                    success=False,
                    error=f"file does not exist or cannot be presented: {full_path}",
                )
            references.append({**reference, "operation": "PRESENTED"})

        return ToolResult(
            success=True,
            content="Presented files:\n" + "\n".join(
                str(reference["path"]) for reference in references
            ),
            assistant_file_references=references,
        )
