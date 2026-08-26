import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from src.api.routes import sessions
from src.api.services.file_preview_service import (
    FilePreviewConversionError,
    FilePreviewSourceNotFoundError,
    FilePreviewTimeoutError,
    FilePreviewTooLargeError,
    FilePreviewUnavailableError,
    FilePreviewUnsupportedError,
    MAX_RENDERED_PDF_BYTES,
    RenderedOfficePreview,
    render_office_document_to_pdf,
)
from tests.helpers import make_fake_execution


CONTENT_DIGEST = "a" * 64


def _snapshot_execution(*, size: int = 12, reason: str | None = None):
    payload = (
        {"ok": False, "reason": reason}
        if reason
        else {"ok": True, "size": size, "sha256": CONTENT_DIGEST}
    )
    return make_fake_execution(stdout_text=json.dumps(payload, separators=(",", ":")))


def _sandbox_with_commands(*executions):
    sandbox = MagicMock()
    sandbox.commands.run = AsyncMock(side_effect=list(executions))
    sandbox.files.read_bytes = AsyncMock()
    sandbox.files.read_bytes_stream = None
    return sandbox


@pytest.mark.asyncio
async def test_office_preview_reuses_valid_cached_pdf():
    sandbox = _sandbox_with_commands(
        _snapshot_execution(),
        make_fake_execution(stdout_text="4096"),
        make_fake_execution(stdout_text="", exit_code=0),
    )
    rendered = await render_office_document_to_pdf(
        sandbox,
        source_filename="报告.docx",
        source_path="/home/user/sessions/s1/报告.docx",
        session_root="/home/user/sessions/s1",
    )
    assert rendered.filename == "报告.pdf"
    assert rendered.sandbox_path.endswith("/source.pdf")
    assert rendered.size == 4096
    commands = [call.args[0] for call in sandbox.commands.run.await_args_list]
    assert not any("soffice" in command for command in commands)
    assert all(call.kwargs.get("opts") is not None for call in sandbox.commands.run.await_args_list)


@pytest.mark.asyncio
async def test_office_preview_converts_in_unique_scratch_then_atomically_publishes():
    sandbox = _sandbox_with_commands(
        _snapshot_execution(),
        make_fake_execution(stdout_text="", exit_code=1),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="OWNER", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=1),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="/usr/bin/soffice", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="8192", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="8192", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
    )
    rendered = await render_office_document_to_pdf(
        sandbox,
        source_filename="deck.pptx",
        source_path="/home/user/sessions/s1/deck.pptx",
        session_root="/home/user/sessions/s1",
    )
    assert "/.opencapybox-preview/" in rendered.sandbox_path
    assert rendered.filename == "deck.pdf"
    assert rendered.size == 8192
    commands = [call.args[0] for call in sandbox.commands.run.await_args_list]
    conversion = next(command for command in commands if "--convert-to" in command)
    assert "timeout -k 5 90 soffice" in conversion
    assert ".incoming-" in conversion
    assert any(command.startswith("mv -f --") for command in commands)
    assert any("head -c 5" in command for command in commands)


@pytest.mark.asyncio
async def test_office_preview_rejects_unsupported_and_oversized_source():
    with pytest.raises(FilePreviewUnsupportedError):
        await render_office_document_to_pdf(
            _sandbox_with_commands(),
            source_filename="notes.txt",
            source_path="/home/user/sessions/s1/notes.txt",
            session_root="/home/user/sessions/s1",
        )

    sandbox = _sandbox_with_commands(
        _snapshot_execution(reason="too_large"),
        make_fake_execution(stdout_text="", exit_code=0),
    )
    with pytest.raises(FilePreviewTooLargeError, match="50 MiB"):
        await render_office_document_to_pdf(
            sandbox,
            source_filename="huge.docx",
            source_path="/home/user/sessions/s1/huge.docx",
            session_root="/home/user/sessions/s1",
        )


@pytest.mark.asyncio
async def test_office_preview_maps_missing_source_separately():
    sandbox = _sandbox_with_commands(
        _snapshot_execution(reason="missing"),
        make_fake_execution(stdout_text="", exit_code=0),
    )
    with pytest.raises(FilePreviewSourceNotFoundError):
        await render_office_document_to_pdf(
            sandbox,
            source_filename="missing.docx",
            source_path="/home/user/sessions/s1/missing.docx",
            session_root="/home/user/sessions/s1",
        )


