"""Sandbox-scoped office document preview conversion.

The source file and all conversion work stay inside the user's OpenSandbox.
The backend never executes LibreOffice against an untrusted document
on the API host. Converted artifacts stay below the session root and therefore
disappear with the session workspace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import shlex
import uuid
from dataclasses import dataclass
from datetime import timedelta

from opensandbox.models.execd import RunCommandOpts


logger = logging.getLogger(__name__)

OFFICE_PDF_EXTENSIONS = frozenset({".doc", ".docx", ".ppt", ".pptx"})
MAX_OFFICE_PREVIEW_BYTES = 50 * 1024 * 1024
MAX_RENDERED_PDF_BYTES = 100 * 1024 * 1024
OFFICE_PREVIEW_TIMEOUT_SECONDS = 90
OFFICE_PREVIEW_LOCK_STALE_SECONDS = 180
OFFICE_PREVIEW_INCOMING_STALE_SECONDS = 300


async def _complete_cleanup_before_cancellation(awaitable):
    """Finish remote cleanup before propagating an HTTP/task cancellation."""
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


class FilePreviewError(RuntimeError):
    """Base class for a user-facing preview conversion failure."""


class FilePreviewUnsupportedError(FilePreviewError):
    """The requested file type cannot be converted by this renderer."""


class FilePreviewTooLargeError(FilePreviewError):
    """The requested file exceeds the bounded conversion size."""


class FilePreviewUnavailableError(FilePreviewError):
    """The sandbox renderer is unavailable or failed to produce output."""


class FilePreviewSourceNotFoundError(FilePreviewError):
    """The requested sandbox source no longer exists."""


class FilePreviewConversionError(FilePreviewError):
    """LibreOffice rejected or could not parse the source document."""


class FilePreviewTimeoutError(FilePreviewError):
    """The bounded preview conversion timed out."""


@dataclass(frozen=True)
class RenderedOfficePreview:
    sandbox_path: str
    filename: str
    cache_key: str
    size: int


def _command_exit_code(execution) -> int:
    value = getattr(execution, "exit_code", 0)
    return int(value) if isinstance(value, int) else 0


async def _run_command(sandbox, command: str, *, timeout_seconds: int = 15):
    try:
        return await sandbox.commands.run(
            command,
            opts=RunCommandOpts(timeout=timedelta(seconds=timeout_seconds)),
        )
    except FilePreviewError:
        raise
    except Exception as exc:
        raise FilePreviewUnavailableError("沙箱预览服务不可用") from exc


def _preview_cache_key(content_digest: str, extension: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"opencapybox-office-preview-v1\0")
    digest.update(extension.encode("ascii"))
    digest.update(b"\0")
    digest.update(content_digest.encode("ascii"))
    return digest.hexdigest()


def office_preview_cache_keys(content_digest: str) -> tuple[str, ...]:
    """Return every Office cache key that can be derived from one content SHA."""
    normalized_digest = content_digest.lower()
    if (
        len(normalized_digest) != 64
        or any(character not in "0123456789abcdef" for character in normalized_digest)
    ):
        raise ValueError("invalid Office preview content digest")
    return tuple(
        _preview_cache_key(normalized_digest, extension)
        for extension in sorted(OFFICE_PDF_EXTENSIONS)
    )


def _trusted_preview_cache_key(
    source_sha256: str | None,
    source_size: int | None,
    extension: str,
) -> str | None:
    """Resolve an immutable Workspace object's cache key without reading it."""
    if source_sha256 is None and source_size is None:
        return None
    if source_sha256 is None or source_size is None:
        raise FilePreviewUnavailableError("文件版本元数据不完整")
    normalized_sha256 = source_sha256.lower()
    if (
        len(normalized_sha256) != 64
        or any(character not in "0123456789abcdef" for character in normalized_sha256)
        or source_size < 0
    ):
        raise FilePreviewUnavailableError("文件版本元数据无效")
    if source_size > MAX_OFFICE_PREVIEW_BYTES:
        raise FilePreviewTooLargeError("文件超过 50 MiB，请下载后查看")
    return _preview_cache_key(normalized_sha256, extension)


