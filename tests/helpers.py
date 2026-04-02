"""测试公共工具函数 — 可被测试文件直接 import

提供统一的 Mock 类和工厂函数，消除跨测试文件的重复代码。
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from src.agent.tools.base import Tool, ToolResult
from src.agent.schema import Message, LLMResponse

__all__ = [
    # Mock 类
    "MockLLMClient", "MockTool", "SlowTool", "FakeAsyncStream",
    "MockModelConfig", "MockRegistry",
    # 工厂函数
    "make_query_db", "make_mock_sandbox", "make_agent_service",
    "make_agent", "fake_stream", "make_mock_round", "make_mock_httpx_client",
    "make_test_client", "make_mock_settings",
    "make_mock_agent", "make_fake_execution",
    "make_tool_call_agui_events",
    # 异步工具
    "collect_agui_events",
]


# ============== Mock 类 ==============


class MockLLMClient:
    """统一的 Mock LLM 客户端。

    支持 chat / generate / generate_stream 三种调用方式，
    通过 ``responses`` 队列控制返回值。

    用法::

        llm = MockLLMClient()
        llm.responses = [LLMResponse(content="hello", finish_reason="stop")]
    """

    def __init__(self):
        self.responses: list = []
        self.stream_responses: list = []
        self.call_count = 0

    def _next_response(self, *, stream: bool = False):
        self.call_count += 1
        if stream and self.stream_responses:
            return self.stream_responses.pop(0)
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(content="Mock response", finish_reason="stop")

    # Anthropic-style
    async def chat(self, messages, tools=None):
        resp = self._next_response()
        return Message(role="assistant", content=resp.content if hasattr(resp, "content") else str(resp))

    # OpenAI-style (non-stream)
    async def generate(self, messages, tools=None, **kwargs):
        return self._next_response()

    # OpenAI-style (stream)
    async def generate_stream(self, messages, tools=None, on_content=None, on_thinking=None, on_tool_call=None):
        resp = self._next_response(stream=True)
        if on_content and resp.content:
            await on_content(resp.content)
        return resp


class MockTool(Tool):
    """统一的 Mock 工具类。

    支持可选参数：
    - name: 工具名（默认 "mock_tool"）
    - should_fail: 如果为 True，execute 返回失败结果
    - raise_on_execute: 如果为 True，execute 抛出 RuntimeError（模拟工具崩溃）

    调用追踪：
    - execute_count: 被调用次数
    - last_args: 最后一次调用的参数
    """

    def __init__(self, name: str = "mock_tool", *, should_fail: bool = False,
                 raise_on_execute: bool = False):
        self._name = name
        self.should_fail = should_fail
        self.raise_on_execute = raise_on_execute
        self.execute_count = 0
        self.last_args: dict | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "A mock tool for testing"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "Test parameter"}
            },
            "required": ["param1"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        self.execute_count += 1
        self.last_args = kwargs
        if self.raise_on_execute:
            raise RuntimeError("Tool execution failed!")
        if self.should_fail:
            return ToolResult(success=False, content="", error="Mock tool failed")
        return ToolResult(success=True, content="Mock tool executed")


class SlowTool(Tool):
    """模拟耗时工具（用于取消测试）"""

    def __init__(self):
        self.call_count = 0

    @property
    def name(self) -> str:
        return "slow_tool"

    @property
    def description(self) -> str:
        return "A slow tool for testing"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"msg": {"type": "string"}}}

    async def execute(self, **kwargs) -> ToolResult:
        self.call_count += 1
        return ToolResult(success=True, content=f"done-{self.call_count}")


# ============== 工厂函数 ==============


def make_query_db(*, first=None, all_results=None, count=None, side_effect=None):
    """创建预配置 query chain 的 mock DB session。

    解决最广泛的重复模式: mock_db.query().filter().first/all/count/order_by

    参数:
        first: query().filter().first() 的返回值
        all_results: query().filter().all() 和 .order_by().all() 的返回值
        count: query().filter().count() 的返回值
        side_effect: 为 query().filter().order_by().all() 设置 side_effect（多次调用返回不同结果）

    用法::

        db = make_query_db(first=mock_round, all_results=[r1, r2])
    """
    db = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    db.delete = MagicMock()
    db.close = MagicMock()

    chain = db.query.return_value.filter.return_value
    chain.first.return_value = first
    chain.all.return_value = all_results or []
    chain.count.return_value = count or 0

    order_chain = chain.order_by.return_value
    if side_effect is not None:
        order_chain.all.side_effect = side_effect
    else:
        order_chain.all.return_value = all_results or []

    return db


def make_mock_sandbox(*, sandbox_id="sbx-test-123",
                      read_return=None, read_side_effect=None,
                      write_side_effect=None, run_return=None):
    """创建统一的 mock sandbox 实例。

    参数:
        sandbox_id: sandbox.id
        read_return: files.read_file 的返回值
        read_side_effect: files.read_file 的 side_effect
        write_side_effect: files.write_file 的 side_effect
        run_return: commands.run 的返回值

    用法::

        sandbox = make_mock_sandbox(read_return="file content")
        sandbox = make_mock_sandbox(read_side_effect=FileNotFoundError)
    """
    sandbox = AsyncMock()
    sandbox.id = sandbox_id

    # commands
    sandbox.commands = AsyncMock()
    if run_return is not None:
        sandbox.commands.run.return_value = run_return

    # files
    sandbox.files = AsyncMock()
    if read_side_effect:
        sandbox.files.read_file = AsyncMock(side_effect=read_side_effect)
    elif read_return is not None:
        sandbox.files.read_file = AsyncMock(return_value=read_return)
    else:
        sandbox.files.read_file = AsyncMock(return_value="")
    sandbox.files.read = AsyncMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.write_file = AsyncMock(side_effect=write_side_effect)

    # lifecycle
    sandbox.is_healthy = AsyncMock(return_value=True)
    sandbox.pause = AsyncMock()
    sandbox.kill = AsyncMock()
    sandbox.resume = AsyncMock()
    sandbox.renew = AsyncMock()

    return sandbox


def make_agent_service(*, sandbox=None, history_service=None,
                       session_id="session-123", user_id="test-user",
                       attach_db=False, mount_path="/home/user"):
    """创建 AgentService 实例，减少测试中的构造样板。

    参数:
        attach_db: 为 True 时在 history_service 上附加 mock_db，
                   并返回 (service, mock_db) 元组
        mount_path: get_sandbox_mount_path 的返回值
    """
    from src.api.services.agent_service import AgentService

    if history_service is None:
        history_service = MagicMock()
    if attach_db:
        history_service.db = MagicMock()

    with patch("src.api.services.agent_service.get_sandbox_mount_path", return_value=mount_path):
        svc = AgentService(
            sandbox=sandbox or make_mock_sandbox(),
            history_service=history_service,
            session_id=session_id,
            user_id=user_id,
        )

    if attach_db:
        return svc, history_service.db
    return svc


def make_agent(tmp_path=None, *, llm=None, tools=None, **kwargs):
    """创建 Agent 实例的统一工厂。

    用法::

        agent = make_agent(tmp_path)
        agent = make_agent(tmp_path, llm=my_llm, max_steps=3)
    """
    from src.agent.agent import Agent

    import tempfile
    ws_dir = str(tmp_path / "workspace") if tmp_path else tempfile.mkdtemp()
    return Agent(
        llm_client=llm or MockLLMClient(),
        system_prompt=kwargs.pop("system_prompt", "Test"),
        tools=tools if tools is not None else [MockTool()],
        workspace_dir=ws_dir,
        **kwargs,
    )


def fake_stream(response, on_content_text=None):
    """构建 fake generate_stream 函数，减少取消测试的样板。"""
    async def _fake_generate_stream(messages, tools, on_content=None, on_thinking=None):
        if on_content_text and on_content:
            await on_content(on_content_text)
        return response
    return _fake_generate_stream


class FakeAsyncStream:
    """通用的异步迭代器 mock，用于测试流式响应。

    用法::

        stream = FakeAsyncStream([chunk1, chunk2])
        async for chunk in stream:
            process(chunk)
    """

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration


def make_mock_round(*, round_id="round-123", session_id="session-123",
                    status="completed", final_response="Done", step_count=1):
    """创建 mock Round 对象的统一工厂。

    用法::

        r = make_mock_round(status="running", step_count=0)
    """
    round_obj = MagicMock()
    round_obj.id = round_id
    round_obj.session_id = session_id
    round_obj.status = status
    round_obj.final_response = final_response
    round_obj.step_count = step_count
    return round_obj


@asynccontextmanager
async def make_mock_httpx_client(mock_response=None, side_effect=None):
    """创建一个可复用的 httpx.AsyncClient mock 上下文管理器。

    用法::

        async with make_mock_httpx_client(mock_response=resp) as (mock_client, patcher):
            result = await tool.execute(query="test")
            mock_client.post.assert_called_once()
    """
    mock_client = AsyncMock()
    if side_effect:
        mock_client.post.side_effect = side_effect
    elif mock_response is not None:
        mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.agent.tools.glm_search_tool.httpx.AsyncClient") as MockClient:
        MockClient.return_value = mock_client
        yield mock_client, MockClient


# ============== Mock Model Config/Registry ==============


class MockModelConfig:
    """统一的 Mock 模型配置，用于 _validate_multimodal_blocks 等需要 ModelConfig 的测试。"""

    def __init__(self, supports_image: bool = False, max_images: int = 0,
                 *, supports_video: bool = False, max_videos: int = 0,
                 model_id: str = "mock-model"):
        self.id = model_id
        self.supports_image = supports_image
        self.max_images = max_images
        self.supports_video = supports_video
        self.max_videos = max_videos


class MockRegistry:
    """统一的 Mock 模型注册表。

    用法::

        with patch("...get_model_registry", return_value=MockRegistry(supports_image=True, max_images=5)):
            ...
    """

    def __init__(self, supports_image: bool = False, max_images: int = 0, **kwargs):
        self._cfg = MockModelConfig(supports_image=supports_image, max_images=max_images, **kwargs)

    def get_or_raise(self, _model_id: str):
        return self._cfg

    def get_default(self):
        return self._cfg


# ============== AGUI 事件收集 ==============


async def collect_agui_events(agent, *, thread_id="test_thread", run_id="test_run"):
    """收集 agent.run_agui 产生的所有事件，返回 (events, event_types)。

    用法::

        events, event_types = await collect_agui_events(agent)
        assert "RUN_STARTED" in event_types
    """
    events = []
    async for event in agent.run_agui(thread_id=thread_id, run_id=run_id):
        events.append(event)
    event_types = [e.type.value for e in events]
    return events, event_types


# ============== TestClient 工厂 ==============


def make_test_client(router, prefix, *, user="testuser", db=None):
    """创建带 dependency override 的 FastAPI TestClient。

    自动覆盖 get_current_user 和 get_db 依赖。
    返回的 client 上附加 mock_db 属性以方便测试访问。

    用法::

        client = make_test_client(config_routes.router, "/config")
        client.mock_db.query.return_value...
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api.deps import get_current_user
    from src.api.models.database import get_db

    app = FastAPI()
    app.include_router(router, prefix=prefix)
    app.dependency_overrides[get_current_user] = lambda: user

    if db is None:
        db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    client = TestClient(app)
    client.mock_db = db  # type: ignore[attr-defined]
    return client


