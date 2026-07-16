"""Runtime client and tool catalog projection for Streamable HTTP MCP servers.

The database catalog is the durable source of truth.  This module deliberately
keeps transport state out of SQLAlchemy models and exposes a small immutable
snapshot to the Agent layer.  Every remote call re-resolves the installation so
an administrator disable, credential change, or ownership change takes effect
even when an Agent still holds an older tool snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import weakref
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from time import time
from typing import Any, AsyncIterator, Callable, Protocol

import httpx
from jsonschema import Draft7Validator, Draft201909Validator, Draft202012Validator
from jsonschema.exceptions import SchemaError

from src.api.models.database import SessionLocal
from src.api.config import get_settings
from src.api.services.mcp_security import (
    McpSecurityError,
    ResolvedMcpEndpoint,
    credential_headers,
    resolve_mcp_endpoint,
    sanitize_mcp_exception,
)
from src.api.services.secret_crypto import secret_fingerprint


logger = logging.getLogger(__name__)

_MAX_MODEL_TOOL_NAME = 64
_MAX_TOOL_NAME_CHARS = 255
_MAX_TOOL_TITLE_CHARS = 255
_MAX_TOOLS_PER_INSTALLATION = 512
_MAX_TOOL_LIST_PAGES = 100
_MAX_STREAM_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_DISCOVERY_SESSION_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_CALL_SESSION_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_TOOL_DESCRIPTION_BYTES = 16 * 1024
_MAX_TOOL_SCHEMA_BYTES = 512 * 1024
_MAX_TOOL_ANNOTATIONS_BYTES = 128 * 1024
_MAX_MCP_ARGUMENT_BYTES = 256 * 1024
_MAX_SCHEMA_DEPTH = 32
_MAX_SCHEMA_NODES = 4096
_MAX_SCHEMA_CONTAINER_ITEMS = 1024
_MAX_SCHEMA_COMBINATOR_BRANCHES = 64
_MAX_SCHEMA_LOCAL_REFS = 128
_MAX_ARGUMENT_DEPTH = 32
_MAX_ARGUMENT_NODES = 4096
_MAX_ARGUMENT_CONTAINER_ITEMS = 1024
_MAX_INSTALLATION_CATALOG_BYTES = 4 * 1024 * 1024
_MAX_TOOL_CURSOR_BYTES = 4096
_MAX_MCP_CALL_TIMEOUT_SECONDS = 600.0
_CALL_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0
_MCP_SETTINGS = get_settings()
_MAX_CONCURRENT_MCP_DISCOVERIES = int(
    _MCP_SETTINGS.mcp_max_concurrent_discoveries_per_user
)
_MAX_GLOBAL_CONCURRENT_MCP_DISCOVERIES = int(
    _MCP_SETTINGS.mcp_max_concurrent_discoveries_global
)
_MAX_INSTALLATIONS_PER_CATALOG = int(_MCP_SETTINGS.mcp_max_installations_per_user)
_MAX_TOOLS_PER_USER_CATALOG = int(_MCP_SETTINGS.mcp_max_tools_per_user)
_DISCOVERY_TIMEOUT_SECONDS = float(_MCP_SETTINGS.mcp_discovery_timeout_seconds)
_CATALOG_BUILD_TIMEOUT_SECONDS = float(_MCP_SETTINGS.mcp_catalog_build_timeout_seconds)
_MAX_USER_CATALOG_BYTES = 32 * 1024 * 1024
_SAFE_TOOL_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
del _MCP_SETTINGS

_GLOBAL_DISCOVERY_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    asyncio.Semaphore,
] = weakref.WeakKeyDictionary()


def _global_discovery_semaphore() -> asyncio.Semaphore:
    """Return the process-wide MCP discovery limiter for the active server loop."""

    loop = asyncio.get_running_loop()
    semaphore = _GLOBAL_DISCOVERY_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_MAX_GLOBAL_CONCURRENT_MCP_DISCOVERIES)
        _GLOBAL_DISCOVERY_SEMAPHORES[loop] = semaphore
    return semaphore


class McpRuntimeError(RuntimeError):
    """Base error raised by the MCP runtime boundary."""


class McpRequiredServerUnavailable(McpRuntimeError):
    """A server marked required could not be discovered."""


class McpToolNameCollisionError(McpRuntimeError):
    """Two remote tools projected to the same model-visible name."""


class McpInstallationUnavailable(McpRuntimeError):
    """The installation is no longer executable for the current user."""


class McpToolNotPublished(McpRuntimeError):
    """A previously discovered tool is hidden by installation visibility."""


class McpToolSnapshotStale(McpRuntimeError):
    """The durable tool schema no longer matches the Agent's snapshot."""


class McpCallCancelled(McpRuntimeError):
    """The local Turn cancelled an in-flight MCP request."""


class McpCallOutcomeUnknown(McpRuntimeError):
    """The MCP request crossed the dispatch boundary but no result was observed."""


class McpToolArgumentsInvalid(McpRuntimeError):
    """MCP arguments are not safe or do not satisfy the discovered schema."""


@dataclass(frozen=True)
class EffectiveMcpInstallation:
    """Credential-scoped Streamable HTTP connection resolved for one user."""

    installation_id: str
    server_id: str
    user_id: str
    server_name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    required: bool = False
    source: str = "personal"
    server_version: int = 1
    auth_type: str = "none"
    credential_fingerprint: str = ""
    allow_private_network: bool = False
    allow_insecure_http: bool = False
    enabled_tools: frozenset[str] | None = None
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    configuration_error: str | None = None

    def publishes_tool(self, raw_name: str) -> bool:
        return (
            (self.enabled_tools is None or raw_name in self.enabled_tools)
            and raw_name not in self.disabled_tools
        )

    @property
    def execution_fingerprint(self) -> str:
        """Bind approvals to the concrete remote execution target.

        Tool publication is intentionally excluded. Enabling or hiding another
        tool must invalidate the model catalog, but it must not revoke an
        approval for an otherwise unchanged endpoint and credential.
        """

        return mcp_execution_fingerprint(
            installation_id=self.installation_id,
            server_id=self.server_id,
            user_id=self.user_id,
            url=self.url,
            auth_type=self.auth_type,
            credential_fingerprint=self.credential_fingerprint,
            allow_private_network=self.allow_private_network,
            allow_insecure_http=self.allow_insecure_http,
        )

    @property
    def connection_fingerprint(self) -> str:
        """Compatibility name for the endpoint/credential binding."""

        return self.execution_fingerprint


def _sanitize_mcp_exception(
    installation: EffectiveMcpInstallation,
    exc: BaseException,
) -> str:
    """Return diagnostics through the shared MCP security boundary."""

    return sanitize_mcp_exception(
        exc,
        url=installation.url,
        headers=installation.headers,
        include_exception_type=True,
    )


@dataclass(frozen=True)
class McpToolSnapshot:
    """Immutable model-facing projection of one remote MCP tool."""

    installation_id: str
    server_id: str
    server_name: str
    source: str
    raw_name: str
    model_name: str
    description: str
    input_schema: dict[str, Any]
    title: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    schema_hash: str = ""
    connection_fingerprint: str = ""
    stale: bool = False


@dataclass(frozen=True)
class McpCatalogSnapshot:
    """Per-user immutable catalog used to build an Agent tool set."""

    user_id: str
    fingerprint: str
    tools: tuple[McpToolSnapshot, ...]
    errors: tuple[str, ...] = ()


@dataclass
class _CatalogCacheEntry:
    snapshot: McpCatalogSnapshot
    logical_bytes: int
    last_access_at: float


class McpRepository(Protocol):
    def list_effective_installations(self, user_id: str) -> list[EffectiveMcpInstallation]: ...

    def get_effective_installation(
        self,
        user_id: str,
        installation_id: str,
    ) -> EffectiveMcpInstallation | None: ...

    def catalog_fingerprint(self, user_id: str) -> str: ...

    def load_tool_snapshots(self, installation: EffectiveMcpInstallation) -> list[McpToolSnapshot]: ...

    def get_tool_snapshot_binding(
        self,
        installation: EffectiveMcpInstallation,
        raw_name: str,
    ) -> tuple[str, str] | None: ...

    def replace_tool_snapshots(
        self,
        installation: EffectiveMcpInstallation,
        tools: list[McpToolSnapshot],
    ) -> bool: ...


class McpSessionConnector(Protocol):
    async def list_tools(self, installation: EffectiveMcpInstallation) -> list[Any]: ...

    async def call_tool(
        self,
        installation: EffectiveMcpInstallation,
        raw_name: str,
        arguments: dict[str, Any],
        *,
        on_dispatch: Callable[[], None] | None = None,
    ) -> Any: ...


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def mcp_execution_fingerprint(
    *,
    installation_id: str,
    server_id: str,
    user_id: str,
    url: str,
    auth_type: str,
    credential_fingerprint: str,
    allow_private_network: bool,
    allow_insecure_http: bool,
) -> str:
    """Hash only state that selects or authorizes the remote call target."""

    return _stable_hash({
        "installation_id": installation_id,
        "server_id": server_id,
        "user_id": user_id,
        "url": url,
        "auth_type": auth_type,
        "credential": credential_fingerprint,
        "allow_private_network": bool(allow_private_network),
        "allow_insecure_http": bool(allow_insecure_http),
    })


