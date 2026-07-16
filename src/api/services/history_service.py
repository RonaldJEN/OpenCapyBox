"""对话历史服务

負責管理對話歷史記錄，使用 Round + AGUIEventLog 雙表結構：
- Round: 對話輪次（用戶輸入 + 最終響應）
- AGUIEventLog: AG-UI 事件流（包含完整的步驟細節，用於 SSE 重連和歷史重建）
"""
from collections.abc import Callable
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.exc import IntegrityError
from src.api.models.session import Session
from src.api.models.round import Round
from src.api.models.agui_event import AGUIEventLog
from src.api.models.interrupt_resolution import InterruptResolution
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.subagent_run import SubagentRun
from src.api.models.tool_permission import ToolApprovalRequest
from src.agent.schema.agui_events import AGUIEvent, EventType
from src.api.services.agui_event_bus import AguiEventBus, StoredEvent
from src.api.services.run_completion_service import RunCompletionService
from typing import List, Dict, Optional, AsyncIterator, Any
from datetime import datetime
from src.api.utils.timezone import now_naive
import json
import logging

logger = logging.getLogger(__name__)


def _is_interrupt_resolution_unique_violation(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None)
    if pgcode and pgcode != "23505":
        return False
    constraint_name = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if constraint_name:
        return constraint_name in {
            "interrupt_resolutions_pkey",
            "uq_interrupt_resolution_resume_round",
        }
    message = str(orig or exc).lower()
    return "interrupt_resolutions" in message and (
        "unique" in message or "duplicate" in message
    )


