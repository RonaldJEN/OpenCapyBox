import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

import src.agent.tools.mcp_tool as mcp_tool_module
import src.api.services.mcp_runtime as mcp_runtime_module
from src.agent.tools.base import ToolRuntimeContext
from src.agent.tools.mcp_tool import McpRemoteTool
from src.api.services.mcp_runtime import (
    EffectiveMcpInstallation,
    McpCallCancelled,
    McpCallOutcomeUnknown,
    McpInstallationUnavailable,
    McpRequiredServerUnavailable,
    McpRuntime,
    McpRuntimeError,
    McpToolNotPublished,
    McpToolArgumentsInvalid,
    McpToolSnapshot,
    McpToolSnapshotStale,
    _LimitedAsyncTransport,
    _LimitedResponseStream,
    _PinnedNetworkBackend,
    _SdkSessionConnector,
    model_tool_name,
    validate_mcp_tool_arguments,
    validate_mcp_tool_metadata,
)
from src.api.services.mcp_security import ResolvedMcpEndpoint


def _installation(
    *,
    server_id="server-12345678",
    installation_id="install-1",
    required=False,
    enabled_tools=None,
    disabled_tools=frozenset(),
):
    return EffectiveMcpInstallation(
        installation_id=installation_id,
        server_id=server_id,
        user_id="user-1",
        server_name="example",
        url="https://mcp.example.test/mcp",
        required=required,
        enabled_tools=(
            None if enabled_tools is None else frozenset(enabled_tools)
        ),
        disabled_tools=frozenset(disabled_tools),
    )


class FakeRepository:
    def __init__(self, installations):
        self.installations = list(installations)
        self.fingerprint = "fp-1"
        self.cached = {}
        self.saved = {}
        self.live_bindings = {}

    def list_effective_installations(self, user_id):
        return [item for item in self.installations if item.user_id == user_id]

    def get_effective_installation(self, user_id, installation_id):
        return next(
            (
                item for item in self.installations
                if item.user_id == user_id and item.installation_id == installation_id
            ),
            None,
        )

    def catalog_fingerprint(self, user_id):
        return self.fingerprint

    def load_tool_snapshots(self, installation):
        return list(self.cached.get(installation.installation_id, []))

    def get_tool_snapshot_binding(self, installation, raw_name):
        return self.live_bindings.get((installation.installation_id, raw_name))

    def replace_tool_snapshots(self, installation, tools):
        current = self.get_effective_installation(
            installation.user_id,
            installation.installation_id,
        )
        if (
            current is None
            or current.execution_fingerprint != installation.execution_fingerprint
        ):
            return False
        self.saved[installation.installation_id] = list(tools)
        for tool in tools:
            self.live_bindings[(installation.installation_id, tool.raw_name)] = (
                tool.schema_hash,
                tool.connection_fingerprint,
            )
        return True


def _bind_snapshot(repository, snapshot):
    repository.live_bindings[(snapshot.installation_id, snapshot.raw_name)] = (
        snapshot.schema_hash,
        snapshot.connection_fingerprint,
    )


class FakeConnector:
    def __init__(self):
        self.tools = {}
        self.errors = {}
        self.list_calls = []
        self.calls = []
        self.call_started = asyncio.Event()
        self.call_release = asyncio.Event()
        self.call_finished = asyncio.Event()
        self.call_cancelled = asyncio.Event()
        self.dispatch_observed = asyncio.Event()
        self.block_before_dispatch = False
        self.block_calls = False
        self.dispatch_calls = True
        self.call_error = None
        self.result = {"content": [{"type": "text", "text": "ok"}]}

    async def list_tools(self, installation):
        self.list_calls.append(installation.installation_id)
        if installation.installation_id in self.errors:
            raise self.errors[installation.installation_id]
        return list(self.tools.get(installation.installation_id, []))

    async def call_tool(
        self,
        installation,
        raw_name,
        arguments,
        *,
        on_dispatch=None,
    ):
        self.calls.append((installation.installation_id, raw_name, arguments))
        self.call_started.set()
        try:
            if self.block_before_dispatch:
                await self.call_release.wait()
            if self.dispatch_calls and on_dispatch is not None:
                on_dispatch()
                self.dispatch_observed.set()
            if self.call_error is not None:
                raise self.call_error
            if self.block_calls:
                await self.call_release.wait()
            return self.result
        except asyncio.CancelledError:
            self.call_cancelled.set()
            raise
        finally:
            self.call_finished.set()


class _FakeNetworkBackend:
    def __init__(self):
        self.connections = []

    async def connect_tcp(self, host, port, **kwargs):
        self.connections.append((host, port, kwargs))
        return object()

    async def sleep(self, seconds):
        return None


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class _EncodedResponseTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream):
        self.stream = stream

    async def handle_async_request(self, request):
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=self.stream,
        )


class _SequenceResponseTransport(httpx.AsyncBaseTransport):
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.requests = 0

    async def handle_async_request(self, request):
        chunk = self.chunks[self.requests]
        self.requests += 1
        return httpx.Response(200, stream=_ChunkStream([chunk]))


@pytest.mark.parametrize(
    "schema_uri",
    [
        None,
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2019-09/schema",
        "http://json-schema.org/draft-07/schema#",
    ],
)
def test_tool_schema_validation_supports_bounded_standard_drafts(schema_uri):
    schema = {
        "type": "object",
        "properties": {
            # Property names are data, not schema keywords.
            "pattern": {"type": "string"},
            "$id": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    if schema_uri is not None:
        schema["$schema"] = schema_uri

    validate_mcp_tool_metadata(
        raw_name="safe_tool",
        title=None,
        description="safe",
        input_schema=schema,
        annotations={},
    )


@pytest.mark.parametrize(
    ("schema", "error"),
    [
        ({"type": "array"}, "root type must be object"),
        (
            {"$schema": "http://json-schema.org/draft-04/schema#", "type": "object"},
            "unsupported JSON Schema draft",
        ),
        (
            {"type": "object", "properties": {"x": {"$ref": "https://example.test/schema"}}},
            "local JSON Pointer",
        ),
        (
            {"type": "object", "properties": {"x": {"type": "string", "pattern": "(a+)+$"}}},
            "keyword 'pattern' is not allowed",
        ),
        (
            {"type": "object", "required": "x"},
            "not valid JSON Schema",
        ),
    ],
)
def test_tool_schema_validation_rejects_unsafe_or_invalid_schemas(schema, error):
    with pytest.raises(McpRuntimeError, match=error):
        validate_mcp_tool_metadata(
            raw_name="unsafe_tool",
            title=None,
            description="unsafe",
            input_schema=schema,
            annotations={},
        )


def test_mcp_argument_validation_enforces_full_json_schema():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["read", "write"]},
            "count": {"type": "integer", "minimum": 1},
        },
        "required": ["mode", "count"],
        "additionalProperties": False,
    }

    validate_mcp_tool_arguments(schema, {"mode": "read", "count": 1})
    with pytest.raises(McpToolArgumentsInvalid, match="input schema"):
        validate_mcp_tool_arguments(schema, {"mode": "delete", "count": 1})
    with pytest.raises(McpToolArgumentsInvalid, match="input schema"):
        validate_mcp_tool_arguments(schema, {"mode": "read", "count": "1"})
    with pytest.raises(McpToolArgumentsInvalid, match="input schema"):
        validate_mcp_tool_arguments(
            schema,
            {"mode": "read", "count": 1, "unexpected": True},
        )