# ============== Mock Settings 工厂 ==============


def make_mock_settings(**overrides):
    """创建统一的 mock settings 对象。

    默认提供 auth 相关的基本配置，可通过 overrides 覆盖任意属性。

    用法::

        s = make_mock_settings(auth_secret_key="custom-key")
        with patch("src.api.deps.get_settings", return_value=s):
            ...
    """
    defaults = {
        "get_auth_users": MagicMock(return_value={"testuser": "testpass", "admin": "admin123"}),
        "auth_secret_key": "unit-test-secret-key-that-is-long-enough-for-hs256",
        "auth_token_expire_minutes": 60,
    }
    defaults.update(overrides)
    settings = MagicMock()
    for key, value in defaults.items():
        setattr(settings, key, value)
    return settings


# ============== Mock Agent 工厂 ==============


def make_mock_agent(*, run_agui_fn=None):
    """创建统一的 mock Agent 实例（用于 AgentService 测试）。

    用法::

        agent = make_mock_agent(run_agui_fn=my_async_gen)
        service.agent = agent
    """
    agent = MagicMock()
    agent.messages = []
    agent.add_user_message = MagicMock()
    agent._pending_interrupt = None
    if run_agui_fn is not None:
        agent.run_agui = run_agui_fn
    return agent


# ============== FakeExecution 工厂 ==============


