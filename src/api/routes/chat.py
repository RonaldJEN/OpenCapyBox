"""对话 API - AG-UI 协议实现

AG-UI 協議的瀏覽器刷新/重連機制：
1. 所有事件在發送時同時持久化到 agui_events 表
2. 客戶端重連時通過 lastSequence 參數告知最後收到的事件序號
3. 服務端重放 lastSequence 之後的所有事件
4. 使用 MESSAGES_SNAPSHOT 恢復歷史，然後繼續流式推送

重構說明 (v2):
- 主路由只做 Web HTTP/SSE adapter，run 创建通过 TurnOrchestrator 下沉
- 移除了 ~350 行的手動事件轉換代碼
- 事件持久化由 AgentService 內部處理
- 保留訂閱者廣播和標題生成功能
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as DBSession
import inspect
from src.api.models.database import get_db
from src.api.deps import get_current_user
from src.api.models.session import Session
from src.api.models.user_run_lock import UserRunLock
from src.api.models.run_cancel_request import RunCancelRequest
from src.api.schemas.chat import SendMessageRequest, ResumeRequest
from src.api.services.agent_pool_service import get_agent_pool
from src.api.services.auth_service import enforce_token_limits
from src.api.models.user_sandbox import UserSandbox
from src.api.config import get_settings
import logging
import time
from datetime import datetime
from src.api.utils.timezone import now_naive
# AG-UI 事件類型統一從 Agent 層導入
from src.agent.schema.agui_events import CustomEvent, EventType, RunErrorEvent
from src.agent.schema.run_context import (
    RequestedReasoningContext,
    resolve_reasoning_selection,
)
from src.api.utils.agui_encoder import EventEncoder
from src.api.services.agent_service import (
    DuplicateRoundError,
    InvalidInteractionResponseError,
)
from src.api.services.agent_interaction_service import InteractionConflictError
from src.api.services.agui_event_bus import AguiEventBus, get_agui_event_bus
from src.api.services.run_completion_service import RunCompletionService
from src.api.services.run_cancel_service import get_run_cancel_service
from src.api.services.run_coordinator import get_run_coordinator
from src.api.services.running_rounds import get_main_running_round
from src.api.services.subagent_graph_service import get_subagent_graph_service
from src.api.services.turn_orchestrator import TurnExecution, get_turn_orchestrator
from src.api.services.web_chat_adapter import WebCancelAdapter, WebChatAdapter, WebResumeAdapter
from src.api.services.workspace_service import WorkspaceError
from src.api.services.model_access_service import assert_user_can_access_model, resolve_default_model_for_user
from src.api.models.round import Round
from src.api.services.history_service import HistoryService
from src.api.models.database import SessionLocal
import asyncio
import json
import traceback
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)
router = APIRouter()
event_encoder = EventEncoder()

# 上次清理時間（節流：每3600秒最多清理一次）
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
_agui_event_bus = get_agui_event_bus()
_run_cancel_service = get_run_cancel_service()
_run_coordinator = get_run_coordinator()
_turn_orchestrator = get_turn_orchestrator()

ABORT_OUTCOME_WARNING = (
    "本地执行已停止，但这不能撤销已经发送到远端 MCP 的请求；"
    "远端副作用可能已经发生，请先确认外部状态再决定是否重试。"
)


def _resolve_session_model_for_user(db: DBSession, session: Session, user_id: str) -> str:
    """Validate and resolve a session model for the current user."""
    if session.model_id:
        if not isinstance(session, Session):
            return str(session.model_id)
        config = assert_user_can_access_model(db, user_id, session.model_id)
        return config.id
    config = resolve_default_model_for_user(db, user_id)
    session.model_id = config.id
    session.updated_at = now_naive()
    db.commit()
    db.refresh(session)
    return config.id


def _validate_turn_reasoning_request(
    db: DBSession,
    *,
    user_id: str,
    model_id: str,
    request: SendMessageRequest,
) -> None:
    """Validate a per-turn reasoning selection against the exact session model."""
    if request.thinking_mode is None and request.reasoning_effort is None:
        return
    config = assert_user_can_access_model(db, user_id, model_id)
    try:
        resolve_reasoning_selection(
            RequestedReasoningContext(
                mode=request.thinking_mode or "provider_default",
                effort=request.reasoning_effort,
            ),
            provider=config.provider,
            supports_reasoning_control=config.supports_reasoning_control,
            supported_reasoning_efforts=config.supported_reasoning_efforts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
_web_chat_adapter = WebChatAdapter()
_web_resume_adapter = WebResumeAdapter()
_web_cancel_adapter = WebCancelAdapter()

# 活躍的後台 Agent 運行任務（session_id -> producer task）
# Agent 執行與 SSE 連接解耦：瀏覽器斷開後 Agent 繼續在後台運行
_active_runners: dict[str, asyncio.Task] = _turn_orchestrator.active_runners

async def _clear_pending_cancel_request(
    db: DBSession,
    *,
    user_id: str,
    session_id: str,
) -> bool:
    """Append-only cancel rows are audit-only, so stale rows do not kill new runs."""
    db.rollback()
    return False


async def _run_in_new_session(
    fn,
    *,
    label: str,
    **kwargs,
) -> bool:
    """在独立 DB Session 中执行操作（通用 wrapper）。

    用于 SSE producer finally 等需要脱离请求级 Session 的场景。
    fn 签名: async def fn(db: DBSession, **kwargs) -> bool
    """
    try:
        with SessionLocal() as db:
            return await fn(db, **kwargs)
    except Exception:
        logger.warning("%s 失败: %s", label, kwargs, exc_info=True)
        return False


async def _complete_cancel_request_in_new_session(
    *,
    user_id: str,
    session_id: str,
) -> bool:
    """运行结束后将取消请求收敛为 completed（若存在）。"""

    async def _op(db, *, user_id, session_id):
        def _do():
            return bool(_run_cancel_service.mark_completed(
                db,
                user_id=user_id,
                session_id=session_id,
            ))

        return await _with_db_retry(_do, rollback=db.rollback)

    return await _run_in_new_session(
        _op, label="完成 cancel request", user_id=user_id, session_id=session_id,
    )


def _has_cancel_activity_since(
    db: DBSession,
    *,
    user_id: str,
    session_id: str,
    started_at: datetime,
) -> bool:
    """檢查 run 啟動後是否發生過 cancel 活動。"""
    try:
        row = (
            db.query(RunCancelRequest)
            .filter(
                RunCancelRequest.session_id == session_id,
                RunCancelRequest.user_id == user_id,
                RunCancelRequest.requested_at > started_at,
            )
            .order_by(RunCancelRequest.requested_at.desc())
            .first()
        )
        if row is None:
            return False
        requested_at = getattr(row, "requested_at", None)
        return isinstance(requested_at, datetime) and requested_at > started_at
    finally:
        db.rollback()


def _has_cancel_activity_since_in_new_session(
    *,
    user_id: str,
    session_id: str,
    started_at: datetime,
) -> bool:
    with SessionLocal() as db:
        return _has_cancel_activity_since(
            db,
            user_id=user_id,
            session_id=session_id,
            started_at=started_at,
        )


def _is_retryable_db_error(exc: OperationalError) -> bool:
    """判斷是否為 PostgreSQL 瞬時可重試錯誤（死鎖 / 序列化失敗）。"""
    pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
    # 40001 = serialization_failure, 40P01 = deadlock_detected
    return pgcode in ("40001", "40P01")


async def _with_db_retry(
    fn: Callable[[], Any | Awaitable[Any]],
    *,
    max_retries: int = 5,
    retry_interval: float = 0.1,
    rollback: Callable[[], None] | None = None,
) -> Any:
    """PostgreSQL 瞬時寫衝突（死鎖 / 序列化失敗）的通用重試 wrapper。

    Args:
        fn: 要執行的 callable（包含 DB 操作 + commit），可返回同步值或 awaitable。
        max_retries: 最大嘗試次數。
        retry_interval: 每次重試前的等待秒數。
        rollback: 遇到 OperationalError 時的回滾 callable（可選）。
    Returns:
        fn 的返回值。
    Raises:
        最後一次嘗試的異常（如果全部重試失敗）。
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            result = fn()
            if hasattr(result, "__await__"):
                return await result
            return result
        except OperationalError as exc:
            last_exc = exc
            if rollback:
                rollback()
            if _is_retryable_db_error(exc) and attempt + 1 < max_retries:
                await asyncio.sleep(retry_interval)
                continue
            raise
        except Exception:
            if rollback:
                rollback()
            raise
    raise last_exc  # type: ignore[misc]


