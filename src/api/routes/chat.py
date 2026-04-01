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
from src.api.schemas.chat import SendMessageRequest
from src.api.services.agent_pool_service import get_agent_pool
from src.api.models.user_sandbox import UserSandbox
from src.api.config import get_settings
import logging
import time
from datetime import datetime
from src.api.utils.timezone import now_naive
# AG-UI 事件類型統一從 Agent 層導入
from src.agent.schema.agui_events import AGUIEvent, RunStartedEvent, CustomEvent, EventType, RunErrorEvent, RunFinishedEvent, MessagesSnapshotEvent
from src.api.utils.agui_encoder import EventEncoder
import asyncio
import json
import uuid

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

    # 使用 AgentPoolService 管理 Agent 實例
    agent_pool = get_agent_pool()
    
    # 定期清理過期 Agent（節流：每60秒最多一次）
    global _last_cleanup_time
    now = time.time()
    if now - _last_cleanup_time > 60:
        await agent_pool.cleanup_expired_async()
        _last_cleanup_time = now

    # 獲取或創建 Agent Service（sandbox_id 從 UserSandbox 表讀取）
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
        logger.error(
            "Agent 初始化失敗: %s: %s", type(e).__name__, e, exc_info=True,
        )

        error_msg = f"Agent 初始化失敗: {type(e).__name__}: {str(e)}"
        if "api_key" in str(e).lower() or "apikey" in str(e).lower():
            error_msg += "\n\n💡 提示：請檢查 .env 文件中的 LLM_API_KEY 配置是否正確"
        raise HTTPException(status_code=500, detail=error_msg)

    # 標題生成任務（如果是第一條消息）
    title_generation_task = None
    from src.api.models.round import Round
    from src.api.models.database import SessionLocal

    round_count = db.query(Round).filter(Round.session_id == chat_session_id).count()
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

    # 定義事件生成器
    async def event_generator():
        nonlocal title_generation_task
        
        # 用於追蹤 run_id（用於訂閱者廣播）
        current_run_id: str | None = None
        event_queue = asyncio.Queue()
        
        try:
            # 透傳 Agent 的 AG-UI 事件流
            async for event in agent_service.chat_agui(
                user_content=request.content,
            ):
                # 提取 run_id（用於訂閱者管理）
                if hasattr(event, 'run_id') and event.run_id:
                    current_run_id = event.run_id
                
                # 序列化事件
                event_str = event_encoder.encode(event)

                # 广播给订阅者（AG-UI 格式）
                if current_run_id:
                    # 获取事件字典用于广播
                    # 注意：广播需要字典格式而非字符串
                    event_dict = event.model_dump(by_alias=True, exclude_none=True)
                    await _broadcast_to_subscribers(current_run_id, event_dict)

                yield event_str

                # 檢測運行結束事件
                if event.type == EventType.RUN_FINISHED:
                    # 等待標題生成完成
                    if title_generation_task:
                        try:
                            title = await title_generation_task
                            if title:
                                # 發送標題更新事件
                                title_event = CustomEvent(
                                    name="title_updated",
                                    value={"sessionId": chat_session_id, "title": title},
                                )
                                yield event_encoder.encode(title_event)
                        except Exception as e:
                            print(f"⚠️  等待標題生成失敗: {e}")
                    
                    # 清理訂閱者
                    if current_run_id:
                        _cleanup_subscribers(current_run_id)
                        
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            print(f"\n❌ AG-UI 事件流錯誤: {str(e)}\n{error_detail}")

            # 发送错误事件
            try:
                error_event = RunErrorEvent(
                    message=str(e),
                    code=type(e).__name__
                )
                yield event_encoder.encode(error_event)
            except Exception as inner_e:
                print(f"❌ 错误事件生成失败: {inner_e}")
                # 兜底简单的 JSON
                fallback_json = json.dumps({
                    "type": EventType.RUN_ERROR.value,
                    "message": str(e),
                    "code": type(e).__name__,
                    "timestamp": datetime.now().timestamp() * 1000
                })
                yield f"data: {fallback_json}\n\n"

            # 清理訂閱者
            if current_run_id:
                _cleanup_subscribers(current_run_id)

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



# 🆕 辅助函数：广播事件给所有订阅者
async def _broadcast_to_subscribers(round_id: str, event: dict):
    """向所有订阅该轮次的客户端广播事件（AG-UI 格式）"""
    if round_id in _round_subscribers:
        for queue in _round_subscribers[round_id]:
            try:
                await queue.put(event)
            except Exception as e:
                print(f"⚠️ 广播事件失败: {e}")



# 🆕 辅助函数：清理订阅者
def _cleanup_subscribers(round_id: str):
    """清理已完成轮次的订阅者"""
    if round_id in _round_subscribers:
        del _round_subscribers[round_id]
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
    from src.api.models.round import Round
    from src.api.services.history_service import HistoryService

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

            if round_obj.status in ("completed", "failed"):
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
                # outcome: success（正常完成）/ interrupt（异常终止）
                if round_obj.status == "failed":
                    # failed 路径：先发 RUN_ERROR（携带错误详情），再发 RUN_FINISHED（终态收敛）
                    error_event = RunErrorEvent(
                         message="Run failed (status=failed)",
                         code="RUN_FAILED"
                    )
                    yield event_encoder.encode(error_event)
                    complete_event = RunFinishedEvent(
                        threadId=chat_session_id,
                        runId=round_id,
                        result={
                            "finalResponse": round_obj.final_response or "",
                            "stepCount": round_obj.step_count,
                        },
                        outcome="interrupt"
                    )
                    yield event_encoder.encode(complete_event)
                    return

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
            if round_id not in _round_subscribers:
                _round_subscribers[round_id] = []
            _round_subscribers[round_id].append(subscriber_queue)
            print(f"📡 新订阅者已注册到轮次 {round_id}，当前订阅者数: {len(_round_subscribers[round_id])}")

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
            if round_id in _round_subscribers and subscriber_queue in _round_subscribers[round_id]:
                _round_subscribers[round_id].remove(subscriber_queue)
                print(f"📡 订阅者已从轮次 {round_id} 移除，剩余订阅者数: {len(_round_subscribers.get(round_id, []))}")

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

    設置 cancel_token，Agent 在下一個檢查點（step 開始 / 工具執行前）退出，
    並通過仍然活躍的 SSE 連接推送 RUN_FINISHED(outcome=interrupt)。
    """
    # 驗證會話
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="會話不存在")

    agent_pool = get_agent_pool()
    agent_service = agent_pool.get(chat_session_id)
    if not agent_service:
        raise HTTPException(status_code=404, detail="該會話沒有正在執行的 Agent")

    if agent_service.cancel_token:
        agent_service.cancel_token.set()
        logger.info("已觸發取消: session=%s", chat_session_id)
        return {"status": "cancelled"}
    else:
        raise HTTPException(status_code=409, detail="該會話沒有正在進行的執行")


# =============================================================================
# 已棄用：/message/agui 路由已合併到 /message/stream
# 主路由現在直接使用 chat_agui() 透傳 AG-UI 事件，無需單獨的簡化路由
# =============================================================================
