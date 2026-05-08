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
from sqlalchemy import insert, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as DBSession
from src.api.models.database import get_db
from src.api.deps import get_current_user
from src.api.models.session import Session
from src.api.models.agui_event import AGUIEventLog
from src.api.models.user_run_lock import UserRunLock
from src.api.models.run_cancel_request import RunCancelRequest
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
_round_subscribers: dict[str, list[asyncio.Queue]] = {}
_round_subscribers_lock = threading.Lock()

# 活躍的後台 Agent 運行任務（session_id -> producer task）
# Agent 執行與 SSE 連接解耦：瀏覽器斷開後 Agent 繼續在後台運行
_active_runners: dict[str, asyncio.Task] = {}

_CANCEL_STATE_REQUESTED = "requested"
_CANCEL_STATE_ACKED = "acked"
_CANCEL_STATE_COMPLETED = "completed"
_LOCAL_RUNNER_ABORT_WAIT_SECONDS = 0.2


async def _upsert_cancel_request(
    db: DBSession,
    *,
    user_id: str,
    session_id: str,
) -> str:
    """写入/覆盖会话取消请求（跨 worker 可见）。"""
    request_id = str(uuid.uuid4())

    def _do():
        now = now_naive()
        row = (
            db.query(RunCancelRequest)
            .filter(
                RunCancelRequest.session_id == session_id,
                RunCancelRequest.user_id == user_id,
            )
            .first()
        )
        if row:
            row.state = _CANCEL_STATE_REQUESTED
            row.request_id = request_id
            row.requested_at = now
            row.acked_at = None
            row.completed_at = None
        else:
            db.add(
                RunCancelRequest(
                    session_id=session_id,
                    user_id=user_id,
                    state=_CANCEL_STATE_REQUESTED,
                    request_id=request_id,
                    requested_at=now,
                )
            )
        db.commit()
        return request_id

    result = await _with_sqlite_retry(_do, rollback=db.rollback)
    logger.info(
        "cancel request -> requested: user=%s session=%s request_id=%s",
        user_id,
        session_id,
        result,
    )
    return result


async def _clear_pending_cancel_request(
    db: DBSession,
    *,
    user_id: str,
    session_id: str,
) -> bool:
    """新 run 开始前清理可能遗留的 requested/acked 取消请求。"""

    def _do():
        row = (
            db.query(RunCancelRequest)
            .filter(
                RunCancelRequest.session_id == session_id,
                RunCancelRequest.user_id == user_id,
            )
            .first()
        )
        if not row:
            return False
        if row.state in (_CANCEL_STATE_REQUESTED, _CANCEL_STATE_ACKED):
            row.state = _CANCEL_STATE_COMPLETED
            row.completed_at = now_naive()
            db.commit()
            logger.info(
                "clear stale cancel request -> completed: user=%s session=%s request_id=%s",
                user_id,
                session_id,
                row.request_id,
            )
            return True
        return False

    return await _with_sqlite_retry(_do, rollback=db.rollback)


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
            row = (
                db.query(RunCancelRequest)
                .filter(
                    RunCancelRequest.session_id == session_id,
                    RunCancelRequest.user_id == user_id,
                )
                .first()
            )
            if not row:
                return False
            if row.state != _CANCEL_STATE_COMPLETED:
                prev_state = row.state
                row.state = _CANCEL_STATE_COMPLETED
                row.completed_at = now_naive()
                db.commit()
                logger.info(
                    "cancel request %s -> completed: user=%s session=%s request_id=%s",
                    prev_state,
                    user_id,
                    session_id,
                    row.request_id,
                )
            return True

        return await _with_sqlite_retry(_do, rollback=db.rollback)

    return await _run_in_new_session(
        _op, label="完成 cancel request", user_id=user_id, session_id=session_id,
    )


def _cancel_row_touched_after(*, row: RunCancelRequest | None, started_at: datetime) -> bool:
    """判斷取消請求是否在 run 啟動後被更新過（作為 abort epoch）。"""
    if not row:
        return False
    for ts in (row.requested_at, row.acked_at, row.completed_at):
        if ts and ts > started_at:
            return True
    return False


def _has_cancel_activity_since(
    db: DBSession,
    *,
    user_id: str,
    session_id: str,
    started_at: datetime,
) -> bool:
    """檢查 run 啟動後是否發生過 cancel 活動。"""
    row = (
        db.query(RunCancelRequest)
        .filter(
            RunCancelRequest.session_id == session_id,
            RunCancelRequest.user_id == user_id,
        )
        .first()
    )
    return _cancel_row_touched_after(row=row, started_at=started_at)


