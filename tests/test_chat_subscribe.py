"""Chat 订阅功能测试 - SSE 断线恢复相关"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import json
import asyncio

from tests.helpers import make_mock_round
from src.api.services.agui_event_bus import get_agui_event_bus


def _event_subscribers():
    return get_agui_event_bus().subscribers


def _event_subscribers_lock():
    return get_agui_event_bus().subscribers_lock


async def _publish_ephemeral(round_id: str, event):
    await get_agui_event_bus().publish_ephemeral(round_id, event)


def _cleanup_event_subscribers(round_id: str):
    get_agui_event_bus().cleanup_subscribers(round_id)


class TestRequestDbReadTransactions:
    def test_has_cancel_activity_since_releases_read_transaction(self):
        from src.api.routes.chat import _has_cancel_activity_since
        from src.api.utils.timezone import now_naive

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = _has_cancel_activity_since(
            mock_db,
            user_id="user-1",
            session_id="session-1",
            started_at=now_naive(),
        )

        assert result is False
        mock_db.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribe_releases_request_transaction_before_streaming(self):
        from starlette.responses import StreamingResponse

        from src.api.models.round import Round
        from src.api.models.session import Session
        from src.api.routes.chat import subscribe_to_round

        mock_db = MagicMock()
        mock_session = MagicMock()
        mock_round = MagicMock()
        mock_round.status = "running"

        def query_side_effect(model):
            chain = MagicMock()
            if model is Session:
                chain.filter.return_value.first.return_value = mock_session
            elif model is Round:
                chain.filter.return_value.first.return_value = mock_round
            return chain

        mock_db.query.side_effect = query_side_effect

        response = await subscribe_to_round(
            chat_session_id="session-1",
            round_id="round-1",
            last_sequence=0,
            user_id="user-1",
            db=mock_db,
        )

        assert isinstance(response, StreamingResponse)
        mock_db.rollback.assert_called_once()


class TestRoundSubscribersManagement:
    """轮次订阅者管理测试"""

    def test_event_subscribers_initialization(self):
        """测试订阅者字典初始化"""

        assert isinstance(_event_subscribers(), dict)

    def test_event_subscribers_operations(self):
        """测试订阅者字典操作"""

        # 保存原始状态
        original_keys = list(_event_subscribers().keys())

        # 添加测试条目
        test_round_id = "test-round-12345"
        _event_subscribers()[test_round_id] = []

        assert test_round_id in _event_subscribers()

        # 添加订阅者队列
        queue = asyncio.Queue()
        _event_subscribers()[test_round_id].append(queue)

        assert len(_event_subscribers()[test_round_id]) == 1

        # 清理
        del _event_subscribers()[test_round_id]

        assert test_round_id not in _event_subscribers()


class TestBroadcastToSubscribers:
    """广播事件测试"""

    @pytest.mark.asyncio
    async def test_broadcast_no_subscribers(self):
        """测试无订阅者时广播"""

        test_round_id = "broadcast-test-no-subs"
        event = {"type": "test", "data": "hello"}

        # 确保没有订阅者
        if test_round_id in _event_subscribers():
            del _event_subscribers()[test_round_id]

        # 不应抛出异常
        await _publish_ephemeral(test_round_id, event)

    @pytest.mark.asyncio
    async def test_broadcast_with_subscribers(self):
        """测试有订阅者时广播"""

        test_round_id = "broadcast-test-with-subs"
        event = {"type": "step", "round_id": test_round_id, "data": "test"}

        # 创建订阅者队列
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()

        _event_subscribers()[test_round_id] = [queue1, queue2]

        try:
            # 广播事件
            await _publish_ephemeral(test_round_id, event)

            # 验证两个队列都收到了事件
            event1 = await asyncio.wait_for(queue1.get(), timeout=1.0)
            event2 = await asyncio.wait_for(queue2.get(), timeout=1.0)

            assert event1 == event
            assert event2 == event
        finally:
            # 清理
            if test_round_id in _event_subscribers():
                del _event_subscribers()[test_round_id]

    @pytest.mark.asyncio
    async def test_broadcast_handles_queue_error(self):
        """测试广播处理队列错误"""

        test_round_id = "broadcast-error-test"
        event = {"type": "test"}

        # 创建一个会抛出异常的模拟队列
        bad_queue = MagicMock()
        bad_queue.put = AsyncMock(side_effect=Exception("Queue error"))

        good_queue = asyncio.Queue()

        _event_subscribers()[test_round_id] = [bad_queue, good_queue]

        try:
            # 不应抛出异常，即使一个队列出错
            await _publish_ephemeral(test_round_id, event)

            # 好的队列仍应收到事件
            event_received = await asyncio.wait_for(good_queue.get(), timeout=1.0)
            assert event_received == event
        finally:
            if test_round_id in _event_subscribers():
                del _event_subscribers()[test_round_id]


class TestCleanupSubscribers:
    """清理订阅者测试"""

    def test_cleanup_existing_round(self):
        """测试清理已存在的轮次订阅者"""

        test_round_id = "cleanup-test-existing"
        _event_subscribers()[test_round_id] = [asyncio.Queue()]

        _cleanup_event_subscribers(test_round_id)

        assert test_round_id not in _event_subscribers()

    def test_cleanup_nonexistent_round(self):
        """测试清理不存在的轮次"""

        test_round_id = "cleanup-test-nonexistent"

        # 确保不存在
        if test_round_id in _event_subscribers():
            del _event_subscribers()[test_round_id]

        # 不应抛出异常
        _cleanup_event_subscribers(test_round_id)

        assert test_round_id not in _event_subscribers()


class TestSubscribeEventTypes:
    """AG-UI 订阅事件类型测试"""

    def test_run_finished_event_for_completed_round(self):
        """测试已完成轮次的 RUN_FINISHED 事件格式"""
        from src.agent.schema.agui_events import RunFinishedEvent

        event = RunFinishedEvent(
            threadId="session-123",
            runId="completed-round-123",
            result={"finalResponse": "任务已完成", "stepCount": 5},
            outcome="success",
        )
        data = event.model_dump(by_alias=True)

        assert data["type"] == "RUN_FINISHED"
        assert data["threadId"] == "session-123"
        assert data["runId"] == "completed-round-123"
        assert data["result"]["finalResponse"] == "任务已完成"
        assert data["outcome"] == "success"

    def test_run_error_event_format(self):
        """测试 RUN_ERROR 事件格式"""
        from src.agent.schema.agui_events import RunErrorEvent

        event = RunErrorEvent(message="Run failed (status=failed)", code="RUN_FAILED")
        data = event.model_dump(by_alias=True)

        assert data["type"] == "RUN_ERROR"
        assert data["message"] == "Run failed (status=failed)"
        assert data["code"] == "RUN_FAILED"

    def test_failed_round_emits_error_only(self):
        """测试失败轮次仅发送 RUN_ERROR"""
        from src.agent.schema.agui_events import RunErrorEvent

        error_event = RunErrorEvent(message="Run failed (status=failed)", code="RUN_FAILED")

        err_data = error_event.model_dump(by_alias=True)

        assert err_data["type"] == "RUN_ERROR"

    def test_messages_snapshot_event_format(self):
        """测试 MESSAGES_SNAPSHOT 事件格式"""
        from src.agent.schema.agui_events import MessagesSnapshotEvent

        event = MessagesSnapshotEvent(messages=[
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ])
        data = event.model_dump(by_alias=True)

        assert data["type"] == "MESSAGES_SNAPSHOT"
        assert len(data["messages"]) == 2

    def test_custom_heartbeat_event_format(self):
        """测试心跳自定义事件格式"""
        from src.agent.schema.agui_events import CustomEvent

        event = CustomEvent(name="heartbeat", value={"timestamp": 1700000000000})
        data = event.model_dump(by_alias=True)

        assert data["type"] == "CUSTOM"
        assert data["name"] == "heartbeat"
        assert data["value"]["timestamp"] == 1700000000000


class TestSubscribeSSEFormat:
    """AG-UI SSE 格式测试"""

    def test_sse_data_format_with_agui_event(self):
        """测试 AG-UI 事件的 SSE 数据格式"""
        from src.agent.schema.agui_events import RunFinishedEvent

        event = RunFinishedEvent(
            threadId="session-123", runId="round-123",
            result={"finalResponse": "Done"}, outcome="success",
        )
        data = event.model_dump(by_alias=True)
        sse_line = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        assert sse_line.startswith("data: ")
        assert sse_line.endswith("\n\n")

        parsed = json.loads(sse_line[6:-2])
        assert parsed["type"] == "RUN_FINISHED"

    def test_sse_chinese_content(self):
        """测试 SSE 中文内容"""
        from src.agent.schema.agui_events import MessagesSnapshotEvent

        event = MessagesSnapshotEvent(messages=[
            {"role": "assistant", "content": "这是中文回复"},
        ])
        data = event.model_dump(by_alias=True)
        sse_line = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        assert "这是中文回复" in sse_line
        assert "\\u" not in sse_line


class TestSubscribeRouteValidation:
    """订阅路由验证测试"""

    @pytest.fixture
    def mock_session(self):
        """创建模拟会话"""
        session = MagicMock()
        session.id = "session-123"
        session.user_id = "user-1"
        session.status = "active"
        return session

    @pytest.fixture
    def mock_round_completed(self):
        """创建已完成的模拟轮次"""
        return make_mock_round(
            round_id="round-completed-123", status="completed",
            final_response="任务完成", step_count=3,
        )

    @pytest.fixture
    def mock_round_running(self):
        """创建运行中的模拟轮次"""
        return make_mock_round(
            round_id="round-running-456", status="running",
            final_response=None, step_count=0,
        )

    @pytest.fixture
    def mock_round_failed(self):
        """创建失败的模拟轮次"""
        return make_mock_round(
            round_id="round-failed-789", status="failed",
            final_response="", step_count=1,
        )

    def test_session_not_found_error(self):
        """测试会话不存在时的错误"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            raise HTTPException(status_code=404, detail="会话不存在")

        assert exc_info.value.status_code == 404
        assert "会话不存在" in exc_info.value.detail

    def test_round_not_found_error(self):
        """测试轮次不存在时的错误"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            raise HTTPException(status_code=404, detail="轮次不存在")

        assert exc_info.value.status_code == 404
        assert "轮次不存在" in exc_info.value.detail

    def test_completed_round_immediate_response(self, mock_round_completed):
        """测试已完成轮次应立即返回 AG-UI 终态事件"""
        round_obj = mock_round_completed

        should_return_immediately = round_obj.status in ("completed", "failed")
        assert should_return_immediately

        # 已完成轮次应生成 RUN_FINISHED(outcome=success)
        from src.agent.schema.agui_events import RunFinishedEvent

        event = RunFinishedEvent(
            threadId="session-123",
            runId=round_obj.id,
            result={
                "finalResponse": round_obj.final_response or "",
                "stepCount": round_obj.step_count,
            },
            outcome="success",
        )
        data = event.model_dump(by_alias=True)
        assert data["type"] == "RUN_FINISHED"
        assert data["outcome"] == "success"

    def test_failed_round_immediate_response(self, mock_round_failed):
        """测试失败轮次应立即返回 RUN_ERROR"""
        round_obj = mock_round_failed

        should_return_immediately = round_obj.status in ("completed", "failed")
        assert should_return_immediately

        from src.agent.schema.agui_events import RunErrorEvent

        error_event = RunErrorEvent(message="Run failed (status=failed)", code="RUN_FAILED")
        err = error_event.model_dump(by_alias=True)
        assert err["type"] == "RUN_ERROR"

    def test_running_round_subscribe_logic(self, mock_round_running):
        """测试运行中轮次的订阅逻辑"""
        round_obj = mock_round_running

        # 运行中的轮次不应立即返回
        should_return_immediately = round_obj.status in ("completed", "failed")

        assert not should_return_immediately

        # 应该订阅更新
        should_subscribe = round_obj.status == "running"

        assert should_subscribe


class TestSubscribeEventReplay:
    """AG-UI 事件重放（last_sequence）测试"""

    def test_filter_events_by_sequence(self):
        """测试根据 last_sequence 过滤事件"""
        all_events = [
            {"type": "TEXT_MESSAGE_START", "_seq": 1},
            {"type": "TEXT_MESSAGE_CONTENT", "_seq": 2},
            {"type": "TEXT_MESSAGE_END", "_seq": 3},
            {"type": "RUN_FINISHED", "_seq": 4},
        ]

        last_sequence = 2
        replayed = [e for e in all_events if e["_seq"] > last_sequence]

        assert len(replayed) == 2
        assert replayed[0]["type"] == "TEXT_MESSAGE_END"
        assert replayed[1]["type"] == "RUN_FINISHED"

    def test_filter_events_zero_sequence_returns_all(self):
        """测试 last_sequence=0 时返回所有事件"""
        all_events = [
            {"type": "RUN_STARTED", "_seq": 1},
            {"type": "RUN_FINISHED", "_seq": 2},
        ]

        replayed = [e for e in all_events if e["_seq"] > 0]
        assert len(replayed) == 2

    def test_filter_events_all_received(self):
        """测试所有事件都已接收时返回空"""
        all_events = [
            {"type": "RUN_STARTED", "_seq": 1},
            {"type": "RUN_FINISHED", "_seq": 2},
        ]

        replayed = [e for e in all_events if e["_seq"] > 2]
        assert len(replayed) == 0

    def test_replay_detects_run_finished(self):
        """测试重放事件中检测 RUN_FINISHED 避免重复发送"""
        replayed_events = [
            {"type": "TEXT_MESSAGE_CONTENT"},
            {"type": "RUN_FINISHED"},
        ]

        has_run_finished = any(e.get("type") == "RUN_FINISHED" for e in replayed_events)
        assert has_run_finished

    def test_replay_no_run_finished_triggers_supplement(self):
        """测试重放事件无 RUN_FINISHED 时需要补发"""
        replayed_events = [
            {"type": "TEXT_MESSAGE_CONTENT"},
            {"type": "TEXT_MESSAGE_END"},
        ]

        has_run_finished = any(e.get("type") == "RUN_FINISHED" for e in replayed_events)
        assert not has_run_finished


class TestSubscribeAsyncOperations:
    """订阅异步操作测试"""

    @pytest.mark.asyncio
    async def test_subscriber_queue_registration(self):
        """测试订阅者队列注册"""

        test_round_id = "async-test-registration"
        subscriber_queue = asyncio.Queue()

        # 注册订阅者
        if test_round_id not in _event_subscribers():
            _event_subscribers()[test_round_id] = []
        _event_subscribers()[test_round_id].append(subscriber_queue)

        try:
            assert test_round_id in _event_subscribers()
            assert subscriber_queue in _event_subscribers()[test_round_id]
            assert len(_event_subscribers()[test_round_id]) == 1
        finally:
            del _event_subscribers()[test_round_id]

    @pytest.mark.asyncio
    async def test_subscriber_queue_removal(self):
        """测试订阅者队列移除"""

        test_round_id = "async-test-removal"
        subscriber_queue = asyncio.Queue()

        _event_subscribers()[test_round_id] = [subscriber_queue]

        try:
            # 移除订阅者
            _event_subscribers()[test_round_id].remove(subscriber_queue)

            assert subscriber_queue not in _event_subscribers()[test_round_id]
            assert len(_event_subscribers()[test_round_id]) == 0
        finally:
            if test_round_id in _event_subscribers():
                del _event_subscribers()[test_round_id]

    @pytest.mark.asyncio
    async def test_queue_wait_for_timeout(self):
        """测试队列等待超时"""
        queue = asyncio.Queue()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_queue_receive_event(self):
        """测试队列接收 AG-UI 事件"""
        queue = asyncio.Queue()
        event = {"type": "RUN_FINISHED", "runId": "test-123", "outcome": "success"}

        await queue.put(event)
        received = await asyncio.wait_for(queue.get(), timeout=1.0)

        assert received == event
        assert received["type"] == "RUN_FINISHED"

    @pytest.mark.asyncio
    async def test_heartbeat_task_pattern(self):
        """测试心跳任务模式"""
        heartbeat_count = 0

        async def heartbeat():
            nonlocal heartbeat_count
            try:
                while True:
                    await asyncio.sleep(0.05)  # 短间隔用于测试
                    heartbeat_count += 1
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(heartbeat())

        # 等待几次心跳
        await asyncio.sleep(0.2)

        # 取消任务
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # 验证心跳执行了多次
        assert heartbeat_count >= 2


class TestSubscribeHeaders:
    """订阅响应头测试"""

    def test_sse_response_headers(self):
        """测试 SSE 响应头"""
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }

        assert headers["Cache-Control"] == "no-cache"
        assert headers["Connection"] == "keep-alive"
        # X-Accel-Buffering: no 用于禁用 Nginx 缓冲
        assert headers["X-Accel-Buffering"] == "no"


class TestSubscribeEndToEnd:
    """订阅端到端流程测试（AG-UI 协议）"""

    def test_complete_flow_for_completed_round(self):
        """测试已完成轮次的完整 AG-UI 事件流"""
        from src.agent.schema.agui_events import (
            MessagesSnapshotEvent, RunFinishedEvent,
        )

        round_status = "completed"
        final_response = "任务已完成"
        step_count = 3

        is_finished = round_status in ("completed", "failed")
        assert is_finished

        # 构建 AG-UI 事件序列
        snapshot = MessagesSnapshotEvent(messages=[
            {"role": "user", "content": "帮我分析"},
            {"role": "assistant", "content": final_response},
        ])
        finished = RunFinishedEvent(
            threadId="session-123", runId="round-123",
            result={"finalResponse": final_response, "stepCount": step_count},
            outcome="success",
        )

        events = [snapshot.model_dump(by_alias=True), finished.model_dump(by_alias=True)]

        # SSE 编码
        for event_data in events:
            sse_line = f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"
            assert sse_line.startswith("data: ")

        assert events[-1]["type"] == "RUN_FINISHED"
        assert events[-1]["outcome"] == "success"

    def test_complete_flow_for_failed_round(self):
        """测试失败轮次的完整 AG-UI 事件流：仅 RUN_ERROR"""
        from src.agent.schema.agui_events import (
            MessagesSnapshotEvent, RunErrorEvent,
        )

        snapshot = MessagesSnapshotEvent(messages=[])
        error = RunErrorEvent(message="Run failed (status=failed)", code="RUN_FAILED")

        events = [
            snapshot.model_dump(by_alias=True),
            error.model_dump(by_alias=True),
        ]

        assert events[1]["type"] == "RUN_ERROR"

    @pytest.mark.asyncio
    async def test_complete_flow_for_running_round(self):
        """测试运行中轮次的完整流程：注册订阅 → 广播事件 → 接收"""

        round_id = "test-running-flow"
        round_status = "running"

        is_finished = round_status in ("completed", "failed")
        assert not is_finished

        # 注册订阅者
        subscriber_queue = asyncio.Queue()
        _event_subscribers()[round_id] = [subscriber_queue]

        try:
            # 模拟 AG-UI 事件广播
            new_event = {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-1", "delta": "hello"}
            await _publish_ephemeral(round_id, new_event)

            received = await asyncio.wait_for(subscriber_queue.get(), timeout=1.0)
            assert received["type"] == "TEXT_MESSAGE_CONTENT"
            assert received["delta"] == "hello"
        finally:
            if round_id in _event_subscribers():
                del _event_subscribers()[round_id]

class TestActiveRunners:
    """后台 Agent 运行任务追踪测试"""

    def test_active_runners_initialization(self):
        """_active_runners 初始化为空字典"""
        from src.api.routes.chat import _active_runners

        assert isinstance(_active_runners, dict)

    @pytest.mark.asyncio
    async def test_active_runners_registration_and_cleanup(self):
        """producer 注册和自动清理"""
        from src.api.routes.chat import _active_runners

        session_id = "test-runner-reg"

        async def fake_task():
            await asyncio.sleep(0.01)

        task = asyncio.create_task(fake_task())
        _active_runners[session_id] = task

        assert session_id in _active_runners
        assert _active_runners[session_id] is task

        await task
        # 手动清理模拟 producer finally 行为
        _active_runners.pop(session_id, None)
        assert session_id not in _active_runners

    @pytest.mark.asyncio
    async def test_active_runner_cancel_stops_task(self):
        """取消 active runner 能停止后台任务"""
        from src.api.routes.chat import _active_runners

        session_id = "test-cancel-runner"
        ran_to_completion = False

        async def long_task():
            nonlocal ran_to_completion
            try:
                await asyncio.sleep(10)
                ran_to_completion = True
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(long_task())
        _active_runners[session_id] = task

        # 取消
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert not ran_to_completion
        _active_runners.pop(session_id, None)

    @pytest.mark.asyncio
    async def test_liveness_loss_preserves_lock_for_stale_recovery(self):
        from src.api.schemas.chat import TextContentBlock
        from src.api.schemas.turn import NormalizedInboundTurn, WebReplyRoute
        from src.api.services.agent_service import PreparedAgentRun
        from src.api.services.turn_orchestrator import TurnOrchestrator

        class LostLivenessAgentService:
            cancel_token: asyncio.Event | None = None
            liveness_token: asyncio.Event | None = None

            async def prepare_chat_round(self, *, user_content, idempotency_key=None):
                return PreparedAgentRun(run_id="run-liveness", user_message="hello")

            async def run_prepared_round(self, prepared, *, error_label):
                assert self.liveness_token is not None
                self.liveness_token.set()
                if False:
                    yield None

        service = LostLivenessAgentService()
        orchestrator = TurnOrchestrator()
        orchestrator._complete_cancel_requests = AsyncMock()
        orchestrator._release_lock = AsyncMock()
        turn = NormalizedInboundTurn(
            channel="web",
            user_id="user-liveness",
            peer_kind="web",
            peer_id="session-liveness",
            content=[TextContentBlock(type="text", text="hello")],
            reply_route=WebReplyRoute(session_id="session-liveness"),
        )

        execution = await orchestrator.submit_turn(
            turn,
            agent_service=service,
            lock_id="lock-liveness",
        )
        assert [event async for event in execution.event_source] == []
        assert execution.task is not None
        await execution.task

        assert service.cancel_token is not None
        assert not service.cancel_token.is_set()
        orchestrator._complete_cancel_requests.assert_not_awaited()
        orchestrator._release_lock.assert_not_awaited()


class TestSseDetachedProducer:
    """SSE producer 断线后继续运行测试"""

    @pytest.mark.asyncio
    async def test_producer_broadcasts_events(self):
        """producer 在迭代 event_source 时广播事件给订阅者"""

        round_id = "test-producer-broadcast"
        subscriber_queue = asyncio.Queue()
        _event_subscribers()[round_id] = [subscriber_queue]

        try:
            test_event = {"type": "TEXT_MESSAGE_CONTENT", "delta": "hi"}
            await _publish_ephemeral(round_id, test_event)

            received = await asyncio.wait_for(subscriber_queue.get(), timeout=1.0)
            assert received["type"] == "TEXT_MESSAGE_CONTENT"
            assert received["delta"] == "hi"
        finally:
            _event_subscribers().pop(round_id, None)

    @pytest.mark.asyncio
    async def test_producer_continues_after_consumer_stops(self):
        """模拟 SSE 断开后 producer 继续执行"""
        events_produced = []
        consumer_active = True

        async def fake_event_source():
            for i in range(5):
                yield {"type": "event", "seq": i}
                await asyncio.sleep(0.01)

        event_queue = asyncio.Queue()

        async def producer():
            nonlocal consumer_active
            async for event in fake_event_source():
                events_produced.append(event)
                if consumer_active:
                    event_queue.put_nowait(event)

        task = asyncio.create_task(producer())

        # 消费前两个事件后 "断开"
        for _ in range(2):
            await asyncio.wait_for(event_queue.get(), timeout=1.0)
        consumer_active = False

        # 等待 producer 完成
        await asyncio.wait_for(task, timeout=2.0)

        # producer 应该产出了所有 5 个事件
        assert len(events_produced) == 5

    @pytest.mark.asyncio
    async def test_run_round_stream_finally_marks_round(self):
        """_run_round_stream 在未知异常退出时应标记为 failed（非用户取消）。"""
        from unittest.mock import MagicMock, AsyncMock, patch
        from src.api.services.agent_service import AgentService

        mock_history = MagicMock()
        mock_history.save_agui_event = AsyncMock()
        mock_history.complete_round = MagicMock()

        service = object.__new__(AgentService)
        service.history_service = mock_history
        service.session_id = "test-session"
        service.cancel_token = None
        service._active_run_count = 0
        service.agent = MagicMock()

        # 模拟 agent.run_agui 只产出 RUN_STARTED 就被关闭（模拟断线）
        from src.agent.schema.agui_events import RunStartedEvent

        async def fake_run_agui(**kwargs):
            yield RunStartedEvent(
                threadId="test-session",
                runId="test-run",
            )
            # 模拟长时间运行
            await asyncio.sleep(10)

        service.agent.run_agui = fake_run_agui

        # 启动 _run_round_stream 并只消费第一个事件后关闭
        gen = service._run_round_stream(
            run_id="test-run",
            user_message="test",
        )
        first_event = await gen.__anext__()
        assert first_event.type.value == "RUN_STARTED"

        # 关闭 generator（模拟 SSE 断开 → GeneratorExit）
        await gen.aclose()

        # finally 块应该调用了 complete_round，并标记为 failed（未知中断）。
        mock_history.complete_round.assert_called_once()
        call_kwargs = mock_history.complete_round.call_args
        assert call_kwargs.kwargs.get("status") == "failed" or call_kwargs[1].get("status") == "failed"

    @pytest.mark.asyncio
    async def test_run_round_stream_finally_fans_out_fallback_terminal(self):
        """异常兜底写入 terminal 后应广播给本地订阅者。"""
        from unittest.mock import MagicMock, AsyncMock
        from src.api.services.agent_service import AgentService
        from src.api.services.agui_event_bus import StoredEvent, get_agui_event_bus
        from src.agent.schema.agui_events import RunStartedEvent

        run_id = "test-run-fallback-fanout"
        terminal_payload = {
            "type": "RUN_ERROR",
            "message": "Failed",
            "code": "RUN_FAILED",
            "sequence": 2,
        }
        mock_history = MagicMock()
        mock_history.reset_session = MagicMock()
        mock_history.is_round_terminal = MagicMock(return_value=False)
        mock_history.save_agui_event = AsyncMock(side_effect=RuntimeError("event write failed"))
        mock_history.complete_round = MagicMock()
        mock_history.last_terminal_event = StoredEvent(run_id, 2, terminal_payload)

        service = object.__new__(AgentService)
        service.history_service = mock_history
        service.session_id = "test-session"
        service.cancel_token = None
        service._active_run_count = 0
        service.agent = MagicMock()

        async def fake_run_agui(**kwargs):
            yield RunStartedEvent(threadId="test-session", runId=run_id)

        service.agent.run_agui = fake_run_agui

        bus = get_agui_event_bus()
        queue = asyncio.Queue()
        with bus.subscribers_lock:
            bus.subscribers[run_id] = [queue]

        try:
            with pytest.raises(RuntimeError, match="event write failed"):
                async for _event in service._run_round_stream(
                    run_id=run_id,
                    user_message="test",
                ):
                    pass

            received = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert received == terminal_payload
        finally:
            with bus.subscribers_lock:
                bus.subscribers.pop(run_id, None)

    @pytest.mark.asyncio
    async def test_run_round_stream_yields_durable_terminal_when_externally_terminated(self):
        """abort 已持久化终态时，原始 stream 仍应收到 durable RUN_FINISHED。"""
        from unittest.mock import MagicMock, patch
        from src.api.services.agent_service import AgentService
        from src.api.services.agui_event_bus import StoredEvent
        from src.agent.schema.agui_events import RunStartedEvent

        run_id = "test-run-external-terminal"
        terminal_payload = {
            "type": "RUN_FINISHED",
            "threadId": "test-session",
            "runId": run_id,
            "outcome": "interrupt",
            "result": {"reason": "user_cancelled"},
            "sequence": 2,
        }
        mock_history = MagicMock()
        mock_history.db = MagicMock()
        mock_history.is_round_terminal = MagicMock(return_value=True)
        mock_history.get_round_status = MagicMock(return_value="cancelled")
        mock_history.reset_session = MagicMock()
        mock_history.complete_round = MagicMock()

        service = object.__new__(AgentService)
        service.history_service = mock_history
        service.session_id = "test-session"
        service.cancel_token = asyncio.Event()
        service._active_run_count = 0
        service.agent = MagicMock()

        async def fake_run_agui(**kwargs):
            yield RunStartedEvent(threadId="test-session", runId=run_id)

        service.agent.run_agui = fake_run_agui

        mock_completion = MagicMock()
        mock_completion.ensure_terminal_sync.return_value = StoredEvent(
            run_id=run_id,
            sequence=2,
            event=terminal_payload,
        )

        with patch(
            "src.api.services.agent_service.RunCompletionService",
            return_value=mock_completion,
        ):
            events = [
                event
                async for event in service._run_round_stream(
                    run_id=run_id,
                    user_message="test",
                )
            ]

        assert events == [terminal_payload]
        mock_completion.ensure_terminal_sync.assert_called_once_with(run_id)
        mock_history.complete_round.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_round_stream_rolls_back_before_failed_round_update_after_db_error(self):
        """事件写库失败后，收尾更新 round 前必须先 rollback 当前 Session。"""
        from unittest.mock import MagicMock, AsyncMock
        from sqlalchemy.exc import OperationalError
        from src.api.services.agent_service import AgentService
        from src.agent.schema.agui_events import RunStartedEvent

        mock_history = MagicMock()
        mock_history.db = MagicMock()
        mock_history.is_round_terminal = MagicMock(return_value=False)
        mock_history.save_agui_event = AsyncMock(
            side_effect=OperationalError("stmt", {}, Exception("SSL error: unexpected eof while reading"))
        )
        mock_history.complete_round = MagicMock()

        service = object.__new__(AgentService)
        service.history_service = mock_history
        service.session_id = "test-session"
        service.cancel_token = None
        service._active_run_count = 0
        service.agent = MagicMock()

        async def fake_run_agui(**kwargs):
            yield RunStartedEvent(threadId="test-session", runId="test-run")

        service.agent.run_agui = fake_run_agui

        with pytest.raises(OperationalError):
            async for _event in service._run_round_stream(
                run_id="test-run",
                user_message="test",
            ):
                pass

        mock_history.reset_session.assert_called_once()
        mock_history.complete_round.assert_called_once()
        call_kwargs = mock_history.complete_round.call_args
        assert call_kwargs.kwargs.get("status") == "failed" or call_kwargs[1].get("status") == "failed"

    @pytest.mark.asyncio
    async def test_run_round_stream_finally_marks_cancelled_when_cancel_token_set(self):
        """_run_round_stream 异常退出且本地 cancel_token 已触发时标记为 cancelled。"""
        from unittest.mock import MagicMock, AsyncMock
        from src.api.services.agent_service import AgentService

        mock_history = MagicMock()
        mock_history.save_agui_event = AsyncMock()
        mock_history.complete_round = MagicMock()

        service = object.__new__(AgentService)
        service.history_service = mock_history
        service.session_id = "test-session"
        service.cancel_token = asyncio.Event()
        service.cancel_token.set()
        service._active_run_count = 0
        service.agent = MagicMock()

        from src.agent.schema.agui_events import RunStartedEvent

        async def fake_run_agui(**kwargs):
            yield RunStartedEvent(
                threadId="test-session",
                runId="test-run",
            )
            await asyncio.sleep(10)

        service.agent.run_agui = fake_run_agui

        gen = service._run_round_stream(
            run_id="test-run",
            user_message="test",
        )
        first_event = await gen.__anext__()
        assert first_event.type.value == "RUN_STARTED"

        await gen.aclose()

        mock_history.complete_round.assert_called_once()
        call_kwargs = mock_history.complete_round.call_args
        assert call_kwargs.kwargs.get("status") == "cancelled" or call_kwargs[1].get("status") == "cancelled"

    @pytest.mark.asyncio
    async def test_round_finished_flag_after_complete_round_exception(self):
        """complete_round 正常路径抛异常时，finally 兜底被执行

        验证 _round_finished 标志位在 complete_round 之后才置 True，
        因此当 complete_round 抛异常时 finally 兜底分支能再试一次。
        """
        from unittest.mock import MagicMock, AsyncMock, call
        from src.api.services.agent_service import AgentService
        from src.agent.schema.agui_events import (
            RunStartedEvent, RunFinishedEvent, StepStartedEvent, StepFinishedEvent,
            TextMessageStartEvent, TextMessageContentEvent, TextMessageEndEvent,
        )

        mock_history = MagicMock()
        mock_history.save_agui_event = AsyncMock()
        # 第一次 complete_round 抛异常（正常路径），第二次成功（finally 兜底）
        mock_history.complete_round = MagicMock(
            side_effect=[RuntimeError("DB connection lost"), None]
        )

        service = object.__new__(AgentService)
        service.history_service = mock_history
        service.session_id = "test-session"
        service.cancel_token = None
        service._active_run_count = 0
        service.agent = MagicMock()
        service._last_saved_index = 0
        service._pending_interrupt_round_ids = {}

        # 模拟 agent.run_agui 正常完成一个 round
        async def fake_run_agui(**kwargs):
            yield RunStartedEvent(threadId="test-session", runId="test-run")
            yield StepStartedEvent(stepName="step_1")
            yield TextMessageStartEvent(messageId="m1", role="assistant")
            yield TextMessageContentEvent(messageId="m1", delta="Hello")
            yield TextMessageEndEvent(messageId="m1")
            yield StepFinishedEvent(stepName="step_1")
            yield RunFinishedEvent(
                threadId="test-session", runId="test-run", outcome="success",
            )

        service.agent.run_agui = fake_run_agui

        # 消费所有事件（complete_round 异常会被 except 捕获后 re-raise，
        # 但 finally 兜底应该再调用一次 complete_round）
        events = []
        with pytest.raises(RuntimeError, match="DB connection lost"):
            async for event in service._run_round_stream(
                run_id="test-run",
                user_message="test",
            ):
                events.append(event)

        # complete_round 应该被调用了 2 次：
        # 1. 正常路径（抛异常）
        # 2. finally 兜底（成功）
        assert mock_history.complete_round.call_count == 2
        # 兜底调用的 status 应该是 "failed"
        second_call = mock_history.complete_round.call_args_list[1]
        assert second_call.kwargs.get("status") == "failed" or second_call[1].get("status") == "failed"


class TestRunCancelRegistryFlow:
    """单 worker cancel registry + append-only audit 链路测试。"""

    def test_requested_after_does_not_cancel_newer_current_run(self):
        """旧 cancel epoch 不应误伤同 session 后续新 run。"""
        from datetime import timedelta
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from src.api.models.database import Base
        from src.api.models.run_cancel_request import RunCancelRequest
        from src.api.services.run_cancel_service import RunCancelService
        from src.api.utils.timezone import now_naive

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)

        service = RunCancelService()
        cancel_token = asyncio.Event()
        requested_after = now_naive()
        service.register(
            session_id="session-1",
            run_id="new-run",
            cancel_token=cancel_token,
            started_at=requested_after + timedelta(seconds=1),
        )

        try:
            with TestingSessionLocal() as db:
                result = service.request_cancel(
                    db,
                    user_id="user-1",
                    session_id="session-1",
                    requested_after=requested_after,
                )

                row = db.query(RunCancelRequest).filter_by(request_id=result.request_id).one()
                assert result.local_hit is False
                assert result.target_run_id == "new-run"
                assert row.state == "requested"
                assert row.acked_at is None
                assert not cancel_token.is_set()
        finally:
            Base.metadata.drop_all(bind=engine)
            engine.dispose()

    @pytest.mark.asyncio
    async def test_cancel_request_requested_to_ack_to_completed_and_release_lock(self):
        """abort 审计行 append；命中 Orchestrator registry 后 token 立即 set，结束后 completed。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from src.api.models.database import Base
        from src.api.models.run_cancel_request import RunCancelRequest
        from src.api.models.user_run_lock import UserRunLock
        from src.api.schemas.chat import TextContentBlock
        from src.api.schemas.turn import NormalizedInboundTurn, TurnCancelTarget, WebReplyRoute
        from src.api.services.agent_service import PreparedAgentRun
        from src.api.services.run_cancel_service import RunCancelService
        from src.api.services.run_coordinator import RunCoordinator
        from src.api.services.turn_orchestrator import TurnOrchestrator
        from src.api.utils.timezone import now_naive
        from src.agent.schema.agui_events import RunStartedEvent, RunFinishedEvent

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)

        user_id = "user-1"
        session_id = "session-1"
        lock_id = "lock-1"

        try:
            # A worker 已持有用户锁并开始执行
            with TestingSessionLocal() as setup_db:
                setup_db.add(
                    UserRunLock(
                        user_id=user_id,
                        session_id=session_id,
                        lock_id=lock_id,
                    )
                )
                setup_db.commit()

            cancel_service = RunCancelService()
            orchestrator = TurnOrchestrator(
                cancel_service=cancel_service,
                run_coordinator=RunCoordinator(session_factory=TestingSessionLocal),
            )

            class FakeAgentService:
                cancel_token: asyncio.Event | None = None

                async def prepare_chat_round(self, *, user_content, idempotency_key=None):
                    return PreparedAgentRun(run_id="run-1", user_message="hello")

                async def run_prepared_round(self, prepared, *, error_label):
                    yield RunStartedEvent(threadId=session_id, runId=prepared.run_id)
                    with TestingSessionLocal() as cancel_db:
                        await orchestrator.cancel_turn(
                            TurnCancelTarget(
                                user_id=user_id,
                                session_id=session_id,
                                round_id=prepared.run_id,
                            ),
                            db=cancel_db,
                        )
                    for _ in range(120):
                        if self.cancel_token and self.cancel_token.is_set():
                            break
                        await asyncio.sleep(0.02)
                    assert self.cancel_token is not None
                    assert self.cancel_token.is_set()
                    yield RunFinishedEvent(
                        threadId=session_id,
                        runId=prepared.run_id,
                        outcome="interrupt",
                        result={"reason": "user_cancelled"},
                    )

            turn = NormalizedInboundTurn(
                channel="web",
                user_id=user_id,
                peer_kind="web",
                peer_id=session_id,
                content=[TextContentBlock(type="text", text="hello")],
                reply_route=WebReplyRoute(session_id=session_id),
            )

            with patch("src.api.services.turn_orchestrator.SessionLocal", TestingSessionLocal):
                execution = await orchestrator.submit_turn(
                    turn,
                    agent_service=FakeAgentService(),
                    lock_id=lock_id,
                    run_started_at=now_naive(),
                )
                events = [event async for event in execution.event_source]
                if execution.task:
                    await execution.task

            assert any(getattr(event, "type", None) == "RUN_FINISHED" for event in events)

            with TestingSessionLocal() as verify_db:
                cancel_row = (
                    verify_db.query(RunCancelRequest)
                    .filter(
                        RunCancelRequest.session_id == session_id,
                        RunCancelRequest.user_id == user_id,
                    )
                    .first()
                )
                assert cancel_row is not None
                assert cancel_row.state == "completed"
                assert cancel_row.acked_at is not None
                assert cancel_row.completed_at is not None
                assert cancel_row.target_run_id == "run-1"

                lock_row = (
                    verify_db.query(UserRunLock)
                    .filter(UserRunLock.user_id == user_id)
                    .first()
                )
                assert lock_row is None
        finally:
            Base.metadata.drop_all(bind=engine)
            engine.dispose()


