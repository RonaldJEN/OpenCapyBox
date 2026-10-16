import asyncio
import hashlib
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from src.api.routes import sessions
from src.api.services import file_preview_service
from src.api.services.file_preview_service import (
    OFFICE_PREVIEW_SCRIPT_TIMEOUT_SECONDS,
    FilePreviewConversionError,
    FilePreviewSourceNotFoundError,
    FilePreviewTimeoutError,
    FilePreviewTooLargeError,
    FilePreviewUnavailableError,
    FilePreviewUnsupportedError,
    RenderedOfficePreview,
    _complete_cleanup_before_cancellation,
    _preview_cache_key,
    render_office_document_to_pdf,
)
from tests.helpers import make_fake_execution


HEREDOC_PREFIX = "python3 - <<'PY'\n"
HEREDOC_SUFFIX = "\nPY"
FAKE_PDF = b"%PDF-1.7 fake"

# 替身 soffice：记录调用次数，按 mode 产出 PDF、垃圾文件、无输出或指定退出码。
FAKE_SOFFICE = """
import os, sys, time
mode, output_dir, counter = sys.argv[1], sys.argv[2], sys.argv[3]
with open(counter, "a") as handle:
    handle.write("x")
if mode == "slow-ok":
    time.sleep(0.6)
    mode = "ok"
if mode == "ok":
    with open(os.path.join(output_dir, "source.pdf"), "wb") as handle:
        handle.write(b"%PDF-1.7 fake")
elif mode == "garbage":
    with open(os.path.join(output_dir, "source.pdf"), "wb") as handle:
        handle.write(b"not a pdf")
elif mode != "none":
    sys.exit(int(mode))
"""


class _OfficeSandbox:
    """在本机真实执行沙箱脚本的替身，只把 soffice 换成可控的假转换器。"""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "session"
        self.root.mkdir()
        self.cache_root = self.root / ".opencapybox-preview"
        self.counter = tmp_path / "conversions.log"
        self.counter.write_text("")
        self.mode = "ok"
        self.command_count = 0
        self.commands = SimpleNamespace(run=self._run)
        converter = tmp_path / "fake_soffice.py"
        converter.write_text(FAKE_SOFFICE, encoding="utf-8")
        monkeypatch.setattr(
            file_preview_service,
            "_office_convert_argv",
            lambda *, profile_dir, output_dir, snapshot_path: [
                sys.executable,
                str(converter),
                self.mode,
                output_dir,
                str(self.counter),
            ],
        )

    async def _run(self, command, *, opts):
        assert opts.timeout.total_seconds() == OFFICE_PREVIEW_SCRIPT_TIMEOUT_SECONDS
        assert command.startswith(HEREDOC_PREFIX) and command.endswith(HEREDOC_SUFFIX)
        self.command_count += 1
        script = command[len(HEREDOC_PREFIX):-len(HEREDOC_SUFFIX)]
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-"],
            input=script.encode("utf-8"),
            capture_output=True,
        )
        return make_fake_execution(
            stdout_text=completed.stdout.decode("utf-8"),
            exit_code=completed.returncode,
        )

    def write_source(self, name: str, content: bytes = b"docx-bytes") -> None:
        (self.root / name).write_bytes(content)

    async def render(self, name: str, **kwargs) -> RenderedOfficePreview:
        return await render_office_document_to_pdf(
            self,
            source_filename=name,
            source_path=str(self.root / name),
            session_root=str(self.root),
            **kwargs,
        )

    def conversions(self) -> int:
        return len(self.counter.read_text())

    def cache_entries(self) -> list[str]:
        return sorted(os.listdir(self.cache_root))

    def seed_cache(self, cache_key: str, content: bytes = FAKE_PDF, *, age: float = 0):
        directory = self.cache_root / cache_key
        directory.mkdir(parents=True)
        pdf = directory / "source.pdf"
        pdf.write_bytes(content)
        stamp = time.time() - age
        os.utime(pdf, (stamp, stamp))
        return directory