async def _cancel_request_watcher(
    *,
    user_id: str,
    session_id: str,
    cancel_token: asyncio.Event,
    lock_id: str | None = None,
    run_started_at: datetime | None = None,
):
    """轮询 DB 取消请求并触发本地 cancel_token（跨 worker cancel）。

    同时定期刷新 UserRunLock.updated_at 作为心跳。
    每轮检查使用独立短生命周期 Session，在 asyncio.sleep 期间将连接归还连接池，
    避免长时间独占 DB 连接导致 QueuePool 耗尽（FIFO 阻塞 event loop 死锁）。
    cancel check 频率由 cancel_watcher_interval_seconds 控制（默认 3s），
    心跳频率复用 sse_heartbeat_interval（默认 15s）。
    连续心跳失败 N 次后自杀，避免被其他 worker 误判存活。

    心跳失败语义：OperationalError/Exception 分支不刷新 `_last_heartbeat`，
    下一轮 `check_interval`（默认 3s）后立即重试；连续 `max_heartbeat_failures`
    次失败则触发自杀。与旧实现（无条件刷新、每 `heartbeat_interval` 重试一次）
    相比，最坏自杀时间由 ~45s 缩短到 ~9s，更快收敛到 "worker 死亡" 判定。
    """
    settings = get_settings()
    check_interval = max(settings.cancel_watcher_interval_seconds, 0.5)
    heartbeat_interval = max(settings.sse_heartbeat_interval, check_interval)
    max_heartbeat_failures = 3
    _heartbeat_fail_count = 0
    _last_heartbeat = time.monotonic()

    try:
        while not cancel_token.is_set():
            # 1. 检查取消请求 —— 独立短生命周期 Session
            try:
                with SessionLocal() as check_db:
                    row = (
                        check_db.query(RunCancelRequest)
                        .filter(
                            RunCancelRequest.session_id == session_id,
                            RunCancelRequest.user_id == user_id,
                        )
                        .first()
                    )
                    if row and row.state == _CANCEL_STATE_REQUESTED:
                        row.state = _CANCEL_STATE_ACKED
                        row.acked_at = now_naive()
                        check_db.commit()
                        cancel_token.set()
                        logger.info(
                            "检测到跨 worker cancel 请求，已触发本地取消: user=%s session=%s request_id=%s",
                            user_id, session_id, row.request_id,
                        )
                        return
                    # abort-epoch：即使 requested 已被其他流程快速收敛为 completed，
                    # 只要該次更新發生在本 run 啟動之後，也應終止本地執行。
                    if row and run_started_at and _cancel_row_touched_after(row=row, started_at=run_started_at):
                        cancel_token.set()
                        logger.info(
                            "检测到较新 cancel epoch，触发本地取消: user=%s session=%s request_id=%s state=%s",
                            user_id,
                            session_id,
                            row.request_id,
                            row.state,
                        )
                        return
            except OperationalError as exc:
                if not _is_sqlite_locked_error(exc):
                    raise
                # SQLite 瞬时锁冲突，下次再试

            # 2. 定期刷新锁心跳（频率低于 cancel check）—— 独立短生命周期 Session
            if lock_id and (time.monotonic() - _last_heartbeat) >= heartbeat_interval:
                try:
                    with SessionLocal() as hb_db:
                        updated = hb_db.query(UserRunLock).filter(
                            UserRunLock.user_id == user_id,
                            UserRunLock.lock_id == lock_id,
                        ).update({UserRunLock.updated_at: now_naive()}, synchronize_session=False)
                        hb_db.commit()
                    if updated:
                        _heartbeat_fail_count = 0
                        _last_heartbeat = time.monotonic()
                    else:
                        # 锁已不存在（被其他 worker 回收），自杀
                        logger.warning(
                            "心跳发现锁已被回收，触发取消: user=%s session=%s",
                            user_id, session_id,
                        )
                        cancel_token.set()
                        return
                except OperationalError as exc:
                    if _is_sqlite_locked_error(exc):
                        _heartbeat_fail_count += 1
                        logger.warning(
                            "心跳写入失败（SQLite 锁冲突）: user=%s fail_count=%d/%d",
                            user_id, _heartbeat_fail_count, max_heartbeat_failures,
                        )
                    else:
                        raise
                except Exception:
                    _heartbeat_fail_count += 1
                    logger.warning(
                        "心跳写入异常: user=%s fail_count=%d/%d",
                        user_id, _heartbeat_fail_count, max_heartbeat_failures,
                        exc_info=True,
                    )

                if _heartbeat_fail_count >= max_heartbeat_failures:
                    logger.error(
                        "心跳连续失败 %d 次，触发取消防止假活: user=%s session=%s",
                        _heartbeat_fail_count, user_id, session_id,
                    )
                    cancel_token.set()
                    return

            await asyncio.sleep(check_interval)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "cancel watcher 异常退出: user=%s session=%s",
            user_id,
            session_id,
            exc_info=True,
        )