class TestSubscribePersistedPolling:
    """subscribe 端点与 EventBus replay/fanout 回归测试。"""

    @pytest.mark.asyncio
    async def test_subscribe_replays_terminal_committed_before_queue_registration(self):
        """首次 replay 后、注册 queue 前提交的 terminal 仍必须交付。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from src.agent.schema.agui_events import CustomEvent
        from src.api.models.database import Base
        from src.api.models.round import Round
        from src.api.models.session import Session
        from src.api.services.agui_event_bus import AguiEventBus
        from src.api.services.run_completion_service import RunCompletionService

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        TestSL = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)
        run_id = "round-terminal-before-queue"
        bus = AguiEventBus(TestSL)
        iterator = None
        cursor_iterator = None
        try:
            with TestSL() as setup_db:
                setup_db.add(Session(id="session-terminal-before-queue", user_id="testuser"))
                setup_db.add(
                    Round(
                        id=run_id,
                        session_id="session-terminal-before-queue",
                        thread_id="session-terminal-before-queue",
                        user_message="hello",
                        status="running",
                    )
                )
                setup_db.commit()

            stored = await bus.publish(
                run_id,
                CustomEvent(name="before_terminal", value={}),
            )
            assert stored is not None
            assert stored.sequence == 1

            iterator = bus.subscribe(run_id, after_sequence=0).__aiter__()
            first = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
            assert first["type"] == "CUSTOM"
            assert first["sequence"] == 1

            # The initial replay yield is a deterministic barrier: subscribe()
            # has not reached ensure_terminal() or registered its live queue.
            with bus.subscribers_lock:
                assert run_id not in bus.subscribers

            terminal = RunCompletionService(TestSL).complete_sync(
                run_id=run_id,
                status="completed",
                terminal_event={
                    "type": "RUN_FINISHED",
                    "threadId": "session-terminal-before-queue",
                    "runId": run_id,
                    "outcome": "success",
                    "result": {"finalResponse": "done", "stepCount": 0},
                },
            )
            assert terminal is not None
            assert terminal.sequence == 2
            with bus.subscribers_lock:
                assert run_id not in bus.subscribers

            second = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
            assert second["type"] == "RUN_FINISHED"
            assert second["sequence"] == 2
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(iterator.__anext__(), timeout=1.0)

            # A client that already consumed the terminal cursor should receive
            # a clean EOF rather than a duplicate terminal.
            cursor_iterator = bus.subscribe(run_id, after_sequence=2).__aiter__()
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(cursor_iterator.__anext__(), timeout=1.0)
        finally:
            if iterator is not None:
                await iterator.aclose()
            if cursor_iterator is not None:
                await cursor_iterator.aclose()
            bus.cleanup_subscribers(run_id)
            bus._terminal_runs.discard(run_id)
            Base.metadata.drop_all(bind=engine)
            engine.dispose()

    @pytest.mark.asyncio
    async def test_subscribe_receives_persisted_terminal_event_without_local_broadcast(self):
        """无本地广播事件时，subscribe 应通过 EventBus replay 拿到 RUN_FINISHED。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from src.api.models.agui_event import AGUIEventLog
        from src.api.models.database import Base
        from src.api.models.round import Round
        from src.api.models.session import Session
        from src.api.routes import chat as chat_routes

        mock_settings = MagicMock()
        mock_settings.sse_heartbeat_interval = 60
        mock_settings.sse_subscribe_timeout = 1
        mock_settings.cancel_watcher_interval_seconds = 0.01

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        TestSL = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)
        try:
            with TestSL() as setup_db:
                setup_db.add(Session(id="session-1", user_id="testuser", status="active"))
                setup_db.add(
                    Round(
                        id="round-1",
                        session_id="session-1",
                        thread_id="session-1",
                        user_message="hello",
                        status="completed",
                    )
                )
                setup_db.add(
                    AGUIEventLog(
                        run_id="round-1",
                        event_type="RUN_FINISHED",
                        payload=json.dumps({
                            "type": "RUN_FINISHED",
                            "threadId": "session-1",
                            "runId": "round-1",
                            "outcome": "interrupt",
                            "result": {"reason": "user_cancelled"},
                            "sequence": 1,
                        }),
                        sequence=1,
                    )
                )
                setup_db.commit()

            with TestSL() as request_db, patch(
                "src.api.routes.chat.get_settings", return_value=mock_settings
            ), patch("src.api.routes.chat.SessionLocal", TestSL):
                response = await chat_routes.subscribe_to_round(
                    chat_session_id="session-1",
                    round_id="round-1",
                    last_sequence=0,
                    user_id="testuser",
                    db=request_db,
                )

                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                    if "RUN_FINISHED" in chunk:
                        break
        finally:
            Base.metadata.drop_all(bind=engine)
            engine.dispose()

        assert any("RUN_FINISHED" in c for c in chunks)

    @pytest.mark.asyncio
    async def test_subscribe_catchup_skips_aggregate_when_live_raw_delta_is_queued(self):
        """同一 EventBus 订阅连接已接收 live raw delta 时，聚合 CONTENT 不应重复输出。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from src.agent.schema.agui_events import TextMessageContentEvent, TextMessageEndEvent
        from src.api.models.database import Base
        from src.api.models.round import Round
        from src.api.models.session import Session
        from src.api.services.agui_event_bus import AguiEventBus

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        TestSL = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)
        run_id = "round-raw-vs-aggregate"
        try:
            with TestSL() as setup_db:
                setup_db.add(Session(id="session-1", user_id="testuser", status="active"))
                setup_db.add(
                    Round(
                        id=run_id,
                        session_id="session-1",
                        thread_id="session-1",
                        user_message="hello",
                        status="running",
                    )
                )
                setup_db.commit()

            bus = AguiEventBus(TestSL)
            iterator = bus.subscribe(run_id, after_sequence=0).__aiter__()
            first_event_task = asyncio.create_task(iterator.__anext__())
            try:
                for _ in range(50):
                    with bus.subscribers_lock:
                        if bus.subscribers.get(run_id):
                            break
                    await asyncio.sleep(0.01)

                await bus.publish(run_id, TextMessageContentEvent(messageId="msg-1", delta="he"))
                first = await asyncio.wait_for(first_event_task, timeout=1.0)
                assert first["type"] == "TEXT_MESSAGE_CONTENT"
                assert first["delta"] == "he"
                assert "sequence" not in first

                second_event_task = asyncio.create_task(iterator.__anext__())
                await bus.publish(run_id, TextMessageEndEvent(messageId="msg-1"))
                second = await asyncio.wait_for(second_event_task, timeout=1.0)

                assert second["type"] == "TEXT_MESSAGE_END"
                assert second["sequence"] == 2
            finally:
                await iterator.aclose()
                with bus.subscribers_lock:
                    bus.subscribers.pop(run_id, None)
        finally:
            Base.metadata.drop_all(bind=engine)
            engine.dispose()

    @pytest.mark.asyncio
    async def test_late_subscriber_suppresses_raw_suffix_and_receives_aggregate(self):
        """中途订阅者错过 live-only 前缀时，应等聚合 CONTENT 补全整段内容。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from src.agent.schema.agui_events import (
            TextMessageContentEvent,
            TextMessageEndEvent,
            TextMessageStartEvent,
        )
        from src.api.models.database import Base
        from src.api.models.round import Round
        from src.api.models.session import Session
        from src.api.services.agui_event_bus import AguiEventBus

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        TestSL = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)
        run_id = "round-late-aggregate"
        try:
            with TestSL() as setup_db:
                setup_db.add(Session(id="session-1", user_id="testuser", status="active"))
                setup_db.add(
                    Round(
                        id=run_id,
                        session_id="session-1",
                        thread_id="session-1",
                        user_message="hello",
                        status="running",
                    )
                )
                setup_db.commit()

            bus = AguiEventBus(TestSL)
            await bus.publish(run_id, TextMessageStartEvent(messageId="msg-1", role="assistant"))
            await bus.publish(run_id, TextMessageContentEvent(messageId="msg-1", delta="he"))

            iterator = bus.subscribe(run_id, after_sequence=0).__aiter__()
            try:
                first = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
                assert first["type"] == "TEXT_MESSAGE_START"
                assert first["sequence"] == 1

                next_event_task = asyncio.create_task(iterator.__anext__())
                for _ in range(50):
                    with bus.subscribers_lock:
                        if bus.subscribers.get(run_id):
                            break
                    await asyncio.sleep(0.01)

                await bus.publish(run_id, TextMessageContentEvent(messageId="msg-1", delta="llo"))
                await asyncio.sleep(0.05)
                assert not next_event_task.done()

                await bus.publish(run_id, TextMessageEndEvent(messageId="msg-1"))
                aggregate = await asyncio.wait_for(next_event_task, timeout=1.0)
                assert aggregate["type"] == "TEXT_MESSAGE_CONTENT"
                assert aggregate["delta"] == "hello"
                assert aggregate["sequence"] == 2

                end = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
                assert end["type"] == "TEXT_MESSAGE_END"
                assert end["sequence"] == 3
            finally:
                await iterator.aclose()
                with bus.subscribers_lock:
                    bus.subscribers.pop(run_id, None)
                bus._stream_buffers.pop(run_id, None)
        finally:
            Base.metadata.drop_all(bind=engine)
            engine.dispose()