def mcp_tool_schema_hash(
    *,
    raw_name: str,
    description: str,
    input_schema: dict[str, Any],
    annotations: dict[str, Any],
) -> str:
    """Canonical hash shared by runtime discovery and manual probes."""

    return _stable_hash({
        "name": raw_name,
        "description": description,
        "input_schema": input_schema,
        "annotations": annotations,
    })


def _server_short_id(server_id: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", str(server_id))
    if len(compact) >= 8:
        return compact[:8].lower()
    return hashlib.sha256(str(server_id).encode("utf-8")).hexdigest()[:8]


def model_tool_name(server_id: str, raw_name: str) -> str:
    """Build a deterministic, provider-safe model tool name (max 64 bytes)."""

    if not isinstance(raw_name, str) or not raw_name or raw_name != raw_name.strip():
        raise ValueError("MCP tool names must be non-empty and contain no surrounding whitespace")
    if raw_name == "*":
        raise ValueError("MCP tool name '*' is reserved for permission wildcards")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_name):
        raise ValueError("MCP tool names cannot contain ASCII control characters")
    safe_raw = _SAFE_TOOL_CHARS.sub("_", raw_name).strip("_") or "tool"
    prefix = f"mcp__{_server_short_id(server_id)}__"
    available = _MAX_MODEL_TOOL_NAME - len(prefix)
    needs_hash = safe_raw != raw_name or len(safe_raw.encode("utf-8")) > available
    suffix = (
        "_" + hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:8]
        if needs_hash
        else ""
    )
    base_budget = max(1, available - len(suffix))
    if len(safe_raw.encode("utf-8")) > base_budget:
        # Sanitized model names contain ASCII only, so character slicing is
        # also an exact byte-bound operation.
        safe_raw = safe_raw[:base_budget]
    return prefix + safe_raw + suffix


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(by_alias=True, exclude_none=True, mode="json")
        return result if isinstance(result, dict) else {}
    return {}