def test_mcp_argument_validation_rejects_oversized_payload(monkeypatch):
    monkeypatch.setattr(mcp_runtime_module, "_MAX_MCP_ARGUMENT_BYTES", 32)

    with pytest.raises(McpToolArgumentsInvalid, match="byte limit"):
        validate_mcp_tool_arguments(
            {"type": "object", "properties": {"text": {"type": "string"}}},
            {"text": "x" * 64},
        )


@pytest.mark.asyncio
async def test_pinned_network_backend_connects_to_validated_ip_only():
    endpoint = ResolvedMcpEndpoint(
        url="https://mcp.example.test/mcp",
        hostname="mcp.example.test",
        port=443,
        addresses=("93.184.216.34",),
    )
    delegate = _FakeNetworkBackend()
    backend = _PinnedNetworkBackend(endpoint, delegate)

    await backend.connect_tcp("mcp.example.test", 443, timeout=2.0)

    assert len(delegate.connections) == 1
    host, port, kwargs = delegate.connections[0]
    assert (host, port) == ("93.184.216.34", 443)
    assert kwargs["timeout"] == pytest.approx(2.0, abs=0.05)
    assert kwargs["local_address"] is None
    assert kwargs["socket_options"] is None
    with pytest.raises(McpRuntimeError, match="unvalidated"):
        await backend.connect_tcp("redirect.example.test", 443)


@pytest.mark.asyncio
async def test_streamable_http_response_has_a_hard_byte_limit():
    source = _ChunkStream([b"abc", b"def"])
    stream = _LimitedResponseStream(source, limit=5)

    with pytest.raises(McpRuntimeError, match="byte limit"):
        _ = [chunk async for chunk in stream]
    assert source.closed is True