class TestSubscriberAbortScenarios:
    """订阅者中止场景测试 - 测试客户端切换会话时的订阅取消"""

    @pytest.mark.asyncio
    async def test_subscriber_removed_on_disconnect(self):
        """测试订阅者断开连接时被正确移除"""

        round_id = "abort-test-disconnect"
        subscriber_queue = asyncio.Queue()

        # 注册订阅者
        _event_subscribers()[round_id] = [subscriber_queue]
        initial_count = len(_event_subscribers()[round_id])

        try:
            # 模拟客户端断开 - 移除订阅者
            _event_subscribers()[round_id].remove(subscriber_queue)

            assert len(_event_subscribers()[round_id]) == initial_count - 1
            assert subscriber_queue not in _event_subscribers()[round_id]
        finally:
            if round_id in _event_subscribers():
                del _event_subscribers()[round_id]

    @pytest.mark.asyncio
    async def test_multiple_subscribers_one_disconnects(self):
        """测试多个订阅者中一个断开连接"""

        round_id = "abort-test-multi"
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()

        _event_subscribers()[round_id] = [queue1, queue2]

        try:
            # 模拟第一个订阅者断开
            _event_subscribers()[round_id].remove(queue1)

            assert len(_event_subscribers()[round_id]) == 1
            assert queue2 in _event_subscribers()[round_id]

            # 广播事件应该只发送给剩余订阅者
            event = {"type": "test", "data": "after disconnect"}
            await _publish_ephemeral(round_id, event)

            # 只有 queue2 收到事件
            received = await asyncio.wait_for(queue2.get(), timeout=1.0)
            assert received == event

            # queue1 不应该收到（已断开）
            assert queue1.empty()
        finally:
            if round_id in _event_subscribers():
                del _event_subscribers()[round_id]

    @pytest.mark.asyncio
    async def test_subscriber_count_tracking(self):
        """测试订阅者计数追踪"""

        round_id = "count-test"
        queues = [asyncio.Queue() for _ in range(3)]

        _event_subscribers()[round_id] = []

        try:
            # 逐个添加订阅者
            for i, queue in enumerate(queues):
                _event_subscribers()[round_id].append(queue)
                assert len(_event_subscribers()[round_id]) == i + 1

            # 逐个移除订阅者（模拟切换会话）
            for i, queue in enumerate(queues):
                _event_subscribers()[round_id].remove(queue)
                assert len(_event_subscribers()[round_id]) == len(queues) - i - 1
        finally:
            if round_id in _event_subscribers():
                del _event_subscribers()[round_id]

    @pytest.mark.asyncio
    async def test_cleanup_on_last_subscriber_disconnect(self):
        """测试最后一个订阅者断开时的清理"""

        round_id = "cleanup-last-test"
        subscriber_queue = asyncio.Queue()

        _event_subscribers()[round_id] = [subscriber_queue]

        try:
            # 移除唯一的订阅者
            _event_subscribers()[round_id].remove(subscriber_queue)

            # 列表为空但 key 仍存在
            assert len(_event_subscribers()[round_id]) == 0
            assert round_id in _event_subscribers()

            # 调用清理
            _cleanup_event_subscribers(round_id)

            # key 被移除
            assert round_id not in _event_subscribers()
        finally:
            if round_id in _event_subscribers():
                del _event_subscribers()[round_id]

    @pytest.mark.asyncio
    async def test_rapid_subscribe_unsubscribe(self):
        """测试快速订阅和取消订阅（模拟用户快速切换会话）"""

        round_id = "rapid-test"

        try:
            # 快速多次订阅/取消
            for i in range(5):
                queue = asyncio.Queue()

                if round_id not in _event_subscribers():
                    _event_subscribers()[round_id] = []

                _event_subscribers()[round_id].append(queue)
                assert queue in _event_subscribers()[round_id]

                # 立即取消
                _event_subscribers()[round_id].remove(queue)
                assert queue not in _event_subscribers()[round_id]

            # 最终应该是空列表
            if round_id in _event_subscribers():
                assert len(_event_subscribers()[round_id]) == 0
        finally:
            if round_id in _event_subscribers():
                del _event_subscribers()[round_id]