async def _snapshot_bounded_source(
    sandbox,
    *,
    source_path: str,
    snapshot_path: str,
    max_bytes: int,
    too_large_message: str,
    cleanup_stale_incoming_seconds: int | None = None,
) -> tuple[str, int]:
    """Copy/hash a bounded source in one sandbox process.

    The API host never receives the untrusted Office bytes.  Opening the source
    once also removes the stat/read TOCTOU window: replacing the path while the
    copy runs does not change the already-open file descriptor.
    """

    script = f"""python3 - <<'PY'
import hashlib, json, os, re, shutil, time
source = {json.dumps(source_path)}
destination = {json.dumps(snapshot_path)}
limit = {max_bytes}
stale_incoming_seconds = {cleanup_stale_incoming_seconds!r}
digest = hashlib.sha256()
total = 0
result = {{"ok": False, "reason": "unavailable"}}
try:
    if stale_incoming_seconds is not None:
        incoming_dir = os.path.dirname(destination)
        cache_root = os.path.dirname(incoming_dir)
        pattern = re.compile(r'^\\.incoming-[0-9a-f]{{32}}$')
        if os.path.isdir(cache_root) and not os.path.islink(cache_root):
            now = time.time()
            for entry in os.scandir(cache_root):
                if (
                    entry.path != incoming_dir
                    and pattern.fullmatch(entry.name)
                    and entry.is_dir(follow_symlinks=False)
                    and now - entry.stat(follow_symlinks=False).st_mtime > stale_incoming_seconds
                ):
                    shutil.rmtree(entry.path)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(source, "rb") as reader, open(destination, "xb") as writer:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                result = {{"ok": False, "reason": "too_large"}}
                break
            digest.update(chunk)
            writer.write(chunk)
        else:
            pass
    if total <= limit:
        result = {{"ok": True, "size": total, "sha256": digest.hexdigest()}}
except FileNotFoundError:
    result = {{"ok": False, "reason": "missing"}}
except Exception:
    result = {{"ok": False, "reason": "unavailable"}}
if not result.get("ok"):
    try:
        os.remove(destination)
    except OSError:
        pass
print(json.dumps(result, separators=(",", ":")))
PY"""
    execution = await _run_command(sandbox, script, timeout_seconds=30)
    if _command_exit_code(execution) != 0:
        raise FilePreviewUnavailableError("无法读取待预览文件")
    try:
        payload = json.loads(_command_stdout_text(execution))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FilePreviewUnavailableError("无法读取待预览文件") from exc
    if payload.get("reason") == "too_large":
        raise FilePreviewTooLargeError(too_large_message)
    if payload.get("reason") == "missing":
        raise FilePreviewSourceNotFoundError("文件不存在或无法读取")
    if payload.get("ok") is not True:
        raise FilePreviewUnavailableError("无法读取待预览文件")
    size = payload.get("size")
    content_digest = payload.get("sha256")
    if not isinstance(size, int) or size < 0:
        raise FilePreviewUnavailableError("无法读取待预览文件")
    if not isinstance(content_digest, str) or len(content_digest) != 64:
        raise FilePreviewUnavailableError("无法读取待预览文件")
    return content_digest, size


async def _snapshot_office_source(
    sandbox,
    *,
    source_path: str,
    snapshot_path: str,
    extension: str,
) -> tuple[str, int]:
    content_digest, size = await _snapshot_bounded_source(
        sandbox,
        source_path=source_path,
        snapshot_path=snapshot_path,
        max_bytes=MAX_OFFICE_PREVIEW_BYTES,
        too_large_message="文件超过 50 MiB，请下载后查看",
        cleanup_stale_incoming_seconds=OFFICE_PREVIEW_INCOMING_STALE_SECONDS,
    )
    return _preview_cache_key(content_digest, extension), size


async def _cleanup_preview_working_files(sandbox, *paths: str) -> None:
    if not paths:
        return
    try:
        quoted = " ".join(shlex.quote(path) for path in paths)
        await _run_command(sandbox, f"rm -rf -- {quoted}")
    except Exception:
        logger.debug("Failed to clean preview working files", exc_info=True)


async def _validated_pdf_size(sandbox, path: str) -> int | None:
    quoted_path = shlex.quote(path)
    result = await _run_command(
        sandbox,
        f"test -s {quoted_path} "
        f"&& test \"$(head -c 5 -- {quoted_path})\" = '%PDF-' "
        f"&& stat -c %s -- {quoted_path}"
    )
    if _command_exit_code(result) != 0:
        return None
    try:
        size = int(_command_stdout_text(result))
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


