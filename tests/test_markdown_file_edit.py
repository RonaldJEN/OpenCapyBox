import base64
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.routes import sessions
from src.api.schemas.session import UpdateSessionFileRequest


def execution(*, exit_code: int = 0, stdout: str = ""):
    return SimpleNamespace(
        exit_code=exit_code,
        error=None,
        logs=SimpleNamespace(stdout=stdout),
    )


def request(content: str = "# 更新", *, size: int = 8) -> UpdateSessionFileRequest:
    return UpdateSessionFileRequest(
        content=content,
        expected_size=size,
        expected_modified="2026-08-26T02:00:00+00:00",
    )


def spreadsheet_request(content: bytes, *, size: int = 8) -> UpdateSessionFileRequest:
    return UpdateSessionFileRequest(
        content_base64=base64.b64encode(content).decode("ascii"),
        expected_size=size,
        expected_modified="2026-08-26T02:00:00+00:00",
    )


def valid_xlsx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            '</Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            '</Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>',
        )
    return output.getvalue()


def disconnected_xlsx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>',
        )
    return output.getvalue()


def db_with_session(*, active_lock=None):
    db = MagicMock()
    first = db.query.return_value.filter.return_value.first
    first.side_effect = [SimpleNamespace(user_id="u1"), active_lock]
    return db


@pytest.mark.asyncio
async def test_markdown_edit_atomically_replaces_current_version():
    db = db_with_session()
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=[
        execution(),
        execution(stdout=json.dumps({"status": "saved", "size": 10, "mtime": 1787709660.25})),
        execution(),
    ])
    service = MagicMock()
    service.get_mount_path.return_value = "/home/user"

    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)),
        patch("src.api.routes.sessions.get_settings", return_value=SimpleNamespace(sse_subscribe_timeout=300)),
    ):
        result = await sessions.update_session_file(
            "s1", "notes/report.md", request(), "u1", db,
        )

    assert result.path == "notes/report.md"
    assert result.name == "report.md"
    assert result.size == 10
    assert result.type == "md"
    written_path, written_content = sandbox.files.write.await_args.args
    assert "/.opencapybox-edit/." in written_path
    assert written_content == "# 更新".encode("utf-8")
    update_command = sandbox.commands.run.await_args_list[1].args[0]
    assert "os.replace(temp, target)" in update_command
    assert "expected_size = 8" in update_command


@pytest.mark.asyncio
async def test_markdown_edit_rejects_stale_file_version():
    db = db_with_session()
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=[
        execution(),
        execution(exit_code=3, stdout='{"status":"conflict"}'),
        execution(),
    ])
    service = MagicMock()
    service.get_mount_path.return_value = "/home/user"

    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)),
        patch("src.api.routes.sessions.get_settings", return_value=SimpleNamespace(sse_subscribe_timeout=300)),
    ):
        with pytest.raises(HTTPException) as exc:
            await sessions.update_session_file("s1", "report.md", request(), "u1", db)

    assert exc.value.status_code == 409
    assert "刷新" in exc.value.detail


@pytest.mark.asyncio
async def test_markdown_edit_rejects_active_agent_run():
    db = db_with_session(active_lock=SimpleNamespace(lock_id="lock-1"))
    with patch(
        "src.api.routes.sessions.get_settings",
        return_value=SimpleNamespace(sse_subscribe_timeout=300),
    ):
        with pytest.raises(HTTPException) as exc:
            await sessions.update_session_file("s1", "report.md", request(), "u1", db)
    assert exc.value.status_code == 409
    assert "Agent 正在使用" in exc.value.detail


@pytest.mark.asyncio
async def test_markdown_edit_rejects_non_markdown_and_oversized_content():
    db = db_with_session()
    with pytest.raises(HTTPException) as unsupported:
        await sessions.update_session_file("s1", "report.html", request(), "u1", db)
    assert unsupported.value.status_code == 415

    db = db_with_session()
    with pytest.raises(HTTPException) as oversized:
        await sessions.update_session_file(
            "s1",
            "report.md",
            request("中" * (2 * 1024 * 1024), size=0),
            "u1",
            db,
        )
    assert oversized.value.status_code == 413


