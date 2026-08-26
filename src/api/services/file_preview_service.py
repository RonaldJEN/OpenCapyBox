"""Sandbox-scoped office document preview conversion.

The source file and all conversion work stay inside the user's OpenSandbox.
The backend never executes LibreOffice against an untrusted document on the
API host.  Converted PDFs are cached in a hidden directory below the session
root and therefore disappear with the session workspace.
"""

from __future__ import annotations

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


async def _snapshot_office_source(
    sandbox,
    *,
    source_path: str,
    snapshot_path: str,
    extension: str,
) -> tuple[str, int]:
    """Copy/hash a bounded source in one sandbox process.

    The API host never receives the untrusted Office bytes.  Opening the source
    once also removes the stat/read TOCTOU window: replacing the path while the
    copy runs does not change the already-open file descriptor.
    """

    script = f"""python3 - <<'PY'
import hashlib, json, os
source = {json.dumps(source_path)}
destination = {json.dumps(snapshot_path)}
limit = {MAX_OFFICE_PREVIEW_BYTES}
digest = hashlib.sha256()
total = 0
result = {{"ok": False, "reason": "unavailable"}}
try:
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
        raise FilePreviewTooLargeError("文件超过 50 MiB，请下载后查看")
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


async def render_office_document_to_pdf(
    sandbox,
    *,
    source_filename: str,
    source_path: str,
    session_root: str,
) -> RenderedOfficePreview:
    """Convert a Word/PowerPoint file to a cached PDF inside OpenSandbox."""

    extension = posixpath.splitext(source_filename)[1].lower()
    if extension not in OFFICE_PDF_EXTENSIONS:
        raise FilePreviewUnsupportedError("此文件类型不支持转换为 PDF")

    request_key = uuid.uuid4().hex
    incoming_dir = posixpath.join(
        session_root,
        ".opencapybox-preview",
        f".incoming-{request_key}",
    )
    snapshot_path = posixpath.join(incoming_dir, f"source{extension}")
    profile_path = posixpath.join("/tmp", f"opencapybox-lo-{request_key}")
    owns_lock = False
    lock_path = ""

    try:
        cache_key, _source_size = await _snapshot_office_source(
            sandbox,
            source_path=source_path,
            snapshot_path=snapshot_path,
            extension=extension,
        )
        cache_dir = posixpath.join(
            session_root,
            ".opencapybox-preview",
            cache_key,
        )
        pdf_path = posixpath.join(cache_dir, "source.pdf")
        lock_path = posixpath.join(cache_dir, ".lock")

        cached_size = await _validated_pdf_size(sandbox, pdf_path)
        if cached_size:
            if cached_size > MAX_RENDERED_PDF_BYTES:
                await _run_command(sandbox, f"rm -f -- {shlex.quote(pdf_path)}")
                raise FilePreviewTooLargeError("转换后的 PDF 超过 100 MiB，请下载后查看")
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
            return RenderedOfficePreview(
                sandbox_path=pdf_path,
                filename=f"{posixpath.splitext(source_filename)[0]}.pdf",
                cache_key=cache_key,
                size=cached_size,
            )

        await _run_command(sandbox, f"rm -f -- {shlex.quote(pdf_path)}")
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

        return RenderedOfficePreview(
            sandbox_path=pdf_path,
            filename=f"{posixpath.splitext(source_filename)[0]}.pdf",
            cache_key=cache_key,
            size=published_size,
        )
    finally:
        await _cleanup_preview_working_files(sandbox, incoming_dir, profile_path)
        if owns_lock and lock_path:
            await _release_cache_lock(sandbox, lock_path)


def _command_stdout_text(execution) -> str:
    logs = getattr(execution, "logs", None)
    stdout = getattr(logs, "stdout", None) if logs is not None else None
    if stdout:
        return "".join(str(getattr(item, "text", item)) for item in stdout).strip()
    direct = getattr(execution, "stdout", None)
    return str(direct or "").strip()
