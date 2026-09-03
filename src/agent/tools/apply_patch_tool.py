"""Codex-compatible freeform patch tool for OpenSandbox text files."""

import hashlib
import posixpath
import shlex
from dataclasses import dataclass
from typing import Any

from opensandbox import Sandbox

from .base import Tool, ToolResult
from .sandbox_file_tools import (
    AgentConfigSync,
    _SandboxWriteConflictError,
    _SandboxWriteNotDispatchedError,
    _classify_text_write,
    _extract_exit_code,
    _is_missing_file_error,
    _normalize_read_only_paths,
    _normalize_workspace_dir,
    _resolve_workspace_path,
    _sandbox_read_text,
    _sandbox_write_text,
    _sync_agent_config_after_write,
)
APPLY_PATCH_GRAMMAR = r'''start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" LF
end_patch: "*** End Patch" LF?

hunk: add_hunk | delete_hunk | update_hunk
add_hunk: "*** Add File: " filename LF add_line+
delete_hunk: "*** Delete File: " filename LF
update_hunk: "*** Update File: " filename LF change_move? change?

filename: /(.+)/
add_line: "+" /(.*)/ LF -> line

change_move: "*** Move to: " filename LF
change: (change_context | change_line)+ eof_line?
change_context: ("@@" | "@@ " /(.+)/) LF
change_line: ("+" | "-" | " ") /(.*)/ LF
eof_line: "*** End of File" LF

%import common.LF
'''


class PatchError(ValueError):
    """A patch is malformed or cannot be applied to the observed file."""


class _DeleteConflictError(RuntimeError):
    pass


class _PatchWriteUncertain(RuntimeError):
    def __init__(self, path: str, error: Exception):
        super().__init__(str(error))
        self.path = path


@dataclass(frozen=True)
class PatchChunk:
    context: str | None
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    context_indices: tuple[tuple[int, int], ...]
    end_of_file: bool


@dataclass(frozen=True)
class PatchAction:
    kind: str
    path: str
    content: str | None = None
    chunks: tuple[PatchChunk, ...] = ()
    move_path: str | None = None
    added_lines: int = 0
    removed_lines: int = 0


@dataclass(frozen=True)
class ParsedPatch:
    actions: tuple[PatchAction, ...]


@dataclass(frozen=True)
class PreparedPatchAction:
    action: PatchAction
    source: str
    destination: str | None
    source_sha256: str | None
    new_content: str | None


@dataclass(frozen=True)
class _SourceLine:
    text: str
    ending: str


def parse_patch(patch: str) -> ParsedPatch:
    if not isinstance(patch, str) or not patch.strip():
        raise PatchError("patch must not be empty")
    lines = patch.strip().splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise PatchError("The first line of the patch must be '*** Begin Patch'")
    if lines[-1].strip() != "*** End Patch":
        raise PatchError("The last line of the patch must be '*** End Patch'")

    actions: list[PatchAction] = []
    index = 1
    last = len(lines) - 1
    while index < last:
        header = lines[index].strip()
        if header.startswith("*** Add File: "):
            path = _patch_path(header, "*** Add File: ")
            index += 1
            added: list[str] = []
            while index < last and not _is_file_header(lines[index]):
                line = lines[index]
                if not line.startswith("+"):
                    raise PatchError(
                        f"Invalid add-file line {index + 1}: every line must start with '+'"
                    )
                added.append(line[1:])
                index += 1
            if not added:
                raise PatchError(f"Add file hunk for path '{path}' is empty")
            actions.append(PatchAction(
                kind="add",
                path=path,
                content="\n".join(added) + "\n",
                added_lines=len(added),
            ))
            continue

        if header.startswith("*** Delete File: "):
            path = _patch_path(header, "*** Delete File: ")
            actions.append(PatchAction(kind="delete", path=path))
            index += 1
            continue

        if not header.startswith("*** Update File: "):
            raise PatchError(
                f"Invalid patch hunk on line {index + 1}: expected Add, Delete, or Update File"
            )

        path = _patch_path(header, "*** Update File: ")
        index += 1
        move_path = None
        if index < last and lines[index].strip().startswith("*** Move to: "):
            move_path = _patch_path(lines[index].strip(), "*** Move to: ")
            index += 1

        chunks: list[PatchChunk] = []
        context: str | None = None
        old_lines: list[str] = []
        new_lines: list[str] = []
        context_indices: list[tuple[int, int]] = []
        changed = False
        end_of_file = False
        added_lines = 0
        removed_lines = 0

        def flush_chunk() -> None:
            nonlocal context, old_lines, new_lines, context_indices, changed, end_of_file
            if context is None and not old_lines and not new_lines:
                return
            if not changed:
                raise PatchError(f"Update chunk for path '{path}' contains no changes")
            chunks.append(PatchChunk(
                context=context,
                old_lines=tuple(old_lines),
                new_lines=tuple(new_lines),
                context_indices=tuple(context_indices),
                end_of_file=end_of_file,
            ))
            context = None
            old_lines = []
            new_lines = []
            context_indices = []
            changed = False
            end_of_file = False

        while index < last and not _is_file_header(lines[index]):
            line = lines[index]
            if line == "@@" or line.startswith("@@ "):
                flush_chunk()
                context = line[3:] if line.startswith("@@ ") else None
                index += 1
                continue
            if line.strip() == "*** End of File":
                if context is None and not old_lines and not new_lines:
                    raise PatchError(f"Misplaced end-of-file marker on line {index + 1}")
                end_of_file = True
                index += 1
                if index < last and not _is_file_header(lines[index]):
                    raise PatchError("Patch content follows an end-of-file marker")
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                raise PatchError(
                    f"Invalid update line {index + 1}: expected ' ', '+', '-', or '@@'"
                )
            marker, content = line[0], line[1:]
            if marker == " ":
                context_indices.append((len(old_lines), len(new_lines)))
                old_lines.append(content)
                new_lines.append(content)
            elif marker == "+":
                new_lines.append(content)
                added_lines += 1
                changed = True
            else:
                old_lines.append(content)
                removed_lines += 1
                changed = True
            index += 1

        flush_chunk()
        if not chunks and move_path is None:
            raise PatchError(f"Update file hunk for path '{path}' is empty")
        actions.append(PatchAction(
            kind="update",
            path=path,
            chunks=tuple(chunks),
            move_path=move_path,
            added_lines=added_lines,
            removed_lines=removed_lines,
        ))

    if not actions:
        raise PatchError("No files were modified")
    return ParsedPatch(actions=tuple(actions))