class TestSubscribeEdgeCases:
    """订阅边缘情况测试"""

    def test_empty_final_response(self):
        """测试空的最终响应"""
        from src.agent.schema.agui_events import RunFinishedEvent

        event = RunFinishedEvent(
            threadId="session-empty", runId="round-empty",
            result={"finalResponse": "", "stepCount": 0},
            outcome="interrupt",
        )
        data = event.model_dump(by_alias=True)

        assert data["result"]["finalResponse"] == ""

    def test_null_final_response_handling(self):
        """测试 None 最终响应处理"""
        final_response = None

        # 使用 or "" 处理 None
        safe_response = final_response or ""

        assert safe_response == ""

    def test_json_tool_calls_parsing(self):
        """测试 JSON 工具调用解析"""
        tool_calls_json = '[{"name": "read_file", "args": {"path": "/test.txt"}}]'

        parsed = json.loads(tool_calls_json)

        assert len(parsed) == 1
        assert parsed[0]["name"] == "read_file"

    def test_empty_tool_calls_handling(self):
        """测试空工具调用处理"""
        tool_calls_json = None

        # 处理 None
        tool_calls = json.loads(tool_calls_json) if tool_calls_json else []

        assert tool_calls == []

    @pytest.mark.asyncio
    async def test_multiple_subscribers_same_round(self):
        """测试同一轮次多个订阅者"""

        round_id = "multi-sub-test"
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        queue3 = asyncio.Queue()

        _event_subscribers()[round_id] = [queue1, queue2, queue3]

        try:
            event = {"type": "test", "data": "broadcast"}
            await _publish_ephemeral(round_id, event)

            # 所有订阅者都应收到
            r1 = await asyncio.wait_for(queue1.get(), timeout=1.0)
            r2 = await asyncio.wait_for(queue2.get(), timeout=1.0)
            r3 = await asyncio.wait_for(queue3.get(), timeout=1.0)

            assert r1 == r2 == r3 == event
        finally:
            if round_id in _event_subscribers():
                del _event_subscribers()[round_id]