def _is_sqlite_locked_error(exc: OperationalError) -> bool:
    """判斷是否為 SQLite 常見鎖衝突錯誤（database is locked / table is locked）。"""
    message = str(getattr(exc, "orig", exc)).lower()
    return "database is locked" in message or "database table is locked" in message


async def _with_sqlite_retry(
    fn: Callable[[], any],
    *,
    max_retries: int = 5,
    retry_interval: float = 0.1,
    rollback: Callable[[], None] | None = None,
) -> any:
    """SQLite 瞬時寫鎖衝突的通用重試 wrapper。

    Args:
        fn: 要執行的同步 callable（包含 DB 操作 + commit）。
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
            return fn()
        except OperationalError as exc:
            last_exc = exc
            if rollback:
                rollback()
            if _is_sqlite_locked_error(exc) and attempt + 1 < max_retries:
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
    """嘗試獲取用戶運行鎖（跨 worker）。

    使用獨立 DB Session，避免 rollback 污染請求級 Session 中已載入的 ORM 物件。
    返回 lock_id 表示獲取成功；返回 None 表示已有運行中的任務。
    """
    settings = get_settings()
    stale_threshold_seconds = max(settings.sse_subscribe_timeout, 1)
    max_busy_retries = 5
    retry_interval_seconds = 0.1

    with SessionLocal() as lock_db:
        async def _try_insert_lock(lock_id: str) -> str | None:
            for attempt in range(max_busy_retries):
                try:
                    lock_db.execute(
                        insert(UserRunLock).values(
                            user_id=user_id,
                            session_id=session_id,
                            lock_id=lock_id,
                        )
                    )
                    lock_db.commit()
                    return lock_id
                except IntegrityError:
                    lock_db.rollback()
                    return None
                except OperationalError as exc:
                    lock_db.rollback()
                    if not _is_sqlite_locked_error(exc):
                        raise
                    if attempt + 1 < max_busy_retries:
                        await asyncio.sleep(retry_interval_seconds)
                        continue
                    logger.warning(
                        "獲取用戶運行鎖遇到 SQLite 寫鎖衝突: user=%s session=%s",
                        user_id,
                        session_id,
                        exc_info=True,
                    )
                    return None
                except Exception:
                    lock_db.rollback()
                    raise
            return None

        acquired_lock_id = await _try_insert_lock(str(uuid.uuid4()))
        if acquired_lock_id:
            return acquired_lock_id

        # 主鍵衝突：可能是另一個 worker 正在運行，也可能是崩潰遺留的陳舊鎖。
        try:
            existing_lock = lock_db.query(UserRunLock).filter(UserRunLock.user_id == user_id).first()
        except OperationalError as exc:
            lock_db.rollback()
            if _is_sqlite_locked_error(exc):
                logger.warning(
                    "查詢用戶運行鎖遇到 SQLite 鎖衝突: user=%s session=%s",
                    user_id,
                    session_id,
                    exc_info=True,
                )
                return None
            raise
        if not existing_lock:
            return await _try_insert_lock(str(uuid.uuid4()))

        # 用 updated_at（心跳時間）判斷 worker 是否存活。
        # _cancel_request_watcher 定期刷新 lock.updated_at（間隔 sse_heartbeat_interval），
        # 所以 updated_at 陳舊 = worker 已死（而非用 created_at 猜測）。
        lock_heartbeat_age = (now_naive() - existing_lock.updated_at).total_seconds()
        if lock_heartbeat_age < stale_threshold_seconds:
            return None

        # 心跳已過期，worker 很可能已死。直接回收陳舊鎖。
        # 使用 user_id + lock_id 精確刪除，避免 TOCTOU 窗口誤刪新鎖。
        stale_lock_id = existing_lock.lock_id
        logger.warning(
            "檢測到陳舊用戶鎖（心跳 %.1fs 前），回收: user=%s session=%s lock=%s",
            lock_heartbeat_age, user_id, existing_lock.session_id, stale_lock_id,
        )
        try:
            deleted = (
                lock_db.query(UserRunLock)
                .filter(
                    UserRunLock.user_id == user_id,
                    UserRunLock.lock_id == stale_lock_id,
                )
                .delete(synchronize_session=False)
            )
            if deleted != 1:
                lock_db.rollback()
                return None
            lock_db.commit()
        except Exception:
            lock_db.rollback()
            return None

        # 回收陳舊鎖後，連帶清理遺留的 running round（避免永久卡死）
        try:
            _cleanup_orphaned_rounds(lock_db, user_id=user_id)
        except Exception:
            logger.warning("清理孤兒 round 失敗: user=%s", user_id, exc_info=True)

        return await _try_insert_lock(str(uuid.uuid4()))


def _cleanup_orphaned_rounds(db: DBSession, *, user_id: str) -> int:
    """將用戶所有 running round 標記為 cancelled（用於 worker 崩潰後回收）。

    只在鎖心跳已過期且鎖被回收後調用 — 此時可確定原 worker 已死。
    """
    user_session_ids_stmt = select(Session.id).where(Session.user_id == user_id)
    orphaned_rounds = (
        db.query(Round)
        .filter(
            Round.status == "running",
            or_(
                Round.session_id.in_(user_session_ids_stmt),
                Round.thread_id.in_(user_session_ids_stmt),
            ),
        )
        .all()
    )
    if not orphaned_rounds:
        return 0
    for r in orphaned_rounds:
        r.status = "cancelled"
        r.final_response = r.final_response or "Worker crashed, round orphaned"
        r.completed_at = now_naive()
    db.commit()
    logger.warning(
        "已回收 %d 個孤兒 round: user=%s round_ids=%s",
        len(orphaned_rounds), user_id, [r.id for r in orphaned_rounds],
    )
    return len(orphaned_rounds)


def _parse_event_sequence(event_data: dict) -> int | None:
    """从事件字典中提取 sequence（兼容 sequence/_sequence）。"""
    for key in ("sequence", "_sequence"):
        value = event_data.get(key)
        if isinstance(value, int):
            return value
    return None


def _load_persisted_run_events_after_sequence(
    db: DBSession,
    *,
    run_id: str,
    last_sequence: int,
) -> tuple[list[dict], int]:
    """从 agui_events 表加载指定 run 在某序号后的持久化事件。"""
    rows = (
        db.query(AGUIEventLog)
        .filter(
            AGUIEventLog.run_id == run_id,
            AGUIEventLog.sequence > last_sequence,
        )
        .order_by(AGUIEventLog.sequence)
        .all()
    )

    latest_sequence = last_sequence
    events: list[dict] = []
    for row in rows:
        try:
            event_data = json.loads(row.payload)
            events.append(event_data)
            latest_sequence = max(latest_sequence, row.sequence)
        except json.JSONDecodeError as e:
            logger.warning("解析持久化事件失败: run=%s id=%s err=%s", run_id, row.id, e)

    return events, latest_sequence


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
    遇到 SQLite 瞬時鎖衝突時最多重試 5 次。
    """
    def _do():
        query = db.query(UserRunLock).filter(UserRunLock.user_id == user_id)
        if lock_id is not None:
            query = query.filter(UserRunLock.lock_id == lock_id)
        elif session_id is not None:
            query = query.filter(UserRunLock.session_id == session_id)

        lock_row = query.first()
        if not lock_row:
            # 带过滤条件时：锁可能存在但不匹配（属于其他 session/lock_id），应返回 False
            if lock_id is not None or session_id is not None:
                any_lock = db.query(UserRunLock).filter(UserRunLock.user_id == user_id).first()
                if any_lock:
                    return False
            # 锁确实不存在，视为已释放
            return True

        db.delete(lock_row)
        db.commit()
        return True

    try:
        return await _with_sqlite_retry(_do, rollback=db.rollback)
    except OperationalError:
        logger.warning(
            "釋放用戶運行鎖失敗: user=%s lock_id=%s session=%s",
            user_id, lock_id, session_id, exc_info=True,
        )
        return False
    except Exception:
        logger.warning(
            "釋放用戶運行鎖異常: user=%s lock_id=%s session=%s",
            user_id, lock_id, session_id, exc_info=True,
        )
        return False