def _patch_path(header: str, prefix: str) -> str:
    path = header[len(prefix):].strip()
    if not path or "\x00" in path:
        raise PatchError("Patch path must not be empty")
    return path


def _is_file_header(line: str) -> bool:
    stripped = line.strip()
    return any(stripped.startswith(prefix) for prefix in (
        "*** Add File: ",
        "*** Delete File: ",
        "*** Update File: ",
    ))


def apply_update(raw_content: str, action: PatchAction) -> str:
    source = _split_source_lines(raw_content)
    texts = [line.text for line in source]
    ending = _preferred_line_ending(source)
    replacements: list[tuple[int, int, list[_SourceLine]]] = []
    line_index = 0

    for chunk in action.chunks:
        if chunk.context is not None:
            context_index = _seek_sequence(texts, [chunk.context], line_index, False)
            if context_index is None:
                raise PatchError(f"Failed to find context '{chunk.context}' in {action.path}")
            line_index = context_index + 1

        pattern = list(chunk.old_lines)
        start = (
            _seek_sequence(texts, pattern, line_index, chunk.end_of_file)
            if pattern
            else len(source)
        )
        if start is None:
            raise PatchError(
                f"Failed to find expected lines in {action.path}:\n"
                + "\n".join(chunk.old_lines)
            )
        preserved = {
            new_index: source[start + old_index]
            for old_index, new_index in chunk.context_indices
            if start + old_index < len(source)
        }
        rendered = [
            preserved.get(position, _SourceLine(text, ending))
            for position, text in enumerate(chunk.new_lines)
        ]
        replacements.append((start, len(pattern), rendered))
        line_index = start + len(pattern)

    for start, old_length, rendered in reversed(replacements):
        source[start:start + old_length] = rendered
    return "".join(line.text + line.ending for line in source)


def _split_source_lines(content: str) -> list[_SourceLine]:
    lines: list[_SourceLine] = []
    start = 0
    index = 0
    while index < len(content):
        if content[index] not in "\r\n":
            index += 1
            continue
        ending = content[index]
        if ending == "\r" and index + 1 < len(content) and content[index + 1] == "\n":
            ending = "\r\n"
        lines.append(_SourceLine(content[start:index], ending))
        index += len(ending)
        start = index
    if start < len(content):
        lines.append(_SourceLine(content[start:], ""))
    return lines


def _preferred_line_ending(lines: list[_SourceLine]) -> str:
    endings = [line.ending for line in lines if line.ending]
    if not endings:
        return "\n"
    return max(("\r\n", "\n", "\r"), key=endings.count)