@pytest.mark.asyncio
async def test_spreadsheet_edit_decodes_and_atomically_replaces_xlsx():
    db = db_with_session()
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=[
        execution(),
        execution(stdout=json.dumps({"status": "saved", "size": 12, "mtime": 1787709720.5})),
        execution(),
    ])
    service = MagicMock()
    service.get_mount_path.return_value = "/home/user"
    xlsx_bytes = valid_xlsx_bytes()

    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)),
        patch("src.api.routes.sessions.get_settings", return_value=SimpleNamespace(sse_subscribe_timeout=300)),
    ):
        result = await sessions.update_session_file(
            "s1", "reports/model.xlsx", spreadsheet_request(xlsx_bytes), "u1", db,
        )

    assert result.path == "reports/model.xlsx"
    assert result.type == "xlsx"
    assert sandbox.files.write.await_args.args[1] == xlsx_bytes


@pytest.mark.asyncio
async def test_spreadsheet_edit_preserves_valid_utf8_csv_bytes():
    db = db_with_session()
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=[
        execution(),
        execution(stdout=json.dumps({"status": "saved", "size": 32, "mtime": 1787709720.5})),
        execution(),
    ])
    service = MagicMock()
    service.get_mount_path.return_value = "/home/user"
    csv_bytes = "\ufeff项目;数值\r\n收入;88\r\n".encode("utf-8")

    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)),
        patch("src.api.routes.sessions.get_settings", return_value=SimpleNamespace(sse_subscribe_timeout=300)),
    ):
        result = await sessions.update_session_file(
            "s1", "reports/model.csv", spreadsheet_request(csv_bytes), "u1", db,
        )

    assert result.type == "csv"
    assert sandbox.files.write.await_args.args[1] == csv_bytes


@pytest.mark.asyncio
async def test_spreadsheet_edit_rejects_invalid_container_and_payload_shape():
    db = db_with_session()
    with pytest.raises(HTTPException) as invalid_container:
        await sessions.update_session_file(
            "s1", "report.xlsx", spreadsheet_request(b"not-an-xlsx"), "u1", db,
        )
    assert invalid_container.value.status_code == 422

    db = db_with_session()
    with pytest.raises(HTTPException) as invalid_shape:
        await sessions.update_session_file("s1", "report.csv", request(), "u1", db)
    assert invalid_shape.value.status_code == 422


@pytest.mark.asyncio
async def test_spreadsheet_edit_rejects_disconnected_ooxml_relationship_graph():
    with pytest.raises(HTTPException) as invalid_workbook:
        await sessions.update_session_file(
            "s1",
            "report.xlsx",
            spreadsheet_request(disconnected_xlsx_bytes()),
            "u1",
            db_with_session(),
        )
    assert invalid_workbook.value.status_code == 422
    assert "XLSX" in invalid_workbook.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"PKgarbage",
        b"PK\x03\x04truncated-local-header",
        b"not-an-xlsx",
    ],
)
async def test_spreadsheet_edit_rejects_truncated_xlsx(payload):
    with pytest.raises(HTTPException) as invalid_container:
        await sessions.update_session_file(
            "s1", "report.xlsx", spreadsheet_request(payload), "u1", db_with_session(),
        )
    assert invalid_container.value.status_code == 422
    assert "XLSX" in invalid_container.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"\xc3\x28", b"name,value\nhello,\x00world\n"])
async def test_spreadsheet_edit_rejects_invalid_utf8_csv(payload):
    with pytest.raises(HTTPException) as invalid_csv:
        await sessions.update_session_file(
            "s1", "report.csv", spreadsheet_request(payload), "u1", db_with_session(),
        )
    assert invalid_csv.value.status_code == 422
    assert "CSV" in invalid_csv.value.detail


@pytest.mark.asyncio
async def test_binary_xls_is_read_only():
    with pytest.raises(HTTPException) as unsupported:
        await sessions.update_session_file(
            "s1", "legacy.xls", spreadsheet_request(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), "u1", db_with_session(),
        )
    assert unsupported.value.status_code == 415