class HistoryService:
    """对话历史服务"""

    def __init__(self, db: DBSession | Callable[[], DBSession]):
        if callable(db) and not hasattr(db, "query"):
            self._session_factory: Callable[[], DBSession] | None = db
            self._db: DBSession | None = None
            self._owns_db = True
        else:
            self._session_factory = None
            self._db = db  # type: ignore[assignment]
            self._owns_db = False
        self._last_terminal_event: StoredEvent | None = None

    @property
    def db(self) -> DBSession:
        if self._db is None:
            if self._session_factory is None:
                raise RuntimeError("HistoryService has no DB session factory")
            self._db = self._session_factory()
        return self._db

    @db.setter
    def db(self, value: DBSession) -> None:
        if self._owns_db and self._db is not None and self._db is not value:
            self.close()
        self._db = value
        self._owns_db = False

    @property
    def session_factory(self) -> Callable[[], DBSession] | None:
        return self._session_factory

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    def reset_session(self):
        """回滚当前事务并清除 Session 状态，确保后续操作不受前序脏状态影响。"""
        self.db.rollback()

    def _refresh_detached(self, obj: Any) -> None:
        """刷新 DB 生成字段后分离对象，并结束 refresh 打开的只读事务。"""
        self.db.refresh(obj)
        self.db.expunge(obj)
        self.db.rollback()

    def get_round_status(self, round_id: str) -> str | None:
        """查询 round 当前状态。"""
        try:
            row = self.db.query(Round.status).filter(Round.id == round_id).first()
            if not row:
                return None
            return row[0]
        finally:
            self.db.rollback()

    def is_round_terminal(self, round_id: str) -> bool:
        """判断 round 是否已进入 subscribe 终态。"""
        status = self.get_round_status(round_id)
        return bool(status and status in Round.SUBSCRIBE_TERMINAL_STATUSES)

    # 🆕 Round 相关方法

    def create_round(
        self,
        session_id: str,
        round_id: str,
        user_message: str,
        user_attachments: Optional[List[Dict]] = None,
        idempotency_key: Optional[str] = None,
        parent_run_id: Optional[str] = None,
    ) -> Round:
        """创建新的对话轮次
        
        若 idempotency_key 觸發唯一約束衝突，返回已有的 Round（其 id != round_id）。
        調用方可通過比較 returned_round.id != round_id 判斷是否為重複請求。
        """
        round_obj = Round(
            id=round_id,
            session_id=session_id,
            user_message=user_message,
            user_attachments=json.dumps(user_attachments or [], ensure_ascii=False),
            status="running",
            idempotency_key=idempotency_key,
            parent_run_id=parent_run_id,
        )
        self.db.add(round_obj)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            # idempotency_key 唯一約束衝突 → 查詢已有 Round
            if idempotency_key:
                existing = (
                    self.db.query(Round)
                    .filter(Round.session_id == session_id, Round.idempotency_key == idempotency_key)
                    .first()
                )
                if existing:
                    self.db.expunge(existing)
                    self.db.rollback()
                    return existing
            self.db.rollback()
            raise
        self._refresh_detached(round_obj)
        return round_obj

    def create_resume_round(
        self,
        session_id: str,
        round_id: str,
        user_message: str,
        parent_run_id: str,
        user_attachments: Optional[List[Dict]] = None,
        interrupt_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        answers: Optional[Dict[str, str]] = None,
        tool_result_content: Optional[str] = None,
        restore_strategy: Optional[str] = None,
        fallback_reason: Optional[str] = None,
        commit: bool = True,
    ) -> Round:
        """原子创建 resume round，并将被接管的父 round 标记为 resumed。"""
        completed_at = now_naive()
        updated = (
            self.db.query(Round)
            .filter(
                Round.id == parent_run_id,
                Round.session_id == session_id,
                Round.status.in_(("running", "interrupted")),
            )
            .update(
                {
                    "status": "resumed",
                    "interrupt_payload": None,
                    "completed_at": completed_at,
                },
                synchronize_session="fetch",
            )
        )
        if updated != 1:
            self.db.rollback()
            parent_round = (
                self.db.query(Round)
                .filter(Round.id == parent_run_id, Round.session_id == session_id)
                .first()
            )
            parent_status = parent_round.status if parent_round else None
            self.db.rollback()
            if not parent_round:
                raise ValueError(f"Interrupted round not found: {parent_run_id}")
            raise ValueError(
                f"Round is not resumable: {parent_run_id} status={parent_status}"
            )

        round_obj = Round(
            id=round_id,
            session_id=session_id,
            user_message=user_message,
            user_attachments=json.dumps(user_attachments or [], ensure_ascii=False),
            status="running",
            parent_run_id=parent_run_id,
        )
        try:
            self.db.add(round_obj)
            self.db.flush()
            if interrupt_id:
                if tool_result_content is None:
                    self.db.rollback()
                    raise ValueError("tool_result_content is required for interrupt resolution")
                resolution = InterruptResolution(
                    interrupt_id=interrupt_id,
                    session_id=session_id,
                    parent_round_id=parent_run_id,
                    resume_round_id=round_id,
                    tool_call_id=tool_call_id,
                    answers_json=json.dumps(answers or {}, ensure_ascii=False),
                    resume_user_message=user_message,
                    tool_result_content=tool_result_content,
                    restore_strategy=restore_strategy,
                    fallback_reason=fallback_reason,
                )
                self.db.add(resolution)
            if commit:
                self.db.commit()
            else:
                self.db.flush()
        except IntegrityError as e:
            self.db.rollback()
            if interrupt_id and _is_interrupt_resolution_unique_violation(e):
                raise ValueError(f"Interrupt already resumed: {interrupt_id}") from e
            raise
        except Exception:
            self.db.rollback()
            raise
        if commit:
            self._refresh_detached(round_obj)
        return round_obj

    def update_interrupt_resolution_fallback(
        self,
        interrupt_id: str,
        fallback_reason: str,
    ) -> int:
        """记录已创建 resolution 在冷启动 stitching 阶段的降级原因。"""
        updated = (
            self.db.query(InterruptResolution)
            .filter(InterruptResolution.interrupt_id == interrupt_id)
            .update({"fallback_reason": fallback_reason}, synchronize_session="fetch")
        )
        if updated:
            self.db.commit()
        return updated

    def resolve_interrupted_rounds(self, session_id: str, *, commit: bool = True) -> int:
        """将会话中所有 interrupted 轮次标记为已解决（清除 interrupt_payload）。

        在 resume 成功创建新 round 之前调用，防止旧中断被前端重复恢复。
        Returns:
            被更新的轮次数量
        """
        updated = (
            self.db.query(Round)
            .filter(Round.session_id == session_id, Round.status == "interrupted")
            .update(
                {"status": "resumed", "interrupt_payload": None, "completed_at": now_naive()},
                synchronize_session="fetch",
            )
        )
        if updated and commit:
            self.db.commit()
        elif updated:
            self.db.flush()
        return updated

    # 終態集合引用 Round 模型的全局常量（唯一事實源）。
    _TERMINAL_STATUSES = Round.COMPLETE_TERMINAL_STATUSES | {"resumed"}

    def complete_round(
        self, round_id: str, final_response: str, step_count: int,
        status: str = "completed", interrupt_payload: str | None = None,
        terminal_event: AGUIEvent | dict[str, Any] | None = None,
    ) -> Round:
        """完成对话轮次

        若 round 已處於終態（completed/failed/cancelled/resumed），跳過狀態覆寫。
        resumed round 允許接收遲到的 interrupted 完成回填展示元數據，但狀態保持 resumed。
        """
        self._last_terminal_event = None
        round_obj = self.db.query(Round).filter(Round.id == round_id).first()
        if round_obj:
            if round_obj.status in self._TERMINAL_STATUSES:
                if round_obj.status == "resumed" and status == "interrupted":
                    changed = False
                    if final_response and not round_obj.final_response:
                        round_obj.final_response = final_response
                        changed = True
                    if (
                        step_count is not None
                        and (round_obj.step_count is None or step_count > round_obj.step_count)
                    ):
                        round_obj.step_count = step_count
                        changed = True
                    if round_obj.completed_at is None:
                        round_obj.completed_at = now_naive()
                        changed = True
                    if changed:
                        self.db.commit()
                        self._refresh_detached(round_obj)
                    else:
                        self.db.expunge(round_obj)
                        self.db.rollback()
                    return round_obj
                logger.info(
                    "Round %s 已處於終態 %s，跳過 complete_round(status=%s)",
                    round_id, round_obj.status, status,
                )
                self.db.expunge(round_obj)
                self.db.rollback()
                return round_obj
            completion = RunCompletionService(self.db)
            self._last_terminal_event = completion.complete_sync(
                run_id=round_id,
                status=status,
                final_response=final_response,
                step_count=step_count,
                interrupt_payload=interrupt_payload,
                terminal_event=terminal_event,
            )
            round_obj = self.db.query(Round).filter(Round.id == round_id).first()
            if round_obj is None:
                self.db.rollback()
                return None
            self._refresh_detached(round_obj)
        else:
            self.db.rollback()
        return round_obj

    def _rebuild_steps_from_events(
        self,
        run_id: str,
        *,
        return_last_sequence: bool = False,
    ) -> List[Dict] | tuple[List[Dict], int]:
        """从 AG-UI 事件重建步骤列表
        
        解析 STEP_STARTED/FINISHED, TEXT_MESSAGE_*, TOOL_CALL_* 等事件
        重建前端所需的 steps 数据结构。
        
        Args:
            run_id: 运行 ID
            
        Returns:
            步骤列表
        """
        events = (
            self.db.query(AGUIEventLog)
            .filter(AGUIEventLog.run_id == run_id)
            .order_by(AGUIEventLog.sequence)
            .all()
        )
        def event_sequence(event_log) -> int:
            value = getattr(event_log, "sequence", 0)
            return int(value) if isinstance(value, (int, float)) else 0

        last_event_sequence = max((event_sequence(event_log) for event_log in events), default=0)
        
        steps = []
        current_step = None
        current_tool_call = None

        def event_timestamp(event_log, event_data: dict) -> int | None:
            timestamp = getattr(event_log, "timestamp", None)
            if isinstance(timestamp, (int, float)):
                return int(timestamp)
            payload_timestamp = event_data.get("timestamp")
            if isinstance(payload_timestamp, (int, float)):
                return int(payload_timestamp)
            return None
        
        for event_log in events:
            try:
                event_data = json.loads(event_log.payload)
                event_type = event_log.event_type
                timestamp = event_timestamp(event_log, event_data)
                
                if event_type == "STEP_STARTED":
                    # 开始新步骤
                    current_step = {
                        "step_number": len(steps) + 1,
                        "thinking": "",
                        "assistant_content": "",
                        "tool_calls": [],
                        "tool_results": [],
                        "status": "running",
                        "created_at": event_log.created_at.isoformat() if event_log.created_at else None,
                        "started_at_ts": timestamp,
                    }
                    steps.append(current_step)
                    
                elif event_type == "STEP_FINISHED" and current_step:
                    current_step["status"] = "completed"
                    current_step["finished_at_ts"] = timestamp

                elif event_type == "THINKING_TEXT_MESSAGE_START" and current_step:
                    current_step["thinking_start_ts"] = timestamp
                    
                # === CONTENT delta 事件：累積內容（新格式 + 舊數據兼容）===
                elif event_type == "THINKING_TEXT_MESSAGE_CONTENT" and current_step:
                    delta = event_data.get("delta", "")
                    current_step["thinking"] += delta
                    
                elif event_type == "TEXT_MESSAGE_CONTENT" and current_step:
                    delta = event_data.get("delta", "")
                    current_step["assistant_content"] += delta
                    
                # === *_END 事件：向下兼容舊數據的 fullContent ===
                elif event_type == "THINKING_TEXT_MESSAGE_END" and current_step:
                    full_content = event_data.get("fullContent", "")
                    if full_content and not current_step["thinking"]:
                        current_step["thinking"] = full_content
                    current_step["thinking_end_ts"] = timestamp
                    
                elif event_type == "TEXT_MESSAGE_END" and current_step:
                    full_content = event_data.get("fullContent", "")
                    if full_content and not current_step["assistant_content"]:
                        current_step["assistant_content"] = full_content
                    
                elif event_type == "TOOL_CALL_START" and current_step:
                    # 开始工具调用
                    current_tool_call = {
                        "id": event_data.get("toolCallId", ""),
                        "name": event_data.get("toolCallName", ""),
                        "input": "",
                        "started_at_ts": timestamp,
                    }
                    
                elif event_type == "TOOL_CALL_ARGS" and current_tool_call:
                    # 累积工具参数（兼容舊數據）
                    delta = event_data.get("delta", "")
                    current_tool_call["input"] += delta
                    
                elif event_type == "TOOL_CALL_END" and current_step and current_tool_call:
                    # 完成工具调用
                    # 向下兼容：舊數據的 fullContent 在 END 事件中
                    full_content = event_data.get("fullContent", "")
                    if full_content and not current_tool_call["input"]:
                        current_tool_call["input"] = full_content
                    # 尝试解析参数为 JSON（Schema 期望 Dict[str, Any]）
                    try:
                        current_tool_call["input"] = json.loads(current_tool_call["input"])
                    except (json.JSONDecodeError, TypeError):
                        # 解析失敗則包裝為 dict
                        current_tool_call["input"] = {"raw": current_tool_call["input"]}
                    current_tool_call["ended_at_ts"] = timestamp
                    current_step["tool_calls"].append(current_tool_call)
                    current_tool_call = None
                    
                elif event_type == "TOOL_CALL_RESULT" and current_step:
                    # 工具调用结果（匹配 ToolResult Schema: success, content, error）
                    result_content = event_data.get("result", event_data.get("content", ""))
                    is_error = event_data.get("isError", False)
                    result = {
                        "tool_call_id": event_data.get("toolCallId", ""),
                        "success": not is_error,
                        "content": result_content if isinstance(result_content, str) else json.dumps(result_content, ensure_ascii=False),
                        "error": result_content if is_error else None,
                        "received_at_ts": timestamp,
                        "execution_time_ms": event_data.get("executionTimeMs"),
                    }
                    current_step["tool_results"].append(result)
                    
            except (json.JSONDecodeError, KeyError) as e:
                print(f"⚠️ 解析事件失败: {e} (run_id={run_id}, id={event_log.id})")
                continue
        
        if return_last_sequence:
            return steps, last_event_sequence
        return steps

    def get_session_rounds(self, session_id: str) -> List[Dict]:
        """获取会话的所有轮次
        
        步骤(steps)从 AG-UI 事件日志动态重建，而非单独存储。
        """
        subagent_child_round_ids = self._get_subagent_child_round_ids(session_id)
        tool_approval_resume_round_ids = self._get_tool_approval_resume_round_ids(
            session_id
        )
        rounds = (
            self.db.query(Round)
            .filter(Round.session_id == session_id)
            .order_by(Round.created_at)
            .all()
        )
        if subagent_child_round_ids:
            rounds = [round_obj for round_obj in rounds if round_obj.id not in subagent_child_round_ids]

        result = []
        for round_obj in rounds:
            # 从 AG-UI 事件重建步骤
            steps, last_event_sequence = self._rebuild_steps_from_events(
                round_obj.id,
                return_last_sequence=True,
            )
            attachments: List[Dict] = []
            if round_obj.user_attachments:
                try:
                    parsed = json.loads(round_obj.user_attachments)
                    if isinstance(parsed, list):
                        attachments = parsed
                except json.JSONDecodeError:
                    attachments = []

            # 解析 interrupt_payload（仅 interrupted 状态）
            interrupt_details = None
            if round_obj.status == "interrupted" and round_obj.interrupt_payload:
                try:
                    interrupt_details = json.loads(round_obj.interrupt_payload)
                except json.JSONDecodeError:
                    interrupt_details = None

            result.append(
                {
                    "round_id": round_obj.id,
                    "parent_run_id": round_obj.parent_run_id,
                    "control_kind": (
                        "tool_approval"
                        if round_obj.id in tool_approval_resume_round_ids
                        else None
                    ),
                    "idempotency_key": round_obj.idempotency_key,
                    "last_event_sequence": last_event_sequence,
                    "user_message": round_obj.user_message,
                    "user_attachments": attachments,
                    "final_response": round_obj.final_response,
                    "step_count": round_obj.step_count,
                    "status": round_obj.status,
                    "created_at": round_obj.created_at.isoformat(),
                    "completed_at": round_obj.completed_at.isoformat()
                    if round_obj.completed_at
                    else None,
                    "steps": steps,
                    "interrupt": interrupt_details,
                }
            )

        return result

    def _get_tool_approval_resume_round_ids(self, session_id: str) -> set[str]:
        """Return resume rounds backed by a durable tool approval request.

        ``parent_run_id`` is shared by every interrupt resume (and may also be
        used by future branching flows), so it cannot identify approval control
        rounds on its own.  ``InterruptResolution`` already records the resume
        round structurally, while a matching ``ToolApprovalRequest`` separates
        tool approvals from ordinary ``ask_user`` answers without inspecting
        user-visible message text.
        """

        rows = (
            self.db.query(InterruptResolution.resume_round_id)
            .join(
                ToolApprovalRequest,
                ToolApprovalRequest.id == InterruptResolution.interrupt_id,
            )
            .filter(
                InterruptResolution.session_id == session_id,
                ToolApprovalRequest.session_id == session_id,
            )
            .all()
        )
        return {
            str(row[0] if isinstance(row, tuple) else row.resume_round_id)
            for row in rows
        }

    def _get_subagent_child_round_ids(self, session_id: str) -> set[str]:
        """Return child round ids that belong to subagent sidechains."""
        rows = (
            self.db.query(SubagentRun.child_run_id)
            .filter(
                SubagentRun.session_id == session_id,
                SubagentRun.child_run_id.isnot(None),
            )
            .all()
        )
        child_ids: set[str] = set()
        for row in rows:
            value = row[0] if isinstance(row, tuple) else getattr(row, "child_run_id", None)
            if value:
                child_ids.add(value)
        return child_ids

    # =========================================================================
    # AG-UI 事件相關方法
    # =========================================================================

    @property
    def last_terminal_event(self) -> StoredEvent | None:
        return self._last_terminal_event

    async def save_agui_event(self, run_id: str, event: AGUIEvent) -> Optional[StoredEvent]:
        """Store one non-terminal AG-UI event via AguiEventBus."""
        event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
        if event_type in (EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value):
            if self.is_round_terminal(run_id):
                logger.info("Run %s 已终态，丢弃迟到 terminal 事件: %s", run_id, event_type)
                return None
            raise ValueError("terminal events must be written via complete_round/RunCompletionService")
        return await AguiEventBus(self.db).publish(run_id, event)
    
    def get_run_events(self, run_id: str) -> List[Dict]:
        """獲取某次運行的所有事件（按序號排序）
        
        Args:
            run_id: 運行 ID
            
        Returns:
            事件列表（解析後的 JSON）
        """
        events = (
            self.db.query(AGUIEventLog)
            .filter(AGUIEventLog.run_id == run_id)
            .order_by(AGUIEventLog.sequence)
            .all()
        )
        return [json.loads(e.payload) for e in events]
    
    async def replay_run_events(self, run_id: str, last_sequence: int = 0) -> List[Dict]:
        """重放某次运行的事件（从 last_sequence 之后）

        Args:
            run_id: 运行 ID
            last_sequence: 客户端最后收到的事件序号

        Returns:
            事件字典列表（不包含 _sequence 字段）
        """
        events = (
            self.db.query(AGUIEventLog)
            .filter(AGUIEventLog.run_id == run_id)
            .filter(AGUIEventLog.sequence > last_sequence)
            .order_by(AGUIEventLog.sequence)
            .all()
        )

        result = []
        for event_log in events:
            try:
                event_data = json.loads(event_log.payload)
                event_data["sequence"] = event_log.sequence
                result.append(event_data)
            except json.JSONDecodeError as e:
                print(f"⚠️ 解析事件失败: {e} (run_id={run_id}, id={event_log.id})")
        return result

    # 兼容性别名
    async def replay_run(self, run_id: str) -> AsyncIterator[Dict]:
        """[Deprecated] 重放某次运行的完整事件流"""
        events = self.get_run_events(run_id)
        for event in events:
            yield event
    
    def get_run_summary(self, run_id: str) -> Dict:
        """獲取運行摘要
        
        Args:
            run_id: 運行 ID
            
        Returns:
            包含事件統計的摘要字典
        """
        events = (
            self.db.query(AGUIEventLog)
            .filter(AGUIEventLog.run_id == run_id)
            .all()
        )
        
        # 統計各類型事件數量
        event_counts = {}
        for event in events:
            event_type = event.event_type
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        
        return {
            "run_id": run_id,
            "total_events": len(events),
            "event_counts": event_counts,
        }

    # =========================================================================
    # 歷史恢復相關方法
    # =========================================================================

    def get_minimal_history(self, session_id: str) -> List[Dict]:
        """獲取精簡的對話歷史（用於 Agent 上下文恢復）
        
        只返回每輪已完成對話的 user_message 和 final_response，
        不包含中間的 thinking、tool_calls、tool 結果，以節省 token。
        
        Args:
            session_id: 會話 ID
            
        Returns:
            精簡歷史列表，每項包含 role 和 content
        """
        subagent_child_round_ids = self._get_subagent_child_round_ids(session_id)
        rounds = (
            self.db.query(Round)
            .filter(Round.session_id == session_id, Round.status == "completed")
            .order_by(Round.created_at)
            .all()
        )
        if subagent_child_round_ids:
            rounds = [round_obj for round_obj in rounds if round_obj.id not in subagent_child_round_ids]
        
        history = []
        for round_obj in rounds:
            # 1. 添加用戶的初始問題
            if round_obj.user_message:
                history.append({
                    "role": "user",
                    "content": round_obj.user_message,
                })
            
            # 2. 添加最終回復（不含 thinking/tool_calls）
            if round_obj.final_response:
                history.append({
                    "role": "assistant",
                    "content": round_obj.final_response,
                })
        
        return history

    def build_messages_snapshot(self, round_id: str) -> List[Dict]:
        """構建 AG-UI MESSAGES_SNAPSHOT 格式的消息列表
        
        從 AG-UI 事件日誌重建消息歷史，用於 SSE 重連時恢復已完成輪次。
        
        Args:
            round_id: 輪次 ID（同時也是 run_id）
            
        Returns:
            AG-UI Message 格式的消息列表
        """
        steps = self._rebuild_steps_from_events(round_id)
        
        messages = []
        for step in steps:
            step_num = step.get("step_number", 0)
            
            # 助手內容消息
            assistant_content = step.get("assistant_content", "")
            if assistant_content:
                messages.append({
                    "id": f"msg_{round_id}_{step_num}",
                    "role": "assistant",
                    "content": assistant_content,
                })
            
            # 工具調用結果消息
            tool_calls = step.get("tool_calls", [])
            tool_results = step.get("tool_results", [])
            
            for i, tc in enumerate(tool_calls):
                if i < len(tool_results):
                    messages.append({
                        "id": f"tool_{round_id}_{step_num}_{i}",
                        "role": "tool",
                        "toolCallId": tc.get("id", f"tc_{round_id}_{step_num}_{i}"),
                        "content": tool_results[i].get("content", ""),
                    })
        
        return messages

    async def save_llm_call_record(
        self,
        *,
        session_id: str,
        round_id: str,
        step_index: int,
        request_messages: list[dict[str, Any]],
        request_tools: list[str],
        response_content: str | None,
        response_thinking: str | None,
        response_tool_calls: list[dict[str, Any]] | None,
        response_error: str | None,
        finish_reason: str | None,
        usage_prompt_tokens: int | None,
        usage_completion_tokens: int | None,
        usage_total_tokens: int | None,
        first_token_latency_s: float | None,
        completion_latency_s: float | None,
        compaction_triggered: bool = False,
        compaction_pre_tokens: int | None = None,
        compaction_post_tokens: int | None = None,
        compaction_tokens_saved: int | None = None,
        compaction_microcompact_compacted_messages: int | None = None,
        compaction_summary_generated_count: int | None = None,
        compaction_summary_reused_count: int | None = None,
        compaction_summary_quality_repair_count: int | None = None,
        compaction_emergency_truncate_dropped_rounds: int | None = None,
    ) -> LLMCallRecord:
        """持久化单次 LLM 调用快照。"""
        request_message_count = len(request_messages)
        if (
            len(request_messages) == 1
            and isinstance(request_messages[0], dict)
            and isinstance(request_messages[0].get("messages"), list)
        ):
            request_message_count = len(request_messages[0]["messages"])

        row = LLMCallRecord(
            session_id=session_id,
            round_id=round_id,
            step_index=step_index,
            request_message_count=request_message_count,
            manual_review_status="没问题",
            request_messages=json.dumps(request_messages, ensure_ascii=False),
            request_tools=json.dumps(request_tools, ensure_ascii=False),
            response_content=response_content,
            response_thinking=response_thinking,
            response_tool_calls=(
                json.dumps(response_tool_calls, ensure_ascii=False)
                if response_tool_calls is not None
                else None
            ),
            response_error=response_error,
            finish_reason=finish_reason,
            usage_prompt_tokens=usage_prompt_tokens,
            usage_completion_tokens=usage_completion_tokens,
            usage_total_tokens=usage_total_tokens,
            first_token_latency_s=first_token_latency_s,
            completion_latency_s=completion_latency_s,
            compaction_triggered=compaction_triggered,
            compaction_pre_tokens=compaction_pre_tokens,
            compaction_post_tokens=compaction_post_tokens,
            compaction_tokens_saved=compaction_tokens_saved,
            compaction_microcompact_compacted_messages=compaction_microcompact_compacted_messages,
            compaction_summary_generated_count=compaction_summary_generated_count,
            compaction_summary_reused_count=compaction_summary_reused_count,
            compaction_summary_quality_repair_count=compaction_summary_quality_repair_count,
            compaction_emergency_truncate_dropped_rounds=compaction_emergency_truncate_dropped_rounds,
        )
        self.db.add(row)
        self.db.commit()
        self._refresh_detached(row)
        return row