@pytest.mark.asyncio
async def test_compressed_streamable_response_fails_closed_before_decompression():
    source = _ChunkStream([b"compressed"])
    transport = _LimitedAsyncTransport(
        _EncodedResponseTransport(source),
        response_limit=1024,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(McpRuntimeError, match="Compressed"):
            await client.get("https://mcp.example.test/mcp")
    assert source.closed is True


@pytest.mark.asyncio
async def test_streamable_http_byte_budget_is_shared_across_responses():
    raw = _SequenceResponseTransport([b"123", b"456"])
    transport = _LimitedAsyncTransport(
        raw,
        response_limit=5,
        cumulative_limit=5,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        first = await client.get("https://mcp.example.test/page-1")
        assert first.content == b"123"
        with pytest.raises(McpRuntimeError, match="cumulative response byte limit"):
            await client.get("https://mcp.example.test/page-2")
    assert raw.requests == 2


@pytest.mark.asyncio
async def test_sdk_without_secure_client_injection_fails_closed(monkeypatch):
    def insecure_transport(url, headers=None, terminate_on_close=True):
        raise AssertionError("insecure transport must not be called")

    monkeypatch.setattr(
        _SdkSessionConnector,
        "_sdk",
        staticmethod(lambda: (object, insecure_transport)),
    )
    monkeypatch.setattr(
        "src.api.services.mcp_runtime.resolve_mcp_endpoint",
        lambda *args, **kwargs: ResolvedMcpEndpoint(
            url="https://mcp.example.test/mcp",
            hostname="mcp.example.test",
            port=443,
            addresses=("93.184.216.34",),
        ),
    )

    connector = _SdkSessionConnector()
    with pytest.raises(McpRuntimeError, match="secure HTTP client"):
        async with connector._session(_installation()):
            pass


@pytest.mark.asyncio
async def test_sdk_dns_resolution_is_async_and_bounded(monkeypatch):
    def transport_with_factory(url, httpx_client_factory=None):
        raise AssertionError("transport must not be reached after DNS timeout")

    async def slow_to_thread(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(
        _SdkSessionConnector,
        "_sdk",
        staticmethod(lambda: (object, transport_with_factory)),
    )
    monkeypatch.setattr(mcp_runtime_module.asyncio, "to_thread", slow_to_thread)

    connector = _SdkSessionConnector(connect_timeout=0.01)
    with pytest.raises(McpRuntimeError, match="DNS resolution timed out"):
        async with connector._session(_installation()):
            pass


@pytest.mark.asyncio
async def test_sdk_factory_receives_pinned_no_proxy_http_client(monkeypatch):
    captured = {}

    class FakeClientSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

    @asynccontextmanager
    async def injectable_transport(
        url,
        headers=None,
        timeout=None,
        sse_read_timeout=None,
        terminate_on_close=True,
        httpx_client_factory=None,
    ):
        client = httpx_client_factory(
            headers=headers,
            timeout=httpx.Timeout(timeout, read=sse_read_timeout),
        )
        captured["url"] = url
        captured["client"] = client
        async with client:
            yield (object(), object(), lambda: None)

    endpoint = ResolvedMcpEndpoint(
        url="https://mcp.example.test/mcp",
        hostname="mcp.example.test",
        port=443,
        addresses=("93.184.216.34",),
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    monkeypatch.setattr(
        _SdkSessionConnector,
        "_sdk",
        staticmethod(lambda: (FakeClientSession, injectable_transport)),
    )
    monkeypatch.setattr(
        "src.api.services.mcp_runtime.resolve_mcp_endpoint",
        lambda *args, **kwargs: endpoint,
    )

    connector = _SdkSessionConnector()
    async with connector._session(_installation()):
        pass

    client = captured["client"]
    assert captured["url"] == endpoint.url
    assert client.follow_redirects is False
    assert client.headers["Accept-Encoding"] == "identity"
    raw_transport = client._transport._transport
    assert type(raw_transport._pool).__name__ == "AsyncConnectionPool"
    assert isinstance(raw_transport._pool._network_backend, _PinnedNetworkBackend)


@pytest.mark.asyncio
async def test_paginated_tool_discovery_has_a_cumulative_metadata_limit(monkeypatch):
    first_tool = {"name": "first", "inputSchema": {}}
    second_tool = {"name": "second", "inputSchema": {}}
    limit = (
        mcp_runtime_module._json_size_bytes(first_tool)
        + mcp_runtime_module._json_size_bytes(second_tool)
        - 1
    )
    monkeypatch.setattr(mcp_runtime_module, "_MAX_INSTALLATION_CATALOG_BYTES", limit)

    class PagedSession:
        calls = 0

        async def list_tools(self, cursor=None):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(tools=[first_tool], nextCursor="page-2")
            return SimpleNamespace(tools=[second_tool], nextCursor=None)

    session = PagedSession()

    @asynccontextmanager
    async def fake_session(_installation, **_kwargs):
        yield session

    connector = _SdkSessionConnector()
    monkeypatch.setattr(connector, "_session", fake_session)

    with pytest.raises(McpRuntimeError, match="cumulative byte limit"):
        await connector.list_tools(_installation())

    assert session.calls == 2


@pytest.mark.asyncio
async def test_paginated_discovery_shares_transport_budget_across_pages(monkeypatch):
    """Unknown padding on every tools/list page cannot multiply the limit."""

    raw_transport = _SequenceResponseTransport([b"123456", b"abcdef"])
    captured = {}

    def fake_secure_http_client(
        endpoint,
        *,
        headers,
        timeout,
        auth=None,
        cumulative_budget=None,
    ):
        captured["budget"] = cumulative_budget
        return httpx.AsyncClient(
            transport=_LimitedAsyncTransport(
                raw_transport,
                response_limit=8,
                cumulative_budget=cumulative_budget,
            )
        )

    class PagedClientSession:
        def __init__(self, read_stream, *_args, **_kwargs):
            self.client = read_stream
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return None

        async def list_tools(self, cursor=None):
            self.calls += 1
            await self.client.get(
                f"https://mcp.example.test/page-{self.calls}"
            )
            return SimpleNamespace(
                tools=[{"name": f"tool-{self.calls}", "inputSchema": {}}],
                nextCursor="next" if self.calls == 1 else None,
            )

    @asynccontextmanager
    async def injectable_transport(
        url,
        http_client=None,
        terminate_on_close=True,
    ):
        yield (http_client, object(), lambda: None)

    endpoint = ResolvedMcpEndpoint(
        url="https://mcp.example.test/mcp",
        hostname="mcp.example.test",
        port=443,
        addresses=("93.184.216.34",),
    )
    monkeypatch.setattr(
        _SdkSessionConnector,
        "_sdk",
        staticmethod(lambda: (PagedClientSession, injectable_transport)),
    )
    monkeypatch.setattr(
        "src.api.services.mcp_runtime.resolve_mcp_endpoint",
        lambda *args, **kwargs: endpoint,
    )
    monkeypatch.setattr(
        mcp_runtime_module,
        "_secure_http_client",
        fake_secure_http_client,
    )
    monkeypatch.setattr(
        mcp_runtime_module,
        "_MAX_DISCOVERY_SESSION_RESPONSE_BYTES",
        10,
    )

    connector = _SdkSessionConnector()
    with pytest.raises(McpRuntimeError, match="cumulative response byte limit"):
        await connector.list_tools(_installation())

    assert captured["budget"].limit == 10
    assert captured["budget"].consumed == 12
    assert raw_transport.requests == 2


@pytest.mark.asyncio
async def test_resolve_catalog_projects_stable_names_and_persists_snapshot():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.tools[installation.installation_id] = [{
        "name": "read/report.v1",
        "description": "Read a report",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    }]
    runtime = McpRuntime(repository=repository, connector=connector)

    catalog = await runtime.resolve_catalog("user-1")

    assert len(catalog.tools) == 1
    tool = catalog.tools[0]
    assert tool.model_name == "mcp__server12__read_report_v1_666cae62"
    assert tool.raw_name == "read/report.v1"
    assert repository.saved[installation.installation_id][0].schema_hash == tool.schema_hash


@pytest.mark.asyncio
async def test_catalog_cache_evicts_least_recently_used_user_and_fingerprint():
    now = [0.0]
    repository = FakeRepository([])
    runtime = McpRuntime(
        repository=repository,
        connector=FakeConnector(),
        cache_max_users=2,
        cache_max_bytes=1024 * 1024,
        cache_idle_ttl_seconds=60,
        clock=lambda: now[0],
    )

    await runtime.resolve_catalog("user-1")
    now[0] = 1
    await runtime.resolve_catalog("user-2")
    now[0] = 2
    await runtime.resolve_catalog("user-1")  # user-1 is now MRU
    now[0] = 3
    await runtime.resolve_catalog("user-3")

    cached_users = [key[0] for key in runtime._catalog_cache]
    assert cached_users == ["user-1", "user-3"]
    assert runtime.last_resolved_fingerprint("user-1") == repository.fingerprint
    assert runtime.last_resolved_fingerprint("user-2") is None
    assert runtime.last_resolved_fingerprint("user-3") == repository.fingerprint


@pytest.mark.asyncio
async def test_catalog_cache_enforces_total_logical_byte_budget():
    repository = FakeRepository([])
    sizing_runtime = McpRuntime(repository=repository, connector=FakeConnector())
    sample = await sizing_runtime.resolve_catalog("user-1")
    one_entry_bytes = mcp_runtime_module._catalog_snapshot_size_bytes(sample)
    runtime = McpRuntime(
        repository=repository,
        connector=FakeConnector(),
        cache_max_users=10,
        cache_max_bytes=one_entry_bytes * 2 - 1,
        cache_idle_ttl_seconds=60,
    )

    await runtime.resolve_catalog("user-1")
    await runtime.resolve_catalog("user-2")

    assert [key[0] for key in runtime._catalog_cache] == ["user-2"]
    assert runtime._catalog_cache_bytes <= runtime._cache_max_bytes
    assert runtime.last_resolved_fingerprint("user-1") is None


@pytest.mark.asyncio
async def test_catalog_cache_idle_expiry_and_clear_remove_related_state():
    now = [0.0]
    repository = FakeRepository([])
    runtime = McpRuntime(
        repository=repository,
        connector=FakeConnector(),
        cache_max_users=4,
        cache_max_bytes=1024 * 1024,
        cache_idle_ttl_seconds=10,
        clock=lambda: now[0],
    )
    await runtime.resolve_catalog("user-1")
    assert runtime.last_resolved_fingerprint("user-1") == repository.fingerprint

    now[0] = 11
    assert runtime.last_resolved_fingerprint("user-1") is None
    assert not runtime._catalog_cache
    assert runtime._catalog_cache_bytes == 0

    await runtime.resolve_catalog("user-1")
    lock = runtime._resolve_lock("user-1")
    assert "user-1" in runtime._resolve_locks
    runtime.clear_cache("user-1")
    assert runtime.last_resolved_fingerprint("user-1") is None
    assert "user-1" not in runtime._resolve_locks
    assert runtime._catalog_cache_bytes == 0
    assert lock.locked() is False


def test_per_user_resolve_locks_do_not_retain_unbounded_user_keys():
    import gc
    import weakref

    runtime = McpRuntime(repository=FakeRepository([]), connector=FakeConnector())
    lock = runtime._resolve_lock("ephemeral-user")
    lock_ref = weakref.ref(lock)
    assert "ephemeral-user" in runtime._resolve_locks

    del lock
    gc.collect()

    assert lock_ref() is None
    assert "ephemeral-user" not in runtime._resolve_locks


@pytest.mark.asyncio
async def test_catalog_discovery_bounds_concurrent_installations(monkeypatch):
    installations = [
        _installation(
            server_id=f"server-{index:08d}",
            installation_id=f"install-{index}",
        )
        for index in range(6)
    ]
    repository = FakeRepository(installations)

    class TrackingConnector(FakeConnector):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def list_tools(self, installation):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return []
            finally:
                self.active -= 1

    connector = TrackingConnector()
    monkeypatch.setattr(mcp_runtime_module, "_MAX_CONCURRENT_MCP_DISCOVERIES", 2)

    await McpRuntime(repository=repository, connector=connector).resolve_catalog("user-1")

    assert connector.max_active == 2


@pytest.mark.asyncio
async def test_catalog_deterministically_skips_optional_installation_fanout(monkeypatch):
    installations = [
        _installation(
            server_id=f"server-{index:08d}",
            installation_id=f"install-{index}",
        )
        for index in range(3)
    ]
    repository = FakeRepository(installations)
    connector = FakeConnector()
    monkeypatch.setattr(mcp_runtime_module, "_MAX_INSTALLATIONS_PER_CATALOG", 2)

    catalog = await McpRuntime(
        repository=repository,
        connector=connector,
    ).resolve_catalog("user-1")

    assert connector.list_calls == ["install-0", "install-1"]
    assert set(repository.saved) == {"install-0", "install-1"}
    assert len(catalog.errors) == 1
    assert "installation limit" in catalog.errors[0]


@pytest.mark.asyncio
async def test_required_installation_is_prioritized_over_optional_fanout(
    monkeypatch,
):
    installations = [
        _installation(
            required=index == 2,
            server_id=f"server-{index:08d}",
            installation_id=f"install-{index}",
        )
        for index in range(3)
    ]
    repository = FakeRepository(installations)
    connector = FakeConnector()
    monkeypatch.setattr(mcp_runtime_module, "_MAX_INSTALLATIONS_PER_CATALOG", 2)

    catalog = await McpRuntime(
        repository=repository,
        connector=connector,
    ).resolve_catalog("user-1")

    assert connector.list_calls == ["install-2", "install-0"]
    assert len(catalog.errors) == 1
    assert "installation limit" in catalog.errors[0]


@pytest.mark.asyncio
async def test_required_catalog_fails_when_required_installations_exceed_limit(
    monkeypatch,
):
    installations = [
        _installation(
            required=True,
            server_id=f"server-{index:08d}",
            installation_id=f"install-{index}",
        )
        for index in range(3)
    ]
    repository = FakeRepository(installations)
    monkeypatch.setattr(mcp_runtime_module, "_MAX_INSTALLATIONS_PER_CATALOG", 2)

    with pytest.raises(McpRequiredServerUnavailable, match="installation limit"):
        await McpRuntime(
            repository=repository,
            connector=FakeConnector(),
        ).resolve_catalog("user-1")

    assert repository.saved == {}


@pytest.mark.asyncio
async def test_catalog_discovery_has_a_global_cross_user_concurrency_limit(monkeypatch):
    user_one = [
        _installation(installation_id=f"u1-{index}", server_id=f"u1-server-{index}")
        for index in range(3)
    ]
    user_two = [
        replace(
            _installation(
                installation_id=f"u2-{index}",
                server_id=f"u2-server-{index}",
            ),
            user_id="user-2",
        )
        for index in range(3)
    ]
    repository = FakeRepository([*user_one, *user_two])

    class TrackingConnector(FakeConnector):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def list_tools(self, installation):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return []
            finally:
                self.active -= 1

    connector = TrackingConnector()
    monkeypatch.setattr(mcp_runtime_module, "_MAX_CONCURRENT_MCP_DISCOVERIES", 4)
    monkeypatch.setattr(mcp_runtime_module, "_MAX_GLOBAL_CONCURRENT_MCP_DISCOVERIES", 2)

    runtime = McpRuntime(repository=repository, connector=connector)
    await asyncio.gather(
        runtime.resolve_catalog("user-1"),
        runtime.resolve_catalog("user-2"),
    )

    assert connector.max_active == 2


@pytest.mark.asyncio
async def test_catalog_discovery_has_a_total_wall_clock_timeout(monkeypatch):
    installation = _installation()
    repository = FakeRepository([installation])

    class SlowConnector(FakeConnector):
        async def list_tools(self, installation):
            await asyncio.sleep(1)
            return []

    monkeypatch.setattr(mcp_runtime_module, "_DISCOVERY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(mcp_runtime_module, "_CATALOG_BUILD_TIMEOUT_SECONDS", 1.0)

    catalog = await McpRuntime(
        repository=repository,
        connector=SlowConnector(),
    ).resolve_catalog("user-1")

    assert catalog.tools == ()
    assert "wall-clock timeout" in catalog.errors[0]


@pytest.mark.asyncio
async def test_optional_catalog_build_timeout_is_isolated(monkeypatch):
    installation = _installation()
    repository = FakeRepository([installation])

    class SlowConnector(FakeConnector):
        async def list_tools(self, installation):
            await asyncio.sleep(1)
            return []

    monkeypatch.setattr(mcp_runtime_module, "_DISCOVERY_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(mcp_runtime_module, "_CATALOG_BUILD_TIMEOUT_SECONDS", 0.01)

    catalog = await McpRuntime(
        repository=repository,
        connector=SlowConnector(),
    ).resolve_catalog("user-1")

    assert catalog.tools == ()
    assert len(catalog.errors) == 1
    assert "catalog build" in catalog.errors[0]
    assert "wall-clock timeout" in catalog.errors[0]


@pytest.mark.asyncio
async def test_optional_timeout_cannot_discard_healthy_required_catalog(monkeypatch):
    required = _installation(
        required=True,
        server_id="required-server",
        installation_id="required-install",
    )
    optional = _installation(
        server_id="optional-server",
        installation_id="optional-install",
    )
    repository = FakeRepository([optional, required])

    class RequiredFirstConnector(FakeConnector):
        async def list_tools(self, installation):
            self.list_calls.append(installation.installation_id)
            if installation.required:
                return [
                    {"name": "required_tool", "inputSchema": {"type": "object"}}
                ]
            await asyncio.sleep(1)
            return [
                {"name": "optional_tool", "inputSchema": {"type": "object"}}
            ]

    monkeypatch.setattr(mcp_runtime_module, "_DISCOVERY_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(mcp_runtime_module, "_CATALOG_BUILD_TIMEOUT_SECONDS", 0.02)

    catalog = await McpRuntime(
        repository=repository,
        connector=RequiredFirstConnector(),
    ).resolve_catalog("user-1")

    assert [tool.raw_name for tool in catalog.tools] == ["required_tool"]
    assert len(catalog.errors) == 1
    assert "catalog build" in catalog.errors[0]


@pytest.mark.asyncio
async def test_required_catalog_timeout_remains_fatal(monkeypatch):
    required = _installation(required=True)
    repository = FakeRepository([required])

    class SlowConnector(FakeConnector):
        async def list_tools(self, installation):
            await asyncio.sleep(1)
            return []

    monkeypatch.setattr(mcp_runtime_module, "_DISCOVERY_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(mcp_runtime_module, "_CATALOG_BUILD_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(McpRequiredServerUnavailable, match="catalog build"):
        await McpRuntime(
            repository=repository,
            connector=SlowConnector(),
        ).resolve_catalog("user-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["tools", "bytes"])
async def test_optional_budget_overflow_does_not_poison_required_catalog(
    monkeypatch,
    budget,
):
    required = _installation(
        required=True,
        server_id="required-server",
        installation_id="required-install",
    )
    optional = _installation(
        server_id="optional-server",
        installation_id="optional-install",
    )
    repository = FakeRepository([optional, required])
    connector = FakeConnector()
    connector.tools[required.installation_id] = [
        {"name": "required_tool", "inputSchema": {"type": "object"}}
    ]
    connector.tools[optional.installation_id] = [
        {"name": "optional_tool", "inputSchema": {"type": "object"}}
    ]
    if budget == "tools":
        monkeypatch.setattr(mcp_runtime_module, "_MAX_TOOLS_PER_USER_CATALOG", 1)
    else:
        monkeypatch.setattr(mcp_runtime_module, "_MAX_USER_CATALOG_BYTES", 1)
        monkeypatch.setattr(
            mcp_runtime_module,
            "_tool_snapshot_size_bytes",
            lambda _tool: 1,
        )

    catalog = await McpRuntime(
        repository=repository,
        connector=connector,
    ).resolve_catalog("user-1")

    assert [tool.raw_name for tool in catalog.tools] == ["required_tool"]
    assert len(catalog.errors) == 1
    assert "limit" in catalog.errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["tools", "bytes"])
async def test_required_catalog_fails_when_its_own_budget_is_exceeded(
    monkeypatch,
    budget,
):
    required = _installation(required=True)
    repository = FakeRepository([required])
    connector = FakeConnector()
    connector.tools[required.installation_id] = [
        {"name": "required_one", "inputSchema": {"type": "object"}},
        {"name": "required_two", "inputSchema": {"type": "object"}},
    ]
    if budget == "tools":
        monkeypatch.setattr(mcp_runtime_module, "_MAX_TOOLS_PER_USER_CATALOG", 1)
    else:
        monkeypatch.setattr(mcp_runtime_module, "_MAX_USER_CATALOG_BYTES", 1)
        monkeypatch.setattr(
            mcp_runtime_module,
            "_tool_snapshot_size_bytes",
            lambda _tool: 1,
        )

    with pytest.raises(McpRequiredServerUnavailable, match="required MCP catalog"):
        await McpRuntime(
            repository=repository,
            connector=connector,
        ).resolve_catalog("user-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_name", "remote_tool"),
    [
        (
            "_MAX_TOOL_DESCRIPTION_BYTES",
            {"name": "tool", "description": "oversized", "inputSchema": {}},
        ),
        (
            "_MAX_TOOL_SCHEMA_BYTES",
            {"name": "tool", "inputSchema": {"value": "oversized"}},
        ),
        (
            "_MAX_TOOL_ANNOTATIONS_BYTES",
            {
                "name": "tool",
                "inputSchema": {"type": "object"},
                "annotations": {"value": "oversized"},
            },
        ),
    ],
)
async def test_discovery_rejects_oversized_tool_metadata(
    monkeypatch,
    limit_name,
    remote_tool,
):
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.tools[installation.installation_id] = [remote_tool]
    runtime = McpRuntime(repository=repository, connector=connector)
    monkeypatch.setattr(mcp_runtime_module, limit_name, 4)

    with pytest.raises(McpRuntimeError, match="byte limit"):
        await runtime._discover_installation(installation)

    assert repository.saved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remote_tool",
    [
        {"name": "x" * 256, "inputSchema": {}},
        {"name": "tool", "title": "x" * 256, "inputSchema": {}},
    ],
)
async def test_discovery_rejects_tool_metadata_that_exceeds_db_columns(remote_tool):
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.tools[installation.installation_id] = [remote_tool]
    runtime = McpRuntime(repository=repository, connector=connector)

    with pytest.raises(McpRuntimeError, match="255-character"):
        await runtime._discover_installation(installation)

    assert repository.saved == {}


@pytest.mark.asyncio
async def test_optional_discovery_failure_does_not_expose_cached_snapshot():
    installation = _installation()
    repository = FakeRepository([installation])
    cached = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name=installation.server_name,
        source="official",
        raw_name="cached_read",
        model_name=model_tool_name(installation.server_id, "cached_read"),
        description="cached",
        input_schema={"type": "object"},
        connection_fingerprint=installation.execution_fingerprint,
        stale=True,
    )
    repository.cached[installation.installation_id] = [cached]
    connector = FakeConnector()
    connector.errors[installation.installation_id] = ConnectionError("offline")

    catalog = await McpRuntime(repository=repository, connector=connector).resolve_catalog("user-1")

    assert catalog.tools == ()
    assert "offline" in catalog.errors[0]
    assert "using cached tool snapshot" not in catalog.errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_discovery_errors_redact_credentials_and_endpoint(required):
    secret = "catalog-super-secret-token"
    endpoint = "https://mcp.example.test/private?credential=visible"
    installation = replace(
        _installation(required=required),
        url=endpoint,
        headers={"Authorization": f"Bearer {secret}"},
        credential_fingerprint="credential-fingerprint",
    )
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.errors[installation.installation_id] = RuntimeError(
        f"initialize rejected Authorization: Bearer {secret} at {endpoint}\nnext-line"
    )
    runtime = McpRuntime(repository=repository, connector=connector)

    if required:
        with pytest.raises(McpRequiredServerUnavailable) as caught:
            await runtime.resolve_catalog("user-1")
        error_text = str(caught.value)
    else:
        catalog = await runtime.resolve_catalog("user-1")
        error_text = catalog.errors[0]

    assert secret not in error_text
    assert endpoint not in error_text
    assert "next-line" in error_text
    assert "\n" not in error_text
    assert "REDACTED" in error_text


@pytest.mark.asyncio
async def test_partial_catalog_is_cached_until_next_refresh_bucket():
    healthy = _installation(
        installation_id="install-healthy",
        server_id="server-healthy",
    )
    offline = _installation(
        installation_id="install-offline",
        server_id="server-offline",
    )

    class BucketRepository(FakeRepository):
        def __init__(self, installations):
            super().__init__(installations)
            self.now = 0.0

        def catalog_fingerprint(self, user_id):
            return f"fp-{int(self.now // 300)}"

    repository = BucketRepository([healthy, offline])
    connector = FakeConnector()
    connector.tools[healthy.installation_id] = [{
        "name": "healthy_tool",
        "inputSchema": {"type": "object"},
    }]
    connector.errors[offline.installation_id] = ConnectionError("offline")
    runtime = McpRuntime(repository=repository, connector=connector)

    partial = await runtime.resolve_catalog("user-1")
    assert [tool.raw_name for tool in partial.tools] == ["healthy_tool"]
    assert partial.errors and "offline" in partial.errors[0]
    assert len(connector.list_calls) == 2

    del connector.errors[offline.installation_id]
    connector.tools[offline.installation_id] = [{
        "name": "recovered",
        "inputSchema": {"type": "object"},
    }]
    same_bucket = await runtime.resolve_catalog("user-1")

    assert same_bucket is partial
    assert [tool.raw_name for tool in same_bucket.tools] == ["healthy_tool"]
    assert len(connector.list_calls) == 2

    repository.now = 300.0
    recovered = await runtime.resolve_catalog("user-1")

    assert {tool.raw_name for tool in recovered.tools} == {
        "healthy_tool",
        "recovered",
    }
    assert recovered.errors == ()
    assert len(connector.list_calls) == 4


@pytest.mark.asyncio
async def test_discovery_failure_does_not_rebind_cached_schema_to_changed_target():
    original = _installation()
    changed = replace(original, url="https://new-target.example.test/mcp")
    repository = FakeRepository([changed])
    repository.cached[changed.installation_id] = [McpToolSnapshot(
        installation_id=original.installation_id,
        server_id=original.server_id,
        server_name=original.server_name,
        source="official",
        raw_name="cached_read",
        model_name=model_tool_name(original.server_id, "cached_read"),
        description="cached from old endpoint",
        input_schema={"type": "object"},
        schema_hash="schema-old",
        connection_fingerprint=original.execution_fingerprint,
        stale=True,
    )]
    connector = FakeConnector()
    connector.errors[changed.installation_id] = ConnectionError("offline")

    catalog = await McpRuntime(
        repository=repository,
        connector=connector,
    ).resolve_catalog("user-1")

    assert catalog.tools == ()
    assert "using cached tool snapshot" not in catalog.errors[0]


@pytest.mark.asyncio
async def test_discovery_result_is_rejected_when_execution_target_changes_midflight():
    original = _installation()
    changed = replace(original, url="https://new-target.example.test/mcp")
    repository = FakeRepository([original])

    class ReconfiguringConnector(FakeConnector):
        async def list_tools(self, installation):
            self.list_calls.append(installation.installation_id)
            repository.installations = [changed]
            repository.fingerprint = "fp-2"
            return [{"name": "stale_tool", "inputSchema": {"type": "object"}}]

    runtime = McpRuntime(
        repository=repository,
        connector=ReconfiguringConnector(),
    )

    with pytest.raises(McpToolSnapshotStale, match="configuration changed"):
        await runtime.resolve_catalog("user-1")

    assert repository.saved == {}
    assert runtime._catalog_cache == {}
    assert runtime.last_resolved_fingerprint("user-1") is None


def test_execution_fingerprint_binds_target_not_tool_publication():
    installation = _installation(enabled_tools={"read"})

    assert replace(
        installation,
        enabled_tools=frozenset({"write"}),
        disabled_tools=frozenset({"read"}),
    ).execution_fingerprint == installation.execution_fingerprint
    assert replace(
        installation,
        credential_fingerprint="new-credential",
    ).execution_fingerprint != installation.execution_fingerprint
    assert replace(
        installation,
        allow_private_network=True,
    ).execution_fingerprint != installation.execution_fingerprint


@pytest.mark.asyncio
async def test_required_discovery_failure_blocks_agent_even_with_cache():
    installation = _installation(required=True)
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.errors[installation.installation_id] = ConnectionError("offline")

    with pytest.raises(McpRequiredServerUnavailable, match="offline"):
        await McpRuntime(repository=repository, connector=connector).resolve_catalog("user-1")


@pytest.mark.asyncio
async def test_sanitized_tool_names_receive_hashes_instead_of_colliding():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.tools[installation.installation_id] = [
        {"name": "read/a", "inputSchema": {"type": "object"}},
        {"name": "read.a", "inputSchema": {"type": "object"}},
    ]

    catalog = await McpRuntime(
        repository=repository,
        connector=connector,
    ).resolve_catalog("user-1")

    assert {tool.raw_name for tool in catalog.tools} == {"read/a", "read.a"}
    assert len({tool.model_name for tool in catalog.tools}) == 2


@pytest.mark.parametrize(
    ("raw_name", "error"),
    [
        (" delete ", "leading or trailing whitespace"),
        ("*", "reserved for permission wildcards"),
        ("delete\x00all", "ASCII control character"),
    ],
)
@pytest.mark.asyncio
async def test_ambiguous_or_reserved_remote_tool_name_is_rejected(raw_name, error):
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.tools[installation.installation_id] = [
        {"name": raw_name, "inputSchema": {"type": "object"}},
    ]

    catalog = await McpRuntime(repository=repository, connector=connector).resolve_catalog("user-1")

    assert catalog.tools == ()
    assert error in catalog.errors[0]
    assert repository.saved == {}


@pytest.mark.asyncio
async def test_call_rechecks_installation_and_routes_with_raw_name_once():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="actual/remote.name",
        model_name=model_tool_name(installation.server_id, "actual/remote.name"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)

    await runtime.call_tool(user_id="user-1", tool=snapshot, arguments={"x": 1})
    assert connector.calls == [(installation.installation_id, "actual/remote.name", {"x": 1})]

    repository.installations.clear()
    with pytest.raises(McpInstallationUnavailable):
        await runtime.call_tool(user_id="user-1", tool=snapshot, arguments={})
    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_call_validates_arguments_before_remote_dispatch():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="strict_write",
        model_name=model_tool_name(installation.server_id, "strict_write"),
        description="test",
        input_schema={
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["safe"]}},
            "required": ["mode"],
            "additionalProperties": False,
        },
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)

    with pytest.raises(McpToolArgumentsInvalid, match="input schema"):
        await runtime.call_tool(
            user_id="user-1",
            tool=snapshot,
            arguments={"mode": "unsafe", "extra": True},
        )

    assert connector.calls == []


@pytest.mark.asyncio
async def test_call_rejects_legacy_snapshot_with_reserved_remote_identity():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="official",
        raw_name="*",
        model_name="mcp__server12__tool",
        description="legacy",
        input_schema={"type": "object"},
        connection_fingerprint=installation.execution_fingerprint,
    )

    with pytest.raises(McpInstallationUnavailable, match="invalid identity"):
        await runtime.call_tool(user_id="user-1", tool=snapshot, arguments={})

    assert connector.calls == []


@pytest.mark.asyncio
async def test_call_blocks_when_endpoint_or_credential_changed_after_snapshot():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    repository.installations[0] = replace(
        installation,
        credential_fingerprint="rotated",
    )

    with pytest.raises(McpInstallationUnavailable, match="endpoint or credential changed"):
        await runtime.call_tool(user_id="user-1", tool=snapshot, arguments={})

    assert connector.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "live_binding",
    [
        ("schema-v2", None),
        ("schema-v1", "different-target"),
        None,
    ],
)
async def test_call_blocks_when_durable_tool_snapshot_binding_changed(live_binding):
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    if live_binding is not None:
        schema_hash, connection_fingerprint = live_binding
        repository.live_bindings[(installation.installation_id, snapshot.raw_name)] = (
            schema_hash,
            connection_fingerprint or installation.execution_fingerprint,
        )

    with pytest.raises(McpToolSnapshotStale, match="schema or persisted"):
        await runtime.call_tool(user_id="user-1", tool=snapshot, arguments={})

    assert connector.calls == []