@pytest.fixture
def office_sandbox(tmp_path, monkeypatch):
    return _OfficeSandbox(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_office_preview_converts_once_then_serves_cache_with_one_command_each(office_sandbox):
    office_sandbox.write_source("报告.docx", b"report-v1")

    first = await office_sandbox.render("报告.docx")
    second = await office_sandbox.render("报告.docx")

    expected_key = _preview_cache_key(hashlib.sha256(b"report-v1").hexdigest(), ".docx")
    assert first.cache_key == second.cache_key == expected_key
    assert first.filename == "报告.pdf"
    assert first.size == len(FAKE_PDF)
    with open(first.sandbox_path, "rb") as reader:
        assert reader.read() == FAKE_PDF
    assert office_sandbox.conversions() == 1
    assert office_sandbox.command_count == 2
    assert office_sandbox.cache_entries() == [expected_key]
    assert os.listdir(office_sandbox.cache_root / expected_key) == ["source.pdf"]


@pytest.mark.asyncio
async def test_trusted_workspace_cache_hit_does_not_read_source(office_sandbox):
    digest = "a" * 64
    office_sandbox.seed_cache(_preview_cache_key(digest, ".pptx"))

    rendered = await office_sandbox.render(
        "deck.pptx",
        source_sha256=digest,
        source_size=4096,
    )

    assert rendered.size == len(FAKE_PDF)
    assert office_sandbox.conversions() == 0


@pytest.mark.asyncio
async def test_trusted_cache_miss_rejects_content_that_differs_from_metadata(office_sandbox):
    office_sandbox.write_source("deck.pptx", b"tampered")
    trusted_key = _preview_cache_key("b" * 64, ".pptx")

    with pytest.raises(FilePreviewConversionError, match="版本元数据不一致"):
        await office_sandbox.render(
            "deck.pptx",
            source_sha256="b" * 64,
            source_size=len(b"tampered"),
        )

    assert office_sandbox.conversions() == 0
    assert office_sandbox.cache_entries() == [trusted_key]
    assert os.listdir(office_sandbox.cache_root / trusted_key) == []


@pytest.mark.asyncio
async def test_office_preview_rejects_unsupported_missing_and_oversized_source(
    office_sandbox, monkeypatch
):
    with pytest.raises(FilePreviewUnsupportedError):
        await office_sandbox.render("notes.txt")

    with pytest.raises(FilePreviewSourceNotFoundError):
        await office_sandbox.render("missing.docx")

    monkeypatch.setattr(file_preview_service, "MAX_OFFICE_PREVIEW_BYTES", 4)
    office_sandbox.write_source("huge.docx", b"12345")
    with pytest.raises(FilePreviewTooLargeError, match="50 MiB"):
        await office_sandbox.render("huge.docx")

    assert office_sandbox.conversions() == 0
    assert office_sandbox.cache_entries() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "error_type", "message"),
    [
        ("1", FilePreviewConversionError, "文档转换失败"),
        ("124", FilePreviewTimeoutError, "文档转换超时"),
        ("137", FilePreviewTimeoutError, "文档转换超时"),
        ("127", FilePreviewUnavailableError, "未安装 Office 预览组件"),
        ("none", FilePreviewConversionError, "未生成可用的 PDF"),
        ("garbage", FilePreviewConversionError, "未生成可用的 PDF"),
    ],
)
async def test_office_preview_maps_converter_failures_and_cleans_up(
    office_sandbox, mode, error_type, message
):
    office_sandbox.mode = mode
    office_sandbox.write_source("broken.docx")

    with pytest.raises(error_type, match=message):
        await office_sandbox.render("broken.docx")

    [cache_key] = office_sandbox.cache_entries()
    assert os.listdir(office_sandbox.cache_root / cache_key) == []


@pytest.mark.asyncio
async def test_office_preview_rejects_oversized_rendered_and_cached_pdf(
    office_sandbox, monkeypatch
):
    monkeypatch.setattr(file_preview_service, "MAX_RENDERED_PDF_BYTES", 4)
    office_sandbox.write_source("big.docx", b"fresh")
    with pytest.raises(FilePreviewTooLargeError, match="转换后的 PDF"):
        await office_sandbox.render("big.docx")
    [fresh_key] = office_sandbox.cache_entries()
    assert os.listdir(office_sandbox.cache_root / fresh_key) == []

    office_sandbox.write_source("cached.docx", b"cached")
    cached_key = _preview_cache_key(hashlib.sha256(b"cached").hexdigest(), ".docx")
    cached_dir = office_sandbox.seed_cache(cached_key)
    with pytest.raises(FilePreviewTooLargeError, match="转换后的 PDF"):
        await office_sandbox.render("cached.docx")
    assert os.listdir(cached_dir) == []


@pytest.mark.asyncio
async def test_concurrent_same_content_requests_publish_once(office_sandbox):
    office_sandbox.mode = "slow-ok"
    office_sandbox.write_source("same.docx")

    first, second = await asyncio.gather(
        office_sandbox.render("same.docx"),
        office_sandbox.render("same.docx"),
    )

    assert first.cache_key == second.cache_key
    assert first.size == second.size == len(FAKE_PDF)
    assert office_sandbox.conversions() == 1


