"""Public request and response contracts for the persistent workspace API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


WorkspaceEntryKind = Literal["file", "directory"]
WorkspaceEntryStatus = Literal["active"]
WorkspaceMutationStatus = Literal[
    "CREATED",
    "UPDATED",
    "NO_CHANGE",
    "MOVED",
]


class WorkspaceEntryResponse(BaseModel):
    entry_id: str
    parent_id: str | None
    name: str
    kind: WorkspaceEntryKind
    path: str
    size_bytes: int
    mime_type: str | None
    sha256: str | None
    revision: int
    current_version_id: str | None = None
    tree_revision: int = 1
    status: WorkspaceEntryStatus
    created_at: datetime
    updated_at: datetime


class WorkspaceEntryListResponse(BaseModel):
    items: list[WorkspaceEntryResponse]
    next_cursor: str | None = None
    workspace_revision: int


class WorkspaceMutationResponse(BaseModel):
    status: WorkspaceMutationStatus
    entry: WorkspaceEntryResponse
    mutation_id: str
    auto_merged: bool = False


class WorkspaceDirectoryCreateRequest(BaseModel):
    parent_id: str | None = None
    name: str = Field(..., min_length=1, max_length=255)
    idempotency_key: str | None = Field(None, min_length=1, max_length=128)


class WorkspaceFileCreateRequest(BaseModel):
    parent_id: str | None = None
    name: str = Field(..., min_length=1, max_length=255)
    file_type: Literal["markdown", "xlsx"]
    idempotency_key: str | None = Field(None, min_length=1, max_length=128)


class WorkspaceEntryPatchRequest(BaseModel):
    parent_id: str | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    expected_revision: int = Field(..., ge=1)
    idempotency_key: str | None = Field(None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_move_or_rename(self):
        if "parent_id" not in self.model_fields_set and "name" not in self.model_fields_set:
            raise ValueError("必须提供 parent_id 或 name")
        return self


class WorkspaceDeleteItem(BaseModel):
    entry_id: str = Field(..., min_length=1, max_length=36)
    expected_revision: int = Field(..., ge=1)


class WorkspaceDeleteRequest(BaseModel):
    items: list[WorkspaceDeleteItem] = Field(..., min_length=1, max_length=200)
    idempotency_key: str = Field(..., min_length=1, max_length=128)


class WorkspaceVersionRestoreRequest(BaseModel):
    version_id: str = Field(..., min_length=1, max_length=36)
    expected_revision: int = Field(..., ge=1)
    idempotency_key: str = Field(..., min_length=1, max_length=128)


class WorkspaceCheckpointRequest(BaseModel):
    expected_revision: int = Field(..., ge=1)
    version_id: str = Field(..., min_length=1, max_length=36)
    checkpoint_kind: Literal["web_idle", "web_close", "web_periodic"] = "web_idle"


class WorkspaceVersionResponse(BaseModel):
    version_id: str
    entry_id: str
    sequence: int
    parent_version_id: str | None
    restored_from_version_id: str | None
    sha256: str | None
    size_bytes: int
    mime_type: str | None
    actor: str
    state: str
    pinned: bool
    checkpoint_kind: str | None = None
    created_at: datetime


class WorkspaceSessionImportRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=36)
    source_path: str = Field(..., min_length=1, max_length=2000)
    source_revision: str = Field(..., min_length=1, max_length=200)
    destination_parent_id: str | None = None
    destination_name: str = Field(..., min_length=1, max_length=255)
    conflict_policy: Literal["fail", "overwrite"] = "fail"
    expected_destination_revision: int | None = Field(None, ge=1)
    idempotency_key: str = Field(..., min_length=1, max_length=128)


class WorkspaceDeleteResponse(BaseModel):
    status: Literal["DELETED"] = "DELETED"
    mutation_id: str
    affected_entry_ids: list[str]
    root_count: int
    entry_count: int


__all__ = [
    "WorkspaceDirectoryCreateRequest",
    "WorkspaceCheckpointRequest",
    "WorkspaceDeleteResponse",
    "WorkspaceEntryListResponse",
    "WorkspaceEntryPatchRequest",
    "WorkspaceEntryResponse",
    "WorkspaceFileCreateRequest",
    "WorkspaceMutationResponse",
    "WorkspaceDeleteItem",
    "WorkspaceDeleteRequest",
    "WorkspaceSessionImportRequest",
    "WorkspaceVersionResponse",
    "WorkspaceVersionRestoreRequest",
]