@pytest.mark.asyncio
async def test_hidden_tools_are_not_published_and_are_rechecked_before_execution():
    installation = _installation(
        enabled_tools={"readReport", "deleteReport"},
        disabled_tools={"deleteReport"},
    )
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.tools[installation.installation_id] = [
        {"name": "readReport", "inputSchema": {"type": "object"}},
        {"name": "deleteReport", "inputSchema": {"type": "object"}},
    ]
    runtime = McpRuntime(repository=repository, connector=connector)

    catalog = await runtime.resolve_catalog("user-1")

    assert [tool.raw_name for tool in catalog.tools] == ["readReport"]
    assert {tool.raw_name for tool in repository.saved[installation.installation_id]} == {
        "readReport",
        "deleteReport",
    }

    hidden_snapshot = next(
        tool
        for tool in repository.saved[installation.installation_id]
        if tool.raw_name == "deleteReport"
    )
    with pytest.raises(McpToolNotPublished):
        await runtime.call_tool(user_id="user-1", tool=hidden_snapshot, arguments={})
    assert connector.calls == []

    # A tool disabled after an Agent was built must also fail the fresh
    # execution-time repository check.
    visible_snapshot = catalog.tools[0]
    repository.installations[0] = replace(
        installation,
        disabled_tools=frozenset({"readReport"}),
    )
    with pytest.raises(McpToolNotPublished):
        await runtime.call_tool(user_id="user-1", tool=visible_snapshot, arguments={})
    assert connector.calls == []


