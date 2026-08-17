"""Database-backed MCP catalog and per-user connection state.

Only the Streamable HTTP transport is supported.  Connection credentials are
stored separately from catalog metadata so no server serialization can expose
secret material by accident.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)

from .database import Base
from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS, PGVector
from src.api.utils.timezone import now_naive


def _uuid() -> str:
    return str(uuid.uuid4())


class McpServer(Base):
    """Official or user-owned Streamable HTTP MCP server definition."""

    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint(
            "source IN ('official', 'personal')",
            name="ck_mcp_servers_source",
        ),
        CheckConstraint(
            "(source = 'official' AND owner_user_id IS NULL) OR "
            "(source = 'personal' AND owner_user_id IS NOT NULL)",
            name="ck_mcp_servers_source_owner",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'disabled')",
            name="ck_mcp_servers_status",
        ),
        CheckConstraint(
            "auth_type IN ('none', 'bearer', 'headers')",
            name="ck_mcp_servers_auth_type",
        ),
        Index(
            "uq_mcp_servers_official_name",
            "name",
            unique=True,
            postgresql_where=text("source = 'official'"),
            sqlite_where=text("source = 'official'"),
        ),
        Index(
            "uq_mcp_servers_personal_owner_name",
            "owner_user_id",
            "name",
            unique=True,
            postgresql_where=text("source = 'personal'"),
            sqlite_where=text("source = 'personal'"),
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    source = Column(String(20), nullable=False, index=True)
    owner_user_id = Column(
        String(100),
        ForeignKey("auth_users.user_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    url = Column(Text, nullable=False)
    status = Column(
        String(20),
        nullable=False,
        default="draft",
        server_default=text("'draft'"),
        index=True,
    )
    auth_type = Column(
        String(20),
        nullable=False,
        default="none",
        server_default=text("'none'"),
    )
    allow_private_network = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    allow_insecure_http = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    required = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    version = Column(Integer, nullable=False, default=1, server_default=text("1"))
    last_tested_at = Column(DateTime, nullable=True)
    # Platform probes have no per-user installation/tool snapshots. Persist the
    # last discovered count separately so the admin catalog can report the
    # result after the immediate test response has gone away.
    last_tools_count = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)


class McpCredential(Base):
    """Encrypted connection secret for a platform or a specific user."""

    __tablename__ = "mcp_credentials"
    __table_args__ = (
        CheckConstraint(
            "auth_type IN ('bearer', 'headers')",
            name="ck_mcp_credentials_auth_type",
        ),
        UniqueConstraint(
            "server_id",
            "user_id",
            name="uq_mcp_credentials_server_user",
        ),
        Index(
            "uq_mcp_credentials_platform_server",
            "server_id",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
            sqlite_where=text("user_id IS NULL"),
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    server_id = Column(
        String(36),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL means the platform credential configured by an administrator.
    user_id = Column(
        String(100),
        ForeignKey("auth_users.user_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    auth_type = Column(String(20), nullable=False)
    encrypted_secret = Column(Text, nullable=False)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)


class McpInstallation(Base):
    """A user's enablement and credential selection for an MCP server."""

    __tablename__ = "mcp_installations"
    __table_args__ = (
        UniqueConstraint(
            "server_id",
            "user_id",
            name="uq_mcp_installations_server_user",
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    server_id = Column(
        String(36),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        String(100),
        ForeignKey("auth_users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        index=True,
    )
    credential_id = Column(
        String(36),
        ForeignKey("mcp_credentials.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Evidence from the last successful activation when a personal connection
    # needed the administrator-managed private-network policy.  NULL means the
    # endpoint was ordinary public HTTPS and did not consume an exception.
    network_authorization_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)


class McpPersonalNetworkPolicy(Base):
    """Singleton administrator policy for personal MCP network exceptions."""

    __tablename__ = "mcp_personal_network_policies"

    scope_key = Column(String(20), primary_key=True, default="global")
    domain_suffixes_json = Column(
        Text,
        nullable=False,
        default="[]",
        server_default=text("'[]'"),
    )
    cidrs_json = Column(
        Text,
        nullable=False,
        default="[]",
        server_default=text("'[]'"),
    )
    version = Column(Integer, nullable=False, default=1, server_default=text("1"))
    updated_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)


class McpToolVisibility(Base):
    """Per-installation MCP tool publication policy.

    Absence of a row means all remote tools are published. A NULL allowlist
    keeps that default, while a non-NULL JSON list publishes only the named
    tools. The denylist always wins. Names are kept separate from snapshots so
    policy survives temporary disappearance or failed discovery.
    """

    __tablename__ = "mcp_tool_visibility"

    installation_id = Column(
        String(36),
        ForeignKey("mcp_installations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # NULL means "no allowlist"; "[]" means "publish no tools".
    enabled_tools_json = Column(Text, nullable=True)
    disabled_tools_json = Column(
        Text,
        nullable=False,
        default="[]",
        server_default=text("'[]'"),
    )
    revision = Column(Integer, nullable=False, default=1, server_default=text("1"))
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)


class McpToolSnapshot(Base):
    """Last discovered tool metadata for one credential-scoped installation."""

    __tablename__ = "mcp_tool_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "tool_name",
            name="uq_mcp_tool_snapshots_installation_tool",
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    installation_id = Column(
        String(36),
        ForeignKey("mcp_installations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name = Column(String(255), nullable=False)
    title = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    input_schema_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    annotations_json = Column(Text, nullable=True)
    schema_hash = Column(String(64), nullable=False, index=True)
    # Endpoint/credential target that supplied this schema. NULL is a legacy
    # snapshot and is never safe for fallback execution.
    connection_fingerprint = Column(String(64), nullable=True)
    discovered_at = Column(DateTime, default=now_naive, nullable=False, index=True)


class McpToolSearchIndex(Base):
    """Derived semantic-routing index keyed by stable MCP tool identity.

    This table stays separate from the executable snapshot hot path. Snapshot
    replacement can therefore retain an unchanged vector, while candidate-
    scoped search can never turn a durable-but-hidden snapshot into a tool.
    """

    __tablename__ = "mcp_tool_search_indexes"
    __table_args__ = (
        Index(
            "idx_mcp_tool_search_embedding_claim",
            "embedding_model_fingerprint",
            "lease_expires_at",
            "retry_after",
        ),
    )

    installation_id = Column(
        String(36),
        ForeignKey("mcp_installations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tool_name = Column(String(255), primary_key=True)
    search_document = Column(Text, nullable=False)
    search_document_hash = Column(String(64), nullable=False, index=True)
    schema_hash = Column(String(64), nullable=False)
    connection_fingerprint = Column(String(64), nullable=False)
    embedding = Column(
        JSON().with_variant(PGVector(MEMORY_EMBEDDING_DIMENSIONS), "postgresql"),
        nullable=True,
    )
    embedding_model_fingerprint = Column(String(64), nullable=True, index=True)
    embedded_document_hash = Column(String(64), nullable=True)
    claim_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    retry_after = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)


class McpConfigVersion(Base):
    """Monotonic catalog generation used for cross-worker cache invalidation."""

    __tablename__ = "mcp_config_versions"

    # "global" or "user:<user_id>".  A single key avoids NULL uniqueness
    # differences across supported test/production databases.
    scope_key = Column(String(140), primary_key=True)
    version = Column(Integer, nullable=False, default=1, server_default=text("1"))
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