def _format_datetime(value: datetime | None) -> str | None:
    """统一格式化时间字段，便于管理接口返回。"""
    if not value:
        return None
    return value.isoformat()

async def _acquire_user_run_lock(*, user_id: str, session_id: str) -> str | None:
    """嘗試獲取用戶運行 slot。

    使用獨立 DB Session，避免 rollback 污染請求級 Session 中已載入的 ORM 物件。
    返回 lock_id 表示獲取成功；返回 None 表示同 session 已在跑或用户并发已达上限。
    """
    _run_coordinator.session_factory = SessionLocal
    _run_coordinator.settings_provider = get_settings
    return await _run_coordinator.acquire_user_run_lock(user_id=user_id, session_id=session_id)


def _cleanup_orphaned_rounds(
    db: DBSession,
    *,
    user_id: str,
    session_id: str | None = None,
) -> int:
    """恢复安全 continuation，并将其余 worker 崩溃 Round 标记为 failed。

    只在鎖心跳已過期且鎖被回收後調用 — 此時可確定該 session 的原 worker 已死。
    """
    _run_coordinator.session_factory = SessionLocal
    _run_coordinator.settings_provider = get_settings
    return _run_coordinator.cleanup_orphaned_rounds(
        db,
        user_id=user_id,
        session_id=session_id,
    )


async def _release_user_run_lock(
    db: DBSession,
    *,
    user_id: str,
    lock_id: str | None = None,
    session_id: str | None = None,
) -> bool:
    """釋放用戶運行鎖。

    若傳入 lock_id，僅在鎖歸屬於該 lock_id 時釋放，避免舊請求誤刪新鎖。
    若未傳 lock_id 但傳入 session_id，僅在鎖歸屬於該會話時釋放。
    遇到 PostgreSQL 瞬時寫衝突時最多重試 5 次。
    """
    _run_coordinator.session_factory = SessionLocal
    _run_coordinator.settings_provider = get_settings
    return await _run_coordinator.release_user_run_lock(
        db,
        user_id=user_id,
        lock_id=lock_id,
        session_id=session_id,
    )