@pytest.mark.asyncio
async def test_office_preview_reclaims_stale_lock(office_sandbox):
    office_sandbox.write_source("locked.docx", b"locked")
    cache_key = _preview_cache_key(hashlib.sha256(b"locked").hexdigest(), ".docx")
    lock = office_sandbox.cache_root / cache_key / ".lock"
    lock.mkdir(parents=True)
    stale = time.time() - file_preview_service.OFFICE_PREVIEW_LOCK_STALE_SECONDS - 5
    os.utime(lock, (stale, stale))

    rendered = await office_sandbox.render("locked.docx")

    assert rendered.cache_key == cache_key
    assert office_sandbox.conversions() == 1
    assert os.listdir(office_sandbox.cache_root / cache_key) == ["source.pdf"]


@pytest.mark.asyncio
async def test_office_preview_self_heals_only_stale_incoming_directories(office_sandbox):
    stale = time.time() - file_preview_service.OFFICE_PREVIEW_INCOMING_STALE_SECONDS - 5
    stale_incoming = office_sandbox.cache_root / f".incoming-{'1' * 32}"
    fresh_incoming = office_sandbox.cache_root / f".incoming-{'2' * 32}"
    unrelated = office_sandbox.cache_root / ".incoming-notes"
    for directory in (stale_incoming, fresh_incoming, unrelated):
        (directory / "profile").mkdir(parents=True)
    for directory in (stale_incoming, unrelated):
        os.utime(directory, (stale, stale))
    office_sandbox.write_source("report.docx")

    await office_sandbox.render("report.docx")

    assert not stale_incoming.exists()
    assert fresh_incoming.exists()
    assert unrelated.exists()


@pytest.mark.asyncio
async def test_office_preview_prunes_oldest_complete_cache_entries(office_sandbox):
    oldest = office_sandbox.seed_cache("a" * 64, age=300)
    newer = office_sandbox.seed_cache("b" * 64, age=200)
    locked = office_sandbox.seed_cache("c" * 64, age=400)
    (locked / ".lock").mkdir()
    office_sandbox.write_source("report.docx")

    rendered = await office_sandbox.render(
        "report.docx",
        cache_max_bytes=2 * len(FAKE_PDF),
    )

    assert not oldest.exists()
    assert newer.exists()
    assert locked.exists()
    assert (office_sandbox.cache_root / rendered.cache_key / "source.pdf").exists()


@pytest.mark.asyncio
async def test_office_preview_script_crash_maps_to_unavailable(office_sandbox, monkeypatch):
    monkeypatch.setattr(
        file_preview_service,
        "_office_convert_argv",
        lambda **_kwargs: ["opencapybox-missing-timeout-binary"],
    )
    office_sandbox.write_source("report.docx")

    with pytest.raises(FilePreviewUnavailableError, match="沙箱预览服务不可用"):
        await office_sandbox.render("report.docx")


@pytest.mark.asyncio
async def test_office_preview_maps_sandbox_transport_failure_to_unavailable():
    sandbox = MagicMock()
    sandbox.commands.run = AsyncMock(side_effect=ConnectionError("execd down"))

    with pytest.raises(FilePreviewUnavailableError, match="沙箱预览服务不可用"):
        await render_office_document_to_pdf(
            sandbox,
            source_filename="report.docx",
            source_path="/home/user/sessions/s1/report.docx",
            session_root="/home/user/sessions/s1",
        )


@pytest.mark.asyncio
async def test_office_preview_missing_exit_code_maps_to_unavailable():
    # SDK 未收到 complete 事件（服务端超时、流中断）时 exit_code 为 None，且没有 JSON 输出。
    sandbox = MagicMock()
    sandbox.commands.run = AsyncMock(
        return_value=make_fake_execution(stdout_text="", exit_code=None)
    )

    with pytest.raises(FilePreviewUnavailableError, match="沙箱预览服务不可用"):
        await render_office_document_to_pdf(
            sandbox,
            source_filename="report.docx",
            source_path="/home/user/sessions/s1/report.docx",
            session_root="/home/user/sessions/s1",
        )


@pytest.mark.asyncio
async def test_office_preview_cancellation_waits_for_sandbox_script():
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def run(_command, *, opts):
        started.set()
        await release.wait()
        completed.set()
        return make_fake_execution(stdout_text='{"ok":false,"reason":"busy"}')

    sandbox = MagicMock()
    sandbox.commands.run = run
    request = asyncio.create_task(
        render_office_document_to_pdf(
            sandbox,
            source_filename="report.docx",
            source_path="/home/user/sessions/s1/report.docx",
            session_root="/home/user/sessions/s1",
        )
    )
    await started.wait()
    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert completed.is_set()