async def _acquire_cache_lock(sandbox, lock_path: str) -> bool:
    quoted_lock = shlex.quote(lock_path)
    command = (
        f"if test -d {quoted_lock}; then "
        f"now=$(date +%s); modified=$(stat -c %Y -- {quoted_lock} 2>/dev/null || echo \"$now\"); "
        f"if test $((now-modified)) -gt {OFFICE_PREVIEW_LOCK_STALE_SECONDS}; "
        f"then rm -rf -- {quoted_lock}; fi; fi; "
        f"if mkdir {quoted_lock} 2>/dev/null; then echo OWNER; else echo WAITER; fi"
    )
    result = await _run_command(sandbox, command)
    return _command_exit_code(result) == 0 and _command_stdout_text(result) == "OWNER"


async def _wait_for_cache_lock(sandbox, lock_path: str) -> None:
    wait_script = f"while test -d {shlex.quote(lock_path)}; do sleep 1; done"
    result = await _run_command(
        sandbox,
        f"timeout -k 5 {OFFICE_PREVIEW_TIMEOUT_SECONDS + 15} sh -c {shlex.quote(wait_script)}",
        timeout_seconds=OFFICE_PREVIEW_TIMEOUT_SECONDS + 25,
    )
    if _command_exit_code(result) != 0:
        raise FilePreviewTimeoutError("文档预览转换繁忙，请稍后重试")


async def _release_cache_lock(sandbox, lock_path: str) -> None:
    try:
        await _run_command(
            sandbox,
            f"rmdir -- {shlex.quote(lock_path)} 2>/dev/null || true",
        )
    except Exception:
        logger.debug("Failed to release preview cache lock", exc_info=True)


async def _prune_office_preview_cache(
    sandbox,
    *,
    cache_root: str,
    max_bytes: int,
    protected_cache_key: str,
) -> None:
    """Delete oldest complete cache directories without touching active work."""

    if max_bytes <= 0:
        return
    script = f"""python3 - <<'PY'
import json, os, re, shutil, stat
root = {json.dumps(cache_root)}
limit = {int(max_bytes)}
protected = {json.dumps(protected_cache_key)}
entries = []
total = 0
try:
    names = os.listdir(root)
except FileNotFoundError:
    names = []
for name in names:
    if not re.fullmatch(r'[0-9a-f]{{64}}', name):
        continue
    directory = os.path.join(root, name)
    try:
        directory_stat = os.lstat(directory)
        if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
            continue
        if os.path.isdir(os.path.join(directory, '.lock')):
            continue
        pdf_path = os.path.join(directory, 'source.pdf')
        pdf_stat = os.lstat(pdf_path)
        if not stat.S_ISREG(pdf_stat.st_mode) or stat.S_ISLNK(pdf_stat.st_mode):
            continue
    except OSError:
        continue
    size = int(pdf_stat.st_size)
    total += size
    entries.append((float(pdf_stat.st_mtime), name, directory, size))
removed = 0
for _mtime, name, directory, size in sorted(entries):
    if total <= limit:
        break
    if name == protected:
        continue
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError:
        continue
    total -= size
    removed += size
print(json.dumps({{'removed_bytes': removed, 'remaining_bytes': total}}))
PY"""
    result = await _run_command(sandbox, script)
    if _command_exit_code(result) != 0:
        logger.warning("Office preview cache pruning failed root=%s", cache_root)


async def _touch_and_prune_office_preview_cache(
    sandbox,
    *,
    pdf_path: str,
    cache_root: str,
    cache_key: str,
    cache_max_bytes: int | None,
) -> None:
    await _run_command(sandbox, f"touch -- {shlex.quote(pdf_path)}")
    if cache_max_bytes is not None:
        await _prune_office_preview_cache(
            sandbox,
            cache_root=cache_root,
            max_bytes=cache_max_bytes,
            protected_cache_key=cache_key,
        )


