"""Sandbox-scoped office document preview conversion.

The source file and all conversion work stay inside the user's OpenSandbox.
The backend never executes LibreOffice against an untrusted document
on the API host. Converted artifacts stay below the session root and therefore
disappear with the session workspace.

Every ``sandbox.commands.run`` call carries a fixed round-trip cost, so the
whole pipeline (bounded snapshot, hash, cache probe, lock, conversion,
validation, publish, prune and cleanup) runs as one sandbox process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
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
OFFICE_PREVIEW_LOCK_WAIT_SECONDS = OFFICE_PREVIEW_TIMEOUT_SECONDS + 15
OFFICE_PREVIEW_INCOMING_STALE_SECONDS = 300
# 外层 SDK 超时必须覆盖脚本内部最长路径：等锁 + 转换（含 kill 宽限）+ 复制/清理余量。
OFFICE_PREVIEW_SCRIPT_TIMEOUT_SECONDS = (
    OFFICE_PREVIEW_LOCK_WAIT_SECONDS + OFFICE_PREVIEW_TIMEOUT_SECONDS + 60
)


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
    # 沙箱脚本 _OFFICE_PREVIEW_SCRIPT 中的 cache_key_for() 必须与此保持同一公式。
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


def _office_convert_argv(
    *,
    profile_dir: str,
    output_dir: str,
    snapshot_path: str,
) -> list[str]:
    return [
        "timeout",
        "-k",
        "5",
        str(OFFICE_PREVIEW_TIMEOUT_SECONDS),
        "soffice",
        "--headless",
        "--nologo",
        "--nodefault",
        "--norestore",
        f"-env:UserInstallation=file://{profile_dir}",
        "--convert-to",
        "pdf",
        "--outdir",
        output_dir,
        snapshot_path,
    ]


# 在沙箱内以单进程执行。源文件只打开一次完成有界复制与 hash，消除 stat/read TOCTOU；
# 所有失败以 {"ok": false, "reason": ...} 返回，未预期异常让进程以非零码退出。
_OFFICE_PREVIEW_SCRIPT = r'''
import hashlib, json, os, re, shutil, stat, subprocess, time

INCOMING_NAME = re.compile(r"\.incoming-[0-9a-f]{32}")
CACHE_NAME = re.compile(r"[0-9a-f]{64}")
cache_root = PARAMS["cache_root"]
incoming_dir = PARAMS["incoming_dir"]


class Failure(Exception):
    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def cache_key_for(content_digest):
    hasher = hashlib.sha256()
    hasher.update(b"opencapybox-office-preview-v1\0")
    hasher.update(PARAMS["extension"].encode("ascii"))
    hasher.update(b"\0")
    hasher.update(content_digest.encode("ascii"))
    return hasher.hexdigest()


def remove_stale_incoming():
    now = time.time()
    for entry in os.scandir(cache_root):
        if not INCOMING_NAME.fullmatch(entry.name):
            continue
        try:
            if (
                entry.is_dir(follow_symlinks=False)
                and now - entry.stat(follow_symlinks=False).st_mtime > PARAMS["incoming_stale_seconds"]
            ):
                shutil.rmtree(entry.path)
        except FileNotFoundError:
            # 并发请求刚清掉同一个残留目录。
            continue


def snapshot():
    digest = hashlib.sha256()
    size = 0
    try:
        reader = open(PARAMS["source_path"], "rb")
    except FileNotFoundError:
        raise Failure("missing")
    with reader, open(PARAMS["snapshot_path"], "xb") as writer:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > PARAMS["max_source_bytes"]:
                raise Failure("source_too_large")
            digest.update(chunk)
            writer.write(chunk)
    return digest.hexdigest(), size


def valid_pdf_size(path):
    try:
        with open(path, "rb") as reader:
            if reader.read(5) != b"%PDF-":
                return 0
            return os.fstat(reader.fileno()).st_size
    except FileNotFoundError:
        return 0


def acquire_lock(lock):
    try:
        if time.time() - os.stat(lock).st_mtime > PARAMS["lock_stale_seconds"]:
            os.rmdir(lock)
    except FileNotFoundError:
        pass
    try:
        os.mkdir(lock)
    except FileExistsError:
        return False
    return True


def wait_for_lock(lock):
    deadline = time.monotonic() + PARAMS["lock_wait_seconds"]
    while os.path.isdir(lock):
        if time.monotonic() > deadline:
            raise Failure("busy_timeout")
        time.sleep(0.2)


def convert(pdf_path):
    os.makedirs(PARAMS["profile_dir"])
    exit_code = subprocess.run(
        PARAMS["convert_argv"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    if exit_code in (124, 137):
        raise Failure("timeout")
    if exit_code == 127:
        raise Failure("renderer_missing")
    if exit_code != 0:
        raise Failure("conversion_failed", exit_code=exit_code)
    scratch_pdf = os.path.join(incoming_dir, "source.pdf")
    size = valid_pdf_size(scratch_pdf)
    if not size:
        raise Failure("no_output")
    if size > PARAMS["max_pdf_bytes"]:
        raise Failure("pdf_too_large")
    os.replace(scratch_pdf, pdf_path)
    return size


def prune(protected_key):
    limit = PARAMS["cache_max_bytes"]
    if limit is None:
        return
    entries = []
    total = 0
    for name in os.listdir(cache_root):
        if not CACHE_NAME.fullmatch(name):
            continue
        directory = os.path.join(cache_root, name)
        try:
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                continue
            if os.path.isdir(os.path.join(directory, ".lock")):
                continue
            pdf_stat = os.lstat(os.path.join(directory, "source.pdf"))
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(pdf_stat.st_mode):
            continue
        total += pdf_stat.st_size
        entries.append((pdf_stat.st_mtime, name, directory, pdf_stat.st_size))
    for _mtime, name, directory, size in sorted(entries):
        if total <= limit:
            break
        if name == protected_key:
            continue
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        total -= size


def render():
    trusted_key = PARAMS["trusted_cache_key"]
    cache_key = trusted_key or cache_key_for(snapshot()[0])
    cache_dir = os.path.join(cache_root, cache_key)
    pdf_path = os.path.join(cache_dir, "source.pdf")
    lock = os.path.join(cache_dir, ".lock")
    holds_lock = False
    try:
        size = valid_pdf_size(pdf_path)
        if not size:
            os.makedirs(cache_dir, exist_ok=True)
            holds_lock = acquire_lock(lock)
            if not holds_lock:
                wait_for_lock(lock)
                size = valid_pdf_size(pdf_path)
                if not size:
                    holds_lock = acquire_lock(lock)
                    if not holds_lock:
                        raise Failure("busy")
        if not size:
            # 首次探测与拿到锁之间，另一个请求可能已经发布完成。
            size = valid_pdf_size(pdf_path)
        if not size:
            if trusted_key is not None:
                content_digest, observed_size = snapshot()
                if cache_key_for(content_digest) != trusted_key or observed_size != PARAMS["trusted_size"]:
                    raise Failure("digest_mismatch")
            size = convert(pdf_path)
        elif size > PARAMS["max_pdf_bytes"]:
            os.remove(pdf_path)
            raise Failure("pdf_too_large")
        os.utime(pdf_path)
    finally:
        if holds_lock:
            os.rmdir(lock)
    # 释放锁后再淘汰，当前条目才会计入容量。
    prune(cache_key)
    return {"ok": True, "cache_key": cache_key, "pdf_path": pdf_path, "size": size}


def main():
    os.makedirs(cache_root, exist_ok=True)
    remove_stale_incoming()
    os.makedirs(incoming_dir)
    try:
        return render()
    finally:
        shutil.rmtree(incoming_dir)


try:
    result = main()
except Failure as failure:
    result = {"ok": False, "reason": failure.reason, **failure.detail}
print(json.dumps(result, separators=(",", ":")))
'''


_PREVIEW_FAILURES = {
    "missing": (FilePreviewSourceNotFoundError, "文件不存在或无法读取"),
    "source_too_large": (FilePreviewTooLargeError, "文件超过 50 MiB，请下载后查看"),
    "busy_timeout": (FilePreviewTimeoutError, "文档预览转换繁忙，请稍后重试"),
    "busy": (FilePreviewUnavailableError, "文档预览转换繁忙，请稍后重试"),
    "digest_mismatch": (FilePreviewConversionError, "文件内容与版本元数据不一致"),
    "renderer_missing": (FilePreviewUnavailableError, "沙箱未安装 Office 预览组件"),
    "timeout": (FilePreviewTimeoutError, "文档转换超时，请稍后重试"),
    "conversion_failed": (FilePreviewConversionError, "文档转换失败，请下载后查看"),
    "no_output": (FilePreviewConversionError, "文档转换未生成可用的 PDF"),
    "pdf_too_large": (FilePreviewTooLargeError, "转换后的 PDF 超过 100 MiB，请下载后查看"),
}


def _office_preview_command(params: dict) -> str:
    # ascii() 产出纯 ASCII 的 Python 字面量，中文路径不依赖沙箱 locale。
    return f"python3 - <<'PY'\nPARAMS = {ascii(params)}\n{_OFFICE_PREVIEW_SCRIPT}\nPY"


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

    trusted_cache_key = _trusted_preview_cache_key(source_sha256, source_size, extension)
    effective_cache_root = cache_root or posixpath.join(session_root, ".opencapybox-preview")
    incoming_dir = posixpath.join(effective_cache_root, f".incoming-{uuid.uuid4().hex}")
    snapshot_path = posixpath.join(incoming_dir, f"source{extension}")
    profile_dir = posixpath.join(incoming_dir, "profile")
    command = _office_preview_command(
        {
            "source_path": source_path,
            "extension": extension,
            "cache_root": effective_cache_root,
            "incoming_dir": incoming_dir,
            "snapshot_path": snapshot_path,
            "profile_dir": profile_dir,
            "convert_argv": _office_convert_argv(
                profile_dir=profile_dir,
                output_dir=incoming_dir,
                snapshot_path=snapshot_path,
            ),
            "trusted_cache_key": trusted_cache_key,
            "trusted_size": source_size,
            "max_source_bytes": MAX_OFFICE_PREVIEW_BYTES,
            "max_pdf_bytes": MAX_RENDERED_PDF_BYTES,
            "cache_max_bytes": cache_max_bytes,
            "incoming_stale_seconds": OFFICE_PREVIEW_INCOMING_STALE_SECONDS,
            "lock_stale_seconds": OFFICE_PREVIEW_LOCK_STALE_SECONDS,
            "lock_wait_seconds": OFFICE_PREVIEW_LOCK_WAIT_SECONDS,
        }
    )
    # 请求取消时仍等脚本跑完：脚本自己负责释放锁与清理 scratch，转换结果也会进入缓存。
    execution = await _complete_cleanup_before_cancellation(
        _run_command(
            sandbox,
            command,
            timeout_seconds=OFFICE_PREVIEW_SCRIPT_TIMEOUT_SECONDS,
        )
    )
    # SDK 只在收到 complete 或数值错误事件时给出退出码；None（服务端超时、流中断）不是成功。
    exit_code = execution.exit_code
    if exit_code != 0:
        logger.warning(
            "Office preview script crashed (ext=%s, exit=%s): %s",
            extension,
            exit_code,
            _command_stderr_tail(execution),
        )
        raise FilePreviewUnavailableError("沙箱预览服务不可用")

    payload = json.loads(_command_stdout_text(execution))
    if not payload["ok"]:
        if payload["reason"] == "conversion_failed":
            logger.warning(
                "Office preview conversion failed (ext=%s, exit=%s)",
                extension,
                payload["exit_code"],
            )
        error_type, message = _PREVIEW_FAILURES[payload["reason"]]
        raise error_type(message)

    return RenderedOfficePreview(
        sandbox_path=payload["pdf_path"],
        filename=f"{posixpath.splitext(source_filename)[0]}.pdf",
        cache_key=payload["cache_key"],
        size=payload["size"],
    )


def _command_stdout_text(execution) -> str:
    logs = getattr(execution, "logs", None)
    stdout = getattr(logs, "stdout", None) if logs is not None else None
    if stdout:
        return "".join(str(getattr(item, "text", item)) for item in stdout).strip()
    direct = getattr(execution, "stdout", None)
    return str(direct or "").strip()


def _command_stderr_tail(execution, limit: int = 2000) -> str:
    stderr = execution.logs.stderr
    return "".join(str(item.text) for item in stderr)[-limit:]
