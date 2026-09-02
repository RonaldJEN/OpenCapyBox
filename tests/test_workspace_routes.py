"""Workspace REST authentication boundary and wire-contract tests."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import get_current_user
from src.api.models.database import get_db
from src.api.routes import workspace as workspace_routes
from src.api.services.workspace_service import (
    WorkspaceContent,
    WorkspaceEntryPage,
    WorkspaceError,
    WorkspaceMutationResult,
)


def _entry(
    *,
    entry_id="entry-1",
    user_id="user-1",
    revision=1,
    path="report.md",
    sha256="abc",
    current_version_id="version-1",
):
    now = datetime(2026, 8, 26, 12, 0, 0)
    return SimpleNamespace(
        entry_id=entry_id,
        user_id=user_id,
        parent_id=None,
        parent_key="",
        name=path.rsplit("/", 1)[-1],
        kind="file",
        relative_path=path,
        size_bytes=3,
        mime_type="text/markdown",
        sha256=sha256,
        revision=revision,
        current_version_id=current_version_id,
        status="active",
        created_at=now,
        updated_at=now,
    )


def _mutation(entry=None, *, status="CREATED"):
    return WorkspaceMutationResult(status, entry or _entry(), "mutation-1")


def _version(*, version_id="version-1", checkpoint_kind="web_idle"):
    now = datetime(2026, 8, 26, 12, 0, 0)
    return SimpleNamespace(
        version_id=version_id,
        entry_id="entry-1",
        sequence=2,
        parent_version_id="version-0",
        restored_from_version_id=None,
        sha256="abc",
        size_bytes=3,
        mime_type="text/markdown",
        actor="web",
        state="materialized",
        pinned=False,
        checkpoint_kind=checkpoint_kind,
        created_at=now,
    )


async def _stream_bytes(content: bytes):
    yield content


def _client(monkeypatch, service, *, user_id="user-1"):
    app = FastAPI()
    app.include_router(workspace_routes.router, prefix="/api/workspace")
    app.dependency_overrides[get_current_user] = lambda: user_id
    app.dependency_overrides[get_db] = lambda: SimpleNamespace(rollback=lambda: None)
    monkeypatch.setattr(workspace_routes, "_service", lambda _db: service)
    return TestClient(app)




def test_cross_user_get_is_404_not_an_entry_leak(monkeypatch):
    service = SimpleNamespace(
        get_entry=AsyncMock(
            side_effect=WorkspaceError(404, "ENTRY_NOT_FOUND", "工作区条目不存在")
        )
    )
    client = _client(monkeypatch, service, user_id="user-2")

    response = client.get("/api/workspace/entries/user-1-entry")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ENTRY_NOT_FOUND"
    service.get_entry.assert_awaited_once_with(
        "user-2",
        "user-1-entry",
    )






def test_import_source_revision_conflict_is_412_with_current_revision(monkeypatch):
    service = SimpleNamespace(
        import_session_file=AsyncMock(
            side_effect=WorkspaceError(
                412,
                "SOURCE_REVISION_CONFLICT",
                "源文件已变化",
                extra={"current_revision": "v1:5:20"},
            )
        )
    )
    client = _client(monkeypatch, service)

    response = client.post(
        "/api/workspace/imports/session-file",
        json={
            "session_id": "session-1",
            "source_path": "report.pdf",
            "source_revision": "v1:3:10",
            "destination_parent_id": None,
            "destination_name": "report.pdf",
            "conflict_policy": "fail",
            "idempotency_key": "import-2",
        },
    )

    assert response.status_code == 412
    assert response.json()["detail"]["current_revision"] == "v1:5:20"


def test_cross_user_session_import_is_404(monkeypatch):
    service = SimpleNamespace(
        import_session_file=AsyncMock(
            side_effect=WorkspaceError(404, "SESSION_NOT_FOUND", "会话不存在")
        )
    )
    client = _client(monkeypatch, service, user_id="user-2")
    response = client.post(
        "/api/workspace/imports/session-file",
        json={
            "session_id": "user-1-session",
            "source_path": "secret.pdf",
            "source_revision": "v1:3:10",
            "destination_parent_id": None,
            "destination_name": "secret.pdf",
            "conflict_policy": "fail",
            "idempotency_key": "cross-user-import",
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "SESSION_NOT_FOUND"
    assert service.import_session_file.await_args.args[0] == "user-2"


def test_content_update_requires_if_match_and_forwards_base_version(monkeypatch):
    conflict_entry = _entry(revision=2)
    service = SimpleNamespace(
        get_entry=AsyncMock(return_value=_entry()),
        write_content_auto_merge=AsyncMock(
            side_effect=WorkspaceError(
                409,
                "REVISION_CONFLICT",
                "文件已被修改",
                entry=conflict_entry,
            )
        )
    )
    client = _client(monkeypatch, service)

    missing = client.put("/api/workspace/entries/entry-1/content", content=b"new")
    missing_base = client.put(
        "/api/workspace/entries/entry-1/content",
        content=b"new",
        headers={"If-Match": '"1"'},
    )
    stale = client.put(
        "/api/workspace/entries/entry-1/content",
        content=b"new",
        headers={"If-Match": '"1"', "X-Workspace-Base-Version": "version-1"},
    )

    assert missing.status_code == 428
    assert missing.json()["detail"]["code"] == "PRECONDITION_REQUIRED"
    assert missing_base.status_code == 428
    assert missing_base.json()["detail"]["code"] == "BASE_VERSION_REQUIRED"
    assert stale.status_code == 409
    assert stale.json()["detail"]["entry"]["revision"] == 2
    assert service.write_content_auto_merge.await_args.kwargs["base_version_id"] == "version-1"


def test_upload_route_passes_uploadfile_stream_without_eager_byte_buffer(monkeypatch):
    received = {}

    async def upload_file_stream(user_id, parent_id, name, source, **kwargs):
        received.update(
            user_id=user_id,
            parent_id=parent_id,
            name=name,
            source=source,
            declared_size=kwargs["declared_size"],
        )
        chunks = []
        while True:
            chunk = await source.read(2)
            if not chunk:
                break
            chunks.append(chunk)
        received["content"] = b"".join(chunks)
        return _mutation()

    service = SimpleNamespace(upload_file_stream=upload_file_stream)
    client = _client(monkeypatch, service)

    response = client.post(
        "/api/workspace/uploads",
        files={"file": ("data.bin", b"abcdef", "application/octet-stream")},
        data={"idempotency_key": "upload-1"},
    )

    assert response.status_code == 200
    assert received["content"] == b"abcdef"
    assert received["declared_size"] == 6
    assert received["source"].filename == "data.bin"




def test_fixed_version_content_uses_the_version_resource_route(monkeypatch):
    selected_version = _version(version_id="older-version")
    selected_version.size_bytes = 2
    stream_reader = AsyncMock(return_value=_stream_bytes(b"v1"))
    sandbox = SimpleNamespace(files=SimpleNamespace(read_bytes_stream=stream_reader))
    service = SimpleNamespace(
        open_version_content=AsyncMock(return_value=SimpleNamespace(
            version=selected_version,
            sandbox=sandbox,
            sandbox_path="/home/user/workdir/object/older",
            workspace_root="/home/user/workdir",
            name="report.md",
        )),
    )
    client = _client(monkeypatch, service)

    selected = client.get("/api/workspace/versions/older-version/content?preview=true")

    assert selected.status_code == 200
    assert selected.content == b"v1"
    assert selected.headers["Content-Length"] == "2"
    assert selected.headers["ETag"] == '"older-version"'
    assert selected.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    service.open_version_content.assert_awaited_once_with("user-1", "older-version")




def test_stream_initialization_failure_is_not_retried_as_buffered_read(monkeypatch):
    entry = _entry(path="notes/image.png")
    buffered_reader = AsyncMock(return_value=b"png")
    sandbox = SimpleNamespace(
        files=SimpleNamespace(
            read_bytes_stream=AsyncMock(side_effect=RuntimeError("stream failed")),
            read_bytes=buffered_reader,
        )
    )
    service = SimpleNamespace(
        open_content=AsyncMock(
            return_value=WorkspaceContent(
                entry=entry,
                sandbox=sandbox,
                sandbox_path="/home/user/workdir/notes/image.png",
                workspace_root="/home/user/workdir",
            )
        )
    )
    client = _client(monkeypatch, service)

    response = client.get(
        "/api/workspace/entries/entry-1/content",
        params={"preview": "true"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SANDBOX_READ_FAILED"
    buffered_reader.assert_not_awaited()


def test_immutable_stream_initialization_failures_use_stable_workspace_code(monkeypatch):
    buffered_reader = AsyncMock(return_value=b"bytes")
    sandbox = SimpleNamespace(
        files=SimpleNamespace(
            read_bytes_stream=AsyncMock(side_effect=RuntimeError("stream failed")),
            read_bytes=buffered_reader,
        )
    )
    service = SimpleNamespace(
        open_version_content=AsyncMock(return_value=SimpleNamespace(
            version=_version(),
            sandbox=sandbox,
            sandbox_path="/home/user/workdir/.opencapybox/objects/version/content",
            workspace_root="/home/user/workdir",
            name="report.md",
        )),
    )
    client = _client(monkeypatch, service)

    version_response = client.get("/api/workspace/versions/version-1/content")

    assert version_response.status_code == 503
    assert version_response.json()["detail"]["code"] == "SANDBOX_READ_FAILED"
    buffered_reader.assert_not_awaited()