@pytest.mark.asyncio
async def test_office_preview_reports_conversion_failure_and_cleans_lock():
    sandbox = _sandbox_with_commands(
        _snapshot_execution(),
        make_fake_execution(stdout_text="", exit_code=1),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="OWNER", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=1),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="/usr/bin/soffice", exit_code=0),
        make_fake_execution(stdout_text="secret diagnostic", exit_code=1),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
    )
    with pytest.raises(FilePreviewConversionError, match="文档转换失败") as exc:
        await render_office_document_to_pdf(
            sandbox,
            source_filename="broken.docx",
            source_path="/home/user/sessions/s1/broken.docx",
            session_root="/home/user/sessions/s1",
        )
    assert "secret diagnostic" not in str(exc.value)
    commands = [call.args[0] for call in sandbox.commands.run.await_args_list]
    assert commands[-1].startswith("rmdir --")


@pytest.mark.asyncio
async def test_office_preview_maps_shell_timeout():
    sandbox = _sandbox_with_commands(
        _snapshot_execution(),
        make_fake_execution(stdout_text="", exit_code=1),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="OWNER", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=1),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="/usr/bin/soffice", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=124),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
    )
    with pytest.raises(FilePreviewTimeoutError):
        await render_office_document_to_pdf(
            sandbox,
            source_filename="slow.pptx",
            source_path="/home/user/sessions/s1/slow.pptx",
            session_root="/home/user/sessions/s1",
        )


@pytest.mark.asyncio
async def test_office_preview_rejects_oversized_cached_pdf():
    sandbox = _sandbox_with_commands(
        _snapshot_execution(),
        make_fake_execution(stdout_text=str(MAX_RENDERED_PDF_BYTES + 1)),
        make_fake_execution(stdout_text="", exit_code=0),
        make_fake_execution(stdout_text="", exit_code=0),
    )
    with pytest.raises(FilePreviewTooLargeError, match="转换后的 PDF"):
        await render_office_document_to_pdf(
            sandbox,
            source_filename="report.docx",
            source_path="/home/user/sessions/s1/report.docx",
            session_root="/home/user/sessions/s1",
        )


class _ConcurrentCommandRunner:
    def __init__(self):
        self.locked = False
        self.ready = False
        self.released = asyncio.Event()
        self.conversions = 0

    async def __call__(self, command, **_kwargs):
        if "python3 - <<'PY'" in command:
            return _snapshot_execution()
        if "head -c 5" in command:
            if ".incoming-" in command:
                return make_fake_execution(stdout_text="8192")
            return make_fake_execution(
                stdout_text="8192" if self.ready else "",
                exit_code=0 if self.ready else 1,
            )
        if command.startswith("mkdir -p"):
            return make_fake_execution(stdout_text="")
        if "echo OWNER" in command:
            if self.locked:
                return make_fake_execution(stdout_text="WAITER")
            self.locked = True
            return make_fake_execution(stdout_text="OWNER")
        if "while test -d" in command:
            await self.released.wait()
            return make_fake_execution(stdout_text="")
        if command.startswith("command -v soffice"):
            return make_fake_execution(stdout_text="/usr/bin/soffice")
        if "soffice" in command:
            self.conversions += 1
            await asyncio.sleep(0.02)
            return make_fake_execution(stdout_text="")
        if command.startswith("mv -f --"):
            self.ready = True
            return make_fake_execution(stdout_text="")
        if command.startswith("rmdir --"):
            self.locked = False
            self.released.set()
            return make_fake_execution(stdout_text="")
        return make_fake_execution(stdout_text="")


@pytest.mark.asyncio
async def test_concurrent_same_content_requests_publish_once():
    runner = _ConcurrentCommandRunner()
    sandbox = MagicMock()
    sandbox.commands.run = runner.__call__
    first, second = await asyncio.gather(
        render_office_document_to_pdf(
            sandbox,
            source_filename="same.docx",
            source_path="/home/user/sessions/s1/same.docx",
            session_root="/home/user/sessions/s1",
        ),
        render_office_document_to_pdf(
            sandbox,
            source_filename="same.docx",
            source_path="/home/user/sessions/s1/same.docx",
            session_root="/home/user/sessions/s1",
        ),
    )
    assert first.cache_key == second.cache_key
    assert runner.conversions == 1


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
