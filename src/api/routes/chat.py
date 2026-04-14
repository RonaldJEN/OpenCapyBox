"""对话 API - AG-UI 协议实现

AG-UI 協議的瀏覽器刷新/重連機制：
1. 所有事件在發送時同時持久化到 agui_events 表
2. 客戶端重連時通過 lastSequence 參數告知最後收到的事件序號
3. 服務端重放 lastSequence 之後的所有事件
4. 使用 MESSAGES_SNAPSHOT 恢復歷史，然後繼續流式推送

重構說明 (v2):
- 主路由使用 agent_service.chat_agui() 直接透傳 AG-UI 事件
- 移除了 ~350 行的手動事件轉換代碼
- 事件持久化由 AgentService 內部處理
- 保留訂閱者廣播和標題生成功能
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DBSession
from src.api.models.database import get_db
from src.api.deps import get_current_user
from src.api.models.session import Session
from src.api.models.agui_event import AGUIEventLog
from src.api.schemas.chat import SendMessageRequest, ResumeRequest
from src.api.services.agent_pool_service import get_agent_pool
from src.api.models.user_sandbox import UserSandbox
from src.api.config import get_settings
import logging
import time
from datetime import datetime
from src.api.utils.timezone import now_naive
# AG-UI 事件類型統一從 Agent 層導入
from src.agent.schema.agui_events import AGUIEvent, RunStartedEvent, CustomEvent, EventType, RunErrorEvent, RunFinishedEvent, MessagesSnapshotEvent, InterruptDetails
from src.api.utils.agui_encoder import EventEncoder
from src.api.services.agent_service import DuplicateRoundError
from src.api.models.round import Round
from src.api.services.history_service import HistoryService
from src.api.models.database import SessionLocal
import asyncio
import json
import traceback
import uuid
import threading
from typing import AsyncIterator, Callable, Awaitable

logger = logging.getLogger(__name__)
router = APIRouter()
event_encoder = EventEncoder()

# 上次清理時間（節流：每60秒最多清理一次）
_last_cleanup_time: float = 0.0


def _extract_text_for_title(content_blocks) -> str:
    """從 content blocks 抽取可讀文本（用於標題生成）。"""
    parts = []
    for block in content_blocks:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
        elif block_type == "image_url":
            parts.append("[图片]")
        elif block_type == "video_url":
            parts.append("[视频]")
        elif block_type == "file":
            file_obj = getattr(block, "file", None)
            file_name = getattr(file_obj, "name", None) if file_obj else None
            file_path = getattr(file_obj, "path", None) if file_obj else None
            parts.append(f"[文件:{file_name or file_path or 'unknown'}]")
    return " ".join(parts).strip() or "新会话"

# =========================================================================
# 輪次訂閱者管理（用於多客戶端同步）
# =========================================================================

# 輪次訂閱者管理（round_id -> list of asyncio.Queue）
_round_subscribers: dict[str, list[asyncio.Queue]] = {}
_round_subscribers_lock = threading.Lock()

# 活躍的後台 Agent 運行任務（session_id -> producer task）
# Agent 執行與 SSE 連接解耦：瀏覽器斷開後 Agent 繼續在後台運行
_active_runners: dict[str, asyncio.Task] = {}


# =========================================================================
# SSE + 心跳保活通用助手
# =========================================================================


async def _sse_with_heartbeat(
    event_source: AsyncIterator[AGUIEvent],
    *,
    session_id: str | None = None,
    on_run_finished: Callable[[str | None], Awaitable[AsyncIterator[str]]] | None = None,
    error_message: str | None = None,
):
    """通用的 SSE 事件生成器，內建 producer/heartbeat/consumer 隊列模式。

    Producer 負責驅動 Agent 執行和廣播事件給訂閱者。當 SSE 連接斷開（瀏覽器關閉）
    時，producer 不會被取消——Agent 繼續在後台運行，事件通過 DB 持久化和
    subscriber 機制傳遞給重連的客戶端。

    Args:
        event_source: Agent 層的 AG-UI 事件異步迭代器。
        session_id: 會話 ID，用於追蹤後台運行任務（傳入則啟用後台運行模式）。
        on_run_finished: 可選回調，在 RUN_FINISHED 事件之後調用，
                         返回一個異步迭代器以 yield 額外的 SSE 字串（如標題更新）。
        error_message: 錯誤時對外顯示的訊息；為 None 則使用實際異常訊息。
    """
    current_run_id: str | None = None
    event_queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()
    _run_completed = False
    _consumer_active = True  # SSE 消費者是否仍存活

    settings = get_settings()

    def now_ms():
        return int(datetime.now().timestamp() * 1000)

    # 生產者：消費 event_source → 廣播 + 放入本地隊列
    async def producer():
        nonlocal current_run_id
        try:
            async for event in event_source:
                if hasattr(event, 'run_id') and event.run_id:
                    current_run_id = event.run_id
                # 廣播給所有訂閱者（確保重連客戶端能收到實時事件）
                if current_run_id:
                    try:
                        event_dict = event.model_dump(by_alias=True, exclude_none=True)
                        await _broadcast_to_subscribers(current_run_id, event_dict)
                    except Exception:
                        pass
                # 只在 SSE 消費者存活時才放入本地隊列
                if _consumer_active:
                    event_queue.put_nowait(event)
        except asyncio.CancelledError:
            # 被 abort 取消，傳播以觸發 event_source 的清理
            raise
        except Exception as e:
            if _consumer_active:
                event_queue.put_nowait(e)
        finally:
            if _consumer_active:
                event_queue.put_nowait(_SENTINEL)
            # 清理追蹤
            if session_id:
                _active_runners.pop(session_id, None)
            if current_run_id:
                _cleanup_subscribers(current_run_id)

    # 心跳：每 sse_heartbeat_interval 秒發送 CUSTOM heartbeat 事件
    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(settings.sse_heartbeat_interval)
                await event_queue.put(CustomEvent(
                    name="heartbeat",
                    value={"timestamp": now_ms()},
                ))
        except asyncio.CancelledError:
            pass

    producer_task = asyncio.create_task(producer())
    heartbeat_task = asyncio.create_task(heartbeat())

    # 註冊為活躍運行任務
    if session_id:
        _active_runners[session_id] = producer_task

    try:
        while True:
            item = await event_queue.get()

            if item is _SENTINEL:
                break

            if isinstance(item, Exception):
                raise item

            event = item

            if hasattr(event, 'run_id') and event.run_id:
                current_run_id = event.run_id

            event_str = event_encoder.encode(event)

            # 廣播已在 producer 中處理，此處僅 yield 給當前 SSE 連接

            yield event_str

            if event.type == EventType.RUN_FINISHED:
                _run_completed = True
                # 允許調用方注入額外事件（如標題更新）
                if on_run_finished:
                    async for extra in await on_run_finished(current_run_id):
                        yield extra
                break

            if event.type == EventType.RUN_ERROR:
                _run_completed = True
                break

    except Exception as e:
        _run_completed = True
        error_detail = traceback.format_exc()
        logger.error("AG-UI 事件流錯誤: %s\n%s", e, error_detail)

        # DuplicateRoundError: 發送 existing_round_id 供客戶端切換到 subscribe
        if isinstance(e, DuplicateRoundError):
            display_msg = e.existing_round_id
            display_code = "ROUND_IN_PROGRESS"
        else:
            display_msg = error_message or str(e)
            display_code = "INTERNAL_ERROR" if error_message else type(e).__name__
        try:
            yield event_encoder.encode(RunErrorEvent(message=display_msg, code=display_code))
        except Exception:
            fallback_json = json.dumps({
                "type": EventType.RUN_ERROR.value,
                "message": display_msg,
                "code": display_code,
                "timestamp": datetime.now().timestamp() * 1000,
            })
            yield f"data: {fallback_json}\n\n"

    finally:
        _consumer_active = False
        heartbeat_task.cancel()

        if _run_completed:
            # 運行已結束，清理 producer（通常已自行結束）
            producer_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                pass
        else:
            # SSE 斷開但 Agent 仍在運行 → 不取消 producer，讓 Agent 繼續後台執行
            logger.info(
                "SSE 連接斷開，Agent 繼續後台運行 (session=%s, run=%s)",
                session_id, current_run_id,
            )

        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


# =========================================================================
# Agent 初始化助手
# =========================================================================


class _AgentInitFailed(Exception):
    """Agent 初始化失敗（僅用於 generator 內部流轉）"""
    pass


def _start_agent_init(
    *,
    user_id: str,
    chat_session_id: str,
    db,
    model_id: str | None,
    sandbox_id: str | None,
) -> asyncio.Task:
    """啟動 Agent 初始化 Task，並順帶做節流清理。

    返回的 Task 需要在心跳循環中 await 完成，再用 _resolve_agent_init 取結果。
    """
    agent_pool = get_agent_pool()

    # 定期清理過期 Agent（節流：每600秒最多一次）
    global _last_cleanup_time
    now = time.time()
    if now - _last_cleanup_time > 3600:
        _cleanup_task = asyncio.create_task(agent_pool.cleanup_expired_async())
        _cleanup_task.add_done_callback(lambda t: logger.error("Agent 清理異常: %s", t.exception()) if not t.cancelled() and t.exception() else None)
        _last_cleanup_time = now

    return asyncio.create_task(agent_pool.get_or_create(
        user_id=user_id,
        session_id=user_id,
        chat_session_id=chat_session_id,
        db=db,
        model_id=model_id,
        sandbox_id=sandbox_id,
    ))


def _resolve_agent_init(init_task: asyncio.Task):
    """從完成的 init_task 取出 AgentService，失敗時拋 _AgentInitFailed。"""
    try:
        return init_task.result()
    except Exception as e:
        logger.error("Agent 初始化失敗: %s: %s", type(e).__name__, e, exc_info=True)
        error_msg = f"Agent 初始化失敗: {type(e).__name__}: {str(e)}"
        if "api_key" in str(e).lower() or "apikey" in str(e).lower():
            error_msg += "\n\n💡 提示：請檢查 .env 文件中的 LLM_API_KEY 配置是否正確"
        raise _AgentInitFailed(error_msg) from e


@router.post("/{chat_session_id}/message/stream")
async def send_message_stream(
    chat_session_id: str,
    request: SendMessageRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """發送消息並流式返回 AG-UI 事件（Server-Sent Events）"""
    # 驗證會話
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )

    if not session:
        raise HTTPException(status_code=404, detail="會話不存在")

    if session.status == "completed":
        raise HTTPException(status_code=410, detail="會話已完成")

    # 幂等性保證依賴 DB 層 UniqueConstraint（history_service.create_round 的 IntegrityError 兜底）
    # 無需在此做 SELECT fast-path：TOCTOU 窗口使其不可靠，省掉的只是一次 Agent 初始化嘗試

    # 預讀 sandbox_id 和 round_count（輕量查詢，不會超時）
    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    user_sandbox_id = user_sandbox.sandbox_id if user_sandbox else None
    round_count = db.query(Round).filter(Round.session_id == chat_session_id).count()
    model_id = session.model_id

    # 定義事件生成器（Agent 初始化移入 generator 內部，讓 SSE 響應頭先返回，心跳保活撐住連接）
    async def event_generator():
        settings = get_settings()

        # --- Agent 初始化（帶心跳保活，防止 HAProxy 30s 超時）---
        init_task = _start_agent_init(
            user_id=user_id,
            chat_session_id=chat_session_id,
            db=db,
            model_id=model_id,
            sandbox_id=user_sandbox_id,
        )

        try:
            # 在初始化完成前持續發送心跳
            while not init_task.done():
                done, _ = await asyncio.wait({init_task}, timeout=settings.sse_heartbeat_interval)
                if not done:
                    yield event_encoder.encode(CustomEvent(
                        name="heartbeat",
                        value={"timestamp": int(datetime.now().timestamp() * 1000)},
                    ))

            agent_service = _resolve_agent_init(init_task)
        except _AgentInitFailed as e:
            yield event_encoder.encode(RunErrorEvent(message=str(e), code="AGENT_INIT_FAILED"))
            return
        finally:
            if not init_task.done():
                init_task.cancel()
                try:
                    await init_task
                except (asyncio.CancelledError, Exception):
                    pass

        # --- 標題生成任務（如果是第一條消息）---
        title_generation_task = None
        try:
            if round_count == 0:
                print(f"🏷️  檢測到第一條消息，啟動標題生成任務...")

                async def generate_title_async():
                    try:
                        title_source = _extract_text_for_title(request.content)
                        title = await agent_service.generate_session_title(title_source)
                        with SessionLocal() as title_db:
                            title_session = title_db.query(Session).filter(Session.id == chat_session_id).first()
                            if title_session:
                                title_session.title = title
                                title_session.updated_at = now_naive()
                                title_db.commit()
                                print(f"✅ 會話標題已保存: {title}")
                                return title
                    except Exception as e:
                        print(f"⚠️  標題生成失敗: {e}")
                        return None

                title_generation_task = asyncio.create_task(generate_title_async())

            # 創建 per-run 取消令牌
            cancel_token = asyncio.Event()
            agent_service.cancel_token = cancel_token

            # RUN_FINISHED 後追加標題更新事件
            async def on_run_finished(_run_id):
                async def _extra():
                    if title_generation_task:
                        try:
                            title = await title_generation_task
                            if title:
                                title_event = CustomEvent(
                                    name="title_updated",
                                    value={"sessionId": chat_session_id, "title": title},
                                )
                                yield event_encoder.encode(title_event)
                        except Exception as e:
                            print(f"⚠️  等待標題生成失敗: {e}")
                return _extra()

            async for chunk in _sse_with_heartbeat(
                agent_service.chat_agui(
                    user_content=request.content,
                    idempotency_key=request.idempotency_key,
                ),
                session_id=chat_session_id,
                on_run_finished=on_run_finished,
                error_message="Agent 執行失敗",
            ):
                yield chunk
        finally:
            if title_generation_task and not title_generation_task.done():
                title_generation_task.cancel()

    # 更新會話活躍時間
    session.updated_at = now_naive()
    db.commit()

    # 返回流式響應
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{chat_session_id}/resume")
async def resume_interrupt(
    chat_session_id: str,
    request: ResumeRequest,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """恢复被中断的 Agent 执行（Human-in-the-Loop）

    当 Agent 调用 ask_user 工具后，运行中断等待用户回答。
    前端收集用户答案后调用此端点恢复执行。
    """
    # 验证会话
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 获取 Agent Service
    agent_pool = get_agent_pool()
    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    user_sandbox_id = user_sandbox.sandbox_id if user_sandbox else None

    try:
        agent_service = await agent_pool.get_or_create(
            user_id=user_id,
            session_id=user_id,
            chat_session_id=chat_session_id,
            db=db,
            model_id=session.model_id,
            sandbox_id=user_sandbox_id,
        )
    except Exception as e:
        logger.error("Agent 获取失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Agent 获取失败，请稍后重试")

    # 验证中断状态
    if not agent_service.has_pending_interrupt(request.interrupt_id):
        raise HTTPException(status_code=409, detail="没有待处理的中断（可能已过期或已恢复），或中断 ID 不匹配")

    # 创建 per-run 取消令牌
    cancel_token = asyncio.Event()
    agent_service.cancel_token = cancel_token

    async def event_generator():
        async for chunk in _sse_with_heartbeat(
            agent_service.resume_agui(
                interrupt_id=request.interrupt_id,
                answers=request.answers,
            ),
            session_id=chat_session_id,
            error_message="服务暂时不可用，请稍后重试",
        ):
            yield chunk

    session.updated_at = now_naive()
    db.commit()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )



# 🆕 辅助函数：广播事件给所有订阅者
async def _broadcast_to_subscribers(round_id: str, event: dict):
    """向所有订阅该轮次的客户端广播事件（AG-UI 格式）"""
    with _round_subscribers_lock:
        subscribers = list(_round_subscribers.get(round_id, []))

    if not subscribers:
        return

    failed_queues: list[asyncio.Queue] = []
    for queue in subscribers:
        try:
            await queue.put(event)
        except Exception as e:
            failed_queues.append(queue)
            print(f"⚠️ 广播事件失败: {e}")

    if not failed_queues:
        return

    with _round_subscribers_lock:
        active = _round_subscribers.get(round_id)
        if not active:
            return
        for queue in failed_queues:
            if queue in active:
                active.remove(queue)
        if not active:
            del _round_subscribers[round_id]



# 🆕 辅助函数：清理订阅者
def _cleanup_subscribers(round_id: str):
    """清理已完成轮次的订阅者"""
    removed = False
    with _round_subscribers_lock:
        if round_id in _round_subscribers:
            del _round_subscribers[round_id]
            removed = True
    if removed:
        print(f"🧹 已清理轮次 {round_id} 的订阅者")


@router.get("/{chat_session_id}/round/{round_id}/subscribe")
async def subscribe_to_round(
    chat_session_id: str,
    round_id: str,
    last_step: int = Query(0, description="客户端已收到的最后步骤号（已棄用，保留兼容）"),
    last_sequence: int = Query(0, description="客户端已收到的最后事件序列号（AG-UI 重放機制）"),
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """订阅轮次更新（SSE）- 用于断线恢复，使用 AG-UI 事件格式
    
    AG-UI 重連機制：
    1. 客戶端通過 lastSequence 參數告知最後收到的事件序列號
    2. 服務端從 agui_events 表重放 lastSequence 之後的所有事件
    3. 然後註冊為訂閱者接收後續實時事件
    
    Args:
        chat_session_id: 會話 ID
        round_id: 輪次 ID（AG-UI runId）
        last_sequence: 客戶端最後收到的事件序列號（0 表示從頭重放）
        user_id: 用戶 ID
    """
    # 验证会话
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 验证轮次
    round_obj = db.query(Round).filter(Round.id == round_id, Round.session_id == chat_session_id).first()
    if not round_obj:
        raise HTTPException(status_code=404, detail="轮次不存在")

    # 創建 HistoryService 實例用於消息快照構建
    history_service = HistoryService(db)
    
    # 创建订阅者队列
    subscriber_queue = asyncio.Queue()

    async def subscribe_generator():
        try:
            now_ms = lambda: int(datetime.now().timestamp() * 1000)
            
            # === 1. 重放錯過的事件（AG-UI 核心機制）===
            replayed_events = await history_service.replay_run_events(round_id, last_sequence)

            # 檢查重放事件中是否已包含 RUN_FINISHED
            # 由于 replay_run_events 返回的是 dict list，需要检查 type 字段
            has_run_finished_in_replay = any(
                e.get("type") == EventType.RUN_FINISHED.value for e in replayed_events
            )

            if replayed_events:
                 print(f"📤 重放 {len(replayed_events)} 個錯過的事件 (sequence > {last_sequence})")
                 for event_data in replayed_events:
                     yield event_encoder.encode_dict(event_data)

            # === 2. 重新查詢輪次狀態（修復競態條件）===
            # 在重放事件後刷新數據庫對象，獲取最新狀態
            db.refresh(round_obj)

            if round_obj.status in ("completed", "failed", "interrupted", "resumed"):
                # 如果重放事件中已包含 RUN_FINISHED，直接返回不重複發送
                if has_run_finished_in_replay:
                    print(f"✅ 重放事件已包含 RUN_FINISHED，輪次 {round_id} 訂閱正常結束")
                    return

                # 發送 RUN_FINISHED（重放中沒有時才發送）
                print(f"📤 輪次 {round_id} 已完成但重放中無 RUN_FINISHED，補發完成事件")
                # 使用 HistoryService 構建 MESSAGES_SNAPSHOT
                messages = history_service.build_messages_snapshot(round_id)
                # 发送 MESSAGES_SNAPSHOT
                snapshot_event = MessagesSnapshotEvent(messages=messages)
                yield event_encoder.encode(snapshot_event)

                # 发送终态事件
                if round_obj.status == "failed":
                    # failed 路径：仅发 RUN_ERROR 后结束，避免误标为 interrupt
                    error_event = RunErrorEvent(
                         message="Run failed (status=failed)",
                         code="RUN_FAILED"
                    )
                    yield event_encoder.encode(error_event)
                    return

                if round_obj.status == "interrupted":
                    # interrupted 路径：补发 RUN_FINISHED(outcome=interrupt) 并携带中断详情
                    import json as _json
                    _interrupt_details = None
                    if round_obj.interrupt_payload:
                        try:
                            _interrupt_details = InterruptDetails(**_json.loads(round_obj.interrupt_payload))
                        except Exception:
                            _interrupt_details = None
                    complete_event = RunFinishedEvent(
                        threadId=chat_session_id,
                        runId=round_id,
                        result={
                            "finalResponse": round_obj.final_response or "",
                            "stepCount": round_obj.step_count,
                        },
                        outcome="interrupt",
                        interrupt=_interrupt_details,
                    )
                    yield event_encoder.encode(complete_event)
                    return

                if round_obj.status == "resumed":
                    # resumed 路径：该轮次曾被中断，已由新 run 接管
                    # 语义上仍是 interrupt，不应标为 success
                    complete_event = RunFinishedEvent(
                        threadId=chat_session_id,
                        runId=round_id,
                        result={
                            "finalResponse": round_obj.final_response or "",
                            "stepCount": round_obj.step_count,
                            "reason": "resumed_by_new_run",
                        },
                        outcome="interrupt",
                    )
                    yield event_encoder.encode(complete_event)
                    return

                # completed 路径
                complete_event = RunFinishedEvent(
                    threadId=chat_session_id,
                    runId=round_id,
                    result={
                        "finalResponse": round_obj.final_response or "",
                        "stepCount": round_obj.step_count,
                    },
                    outcome="success"
                )
                yield event_encoder.encode(complete_event)
                return  # 輪次已完成，結束訂閱

            # === 3. 輪次仍在運行，註冊為訂閱者 ===
            with _round_subscribers_lock:
                if round_id not in _round_subscribers:
                    _round_subscribers[round_id] = []
                _round_subscribers[round_id].append(subscriber_queue)
                subscriber_count = len(_round_subscribers[round_id])
            print(f"📡 新订阅者已注册到轮次 {round_id}，当前订阅者数: {subscriber_count}")

            # 获取配置
            settings = get_settings()

            # 心跳任务（使用 CUSTOM 事件）
            async def heartbeat():
                try:
                    while True:
                        await asyncio.sleep(settings.sse_heartbeat_interval)
                        heartbeat_event = CustomEvent(
                            name="heartbeat",
                            value={"timestamp": now_ms()}
                        )
                        await subscriber_queue.put(heartbeat_event.model_dump(by_alias=True))
                except asyncio.CancelledError:
                    pass

            heartbeat_task = asyncio.create_task(heartbeat())

            try:
                # 监听队列中的事件
                while True:
                    # subscriber_queue 中的 event 已经是 dict (from broadcast)
                    event_dict = await asyncio.wait_for(subscriber_queue.get(), timeout=settings.sse_subscribe_timeout)
                    yield event_encoder.encode_dict(event_dict)

                    # 如果是 RUN_FINISHED 或 RUN_ERROR 事件，结束订阅
                    if event_dict.get("type") in (EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value):
                        break
            except asyncio.TimeoutError:
                # 超时，发送 RUN_ERROR 事件
                error_event = RunErrorEvent(
                    message="订阅超时",
                    code="TIMEOUT"
                )
                yield event_encoder.encode(error_event)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        finally:
            # 移除订阅者
            removed = False
            remaining = 0
            with _round_subscribers_lock:
                queues = _round_subscribers.get(round_id)
                if queues and subscriber_queue in queues:
                    queues.remove(subscriber_queue)
                    removed = True
                    remaining = len(queues)
                    if not queues:
                        del _round_subscribers[round_id]
            if removed:
                print(f"📡 订阅者已从轮次 {round_id} 移除，剩余订阅者数: {remaining}")

    return StreamingResponse(
        subscribe_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{chat_session_id}/abort")
async def abort_chat(
    chat_session_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """中止正在進行的 Agent 執行

    1. 設置 cancel_token → Agent 在下一個檢查點退出
    2. 取消後台 runner task → 立即中斷 LLM/工具等待
    3. 若 Agent 已不存在但 round 仍 running → 直接更新 DB
    """
    # 驗證會話
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="會話不存在")

    cancelled = False

    # 1. 通過 cancel_token 通知 Agent（在下一個檢查點生效）
    agent_pool = get_agent_pool()
    agent_service = agent_pool.get(chat_session_id)
    if agent_service and agent_service.cancel_token:
        agent_service.cancel_token.set()
        cancelled = True

    # 2. 取消後台 runner task（立即中斷正在進行的 await）
    runner = _active_runners.get(chat_session_id)
    if runner and not runner.done():
        runner.cancel()
        cancelled = True

    # 3. 兜底：若 Agent 已死但 round 仍 running，直接更新 DB
    if not cancelled:
        running_rounds = db.query(Round).filter(
            Round.session_id == chat_session_id,
            Round.status == "running",
        ).all()
        for r in running_rounds:
            r.status = "failed"
            r.final_response = "Aborted by user"
        if running_rounds:
            db.commit()
            cancelled = True
            # 通知訂閱者 run 已結束
            for r in running_rounds:
                error_event = RunErrorEvent(message="Aborted by user", code="USER_ABORT")
                event_dict = error_event.model_dump(by_alias=True, exclude_none=True)
                await _broadcast_to_subscribers(r.id, event_dict)
                _cleanup_subscribers(r.id)

    if cancelled:
        logger.info("已觸發取消: session=%s", chat_session_id)
        return {"status": "cancelled"}
    else:
        raise HTTPException(status_code=409, detail="該會話沒有正在進行的執行")


# =============================================================================
# 已棄用：/message/agui 路由已合併到 /message/stream
# 主路由現在直接使用 chat_agui() 透傳 AG-UI 事件，無需單獨的簡化路由
# =============================================================================