async def render_office_document_to_pdf(
    sandbox,
    *,
    source_filename: str,
    source_path: str,
    session_root: str,
    cache_max_bytes: int | None = None,
    cache_root: str | None = None,
    source_sha256: str | None = None,
    source_size: int | None = None,
) -> RenderedOfficePreview:
    """Convert a Word/PowerPoint file to a cached PDF inside OpenSandbox."""

    extension = posixpath.splitext(source_filename)[1].lower()
    if extension not in OFFICE_PDF_EXTENSIONS:
        raise FilePreviewUnsupportedError("此文件类型不支持转换为 PDF")

    request_key = uuid.uuid4().hex
    effective_cache_root = cache_root or posixpath.join(session_root, ".opencapybox-preview")
    incoming_dir = posixpath.join(
        effective_cache_root,
        f".incoming-{request_key}",
    )
    snapshot_path = posixpath.join(incoming_dir, f"source{extension}")
    profile_path = posixpath.join("/tmp", f"opencapybox-lo-{request_key}")
    owns_lock = False
    lock_path = ""
    cleanup_paths: list[str] = []

    try:
        trusted_cache_key = _trusted_preview_cache_key(
            source_sha256,
            source_size,
            extension,
        )
        if trusted_cache_key is None:
            cleanup_paths.append(incoming_dir)
            cache_key, _source_size = await _snapshot_office_source(
                sandbox,
                source_path=source_path,
                snapshot_path=snapshot_path,
                extension=extension,
            )
        else:
            cache_key = trusted_cache_key
        cache_dir = posixpath.join(
            effective_cache_root,
            cache_key,
        )
        pdf_path = posixpath.join(cache_dir, "source.pdf")
        lock_path = posixpath.join(cache_dir, ".lock")

        cached_size = await _validated_pdf_size(sandbox, pdf_path)
        if cached_size:
            if cached_size > MAX_RENDERED_PDF_BYTES:
                await _run_command(sandbox, f"rm -f -- {shlex.quote(pdf_path)}")
                raise FilePreviewTooLargeError("转换后的 PDF 超过 100 MiB，请下载后查看")
            await _touch_and_prune_office_preview_cache(
                sandbox,
                pdf_path=pdf_path,
                cache_root=effective_cache_root,
                cache_key=cache_key,
                cache_max_bytes=cache_max_bytes,
            )
            return RenderedOfficePreview(
                sandbox_path=pdf_path,
                filename=f"{posixpath.splitext(source_filename)[0]}.pdf",
                cache_key=cache_key,
                size=cached_size,
            )

        mkdir_result = await _run_command(sandbox, f"mkdir -p {shlex.quote(cache_dir)}")
        if _command_exit_code(mkdir_result) != 0:
            raise FilePreviewUnavailableError("无法创建预览缓存目录")

        owns_lock = await _acquire_cache_lock(sandbox, lock_path)
        if not owns_lock:
            await _wait_for_cache_lock(sandbox, lock_path)
            cached_size = await _validated_pdf_size(sandbox, pdf_path)
            if cached_size:
                if cached_size > MAX_RENDERED_PDF_BYTES:
                    raise FilePreviewTooLargeError("转换后的 PDF 超过 100 MiB，请下载后查看")
                await _touch_and_prune_office_preview_cache(
                    sandbox,
                    pdf_path=pdf_path,
                    cache_root=effective_cache_root,
                    cache_key=cache_key,
                    cache_max_bytes=cache_max_bytes,
                )
                return RenderedOfficePreview(
                    sandbox_path=pdf_path,
                    filename=f"{posixpath.splitext(source_filename)[0]}.pdf",
                    cache_key=cache_key,
                    size=cached_size,
                )
            owns_lock = await _acquire_cache_lock(sandbox, lock_path)
            if not owns_lock:
                raise FilePreviewUnavailableError("文档预览转换繁忙，请稍后重试")

        # Another request may have completed between the first probe and lock acquisition.
        cached_size = await _validated_pdf_size(sandbox, pdf_path)
        if cached_size:
            if cached_size > MAX_RENDERED_PDF_BYTES:
                await _run_command(sandbox, f"rm -f -- {shlex.quote(pdf_path)}")
                raise FilePreviewTooLargeError("转换后的 PDF 超过 100 MiB，请下载后查看")
            await _touch_and_prune_office_preview_cache(
                sandbox,
                pdf_path=pdf_path,
                cache_root=effective_cache_root,
                cache_key=cache_key,
                cache_max_bytes=cache_max_bytes,
            )
            return RenderedOfficePreview(
                sandbox_path=pdf_path,
                filename=f"{posixpath.splitext(source_filename)[0]}.pdf",
                cache_key=cache_key,
                size=cached_size,
            )

        if trusted_cache_key is not None:
            cleanup_paths.append(incoming_dir)
            observed_cache_key, observed_size = await _snapshot_office_source(
                sandbox,
                source_path=source_path,
                snapshot_path=snapshot_path,
                extension=extension,
            )
            if observed_cache_key != trusted_cache_key or observed_size != source_size:
                raise FilePreviewConversionError("文件内容与版本元数据不一致")

        await _run_command(sandbox, f"rm -f -- {shlex.quote(pdf_path)}")
        cleanup_paths.append(profile_path)
        work_result = await _run_command(sandbox, f"mkdir -p {shlex.quote(profile_path)}")
        if _command_exit_code(work_result) != 0:
            raise FilePreviewUnavailableError("无法创建预览工作目录")

        capability = await _run_command(sandbox, "command -v soffice >/dev/null 2>&1")
        if _command_exit_code(capability) != 0:
            raise FilePreviewUnavailableError("沙箱未安装 Office 预览组件")

        profile_uri = f"file://{profile_path}"
        command = " ".join(
            [
                "timeout",
                "-k",
                "5",
                str(OFFICE_PREVIEW_TIMEOUT_SECONDS),
                "soffice",
                "--headless",
                "--nologo",
                "--nodefault",
                "--norestore",
                shlex.quote(f"-env:UserInstallation={profile_uri}"),
                "--convert-to",
                "pdf",
                "--outdir",
                shlex.quote(incoming_dir),
                shlex.quote(snapshot_path),
            ]
        )
        conversion = await _run_command(
            sandbox,
            command,
            timeout_seconds=OFFICE_PREVIEW_TIMEOUT_SECONDS + 10,
        )
        conversion_exit = _command_exit_code(conversion)
        if conversion_exit in {124, 137}:
            raise FilePreviewTimeoutError("文档转换超时，请稍后重试")
        if conversion_exit != 0:
            logger.warning(
                "Office preview conversion failed (ext=%s, cache=%s, exit=%s)",
                extension,
                cache_key[:12],
                conversion_exit,
            )
            raise FilePreviewConversionError("文档转换失败，请下载后查看")

        scratch_pdf_path = posixpath.join(incoming_dir, "source.pdf")
        rendered_size = await _validated_pdf_size(sandbox, scratch_pdf_path)
        if not rendered_size:
            raise FilePreviewConversionError("文档转换未生成可用的 PDF")
        if rendered_size > MAX_RENDERED_PDF_BYTES:
            raise FilePreviewTooLargeError("转换后的 PDF 超过 100 MiB，请下载后查看")

        move_result = await _run_command(
            sandbox,
            f"mv -f -- {shlex.quote(scratch_pdf_path)} {shlex.quote(pdf_path)}",
        )
        if _command_exit_code(move_result) != 0:
            raise FilePreviewUnavailableError("无法发布转换后的 PDF")
        published_size = await _validated_pdf_size(sandbox, pdf_path)
        if published_size != rendered_size:
            await _run_command(sandbox, f"rm -f -- {shlex.quote(pdf_path)}")
            raise FilePreviewUnavailableError("转换后的 PDF 校验失败")

        await _touch_and_prune_office_preview_cache(
            sandbox,
            pdf_path=pdf_path,
            cache_root=effective_cache_root,
            cache_key=cache_key,
            cache_max_bytes=cache_max_bytes,
        )

        return RenderedOfficePreview(
            sandbox_path=pdf_path,
            filename=f"{posixpath.splitext(source_filename)[0]}.pdf",
            cache_key=cache_key,
            size=published_size,
        )
    finally:
        try:
            if cleanup_paths:
                await _complete_cleanup_before_cancellation(
                    _cleanup_preview_working_files(sandbox, *cleanup_paths)
                )
        finally:
            if owns_lock and lock_path:
                await _complete_cleanup_before_cancellation(
                    _release_cache_lock(sandbox, lock_path)
                )


def _command_stdout_text(execution) -> str:
    logs = getattr(execution, "logs", None)
    stdout = getattr(logs, "stdout", None) if logs is not None else None
    if stdout:
        return "".join(str(getattr(item, "text", item)) for item in stdout).strip()
    direct = getattr(execution, "stdout", None)
    return str(direct or "").strip()