@pytest.mark.asyncio
async def test_call_races_cancel_token_and_cancels_transport_task():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.block_calls = True
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)
    cancel = asyncio.Event()
    task = asyncio.create_task(runtime.call_tool(
        user_id="user-1",
        tool=snapshot,
        arguments={},
        cancel_token=cancel,
    ))
    await connector.call_started.wait()
    cancel.set()

    with pytest.raises(McpCallOutcomeUnknown, match="请勿自动重试"):
        await task
    assert connector.call_finished.is_set()
    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_call_cancelled_before_dispatch_is_definitely_cancelled():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)
    cancel = asyncio.Event()
    cancel.set()

    with pytest.raises(McpCallCancelled, match="before execution"):
        await runtime.call_tool(
            user_id="user-1",
            tool=snapshot,
            arguments={},
            cancel_token=cancel,
        )
    assert connector.calls == []


@pytest.mark.asyncio
async def test_mcp_call_deadline_cancels_and_drains_before_dispatch():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.block_before_dispatch = True
    runtime = McpRuntime(
        repository=repository,
        connector=connector,
        call_timeout_seconds=0.02,
    )
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)
    started_at = asyncio.get_running_loop().time()

    with pytest.raises(McpRuntimeError, match="timed out before dispatch") as exc_info:
        await asyncio.wait_for(
            runtime.call_tool(user_id="user-1", tool=snapshot, arguments={}),
            timeout=0.5,
        )

    assert not isinstance(exc_info.value, (McpCallCancelled, McpCallOutcomeUnknown))
    assert asyncio.get_running_loop().time() - started_at < 0.25
    assert connector.dispatch_observed.is_set() is False
    assert connector.call_cancelled.is_set()
    assert connector.call_finished.is_set()
    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_mcp_call_deadline_after_dispatch_is_unknown_and_drained():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.block_calls = True
    runtime = McpRuntime(
        repository=repository,
        connector=connector,
        call_timeout_seconds=0.02,
    )
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)
    started_at = asyncio.get_running_loop().time()

    with pytest.raises(McpCallOutcomeUnknown, match="请勿自动重试"):
        await asyncio.wait_for(
            runtime.call_tool(user_id="user-1", tool=snapshot, arguments={}),
            timeout=0.5,
        )

    assert asyncio.get_running_loop().time() - started_at < 0.25
    assert connector.dispatch_observed.is_set()
    assert connector.call_cancelled.is_set()
    assert connector.call_finished.is_set()
    assert len(connector.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", [ConnectionError("reset"), httpx.ReadTimeout("read")])
async def test_transport_error_after_dispatch_has_unknown_outcome(transport_error):
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.call_error = transport_error
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)

    with pytest.raises(McpCallOutcomeUnknown, match="请勿自动重试"):
        await runtime.call_tool(user_id="user-1", tool=snapshot, arguments={})
    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_transport_error_before_dispatch_remains_safe_failure():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.dispatch_calls = False
    connector.call_error = ConnectionError("handshake failed")
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)

    with pytest.raises(McpRuntimeError, match="failed before dispatch.*handshake"):
        await runtime.call_tool(user_id="user-1", tool=snapshot, arguments={})
    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_pre_dispatch_remote_error_never_leaks_token_to_tool_result():
    secret = "call-super-secret-token"
    endpoint = "https://mcp.example.test/private?credential=visible"
    installation = replace(
        _installation(),
        url=endpoint,
        headers={"Authorization": f"Bearer {secret}"},
        credential_fingerprint="credential-fingerprint",
    )
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.dispatch_calls = False
    connector.call_error = RuntimeError(
        f"initialize echoed Bearer {secret} for {endpoint} " + "x" * 1000
    )
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)
    tool = McpRemoteTool(user_id="user-1", snapshot=snapshot, runtime=runtime)

    result = await tool.execute()

    rendered = f"{result.error}\n{result.content}"
    assert result.success is False
    assert result.outcome_uncertain is False
    assert secret not in rendered
    assert endpoint not in rendered
    assert "REDACTED" in rendered
    assert len(result.error or "") < 700


