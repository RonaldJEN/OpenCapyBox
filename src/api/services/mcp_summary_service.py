"""MCP 连接能力摘要：由 LLM 根据连接当前已发布的工具快照派生的统一格式说明。

不变量：``mcp_servers.description`` = f(连接名, 当前已发布的快照工具集)。它是 Agent
请求级 system 提示中「数据连接」路由块与 ``mcp_tool_search`` 检索文档的来源；用户不再手填。

任何写快照或改变其来源的路径（测试、激活、导入激活、运行时目录发现、个人连接改名、
个人连接的发布策略变更）在提交后都只做一件事：``schedule_capability_summary(server_id)``。
生成在后台进行，按来源指纹去重；写回时重新计算当前来源的指纹，与生成所用指纹一致才写，
迟到的旧结果自然作废。

官方服务没有平台凭证下的快照，只由管理员探针把本次发现的工具清单传进来生成；
用户凭证发现到的工具可能含私有数据，不得写入所有用户共享的说明。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import weakref
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any

from src.agent.llm import LLMClient
from src.agent.schema import Message
from src.agent.schema.run_context import current_run_context
from src.api.model_registry import ModelConfig, get_model_registry
from src.api.models.mcp import McpInstallation, McpServer, McpToolSnapshot
from src.api.services.mcp_service import _tool_is_enabled, _tool_visibility_policy

logger = logging.getLogger(__name__)

# 单条工具说明与工具数量的输入上限：80 × 300 字外加名称/标题，约 30KB 以内。
_MAX_TOOLS_IN_PROMPT = 80
_MAX_TOOL_DESCRIPTION_CHARS = 300
# 与 agent_service 请求级 system 提示块对连接说明的截断一致。
MAX_SUMMARY_CHARS = 500
# 单次生成（含模型调用与写回）的后台预算；超时只记日志，下次发现再试。
SUMMARY_TIMEOUT_SECONDS = 20.0
# 同一进程内同时进行的模型调用上限：导入几十个连接也不会打爆模型网关。
_SUMMARY_CONCURRENCY = 2

SUMMARY_SYSTEM_PROMPT = """你是 MCP 数据连接的能力摘要生成器。AI 助手只能看到连接名和这条摘要，并据此决定用户的问题该不该用这个连接、用什么关键词去检索它的工具，所以摘要必须准确概括工具清单体现的真实能力。

输出一行中文，固定格式：
{一句话说明这个连接是什么、对接什么系统}。能做：{动词短语}、{动词短语}、…。适合：{关键词}、{关键词}、…。

