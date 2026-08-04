"""Request/response schemas for the Streamable HTTP MCP catalog."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.services.mcp_security import McpSecurityError, validate_mcp_headers


McpSource = Literal["official", "personal"]
McpStatus = Literal["draft", "published", "disabled"]
McpAuthType = Literal["none", "bearer", "headers"]


class _CredentialInput(BaseModel):
    auth_type: McpAuthType = "none"
    bearer_token: str | None = Field(default=None, max_length=32768)
    headers: dict[str, str] | None = None
    clear_credential: bool = False

    @field_validator("bearer_token", mode="before")
    @classmethod
    def _strip_bearer_token(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        try:
            return validate_mcp_headers(value)
        except McpSecurityError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _credential_matches_auth_type(self):
        if self.bearer_token is not None and self.headers is not None:
            raise ValueError("bearer_token 与 headers 不能同时设置")
        if self.bearer_token is not None and self.auth_type != "bearer":
            raise ValueError("bearer_token 仅适用于 auth_type=bearer")
        if self.headers is not None and self.auth_type != "headers":
            raise ValueError("headers 仅适用于 auth_type=headers")
        return self


class AdminMcpServerCreate(_CredentialInput):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=4000)
    url: str = Field(..., min_length=1, max_length=4096)
    status: McpStatus = "draft"
    allow_private_network: bool = False
    allow_insecure_http: bool = False
    required: bool = False

    @field_validator("name", "description", "url", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class AdminMcpServerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=4000)
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    status: McpStatus | None = None
    auth_type: McpAuthType | None = None
    bearer_token: str | None = Field(default=None, max_length=32768)
    headers: dict[str, str] | None = None
    clear_credential: bool = False
    allow_private_network: bool | None = None
    allow_insecure_http: bool | None = None
    required: bool | None = None

    @field_validator("name", "description", "url", "bearer_token", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value):
        return _CredentialInput._validate_headers(value)

    @model_validator(mode="after")
    def _validate_secret_shape(self):
        if self.bearer_token is not None and self.headers is not None:
            raise ValueError("bearer_token 与 headers 不能同时设置")
        if self.bearer_token is not None and self.auth_type not in (None, "bearer"):
            raise ValueError("bearer_token 仅适用于 auth_type=bearer")
        if self.headers is not None and self.auth_type not in (None, "headers"):
            raise ValueError("headers 仅适用于 auth_type=headers")
        for field_name in (
            "name",
            "url",
            "status",
            "auth_type",
            "allow_private_network",
            "allow_insecure_http",
            "required",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class UserMcpServerCreate(_CredentialInput):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=4000)
    url: str = Field(..., min_length=1, max_length=4096)
    # New connections are staged disabled. Activation performs discovery and
    # publishes the connection in one validated transaction.
    enabled: bool = False

    @field_validator("name", "description", "url", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class UserMcpServerPatch(AdminMcpServerPatch):
    # Personal servers never accept platform network-policy or publication
    # changes; omitting these fields also makes accidental privilege expansion
    # fail validation instead of being ignored.
    model_config = ConfigDict(extra="forbid")

    status: None = None
    allow_private_network: None = None
    allow_insecure_http: None = None
    required: None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _validate_enabled_is_not_null(self):
        if "enabled" in self.model_fields_set and self.enabled is None:
            raise ValueError("enabled cannot be null")
        return self


class McpConnectionUpdate(_CredentialInput):
    auth_type: McpAuthType | None = None
    enabled: bool


class McpActivationRequest(_CredentialInput):
    """Optional credential replacement applied by atomic activation.

    Omitting the request body (or all credential mutation fields) activates the
    already-persisted connection target.  Supplying a credential lets required
    official connections rotate a user override without ever publishing an
    untested intermediate target.
    """

    auth_type: McpAuthType | None = None


class McpServerResponse(BaseModel):
    id: str
    name: str
    description: str | None
    url: str
    source: McpSource
    status: McpStatus
    enabled: bool
    auth_type: McpAuthType
    credential_set: bool
    header_names: list[str]
    allow_private_network: bool
    allow_insecure_http: bool
    required: bool
    installation_id: str | None
    tools_count: int
    enabled_tools_count: int
    enabled_tools: list[str] | None
    disabled_tools: list[str]
    last_tested_at: datetime | None
    last_error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class McpServerListResponse(BaseModel):
    servers: list[McpServerResponse]
    config_version: str


class McpTestResponse(BaseModel):
    ok: bool
    tools_count: int = 0
    latency_ms: int
    error: str | None = None


class McpToolResponse(BaseModel):
    name: str
    title: str | None
    description: str | None
    schema_hash: str
    enabled: bool
    discovered_at: datetime


class McpToolListResponse(BaseModel):
    server_id: str
    installation_id: str | None
    tools_count: int
    enabled_tools_count: int
    enabled_tools: list[str] | None
    disabled_tools: list[str]
    visibility_revision: int
    tools: list[McpToolResponse]


class McpToolVisibilityUpdate(BaseModel):
    """Replace the installation's complete publication policy.

    Names are exact, case-sensitive MCP identities. Unknown names are accepted
    deliberately so policy survives rediscovery. ``enabled_tools=None`` means
    all discovered tools are candidates; an empty list publishes none. The
    disabled list always wins.
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(..., ge=0)
    enabled_tools: list[str] | None = Field(default=None, max_length=512)
    disabled_tools: list[str] = Field(default_factory=list, max_length=512)

    @field_validator("enabled_tools", "disabled_tools")
    @classmethod
    def _validate_tool_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        validated: list[str] = []
        seen: set[str] = set()
        for raw_name in value:
            if not isinstance(raw_name, str):
                raise ValueError("MCP 工具名称必须是字符串")
            name = raw_name
            if not name:
                raise ValueError("MCP 工具名称不能为空")
            if name != name.strip():
                raise ValueError("MCP 工具名称不能包含首尾空白字符")
            if name == "*":
                raise ValueError("MCP 工具名称 * 保留给权限通配规则")
            if any(ord(char) < 32 or ord(char) == 127 for char in name):
                raise ValueError("MCP 工具名称不能包含 ASCII 控制字符")
            if len(name) > 255:
                raise ValueError("MCP 工具名称不能超过 255 字符")
            if name in seen:
                raise ValueError("工具列表不能包含重复名称")
            seen.add(name)
            validated.append(name)
        return validated


class McpImportServer(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["streamable-http"] = "streamable-http"
    url: str = Field(..., min_length=1, max_length=4096)
    description: str | None = Field(default=None, max_length=4000)
    headers: dict[str, str] | None = None
    disabled: bool = False
    enabled_tools: list[str] | None = Field(default=None, max_length=512)
    disabled_tools: list[str] = Field(default_factory=list, max_length=512)

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value):
        return _CredentialInput._validate_headers(value)

    @field_validator("enabled_tools", "disabled_tools")
    @classmethod
    def _validate_tool_names(cls, value: list[str] | None) -> list[str] | None:
        return McpToolVisibilityUpdate._validate_tool_names(value)


class McpImportRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mcp_servers: dict[str, Any] = Field(alias="mcpServers")

    @field_validator("mcp_servers")
    @classmethod
    def _limit_servers(cls, value):
        if len(value) > 100:
            raise ValueError("一次最多导入 100 个 MCP 服务")
        return value


class McpImportResponse(BaseModel):
    imported: int
    servers: list[McpServerResponse]
    errors: list[dict[str, Any]]
