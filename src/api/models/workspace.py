"""Persistent per-user workspace metadata and append-only mutation audit."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from .database import Base
from src.api.utils.timezone import now_naive


class UserWorkspace(Base):
    """One logical workspace rooted at ``<profile mount>/workdir`` per user."""

    __tablename__ = "user_workspaces"

    user_id = Column(String(100), primary_key=True)
    root_path = Column(String(500), nullable=False, default="/home/user/workdir")
    active_profile_id = Column(String(36), nullable=True, index=True)
    active_profile_version = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="active", index=True)
    quota_bytes = Column(BigInteger, nullable=False, default=5 * 1024 * 1024 * 1024)
    used_bytes = Column(BigInteger, nullable=False, default=0)
    history_quota_bytes = Column(BigInteger, nullable=False, default=5 * 1024 * 1024 * 1024)
    history_used_bytes = Column(BigInteger, nullable=False, default=0)
    last_history_gc_at = Column(DateTime, nullable=True)
    entry_count = Column(Integer, nullable=False, default=0)
    revision = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=now_naive)
    updated_at = Column(DateTime, nullable=False, default=now_naive, onupdate=now_naive)


class WorkspaceEntry(Base):
    """Stable identity and current projection for one workspace file or directory."""

    __tablename__ = "workspace_entries"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "parent_key",
            "name",
            "status",
            name="uq_workspace_entry_sibling_status",
        ),
        UniqueConstraint(
            "user_id",
            "relative_path",
            name="uq_workspace_entry_relative_path",
        ),
        Index("idx_workspace_entries_user_parent", "user_id", "parent_key", "status"),
        Index("idx_workspace_entries_user_path", "user_id", "relative_path"),
    )

    entry_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    parent_id = Column(String(36), nullable=True, index=True)
    # PostgreSQL treats NULLs as distinct in UNIQUE constraints.  Keep an
    # explicit root sentinel so two root siblings cannot share a name.
    parent_key = Column(String(64), nullable=False, default="")
    name = Column(String(255), nullable=False)
    kind = Column(String(20), nullable=False, index=True)  # file / directory
    relative_path = Column(String(2000), nullable=False)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    mime_type = Column(String(255), nullable=True)
    sha256 = Column(String(64), nullable=True)
    revision = Column(BigInteger, nullable=False, default=1)
    current_version_id = Column(String(36), nullable=True, index=True)
    head_blob_id = Column(String(36), nullable=True, index=True)
    tree_revision = Column(BigInteger, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="active", index=True)
    created_at = Column(DateTime, nullable=False, default=now_naive)
    updated_at = Column(DateTime, nullable=False, default=now_naive, onupdate=now_naive)


class WorkspaceMutation(Base):
    """Append-only file-operation journal used for audit and idempotency."""

    __tablename__ = "workspace_mutations"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_workspace_mutation_user_idempotency",
        ),
        Index("idx_workspace_mutations_user_created", "user_id", "created_at"),
        Index("idx_workspace_mutations_entry", "entry_id", "created_at"),
        Index("idx_workspace_mutations_state_lease", "state", "lease_expires_at"),
    )

    mutation_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    entry_id = Column(String(36), nullable=True, index=True)
    actor = Column(String(20), nullable=False, default="web", index=True)
    operation = Column(String(50), nullable=False, index=True)
    state = Column(String(20), nullable=False, default="completed", index=True)
    result_status = Column(String(30), nullable=True)
    idempotency_key = Column(String(128), nullable=True)
    session_id = Column(String(36), nullable=True, index=True)
    round_id = Column(String(36), nullable=True, index=True)
    tool_call_id = Column(String(64), nullable=True, index=True)
    cron_job_id = Column(String(36), nullable=True, index=True)
    cron_run_id = Column(String(36), nullable=True, index=True)
    claim_id = Column(String(36), nullable=True, index=True)
    claim_generation = Column(BigInteger, nullable=True)
    owner_token = Column(String(64), nullable=True)
    change_set_id = Column(String(36), nullable=True, index=True)
    before_revision = Column(BigInteger, nullable=True)
    after_revision = Column(BigInteger, nullable=True)
    before_version_id = Column(String(36), nullable=True)
    after_version_id = Column(String(36), nullable=True)
    before_sha256 = Column(String(64), nullable=True)
    after_sha256 = Column(String(64), nullable=True)
    details_json = Column(Text, nullable=False, default="{}")
    error_code = Column(String(60), nullable=True)
    error_message = Column(Text, nullable=True)
    recoverable = Column(Boolean, nullable=False, default=False)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    heartbeat_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_naive)
    completed_at = Column(DateTime, nullable=True)


class WorkspaceFileVersion(Base):
    """Immutable content version referenced by entries, Rounds, and checkpoints."""

    __tablename__ = "workspace_file_versions"
    __table_args__ = (
        UniqueConstraint(
            "entry_id",
            "sequence",
            name="uq_workspace_file_version_entry_sequence",
        ),
        Index("idx_workspace_versions_user_created", "user_id", "created_at"),
        Index("idx_workspace_versions_entry_created", "entry_id", "created_at"),
        Index("idx_workspace_versions_sha", "user_id", "sha256"),
    )

    version_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    entry_id = Column(String(36), nullable=False, index=True)
    sequence = Column(BigInteger, nullable=False)
    parent_version_id = Column(String(36), nullable=True, index=True)
    restored_from_version_id = Column(String(36), nullable=True, index=True)
    blob_id = Column(String(36), nullable=True, index=True)
    content_path = Column(String(2000), nullable=True)
    sha256 = Column(String(64), nullable=True)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    mime_type = Column(String(255), nullable=True)
    actor = Column(String(20), nullable=False, default="web", index=True)
    session_id = Column(String(36), nullable=True, index=True)
    round_id = Column(String(36), nullable=True, index=True)
    cron_run_id = Column(String(36), nullable=True, index=True)
    state = Column(String(20), nullable=False, default="materialized", index=True)
    pinned = Column(Boolean, nullable=False, default=False, index=True)
    checkpoint_kind = Column(String(32), nullable=True, index=True)
    retained_until = Column(DateTime, nullable=True, index=True)
    pruned_at = Column(DateTime, nullable=True)
    legacy_content_path = Column(String(2000), nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_naive)


class WorkspaceContentObject(Base):
    """Per-user content-addressed immutable bytes shared by revisions."""

    __tablename__ = "workspace_content_objects"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "sha256",
            name="uq_workspace_content_object_user_sha",
        ),
        Index(
            "idx_workspace_content_objects_user_state_access",
            "user_id",
            "state",
            "last_accessed_at",
        ),
    )

    blob_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    sha256 = Column(String(64), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    content_path = Column(String(2000), nullable=False)
    state = Column(String(20), nullable=False, default="materialized", index=True)
    created_at = Column(DateTime, nullable=False, default=now_naive)
    last_accessed_at = Column(DateTime, nullable=False, default=now_naive)
    pruned_at = Column(DateTime, nullable=True)


class WorkspaceContentReference(Base):
    """Explicit live reference that protects one content object from GC."""

    __tablename__ = "workspace_content_references"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "reference_kind",
            "reference_key",
            name="uq_workspace_content_reference_owner",
        ),
        Index(
            "idx_workspace_content_references_blob",
            "user_id",
            "blob_id",
        ),
        Index(
            "idx_workspace_content_references_version",
            "user_id",
            "version_id",
        ),
    )

    reference_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    blob_id = Column(String(36), nullable=False, index=True)
    version_id = Column(String(36), nullable=True, index=True)
    reference_kind = Column(String(32), nullable=False, index=True)
    reference_key = Column(String(500), nullable=False)
    retained_until = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=now_naive)
    updated_at = Column(DateTime, nullable=False, default=now_naive, onupdate=now_naive)


class WorkspaceClaim(Base):
    """Durable, renewable ownership for one file, path, tree, or workspace scope."""

    __tablename__ = "workspace_claims"
    __table_args__ = (
        Index(
            "uq_workspace_claim_active_scope",
            "user_id",
            "scope_key",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index("idx_workspace_claims_lease", "state", "lease_expires_at"),
        Index("idx_workspace_claims_mutation", "mutation_id"),
    )

    claim_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    scope_kind = Column(String(20), nullable=False)
    scope_key = Column(String(160), nullable=False)
    entry_id = Column(String(36), nullable=True, index=True)
    mutation_id = Column(String(36), nullable=True, index=True)
    operation = Column(String(50), nullable=False)
    owner_token = Column(String(64), nullable=False)
    generation = Column(BigInteger, nullable=False)
    state = Column(String(20), nullable=False, default="active", index=True)
    lease_expires_at = Column(DateTime, nullable=False, index=True)
    heartbeat_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=now_naive)
    released_at = Column(DateTime, nullable=True)


class WorkspaceChangeSet(Base):
    """A proposed workspace mutation based on an immutable base version."""

    __tablename__ = "workspace_change_sets"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_workspace_change_set_user_idempotency",
        ),
        Index("idx_workspace_change_sets_user_status", "user_id", "status", "created_at"),
        Index("idx_workspace_change_sets_entry", "entry_id", "created_at"),
    )

    change_set_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    entry_id = Column(String(36), nullable=True, index=True)
    operation = Column(String(50), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="proposed", index=True)
    actor = Column(String(20), nullable=False, default="chat", index=True)
    base_version_id = Column(String(36), nullable=True, index=True)
    proposed_version_id = Column(String(36), nullable=True, index=True)
    applied_version_id = Column(String(36), nullable=True, index=True)
    proposal_blob_id = Column(String(36), nullable=True, index=True)
    idempotency_key = Column(String(128), nullable=True)
    session_id = Column(String(36), nullable=True, index=True)
    round_id = Column(String(36), nullable=True, index=True)
    tool_call_id = Column(String(64), nullable=True, index=True)
    cron_run_id = Column(String(36), nullable=True, index=True)
    details_json = Column(Text, nullable=False, default="{}")
    error_code = Column(String(60), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_naive)
    applied_at = Column(DateTime, nullable=True)


__all__ = [
    "UserWorkspace",
    "WorkspaceChangeSet",
    "WorkspaceClaim",
    "WorkspaceContentObject",
    "WorkspaceContentReference",
    "WorkspaceEntry",
    "WorkspaceFileVersion",
    "WorkspaceMutation",
]