要求：
1. 「能做」列 3 到 6 项，合并同类工具，用动词开头，不要罗列工具名。
2. 「适合」列 3 到 5 个用户提问时会用到的关键词或场景。
3. 只依据工具清单，不臆测清单之外的能力。
4. 不超过 200 字，不用 Markdown，不加引号，只输出摘要本身。"""


@dataclass(frozen=True)
class McpToolDigest:
    name: str
    title: str
    description: str


def tool_digests(tools: list[Any]) -> list[McpToolDigest]:
    """Project raw discovery results into the bounded fields the summary uses."""

    digests: list[McpToolDigest] = []
    for tool in tools:
        raw = tool.model_dump(exclude_none=True) if hasattr(tool, "model_dump") else tool
        digests.append(McpToolDigest(
            name=str(raw["name"]),
            title=str(raw.get("title") or ""),
            description=str(raw.get("description") or ""),
        ))
    digests.sort(key=lambda item: item.name)
    return digests


def capability_source_hash(server_name: str, digests: list[McpToolDigest]) -> str:
    """Fingerprint everything the prompt is built from: connection name and the
    discovered tool set (order-insensitive via sorted digests)."""

    payload = json.dumps(
        [server_name, [[item.name, item.title, item.description] for item in digests]],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_capability_summary_prompt(server_name: str, digests: list[McpToolDigest]) -> str:
    lines = [f"连接名：{server_name}", f"工具数：{len(digests)}", "工具清单（名称 | 标题 | 说明）："]
    for item in digests[:_MAX_TOOLS_IN_PROMPT]:
        description = item.description[:_MAX_TOOL_DESCRIPTION_CHARS]
        lines.append(f"- {item.name} | {item.title} | {description}")
    if len(digests) > _MAX_TOOLS_IN_PROMPT:
        lines.append(f"（其余 {len(digests) - _MAX_TOOLS_IN_PROMPT} 个工具省略）")
    lines.append("请输出这个连接的能力摘要。")
    return "\n".join(lines)


def normalize_summary(text: str) -> str:
    """Collapse the model reply to one bounded line."""

    return " ".join(text.split()).strip("\"'“”")[:MAX_SUMMARY_CHARS]


def summary_model_config(config: ModelConfig) -> ModelConfig:
    """Run the digest without reasoning wherever the model can encode that.

    A 200-character digest needs no thinking. ``ModelConfig`` re-validates on
    ``replace``: the switch exists only for OpenAI-protocol models whose wire
    format can encode it and whose effort list (if any) includes ``off``; for
    anything else the catalog configuration is used as is.
    """

    if config.provider != "openai" or config.thinking_wire_format == "none":
        return config
    if config.supported_reasoning_efforts and "off" not in config.supported_reasoning_efforts:
        return config
    return replace(
        config,
        thinking_mode="disabled",
        enable_thinking=False,
        reasoning_effort=None,
    )


class McpSummaryService:
    def __init__(self, config: ModelConfig):
        self.config = summary_model_config(config)
        self._client = LLMClient.from_model_config(self.config)

    async def generate(self, server_name: str, digests: list[McpToolDigest]) -> str:
        started_at = perf_counter()
        # 摘要只按 summary_model_config 的配置调用：清掉当前 run 对主模型的推理覆盖。
        run_context_token = current_run_context.set(None)
        try:
            response = await self._client.generate(messages=[
                Message(role="system", content=SUMMARY_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=build_capability_summary_prompt(server_name, digests),
                ),
            ])
        finally:
            current_run_context.reset(run_context_token)
        summary = normalize_summary(str(response.content or ""))
        usage = response.usage
        logger.info(
            "mcp_capability_summary: model=%s server=%s tools=%d prompt_tokens=%s "
            "completion_tokens=%s latency_s=%.2f",
            self.config.id,
            server_name,
            len(digests),
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
            perf_counter() - started_at,
        )
        if not summary:
            raise ValueError(f"MCP 连接 {server_name} 的能力摘要为空")
        return summary


def _snapshot_digests(db, server: McpServer) -> list[McpToolDigest]:
    """Published tool set of a personal server: its owner installation's
    snapshot minus the tools its publication policy hides from the Agent."""

    installation = (
        db.query(McpInstallation)
        .filter(
            McpInstallation.server_id == server.id,
            McpInstallation.user_id == server.owner_user_id,
        )
        .one_or_none()
    )
    if installation is None:
        return []
    enabled_tools, disabled_tools, _revision = _tool_visibility_policy(db, installation)
    rows = (
        db.query(
            McpToolSnapshot.tool_name,
            McpToolSnapshot.title,
            McpToolSnapshot.description,
        )
        .filter(McpToolSnapshot.installation_id == installation.id)
        .all()
    )
    return tool_digests([
        {"name": row.tool_name, "title": row.title, "description": row.description}
        for row in rows
        if _tool_is_enabled(row.tool_name, enabled_tools, disabled_tools)
    ])


def _current_source(
    db,
    server: McpServer,
    tools: list[Any] | None,
) -> tuple[str, list[McpToolDigest], str]:
    digests = tool_digests(tools) if tools is not None else _snapshot_digests(db, server)
    server_name = str(server.name)
    return server_name, digests, capability_source_hash(server_name, digests)


async def ensure_capability_summary(
    session_factory,
    server_id: str,
    *,
    tools: list[Any] | None = None,
    probe_generation=None,
    service_factory=lambda: McpSummaryService(get_model_registry().get_default()),
) -> bool:
    """Make ``description`` match the server's current source; return whether written.

    The source is the connection name plus its tool set: the owner's committed
    snapshot for a personal server, or ``tools`` passed by the platform probe
    for an official one together with ``probe_generation`` (the
    ``last_tested_at`` that probe stamped, since no platform snapshot exists to
    re-read). No database lock is held across the model call. The write-back
    re-locks the server row and checks the source is still current: the
    recomputed fingerprint for a personal server, the latest probe for an
    official one; a result that is no longer current, or already stored by
    another run, is dropped.

    The summary is routing text, not execution configuration: writing it bumps
    neither ``version`` nor the catalog generation, so it never invalidates a
    catalog being built or a running Agent. It reaches the routing block and
    the search document at the next catalog refresh (300 s).
    """

    with session_factory() as db:
        server = db.get(McpServer, server_id)
        if server is None:
            return False
        server_name, digests, source_hash = _current_source(db, server, tools)
        if not digests or server.description_source_hash == source_hash:
            return False

    summary = await service_factory().generate(server_name, digests)

    with session_factory() as db:
        # The write-back runs on the event loop like the rest of the sync DB
        # code; never wait for a row lock here. A conflicting writer (edit,
        # probe, another summary) means "not now": fail fast, the next
        # discovery retries by fingerprint.
        server = (
            db.query(McpServer)
            .filter(McpServer.id == server_id)
            .with_for_update(nowait=True)
            .one_or_none()
        )
        if server is None:
            db.rollback()
            return False
        # Personal: the snapshot plus name must still match. Official: the
        # name must still match and no newer probe may have landed.
        _name, _digests, current_hash = _current_source(db, server, tools)
        current = current_hash == source_hash and (
            tools is None or server.last_tested_at == probe_generation
        )
        if not current or server.description_source_hash == source_hash:
            db.rollback()
            return False
        server.description = summary
        server.description_source_hash = source_hash
        db.commit()
    return True


def _session_factory():
    from src.api.models.database import SessionLocal

    return SessionLocal


def _log_summary_failure(server_id: str, exc: BaseException) -> None:
    # Only the exception type: a model gateway may echo request text or keys
    # in its error body, and that must not reach the logs.
    logger.warning(
        "MCP 连接能力摘要生成失败，保留原说明: server=%s error=%s",
        server_id,
        type(exc).__name__,
    )


_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    asyncio.Semaphore,
] = weakref.WeakKeyDictionary()
_inflight: dict[str, asyncio.Task[None]] = {}
# A discovery that lands while its server is already being summarized: run
# once more afterwards against the then-current source instead of dropping it.
_rerun: dict[str, tuple[list[Any] | None, Any]] = {}


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_SUMMARY_CONCURRENCY)
        _SEMAPHORES[loop] = semaphore
    return semaphore


async def _run_capability_summary(
    server_id: str,
    tools: list[Any] | None,
    probe_generation,
) -> None:
    try:
        async with _semaphore():
            await asyncio.wait_for(
                ensure_capability_summary(
                    _session_factory(),
                    server_id,
                    tools=tools,
                    probe_generation=probe_generation,
                ),
                timeout=SUMMARY_TIMEOUT_SECONDS,
            )
    except asyncio.CancelledError:
        _rerun.pop(server_id, None)
        raise
    except Exception as exc:
        _log_summary_failure(server_id, exc)
    finally:
        _inflight.pop(server_id, None)
    if server_id in _rerun:
        tools, probe_generation = _rerun.pop(server_id)
        schedule_capability_summary(
            server_id,
            tools=tools,
            probe_generation=probe_generation,
        )


def schedule_capability_summary(
    server_id: str,
    *,
    tools: list[Any] | None = None,
    probe_generation=None,
) -> None:
    """Derive the summary in the background after a committed discovery.

    Personal servers are summarized from their committed snapshot, so callers
    pass only the id; the platform probe passes its discovered ``tools`` and
    the ``last_tested_at`` it stamped. One run per server at a time; a request
    arriving mid-run is replayed once the run finishes.
    """

    if server_id in _inflight:
        _rerun[server_id] = (tools, probe_generation)
        return
    _inflight[server_id] = asyncio.create_task(
        _run_capability_summary(server_id, tools, probe_generation)
    )


__all__ = [
    "MAX_SUMMARY_CHARS",
    "SUMMARY_TIMEOUT_SECONDS",
    "McpSummaryService",
    "McpToolDigest",
    "build_capability_summary_prompt",
    "capability_source_hash",
    "ensure_capability_summary",
    "normalize_summary",
    "schedule_capability_summary",
    "summary_model_config",
    "tool_digests",
]