@pytest.mark.asyncio
async def test_outer_timeout_drains_dispatched_transport_as_unknown():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.block_calls = True
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="write",
        model_name=model_tool_name(installation.server_id, "write"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)

    with pytest.raises(McpCallOutcomeUnknown, match="请勿自动重试"):
        await asyncio.wait_for(
            runtime.call_tool(user_id="user-1", tool=snapshot, arguments={}),
            timeout=0.01,
        )
    assert connector.call_finished.is_set()
    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_remote_tool_converts_result_and_supplies_turn_cancel_context():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="official",
        raw_name="echo",
        model_name=model_tool_name(installation.server_id, "echo"),
        description="echo",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)
    tool = McpRemoteTool(user_id="user-1", snapshot=snapshot, runtime=runtime)
    tool.set_runtime_context(ToolRuntimeContext(
        thread_id="thread",
        run_id="run",
        tool_call_id="call",
        tool_name=tool.name,
        cancel_token=asyncio.Event(),
    ))

    result = await tool.execute(value="hello")

    assert result.success is True
    assert result.content == "ok"
    assert tool.tool_ref.name == "echo"


@pytest.mark.asyncio
async def test_remote_tool_recursively_strips_hidden_meta_from_json_fallbacks():
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.result = {
        "content": [
            {"type": "text", "text": "literal _meta in text remains visible"},
            {
                "type": "audio",
                "mimeType": "audio/wav",
                "data": "YXVkaW8=",
                "_meta": {"secret": "outer-secret"},
                "nested": {"keep": "audio-visible", "_meta": "nested-secret"},
            },
            {
                "type": "resource",
                "resource": {
                    "uri": "https://example.test/resource",
                    "text": "resource-visible",
                    "_meta": {"secret": "resource-secret"},
                    "items": [{"keep": "list-visible", "_meta": "list-secret"}],
                },
            },
        ],
        "structuredContent": {
            "answer": {"value": 42, "_meta": {"secret": "answer-secret"}},
            "items": [{"keep": "structured-visible", "_meta": "item-secret"}],
            "_meta": {"secret": "root-secret"},
        },
    }
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="metadata",
        model_name=model_tool_name(installation.server_id, "metadata"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)
    tool = McpRemoteTool(user_id="user-1", snapshot=snapshot, runtime=runtime)

    result = await tool.execute()

    assert result.success is True
    assert "literal _meta in text remains visible" in result.content
    assert '"_meta"' not in result.content
    for secret in (
        "outer-secret",
        "nested-secret",
        "resource-secret",
        "list-secret",
        "answer-secret",
        "item-secret",
        "root-secret",
    ):
        assert secret not in result.content
    for visible in (
        "audio-visible",
        "resource-visible",
        "list-visible",
        "structured-visible",
    ):
        assert visible in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_name", "payload"),
    [
        (
            "_MAX_RESULT_TEXT_BYTES",
            {"content": [{"type": "text", "text": "oversized text"}]},
        ),
        (
            "_MAX_STRUCTURED_CONTENT_BYTES",
            {"structuredContent": {"value": "oversized structured content"}},
        ),
        (
            "_MAX_MEDIA_BASE64_BYTES",
            {
                "content": [{
                    "type": "image",
                    "mimeType": "image/png",
                    "data": "aGVsbG8=",
                }],
            },
        ),
    ],
)
async def test_remote_tool_rejects_oversized_untrusted_results(
    monkeypatch,
    limit_name,
    payload,
):
    installation = _installation()
    repository = FakeRepository([installation])
    connector = FakeConnector()
    connector.result = payload
    runtime = McpRuntime(repository=repository, connector=connector)
    snapshot = McpToolSnapshot(
        installation_id=installation.installation_id,
        server_id=installation.server_id,
        server_name="example",
        source="personal",
        raw_name="oversized",
        model_name=model_tool_name(installation.server_id, "oversized"),
        description="test",
        input_schema={"type": "object"},
        schema_hash="schema-v1",
        connection_fingerprint=installation.execution_fingerprint,
    )
    _bind_snapshot(repository, snapshot)
    tool = McpRemoteTool(user_id="user-1", snapshot=snapshot, runtime=runtime)
    monkeypatch.setattr(mcp_tool_module, limit_name, 4)

    result = await tool.execute()

    assert result.success is False
    assert "byte limit" in (result.error or "")
    assert result.content_blocks is None


def test_model_tool_name_is_bounded_and_deterministic():
    raw = "x" * 500
    first = model_tool_name("server-12345678", raw)
    assert first == model_tool_name("server-12345678", raw)
    assert len(first.encode("utf-8")) <= 64


def test_model_tool_name_hashes_sanitized_names_to_prevent_collisions():
    slash = model_tool_name("server-12345678", "a/b")
    dot = model_tool_name("server-12345678", "a.b")

    assert slash == "mcp__server12__a_b_c14cddc0"
    assert dot == "mcp__server12__a_b_2e7336dc"
    assert slash != dot
    assert model_tool_name("server-12345678", "safe_name-1") == (
        "mcp__server12__safe_name-1"
    )
    assert len(slash.encode("utf-8")) <= 64
    assert len(dot.encode("utf-8")) <= 64


def test_model_tool_name_does_not_silently_normalize_remote_identity():
    with pytest.raises(ValueError, match="surrounding whitespace"):
        model_tool_name("server-12345678", " delete ")

    with pytest.raises(ValueError, match="permission wildcards"):
        model_tool_name("server-12345678", "*")

    with pytest.raises(ValueError, match="control characters"):
        model_tool_name("server-12345678", "delete\x00all")