def make_fake_execution(*, stdout_text: str = "", exit_code: int = 0,
                        error: str | None = None):
    """创建统一的 mock 命令执行结果，用于沙箱命令测试。

    用法::

        exe = make_fake_execution(stdout_text="hello", exit_code=0)
        sandbox.commands.run = AsyncMock(return_value=exe)
    """
    line = MagicMock()
    line.text = stdout_text
    execution = MagicMock()
    execution.exit_code = exit_code
    execution.logs.stdout = [line]
    if error is not None:
        execution.error = error
    return execution


# ============== AGUI 工具调用事件工厂 ==============


def make_tool_call_agui_events(*, tool_name="write_file", args_deltas=None,
                                thread_id="session-test"):
    """创建一组标准的工具调用 AG-UI 事件序列，用于 chat_agui 集成测试。

    返回一个 async generator factory（接受 **kwargs 含 run_id）。

    参数:
        tool_name: 工具名
        args_deltas: ToolCallArgsEvent 的 delta 列表（支持多段拼接）
        thread_id: RunFinishedEvent 的 threadId

    用法::

        run_agui_fn = make_tool_call_agui_events(
            tool_name="write_file",
            args_deltas=['{"path":"USER.md","content":"x"}'],
        )
        agent = make_mock_agent(run_agui_fn=run_agui_fn)
    """
    from src.agent.schema.agui_events import (
        TextMessageEndEvent,
        StepFinishedEvent,
        RunFinishedEvent,
        ToolCallStartEvent,
        ToolCallArgsEvent,
        ToolCallEndEvent,
        ToolCallResultEvent,
    )

    if args_deltas is None:
        args_deltas = ['{}']

    async def _run_agui(**kwargs):
        yield ToolCallStartEvent(toolCallId="tc1", toolCallName=tool_name)
        for delta in args_deltas:
            yield ToolCallArgsEvent(toolCallId="tc1", delta=delta)
        yield ToolCallEndEvent(toolCallId="tc1")
        yield ToolCallResultEvent(messageId="m1", toolCallId="tc1", content="ok")
        yield TextMessageEndEvent(messageId="m2")
        yield StepFinishedEvent(stepName="step-1")
        yield RunFinishedEvent(threadId=thread_id, runId=kwargs["run_id"], outcome="success")

    return _run_agui