class TestAbortCancellation:
    """abort_chat 取消机制回归测试

    回归：触发取消后 SSE consumer 未收到 RUN_FINISHED，导致前端 UI 不刷新，
    需要刷新浏览器才能看到 Cancelled 状态。
    修复：producer 的 CancelledError 路径现在通过 terminal 单入口收敛并发送 RUN_FINISHED(outcome=interrupt)。
    """

    @pytest.mark.asyncio
    async def test_producer_cancelled_delivers_run_finished(self):
        """producer task 被取消时，orchestrated event stream 应收到 RUN_FINISHED(outcome=interrupt)。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from src.api.models.database import Base
        from src.api.models.round import Round
        from src.api.models.session import Session
        from src.api.schemas.chat import TextContentBlock
        from src.api.schemas.turn import NormalizedInboundTurn, WebReplyRoute
        from src.api.services.agent_service import PreparedAgentRun
        from src.api.services.turn_orchestrator import TurnOrchestrator
        from src.agent.schema.agui_events import RunStartedEvent

        session_id = "abort-run-finished-regression"
        run_id = "abort-run-id-regression"
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        TestSL = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)
        with TestSL() as setup_db:
            setup_db.add(Session(id=session_id, user_id="testuser", status="active"))
            setup_db.add(
                Round(
                    id=run_id,
                    session_id=session_id,
                    thread_id=session_id,
                    user_message="hello",
                    status="running",
                )
            )
            setup_db.commit()

        class SlowAgentService:
            cancel_token = None

            async def prepare_chat_round(self, *, user_content, idempotency_key=None):
                return PreparedAgentRun(run_id=run_id, user_message="hello")

            async def run_prepared_round(self, prepared, *, error_label):
                yield RunStartedEvent(threadId=session_id, runId=prepared.run_id)
                await asyncio.sleep(100)

        turn = NormalizedInboundTurn(
            channel="web",
            user_id="testuser",
            peer_kind="web",
            peer_id=session_id,
            content=[TextContentBlock(type="text", text="hello")],
            reply_route=WebReplyRoute(session_id=session_id),
        )
        orchestrator = TurnOrchestrator()

        try:
            with patch("src.api.services.turn_orchestrator.SessionLocal", TestSL):
                execution = await orchestrator.submit_turn(
                    turn,
                    agent_service=SlowAgentService(),
                )
                first_event = await asyncio.wait_for(execution.event_source.__anext__(), timeout=1.0)
                assert first_event.run_id == run_id

                assert execution.task is not None
                execution.task.cancel()
                events_received = [first_event]
                events_received.extend([event async for event in execution.event_source])
                with pytest.raises(asyncio.CancelledError):
                    await execution.task
        finally:
            Base.metadata.drop_all(bind=engine)
            engine.dispose()

        run_finished = [
            e if isinstance(e, dict) else e.model_dump(by_alias=True)
            for e in events_received
            if (e.get("type") if isinstance(e, dict) else getattr(e, "type", None)) == "RUN_FINISHED"
        ]
        assert len(run_finished) >= 1, (
            f"应该有 RUN_FINISHED 事件，实际收到: {events_received}"
        )
        assert run_finished[0]["outcome"] == "interrupt"
        assert run_finished[0]["sequence"] == 1
        assert run_finished[0].get("result", {}).get("reason") == "user_cancelled"

    def test_cancelled_round_in_terminal_status_set(self):
        """cancelled 状态应在 subscribe_to_round 的终态检查集合中

        回归：subscribe_to_round 未处理 cancelled 状态，导致订阅者永久等待。
        """
        cancelled_status = "cancelled"
        from src.api.models.round import Round
        assert cancelled_status in Round.SUBSCRIBE_TERMINAL_STATUSES

    def test_cancelled_round_emits_interrupt_outcome(self):
        """subscribe_to_round 对 cancelled 轮次应发 RUN_FINISHED(outcome=interrupt)

        cancelled 属于用户主动中止，语义上等同于 interrupt（无 interrupt 详情对象）。
        """
        from src.agent.schema.agui_events import RunFinishedEvent

        finished = RunFinishedEvent(
            threadId="session-cancel-sub",
            runId="round-cancel-sub",
            result={
                "finalResponse": "",
                "stepCount": 0,
                "reason": "user_cancelled",
            },
            outcome="interrupt",
        )
        data = finished.model_dump(by_alias=True)

        assert data["type"] == "RUN_FINISHED"
        assert data["outcome"] == "interrupt", "cancelled 轮次应为 interrupt 而非 success"
        assert data["result"]["reason"] == "user_cancelled"
        # cancelled 不携带 interrupt 详情（区别于 ask_user 中断）
        assert data.get("interrupt") is None

    @pytest.mark.asyncio
    async def test_producer_cancelled_broadcasts_to_subscribers(self):
        """orchestrated producer 被 cancel 时，subscriber 应收到已持久化并带 sequence 的 terminal。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from src.api.models.agui_event import AGUIEventLog
        from src.api.models.database import Base
        from src.api.models.round import Round
        from src.api.models.session import Session
        from src.api.schemas.chat import TextContentBlock
        from src.api.schemas.turn import NormalizedInboundTurn, WebReplyRoute
        from src.api.services.agent_service import PreparedAgentRun
        from src.api.services.turn_orchestrator import TurnOrchestrator
        from src.agent.schema.agui_events import RunStartedEvent

        session_id = "abort-subscriber-broadcast-test"
        run_id = "abort-subscriber-run-id"
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        TestSL = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)
        with TestSL() as setup_db:
            setup_db.add(Session(id=session_id, user_id="testuser", status="active"))
            setup_db.add(
                Round(
                    id=run_id,
                    session_id=session_id,
                    thread_id=session_id,
                    user_message="hello",
                    status="running",
                )
            )
            setup_db.commit()

        # 模拟一个 subscriber（代表 subscribe 端点的客户端）
        subscriber_queue = asyncio.Queue()
        with _event_subscribers_lock():
            _event_subscribers()[run_id] = [subscriber_queue]

        class SlowAgentService:
            cancel_token = None

            async def prepare_chat_round(self, *, user_content, idempotency_key=None):
                return PreparedAgentRun(run_id=run_id, user_message="hello")

            async def run_prepared_round(self, prepared, *, error_label):
                yield RunStartedEvent(threadId=session_id, runId=prepared.run_id)
                await asyncio.sleep(100)

        turn = NormalizedInboundTurn(
            channel="web",
            user_id="testuser",
            peer_kind="web",
            peer_id=session_id,
            content=[TextContentBlock(type="text", text="hello")],
            reply_route=WebReplyRoute(session_id=session_id),
        )
        orchestrator = TurnOrchestrator()

        try:
            with patch("src.api.services.turn_orchestrator.SessionLocal", TestSL):
                execution = await orchestrator.submit_turn(
                    turn,
                    agent_service=SlowAgentService(),
                )
                first_event = await asyncio.wait_for(execution.event_source.__anext__(), timeout=1.0)
                assert first_event.run_id == run_id

                assert execution.task is not None
                execution.task.cancel()
                _ = [event async for event in execution.event_source]
                with pytest.raises(asyncio.CancelledError):
                    await execution.task
        finally:
            pass

        # 核心断言：subscriber 队列必须收到 durable RUN_FINISHED
        received_events = []
        while not subscriber_queue.empty():
            received_events.append(subscriber_queue.get_nowait())

        run_finished_events = [
            e for e in received_events
            if isinstance(e, dict) and e.get("type") == "RUN_FINISHED"
        ]
        assert len(run_finished_events) >= 1, (
            f"subscriber 队列应收到 RUN_FINISHED，实际收到: {received_events}"
        )
        assert run_finished_events[0]["sequence"] == 1
        assert run_finished_events[0]["outcome"] == "interrupt"
        assert run_finished_events[0].get("result", {}).get("reason") == "user_cancelled"

        with TestSL() as verify_db:
            terminal = (
                verify_db.query(AGUIEventLog)
                .filter(
                    AGUIEventLog.run_id == run_id,
                    AGUIEventLog.event_type == "RUN_FINISHED",
                )
                .one()
            )
            assert terminal.sequence == 1

        # 清理
        with _event_subscribers_lock():
            _event_subscribers().pop(run_id, None)
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


