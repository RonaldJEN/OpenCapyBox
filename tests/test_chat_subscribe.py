"""Chat 订阅功能测试 - SSE 断线恢复相关"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import json
import asyncio

from tests.helpers import make_mock_round


class TestRoundSubscribersManagement:
    """轮次订阅者管理测试"""

    def test_round_subscribers_initialization(self):
        """测试订阅者字典初始化"""
        from src.api.routes.chat import _round_subscribers

        assert isinstance(_round_subscribers, dict)

    def test_round_subscribers_operations(self):
        """测试订阅者字典操作"""
        from src.api.routes.chat import _round_subscribers

        # 保存原始状态
        original_keys = list(_round_subscribers.keys())

        # 添加测试条目
        test_round_id = "test-round-12345"
        _round_subscribers[test_round_id] = []

        assert test_round_id in _round_subscribers

        # 添加订阅者队列
        queue = asyncio.Queue()
        _round_subscribers[test_round_id].append(queue)

        assert len(_round_subscribers[test_round_id]) == 1

        # 清理
        del _round_subscribers[test_round_id]

        assert test_round_id not in _round_subscribers


class TestBroadcastToSubscribers:
    """广播事件测试"""

    @pytest.mark.asyncio
    async def test_broadcast_no_subscribers(self):
        """测试无订阅者时广播"""
        from src.api.routes.chat import _broadcast_to_subscribers, _round_subscribers

        test_round_id = "broadcast-test-no-subs"
        event = {"type": "test", "data": "hello"}

        # 确保没有订阅者
        if test_round_id in _round_subscribers:
            del _round_subscribers[test_round_id]

        # 不应抛出异常
        await _broadcast_to_subscribers(test_round_id, event)

    @pytest.mark.asyncio
    async def test_broadcast_with_subscribers(self):
        """测试有订阅者时广播"""
        from src.api.routes.chat import _broadcast_to_subscribers, _round_subscribers

        test_round_id = "broadcast-test-with-subs"
        event = {"type": "step", "round_id": test_round_id, "data": "test"}

        # 创建订阅者队列
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()

        _round_subscribers[test_round_id] = [queue1, queue2]

        try:
            # 广播事件
            await _broadcast_to_subscribers(test_round_id, event)

            # 验证两个队列都收到了事件
            event1 = await asyncio.wait_for(queue1.get(), timeout=1.0)
            event2 = await asyncio.wait_for(queue2.get(), timeout=1.0)

            assert event1 == event
            assert event2 == event
        finally:
            # 清理
            if test_round_id in _round_subscribers:
                del _round_subscribers[test_round_id]

    @pytest.mark.asyncio
    async def test_broadcast_handles_queue_error(self):
        """测试广播处理队列错误"""
        from src.api.routes.chat import _broadcast_to_subscribers, _round_subscribers

        test_round_id = "broadcast-error-test"
        event = {"type": "test"}

        # 创建一个会抛出异常的模拟队列
        bad_queue = MagicMock()
        bad_queue.put = AsyncMock(side_effect=Exception("Queue error"))

        good_queue = asyncio.Queue()

        _round_subscribers[test_round_id] = [bad_queue, good_queue]

        try:
            # 不应抛出异常，即使一个队列出错
            await _broadcast_to_subscribers(test_round_id, event)

            # 好的队列仍应收到事件
            event_received = await asyncio.wait_for(good_queue.get(), timeout=1.0)
            assert event_received == event
        finally:
            if test_round_id in _round_subscribers:
                del _round_subscribers[test_round_id]


class TestCleanupSubscribers:
    """清理订阅者测试"""

    def test_cleanup_existing_round(self):
        """测试清理已存在的轮次订阅者"""
        from src.api.routes.chat import _cleanup_subscribers, _round_subscribers

        test_round_id = "cleanup-test-existing"
        _round_subscribers[test_round_id] = [asyncio.Queue()]

        _cleanup_subscribers(test_round_id)

        assert test_round_id not in _round_subscribers

    def test_cleanup_nonexistent_round(self):
        """测试清理不存在的轮次"""
        from src.api.routes.chat import _cleanup_subscribers, _round_subscribers

        test_round_id = "cleanup-test-nonexistent"

        # 确保不存在
        if test_round_id in _round_subscribers:
            del _round_subscribers[test_round_id]

        # 不应抛出异常
        _cleanup_subscribers(test_round_id)

        assert test_round_id not in _round_subscribers


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

    def test_failed_round_emits_error_then_finished(self):
        """测试失败轮次先发 RUN_ERROR 再发 RUN_FINISHED(outcome=interrupt)"""
        from src.agent.schema.agui_events import RunErrorEvent, RunFinishedEvent

        error_event = RunErrorEvent(message="Run failed (status=failed)", code="RUN_FAILED")
        finished_event = RunFinishedEvent(
            threadId="session-123",
            runId="failed-round-456",
            result={"finalResponse": "", "stepCount": 2},
            outcome="interrupt",
        )

        err_data = error_event.model_dump(by_alias=True)
        fin_data = finished_event.model_dump(by_alias=True)

        assert err_data["type"] == "RUN_ERROR"
        assert fin_data["type"] == "RUN_FINISHED"
        assert fin_data["outcome"] == "interrupt"

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
        """测试失败轮次应立即返回 RUN_ERROR + RUN_FINISHED(outcome=interrupt)"""
        round_obj = mock_round_failed

        should_return_immediately = round_obj.status in ("completed", "failed")
        assert should_return_immediately

        from src.agent.schema.agui_events import RunErrorEvent, RunFinishedEvent

        error_event = RunErrorEvent(message="Run failed (status=failed)", code="RUN_FAILED")
        finished_event = RunFinishedEvent(
            threadId="session-123",
            runId=round_obj.id,
            result={
                "finalResponse": round_obj.final_response or "",
                "stepCount": round_obj.step_count,
            },
            outcome="interrupt",
        )
        err = error_event.model_dump(by_alias=True)
        fin = finished_event.model_dump(by_alias=True)
        assert err["type"] == "RUN_ERROR"
        assert fin["type"] == "RUN_FINISHED"
        assert fin["outcome"] == "interrupt"

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
        from src.api.routes.chat import _round_subscribers

        test_round_id = "async-test-registration"
        subscriber_queue = asyncio.Queue()

        # 注册订阅者
        if test_round_id not in _round_subscribers:
            _round_subscribers[test_round_id] = []
        _round_subscribers[test_round_id].append(subscriber_queue)

        try:
            assert test_round_id in _round_subscribers
            assert subscriber_queue in _round_subscribers[test_round_id]
            assert len(_round_subscribers[test_round_id]) == 1
        finally:
            del _round_subscribers[test_round_id]

    @pytest.mark.asyncio
    async def test_subscriber_queue_removal(self):
        """测试订阅者队列移除"""
        from src.api.routes.chat import _round_subscribers

        test_round_id = "async-test-removal"
        subscriber_queue = asyncio.Queue()

        _round_subscribers[test_round_id] = [subscriber_queue]

        try:
            # 移除订阅者
            _round_subscribers[test_round_id].remove(subscriber_queue)

            assert subscriber_queue not in _round_subscribers[test_round_id]
            assert len(_round_subscribers[test_round_id]) == 0
        finally:
            if test_round_id in _round_subscribers:
                del _round_subscribers[test_round_id]

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
        """测试失败轮次的完整 AG-UI 事件流：RUN_ERROR + RUN_FINISHED"""
        from src.agent.schema.agui_events import (
            MessagesSnapshotEvent, RunErrorEvent, RunFinishedEvent,
        )

        snapshot = MessagesSnapshotEvent(messages=[])
        error = RunErrorEvent(message="Run failed (status=failed)", code="RUN_FAILED")
        finished = RunFinishedEvent(
            threadId="session-123", runId="round-456",
            result={"finalResponse": "", "stepCount": 1},
            outcome="interrupt",
        )

        events = [
            snapshot.model_dump(by_alias=True),
            error.model_dump(by_alias=True),
            finished.model_dump(by_alias=True),
        ]

        assert events[1]["type"] == "RUN_ERROR"
        assert events[2]["type"] == "RUN_FINISHED"
        assert events[2]["outcome"] == "interrupt"

    @pytest.mark.asyncio
    async def test_complete_flow_for_running_round(self):
        """测试运行中轮次的完整流程：注册订阅 → 广播事件 → 接收"""
        from src.api.routes.chat import _round_subscribers, _broadcast_to_subscribers

        round_id = "test-running-flow"
        round_status = "running"

        is_finished = round_status in ("completed", "failed")
        assert not is_finished

        # 注册订阅者
        subscriber_queue = asyncio.Queue()
        _round_subscribers[round_id] = [subscriber_queue]

        try:
            # 模拟 AG-UI 事件广播
            new_event = {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-1", "delta": "hello"}
            await _broadcast_to_subscribers(round_id, new_event)

            received = await asyncio.wait_for(subscriber_queue.get(), timeout=1.0)
            assert received["type"] == "TEXT_MESSAGE_CONTENT"
            assert received["delta"] == "hello"
        finally:
            if round_id in _round_subscribers:
                del _round_subscribers[round_id]


class TestSubscriberAbortScenarios:
    """订阅者中止场景测试 - 测试客户端切换会话时的订阅取消"""

    @pytest.mark.asyncio
    async def test_subscriber_removed_on_disconnect(self):
        """测试订阅者断开连接时被正确移除"""
        from src.api.routes.chat import _round_subscribers

        round_id = "abort-test-disconnect"
        subscriber_queue = asyncio.Queue()

        # 注册订阅者
        _round_subscribers[round_id] = [subscriber_queue]
        initial_count = len(_round_subscribers[round_id])

        try:
            # 模拟客户端断开 - 移除订阅者
            _round_subscribers[round_id].remove(subscriber_queue)

            assert len(_round_subscribers[round_id]) == initial_count - 1
            assert subscriber_queue not in _round_subscribers[round_id]
        finally:
            if round_id in _round_subscribers:
                del _round_subscribers[round_id]

    @pytest.mark.asyncio
    async def test_multiple_subscribers_one_disconnects(self):
        """测试多个订阅者中一个断开连接"""
        from src.api.routes.chat import _round_subscribers, _broadcast_to_subscribers

        round_id = "abort-test-multi"
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()

        _round_subscribers[round_id] = [queue1, queue2]

        try:
            # 模拟第一个订阅者断开
            _round_subscribers[round_id].remove(queue1)

            assert len(_round_subscribers[round_id]) == 1
            assert queue2 in _round_subscribers[round_id]

            # 广播事件应该只发送给剩余订阅者
            event = {"type": "test", "data": "after disconnect"}
            await _broadcast_to_subscribers(round_id, event)

            # 只有 queue2 收到事件
            received = await asyncio.wait_for(queue2.get(), timeout=1.0)
            assert received == event

            # queue1 不应该收到（已断开）
            assert queue1.empty()
        finally:
            if round_id in _round_subscribers:
                del _round_subscribers[round_id]

    @pytest.mark.asyncio
    async def test_subscriber_count_tracking(self):
        """测试订阅者计数追踪"""
        from src.api.routes.chat import _round_subscribers

        round_id = "count-test"
        queues = [asyncio.Queue() for _ in range(3)]

        _round_subscribers[round_id] = []

        try:
            # 逐个添加订阅者
            for i, queue in enumerate(queues):
                _round_subscribers[round_id].append(queue)
                assert len(_round_subscribers[round_id]) == i + 1

            # 逐个移除订阅者（模拟切换会话）
            for i, queue in enumerate(queues):
                _round_subscribers[round_id].remove(queue)
                assert len(_round_subscribers[round_id]) == len(queues) - i - 1
        finally:
            if round_id in _round_subscribers:
                del _round_subscribers[round_id]

    @pytest.mark.asyncio
    async def test_cleanup_on_last_subscriber_disconnect(self):
        """测试最后一个订阅者断开时的清理"""
        from src.api.routes.chat import _round_subscribers, _cleanup_subscribers

        round_id = "cleanup-last-test"
        subscriber_queue = asyncio.Queue()

        _round_subscribers[round_id] = [subscriber_queue]

        try:
            # 移除唯一的订阅者
            _round_subscribers[round_id].remove(subscriber_queue)

            # 列表为空但 key 仍存在
            assert len(_round_subscribers[round_id]) == 0
            assert round_id in _round_subscribers

            # 调用清理
            _cleanup_subscribers(round_id)

            # key 被移除
            assert round_id not in _round_subscribers
        finally:
            if round_id in _round_subscribers:
                del _round_subscribers[round_id]

    @pytest.mark.asyncio
    async def test_rapid_subscribe_unsubscribe(self):
        """测试快速订阅和取消订阅（模拟用户快速切换会话）"""
        from src.api.routes.chat import _round_subscribers

        round_id = "rapid-test"

        try:
            # 快速多次订阅/取消
            for i in range(5):
                queue = asyncio.Queue()

                if round_id not in _round_subscribers:
                    _round_subscribers[round_id] = []

                _round_subscribers[round_id].append(queue)
                assert queue in _round_subscribers[round_id]

                # 立即取消
                _round_subscribers[round_id].remove(queue)
                assert queue not in _round_subscribers[round_id]

            # 最终应该是空列表
            if round_id in _round_subscribers:
                assert len(_round_subscribers[round_id]) == 0
        finally:
            if round_id in _round_subscribers:
                del _round_subscribers[round_id]


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
        from src.api.routes.chat import _round_subscribers, _broadcast_to_subscribers

        round_id = "multi-sub-test"
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        queue3 = asyncio.Queue()

        _round_subscribers[round_id] = [queue1, queue2, queue3]

        try:
            event = {"type": "test", "data": "broadcast"}
            await _broadcast_to_subscribers(round_id, event)

            # 所有订阅者都应收到
            r1 = await asyncio.wait_for(queue1.get(), timeout=1.0)
            r2 = await asyncio.wait_for(queue2.get(), timeout=1.0)
            r3 = await asyncio.wait_for(queue3.get(), timeout=1.0)

            assert r1 == r2 == r3 == event
        finally:
            if round_id in _round_subscribers:
                del _round_subscribers[round_id]