async def _release_user_run_lock_in_new_session(
    *,
    user_id: str,
    lock_id: str | None = None,
    session_id: str | None = None,
) -> bool:
    """在獨立 DB Session 中釋放鎖。

    用於 SSE 背景 producer finally，避免依賴請求作用域的 DB Session。
    """
    _run_coordinator.session_factory = SessionLocal
    _run_coordinator.settings_provider = get_settings
    return await _run_coordinator.release_user_run_lock_in_new_session(
        user_id=user_id,
        lock_id=lock_id,
        session_id=session_id,
    )


async def _sse_from_turn_execution(
    execution: TurnExecution,
    *,
    on_run_finished: Callable[[str | None], Awaitable[AsyncIterator[str]]] | None = None,
    on_stream_finished: Callable[[str | None], Awaitable[AsyncIterator[str]]] | None = None,
    error_message: str | None = None,
):
    """Render an orchestrator-managed run stream as Web SSE.

    The run producer, cancel registry and lock cleanup live in TurnOrchestrator.
    This adapter only handles heartbeat, encoding, terminal callbacks, and
    disconnecting the current Web consumer without cancelling the run task.
    """
    settings = get_settings()
    current_run_id = execution.handle.run_id
    run_completed = False
    iterator = execution.event_source.__aiter__()
    next_event_task = asyncio.create_task(iterator.__anext__())
    heartbeat_task = asyncio.create_task(asyncio.sleep(settings.sse_heartbeat_interval))

    def _event_type(event):
        if isinstance(event, dict):
            return event.get("type")
        return getattr(event, "type", None)

    def _encode_event(event):
        if isinstance(event, dict):
            return event_encoder.encode_dict(event)
        return event_encoder.encode(event)

    try:
        while True:
            done, _ = await asyncio.wait(
                {next_event_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                yield event_encoder.encode(CustomEvent(
                    name="heartbeat",
                    value={"timestamp": int(datetime.now().timestamp() * 1000)},
                ))
                heartbeat_task = asyncio.create_task(asyncio.sleep(settings.sse_heartbeat_interval))

            if next_event_task not in done:
                continue

            try:
                event = next_event_task.result()
            except StopAsyncIteration:
                if on_stream_finished:
                    async for extra in await on_stream_finished(current_run_id):
                        yield extra
                break

            event_type = _event_type(event)
            if isinstance(event, dict) and event.get("runId"):
                current_run_id = event.get("runId")
            elif hasattr(event, "run_id") and event.run_id:
                current_run_id = event.run_id

            yield _encode_event(event)

            if event_type == EventType.RUN_FINISHED or event_type == EventType.RUN_FINISHED.value:
                run_completed = True
                if on_run_finished:
                    async for extra in await on_run_finished(current_run_id):
                        yield extra
                break

            if event_type == EventType.RUN_ERROR or event_type == EventType.RUN_ERROR.value:
                run_completed = True
                break

            next_event_task = asyncio.create_task(iterator.__anext__())
    except Exception as e:
        run_completed = True
        logger.error("orchestrated AG-UI 事件流錯誤: %s\n%s", e, traceback.format_exc())
        if isinstance(e, DuplicateRoundError):
            display_msg = e.existing_round_id
            display_code = "ROUND_IN_PROGRESS"
        else:
            display_msg = error_message or str(e)
            display_code = "INTERNAL_ERROR" if error_message else type(e).__name__
        yield event_encoder.encode(RunErrorEvent(message=display_msg, code=display_code))
    finally:
        heartbeat_task.cancel()
        if not next_event_task.done():
            next_event_task.cancel()
            try:
                await next_event_task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            except Exception:
                logger.debug("closing orchestrated SSE event iterator raised", exc_info=True)
        if not run_completed:
            logger.info(
                "SSE 連接斷開，orchestrated Agent 繼續後台運行 (session=%s, run=%s)",
                execution.handle.session_id,
                current_run_id,
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


class _AgentInitTimedOut(Exception):
    """Agent 初始化超过总墙钟期限。"""
    pass


async def _agent_init_heartbeats(
    init_task: asyncio.Task,
    *,
    heartbeat_interval: float,
    timeout_seconds: float,
):
    """Yield SSE heartbeats while enforcing one total Agent-init deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(timeout_seconds), 0.01)
    interval = max(float(heartbeat_interval), 0.01)
    while not init_task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise _AgentInitTimedOut(
                f"Agent 初始化超过 {int(timeout_seconds)} 秒，已停止本次请求"
            )
        done, _ = await asyncio.wait(
            {init_task},
            timeout=min(interval, remaining),
        )
        if done:
            break
        if loop.time() >= deadline:
            raise _AgentInitTimedOut(
                f"Agent 初始化超过 {int(timeout_seconds)} 秒，已停止本次请求"
            )
        yield CustomEvent(
            name="heartbeat",
            value={"timestamp": int(datetime.now().timestamp() * 1000)},
        )


def _consume_agent_init_task(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Agent 初始化后台任务失败")


async def _cancel_agent_init_task(
    init_task: asyncio.Task,
    *,
    drain_timeout_seconds: float = 5.0,
) -> None:
    """Cancel init without letting a cancellation-hostile dependency hang SSE."""
    if init_task.done():
        _consume_agent_init_task(init_task)
        return
    init_task.cancel()
    done, _ = await asyncio.wait(
        {init_task},
        timeout=max(float(drain_timeout_seconds), 0.01),
    )
    if done:
        _consume_agent_init_task(init_task)
        return
    logger.error("Agent 初始化任务在取消后仍未退出，转为后台回收")
    init_task.add_done_callback(_consume_agent_init_task)


def _start_agent_init(
    *,
    user_id: str,
    chat_session_id: str,
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
        cleanup_result = agent_pool.cleanup_expired_async()
        if inspect.isawaitable(cleanup_result):
            _cleanup_task = asyncio.create_task(cleanup_result)
            _cleanup_task.add_done_callback(lambda t: logger.error("Agent 清理異常: %s", t.exception()) if not t.cancelled() and t.exception() else None)
            _last_cleanup_time = now

    return asyncio.create_task(agent_pool.get_or_create(
        user_id=user_id,
        session_id=user_id,
        chat_session_id=chat_session_id,
        db=None,
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


async def _acquire_lock_and_clear_cancel(
    db: DBSession,
    *,
    user_id: str,
    session_id: str,
) -> str:
    """獲取用戶運行鎖並清理遺留的取消請求（send/resume 共用前置邏輯）。

    Returns:
        lock_id（成功獲取的鎖 ID）。
    Raises:
        HTTPException 503（鎖異常）/ 429（已有運行中任務）。
    """
    try:
        lock_id = await _acquire_user_run_lock(user_id=user_id, session_id=session_id)
    except Exception:
        logger.error("獲取用戶運行鎖時發生異常: user=%s session=%s", user_id, session_id, exc_info=True)
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")
    if not lock_id:
        limit = max(int(getattr(get_settings(), "agent_user_concurrency_limit", 1) or 1), 1)
        raise HTTPException(
            status_code=429,
            detail=f"当前有正在运行的任务，运行任务数已达上限（{limit}），请等待完成后再发送新消息",
        )

    # append-only 审计行不承担投递职责，陈旧 requested 不应误杀新 run。
    # 失败时仅 warning；requested_after 会继续防止旧取消请求误伤后续 run。
    try:
        await _clear_pending_cancel_request(db, user_id=user_id, session_id=session_id)
    except Exception:
        logger.warning(
            "清理取消请求失败（继续执行）: user=%s session=%s",
            user_id, session_id, exc_info=True,
        )

    return lock_id


def _make_sse_response(
    event_generator: AsyncIterator[str],
    *,
    db: DBSession,
    session,
    user_id: str,
    session_id: str,
) -> StreamingResponse:
    """更新會話活躍時間並返回 SSE StreamingResponse（send/resume 共用收尾邏輯）。"""
    try:
        session.updated_at = now_naive()
        db.commit()
    except Exception:
        db.rollback()
        logger.warning(
            "更新會話活躍時間失敗（繼續執行）: user=%s session=%s",
            user_id, session_id, exc_info=True,
        )

    return StreamingResponse(
        event_generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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

    turn = _web_chat_adapter.normalize_send(
        session_id=chat_session_id,
        user_id=user_id,
        request=request,
    )

    enforce_token_limits(db, user_id=user_id)

    # 用戶級並發限制 + 清理遺留取消請求
    lock_id = await _acquire_lock_and_clear_cancel(db, user_id=user_id, session_id=chat_session_id)
    run_guard_started_at = now_naive()

    # 幂等性保證依賴 DB 層 UniqueConstraint（history_service.create_round 的 IntegrityError 兜底）
    # 無需在此做 SELECT fast-path：TOCTOU 窗口使其不可靠，省掉的只是一次 Agent 初始化嘗試

    try:
        # 預讀 sandbox_id 和 round_count（輕量查詢，不會超時）
        user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
        user_sandbox_id = user_sandbox.sandbox_id if user_sandbox else None
        round_count = db.query(Round).filter(Round.session_id == chat_session_id).count()
        model_id = _resolve_session_model_for_user(db, session, user_id)
        _validate_turn_reasoning_request(
            db,
            user_id=user_id,
            model_id=model_id,
            request=request,
        )
    except Exception:
        await _release_user_run_lock_in_new_session(
            user_id=user_id,
            lock_id=lock_id,
            session_id=chat_session_id,
        )
        raise

    # 定義事件生成器（Agent 初始化移入 generator 內部，讓 SSE 響應頭先返回，心跳保活撐住連接）
    async def event_generator():
        _entered_sse = False  # 追蹤 producer 是否已由 TurnOrchestrator 接管
        settings = get_settings()

        # --- Agent 初始化（帶心跳保活，防止 HAProxy 30s 超時）---
        init_task = _start_agent_init(
            user_id=user_id,
            chat_session_id=chat_session_id,
            model_id=model_id,
            sandbox_id=user_sandbox_id,
        )

        try:
            # 在初始化完成前持续发送心跳，但不能无限掩盖卡死。
            async for heartbeat in _agent_init_heartbeats(
                init_task,
                heartbeat_interval=settings.sse_heartbeat_interval,
                timeout_seconds=settings.agent_init_timeout_seconds,
            ):
                yield event_encoder.encode(heartbeat)

            agent_service = _resolve_agent_init(init_task)
        except _AgentInitTimedOut as e:
            logger.error(
                "Agent 初始化超时: user=%s session=%s timeout=%ss",
                user_id,
                chat_session_id,
                settings.agent_init_timeout_seconds,
            )
            yield event_encoder.encode(RunErrorEvent(message=str(e), code="AGENT_INIT_TIMEOUT"))
            await _release_user_run_lock_in_new_session(
                user_id=user_id,
                lock_id=lock_id,
                session_id=chat_session_id,
            )
            return
        except _AgentInitFailed as e:
            yield event_encoder.encode(RunErrorEvent(message=str(e), code="AGENT_INIT_FAILED"))
            await _release_user_run_lock_in_new_session(
                user_id=user_id,
                lock_id=lock_id,
                session_id=chat_session_id,
            )
            return
        finally:
            await _cancel_agent_init_task(init_task)

        # init-window 防漏：若初始化期間發生 abort，立刻短路，不允許舊請求繼續創建 round。
        if _has_cancel_activity_since_in_new_session(
            user_id=user_id,
            session_id=chat_session_id,
            started_at=run_guard_started_at,
        ):
            logger.info(
                "初始化窗口檢測到較新 cancel activity，短路本次請求: user=%s session=%s",
                user_id,
                chat_session_id,
            )
            yield event_encoder.encode(RunErrorEvent(message="Aborted by user", code="USER_ABORT"))
            await _complete_cancel_request_in_new_session(user_id=user_id, session_id=chat_session_id)
            await _release_user_run_lock_in_new_session(
                user_id=user_id,
                lock_id=lock_id,
                session_id=chat_session_id,
            )
            return

        # --- 標題生成任務（如果是第一條消息）---
        title_generation_task = None
        title_run_id: str | None = None
        title_run_ready = asyncio.Event()
        try:
            if round_count == 0:
                print(f"🏷️  檢測到第一條消息，啟動標題生成任務...")

                async def generate_title_async():
                    try:
                        title_source = _extract_text_for_title(turn.content)
                        title = await agent_service.generate_session_title(title_source)
                        with SessionLocal() as title_db:
                            title_session = title_db.query(Session).filter(Session.id == chat_session_id).first()
                            if title_session:
                                title_session.title = title
                                title_session.updated_at = now_naive()
                                title_db.commit()
                                print(f"✅ 會話標題已保存: {title}")
                                await title_run_ready.wait()
                                if title_run_id:
                                    title_event = CustomEvent(
                                        name="title_updated",
                                        value={
                                            "sessionId": chat_session_id,
                                            "title": title,
                                        },
                                    )
                                    try:
                                        await _agui_event_bus.publish_ephemeral(
                                            title_run_id,
                                            title_event,
                                        )
                                    except Exception:
                                        logger.warning(
                                            "标题已落库但实时通知失败: session=%s run=%s",
                                            chat_session_id,
                                            title_run_id,
                                            exc_info=True,
                                        )
                                return title
                    except Exception as e:
                        print(f"⚠️  標題生成失敗: {e}")
                        return None

                title_generation_task = asyncio.create_task(generate_title_async())

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

            attachment_progress_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            async def on_attachment_progress(value: dict[str, Any]) -> None:
                attachment_progress_queue.put_nowait(value)
                # Give the SSE consumer a scheduling point between adjacent
                # directory references so 1/N, 2/N, ... are emitted in order.
                await asyncio.sleep(0)

            submit_task = asyncio.create_task(_turn_orchestrator.submit_turn(
                turn,
                agent_service=agent_service,
                lock_id=lock_id,
                run_started_at=run_guard_started_at,
                attachment_progress=on_attachment_progress,
            ))
            progress_task = asyncio.create_task(attachment_progress_queue.get())
            heartbeat_task = asyncio.create_task(
                asyncio.sleep(settings.sse_heartbeat_interval)
            )
            try:
                while not submit_task.done():
                    done, _ = await asyncio.wait(
                        {submit_task, progress_task, heartbeat_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if progress_task in done:
                        yield event_encoder.encode(CustomEvent(
                            name="attachment_preparing",
                            value=progress_task.result(),
                        ))
                        progress_task = asyncio.create_task(
                            attachment_progress_queue.get()
                        )
                    if heartbeat_task in done:
                        yield event_encoder.encode(CustomEvent(
                            name="heartbeat",
                            value={"timestamp": int(datetime.now().timestamp() * 1000)},
                        ))
                        heartbeat_task = asyncio.create_task(
                            asyncio.sleep(settings.sse_heartbeat_interval)
                        )

                while not attachment_progress_queue.empty():
                    yield event_encoder.encode(CustomEvent(
                        name="attachment_preparing",
                        value=attachment_progress_queue.get_nowait(),
                    ))
                execution = await submit_task
            except DuplicateRoundError as e:
                yield event_encoder.encode(RunErrorEvent(message=e.existing_round_id, code="ROUND_IN_PROGRESS"))
                return
            except InteractionConflictError as e:
                yield event_encoder.encode(RunErrorEvent(
                    message=str(e),
                    code="INTERACTION_PENDING",
                ))
                return
            except WorkspaceError as e:
                yield event_encoder.encode(RunErrorEvent(
                    message=e.message,
                    code=e.code,
                ))
                return
            except Exception:
                logger.error("submit turn failed before orchestrated SSE started", exc_info=True)
                yield event_encoder.encode(RunErrorEvent(message="Agent 執行失敗", code="INTERNAL_ERROR"))
                return
            finally:
                progress_task.cancel()
                heartbeat_task.cancel()
                if not submit_task.done():
                    submit_task.cancel()
                for task in (progress_task, heartbeat_task, submit_task):
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        if task is not submit_task:
                            logger.debug("附件准备保活任务收尾失败", exc_info=True)

            title_run_id = execution.handle.run_id
            title_run_ready.set()
            _entered_sse = True  # producer 已由 TurnOrchestrator 接管，鎖的釋放由 orchestrator 負責
            async for chunk in _sse_from_turn_execution(
                execution,
                on_run_finished=on_run_finished,
                on_stream_finished=on_run_finished,
                error_message="Agent 執行失敗",
            ):
                yield chunk
        finally:
            # Once the orchestrator owns the run, a disconnected Web consumer
            # must not cancel first-message title persistence.  A normally
            # suspended same-Round stream awaits the task via
            # ``on_stream_finished`` and emits ``title_updated`` before EOF.
            if (
                not _entered_sse
                and title_generation_task
                and not title_generation_task.done()
            ):
                title_generation_task.cancel()
            # 若 producer 未由 TurnOrchestrator 接管，手動釋放用戶鎖
            if not _entered_sse:
                await _release_user_run_lock_in_new_session(
                    user_id=user_id,
                    lock_id=lock_id,
                    session_id=chat_session_id,
                )

    return _make_sse_response(
        event_generator(),
        db=db, session=session, user_id=user_id, session_id=chat_session_id,
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

    enforce_token_limits(db, user_id=user_id)
    resume_turn = _web_resume_adapter.normalize_resume(
        session_id=chat_session_id,
        user_id=user_id,
        request=request,
    )

    # 用戶級並發限制 + 清理遺留取消請求。必须先拿 slot，再触碰 cached Agent。
    lock_id = await _acquire_lock_and_clear_cancel(db, user_id=user_id, session_id=chat_session_id)
    run_guard_started_at = now_naive()

    try:
        user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
        user_sandbox_id = user_sandbox.sandbox_id if user_sandbox else None
        model_id = _resolve_session_model_for_user(db, session, user_id)
    except Exception:
        await _release_user_run_lock_in_new_session(
            user_id=user_id,
            lock_id=lock_id,
            session_id=chat_session_id,
        )
        raise

    async def event_generator():
        _entered_sse = False  # 追蹤 producer 是否已由 TurnOrchestrator 接管
        settings = get_settings()
        init_task = _start_agent_init(
            user_id=user_id,
            chat_session_id=chat_session_id,
            model_id=model_id,
            sandbox_id=user_sandbox_id,
        )
        try:
            try:
                async for heartbeat in _agent_init_heartbeats(
                    init_task,
                    heartbeat_interval=settings.sse_heartbeat_interval,
                    timeout_seconds=settings.agent_init_timeout_seconds,
                ):
                    yield event_encoder.encode(heartbeat)

                agent_service = _resolve_agent_init(init_task)
            except _AgentInitTimedOut as e:
                logger.error(
                    "Resume Agent 初始化超时: user=%s session=%s timeout=%ss",
                    user_id,
                    chat_session_id,
                    settings.agent_init_timeout_seconds,
                )
                yield event_encoder.encode(RunErrorEvent(message=str(e), code="AGENT_INIT_TIMEOUT"))
                return
            except _AgentInitFailed as e:
                yield event_encoder.encode(RunErrorEvent(message=str(e), code="AGENT_INIT_FAILED"))
                return
            finally:
                await _cancel_agent_init_task(init_task)

            if _has_cancel_activity_since_in_new_session(
                user_id=user_id,
                session_id=chat_session_id,
                started_at=run_guard_started_at,
            ):
                logger.info(
                    "resume 檢測到較新 cancel activity，短路本次請求: user=%s session=%s",
                    user_id,
                    chat_session_id,
                )
                yield event_encoder.encode(RunErrorEvent(message="Aborted by user", code="USER_ABORT"))
                await _complete_cancel_request_in_new_session(user_id=user_id, session_id=chat_session_id)
                return

            if not agent_service.has_pending_interrupt(resume_turn.interrupt_id):
                yield event_encoder.encode(RunErrorEvent(
                    message="没有待处理的中断（可能已过期或已恢复），或中断 ID 不匹配",
                    code="NO_PENDING_INTERRUPT",
                ))
                return

            try:
                execution = await _turn_orchestrator.resume_turn(
                    resume_turn,
                    agent_service=agent_service,
                    lock_id=lock_id,
                    run_started_at=run_guard_started_at,
                )
            except InteractionConflictError as e:
                yield event_encoder.encode(RunErrorEvent(
                    message=str(e),
                    code="RESUME_CONFLICT",
                ))
                return
            except InvalidInteractionResponseError as e:
                yield event_encoder.encode(RunErrorEvent(
                    message=str(e),
                    code="INVALID_INTERACTION_RESPONSE",
                ))
                return
            except Exception:
                logger.error("resume turn failed before orchestrated SSE started", exc_info=True)
                yield event_encoder.encode(RunErrorEvent(message="服务暂时不可用，请稍后重试", code="INTERNAL_ERROR"))
                return

            _entered_sse = True  # producer 已由 TurnOrchestrator 接管，鎖的釋放由 orchestrator 負責
            async for chunk in _sse_from_turn_execution(
                execution,
                error_message="服务暂时不可用，请稍后重试",
            ):
                yield chunk
        finally:
            # 若 producer 未由 TurnOrchestrator 接管，手動釋放用戶鎖
            if not _entered_sse:
                await _release_user_run_lock_in_new_session(
                    user_id=user_id,
                    lock_id=lock_id,
                    session_id=chat_session_id,
                )

    return _make_sse_response(
        event_generator(),
        db=db, session=session, user_id=user_id, session_id=chat_session_id,
    )

@router.get("/{chat_session_id}/round/{round_id}/subscribe")
async def subscribe_to_round(
    chat_session_id: str,
    round_id: str,
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

    db.refresh(round_obj)
    if round_obj.status in Round.SUBSCRIBE_TERMINAL_STATUSES:
        RunCompletionService(db).ensure_terminal_sync(round_id)
    db.rollback()

    async def subscribe_generator():
        settings = get_settings()
        now_ms = lambda: int(datetime.now().timestamp() * 1000)
        iterator = None
        next_event_task: asyncio.Task | None = None
        heartbeat_task: asyncio.Task | None = None

        try:
            event_bus = AguiEventBus(SessionLocal)
            iterator = event_bus.subscribe(round_id, after_sequence=last_sequence).__aiter__()
            next_event_task = asyncio.create_task(iterator.__anext__())
            heartbeat_task = asyncio.create_task(asyncio.sleep(settings.sse_heartbeat_interval))

            while True:
                active_tasks = {task for task in (next_event_task, heartbeat_task) if task is not None}
                if not active_tasks:
                    return
                done, _ = await asyncio.wait(active_tasks, return_when=asyncio.FIRST_COMPLETED)

                if heartbeat_task in done:
                    yield event_encoder.encode(CustomEvent(
                        name="heartbeat",
                        value={"timestamp": now_ms()},
                    ))
                    heartbeat_task = asyncio.create_task(asyncio.sleep(settings.sse_heartbeat_interval))

                if next_event_task not in done:
                    continue

                try:
                    event_dict = next_event_task.result()
                except StopAsyncIteration:
                    return

                yield event_encoder.encode_dict(event_dict)

                if event_dict.get("type") in (EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value):
                    return

                next_event_task = asyncio.create_task(iterator.__anext__())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("订阅事件流异常: round=%s", round_id, exc_info=True)
            yield event_encoder.encode(RunErrorEvent(message="订阅失败", code="SUBSCRIBE_FAILED"))
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            if next_event_task is not None and not next_event_task.done():
                next_event_task.cancel()
                try:
                    await next_event_task
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                except Exception:
                    logger.debug("closing subscribe event task raised", exc_info=True)
            if iterator is not None and hasattr(iterator, "aclose"):
                try:
                    await iterator.aclose()
                except Exception:
                    logger.debug("closing subscribe iterator raised", exc_info=True)

    return StreamingResponse(
        subscribe_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{chat_session_id}/round/{round_id}/subagent-graph")
async def get_subagent_graph(
    chat_session_id: str,
    round_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """查询指定 run 所属的 subagent run graph。"""
    graph = get_subagent_graph_service().get_graph(
        db,
        user_id=user_id,
        session_id=chat_session_id,
        run_id=round_id,
    )
    return graph.model_dump(mode="json")


@router.post("/{chat_session_id}/abort")
async def abort_chat(
    chat_session_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """中止正在進行的 Agent 執行

    单 worker 取消语义：
    1. 写入 append-only 取消审计行
    2. 若当前 worker 命中本地 Agent，额外 fast-path 设置 cancel_token
    3. 立即收斂 running round 為 cancelled 並釋放用戶鎖（不等待執行 worker 自行結束）
    """
    # 驗證會話
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="會話不存在")

    settings = get_settings()
    stale_threshold = max(settings.sse_subscribe_timeout, 1)

    # 取消可在兩種情況發起：
    # 1) 已有 running round
    # 2) 初始化窗口（尚未建 round）但該會話仍持有「年輕」用戶鎖
    running_round = get_main_running_round(db, session_id=chat_session_id)
    user_lock = (
        db.query(UserRunLock)
        .filter(
            UserRunLock.user_id == user_id,
            UserRunLock.session_id == chat_session_id,
        )
        .first()
    )
    running_round_id = running_round.id if running_round else None
    user_lock_id = user_lock.lock_id if user_lock else None
    lock_recent = False
    lock_heartbeat_age: float | None = None
    if user_lock and user_lock.updated_at:
        lock_heartbeat_age = (now_naive() - user_lock.updated_at).total_seconds()
        lock_recent = lock_heartbeat_age < stale_threshold

    if not running_round and not lock_recent:
        raise HTTPException(status_code=409, detail="該會話沒有正在進行的執行")

    cancel_target = _web_cancel_adapter.normalize_cancel(
        session_id=chat_session_id,
        user_id=user_id,
        round_id=running_round_id,
        root_run_id=running_round_id,
        requested_after=(
            getattr(running_round, "created_at", None)
            if running_round and isinstance(getattr(running_round, "created_at", None), datetime)
            else now_naive()
        ),
    )

    # 寫入 append-only 取消審計請求
    try:
        cancel_result = await _with_db_retry(
            lambda: _turn_orchestrator.cancel_turn(cancel_target, db=db),
            rollback=db.rollback,
        )
        request_id = cancel_result.request_id
    except OperationalError:
        db.rollback()
        logger.warning(
            "寫入取消請求遇到數據庫鎖衝突: user=%s session=%s",
            user_id,
            chat_session_id,
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")
    except Exception:
        db.rollback()
        logger.error(
            "寫入取消請求失敗: user=%s session=%s",
            user_id,
            chat_session_id,
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")

    # 命中同 worker 時走本地 fast-path
    agent_pool = get_agent_pool()
    agent_service = agent_pool.get(chat_session_id)
    if agent_service and agent_service.cancel_token:
        agent_service.cancel_token.set()
        logger.info("cancel_token 已設置 (fast-path): session=%s", chat_session_id)

    local_runner = _active_runners.get(chat_session_id)
    if local_runner and not local_runner.done():
        logger.info("abort 命中本地 runner，等待其通过 cancel_token/终态检查退出: session=%s", chat_session_id)

    # 若有 running round，立即收斂本地状态为 cancelled，避免前端與
    # running-sessions 視圖回跳。终态事务只把 waiting interaction 与
    # approved-but-undispatched 视为可证明安全；已派发审批或没有 durable
    # pre-dispatch 事实的普通 running Round 都保守报告 outcome unknown。
    outcome_warning: str | None = None
    if running_round:
        try:
            cancellation = await RunCompletionService(db).cancel_user_run(
                run_id=running_round_id,
                outcome_warning=ABORT_OUTCOME_WARNING,
            )
            outcome_warning = (
                ABORT_OUTCOME_WARNING
                if cancellation.outcome_uncertain
                else None
            )
        except OperationalError:
            db.rollback()
            logger.warning(
                "abort 收斂 round 時遇到數據庫鎖衝突: user=%s session=%s",
                user_id,
                chat_session_id,
                exc_info=True,
            )
            raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")
        except Exception:
            db.rollback()
            logger.error(
                "abort 收斂 round 失敗: user=%s session=%s",
                user_id,
                chat_session_id,
                exc_info=True,
            )
            raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")

        if agent_service:
            agent_service.discard_pending_runtime_state(
                owner_round_id=running_round_id,
            )
        _agui_event_bus.cleanup_subscribers(running_round_id)

    # 立即釋放該會話鎖（如果存在），允許用戶立刻重發。
    if user_lock_id:
        released = await _release_user_run_lock_in_new_session(
            user_id=user_id,
            lock_id=user_lock_id,
            session_id=chat_session_id,
        )
        if not released:
            logger.error(
                "abort 收斂完成但釋放鎖失敗，拒絕返回 cancelled: user=%s session=%s lock_id=%s",
                user_id,
                chat_session_id,
                user_lock_id,
            )
            raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")

    # 本次 abort 已由接口完成終態收斂，取消請求狀態同步置 completed。
    await _complete_cancel_request_in_new_session(user_id=user_id, session_id=chat_session_id)

    if running_round:
        if lock_heartbeat_age is not None and lock_heartbeat_age >= stale_threshold:
            reason = "worker_dead"
        else:
            reason = "force_aborted"
        return {
            "status": "cancelled",
            "request_id": request_id,
            "reason": reason,
            "outcome_warning": outcome_warning,
        }

    # 僅處於 init-window（有鎖無 round）時也立即解除阻塞。
    return {
        "status": "cancelled",
        "request_id": request_id,
        "reason": "force_unlocked",
        "outcome_warning": None,
    }


@router.get("/{chat_session_id}/abort/status")
async def get_abort_status(
    chat_session_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """查询会话取消请求状态（用于取消审计与排障）。"""
    session = (
        db.query(Session)
        .filter(Session.id == chat_session_id, Session.user_id == user_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="會話不存在")

    cancel_row = (
        db.query(RunCancelRequest)
        .filter(
            RunCancelRequest.session_id == chat_session_id,
            RunCancelRequest.user_id == user_id,
        )
        .order_by(RunCancelRequest.requested_at.desc())
        .first()
    )
    running_round = (
        db.query(Round)
        .filter(
            Round.session_id == chat_session_id,
            Round.status == "running",
        )
        .first()
    )

    if not cancel_row:
        return {
            "session_id": chat_session_id,
            "state": "none",
            "request_id": None,
            "requested_at": None,
            "acked_at": None,
            "completed_at": None,
            "target_run_id": None,
            "root_run_id": None,
            "requested_after": None,
            "running": bool(running_round),
            "running_round_id": running_round.id if running_round else None,
        }

    return {
        "session_id": chat_session_id,
        "state": cancel_row.state,
        "request_id": cancel_row.request_id,
        "requested_at": _format_datetime(cancel_row.requested_at),
        "acked_at": _format_datetime(cancel_row.acked_at),
        "completed_at": _format_datetime(cancel_row.completed_at),
        "target_run_id": cancel_row.target_run_id,
        "root_run_id": cancel_row.root_run_id,
        "requested_after": _format_datetime(cancel_row.requested_after),
        "running": bool(running_round),
        "running_round_id": running_round.id if running_round else None,
    }


# =============================================================================
# 已棄用：/message/agui 路由已合併到 /message/stream
# 主路由現在直接使用 chat_agui() 透傳 AG-UI 事件，無需單獨的簡化路由
# =============================================================================