def _json_size_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _strict_json_bytes(value: Any, *, label: str) -> bytes:
    """Serialize untrusted JSON without Python coercions or non-finite numbers."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise McpRuntimeError(f"{label} must contain only valid JSON values") from exc


_SCHEMA_VALIDATORS = {
    "https://json-schema.org/draft/2020-12/schema": Draft202012Validator,
    "https://json-schema.org/draft/2019-09/schema": Draft201909Validator,
    "http://json-schema.org/draft-07/schema": Draft7Validator,
    # Some generators use HTTPS for draft-07 even though its canonical URI is HTTP.
    "https://json-schema.org/draft-07/schema": Draft7Validator,
}
_FORBIDDEN_SCHEMA_KEYWORDS = {
    "$anchor",
    "$dynamicAnchor",
    "$dynamicRef",
    "$id",
    "$recursiveAnchor",
    "$recursiveRef",
    "$vocabulary",
    "pattern",
    "patternProperties",
}
_SCHEMA_COMBINATORS = {"allOf", "anyOf", "oneOf"}


def _normalized_schema_uri(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise McpRuntimeError("MCP tool input schema has an invalid $schema URI")
    return value[:-1] if value.endswith("#") else value


def _decode_json_pointer_token(token: str) -> str:
    if "%" in token or re.search(r"~(?:[^01]|$)", token):
        raise McpRuntimeError("MCP tool input schema has an invalid local $ref")
    return token.replace("~1", "/").replace("~0", "~")


def _resolve_local_schema_ref(
    schema: dict[str, Any],
    ref: Any,
) -> tuple[tuple[Any, ...], Any]:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise McpRuntimeError(
            "MCP tool input schema may use only non-root local JSON Pointer $ref values"
        )
    path: list[Any] = []
    current: Any = schema
    for raw_token in ref[2:].split("/"):
        token = _decode_json_pointer_token(raw_token)
        if isinstance(current, dict):
            if token not in current:
                raise McpRuntimeError("MCP tool input schema contains an unresolved local $ref")
            path.append(token)
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                raise McpRuntimeError("MCP tool input schema has an invalid array $ref")
            index = int(token)
            if index >= len(current):
                raise McpRuntimeError("MCP tool input schema contains an unresolved local $ref")
            path.append(index)
            current = current[index]
        else:
            raise McpRuntimeError("MCP tool input schema contains an unresolved local $ref")
    if not isinstance(current, (dict, bool)):
        raise McpRuntimeError("MCP tool input schema $ref must resolve to a schema")
    return tuple(path), current


def _validate_schema_structure(schema: dict[str, Any]) -> None:
    """Bound schema work and reject resolution/regex features from remote input."""

    # First bound the entire JSON document, including literal enum/default data.
    stack: list[tuple[tuple[Any, ...], Any, int]] = [((), schema, 0)]
    node_count = 0
    while stack:
        path, value, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_SCHEMA_NODES:
            raise McpRuntimeError("MCP tool input schema is too complex")
        if depth > _MAX_SCHEMA_DEPTH:
            raise McpRuntimeError("MCP tool input schema exceeds the nesting limit")

        if isinstance(value, dict):
            if len(value) > _MAX_SCHEMA_CONTAINER_ITEMS:
                raise McpRuntimeError("MCP tool input schema object is too large")
            if any(not isinstance(key, str) for key in value):
                raise McpRuntimeError("MCP tool input schema keys must be strings")
            for key, item in value.items():
                stack.append((path + (key,), item, depth + 1))
        elif isinstance(value, list):
            if len(value) > _MAX_SCHEMA_CONTAINER_ITEMS:
                raise McpRuntimeError("MCP tool input schema array is too large")
            for index, item in enumerate(value):
                stack.append((path + (index,), item, depth + 1))

    # Walk only locations that JSON Schema treats as schemas. Property/$defs
    # names and enum/const/default payloads are data and may legitimately be
    # named "pattern", "$id", and so on.
    schema_map_keywords = {
        "$defs",
        "definitions",
        "dependentSchemas",
        "properties",
    }
    schema_list_keywords = {"allOf", "anyOf", "oneOf", "prefixItems"}
    schema_single_keywords = {
        "additionalItems",
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
    graph: dict[tuple[Any, ...], set[tuple[Any, ...]]] = {}
    pending_schemas: list[tuple[tuple[Any, ...], Any]] = [((), schema)]
    scanned_schemas: set[tuple[Any, ...]] = set()
    combinator_branches = 0
    local_ref_count = 0

    while pending_schemas:
        path, node = pending_schemas.pop()
        if path in scanned_schemas:
            continue
        scanned_schemas.add(path)
        graph.setdefault(path, set())
        if isinstance(node, bool):
            continue
        if not isinstance(node, dict):
            # The standard validator reports the precise invalid keyword shape.
            continue

        for key in node:
            if key in _FORBIDDEN_SCHEMA_KEYWORDS:
                raise McpRuntimeError(
                    f"MCP tool input schema keyword {key!r} is not allowed"
                )
        if "$schema" in node and path:
            raise McpRuntimeError(
                "MCP tool input schema may declare $schema only at the root"
            )

        for key in _SCHEMA_COMBINATORS:
            branches = node.get(key)
            if isinstance(branches, list):
                combinator_branches += len(branches)
                if combinator_branches > _MAX_SCHEMA_COMBINATOR_BRANCHES:
                    raise McpRuntimeError(
                        "MCP tool input schema has too many combinator branches"
                    )

        if "$ref" in node:
            local_ref_count += 1
            if local_ref_count > _MAX_SCHEMA_LOCAL_REFS:
                raise McpRuntimeError("MCP tool input schema has too many local $ref values")
            target_path, target = _resolve_local_schema_ref(schema, node["$ref"])
            graph[path].add(target_path)
            pending_schemas.append((target_path, target))

        for key in schema_map_keywords:
            mapping = node.get(key)
            if not isinstance(mapping, dict):
                continue
            for name, child in mapping.items():
                if isinstance(child, (dict, bool)):
                    child_path = path + (key, name)
                    graph[path].add(child_path)
                    pending_schemas.append((child_path, child))

        dependencies = node.get("dependencies")
        if isinstance(dependencies, dict):
            for name, child in dependencies.items():
                if isinstance(child, (dict, bool)):
                    child_path = path + ("dependencies", name)
                    graph[path].add(child_path)
                    pending_schemas.append((child_path, child))

        for key in schema_list_keywords:
            children = node.get(key)
            if not isinstance(children, list):
                continue
            for index, child in enumerate(children):
                if isinstance(child, (dict, bool)):
                    child_path = path + (key, index)
                    graph[path].add(child_path)
                    pending_schemas.append((child_path, child))

        # Draft-07 also permits the legacy tuple form of `items`.
        items = node.get("items")
        if isinstance(items, list):
            for index, child in enumerate(items):
                if isinstance(child, (dict, bool)):
                    child_path = path + ("items", index)
                    graph[path].add(child_path)
                    pending_schemas.append((child_path, child))

        for key in schema_single_keywords:
            child = node.get(key)
            if isinstance(child, (dict, bool)):
                child_path = path + (key,)
                graph[path].add(child_path)
                pending_schemas.append((child_path, child))

    visiting: set[tuple[Any, ...]] = set()
    visited: set[tuple[Any, ...]] = set()

    def visit(path: tuple[Any, ...]) -> None:
        if path in visiting:
            raise McpRuntimeError("MCP tool input schema contains a recursive local $ref")
        if path in visited:
            return
        visiting.add(path)
        for child_path in graph.get(path, ()):
            visit(child_path)
        visiting.remove(path)
        visited.add(path)

    visit(())


def _mcp_schema_validator(input_schema: Any) -> Any:
    if not isinstance(input_schema, dict):
        raise McpRuntimeError("MCP tool input schema must be an object")
    encoded = _strict_json_bytes(input_schema, label="MCP tool input schema")
    if len(encoded) > _MAX_TOOL_SCHEMA_BYTES:
        raise McpRuntimeError("MCP tool input schema exceeds the byte limit")
    normalized = json.loads(encoded)
    if normalized.get("type") != "object":
        raise McpRuntimeError("MCP tool input schema root type must be object")

    schema_uri = normalized.get("$schema")
    if schema_uri is None:
        validator_class = Draft202012Validator
    else:
        validator_class = _SCHEMA_VALIDATORS.get(_normalized_schema_uri(schema_uri))
        if validator_class is None:
            raise McpRuntimeError("MCP tool input schema uses an unsupported JSON Schema draft")

    _validate_schema_structure(normalized)
    try:
        validator_class.check_schema(normalized)
    except SchemaError as exc:
        raise McpRuntimeError("MCP tool input schema is not valid JSON Schema") from exc
    return validator_class(normalized)


def _validate_argument_structure(arguments: dict[str, Any]) -> None:
    stack: list[tuple[Any, int]] = [(arguments, 0)]
    node_count = 0
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_ARGUMENT_NODES:
            raise McpToolArgumentsInvalid("arguments are too complex")
        if depth > _MAX_ARGUMENT_DEPTH:
            raise McpToolArgumentsInvalid("arguments exceed the nesting limit")
        if isinstance(value, dict):
            if len(value) > _MAX_ARGUMENT_CONTAINER_ITEMS:
                raise McpToolArgumentsInvalid("an argument object has too many fields")
            if any(not isinstance(key, str) for key in value):
                raise McpToolArgumentsInvalid("argument object keys must be strings")
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            if len(value) > _MAX_ARGUMENT_CONTAINER_ITEMS:
                raise McpToolArgumentsInvalid("an argument array has too many items")
            stack.extend((item, depth + 1) for item in value)


def _validation_error_path(error: Any) -> str:
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        elif isinstance(part, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
            path += f".{part}"
        else:
            path += "[" + json.dumps(str(part), ensure_ascii=False) + "]"
    return path


def validate_mcp_tool_arguments(
    input_schema: dict[str, Any],
    arguments: Any,
) -> None:
    """Fully validate one MCP invocation before approval or remote dispatch."""

    if not isinstance(arguments, dict):
        raise McpToolArgumentsInvalid("arguments must be an object")
    try:
        encoded = _strict_json_bytes(arguments, label="MCP tool arguments")
    except McpRuntimeError as exc:
        raise McpToolArgumentsInvalid(str(exc)) from exc
    if len(encoded) > _MAX_MCP_ARGUMENT_BYTES:
        raise McpToolArgumentsInvalid("arguments exceed the byte limit")
    _validate_argument_structure(arguments)
    try:
        validator = _mcp_schema_validator(input_schema)
        error = next(validator.iter_errors(arguments), None)
    except McpRuntimeError as exc:
        raise McpToolArgumentsInvalid("the snapshotted input schema is invalid") from exc
    except Exception as exc:
        raise McpToolArgumentsInvalid("argument validation could not be completed") from exc
    if error is not None:
        message = str(error.message)
        if len(message) > 300:
            message = message[:300] + "..."
        raise McpToolArgumentsInvalid(
            f"arguments do not match the input schema at {_validation_error_path(error)}: {message}"
        )


def _tool_snapshot_size_bytes(tool: McpToolSnapshot) -> int:
    return _json_size_bytes({
        "name": tool.raw_name,
        "title": tool.title,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "annotations": tool.annotations,
    })


def _catalog_snapshot_size_bytes(snapshot: McpCatalogSnapshot) -> int:
    """Logical cache size based on the model-facing immutable payload."""

    return _json_size_bytes({
        "user_id": snapshot.user_id,
        "fingerprint": snapshot.fingerprint,
        "errors": snapshot.errors,
        "tools": [
            {
                "installation_id": tool.installation_id,
                "server_id": tool.server_id,
                "server_name": tool.server_name,
                "source": tool.source,
                "raw_name": tool.raw_name,
                "model_name": tool.model_name,
                "title": tool.title,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "annotations": tool.annotations,
                "schema_hash": tool.schema_hash,
                "connection_fingerprint": tool.connection_fingerprint,
                "stale": tool.stale,
            }
            for tool in snapshot.tools
        ],
    })


def validate_mcp_tool_name(raw_name: str) -> None:
    if not isinstance(raw_name, str) or not raw_name:
        raise McpRuntimeError("MCP tool name must be a non-empty string")
    if raw_name != raw_name.strip():
        raise McpRuntimeError("MCP tool name contains leading or trailing whitespace")
    if raw_name == "*":
        raise McpRuntimeError("MCP tool name '*' is reserved for permission wildcards")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_name):
        raise McpRuntimeError("MCP tool name contains an ASCII control character")
    if len(raw_name) > _MAX_TOOL_NAME_CHARS:
        raise McpRuntimeError("MCP tool name exceeds the 255-character storage limit")


def validate_mcp_tool_metadata(
    *,
    raw_name: str,
    title: str | None,
    description: str,
    input_schema: dict[str, Any],
    annotations: dict[str, Any],
) -> None:
    validate_mcp_tool_name(raw_name)
    if title is not None and len(title) > _MAX_TOOL_TITLE_CHARS:
        raise McpRuntimeError(f"MCP tool {raw_name!r} title exceeds the 255-character storage limit")
    if title is not None and len(title.encode("utf-8")) > _MAX_TOOL_DESCRIPTION_BYTES:
        raise McpRuntimeError(f"MCP tool {raw_name!r} title exceeds the byte limit")
    if len(description.encode("utf-8")) > _MAX_TOOL_DESCRIPTION_BYTES:
        raise McpRuntimeError(f"MCP tool {raw_name!r} description exceeds the byte limit")
    try:
        _mcp_schema_validator(input_schema)
    except McpRuntimeError as exc:
        raise McpRuntimeError(f"MCP tool {raw_name!r} has an invalid input schema: {exc}") from exc
    if _json_size_bytes(annotations) > _MAX_TOOL_ANNOTATIONS_BYTES:
        raise McpRuntimeError(f"MCP tool {raw_name!r} annotations exceed the byte limit")


class _PinnedNetworkBackend:
    """Resolve no names: connect an origin only to its pre-validated addresses.

    httpcore still sees the original URL hostname and therefore supplies it to
    TLS as ``server_hostname``.  Only the underlying TCP destination is replaced
    here, preserving normal SNI and certificate-hostname verification.
    """

    def __init__(self, endpoint: ResolvedMcpEndpoint, backend: Any | None = None):
        if backend is None:
            import httpcore

            backend = httpcore.AnyIOBackend()
        self._endpoint = endpoint
        self._backend = backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        normalized_host = (
            host.decode("ascii") if isinstance(host, bytes) else str(host)
        ).rstrip(".").lower()
        if normalized_host != self._endpoint.hostname or port != self._endpoint.port:
            raise McpRuntimeError("MCP transport attempted an unvalidated TCP destination")

        last_error: Exception | None = None
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.0, timeout)
        for address in self._endpoint.addresses:
            candidate_timeout = timeout
            if deadline is not None:
                candidate_timeout = deadline - loop.time()
                if candidate_timeout <= 0:
                    raise McpRuntimeError("MCP TCP connection exceeded its total timeout")
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=candidate_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise McpRuntimeError("MCP endpoint has no validated TCP destination")

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise McpRuntimeError("MCP transport cannot use Unix domain sockets")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _CumulativeResponseByteBudget:
    """Shared byte budget for every HTTP response in one MCP session."""

    def __init__(self, limit: int):
        if limit <= 0:
            raise ValueError("MCP cumulative response byte limit must be positive")
        self.limit = int(limit)
        self.consumed = 0

    def consume(self, size: int) -> None:
        self.consumed += size
        if self.consumed > self.limit:
            raise McpRuntimeError(
                "MCP Streamable HTTP session exceeded the cumulative response byte limit"
            )


class _LimitedResponseStream(httpx.AsyncByteStream):
    """Bound both one response and its enclosing MCP session."""

    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        limit: int,
        cumulative_budget: _CumulativeResponseByteBudget | None = None,
    ):
        self._stream = stream
        self._limit = limit
        self._consumed = 0
        self._cumulative_budget = cumulative_budget

    async def __aiter__(self):
        async for chunk in self._stream:
            self._consumed += len(chunk)
            if self._consumed > self._limit:
                await self._stream.aclose()
                raise McpRuntimeError("MCP Streamable HTTP response exceeded the byte limit")
            if self._cumulative_budget is not None:
                try:
                    self._cumulative_budget.consume(len(chunk))
                except McpRuntimeError:
                    await self._stream.aclose()
                    raise
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _LimitedAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        response_limit: int,
        *,
        cumulative_budget: _CumulativeResponseByteBudget | None = None,
        cumulative_limit: int | None = None,
    ):
        self._transport = transport
        self._response_limit = response_limit
        if cumulative_budget is not None and cumulative_limit is not None:
            raise ValueError("provide cumulative_budget or cumulative_limit, not both")
        self._cumulative_budget = cumulative_budget or _CumulativeResponseByteBudget(
            cumulative_limit if cumulative_limit is not None else response_limit
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        content_encoding = response.headers.get("content-encoding", "").strip().lower()
        if content_encoding not in {"", "identity"}:
            await response.aclose()
            raise McpRuntimeError("Compressed MCP HTTP responses are not accepted")
        if not isinstance(response.stream, httpx.AsyncByteStream):
            await response.aclose()
            raise McpRuntimeError("MCP HTTP transport returned an unsupported response stream")
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_LimitedResponseStream(
                response.stream,
                self._response_limit,
                self._cumulative_budget,
            ),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


def _secure_http_client(
    endpoint: ResolvedMcpEndpoint,
    *,
    headers: dict[str, str] | None,
    timeout: httpx.Timeout,
    auth: httpx.Auth | None = None,
    cumulative_budget: _CumulativeResponseByteBudget | None = None,
) -> httpx.AsyncClient:
    """Create a no-proxy client whose sockets are bound to validated DNS data."""

    transport = httpx.AsyncHTTPTransport(
        verify=True,
        trust_env=False,
        proxy=None,
        retries=0,
    )
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise McpRuntimeError("httpx cannot install the secure MCP network backend")
    pool._network_backend = _PinnedNetworkBackend(endpoint)  # type: ignore[attr-defined]
    limited_transport = _LimitedAsyncTransport(
        transport,
        _MAX_STREAM_RESPONSE_BYTES,
        cumulative_budget=cumulative_budget,
        cumulative_limit=(
            _MAX_CALL_SESSION_RESPONSE_BYTES
            if cumulative_budget is None
            else None
        ),
    )
    client_headers = dict(headers or {})
    # The byte limiter observes transport bytes. Requiring identity encoding
    # prevents a compressed response from expanding past the limit afterwards.
    client_headers["Accept-Encoding"] = "identity"
    return httpx.AsyncClient(
        headers=client_headers,
        timeout=timeout,
        auth=auth,
        transport=limited_transport,
        follow_redirects=False,
        trust_env=False,
    )


class _SdkSessionConnector:
    """Thin compatibility adapter for the official MCP Python SDK v1.x."""

    def __init__(self, *, connect_timeout: float = 15.0, read_timeout: float = 300.0):
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    @staticmethod
    def _sdk():
        try:
            from mcp import ClientSession
            from mcp.client import streamable_http as transport_module
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise McpRuntimeError("official MCP Python SDK is not installed") from exc

        transport = getattr(transport_module, "streamable_http_client", None)
        if transport is None:
            transport = getattr(transport_module, "streamablehttp_client", None)
        if transport is None:  # pragma: no cover - unsupported SDK build
            raise McpRuntimeError("MCP SDK has no Streamable HTTP client")
        return ClientSession, transport

    @asynccontextmanager
    async def _session(
        self,
        installation: EffectiveMcpInstallation,
        *,
        response_budget_bytes: int = _MAX_CALL_SESSION_RESPONSE_BYTES,
    ) -> AsyncIterator[Any]:
        ClientSession, transport = self._sdk()
        # Capture DNS once, immediately before creating the client. The custom
        # network backend below connects only to these validated addresses while
        # the URL retains its hostname for HTTP routing and TLS SNI verification.
        try:
            endpoint = await asyncio.wait_for(
                asyncio.to_thread(
                    resolve_mcp_endpoint,
                    installation.url,
                    allow_private_network=installation.allow_private_network,
                    allow_insecure_http=installation.allow_insecure_http,
                ),
                timeout=self.connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise McpRuntimeError("MCP DNS resolution timed out") from exc
        timeout = httpx.Timeout(
            timeout=self.read_timeout,
            connect=self.connect_timeout,
            read=self.read_timeout,
        )
        response_budget = _CumulativeResponseByteBudget(response_budget_bytes)
        try:
            parameters = inspect.signature(transport).parameters
        except (TypeError, ValueError) as exc:
            raise McpRuntimeError(
                "MCP SDK transport cannot be inspected for secure HTTP client injection"
            ) from exc
        async_client: httpx.AsyncClient | None = None
        if "http_client" in parameters:
            async_client = _secure_http_client(
                endpoint,
                headers=dict(installation.headers),
                timeout=timeout,
                cumulative_budget=response_budget,
            )
            transport_context = transport(
                endpoint.url,
                http_client=async_client,
                terminate_on_close=True,
            )
        elif "httpx_client_factory" in parameters:
            def secure_client_factory(
                headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None,
                auth: httpx.Auth | None = None,
            ) -> httpx.AsyncClient:
                return _secure_http_client(
                    endpoint,
                    headers=headers,
                    timeout=timeout or httpx.Timeout(self.read_timeout),
                    auth=auth,
                    cumulative_budget=response_budget,
                )

            transport_context = transport(
                endpoint.url,
                headers=dict(installation.headers),
                timeout=self.connect_timeout,
                sse_read_timeout=self.read_timeout,
                terminate_on_close=True,
                httpx_client_factory=secure_client_factory,
            )
        else:
            raise McpRuntimeError(
                "MCP SDK transport cannot inject the required secure HTTP client"
            )

        try:
            async with transport_context as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self.read_timeout),
                ) as session:
                    await session.initialize()
                    yield session
        finally:
            if async_client is not None:
                await async_client.aclose()

    async def list_tools(self, installation: EffectiveMcpInstallation) -> list[Any]:
        all_tools: list[Any] = []
        catalog_bytes = 0
        cursor: str | None = None
        async with self._session(
            installation,
            response_budget_bytes=_MAX_DISCOVERY_SESSION_RESPONSE_BYTES,
        ) as session:
            for _page in range(_MAX_TOOL_LIST_PAGES):
                result = await session.list_tools(cursor) if cursor else await session.list_tools()
                page_tools = list(getattr(result, "tools", None) or [])
                for remote_tool in page_tools:
                    catalog_bytes += _json_size_bytes(_as_dict(remote_tool))
                    if catalog_bytes > _MAX_INSTALLATION_CATALOG_BYTES:
                        raise McpRuntimeError(
                            f"MCP server {installation.server_name!r} tool catalog "
                            "exceeded the cumulative byte limit"
                        )
                all_tools.extend(page_tools)
                if len(all_tools) > _MAX_TOOLS_PER_INSTALLATION:
                    raise McpRuntimeError(
                        f"MCP server {installation.server_name!r} exposes too many tools"
                    )
                cursor = getattr(result, "nextCursor", None) or getattr(result, "next_cursor", None)
                if cursor is not None:
                    if not isinstance(cursor, str):
                        raise McpRuntimeError(
                            f"MCP server {installation.server_name!r} returned an invalid cursor"
                        )
                    if len(cursor.encode("utf-8")) > _MAX_TOOL_CURSOR_BYTES:
                        raise McpRuntimeError(
                            f"MCP server {installation.server_name!r} returned an oversized cursor"
                        )
                if not cursor:
                    return all_tools
        raise McpRuntimeError(f"MCP server {installation.server_name!r} tool pagination did not terminate")

    async def call_tool(
        self,
        installation: EffectiveMcpInstallation,
        raw_name: str,
        arguments: dict[str, Any],
        *,
        on_dispatch: Callable[[], None] | None = None,
    ) -> Any:
        # Intentionally exactly one request: writes must never be retried here.
        async with self._session(
            installation,
            response_budget_bytes=_MAX_CALL_SESSION_RESPONSE_BYTES,
        ) as session:
            # Everything before this point is local setup/handshake.  Once the
            # SDK call begins we conservatively assume the server may execute
            # the request even if cancellation or a read error follows.
            if on_dispatch is not None:
                on_dispatch()
            return await session.call_tool(raw_name, arguments=arguments)


class _SqlAlchemyMcpRepository:
    """SQLAlchemy adapter for the database-backed MCP catalog."""

    def __init__(
        self,
        session_factory=SessionLocal,
        *,
        catalog_refresh_seconds: float | None = None,
        clock: Callable[[], float] = time,
    ):
        self._session_factory = session_factory
        self._catalog_refresh_seconds = float(
            catalog_refresh_seconds
            if catalog_refresh_seconds is not None
            else get_settings().mcp_catalog_refresh_seconds
        )
        if self._catalog_refresh_seconds <= 0:
            raise ValueError("catalog_refresh_seconds must be positive")
        self._clock = clock

    @staticmethod
    def _models():
        from src.api.models.mcp import (
            McpConfigVersion,
            McpCredential,
            McpInstallation,
            McpServer,
            McpToolSnapshot as McpToolSnapshotRow,
            McpToolVisibility,
        )

        return (
            McpConfigVersion,
            McpCredential,
            McpInstallation,
            McpServer,
            McpToolSnapshotRow,
            McpToolVisibility,
        )

    def _provision_required_installations(self, db, user_id: str) -> bool:
        _, _, McpInstallation, McpServer, _, _ = self._models()
        required_server_ids = [
            str(row[0])
            for row in (
                db.query(McpServer.id)
                .filter(
                    McpServer.source == "official",
                    McpServer.status == "published",
                    McpServer.required.is_(True),
                )
                .order_by(McpServer.id)
                .all()
            )
        ]
        required_installations = (
            {
                str(row.server_id): row
                for row in db.query(McpInstallation)
                .filter(
                    McpInstallation.user_id == user_id,
                    McpInstallation.server_id.in_(required_server_ids),
                )
                .all()
            }
            if required_server_ids
            else {}
        )
        required_changed = False
        for server_id in required_server_ids:
            required_installation = required_installations.get(server_id)
            if required_installation is not None and required_installation.enabled:
                continue
            # Only lock stable server rows that actually need provisioning.
            # Recheck after the lock in case another worker won the race.
            server = (
                db.query(McpServer)
                .filter(
                    McpServer.id == server_id,
                    McpServer.source == "official",
                    McpServer.status == "published",
                    McpServer.required.is_(True),
                )
                .with_for_update()
                .first()
            )
            if server is None:
                continue
            required_installation = (
                db.query(McpInstallation)
                .filter(
                    McpInstallation.server_id == server_id,
                    McpInstallation.user_id == user_id,
                )
                .first()
            )
            if required_installation is None:
                db.add(McpInstallation(
                    server_id=server_id,
                    user_id=user_id,
                    enabled=True,
                ))
                required_changed = True
            elif not required_installation.enabled:
                required_installation.enabled = True
                required_changed = True
        if required_changed:
            db.flush()
        return required_changed

    def _resolve_row(self, db, user_id: str, installation_id: str | None = None):
        """Resolve catalog rows without mutating or committing caller state."""

        _, McpCredential, McpInstallation, McpServer, _, McpToolVisibility = self._models()
        query = (
            db.query(McpInstallation, McpServer)
            .join(McpServer, McpServer.id == McpInstallation.server_id)
            .filter(
                McpInstallation.user_id == user_id,
                McpInstallation.enabled.is_(True),
                McpServer.status == "published",
            )
        )
        if installation_id is not None:
            query = query.filter(McpInstallation.id == installation_id)
        rows = query.order_by(McpServer.id, McpInstallation.id).all()
        resolved = []
        for installation, server in rows:
            if server.source == "personal" and server.owner_user_id != user_id:
                continue

            credential = None
            configuration_error: str | None = None
            if installation.credential_id:
                credential = db.query(McpCredential).filter(
                    McpCredential.id == installation.credential_id,
                    McpCredential.server_id == server.id,
                ).first()
                valid_owners = (None, user_id) if server.source == "official" else (user_id,)
                if credential is None or credential.user_id not in valid_owners:
                    if not bool(server.required):
                        continue
                    credential = None
                    configuration_error = "required MCP credential is missing or not owned by this user"
            elif server.auth_type != "none":
                credential_query = db.query(McpCredential).filter(
                    McpCredential.server_id == server.id,
                )
                credential = (
                    credential_query.filter(McpCredential.user_id.is_(None)).first()
                    if server.source == "official"
                    else credential_query.filter(McpCredential.user_id == user_id).first()
                )
                if credential is None:
                    if not bool(server.required):
                        continue
                    configuration_error = "required MCP credential is not configured"

            encrypted_secret = getattr(credential, "encrypted_secret", None) if credential else None
            try:
                headers = credential_headers(server.auth_type, encrypted_secret)
            except McpSecurityError:
                if not bool(server.required):
                    continue
                headers = {}
                configuration_error = "required MCP credential is invalid or unavailable"
            credential_fp = secret_fingerprint(encrypted_secret) if encrypted_secret else ""
            visibility = db.query(McpToolVisibility).filter(
                McpToolVisibility.installation_id == installation.id
            ).first()
            enabled_tools: frozenset[str] | None = None
            disabled_tools: frozenset[str] = frozenset()
            if visibility is not None:
                try:
                    enabled_value = (
                        None
                        if visibility.enabled_tools_json is None
                        else json.loads(visibility.enabled_tools_json)
                    )
                    disabled_value = json.loads(visibility.disabled_tools_json or "[]")
                    if enabled_value is not None and not isinstance(enabled_value, list):
                        raise ValueError("invalid enabled_tools")
                    if not isinstance(disabled_value, list):
                        raise ValueError("invalid disabled_tools")
                    if enabled_value is not None and not all(
                        isinstance(name, str)
                        and name
                        and name == name.strip()
                        and name != "*"
                        and len(name) <= _MAX_TOOL_NAME_CHARS
                        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
                        for name in enabled_value
                    ):
                        raise ValueError("invalid enabled_tools names")
                    if not all(
                        isinstance(name, str)
                        and name
                        and name == name.strip()
                        and name != "*"
                        and len(name) <= _MAX_TOOL_NAME_CHARS
                        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
                        for name in disabled_value
                    ):
                        raise ValueError("invalid disabled_tools names")
                    enabled_tools = (
                        None
                        if enabled_value is None
                        else frozenset(enabled_value)
                    )
                    disabled_tools = frozenset(disabled_value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    # Corrupt policy fails closed.
                    enabled_tools = frozenset()
                    disabled_tools = frozenset()
            resolved.append(EffectiveMcpInstallation(
                installation_id=str(installation.id),
                server_id=str(server.id),
                user_id=user_id,
                server_name=str(server.name),
                url=str(server.url),
                headers=headers,
                required=bool(getattr(server, "required", False)),
                source=str(server.source),
                server_version=int(server.version or 1),
                auth_type=str(server.auth_type),
                credential_fingerprint=credential_fp,
                allow_private_network=bool(server.allow_private_network),
                allow_insecure_http=bool(server.allow_insecure_http),
                enabled_tools=enabled_tools,
                disabled_tools=disabled_tools,
                configuration_error=configuration_error,
            ))
        return resolved

    def list_effective_installations(self, user_id: str) -> list[EffectiveMcpInstallation]:
        with self._session_factory() as db:
            if self._provision_required_installations(db, user_id):
                db.commit()
            result = self._resolve_row(db, user_id)
            db.rollback()
            return result

    def get_effective_installation(
        self,
        user_id: str,
        installation_id: str,
    ) -> EffectiveMcpInstallation | None:
        with self._session_factory() as db:
            if self._provision_required_installations(db, user_id):
                db.commit()
            rows = self._resolve_row(db, user_id, installation_id)
            db.rollback()
            return rows[0] if rows else None

    def catalog_fingerprint(self, user_id: str) -> str:
        McpConfigVersion, _, _, _, _, _ = self._models()
        with self._session_factory() as db:
            if self._provision_required_installations(db, user_id):
                db.commit()
            versions = {
                row.scope_key: int(row.version or 0)
                for row in db.query(McpConfigVersion)
                .filter(McpConfigVersion.scope_key.in_(["global", f"user:{user_id}"]))
                .all()
            }
            installations = self._resolve_row(db, user_id)
            payload = {
                "versions": versions,
                "installations": [item.connection_fingerprint for item in installations],
            }
            if installations:
                payload["refresh_bucket"] = int(
                    self._clock() // self._catalog_refresh_seconds
                )
            db.rollback()
            return _stable_hash(payload)

    def load_tool_snapshots(self, installation: EffectiveMcpInstallation) -> list[McpToolSnapshot]:
        _, _, _, _, SnapshotRow, _ = self._models()
        with self._session_factory() as db:
            rows = (
                db.query(SnapshotRow)
                .filter(SnapshotRow.installation_id == installation.installation_id)
                .order_by(SnapshotRow.tool_name)
                .all()
            )
            result = []
            for row in rows:
                stored_connection_fingerprint = str(
                    getattr(row, "connection_fingerprint", None) or ""
                )
                if stored_connection_fingerprint != installation.execution_fingerprint:
                    # Never project a schema discovered from an old endpoint or
                    # credential onto a newly configured execution target.
                    continue
                raw_name = str(row.tool_name)
                try:
                    validate_mcp_tool_name(raw_name)
                except McpRuntimeError:
                    # Legacy/corrupt snapshots with ambiguous identities are
                    # never exposed or routed to a remote server.
                    continue
                try:
                    input_schema = json.loads(row.input_schema_json or "{}")
                    annotations = json.loads(row.annotations_json or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(input_schema, dict) or not isinstance(annotations, dict):
                    continue
                title = str(row.title) if row.title is not None else None
                description = str(row.description or title or row.tool_name)
                try:
                    validate_mcp_tool_metadata(
                        raw_name=raw_name,
                        title=title,
                        description=description,
                        input_schema=input_schema,
                        annotations=annotations,
                    )
                except McpRuntimeError:
                    # Legacy or corrupt oversized snapshots fail closed.
                    continue
                result.append(McpToolSnapshot(
                    installation_id=installation.installation_id,
                    server_id=installation.server_id,
                    server_name=installation.server_name,
                    source=installation.source,
                    raw_name=raw_name,
                    model_name=model_tool_name(installation.server_id, raw_name),
                    title=title,
                    description=description,
                    input_schema=input_schema,
                    annotations=annotations,
                    schema_hash=str(row.schema_hash),
                    connection_fingerprint=stored_connection_fingerprint,
                    stale=True,
                ))
            db.rollback()
            return [tool for tool in result if installation.publishes_tool(tool.raw_name)]

    def get_tool_snapshot_binding(
        self,
        installation: EffectiveMcpInstallation,
        raw_name: str,
    ) -> tuple[str, str] | None:
        """Read the current durable schema/target binding immediately before a call."""

        _, _, _, _, SnapshotRow, _ = self._models()
        with self._session_factory() as db:
            row = (
                db.query(SnapshotRow.schema_hash, SnapshotRow.connection_fingerprint)
                .filter(
                    SnapshotRow.installation_id == installation.installation_id,
                    SnapshotRow.tool_name == raw_name,
                )
                .first()
            )
            db.rollback()
            if row is None:
                return None
            return (
                str(row.schema_hash or ""),
                str(row.connection_fingerprint or ""),
            )

    def replace_tool_snapshots(
        self,
        installation: EffectiveMcpInstallation,
        tools: list[McpToolSnapshot],
    ) -> bool:
        """Replace snapshots only while the discovered execution target is current.

        The remote ``tools/list`` call runs without database locks.  Revalidate
        its target after the network round trip and serialize with every
        connection mutation on the stable server row before publishing the
        result.  ``False`` tells the caller that the discovery result is stale
        and must not be returned or cached.
        """

        from src.api.utils.timezone import now_naive

        _, _, McpInstallation, McpServer, SnapshotRow, _ = self._models()
        with self._session_factory() as db:
            # Read only scalar identity first, then acquire locks in the same
            # server -> installation order used by configuration write paths.
            # Locking the installation first would deadlock with
            # update_connection(), which already holds the server row.
            candidate = (
                db.query(McpInstallation.server_id, McpInstallation.user_id)
                .filter(McpInstallation.id == installation.installation_id)
                .first()
            )
            if candidate is None or str(candidate.user_id) != installation.user_id:
                db.rollback()
                return False
            locked_server = (
                db.query(McpServer)
                .filter(McpServer.id == candidate.server_id)
                .with_for_update()
                .first()
            )
            locked = (
                db.query(McpInstallation)
                .filter(McpInstallation.id == installation.installation_id)
                .with_for_update()
                .first()
            )
            if (
                locked_server is None
                or locked is None
                or str(locked.user_id) != installation.user_id
                or str(locked.server_id) != installation.server_id
                or str(locked_server.id) != installation.server_id
            ):
                db.rollback()
                return False

            current = self._resolve_row(
                db,
                installation.user_id,
                installation.installation_id,
            )
            if (
                len(current) != 1
                or current[0].execution_fingerprint
                != installation.execution_fingerprint
            ):
                db.rollback()
                return False
            if any(
                tool.installation_id != installation.installation_id
                or tool.server_id != installation.server_id
                or tool.connection_fingerprint
                != installation.execution_fingerprint
                for tool in tools
            ):
                raise ValueError("MCP tool snapshots do not match their discovery target")

            discovered_at = now_naive()
            existing_identity = sorted(
                [
                    (
                        str(row.tool_name),
                        str(row.title) if row.title is not None else None,
                        str(row.schema_hash or ""),
                        str(row.connection_fingerprint or ""),
                    )
                    for row in (
                        db.query(SnapshotRow)
                        .filter(SnapshotRow.installation_id == installation.installation_id)
                        .all()
                    )
                ],
                key=lambda item: item[0],
            )
            new_identity = sorted(
                [
                    (
                        tool.raw_name,
                        tool.title,
                        tool.schema_hash,
                        tool.connection_fingerprint,
                    )
                    for tool in tools
                ],
                key=lambda item: item[0],
            )
            if existing_identity == new_identity:
                db.query(SnapshotRow).filter(
                    SnapshotRow.installation_id == installation.installation_id
                ).update(
                    {SnapshotRow.discovered_at: discovered_at},
                    synchronize_session=False,
                )
                db.commit()
                return True
            db.query(SnapshotRow).filter(
                SnapshotRow.installation_id == installation.installation_id
            ).delete(synchronize_session=False)
            for tool in tools:
                db.add(SnapshotRow(
                    installation_id=installation.installation_id,
                    tool_name=tool.raw_name,
                    title=tool.title,
                    description=tool.description,
                    input_schema_json=json.dumps(tool.input_schema, ensure_ascii=False, sort_keys=True),
                    annotations_json=json.dumps(tool.annotations, ensure_ascii=False, sort_keys=True),
                    schema_hash=tool.schema_hash,
                    connection_fingerprint=tool.connection_fingerprint,
                    discovered_at=discovered_at,
                ))
            # Local import avoids the module-level mcp_service -> mcp_runtime
            # dependency cycle while reusing its dialect-safe atomic upsert.
            from src.api.services.mcp_service import bump_config_version

            bump_config_version(db, installation.user_id)
            db.commit()
            return True


def resolve_effective_mcp_installation(
    db: Any,
    *,
    user_id: str,
    installation_id: str,
) -> EffectiveMcpInstallation | None:
    """Resolve a live installation inside an existing policy transaction."""

    rows = _SqlAlchemyMcpRepository()._resolve_row(db, user_id, installation_id)
    return rows[0] if rows else None


class McpRuntime:
    """Resolve cached tools and execute one-shot Streamable HTTP calls."""

    def __init__(
        self,
        repository: McpRepository | None = None,
        connector: McpSessionConnector | None = None,
        *,
        cache_max_users: int | None = None,
        cache_max_bytes: int | None = None,
        cache_idle_ttl_seconds: float | None = None,
        call_timeout_seconds: float | None = None,
        clock: Callable[[], float] = time,
    ):
        settings = get_settings()
        self.repository = repository or _SqlAlchemyMcpRepository()
        self.connector = connector or _SdkSessionConnector()
        self._cache_max_users = int(
            cache_max_users
            if cache_max_users is not None
            else getattr(settings, "mcp_catalog_cache_max_users", 64)
        )
        self._cache_max_bytes = int(
            cache_max_bytes
            if cache_max_bytes is not None
            else getattr(settings, "mcp_catalog_cache_max_bytes", 64 * 1024 * 1024)
        )
        self._cache_idle_ttl_seconds = float(
            cache_idle_ttl_seconds
            if cache_idle_ttl_seconds is not None
            else getattr(settings, "mcp_catalog_cache_idle_ttl_seconds", 900.0)
        )
        self._call_timeout_seconds = float(
            call_timeout_seconds
            if call_timeout_seconds is not None
            else getattr(settings, "mcp_call_timeout_seconds", 300.0)
        )
        if self._cache_max_users <= 0:
            raise ValueError("cache_max_users must be positive")
        if self._cache_max_bytes <= 0:
            raise ValueError("cache_max_bytes must be positive")
        if self._cache_idle_ttl_seconds <= 0:
            raise ValueError("cache_idle_ttl_seconds must be positive")
        if not 0 < self._call_timeout_seconds <= _MAX_MCP_CALL_TIMEOUT_SECONDS:
            raise ValueError("call_timeout_seconds must be > 0 and <= 600")
        self._clock = clock
        self._catalog_cache: OrderedDict[
            tuple[str, str],
            _CatalogCacheEntry,
        ] = OrderedDict()
        self._catalog_cache_bytes = 0
        self._resolved_fingerprints: dict[str, str] = {}
        self._resolve_locks: weakref.WeakValueDictionary[
            str,
            asyncio.Lock,
        ] = weakref.WeakValueDictionary()

    def _drop_cache_key(self, cache_key: tuple[str, str]) -> None:
        entry = self._catalog_cache.pop(cache_key, None)
        if entry is None:
            return
        self._catalog_cache_bytes = max(
            0,
            self._catalog_cache_bytes - entry.logical_bytes,
        )
        user_id = cache_key[0]
        if not any(key[0] == user_id for key in self._catalog_cache):
            self._resolved_fingerprints.pop(user_id, None)

    def _evict_user_cache(self, user_id: str) -> None:
        for cache_key in [
            key for key in self._catalog_cache if key[0] == user_id
        ]:
            self._drop_cache_key(cache_key)
        self._resolved_fingerprints.pop(user_id, None)

    def _prune_idle_cache(self, now: float | None = None) -> None:
        current = self._clock() if now is None else now
        for cache_key, entry in list(self._catalog_cache.items()):
            if current - entry.last_access_at < self._cache_idle_ttl_seconds:
                # OrderedDict is LRU ordered, so newer entries cannot be idle.
                break
            self._drop_cache_key(cache_key)

    def _cached_catalog(
        self,
        cache_key: tuple[str, str],
    ) -> McpCatalogSnapshot | None:
        now = self._clock()
        self._prune_idle_cache(now)
        entry = self._catalog_cache.get(cache_key)
        if entry is None:
            return None
        entry.last_access_at = now
        self._catalog_cache.move_to_end(cache_key)
        self._resolved_fingerprints[cache_key[0]] = cache_key[1]
        return entry.snapshot

    def _cache_catalog(self, snapshot: McpCatalogSnapshot) -> None:
        cache_key = (snapshot.user_id, snapshot.fingerprint)
        self._evict_user_cache(snapshot.user_id)
        logical_bytes = _catalog_snapshot_size_bytes(snapshot)
        if logical_bytes > self._cache_max_bytes:
            # Return oversized-but-valid catalogs to the current caller without
            # retaining an unbounded fingerprint/user side table.
            return
        now = self._clock()
        self._prune_idle_cache(now)
        self._catalog_cache[cache_key] = _CatalogCacheEntry(
            snapshot=snapshot,
            logical_bytes=logical_bytes,
            last_access_at=now,
        )
        self._catalog_cache_bytes += logical_bytes
        self._resolved_fingerprints[snapshot.user_id] = snapshot.fingerprint
        while (
            len(self._catalog_cache) > self._cache_max_users
            or self._catalog_cache_bytes > self._cache_max_bytes
        ):
            oldest_key = next(iter(self._catalog_cache))
            self._drop_cache_key(oldest_key)

    def _resolve_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._resolve_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._resolve_locks[user_id] = lock
        return lock

    def catalog_fingerprint(self, user_id: str) -> str:
        return self.repository.catalog_fingerprint(user_id)

    def last_resolved_fingerprint(self, user_id: str) -> str | None:
        self._prune_idle_cache()
        return self._resolved_fingerprints.get(user_id)

    def current_execution_fingerprint(
        self,
        *,
        user_id: str,
        installation_id: str,
    ) -> str | None:
        """Resolve the live endpoint/credential target for an Agent check."""

        installation = self.repository.get_effective_installation(user_id, installation_id)
        return installation.execution_fingerprint if installation is not None else None

    async def resolve_catalog(self, user_id: str) -> McpCatalogSnapshot:
        fingerprint = self.catalog_fingerprint(user_id)
        cache_key = (user_id, fingerprint)
        cached = self._cached_catalog(cache_key)
        if cached is not None:
            return cached

        lock = self._resolve_lock(user_id)
        async with lock:
            # A waiter may have observed an older config/refresh fingerprint
            # before the first resolver completed.
            fingerprint = self.catalog_fingerprint(user_id)
            cache_key = (user_id, fingerprint)
            cached = self._cached_catalog(cache_key)
            if cached is not None:
                return cached

            def installation_key(
                item: EffectiveMcpInstallation,
            ) -> tuple[str, str]:
                return (item.server_id, item.installation_id)

            all_installations = sorted(
                self.repository.list_effective_installations(user_id),
                key=installation_key,
            )
            required_installations = [
                item for item in all_installations if item.required
            ]
            optional_installations = [
                item for item in all_installations if not item.required
            ]
            if len(required_installations) > _MAX_INSTALLATIONS_PER_CATALOG:
                raise McpRequiredServerUnavailable(
                    "required MCP catalog exceeds the per-user installation limit"
                )

            optional_slots = max(
                0,
                _MAX_INSTALLATIONS_PER_CATALOG - len(required_installations),
            )
            selected_optional = optional_installations[:optional_slots]
            skipped_optional = optional_installations[optional_slots:]
            errors: list[str] = [
                (
                    f"{' '.join(item.server_name.split())[:255]}: skipped because "
                    "the MCP catalog exceeds the per-user installation limit"
                )
                for item in skipped_optional
            ]

            discovery_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_MCP_DISCOVERIES)
            global_discovery_semaphore = _global_discovery_semaphore()
            deadline = (
                asyncio.get_running_loop().time()
                + _CATALOG_BUILD_TIMEOUT_SECONDS
            )

            async def discover_bounded(
                installation: EffectiveMcpInstallation,
            ) -> list[McpToolSnapshot]:
                async with discovery_semaphore:
                    async with global_discovery_semaphore:
                        try:
                            return await asyncio.wait_for(
                                self._discover_installation(installation),
                                timeout=_DISCOVERY_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError as exc:
                            raise McpRuntimeError(
                                "MCP tools/list discovery exceeded its wall-clock timeout"
                            ) from exc

            async def discover_phase(
                phase_installations: list[EffectiveMcpInstallation],
            ) -> list[
                tuple[EffectiveMcpInstallation, list[McpToolSnapshot] | BaseException]
            ]:
                if not phase_installations:
                    return []
                tasks = [
                    asyncio.create_task(discover_bounded(item))
                    for item in phase_installations
                ]
                remaining = max(
                    0.0,
                    deadline - asyncio.get_running_loop().time(),
                )
                try:
                    _, pending = await asyncio.wait(tasks, timeout=remaining)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise

                pending_set = set(pending)
                for task in pending_set:
                    task.cancel()
                if pending_set:
                    await asyncio.gather(*pending_set, return_exceptions=True)

                phase_results: list[
                    tuple[
                        EffectiveMcpInstallation,
                        list[McpToolSnapshot] | BaseException,
                    ]
                ] = []
                for item, task in zip(phase_installations, tasks):
                    if task in pending_set:
                        result: list[McpToolSnapshot] | BaseException = McpRuntimeError(
                            "MCP catalog build exceeded its wall-clock timeout"
                        )
                    else:
                        try:
                            result = task.result()
                        except BaseException as exc:
                            result = exc
                    phase_results.append((item, result))
                return phase_results

            tools: list[McpToolSnapshot] = []
            catalog_bytes = 0
            model_names: set[str] = set()

            def consume_result(
                installation: EffectiveMcpInstallation,
                result: list[McpToolSnapshot] | BaseException,
                *,
                required: bool,
            ) -> str | None:
                nonlocal catalog_bytes
                if isinstance(result, BaseException):
                    server_name = " ".join(installation.server_name.split())[:255]
                    return (
                        f"{server_name}: "
                        f"{_sanitize_mcp_exception(installation, result)}"
                    )

                server_name = " ".join(installation.server_name.split())[:255]
                if len(tools) + len(result) > _MAX_TOOLS_PER_USER_CATALOG:
                    return (
                        f"{server_name}: "
                        f"{'required MCP catalog' if required else 'skipped because the MCP catalog'} "
                        "exceeds the per-user tool limit"
                    )
                result_bytes = sum(
                    _tool_snapshot_size_bytes(tool) for tool in result
                )
                if catalog_bytes + result_bytes > _MAX_USER_CATALOG_BYTES:
                    return (
                        f"{server_name}: "
                        f"{'required MCP catalog' if required else 'skipped because the MCP catalog'} "
                        "exceeds the per-user cumulative byte limit"
                    )
                candidate_names = [tool.model_name for tool in result]
                if (
                    len(candidate_names) != len(set(candidate_names))
                    or any(name in model_names for name in candidate_names)
                ):
                    return (
                        f"{server_name}: MCP model-visible tool name collision"
                    )

                catalog_bytes += result_bytes
                tools.extend(result)
                model_names.update(candidate_names)
                return None

            required_errors: list[str] = []
            for installation, result in await discover_phase(
                required_installations
            ):
                message = consume_result(
                    installation,
                    result,
                    required=True,
                )
                if message is not None:
                    required_errors.append(message)

            if required_errors:
                raise McpRequiredServerUnavailable("; ".join(required_errors))

            for installation, result in await discover_phase(selected_optional):
                message = consume_result(
                    installation,
                    result,
                    required=False,
                )
                if message is not None:
                    # Durable snapshots remain useful for management and
                    # call-time binding checks, but a failed or over-budget
                    # optional service is absent from the executable catalog.
                    errors.append(message)

            # Snapshot persistence can advance the catalog generation itself.
            # Sample the generation around a fresh effective-installation read
            # so config changes cannot make a mixed old/new catalog look current.
            final_fingerprint_before = self.catalog_fingerprint(user_id)
            current_installations = sorted(
                self.repository.list_effective_installations(user_id),
                key=installation_key,
            )
            final_fingerprint = self.catalog_fingerprint(user_id)
            if (
                final_fingerprint_before != final_fingerprint
                or current_installations != all_installations
            ):
                raise McpToolSnapshotStale(
                    "MCP configuration changed during catalog discovery"
                )

            self._assert_unique_model_names(tools)
            snapshot = McpCatalogSnapshot(
                user_id=user_id,
                fingerprint=final_fingerprint,
                tools=tuple(sorted(tools, key=lambda item: item.model_name)),
                errors=tuple(errors),
            )
            # Optional failures are a valid partial snapshot for this exact
            # config/refresh generation. Failed installations remain absent,
            # and the next refresh bucket or config version retries discovery.
            self._cache_catalog(snapshot)
            return snapshot

    async def _discover_installation(
        self,
        installation: EffectiveMcpInstallation,
    ) -> list[McpToolSnapshot]:
        if installation.configuration_error:
            raise McpRuntimeError(installation.configuration_error)
        remote_tools = await self.connector.list_tools(installation)
        projected: list[McpToolSnapshot] = []
        for remote in remote_tools:
            raw = _as_dict(remote)
            raw_name = raw.get("name") or getattr(remote, "name", None)
            if not isinstance(raw_name, str) or not raw_name:
                raise McpRuntimeError(f"MCP server {installation.server_name!r} returned a nameless tool")
            validate_mcp_tool_name(raw_name)
            input_schema = raw.get("inputSchema", raw.get("input_schema", {}))
            if not isinstance(input_schema, dict):
                raise McpRuntimeError(f"MCP tool {raw_name!r} has an invalid input schema")
            annotations = raw.get("annotations") or {}
            if not isinstance(annotations, dict):
                annotations = _as_dict(annotations)
            title = raw.get("title")
            if title is not None:
                title = str(title)
            description = str(raw.get("description") or title or raw_name)
            validate_mcp_tool_metadata(
                raw_name=raw_name,
                title=title,
                description=description,
                input_schema=input_schema,
                annotations=annotations,
            )
            schema_hash = mcp_tool_schema_hash(
                raw_name=raw_name,
                description=description,
                input_schema=input_schema,
                annotations=annotations,
            )
            projected.append(McpToolSnapshot(
                installation_id=installation.installation_id,
                server_id=installation.server_id,
                server_name=installation.server_name,
                source=installation.source,
                raw_name=raw_name,
                model_name=model_tool_name(installation.server_id, raw_name),
                title=title,
                description=description,
                input_schema=input_schema,
                annotations=annotations,
                schema_hash=schema_hash,
                connection_fingerprint=installation.execution_fingerprint,
            ))
        # Persist every discovered tool so the management API can show hidden
        # tools and users can re-enable them without a new successful probe.
        if not self.repository.replace_tool_snapshots(installation, projected):
            raise McpToolSnapshotStale(
                "MCP configuration changed during tools/list discovery"
            )
        visible = [
            tool for tool in projected if installation.publishes_tool(tool.raw_name)
        ]
        self._assert_unique_model_names(visible)
        return visible

    @staticmethod
    def _assert_unique_model_names(tools: list[McpToolSnapshot]) -> None:
        seen: dict[str, McpToolSnapshot] = {}
        for tool in tools:
            previous = seen.get(tool.model_name)
            if previous is not None:
                raise McpToolNameCollisionError(
                    f"MCP tool name collision: {previous.server_id}/{previous.raw_name} and "
                    f"{tool.server_id}/{tool.raw_name} -> {tool.model_name}"
                )
            seen[tool.model_name] = tool

    async def call_tool(
        self,
        *,
        user_id: str,
        tool: McpToolSnapshot,
        arguments: dict[str, Any],
        cancel_token: asyncio.Event | None = None,
    ) -> Any:
        """Execute one remote call after a fresh DB authorization check.

        This method never retries.  A cancelled Turn actively cancels the SDK
        task, which closes its Streamable HTTP response/session contexts.
        """

        installation = self.repository.get_effective_installation(user_id, tool.installation_id)
        if installation is None:
            raise McpInstallationUnavailable("MCP installation is disabled, missing, or not owned by this user")
        if installation.server_id != tool.server_id:
            raise McpInstallationUnavailable("MCP installation no longer points to the snapshotted server")
        try:
            validate_mcp_tool_name(tool.raw_name)
        except McpRuntimeError as exc:
            raise McpInstallationUnavailable("MCP tool snapshot has an invalid identity") from exc
        if installation.configuration_error:
            raise McpInstallationUnavailable(installation.configuration_error)
        if not installation.publishes_tool(tool.raw_name):
            raise McpToolNotPublished("MCP tool is disabled for this installation")
        if (
            not tool.connection_fingerprint
            or installation.execution_fingerprint != tool.connection_fingerprint
        ):
            raise McpInstallationUnavailable(
                "MCP endpoint or credential changed after this tool snapshot was created"
            )
        current_binding = self.repository.get_tool_snapshot_binding(
            installation,
            tool.raw_name,
        )
        if (
            not tool.schema_hash
            or current_binding != (tool.schema_hash, tool.connection_fingerprint)
        ):
            raise McpToolSnapshotStale(
                "MCP tool schema or persisted execution target changed after Agent creation"
            )
        validate_mcp_tool_arguments(tool.input_schema, arguments)
        if cancel_token is not None and cancel_token.is_set():
            raise McpCallCancelled("MCP tool call cancelled before execution")

        dispatched = False

        def _mark_dispatched() -> None:
            nonlocal dispatched
            dispatched = True

        async def _call_connector() -> Any:
            call_method = self.connector.call_tool
            try:
                parameters = inspect.signature(call_method).parameters.values()
                accepts_dispatch_hook = any(
                    parameter.name == "on_dispatch"
                    or parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                accepts_dispatch_hook = False
            if accepts_dispatch_hook:
                return await call_method(
                    installation,
                    tool.raw_name,
                    dict(arguments),
                    on_dispatch=_mark_dispatched,
                )
            # Compatibility for injected legacy connectors. With no hook there
            # is no finer dispatch signal, so mark conservatively before entry;
            # never probe by calling twice because this may be a write.
            _mark_dispatched()
            return await call_method(
                installation,
                tool.raw_name,
                dict(arguments),
            )

        # This deadline is an MCP transport safety boundary, independent from
        # the Agent's optional per-tool timeout.  The connector task includes
        # DNS validation, Streamable HTTP setup/initialize and tools/call.
        call_task = asyncio.create_task(_call_connector())
        deadline_task = asyncio.create_task(
            asyncio.sleep(self._call_timeout_seconds)
        )
        cancel_task = (
            asyncio.create_task(cancel_token.wait())
            if cancel_token is not None
            else None
        )

        def _consume_task_completion(task: asyncio.Task[Any]) -> None:
            try:
                task.exception()
            except BaseException:
                pass

        async def _cancel_and_drain_call() -> None:
            if not call_task.done():
                call_task.cancel()
            done, _ = await asyncio.wait(
                {call_task},
                timeout=_CALL_CANCEL_DRAIN_TIMEOUT_SECONDS,
            )
            if call_task in done:
                _consume_task_completion(call_task)
                return
            # A third-party connector can be cancellation-hostile. Cleanup is
            # still bounded so timeout handling itself cannot hang forever;
            # consume a later exception and never invoke the connector again.
            call_task.add_done_callback(_consume_task_completion)
            logger.warning(
                "MCP connector task did not stop within the bounded cancel drain"
            )

        async def _cancel_and_drain_auxiliary(task: asyncio.Task[Any] | None) -> None:
            if task is None:
                return
            if not task.done():
                task.cancel()
            try:
                await task
            except BaseException:
                pass

        def _cancelled_error(message: str) -> McpRuntimeError:
            if dispatched:
                return McpCallOutcomeUnknown(
                    "MCP 调用可能已执行，但未收到确定结果；请勿自动重试。"
                )
            return McpCallCancelled(message)

        def _pre_dispatch_error(exc: BaseException) -> McpRuntimeError:
            return McpRuntimeError(
                "MCP tool call failed before dispatch: "
                + _sanitize_mcp_exception(installation, exc)
            )

        def _deadline_error() -> McpRuntimeError:
            if dispatched:
                return McpCallOutcomeUnknown(
                    "MCP 调用已越过发送边界，但在强制墙钟期限内未收到确定结果；请勿自动重试。"
                )
            return McpRuntimeError(
                "MCP tool call timed out before dispatch: wall-clock deadline exceeded"
            )

        try:
            wait_tasks = {call_task, deadline_task}
            if cancel_task is not None:
                wait_tasks.add(cancel_task)
            done, _ = await asyncio.wait(
                wait_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if call_task in done:
                try:
                    return await call_task
                except asyncio.CancelledError:
                    raise _cancelled_error(
                        "MCP tool call cancelled before dispatch"
                    )
                except McpCallOutcomeUnknown:
                    raise
                except Exception as exc:
                    if dispatched:
                        raise McpCallOutcomeUnknown(
                            "MCP 调用可能已执行，但传输在结果返回前失败；请勿自动重试。"
                        ) from None
                    raise _pre_dispatch_error(exc) from None
            if deadline_task in done:
                await _cancel_and_drain_call()
                raise _deadline_error()
            await _cancel_and_drain_call()
            raise _cancelled_error("MCP tool call cancelled before dispatch")
        except asyncio.CancelledError:
            await _cancel_and_drain_call()
            raise _cancelled_error("MCP tool call cancelled before dispatch")
        finally:
            await _cancel_and_drain_auxiliary(deadline_task)
            await _cancel_and_drain_auxiliary(cancel_task)

    def clear_cache(self, user_id: str | None = None) -> None:
        if user_id is None:
            self._catalog_cache.clear()
            self._catalog_cache_bytes = 0
            self._resolved_fingerprints.clear()
            for lock_user, lock in list(self._resolve_locks.items()):
                if not lock.locked():
                    self._resolve_locks.pop(lock_user, None)
            return
        self._evict_user_cache(user_id)
        lock = self._resolve_locks.get(user_id)
        if lock is not None and not lock.locked():
            self._resolve_locks.pop(user_id, None)


_GLOBAL_MCP_RUNTIME: McpRuntime | None = None


def get_mcp_runtime() -> McpRuntime:
    global _GLOBAL_MCP_RUNTIME
    if _GLOBAL_MCP_RUNTIME is None:
        _GLOBAL_MCP_RUNTIME = McpRuntime()
    return _GLOBAL_MCP_RUNTIME


async def probe_streamable_http(
    *,
    url: str,
    headers: dict[str, str] | None = None,
    allow_private_network: bool = False,
    allow_insecure_http: bool = False,
    timeout_seconds: float = 15.0,
) -> list[Any]:
    """One-shot initialize + tools/list helper for catalog connection tests."""

    installation = EffectiveMcpInstallation(
        installation_id="probe",
        server_id="probe",
        user_id="probe",
        server_name="probe",
        url=url,
        headers=dict(headers or {}),
        allow_private_network=allow_private_network,
        allow_insecure_http=allow_insecure_http,
    )
    connector = _SdkSessionConnector(
        connect_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
    )
    return await asyncio.wait_for(
        connector.list_tools(installation),
        timeout=timeout_seconds,
    )
