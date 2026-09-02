"""Authenticated REST API for the user's persistent workspace."""

from __future__ import annotations

import logging
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.api.config import get_settings
from src.api.models.database import get_db
from src.api.models.workspace import WorkspaceEntry, WorkspaceFileVersion
from src.api.schemas.workspace import (
    WorkspaceCheckpointRequest,
    WorkspaceDirectoryCreateRequest,
    WorkspaceEntryListResponse,
    WorkspaceEntryPatchRequest,
    WorkspaceEntryResponse,
    WorkspaceFileCreateRequest,
    WorkspaceMutationResponse,
    WorkspaceSessionImportRequest,
    WorkspaceVersionResponse,
    WorkspaceDeleteRequest,
    WorkspaceDeleteResponse,
    WorkspaceVersionRestoreRequest,
)
from src.api.services.workspace_service import (
    WorkspaceError,
    WorkspaceMutationResult,
    WorkspaceDeleteResult,
    WorkspaceService,
)
from src.api.services.file_preview_service import (
    FilePreviewConversionError,
    FilePreviewSourceNotFoundError,
    FilePreviewTimeoutError,
    FilePreviewTooLargeError,
    FilePreviewUnavailableError,
    FilePreviewUnsupportedError,
    render_office_document_to_pdf,
)


router = APIRouter()
logger = logging.getLogger(__name__)


