"""Focused tests for model-facing tool exposure planning."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agent.agent import Agent
from src.agent.schema import FunctionCall, LLMResponse, ToolCall
from src.agent.tools.ask_user_tool import AskUserQuestionTool
from src.agent.tools.base import (
    ToolExposure,
    ToolRef as AgentToolRef,
    ToolRuntimeContext,
)
from src.agent.tools.mcp_tool import McpRemoteTool
from src.agent.tools.tool_discovery import (
    DeferredToolCatalogStale,
    MCP_TOOL_SEARCH_NAME,
)
from tests.helpers import MockLLMClient, MockTool


class ExposureTool(MockTool):
    def __init__(self, name: str, exposure: ToolExposure, description: str = "") -> None:
        super().__init__(name)
        self.exposure = exposure
        self._description = description or f"Capability provided by {name}"

    @property
    def description(self) -> str:
        return self._description


class ExposureMcpTool(ExposureTool):
    def __init__(
        self,
        name: str,
        installation_id: str,
        live_reads: list[str],
        *,
        exposure: ToolExposure = ToolExposure.DIRECT,
        description: str = "",
    ) -> None:
        super().__init__(name, exposure, description)
        self._installation_id = installation_id
        self._live_reads = live_reads

    @property
    def tool_ref(self) -> AgentToolRef:
        return AgentToolRef(
            provider="mcp",
            name=self.name,
            server_id="server-1",
            installation_id=self._installation_id,
        )

    @property
    def schema_hash(self) -> str:
        return f"schema-{self.name}"

    def current_connection_fingerprint(self) -> str:
        self._live_reads.append(self.name)
        return "live-fingerprint"


class CapturingLLM(MockLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.tool_names_by_call: list[list[str]] = []

    async def generate_stream(
        self,
        messages,
        tools=None,
        on_content=None,
        on_thinking=None,
        on_tool_call=None,
    ):
        self.tool_names_by_call.append([tool.name for tool in (tools or [])])
        return await super().generate_stream(
            messages,
            tools=tools,
            on_content=on_content,
            on_thinking=on_thinking,
            on_tool_call=on_tool_call,
        )


class StubDeferredRetriever:
    def __init__(self, ranked_names, *, on_rank=None):
        self.ranked_names = list(ranked_names)
        self.candidate_names = []
        self.on_rank = on_rank

    async def rank(self, query, candidates, *, limit):
        self.candidate_names = [item.model_name for item in candidates]
        if self.on_rank is not None:
            self.on_rank()
        return list(self.ranked_names)


def _tool_call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id=f"call-{name}",
                type="function",
                function=FunctionCall(name=name, arguments=arguments),
            )
        ],
        finish_reason="tool_calls",
    )


def _agent(tmp_path, tools, llm=None, **kwargs) -> Agent:
    return Agent(
        llm_client=llm or MockLLMClient(),
        system_prompt="test",
        tools=tools,
        workspace_dir=str(tmp_path / "workspace"),
        **kwargs,
    )


def test_exposure_projection_keeps_deferred_and_hidden_out_of_initial_request(tmp_path):
    direct = ExposureTool("direct", ToolExposure.DIRECT)
    deferred = ExposureTool("deferred", ToolExposure.DEFERRED)
    hidden = ExposureTool("hidden", ToolExposure.HIDDEN)
    model_only = ExposureTool("model_only", ToolExposure.DIRECT_MODEL_ONLY)
    agent = _agent(tmp_path, [direct, deferred, hidden, model_only])

    assert [tool.name for tool in agent._visible_tools_for_request("session-a")] == [
        "direct",
        "model_only",
        MCP_TOOL_SEARCH_NAME,
    ]
    assert "enabled MCP" in agent.tools[MCP_TOOL_SEARCH_NAME].description
    assert "Proactively match the user's request" in (
        agent.tools[MCP_TOOL_SEARCH_NAME].description
    )


@pytest.mark.asyncio
async def test_discovery_exposes_only_matching_deferred_schema_on_next_step(tmp_path):
    weather = ExposureTool(
        "mcp__weather__forecast",
        ToolExposure.DEFERRED,
        "Get a city weather forecast",
    )
    spreadsheet = ExposureTool(
        "mcp__office__sheet",
        ToolExposure.DEFERRED,
        "Edit spreadsheet cells",
    )
    llm = CapturingLLM()
    llm.stream_responses = [
        _tool_call(MCP_TOOL_SEARCH_NAME, {"query": "weather"}),
        _tool_call(weather.name, {"param1": "Shanghai"}),
        LLMResponse(content="done", tool_calls=[], finish_reason="stop"),
    ]
    agent = _agent(tmp_path, [weather, spreadsheet], llm=llm)

    [event async for event in agent.run_agui("session-a", "run-a")]

    assert llm.tool_names_by_call[0] == [MCP_TOOL_SEARCH_NAME]
    assert weather.name in llm.tool_names_by_call[1]
    assert spreadsheet.name not in llm.tool_names_by_call[1]
    assert weather.execute_count == 1
    assert spreadsheet.execute_count == 0


@pytest.mark.asyncio
async def test_discovery_does_not_enable_same_response_deferred_execution(tmp_path):
    deferred = ExposureTool(
        "mcp__weather__forecast",
        ToolExposure.DEFERRED,
        "Get a city weather forecast",
    )
    llm = CapturingLLM()
    llm.stream_responses = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="call-search",
                    type="function",
                    function=FunctionCall(
                        name=MCP_TOOL_SEARCH_NAME,
                        arguments={"query": "weather"},
                    ),
                ),
                ToolCall(
                    id="call-deferred",
                    type="function",
                    function=FunctionCall(
                        name=deferred.name,
                        arguments={"param1": "Shanghai"},
                    ),
                ),
            ],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="done", tool_calls=[], finish_reason="stop"),
    ]
    agent = _agent(tmp_path, [deferred], llm=llm)

    events = [event async for event in agent.run_agui("session-a", "run-a")]

    assert llm.tool_names_by_call[0] == [MCP_TOOL_SEARCH_NAME]
    assert deferred.name in llm.tool_names_by_call[1]
    assert deferred.name in agent._activated_deferred_tools["session-a"]
    assert deferred.execute_count == 0
    deferred_result = next(
        event
        for event in events
        if getattr(event, "tool_call_id", None) == "call-deferred"
        and hasattr(event, "content")
    )
    assert deferred_result.content == "工具已重新加载，将从下一步骤开始可用；请重试该调用。"


@pytest.mark.asyncio
async def test_known_deferred_tool_cold_recovers_then_executes_on_next_step(tmp_path):
    deferred = ExposureTool("mcp__server__danger", ToolExposure.DEFERRED)
    llm = CapturingLLM()
    llm.stream_responses = [
        _tool_call(deferred.name, {"param1": "value"}),
        _tool_call(deferred.name, {"param1": "value"}),
        LLMResponse(content="blocked", tool_calls=[], finish_reason="stop"),
    ]
    agent = _agent(tmp_path, [deferred], llm=llm)

    [event async for event in agent.run_agui("session-a", "run-a")]

    assert deferred.execute_count == 1
    assert deferred.name not in llm.tool_names_by_call[0]
    assert deferred.name in llm.tool_names_by_call[1]
    assert deferred.name in agent._activated_deferred_tools["session-a"]
    assert any(
        message.role == "tool"
        and message.name == deferred.name
        and message.content == "工具已重新加载，将从下一步骤开始可用；请重试该调用。"
        for message in agent.messages
    )
    assert all(
        deferred.name not in str(message.content)
        and MCP_TOOL_SEARCH_NAME not in str(message.content)
        for message in agent.messages
        if message.role == "tool"
    )


@pytest.mark.asyncio
async def test_denied_deferred_tool_is_not_cold_recovered(tmp_path):
    deferred = ExposureTool("mcp__server__denied", ToolExposure.DEFERRED)
    llm = CapturingLLM()
    llm.stream_responses = [
        _tool_call(deferred.name, {"param1": "value"}),
        LLMResponse(content="blocked", tool_calls=[], finish_reason="stop"),
    ]
    agent = _agent(tmp_path, [deferred], llm=llm, user_id="alice")

    denied = SimpleNamespace(effect="deny", reason="test deny", matched_rule_id=None)
    with patch.object(
        agent,
        "_resolve_tool_permissions",
        side_effect=lambda tools, *, session_id: [denied for _tool in tools],
    ):
        [event async for event in agent.run_agui("session-a", "run-a")]

    assert deferred.execute_count == 0
    assert "session-a" not in agent._activated_deferred_tools
    assert any(
        message.role == "tool"
        and message.name == deferred.name
        and message.content == "Tool is unavailable in this conversation"
        for message in agent.messages
    )


@pytest.mark.asyncio
async def test_nonhuman_agent_hides_and_never_interrupts_for_ask_user(tmp_path):
    ask_user = AskUserQuestionTool()
    llm = CapturingLLM()
    llm.stream_responses = [
        _tool_call(
            ask_user.name,
            {
                "questions": [
                    {
                        "question": "Proceed?",
                        "header": "Choice",
                        "options": [
                            {"label": "Yes", "description": "Continue"},
                            {"label": "No", "description": "Stop"},
                        ],
                    }
                ]
            },
        ),
        LLMResponse(content="done", tool_calls=[], finish_reason="stop"),
    ]
    agent = _agent(
        tmp_path,
        [ask_user],
        llm=llm,
        allow_human_interrupts=False,
    )

    events = [event async for event in agent.run_agui("session-a", "run-a")]

    assert ask_user.name not in llm.tool_names_by_call[0]
    assert not agent.has_pending_interrupt()
    assert not any(
        getattr(event, "outcome", None) == "interrupt"
        for event in events
    )
    result = next(
        event
        for event in events
        if getattr(event, "tool_call_id", None) == f"call-{ask_user.name}"
        and hasattr(event, "content")
    )
    assert result.content == "Tool is unavailable in this conversation"


@pytest.mark.asyncio
async def test_discovery_excludes_deny_and_hidden_tools(tmp_path):
    allowed = ExposureTool("allowed_remote", ToolExposure.DEFERRED, "remote records")
    denied = ExposureTool("denied_remote", ToolExposure.DEFERRED, "remote records")
    hidden = ExposureTool("hidden_remote", ToolExposure.HIDDEN, "remote records")
    agent = _agent(tmp_path, [allowed, denied, hidden], user_id="alice")

    def resolve(tools, *, session_id):
        return [
            SimpleNamespace(
                effect="deny" if tool.name == denied.name else "allow",
                reason="test",
                matched_rule_id=None,
            )
            for tool in tools
        ]

    with patch.object(agent, "_resolve_tool_permissions", side_effect=resolve):
        matches = await agent._discover_deferred_tools(
            session_id="session-a",
            query="remote records",
            names=[],
            limit=20,
        )

    assert [match["model_name"] for match in matches] == [allowed.name]
    assert set(agent._activated_deferred_tools["session-a"]) == {allowed.name}


@pytest.mark.asyncio
async def test_semantic_discovery_filters_permissions_before_and_after_ranking(tmp_path):
    allowed = ExposureTool("allowed_remote", ToolExposure.DEFERRED, "market data")
    denied = ExposureTool("denied_remote", ToolExposure.DEFERRED, "market data")
    retriever = StubDeferredRetriever([denied.name, "foreign_tool", allowed.name])
    agent = _agent(
        tmp_path,
        [allowed, denied],
        user_id="alice",
        deferred_tool_retriever=retriever,
    )

    def resolve(tools, *, session_id):
        return [
            SimpleNamespace(
                effect="deny" if tool.name == denied.name else "allow",
                reason="test",
                matched_rule_id=None,
            )
            for tool in tools
        ]

    with patch.object(agent, "_resolve_tool_permissions", side_effect=resolve):
        matches = await agent._discover_deferred_tools(
            session_id="session-a",
            query="share price",
            names=[denied.name],
            limit=1,
        )

    assert retriever.candidate_names == [allowed.name]
    assert [match["model_name"] for match in matches] == [allowed.name]
    assert set(agent._activated_deferred_tools["session-a"]) == {allowed.name}


@pytest.mark.asyncio
async def test_semantic_discovery_rechecks_permission_changes_after_await(tmp_path):
    candidate = ExposureTool("remote_market", ToolExposure.DEFERRED, "market data")
    retriever = StubDeferredRetriever([candidate.name])
    agent = _agent(
        tmp_path,
        [candidate],
        user_id="alice",
        deferred_tool_retriever=retriever,
    )
    resolve_count = 0

    def resolve(tools, *, session_id):
        nonlocal resolve_count
        resolve_count += 1
        effect = "allow" if resolve_count == 1 else "deny"
        return [
            SimpleNamespace(effect=effect, reason="test", matched_rule_id=None)
            for _tool in tools
        ]

    with patch.object(agent, "_resolve_tool_permissions", side_effect=resolve):
        matches = await agent._discover_deferred_tools(
            session_id="session-a",
            query="share price",
            names=[],
            limit=1,
        )

    assert resolve_count == 2
    assert retriever.candidate_names == [candidate.name]
    assert matches == []
    assert "session-a" not in agent._activated_deferred_tools


@pytest.mark.asyncio
async def test_semantic_discovery_applies_limit_after_post_await_permissions(tmp_path):
    first = ExposureTool("first_remote", ToolExposure.DEFERRED, "market data")
    second = ExposureTool("second_remote", ToolExposure.DEFERRED, "market data")
    retriever = StubDeferredRetriever([first.name, second.name])
    agent = _agent(
        tmp_path,
        [first, second],
        user_id="alice",
        deferred_tool_retriever=retriever,
    )
    resolve_count = 0

    def resolve(tools, *, session_id):
        nonlocal resolve_count
        resolve_count += 1
        return [
            SimpleNamespace(
                effect=(
                    "deny"
                    if resolve_count == 2 and tool.name == first.name
                    else "allow"
                ),
                reason="test",
                matched_rule_id=None,
            )
            for tool in tools
        ]

    with patch.object(agent, "_resolve_tool_permissions", side_effect=resolve):
        matches = await agent._discover_deferred_tools(
            session_id="session-a",
            query="share price",
            names=[],
            limit=1,
        )

    assert resolve_count == 2
    assert [match["model_name"] for match in matches] == [second.name]
    assert set(agent._activated_deferred_tools["session-a"]) == {second.name}


@pytest.mark.asyncio
async def test_semantic_discovery_discards_mcp_catalog_that_changes_during_await(
    tmp_path,
):
    state = {"current": True, "checks": 0}

    def is_current():
        state["checks"] += 1
        return state["current"]

    def mark_stale():
        state["current"] = False

    candidate = ExposureMcpTool(
        "mcp__market__quote",
        "installation-1",
        [],
        exposure=ToolExposure.DEFERRED,
        description="realtime stock quote",
    )
    retriever = StubDeferredRetriever([candidate.name], on_rank=mark_stale)
    agent = _agent(
        tmp_path,
        [candidate],
        deferred_tool_retriever=retriever,
        deferred_tool_catalog_is_current=is_current,
    )

    with pytest.raises(DeferredToolCatalogStale):
        await agent._discover_deferred_tools(
            session_id="session-a",
            query="share price",
            names=[candidate.name],
            limit=1,
        )

    assert state["checks"] == 2
    assert retriever.candidate_names == [candidate.name]
    assert "session-a" not in agent._activated_deferred_tools


@pytest.mark.asyncio
async def test_mcp_tool_search_reports_catalog_reload_instead_of_false_no_match(
    tmp_path,
):
    candidate = ExposureMcpTool(
        "mcp__market__quote",
        "installation-1",
        [],
        exposure=ToolExposure.DEFERRED,
        description="realtime stock quote",
    )
    agent = _agent(
        tmp_path,
        [candidate],
        deferred_tool_catalog_is_current=lambda: False,
    )
    tool = agent.tools[MCP_TOOL_SEARCH_NAME]
    tool.set_runtime_context(ToolRuntimeContext(
        thread_id="session-a",
        run_id="run-a",
        tool_call_id="call-a",
        tool_name=MCP_TOOL_SEARCH_NAME,
    ))

    result = await tool.execute(query="stock quote")

    assert result.success is True
    assert "reloaded automatically on the next user step" in result.content
    assert "No available deferred tools matched" not in result.content


@pytest.mark.asyncio
async def test_zero_match_discovery_does_not_read_policy_or_live_bindings(tmp_path):
    deferred = ExposureTool(
        "mcp__weather__forecast",
        ToolExposure.DEFERRED,
        "Get a city weather forecast",
    )
    agent = _agent(tmp_path, [deferred], user_id="alice")

    with patch(
        "src.api.models.database.SessionLocal",
        side_effect=AssertionError("zero-match search must not query policy"),
    ):
        matches = await agent._discover_deferred_tools(
            session_id="session-a",
            query="unrelated spreadsheet capability",
            names=[],
            limit=20,
        )

    assert matches == []
    assert "session-a" not in agent._activated_deferred_tools


@pytest.mark.asyncio
async def test_discovery_uses_partial_keyword_recall_and_coverage_ranking(tmp_path):
    realtime = ExposureTool(
        "stock_highfreq_quotes",
        ToolExposure.DEFERRED,
        "A股股票行情数据的实时快照与高频实时行情指标",
    )
    history = ExposureTool(
        "get_stock_performance",
        ToolExposure.DEFERRED,
        "A股股票日频历史行情和技术指标",
    )
    unrelated = ExposureTool(
        "get_company_filings",
        ToolExposure.DEFERRED,
        "上市公司公告和定期报告",
    )
    agent = _agent(tmp_path, [history, unrelated, realtime])

    matches = await agent._discover_deferred_tools(
        session_id="session-a",
        query="股票 股价 实时行情",
        names=[],
        limit=2,
    )

    assert [item["model_name"] for item in matches] == [
        realtime.name,
        history.name,
    ]
    assert unrelated.name not in agent._activated_deferred_tools["session-a"]


@pytest.mark.asyncio
async def test_discovery_ranks_explicit_model_names_before_keyword_matches(tmp_path):
    keyword_match = ExposureTool(
        "stock_highfreq_quotes",
        ToolExposure.DEFERRED,
        "股票实时行情",
    )
    explicitly_named = ExposureTool(
        "get_stock_info",
        ToolExposure.DEFERRED,
        "股票基本资料",
    )
    agent = _agent(tmp_path, [keyword_match, explicitly_named])

    matches = await agent._discover_deferred_tools(
        session_id="session-a",
        query="股票 实时行情",
        names=[explicitly_named.name],
        limit=1,
    )

    assert [item["model_name"] for item in matches] == [explicitly_named.name]


@pytest.mark.asyncio
async def test_discovery_searches_only_bounded_description_preview(tmp_path):
    deferred = ExposureTool(
        "mcp__large__catalog",
        ToolExposure.DEFERRED,
        "prefix-marker " + ("界" * 5000) + " tail-marker",
    )
    agent = _agent(tmp_path, [deferred])

    prefix_matches = await agent._discover_deferred_tools(
        session_id="session-a",
        query="prefix-marker",
        names=[],
        limit=20,
    )
    tail_matches = await agent._discover_deferred_tools(
        session_id="session-b",
        query="tail-marker",
        names=[],
        limit=20,
    )

    assert [item["model_name"] for item in prefix_matches] == [deferred.name]
    assert tail_matches == []


def test_invalid_exposure_is_rejected_at_agent_initialization(tmp_path):
    invalid = ExposureTool("invalid", ToolExposure.DIRECT)
    invalid.exposure = "deferred"  # type: ignore[assignment]

    with pytest.raises(ValueError, match="Invalid exposure.*invalid"):
        _agent(tmp_path, [invalid])


@pytest.mark.asyncio
async def test_deferred_activation_is_bounded_to_32_tools_per_session(tmp_path):
    deferred = [
        ExposureTool(f"remote_{index:02d}", ToolExposure.DEFERRED, "remote capability")
        for index in range(40)
    ]
    agent = _agent(tmp_path, deferred)

    await agent._discover_deferred_tools(
        session_id="session-a",
        query="",
        names=[tool.name for tool in deferred[:20]],
        limit=20,
    )
    await agent._discover_deferred_tools(
        session_id="session-a",
        query="",
        names=[tool.name for tool in deferred[20:]],
        limit=20,
    )

    active = agent._activated_deferred_tools["session-a"]
    assert len(active) == 32
    assert list(active) == [tool.name for tool in deferred[8:]]


@pytest.mark.asyncio
async def test_silent_memory_flush_excludes_deny_ask_and_hidden_tools(tmp_path):
    denied = ExposureTool("record_memory", ToolExposure.DIRECT)
    ask = ExposureTool("update_long_term_memory", ToolExposure.DIRECT)
    hidden = ExposureTool("update_user", ToolExposure.HIDDEN)
    llm = MockLLMClient()
    agent = _agent(
        tmp_path,
        [denied, ask, hidden],
        llm=llm,
        user_id="alice",
        token_limit=100,
    )

    def resolve(tool, *, session_id):
        effects = {
            denied.name: "deny",
            ask.name: "ask",
            hidden.name: "allow",
        }
        return SimpleNamespace(
            effect=effects[tool.name],
            reason="test policy",
            matched_rule_id=None,
        )

    with (
        patch.object(agent, "_estimate_tokens", return_value=100),
        patch.object(agent, "_resolve_tool_permission", side_effect=resolve),
    ):
        flushed = await agent.maybe_flush_memory_silent(session_id="session-a")

    assert flushed is False
    assert llm.call_count == 0
    assert denied.execute_count == 0
    assert ask.execute_count == 0
    assert hidden.execute_count == 0
    assert agent._memory_flushed_this_compaction is False


def test_schema_hash_is_forwarded_to_visibility_and_runtime_permission_checks(tmp_path):
    tool = ExposureTool("schema_bound", ToolExposure.DIRECT)
    tool.schema_hash = "schema-v7"
    agent = _agent(tmp_path, [tool], user_id="alice")
    decision = SimpleNamespace(
        effect="allow",
        reason="schema-bound allow",
        matched_rule_id="rule-1",
        exposed=True,
    )
    db = object()

    with (
        patch(
            "src.api.models.database.SessionLocal",
            side_effect=lambda: nullcontext(db),
        ),
        patch(
            "src.api.services.tool_permission_service.evaluate_tool_permissions",
            return_value=[decision],
        ) as evaluate_batch,
        patch(
            "src.api.services.tool_permission_service.evaluate_tool_permission",
            return_value=decision,
        ) as evaluate_single,
    ):
        assert agent._visible_tools_for_request("session-a") == [tool]
        check = evaluate_batch.call_args.kwargs["checks"][0]
        assert check.schema_hash == "schema-v7"

        resolved = agent._resolve_tool_permission(tool, session_id="session-a")
        assert resolved.effect == "allow"
        assert evaluate_single.call_args.kwargs["schema_hash"] == "schema-v7"


def test_visibility_resolves_live_fingerprint_once_per_installation(tmp_path):
    live_reads: list[str] = []
    first = ExposureMcpTool("remote_first", "installation-1", live_reads)
    second = ExposureMcpTool("remote_second", "installation-1", live_reads)
    agent = _agent(tmp_path, [first, second], user_id="alice")
    decisions = [
        SimpleNamespace(effect="allow", reason="allowed", matched_rule_id=None),
        SimpleNamespace(effect="allow", reason="allowed", matched_rule_id=None),
    ]
    db = object()

    with (
        patch(
            "src.api.models.database.SessionLocal",
            side_effect=lambda: nullcontext(db),
        ),
        patch(
            "src.api.services.tool_permission_service.evaluate_tool_permissions",
            return_value=decisions,
        ) as evaluate,
    ):
        assert agent._visible_tools_for_request("session-a") == [first, second]

    assert live_reads == [first.name]
    checks = evaluate.call_args.kwargs["checks"]
    assert [check.connection_fingerprint for check in checks[:2]] == [
        "live-fingerprint",
        "live-fingerprint",
    ]


def test_exposure_enum_values_are_stable():
    assert ToolExposure.DIRECT.value == "direct"
    assert ToolExposure.DEFERRED.value == "deferred"
    assert ToolExposure.HIDDEN.value == "hidden"
    assert ToolExposure.DIRECT_MODEL_ONLY.value == "direct_model_only"
    assert McpRemoteTool.exposure == ToolExposure.DEFERRED