async def _release_user_run_lock_in_new_session(
    *,
    user_id: str,
    lock_id: str | None = None,
    session_id: str | None = None,
) -> bool:
    """在獨立 DB Session 中釋放鎖。

    用於 SSE 背景 producer finally，避免依賴請求作用域的 DB Session。
    """

    async def _op(db, *, user_id, lock_id, session_id):
        return await _release_user_run_lock(
            db, user_id=user_id, lock_id=lock_id, session_id=session_id,
        )

    return await _run_in_new_session(
        _op, label="釋放用戶運行鎖",
        user_id=user_id, lock_id=lock_id, session_id=session_id,
    )


# =========================================================================
# SSE + 心跳保活通用助手
# =========================================================================


async def _sse_with_heartbeat(
    event_source: AsyncIterator[AGUIEvent],
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    lock_id: str | None = None,
    cancel_token: asyncio.Event | None = None,
    run_started_at: datetime | None = None,
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
        lock_id: 用戶運行鎖 ID，用於 owner 校驗釋放鎖。
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
            # 防禦性路徑：abort_chat 不再主動調用 runner.cancel()（改用 cancel_token），
            # 但 asyncio 框架或 ASGI server 仍可能取消 task（如進程關閉、超時等）。
            # 此處補發 RUN_FINISHED 確保 SSE consumer 能正常收尾。
            # 守衛：若 Agent 已正常 yield 過 RUN_FINISHED（_run_completed=True），不重複發送。
            if current_run_id and not _run_completed:
                finished_event = RunFinishedEvent(
                    threadId=session_id or "",
                    runId=current_run_id,
                    outcome="interrupt",
                    result={"reason": "user_cancelled"},
                )
                if _consumer_active:
                    event_queue.put_nowait(finished_event)
                # 同步廣播給訂閱者（put_nowait 保證在 finally cleanup 前完成）
                event_dict = finished_event.model_dump(by_alias=True, exclude_none=True)
                with _round_subscribers_lock:
                    subscriber_queues = list(_round_subscribers.get(current_run_id, []))
                for q in subscriber_queues:
                    try:
                        q.put_nowait(event_dict)
                    except Exception:
                        pass
            raise
        except Exception as e:
            if _consumer_active:
                event_queue.put_nowait(e)
        finally:
            if _consumer_active:
                event_queue.put_nowait(_SENTINEL)
            # 清理追蹤
            if session_id:
                # 僅允許當前 producer 移除自己，避免舊 run finally 誤刪新 run 映射。
                current_task = asyncio.current_task()
                if _active_runners.get(session_id) is current_task:
                    _active_runners.pop(session_id, None)
            if user_id:
                if session_id:
                    await _complete_cancel_request_in_new_session(
                        user_id=user_id,
                        session_id=session_id,
                    )
                await _release_user_run_lock_in_new_session(
                    user_id=user_id,
                    lock_id=lock_id,
                    session_id=session_id,
                )
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
    cancel_watch_task: asyncio.Task | None = None

    # 跨 worker cancel：輪詢 DB 取消請求並觸發本地 cancel_token，同時刷新鎖心跳
    if cancel_token and session_id and user_id:
        cancel_watch_task = asyncio.create_task(
            _cancel_request_watcher(
                user_id=user_id,
                session_id=session_id,
                cancel_token=cancel_token,
                lock_id=lock_id,
                run_started_at=run_started_at,
            )
        )

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
            # 運行已結束，清理所有後台任務
            producer_task.cancel()
            if cancel_watch_task:
                cancel_watch_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                pass
            if cancel_watch_task:
                try:
                    await cancel_watch_task
                except asyncio.CancelledError:
                    pass
        else:
            # SSE 斷開但 Agent 仍在運行 → 保留 producer 和 cancel_watch_task，
            # 讓 Agent 繼續後台執行，watcher 繼續刷心跳和輪詢取消請求。
            # 不 await 它們，否則 finally 會卡住。
            # producer finally 會在 Agent 結束後釋放鎖並完成取消請求，
            # 屆時 watcher 下次心跳發現鎖已刪除會自行退出。
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
        raise HTTPException(
            status_code=429,
            detail="当前有正在运行的任务，请等待完成后再发送新消息",
        )

    # 清理旧 run 遗留的取消请求，避免新 run 被陈旧 requested 误杀
    # 失败时仅 warning：最坏情况是 watcher 首次轮询时 ack + cancel，用户可重发
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

    # 用戶級並發限制 + 清理遺留取消請求
    lock_id = await _acquire_lock_and_clear_cancel(db, user_id=user_id, session_id=chat_session_id)
    run_guard_started_at = now_naive()

    # 幂等性保證依賴 DB 層 UniqueConstraint（history_service.create_round 的 IntegrityError 兜底）
    # 無需在此做 SELECT fast-path：TOCTOU 窗口使其不可靠，省掉的只是一次 Agent 初始化嘗試

    # 預讀 sandbox_id 和 round_count（輕量查詢，不會超時）
    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    user_sandbox_id = user_sandbox.sandbox_id if user_sandbox else None
    round_count = db.query(Round).filter(Round.session_id == chat_session_id).count()
    model_id = session.model_id

    # 定義事件生成器（Agent 初始化移入 generator 內部，讓 SSE 響應頭先返回，心跳保活撐住連接）
    async def event_generator():
        _entered_sse = False  # 追蹤是否已進入 _sse_with_heartbeat（producer 負責清理用戶鎖）
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
            await _release_user_run_lock_in_new_session(
                user_id=user_id,
                lock_id=lock_id,
                session_id=chat_session_id,
            )
            return
        finally:
            if not init_task.done():
                init_task.cancel()
                try:
                    await init_task
                except (asyncio.CancelledError, Exception):
                    pass

        # init-window 防漏：若初始化期間發生 abort，立刻短路，不允許舊請求繼續創建 round。
        if _has_cancel_activity_since(
            db,
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

            _entered_sse = True  # producer 即將啟動，鎖的釋放由 _sse_with_heartbeat 的 finally 負責
            async for chunk in _sse_with_heartbeat(
                agent_service.chat_agui(
                    user_content=request.content,
                    idempotency_key=request.idempotency_key,
                ),
                session_id=chat_session_id,
                user_id=user_id,
                lock_id=lock_id,
                cancel_token=cancel_token,
                run_started_at=run_guard_started_at,
                on_run_finished=on_run_finished,
                error_message="Agent 執行失敗",
            ):
                yield chunk
        finally:
            if title_generation_task and not title_generation_task.done():
                title_generation_task.cancel()
            # 若未進入 _sse_with_heartbeat（producer 未啟動），手動釋放用戶鎖
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

    # 用戶級並發限制 + 清理遺留取消請求
    lock_id = await _acquire_lock_and_clear_cancel(db, user_id=user_id, session_id=chat_session_id)
    run_guard_started_at = now_naive()

    # 创建 per-run 取消令牌
    cancel_token = asyncio.Event()
    agent_service.cancel_token = cancel_token

    async def event_generator():
        _entered_sse = False  # 追蹤是否已進入 _sse_with_heartbeat（producer 負責清理用戶鎖）
        try:
            if _has_cancel_activity_since(
                db,
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
                await _release_user_run_lock_in_new_session(
                    user_id=user_id,
                    lock_id=lock_id,
                    session_id=chat_session_id,
                )
                return

            # 先構造 event_source，若 resume_agui 拋異常則 _entered_sse 仍為 False → finally 會釋放鎖
            event_source = agent_service.resume_agui(
                interrupt_id=request.interrupt_id,
                answers=request.answers,
            )
            _entered_sse = True  # producer 即將啟動，鎖的釋放由 _sse_with_heartbeat 的 finally 負責
            async for chunk in _sse_with_heartbeat(
                event_source,
                session_id=chat_session_id,
                user_id=user_id,
                lock_id=lock_id,
                cancel_token=cancel_token,
                run_started_at=run_guard_started_at,
                error_message="服务暂时不可用，请稍后重试",
            ):
                yield chunk
        finally:
            # 若未進入 _sse_with_heartbeat（producer 未啟動），手動釋放用戶鎖
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
            latest_sequence = last_sequence

            def _build_terminal_chunks(*, has_run_finished_in_replay: bool = False) -> tuple[bool, list[str]]:
                if round_obj.status not in Round.SUBSCRIBE_TERMINAL_STATUSES:
                    return False, []

                # 如果重放事件中已包含 RUN_FINISHED，直接返回不重複發送
                if has_run_finished_in_replay:
                    print(f"✅ 重放事件已包含 RUN_FINISHED，輪次 {round_id} 訂閱正常結束")
                    return True, []

                # 發送 RUN_FINISHED（重放中沒有時才發送）
                print(f"📤 輪次 {round_id} 已完成但重放中無 RUN_FINISHED，補發完成事件")
                # 使用 HistoryService 構建 MESSAGES_SNAPSHOT
                messages = history_service.build_messages_snapshot(round_id)
                chunks: list[str] = [event_encoder.encode(MessagesSnapshotEvent(messages=messages))]

                # 发送终态事件
                if round_obj.status == "failed":
                    # failed 路径：仅发 RUN_ERROR 后结束，避免误标为 interrupt
                    chunks.append(event_encoder.encode(RunErrorEvent(
                        message="Run failed (status=failed)",
                        code="RUN_FAILED",
                    )))
                    return True, chunks

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
                    chunks.append(event_encoder.encode(complete_event))
                    return True, chunks

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
                    chunks.append(event_encoder.encode(complete_event))
                    return True, chunks

                if round_obj.status == "cancelled":
                    # cancelled 路径：用户主动取消，视为 interrupt（无中断详情）
                    complete_event = RunFinishedEvent(
                        threadId=chat_session_id,
                        runId=round_id,
                        result={
                            "finalResponse": round_obj.final_response or "",
                            "stepCount": round_obj.step_count,
                            "reason": "user_cancelled",
                        },
                        outcome="interrupt",
                    )
                    chunks.append(event_encoder.encode(complete_event))
                    return True, chunks

                # completed 路径
                complete_event = RunFinishedEvent(
                    threadId=chat_session_id,
                    runId=round_id,
                    result={
                        "finalResponse": round_obj.final_response or "",
                        "stepCount": round_obj.step_count,
                    },
                    outcome="success",
                )
                chunks.append(event_encoder.encode(complete_event))
                return True, chunks

            # === 1. 重放錯過的事件（AG-UI 核心機制）===
            replayed_events, latest_sequence = _load_persisted_run_events_after_sequence(
                db,
                run_id=round_id,
                last_sequence=last_sequence,
            )

            # 檢查重放事件中是否已包含 RUN_FINISHED
            has_run_finished_in_replay = any(
                e.get("type") == EventType.RUN_FINISHED.value for e in replayed_events
            )

            if replayed_events:
                print(f"📤 重放 {len(replayed_events)} 個錯過的事件 (sequence > {last_sequence})")
                for event_data in replayed_events:
                    event_sequence = _parse_event_sequence(event_data)
                    if event_sequence is not None:
                        latest_sequence = max(latest_sequence, event_sequence)
                    yield event_encoder.encode_dict(event_data)

            # === 2. 重新查詢輪次狀態（修復競態條件）===
            db.refresh(round_obj)

            if round_obj.status in Round.SUBSCRIBE_TERMINAL_STATUSES:
                handled, terminal_chunks = _build_terminal_chunks(
                    has_run_finished_in_replay=has_run_finished_in_replay,
                )
                if handled:
                    for terminal_chunk in terminal_chunks:
                        yield terminal_chunk
                return

            # === 3. 輪次仍在運行，註冊為訂閱者 ===
            with _round_subscribers_lock:
                if round_id not in _round_subscribers:
                    _round_subscribers[round_id] = []
                _round_subscribers[round_id].append(subscriber_queue)
                subscriber_count = len(_round_subscribers[round_id])
            print(f"📡 新订阅者已注册到轮次 {round_id}，当前订阅者数: {subscriber_count}")

            # 获取配置
            settings = get_settings()

            # 心跳 + 跨 worker 持久化事件轮询（使用独立 DB Session，避免与请求级 Session 竞争）
            last_realtime_event_at = time.monotonic()

            async def heartbeat_and_poll():
                nonlocal latest_sequence
                terminal_types = (EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value)
                poll_interval = max(settings.cancel_watcher_interval_seconds, 0.5)
                last_heartbeat_sent = time.monotonic()
                try:
                    while True:
                        await asyncio.sleep(poll_interval)

                        # 没有本地实时事件时，回放持久化增量，兜住跨 worker 订阅
                        # 每次查询使用独立短生命周期 Session，避免长期独占 DB 连接
                        if time.monotonic() - last_realtime_event_at >= poll_interval:
                            try:
                                with SessionLocal() as replay_db:
                                    replayed_events, replay_latest = _load_persisted_run_events_after_sequence(
                                        replay_db,
                                        run_id=round_id,
                                        last_sequence=latest_sequence,
                                    )
                            except Exception:
                                logger.warning(
                                    "订阅增量回放查询失败: round=%s", round_id, exc_info=True,
                                )
                                replayed_events, replay_latest = [], latest_sequence
                            latest_sequence = max(latest_sequence, replay_latest)
                            for event_data in replayed_events:
                                await subscriber_queue.put(event_data)
                                if event_data.get("type") in terminal_types:
                                    return

                        # SSE 心跳按 sse_heartbeat_interval 频率发送
                        if time.monotonic() - last_heartbeat_sent >= settings.sse_heartbeat_interval:
                            heartbeat_event = CustomEvent(
                                name="heartbeat",
                                value={"timestamp": now_ms()},
                            )
                            await subscriber_queue.put(heartbeat_event.model_dump(by_alias=True))
                            last_heartbeat_sent = time.monotonic()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.warning("订阅心跳轮询异常: round=%s", round_id, exc_info=True)

            heartbeat_task = asyncio.create_task(heartbeat_and_poll())

            try:
                # 监听队列中的事件
                while True:
                    # subscriber_queue 中的 event 已经是 dict (from broadcast)
                    event_dict = await asyncio.wait_for(subscriber_queue.get(), timeout=settings.sse_subscribe_timeout)
                    if event_dict.get("type") != EventType.CUSTOM.value or event_dict.get("name") != "heartbeat":
                        last_realtime_event_at = time.monotonic()
                    event_sequence = _parse_event_sequence(event_dict)
                    if event_sequence is not None:
                        latest_sequence = max(latest_sequence, event_sequence)
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

    多 worker 安全語義：
    1. 寫入跨 worker 可見的取消請求（DB requested）
    2. 若當前 worker 命中本地 Agent，額外 fast-path 設置 cancel_token
    3. 立即收斂 running round 為 cancelled 並釋放用戶鎖（不等待執行 worker 自行結束）
    4. 若命中本地 active runner，額外 cancel task 以縮短後台殘留窗口
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
    stale_threshold = max(settings.sse_heartbeat_interval * 3, 1)

    # 取消可在兩種情況發起：
    # 1) 已有 running round
    # 2) 初始化窗口（尚未建 round）但該會話仍持有「年輕」用戶鎖
    running_round = (
        db.query(Round)
        .filter(
            Round.session_id == chat_session_id,
            Round.status == "running",
        )
        .first()
    )
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

    # 寫入跨 worker 取消請求
    try:
        request_id = await _upsert_cancel_request(
            db,
            user_id=user_id,
            session_id=chat_session_id,
        )
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

    # 命中同 worker 的 active runner 時，額外嘗試強制取消，縮短後台殘留窗口。
    local_runner = _active_runners.get(chat_session_id)
    runner_stopped = False
    if local_runner and not local_runner.done():
        logger.info("abort 命中本地 runner，執行強制停止: session=%s", chat_session_id)
        local_runner.cancel()
        try:
            # 不長時間阻塞 abort 接口；若超時，語義仍以“已強制收斂 round + 釋放鎖”為準。
            await asyncio.wait_for(
                asyncio.shield(local_runner),
                timeout=_LOCAL_RUNNER_ABORT_WAIT_SECONDS,
            )
            runner_stopped = True
        except asyncio.CancelledError:
            runner_stopped = local_runner.done()
        except asyncio.TimeoutError:
            logger.warning("本地 runner 強制停止超時，回退為異步取消: session=%s", chat_session_id)
        except Exception:
            logger.warning("等待本地 runner 結束異常，回退為異步取消: session=%s", chat_session_id, exc_info=True)

    # 若有 running round，立即收斂為 cancelled，避免前端與 running-session 視圖回跳。
    if running_round:
        running_round.status = "cancelled"
        running_round.final_response = running_round.final_response or "Aborted by user"
        running_round.completed_at = now_naive()

        finished_event = RunFinishedEvent(
            threadId=chat_session_id,
            runId=running_round_id,
            outcome="interrupt",
            result={
                "reason": "user_cancelled",
                "finalResponse": running_round.final_response,
                "stepCount": running_round.step_count or 0,
            },
        )
        event_payload = finished_event.model_dump_json(by_alias=True)
        next_seq = (
            db.query(AGUIEventLog)
            .filter(AGUIEventLog.run_id == running_round_id)
            .count()
        ) + 1
        db.add(AGUIEventLog(
            run_id=running_round_id,
            event_type=EventType.RUN_FINISHED.value,
            payload=event_payload,
            sequence=next_seq,
        ))
        try:
            db.commit()
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

        event_dict = finished_event.model_dump(by_alias=True, exclude_none=True)
        await _broadcast_to_subscribers(running_round_id, event_dict)
        _cleanup_subscribers(running_round_id)

    # 立即釋放該會話鎖（如果存在），允許用戶立刻重發。
    if user_lock_id:
        released = await _release_user_run_lock_in_new_session(user_id=user_id, lock_id=user_lock_id)
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
        elif runner_stopped:
            reason = "force_stopped"
        else:
            reason = "force_aborted"
        return {"status": "cancelled", "request_id": request_id, "reason": reason}

    # 僅處於 init-window（有鎖無 round）時也立即解除阻塞。
    return {"status": "cancelled", "request_id": request_id, "reason": "force_unlocked"}


@router.get("/{chat_session_id}/abort/status")
async def get_abort_status(
    chat_session_id: str,
    user_id: str = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """查询会话取消请求状态（用于多 worker 观测与排障）。"""
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
        "running": bool(running_round),
        "running_round_id": running_round.id if running_round else None,
    }


# =============================================================================
# 已棄用：/message/agui 路由已合併到 /message/stream
# 主路由現在直接使用 chat_agui() 透傳 AG-UI 事件，無需單獨的簡化路由
# =============================================================================
