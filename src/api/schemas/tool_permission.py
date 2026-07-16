"""Request schemas for the generic tool permission API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class ToolPermissionRuleCreate(BaseModel):
    provider: str = Field(pattern="^(builtin|mcp)$")
    server_id: str | None = Field(default=None, max_length=36)
    tool_name: str = Field(default="*", min_length=1, max_length=255)
    effect: str = Field(pattern="^(allow|ask|deny)$")
    priority: int = Field(default=0, ge=-10000, le=10000)
    description: str | None = Field(default=None, max_length=1000)
    expires_at: datetime | None = None

    @field_validator("provider", "effect", mode="before")
    @classmethod
    def _strip_required(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("tool_name", mode="before")
    @classmethod
    def _validate_tool_name(cls, value):
        # MCP tool names are opaque and case-sensitive. Silently trimming here
        # would make a permission rule target a different remote identity.
        if isinstance(value, str) and value != value.strip():
            raise ValueError("tool_name cannot contain leading or trailing whitespace")
        if isinstance(value, str) and any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("tool_name cannot contain ASCII control characters")
        return value

    @field_validator("server_id", "description", mode="before")
    @classmethod
    def _strip_optional(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _validate_reference(self):
        if self.provider == "mcp" and not self.server_id:
            raise ValueError("MCP permission rules require server_id")
        if self.provider == "builtin" and self.server_id is not None:
            raise ValueError("builtin permission rules cannot contain server_id")
        return self


class ToolPermissionRulePatch(BaseModel):
    effect: str | None = Field(default=None, pattern="^(allow|ask|deny)$")
    priority: int | None = Field(default=None, ge=-10000, le=10000)
    description: str | None = Field(default=None, max_length=1000)
    enabled: bool | None = None
    expires_at: datetime | None = None

    @field_validator("effect", mode="before")
    @classmethod
    def _strip_effect(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("description", mode="before")
    @classmethod
    def _strip_description(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _reject_null_for_required_columns(self):
        # PATCH fields may be omitted, but explicitly assigning NULL to these
        # NOT NULL database columns would otherwise survive exclude_unset=True
        # and fail during commit with a 500 response.
        for field_name in ("effect", "priority", "enabled"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class ToolPermissionSelection(BaseModel):
    """Atomic user choice for one exact tool identity."""

    provider: str = Field(pattern="^(builtin|mcp)$")
    server_id: str | None = Field(default=None, max_length=36)
    tool_name: str = Field(min_length=1, max_length=255)
    effect: str = Field(pattern="^(allow|ask|deny)$")

    @field_validator("provider", "effect", mode="before")
    @classmethod
    def _strip_required(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("tool_name", mode="before")
    @classmethod
    def _validate_tool_name(cls, value):
        if isinstance(value, str) and value != value.strip():
            raise ValueError("tool_name cannot contain leading or trailing whitespace")
        if isinstance(value, str) and any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("tool_name cannot contain ASCII control characters")
        return value

    @field_validator("server_id", mode="before")
    @classmethod
    def _strip_optional(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _validate_reference(self):
        if self.provider == "mcp" and not self.server_id:
            raise ValueError("MCP permission rules require server_id")
        if self.provider == "builtin" and self.server_id is not None:
            raise ValueError("builtin permission rules cannot contain server_id")
        return self


class ToolSelectionItem(BaseModel):
    """One exact tool identity inside a batch selection."""

    provider: str = Field(pattern="^(builtin|mcp)$")
    server_id: str | None = Field(default=None, max_length=36)
    tool_name: str = Field(min_length=1, max_length=255)

    @field_validator("provider", mode="before")
    @classmethod
    def _strip_provider(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("tool_name", mode="before")
    @classmethod
    def _validate_tool_name(cls, value):
        if isinstance(value, str) and value != value.strip():
            raise ValueError("tool_name cannot contain leading or trailing whitespace")
        if isinstance(value, str) and any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("tool_name cannot contain ASCII control characters")
        return value

    @field_validator("server_id", mode="before")
    @classmethod
    def _strip_optional(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _validate_reference(self):
        if self.provider == "mcp" and not self.server_id:
            raise ValueError("MCP permission rules require server_id")
        if self.provider == "builtin" and self.server_id is not None:
            raise ValueError("builtin permission rules cannot contain server_id")
        return self


class ToolPermissionSelectionBatch(BaseModel):
    """Apply one effect to many exact tool identities in a single transaction."""

    effect: str = Field(pattern="^(allow|ask|deny)$")
    items: list[ToolSelectionItem] = Field(min_length=1, max_length=500)

    @field_validator("effect", mode="before")
    @classmethod
    def _strip_effect(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_unique(self):
        seen: set[tuple[str, str | None, str]] = set()
        for item in self.items:
            key = (item.provider, item.server_id, item.tool_name)
            if key in seen:
                raise ValueError("batch selection cannot contain duplicate tools")
            seen.add(key)
        return self


class ToolApprovalResolutionPayload(BaseModel):
    resolution: str = Field(
        pattern="^(allow_once|allow_session|allow_always|deny)$"
    )

    @field_validator("resolution", mode="before")
    @classmethod
    def _strip_resolution(cls, value):
        return value.strip().lower() if isinstance(value, str) else value