def _seek_sequence(
    lines: list[str],
    pattern: list[str],
    start: int,
    end_of_file: bool,
) -> int | None:
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    last_start = len(lines) - len(pattern)
    candidates = [last_start] if end_of_file else range(start, last_start + 1)
    for normalize in (
        lambda value: value,
        str.rstrip,
        str.strip,
        _normalize_unicode_context,
    ):
        for index in candidates:
            if all(
                normalize(lines[index + offset]) == normalize(expected)
                for offset, expected in enumerate(pattern)
            ):
                return index
    return None


_UNICODE_CONTEXT_TRANSLATION = str.maketrans({
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "\u00a0": " ",
    "\u2002": " ",
    "\u2003": " ",
    "\u2004": " ",
    "\u2005": " ",
    "\u2006": " ",
    "\u2007": " ",
    "\u2008": " ",
    "\u2009": " ",
    "\u200a": " ",
    "\u202f": " ",
    "\u205f": " ",
    "\u3000": " ",
})


def _normalize_unicode_context(value: str) -> str:
    return value.strip().translate(_UNICODE_CONTEXT_TRANSLATION)


async def _delete_text(
    sandbox: Sandbox,
    path: str,
    *,
    workspace_dir: str,
    expected_sha256: str,
) -> None:
    use_lock = (
        "/sessions/" in workspace_dir
        and path.startswith(workspace_dir.rstrip("/") + "/")
    )
    edit_root = posixpath.join(workspace_dir, ".opencapybox-edit")
    script = (
        "import fcntl,hashlib,os,stat,sys\n"
        f"path={path!r}\nexpected={expected_sha256!r}\n"
        f"use_lock={use_lock!r}\nroot={edit_root!r}\n"
        "lock=None\n"
        "try:\n"
        " if use_lock:\n"
        "  os.makedirs(root+'/locks',mode=0o700,exist_ok=True)\n"
        "  lock=os.open(root+'/locks/'+hashlib.sha256(path.encode()).hexdigest(),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)\n"
        "  fcntl.flock(lock,fcntl.LOCK_EX)\n"
        " try: st=os.lstat(path)\n"
        " except FileNotFoundError: sys.exit(2)\n"
        " if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode): sys.exit(4)\n"
        " fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)\n"
        " try: digest=hashlib.sha256(b''.join(iter(lambda:os.read(fd,65536),b''))).hexdigest()\n"
        " finally: os.close(fd)\n"
        " if digest!=expected: sys.exit(3)\n"
        " os.unlink(path)\n"
        " parent=os.open(os.path.dirname(path),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)\n"
        " try: os.fsync(parent)\n"
        " finally: os.close(parent)\n"
        "finally:\n"
        " if lock is not None: os.close(lock)\n"
    )
    try:
        result = await sandbox.commands.run("python3 -c " + shlex.quote(script))
    except Exception as exc:
        raise _PatchWriteUncertain(path, exc) from exc
    code = _extract_exit_code(result)
    if code == 2:
        raise FileNotFoundError(path)
    if code == 3:
        raise _DeleteConflictError("File changed while applying patch")
    if code == 4:
        raise PatchError(f"Patch delete target is not a regular file: {path}")
    if code != 0:
        raise RuntimeError(f"Patch delete failed for {path}")


