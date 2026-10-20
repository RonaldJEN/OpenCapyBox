"""MCP 连接能力摘要：输入边界、输出规整、按快照派生、内容指纹守卫与后台调度。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import sessionmaker

from src.agent.schema import LLMResponse, TokenUsage
from src.api.model_registry import ModelConfig
from src.api.models.auth_user import AuthUser
from src.api.models.database import Base
from src.api.models.mcp import (
    McpConfigVersion,
    McpInstallation,
    McpServer,
    McpToolSnapshot,
    McpToolVisibility,
)
from src.api.utils.timezone import now_naive
from src.api.services.mcp_summary_service import (
    MAX_SUMMARY_CHARS,
    SUMMARY_SYSTEM_PROMPT,
    McpSummaryService,
    McpToolDigest,
    build_capability_summary_prompt,
    capability_source_hash,
    ensure_capability_summary,
    normalize_summary,
    schedule_capability_summary,
    summary_model_config,
    tool_digests,
)
from tests.db_safety import (
    build_pytest_pg_engine,
    create_all_for_test_engine,
    reset_all_tables,
)

_PROJECT_ROOT = Path(__file__).parent.parent

TOOLS = [
    {"name": "query_records", "title": "查询记录", "description": "按条件查询操作记录"},
    {"name": "create_record", "description": "新增一条操作记录"},
]


def _service(generate: AsyncMock) -> McpSummaryService:
    service = McpSummaryService(ModelConfig(
        id="default-model",
        display_name="默认模型",
        provider="openai",
        api_base="https://llm.example.com/v1",
        api_key="test-key",
        model_name="m1",
        thinking_mode="enabled",
        thinking_wire_format="enable_thinking",
        enable_thinking=True,
    ))
    service._client = type("Client", (), {"generate": generate})()
    return service


def _model(**overrides) -> ModelConfig:
    values = dict(
        id="default-model",
        display_name="默认模型",
        provider="openai",
        api_base="https://llm.example.com/v1",
        api_key="test-key",
        model_name="m1",
    )
    values.update(overrides)
    return ModelConfig(**values)


@pytest.mark.parametrize(
    ("config", "thinking_off"),
    [
        pytest.param(
            _model(thinking_mode="enabled", thinking_wire_format="enable_thinking", enable_thinking=True),
            True,
            id="openai-enable_thinking",
        ),
        pytest.param(
            _model(
                thinking_wire_format="enable_thinking",
                reasoning_effort="max",
                supported_reasoning_efforts=["low", "max", "off"],
            ),
            True,
            id="openai-effort-with-off",
        ),
        pytest.param(_model(provider="anthropic", api_base="https://a.example.com"), False, id="anthropic"),
        pytest.param(
            _model(thinking_wire_format="none", reasoning_effort="max", supported_reasoning_efforts=["low", "max"]),
            False,
            id="openai-wire-none",
        ),
        pytest.param(
            _model(
                thinking_wire_format="enable_thinking",
                reasoning_effort="high",
                supported_reasoning_efforts=["low", "high"],
            ),
            False,
            id="openai-cannot-switch-off",
        ),
    ],
)
def test_summary_model_config_switches_thinking_off_only_where_encodable(config, thinking_off):
    resolved = summary_model_config(config)

    if thinking_off:
        assert resolved.thinking_mode == "disabled"
        assert resolved.enable_thinking is False
        assert resolved.reasoning_effort is None
    else:
        assert resolved is config
    # Whatever the outcome, the service must be constructible from it.
    assert McpSummaryService(config).config == resolved


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        content=text,
        finish_reason="stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def test_source_hash_ignores_tool_order_and_tracks_content():
    forward = capability_source_hash("记录系统", tool_digests(TOOLS))
    reversed_hash = capability_source_hash("记录系统", tool_digests(list(reversed(TOOLS))))
    changed = capability_source_hash("记录系统", tool_digests([
        {**TOOLS[0], "description": "改了说明"},
        TOOLS[1],
    ]))
    renamed = capability_source_hash("审计系统", tool_digests(TOOLS))

    assert forward == reversed_hash
    assert len(forward) == 64
    assert changed != forward
    # The connection name is part of the prompt, so a rename regenerates too.
    assert renamed != forward


def test_prompt_bounds_tool_count_and_description_length():
    digests = [
        McpToolDigest(name=f"tool_{index:03d}", title="", description="x" * 1000)
        for index in range(81)
    ]

    prompt = build_capability_summary_prompt("统一操作记录管理系统", digests)

    assert prompt.startswith("连接名：统一操作记录管理系统\n工具数：81\n")
    assert "tool_079" in prompt
    assert "tool_080" not in prompt
    assert "（其余 1 个工具省略）" in prompt
    assert "x" * 301 not in prompt


def test_normalize_summary_collapses_lines_quotes_and_length():
    assert normalize_summary('  "对接操作记录系统。\n能做：查询、新增。"  ') == "对接操作记录系统。 能做：查询、新增。"
    assert len(normalize_summary("摘" * (MAX_SUMMARY_CHARS + 50))) == MAX_SUMMARY_CHARS


async def test_generate_sends_fixed_system_prompt_and_tool_list():
    generate = AsyncMock(return_value=_response(" 对接统一操作记录系统。能做：查询记录、新增记录。适合：操作记录、审计。 "))

    summary = await _service(generate).generate("统一操作记录管理系统", tool_digests(TOOLS))

    assert summary == "对接统一操作记录系统。能做：查询记录、新增记录。适合：操作记录、审计。"
    messages = generate.await_args.kwargs["messages"]
    assert messages[0].role == "system"
    assert messages[0].content == SUMMARY_SYSTEM_PROMPT
    assert "- create_record |  | 新增一条操作记录" in messages[1].content
    assert "- query_records | 查询记录 | 按条件查询操作记录" in messages[1].content


async def test_generate_rejects_empty_reply():
    with pytest.raises(ValueError, match="能力摘要为空"):
        await _service(AsyncMock(return_value=_response("  \n"))).generate("x", tool_digests(TOOLS))


# ---------------------------------------------------------------------------
# Derivation from the committed snapshot
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory():
    engine = build_pytest_pg_engine(_PROJECT_ROOT)
    create_all_for_test_engine(engine, Base.metadata)
    reset_all_tables(engine, Base.metadata)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with SessionLocal() as db:
        db.add(AuthUser(user_id="alice", username="alice", enabled=True))
        db.add(AuthUser(user_id="bob", username="bob", enabled=True))
        db.add(McpServer(
            id="server-1",
            source="personal",
            owner_user_id="alice",
            name="统一操作记录管理系统",
            url="https://93.184.216.34/mcp",
            status="published",
        ))
        db.add(McpServer(
            id="official-1",
            source="official",
            owner_user_id=None,
            name="官方研报库",
            url="https://93.184.216.34/official",
            status="published",
        ))
        db.flush()
        db.add(McpInstallation(id="install-1", server_id="server-1", user_id="alice", enabled=True))
        db.commit()
    _write_snapshot(SessionLocal, "install-1", TOOLS)
    yield SessionLocal
    reset_all_tables(engine, Base.metadata)
    engine.dispose()


def _write_snapshot(session_factory, installation_id: str, tools: list[dict]) -> None:
    with session_factory() as db:
        db.query(McpToolSnapshot).filter(
            McpToolSnapshot.installation_id == installation_id
        ).delete(synchronize_session=False)
        for tool in tools:
            db.add(McpToolSnapshot(
                installation_id=installation_id,
                tool_name=tool["name"],
                title=tool.get("title"),
                description=tool.get("description"),
                input_schema_json="{}",
                schema_hash="hash-" + tool["name"],
                connection_fingerprint="fp",
            ))
        db.commit()


def _server(session_factory, server_id: str = "server-1") -> McpServer:
    with session_factory() as db:
        server = db.get(McpServer, server_id)
        db.expunge(server)
        return server


def _config_version(session_factory, key: str) -> int | None:
    with session_factory() as db:
        row = db.get(McpConfigVersion, key)
        return None if row is None else int(row.version)


async def test_personal_summary_derives_from_snapshot_once_per_source(session_factory):
    service = AsyncMock()
    service.generate.return_value = "对接操作记录系统。能做：查询、新增。适合：审计。"

    written = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: service,
    )

    assert written is True
    server = _server(session_factory)
    assert server.description == "对接操作记录系统。能做：查询、新增。适合：审计。"
    assert server.description_source_hash == capability_source_hash(
        "统一操作记录管理系统", tool_digests(TOOLS),
    )
    # Routing text only: neither the server version nor the catalog generation
    # moves, so a catalog being built or a running Agent is never invalidated.
    assert server.version == 1
    assert _config_version(session_factory, "user:alice") is None
    service.generate.assert_awaited_once()
    assert service.generate.await_args.args[0] == "统一操作记录管理系统"
    assert [item.name for item in service.generate.await_args.args[1]] == [
        "create_record", "query_records",
    ]

    # Same snapshot (any order): no model call, no version bump.
    service.generate.reset_mock()
    again = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: service,
    )
    assert again is False
    service.generate.assert_not_awaited()

    # Tool set changed on the next discovery: regenerate.
    _write_snapshot(session_factory, "install-1", TOOLS[:1])
    service.generate.return_value = "新摘要"
    changed = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: service,
    )
    assert changed is True
    assert _server(session_factory).description == "新摘要"

    # The connection name is part of the prompt: a rename regenerates too.
    with session_factory() as db:
        db.get(McpServer, "server-1").name = "审计记录系统"
        db.commit()
    service.generate.return_value = "改名后的摘要"
    renamed = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: service,
    )
    assert renamed is True
    assert service.generate.await_args.args[0] == "审计记录系统"
    assert _server(session_factory).description == "改名后的摘要"


async def test_personal_summary_only_covers_published_tools(session_factory):
    with session_factory() as db:
        db.add(McpToolVisibility(
            installation_id="install-1",
            enabled_tools_json=None,
            disabled_tools_json='["create_record"]',
        ))
        db.commit()
    service = AsyncMock()
    service.generate.return_value = "只读摘要"

    written = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: service,
    )

    assert written is True
    assert [item.name for item in service.generate.await_args.args[1]] == ["query_records"]
    assert _server(session_factory).description_source_hash == capability_source_hash(
        "统一操作记录管理系统", tool_digests(TOOLS[:1]),
    )

    # Hiding every tool leaves nothing to describe: no model call.
    with session_factory() as db:
        row = db.get(McpToolVisibility, "install-1")
        row.enabled_tools_json = "[]"
        db.commit()
    service.generate.reset_mock()
    assert await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: service,
    ) is False
    service.generate.assert_not_awaited()


async def test_personal_server_without_snapshot_is_not_summarized(session_factory):
    _write_snapshot(session_factory, "install-1", [])
    service = AsyncMock()

    written = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: service,
    )

    assert written is False
    service.generate.assert_not_awaited()


def _stamp_probe(session_factory, server_id: str):
    with session_factory() as db:
        server = db.get(McpServer, server_id)
        server.last_tested_at = now_naive()
        db.commit()
        return server.last_tested_at


async def test_official_summary_uses_the_probe_tools(session_factory):
    generation = _stamp_probe(session_factory, "official-1")
    service = AsyncMock()
    service.generate.return_value = "官方研报检索。能做：检索研报。适合：研报。"

    written = await ensure_capability_summary(
        session_factory, "official-1", tools=TOOLS,
        probe_generation=generation, service_factory=lambda: service,
    )

    assert written is True
    server = _server(session_factory, "official-1")
    assert server.description == "官方研报检索。能做：检索研报。适合：研报。"
    assert server.description_source_hash == capability_source_hash("官方研报库", tool_digests(TOOLS))
    assert server.version == 1
    assert _config_version(session_factory, "global") is None


async def test_official_result_is_dropped_when_renamed_during_generation(session_factory):
    generation = _stamp_probe(session_factory, "official-1")

    async def generate_while_renamed(_name, _digests):
        with session_factory() as db:
            db.get(McpServer, "official-1").name = "官方研报库（新）"
            db.commit()
        return "旧名字的摘要"

    service = type("Service", (), {"generate": staticmethod(generate_while_renamed)})()
    written = await ensure_capability_summary(
        session_factory, "official-1", tools=TOOLS,
        probe_generation=generation, service_factory=lambda: service,
    )

    assert written is False
    assert _server(session_factory, "official-1").description is None


async def test_official_late_result_is_dropped_once_a_newer_probe_committed(session_factory):
    old_generation = _stamp_probe(session_factory, "official-1")

    async def generate_while_newer_probe_lands(_name, _digests):
        _stamp_probe(session_factory, "official-1")
        return "OLD summary"

    old_service = type("Service", (), {"generate": staticmethod(generate_while_newer_probe_lands)})()
    written = await ensure_capability_summary(
        session_factory, "official-1", tools=TOOLS,
        probe_generation=old_generation, service_factory=lambda: old_service,
    )

    assert written is False
    assert _server(session_factory, "official-1").description is None


async def test_late_result_is_dropped_when_snapshot_changed_during_generation(session_factory):
    async def generate_against_old_snapshot(_name, _digests):
        # A newer discovery replaces the snapshot while the model is running.
        _write_snapshot(session_factory, "install-1", TOOLS[:1])
        return "OLD summary"

    old_service = type("Service", (), {"generate": staticmethod(generate_against_old_snapshot)})()
    old_written = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: old_service,
    )

    assert old_written is False
    assert _server(session_factory).description is None

    new_service = AsyncMock()
    new_service.generate.return_value = "NEW summary"
    new_written = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: new_service,
    )

    assert new_written is True
    assert _server(session_factory).description == "NEW summary"


async def test_duplicate_result_for_the_same_source_is_not_written_twice(session_factory):
    async def generate_while_peer_writes(_name, _digests):
        with session_factory() as db:
            server = db.get(McpServer, "server-1")
            server.description = "peer summary"
            server.description_source_hash = capability_source_hash(
                "统一操作记录管理系统", tool_digests(TOOLS),
            )
            db.commit()
        return "my summary"

    service = type("Service", (), {"generate": staticmethod(generate_while_peer_writes)})()
    written = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: service,
    )

    assert written is False
    assert _server(session_factory).description == "peer summary"


async def test_generation_failure_keeps_old_summary(session_factory):
    with session_factory() as db:
        db.get(McpServer, "server-1").description = "旧摘要"
        db.commit()
    service = AsyncMock()
    service.generate.side_effect = RuntimeError("llm down")

    with pytest.raises(RuntimeError, match="llm down"):
        await ensure_capability_summary(
            session_factory, "server-1", service_factory=lambda: service,
        )

    server = _server(session_factory)
    assert server.description == "旧摘要"
    assert server.description_source_hash is None
    assert server.version == 1


async def test_write_back_never_waits_for_a_locked_row(session_factory):
    from sqlalchemy.exc import OperationalError

    service = AsyncMock()
    service.generate.return_value = "摘要"
    with session_factory() as holder:
        # Another transaction (an edit, a probe) holds the server row.
        holder.query(McpServer).filter(McpServer.id == "server-1").with_for_update().one()

        with pytest.raises(OperationalError):
            await asyncio.wait_for(
                ensure_capability_summary(
                    session_factory, "server-1", service_factory=lambda: service,
                ),
                timeout=5,
            )
        holder.rollback()

    assert _server(session_factory).description is None


async def test_vanished_server_is_not_written(session_factory):
    async def generate(_name, _digests):
        with session_factory() as db:
            db.delete(db.get(McpServer, "server-1"))
            db.commit()
        return "迟到的摘要"

    service = type("Service", (), {"generate": staticmethod(generate)})()

    written = await ensure_capability_summary(
        session_factory, "server-1", service_factory=lambda: service,
    )

    assert written is False
    with session_factory() as db:
        assert db.get(McpServer, "server-1") is None


# ---------------------------------------------------------------------------
# Background scheduling
# ---------------------------------------------------------------------------


@pytest.fixture()
def scheduler(monkeypatch):
    import src.api.services.mcp_summary_service as module

    monkeypatch.setattr(module, "_session_factory", lambda: "factory")
    module._inflight.clear()
    module._rerun.clear()
    yield module
    module._inflight.clear()
    module._rerun.clear()


async def _settle(module) -> None:
    while module._inflight:
        await asyncio.gather(*module._inflight.values(), return_exceptions=True)
        await asyncio.sleep(0)


async def test_schedule_runs_once_per_server_and_replays_a_request_made_mid_run(
    scheduler, monkeypatch,
):
    calls: list[tuple[str, object]] = []
    gate = asyncio.Event()

    async def ensure(_factory, server_id, *, tools=None, probe_generation=None):
        calls.append((server_id, tools, probe_generation))
        await gate.wait()
        return True

    monkeypatch.setattr(scheduler, "ensure_capability_summary", ensure)

    schedule_capability_summary("server-1")
    await asyncio.sleep(0)
    schedule_capability_summary("server-1")  # a discovery landing mid-run
    schedule_capability_summary("server-1")
    schedule_capability_summary("official-1", tools=TOOLS, probe_generation="gen-1")
    await asyncio.sleep(0)
    assert calls == [("server-1", None, None), ("official-1", TOOLS, "gen-1")]

    gate.set()
    await _settle(scheduler)

    # The mid-run requests collapse into exactly one replay.
    assert calls == [
        ("server-1", None, None),
        ("official-1", TOOLS, "gen-1"),
        ("server-1", None, None),
    ]
    assert not scheduler._inflight and not scheduler._rerun


async def test_schedule_logs_failure_and_timeout_with_type_only(scheduler, monkeypatch, caplog):
    async def failing(_factory, server_id, *, tools=None, probe_generation=None):
        raise RuntimeError("gateway echoed Bearer sk-secret-token")

    monkeypatch.setattr(scheduler, "ensure_capability_summary", failing)
    with caplog.at_level("WARNING"):
        schedule_capability_summary("server-1")
        await _settle(scheduler)

    assert "能力摘要生成失败" in caplog.text
    assert "RuntimeError" in caplog.text
    # Gateway error bodies may echo request text or keys; only the type is logged.
    assert "sk-secret-token" not in caplog.text

    async def slow(_factory, server_id, *, tools=None, probe_generation=None):
        await asyncio.sleep(1)

    caplog.clear()
    monkeypatch.setattr(scheduler, "ensure_capability_summary", slow)
    monkeypatch.setattr(scheduler, "SUMMARY_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level("WARNING"):
        schedule_capability_summary("server-1")
        await _settle(scheduler)

    assert "TimeoutError" in caplog.text