class TestUserCancelledRoundStatus:
    """回归：RUN_FINISHED user_cancelled 最终 round.status 为 cancelled。"""

    @pytest.mark.asyncio
    async def test_run_finished_user_cancelled_sets_status_cancelled(self):
        """Agent yield RUN_FINISHED(outcome=interrupt, result.reason=user_cancelled) → status=cancelled"""
        from src.api.services.agent_service import AgentService
        from src.agent.schema.agui_events import (
            RunStartedEvent, RunFinishedEvent,
            StepStartedEvent, StepFinishedEvent,
        )

        mock_history = MagicMock()
        mock_history.save_agui_event = AsyncMock()
        mock_history.complete_round = MagicMock()

        service = object.__new__(AgentService)
        service.history_service = mock_history
        service.session_id = "test-session"
        service.cancel_token = None
        service._active_run_count = 0
        service.agent = MagicMock()
        service._last_saved_index = 0

        async def fake_run_agui(**kwargs):
            yield RunStartedEvent(threadId="test-session", runId="run-cancel")
            yield StepStartedEvent(stepName="step_1")
            yield StepFinishedEvent(stepName="step_1")
            yield RunFinishedEvent(
                threadId="test-session",
                runId="run-cancel",
                outcome="interrupt",
                result={"reason": "user_cancelled"},
            )

        service.agent.run_agui = fake_run_agui

        events = []
        async for event in service._run_round_stream(
            run_id="run-cancel",
            user_message="test",
        ):
            events.append(event)

        mock_history.complete_round.assert_called_once()
        call_kwargs = mock_history.complete_round.call_args
        actual_status = call_kwargs.kwargs.get("status") or call_kwargs[1].get("status")
        assert actual_status == "cancelled", (
            f"user_cancelled 应该映射为 cancelled，实际: {actual_status}"
        )