@pytest.mark.asyncio
async def test_office_cleanup_finishes_before_propagating_request_cancellation():
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def cleanup_operation():
        started.set()
        try:
            await release.wait()
        finally:
            completed.set()

    request = asyncio.create_task(
        _complete_cleanup_before_cancellation(cleanup_operation())
    )
    await started.wait()
    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert completed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_path", "expected_cache_control"),
    [
        ("report.md", "no-store"),
        (
            ".assistant-artifacts/round-1/ref/report.md",
            "private, max-age=31536000, immutable",
        ),
    ],
)
async def test_download_file_defines_cache_policy_in_download_scope(
    file_path, expected_cache_control
):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock(
        id="s1", user_id="u1"
    )
    sandbox = MagicMock()

    async def chunks():
        yield b"# report"

    sandbox.files.read_bytes_stream = AsyncMock(return_value=chunks())
    sandbox_service = MagicMock()
    sandbox_service.get_mount_path.return_value = "/home/user"
    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=sandbox_service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)),
    ):
        response = await sessions.download_file(
            "s1", file_path, "u1", True, None, db
        )

    assert isinstance(response, StreamingResponse)
    assert response.headers["cache-control"] == expected_cache_control


@pytest.mark.asyncio
async def test_download_file_pdf_render_returns_derived_pdf_without_reading_source():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock(id="s1", user_id="u1")
    sandbox = MagicMock()
    sandbox.files.read_bytes = AsyncMock(return_value=b"%PDF-preview")
    sandbox.files.read_bytes_stream = None
    sandbox_service = MagicMock()
    sandbox_service.get_mount_path.return_value = "/home/user"
    rendered = RenderedOfficePreview(
        sandbox_path="/home/user/sessions/s1/.opencapybox-preview/key/source.pdf",
        filename="report.pdf",
        cache_key="abcdef0123456789",
        size=len(b"%PDF-preview"),
    )
    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=sandbox_service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)),
        patch("src.api.routes.sessions.render_office_document_to_pdf", new=AsyncMock(return_value=rendered)) as converter,
    ):
        response = await sessions.download_file("s1", "report.docx", "u1", True, "pdf", db)
    assert response.body == b"%PDF-preview"
    assert response.media_type == "application/pdf"
    converter.assert_awaited_once_with(
        sandbox,
        source_filename="report.docx",
        source_path="/home/user/sessions/s1/report.docx",
        session_root="/home/user/sessions/s1",
        cache_max_bytes=512 * 1024 * 1024,
    )
    assert sandbox.files.read_bytes.await_count == 1


@pytest.mark.asyncio
async def test_download_file_streams_derived_pdf_when_supported():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock(id="s1", user_id="u1")
    sandbox = MagicMock()
    sandbox.files.read_bytes = AsyncMock()

    async def chunks():
        yield b"%PDF"

    sandbox.files.read_bytes_stream = AsyncMock(return_value=chunks())
    sandbox_service = MagicMock()
    sandbox_service.get_mount_path.return_value = "/home/user"
    rendered = RenderedOfficePreview(
        sandbox_path="/home/user/sessions/s1/.opencapybox-preview/key/source.pdf",
        filename="deck.pdf",
        cache_key="abcdef0123456789",
        size=4,
    )
    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=sandbox_service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)),
        patch("src.api.routes.sessions.render_office_document_to_pdf", new=AsyncMock(return_value=rendered)),
    ):
        response = await sessions.download_file("s1", "deck.pptx", "u1", True, "pdf", db)
    assert isinstance(response, StreamingResponse)
    sandbox.files.read_bytes.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (FilePreviewSourceNotFoundError("missing"), 404),
        (FilePreviewTooLargeError("large"), 413),
        (FilePreviewUnsupportedError("unsupported"), 415),
        (FilePreviewConversionError("corrupt"), 422),
        (FilePreviewUnavailableError("unavailable"), 503),
        (FilePreviewTimeoutError("timeout"), 504),
    ],
)
async def test_download_file_maps_preview_error_taxonomy(error, status_code):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock(id="s1", user_id="u1")
    sandbox = MagicMock()
    sandbox_service = MagicMock()
    sandbox_service.get_mount_path.return_value = "/home/user"
    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=sandbox_service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)),
        patch("src.api.routes.sessions.render_office_document_to_pdf", new=AsyncMock(side_effect=error)),
    ):
        with pytest.raises(HTTPException) as exc:
            await sessions.download_file("s1", "report.docx", "u1", True, "pdf", db)
    assert exc.value.status_code == status_code


@pytest.mark.asyncio
async def test_download_file_rejects_render_without_preview():
    with pytest.raises(HTTPException) as exc:
        await sessions.download_file("s1", "report.docx", "u1", False, "pdf", MagicMock())
    assert exc.value.status_code == 400