def _entry_response(entry: WorkspaceEntry) -> WorkspaceEntryResponse:
    return WorkspaceEntryResponse(
        entry_id=entry.entry_id,
        parent_id=entry.parent_id,
        name=entry.name,
        kind=entry.kind,
        path=entry.relative_path,
        size_bytes=int(entry.size_bytes or 0),
        mime_type=entry.mime_type,
        sha256=entry.sha256,
        revision=int(entry.revision),
        current_version_id=getattr(entry, "current_version_id", None),
        tree_revision=int(getattr(entry, "tree_revision", 1) or 1),
        status=entry.status,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _mutation_response(result: WorkspaceMutationResult) -> WorkspaceMutationResponse:
    return WorkspaceMutationResponse(
        status=result.status,
        entry=_entry_response(result.entry),
        mutation_id=result.mutation_id,
        auto_merged=result.auto_merged,
    )


def _delete_response(result: WorkspaceDeleteResult) -> WorkspaceDeleteResponse:
    return WorkspaceDeleteResponse(
        mutation_id=result.mutation_id,
        affected_entry_ids=list(result.affected_entry_ids),
        root_count=len(result.roots),
        entry_count=len(result.affected_entry_ids),
    )


def _version_response(version: WorkspaceFileVersion) -> WorkspaceVersionResponse:
    return WorkspaceVersionResponse(
        version_id=version.version_id,
        entry_id=version.entry_id,
        sequence=int(version.sequence),
        parent_version_id=version.parent_version_id,
        restored_from_version_id=version.restored_from_version_id,
        sha256=version.sha256,
        size_bytes=int(version.size_bytes or 0),
        mime_type=version.mime_type,
        actor=version.actor,
        state=version.state,
        pinned=bool(version.pinned),
        checkpoint_kind=version.checkpoint_kind,
        created_at=version.created_at,
    )


def _raise_workspace_error(exc: WorkspaceError) -> None:
    detail: dict[str, object] = {"code": exc.code, "message": exc.message, **exc.extra}
    if exc.entry is not None:
        detail["entry"] = _entry_response(exc.entry).model_dump(mode="json")
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def _service(db: DBSession) -> WorkspaceService:
    return WorkspaceService(db)


@router.get("/entries", response_model=WorkspaceEntryListResponse)
async def list_workspace_entries(
    parent_id: str | None = Query(None),
    q: str | None = Query(None, max_length=255),
    cursor: str | None = Query(None, max_length=100),
    limit: int = Query(100, ge=1, le=200),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        page = await _service(db).list_entries(
            user_id,
            parent_id=parent_id,
            q=q,
            cursor=cursor,
            limit=limit,
        )
        return WorkspaceEntryListResponse(
            items=[_entry_response(item) for item in page.items],
            next_cursor=page.next_cursor,
            workspace_revision=page.workspace_revision,
        )
    except WorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/directories", response_model=WorkspaceMutationResponse)
async def create_workspace_directory(
    payload: WorkspaceDirectoryCreateRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        result = await _service(db).create_directory(
            user_id,
            payload.parent_id,
            payload.name,
            idempotency_key=payload.idempotency_key,
        )
        return _mutation_response(result)
    except WorkspaceError as exc:
        db.rollback()
        _raise_workspace_error(exc)


@router.post("/files", response_model=WorkspaceMutationResponse)
async def create_workspace_file(
    payload: WorkspaceFileCreateRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        result = await _service(db).create_file(
            user_id,
            payload.parent_id,
            payload.name,
            file_type=payload.file_type,
            idempotency_key=payload.idempotency_key,
        )
        return _mutation_response(result)
    except WorkspaceError as exc:
        db.rollback()
        _raise_workspace_error(exc)


@router.post("/uploads", response_model=WorkspaceMutationResponse)
async def upload_workspace_file(
    file: UploadFile = File(...),
    parent_id: str | None = Form(None),
    idempotency_key: str | None = Form(None),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    service = _service(db)
    try:
        result = await service.upload_file_stream(
            user_id,
            parent_id or None,
            file.filename or "uploaded_file",
            file,
            declared_size=getattr(file, "size", None),
            idempotency_key=idempotency_key or None,
        )
        return _mutation_response(result)
    except WorkspaceError as exc:
        db.rollback()
        _raise_workspace_error(exc)


@router.get("/entries/{entry_id}", response_model=WorkspaceEntryResponse)
async def get_workspace_entry(
    entry_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        entry = await _service(db).get_entry(
            user_id,
            entry_id,
        )
        return _entry_response(entry)
    except WorkspaceError as exc:
        _raise_workspace_error(exc)


@router.get("/entries/{entry_id}/versions", response_model=list[WorkspaceVersionResponse])
async def list_workspace_entry_versions(
    entry_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        versions = await _service(db).list_versions(user_id, entry_id)
        return [_version_response(item) for item in versions]
    except WorkspaceError as exc:
        _raise_workspace_error(exc)


@router.post("/entries/{entry_id}/checkpoint", response_model=WorkspaceVersionResponse)
async def checkpoint_workspace_entry(
    entry_id: str,
    payload: WorkspaceCheckpointRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        version = await _service(db).checkpoint_entry(
            user_id,
            entry_id,
            expected_revision=payload.expected_revision,
            version_id=payload.version_id,
            checkpoint_kind=payload.checkpoint_kind,
        )
        return _version_response(version)
    except WorkspaceError as exc:
        db.rollback()
        _raise_workspace_error(exc)


@router.get("/versions/{version_id}/content")
async def get_workspace_version_content(
    version_id: str,
    preview: bool = Query(False),
    render: Literal["pdf"] | None = Query(None),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    if render is not None and not preview:
        raise HTTPException(status_code=400, detail="派生渲染仅可用于预览")
    try:
        opened = await _service(db).open_version_content(user_id, version_id)
        if render == "pdf":
            rendered = await render_office_document_to_pdf(
                opened.sandbox,
                source_filename=opened.name,
                source_path=opened.sandbox_path,
                session_root=opened.workspace_root,
                cache_max_bytes=int(get_settings().workspace_preview_cache_bytes),
                cache_root=f"{opened.workspace_root}/.opencapybox/derived/office",
                source_sha256=opened.version.sha256,
                source_size=int(opened.version.size_bytes or 0),
            )
            rendered_stream = await opened.sandbox.files.read_bytes_stream(
                rendered.sandbox_path,
                chunk_size=64 * 1024,
            )
            return StreamingResponse(
                rendered_stream,
                media_type="application/pdf",
                headers={
                    "ETag": f'"{opened.version.version_id}-pdf"',
                    "Content-Disposition": (
                        f"inline; filename*=UTF-8''{quote(rendered.filename)}"
                    ),
                    "Content-Length": str(int(rendered.size)),
                    "Cache-Control": "private, max-age=31536000, immutable",
                    "X-OpenCapyBox-Preview-Cache": rendered.cache_key[:16],
                },
            )
        stream = await opened.sandbox.files.read_bytes_stream(
            opened.sandbox_path,
            chunk_size=64 * 1024,
        )
        return StreamingResponse(
            stream,
            media_type=opened.version.mime_type or "application/octet-stream",
            headers={
                "ETag": f'"{opened.version.version_id}"',
                "Content-Length": str(int(opened.version.size_bytes or 0)),
                "Content-Disposition": (
                    "inline" if preview else "attachment"
                ) + (
                    f"; filename*=UTF-8''{quote(opened.name)}"
                ),
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )
    except WorkspaceError as exc:
        _raise_workspace_error(exc)
    except FilePreviewUnsupportedError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except FilePreviewTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except FilePreviewSourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FilePreviewTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except FilePreviewConversionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FilePreviewUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SANDBOX_READ_FAILED", "message": "无法读取工作区文件"},
        ) from exc


@router.post("/entries/{entry_id}/restore", response_model=WorkspaceMutationResponse)
async def restore_workspace_entry_version(
    entry_id: str,
    payload: WorkspaceVersionRestoreRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        result = await _service(db).restore_version(
            user_id,
            entry_id,
            payload.version_id,
            expected_revision=payload.expected_revision,
            idempotency_key=payload.idempotency_key,
        )
        return _mutation_response(result)
    except WorkspaceError as exc:
        db.rollback()
        _raise_workspace_error(exc)


@router.get("/entries/{entry_id}/content")
async def get_workspace_entry_content(
    entry_id: str,
    preview: bool = Query(False),
    render: Literal["pdf"] | None = Query(None),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    if render is not None and not preview:
        raise HTTPException(status_code=400, detail="派生渲染仅可用于预览")
    try:
        opened = await _service(db).open_content(user_id, entry_id)
        content_identity = str(int(opened.entry.revision))
        version_headers = {
            "X-Workspace-Version": opened.entry.current_version_id,
            "X-Workspace-Revision": str(int(opened.entry.revision)),
        }
        if render == "pdf":
            rendered = await render_office_document_to_pdf(
                opened.sandbox,
                source_filename=opened.entry.name,
                source_path=opened.sandbox_path,
                session_root=opened.workspace_root,
                cache_max_bytes=int(get_settings().workspace_preview_cache_bytes),
                cache_root=f"{opened.workspace_root}/.opencapybox/derived/office",
                source_sha256=opened.entry.sha256,
                source_size=int(opened.entry.size_bytes or 0),
            )
            rendered_headers = {
                "ETag": f'"{content_identity}-pdf"',
                **version_headers,
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(rendered.filename)}",
                "Content-Length": str(int(rendered.size)),
                "X-OpenCapyBox-Preview-Cache": rendered.cache_key[:16],
            }
            rendered_stream = await opened.sandbox.files.read_bytes_stream(
                rendered.sandbox_path,
                chunk_size=64 * 1024,
            )
            return StreamingResponse(
                rendered_stream,
                media_type="application/pdf",
                headers=rendered_headers,
            )
        headers = {
            "ETag": f'"{content_identity}"',
            **version_headers,
            "Content-Disposition": (
                "inline" if preview else "attachment"
            ) + f"; filename*=UTF-8''{quote(opened.entry.name)}",
            "Content-Length": str(int(opened.entry.size_bytes or 0)),
        }
        stream = await opened.sandbox.files.read_bytes_stream(
            opened.sandbox_path,
            chunk_size=64 * 1024,
        )
        return StreamingResponse(
            stream,
            media_type=opened.entry.mime_type or "application/octet-stream",
            headers=headers,
        )
    except WorkspaceError as exc:
        _raise_workspace_error(exc)
    except FilePreviewUnsupportedError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except FilePreviewTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except FilePreviewSourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FilePreviewTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except FilePreviewConversionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FilePreviewUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SANDBOX_READ_FAILED", "message": "无法读取工作区文件"},
        ) from exc


@router.get("/content")
async def get_workspace_content_by_path(
    path: str = Query(..., min_length=1, max_length=2000),
    preview: bool = Query(False),
    render: Literal["pdf"] | None = Query(None),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """Resolve Markdown-relative resources through authoritative active metadata."""
    try:
        entry = await _service(db).get_entry_by_path(user_id, path)
    except WorkspaceError as exc:
        _raise_workspace_error(exc)
    return await get_workspace_entry_content(
        entry.entry_id,
        preview,
        render,
        user_id,
        db,
    )


@router.put("/entries/{entry_id}/content", response_model=WorkspaceMutationResponse)
async def update_workspace_entry_content(
    entry_id: str,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    base_version_id: str | None = Header(None, alias="X-Workspace-Base-Version"),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail={"code": "PRECONDITION_REQUIRED", "message": "保存文件必须携带 If-Match 版本"},
        )
    try:
        service = _service(db)
        entry = await service.get_entry(user_id, entry_id)
        if entry.current_version_id and not base_version_id:
            raise WorkspaceError(
                428,
                "BASE_VERSION_REQUIRED",
                "已有版本的文件保存必须携带编辑基线",
                entry=entry,
            )
        extension = "." + entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else ""
        if extension in {".md", ".markdown", ".txt"}:
            edit_limit = 5 * 1024 * 1024
        elif extension in {".csv", ".xlsx"}:
            edit_limit = 20 * 1024 * 1024
        else:
            raise WorkspaceError(415, "UNSUPPORTED_EDIT_TYPE", "当前文件类型不支持在线编辑")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > edit_limit:
                raise WorkspaceError(413, "EDIT_TOO_LARGE", "文件超过在线编辑大小限制")
        result = await service.write_content_auto_merge(
            user_id,
            entry_id,
            bytes(content),
            if_match,
            base_version_id=base_version_id,
            idempotency_key=idempotency_key,
        )
        return _mutation_response(result)
    except WorkspaceError as exc:
        db.rollback()
        _raise_workspace_error(exc)


@router.patch("/entries/{entry_id}", response_model=WorkspaceMutationResponse)
async def patch_workspace_entry(
    entry_id: str,
    payload: WorkspaceEntryPatchRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        current = await _service(db).get_entry(user_id, entry_id)
        target_parent_id = payload.parent_id if "parent_id" in payload.model_fields_set else current.parent_id
        result = await _service(db).move_entry(
            user_id,
            entry_id,
            parent_id=target_parent_id,
            name=payload.name,
            expected_revision=payload.expected_revision,
            idempotency_key=payload.idempotency_key,
        )
        return _mutation_response(result)
    except WorkspaceError as exc:
        db.rollback()
        _raise_workspace_error(exc)


@router.post("/entries/delete-batch", response_model=WorkspaceDeleteResponse)
async def delete_workspace_entries_batch(
    payload: WorkspaceDeleteRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        return _delete_response(await _service(db).delete_entries_batch(
            user_id,
            ((item.entry_id, item.expected_revision) for item in payload.items),
            idempotency_key=payload.idempotency_key,
        ))
    except WorkspaceError as exc:
        db.rollback()
        _raise_workspace_error(exc)


@router.post("/imports/session-file", response_model=WorkspaceMutationResponse)
async def import_session_file_to_workspace(
    payload: WorkspaceSessionImportRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    try:
        result = await _service(db).import_session_file(
            user_id,
            session_id=payload.session_id,
            source_path=payload.source_path,
            source_revision=payload.source_revision,
            destination_parent_id=payload.destination_parent_id,
            destination_name=payload.destination_name,
            conflict_policy=payload.conflict_policy,
            expected_destination_revision=payload.expected_destination_revision,
            idempotency_key=payload.idempotency_key,
        )
        return _mutation_response(result)
    except WorkspaceError as exc:
        db.rollback()
        _raise_workspace_error(exc)