class SandboxApplyPatchTool(Tool):
    """Apply a Codex freeform patch inside the current OpenSandbox workspace."""

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
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "Apply a Codex patch to UTF-8 text files in the current Session directory. "
            "The function call is JSON, but the `patch` field must contain the complete raw "
            "patch string; do not use a `data` field or Markdown fences. The patch must start "
            "with `*** Begin Patch`, end with `*** End Patch`, and contain one or more "
            "`*** Add File:`, `*** Delete File:`, or `*** Update File:` sections. Add-file "
            "content lines start with `+`. Update sections use `@@` hunks whose lines start "
            "with a space, `-`, or `+`. Do not use unified-diff `---`/`+++` headers. "
            "Example: `*** Begin Patch\n*** Update File: path/to/file.py\n@@\n-old\n+new\n"
            "*** End Patch`. Read existing files before updating them."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "The complete raw Codex patch string described by the tool.",
                }
            },
            "required": ["patch"],
        }

    def to_responses_schema(self) -> dict[str, Any]:
        return {
            "type": "custom",
            "name": self.name,
            "description": (
                "The `apply_patch` tool can be used to edit files. This is a "
                "FREEFORM tool, so do not wrap the patch in JSON."
            ),
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": APPLY_PATCH_GRAMMAR,
            },
        }

    async def execute(self, patch: str) -> ToolResult:
        applied: list[str] = []
        try:
            parsed = parse_patch(patch)
            prepared = await self._prepare_patch(parsed)
            for action in prepared:
                await self._apply_action(action, applied)
        except Exception as exc:
            uncertain = isinstance(exc, _PatchWriteUncertain)
            error_text = f"{type(exc).__name__}: {exc}"
            content = ""
            if applied:
                content = (
                    "Patch stopped after partial application. Applied:\n"
                    + "\n".join(applied)
                    + f"\nFailed: {error_text}\n"
                    "Inspect current files and submit only the remaining changes."
                )
            elif uncertain:
                content = (
                    f"The write to {exc.path} may have succeeded. Use read_file on "
                    "that path before retrying any file mutation."
                )
            return ToolResult(
                success=False,
                content=content,
                error=error_text,
                outcome_uncertain=uncertain,
            )

        return ToolResult(
            success=True,
            content="Success. Updated the following files:\n" + "\n".join(applied),
        )

    async def _prepare_patch(
        self,
        parsed: ParsedPatch,
    ) -> tuple[PreparedPatchAction, ...]:
        prepared: list[PreparedPatchAction] = []
        targeted_sources: set[str] = set()
        for action in parsed.actions:
            source = _resolve_workspace_path(action.path, self._workspace_dir)
            destination = (
                _resolve_workspace_path(action.move_path, self._workspace_dir)
                if action.move_path
                else None
            )
            for target in (source, destination):
                if target and target in self._read_only_paths:
                    raise PatchError(
                        f"{target} is managed by the platform template and cannot be edited"
                    )
            if source in targeted_sources:
                raise PatchError(f"multiple operations target {source}")
            targeted_sources.add(source)

            if action.kind == "add":
                prepared.append(PreparedPatchAction(
                    action=action,
                    source=source,
                    destination=destination,
                    source_sha256=None,
                    new_content=action.content,
                ))
                continue

            raw = await _sandbox_read_text(self._sandbox, source)
            new_content = None
            if action.kind == "update":
                new_content = apply_update(raw, action) if action.chunks else raw
            prepared.append(PreparedPatchAction(
                action=action,
                source=source,
                destination=destination,
                source_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                new_content=new_content,
            ))
        return tuple(prepared)

    async def _apply_action(
        self,
        prepared: PreparedPatchAction,
        applied: list[str],
    ) -> None:
        action = prepared.action
        source = prepared.source
        destination = prepared.destination
        if action.kind == "add":
            assert prepared.new_content is not None
            await self._write(source, prepared.new_content)
            await _sync_agent_config_after_write(
                self._agent_config_sync,
                source,
                prepared.new_content,
            )
            applied.append(f"A {action.path}")
            return

        assert prepared.source_sha256 is not None
        if action.kind == "delete":
            await _delete_text(
                self._sandbox,
                source,
                workspace_dir=self._workspace_dir,
                expected_sha256=prepared.source_sha256,
            )
            await _sync_agent_config_after_write(self._agent_config_sync, source, "")
            applied.append(f"D {action.path}")
            return

        assert prepared.new_content is not None
        new_content = prepared.new_content
        target = destination or source
        await self._write(
            target,
            new_content,
            expected_sha256=(
                prepared.source_sha256 if target == source else None
            ),
        )
        await _sync_agent_config_after_write(
            self._agent_config_sync,
            target,
            new_content,
        )
        if destination and destination != source:
            applied.append(f"A {action.move_path}")
            await _delete_text(
                self._sandbox,
                source,
                workspace_dir=self._workspace_dir,
                expected_sha256=prepared.source_sha256,
            )
            await _sync_agent_config_after_write(self._agent_config_sync, source, "")
            applied.append(f"D {action.path}")
            return
        applied.append(
            f"M {action.path} +{action.added_lines} -{action.removed_lines}"
        )

    async def _write(
        self,
        path: str,
        content: str,
        *,
        expected_sha256: str | None = None,
    ) -> None:
        parent = posixpath.dirname(path)
        if parent:
            await self._sandbox.commands.run("mkdir -p -- " + shlex.quote(parent))
        status, observed_sha = await _classify_text_write(
            self._sandbox,
            path,
            content,
        )
        if status == "NO CHANGE":
            return
        options: dict[str, Any] = {"workspace_dir": self._workspace_dir}
        if (
            "/sessions/" in self._workspace_dir
            and path.startswith(self._workspace_dir.rstrip("/") + "/")
        ):
            if expected_sha256 is not None:
                options["expected_sha256"] = expected_sha256
            elif observed_sha:
                options["expected_sha256"] = observed_sha
            else:
                options["must_not_exist"] = True
        try:
            await _sandbox_write_text(self._sandbox, path, content, **options)
        except (_SandboxWriteConflictError, _SandboxWriteNotDispatchedError):
            raise
        except Exception as exc:
            raise _PatchWriteUncertain(path, exc) from exc
