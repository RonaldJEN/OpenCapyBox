"""对话历史服务

負責管理對話歷史記錄，使用 Round + AGUIEventLog 雙表結構：
- Round: 對話輪次（用戶輸入 + 最終響應）
- AGUIEventLog: AG-UI 事件流（包含完整的步驟細節，用於 SSE 重連和歷史重建）
"""
from collections.abc import Callable
import hashlib
import posixpath
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.exc import IntegrityError
from src.api.models.session import Session
from src.api.models.round import Round
from src.api.models.agui_event import AGUIEventLog
from src.api.models.agent_interaction import AgentInteraction
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.subagent_run import SubagentRun
from src.api.models.tool_permission import ToolApprovalRequest
from src.api.models.workspace import WorkspaceFileVersion
from src.agent.schema.agui_events import AGUIEvent, EventType
from src.agent.context_compaction import SUMMARY_PREFIX
from src.api.services.agui_event_bus import AguiEventBus, StoredEvent, get_agui_event_bus
from src.api.services.agent_interaction_service import ContinuationWriteFence
from src.api.services.run_completion_service import RunCompletionService
from typing import List, Dict, Optional, AsyncIterator, Any
from datetime import datetime
from src.api.utils.timezone import now_naive
import json
import logging

logger = logging.getLogger(__name__)


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

    def save_waiting_round_progress(self, round_id: str, *, step_count: int) -> None:
        """Persist non-terminal progress when a logical Round parks for input."""
        round_obj = (
            self.db.query(Round)
            .filter(Round.id == round_id, Round.status == "waiting_interaction")
            .first()
        )
        if round_obj is None:
            self.db.rollback()
            raise ValueError(f"Waiting round not found: {round_id}")
        round_obj.step_count = max(int(round_obj.step_count or 0), int(step_count or 0))
        round_obj.completed_at = None
        self.db.commit()
        self.db.expunge(round_obj)
        self.db.rollback()

    # 🆕 Round 相关方法

    def find_round_by_idempotency_key(
        self, session_id: str, idempotency_key: str
    ) -> Optional[Round]:
        """Look up an already-admitted Round so retries can skip side effects."""
        existing = (
            self.db.query(Round)
            .filter(
                Round.session_id == session_id,
                Round.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing is not None:
            self.db.expunge(existing)
        self.db.rollback()
        return existing

    def create_round(
        self,
        session_id: str,
        round_id: str,
        user_message: str,
        user_attachments: Optional[List[Dict]] = None,
        preferred_skills: Optional[List[Dict[str, str]]] = None,
        preferred_mcp_connections: Optional[List[Dict[str, str]]] = None,
        thinking_mode: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
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
            preferred_skills=(
                json.dumps(
                    [
                        {
                            "key": item["key"],
                            "display_name": item["display_name"],
                        }
                        for item in preferred_skills
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if preferred_skills is not None
                else None
            ),
            preferred_mcp_connections=(
                json.dumps(
                    [
                        {
                            "server_id": item["server_id"],
                            "display_name": item["display_name"],
                        }
                        for item in preferred_mcp_connections
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if preferred_mcp_connections is not None
                else None
            ),
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
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

    # 終態集合引用 Round 模型的全局常量（唯一事實源）。
    _TERMINAL_STATUSES = Round.COMPLETE_TERMINAL_STATUSES

    def complete_round(
        self, round_id: str, final_response: str, step_count: int,
        status: str = "completed",
        terminal_event: AGUIEvent | dict[str, Any] | None = None,
        continuation_fence: ContinuationWriteFence | None = None,
    ) -> Round:
        """完成对话轮次

        若 round 已處於終態，跳過狀態覆寫。
        """
        self._last_terminal_event = None
        round_obj = self.db.query(Round).filter(Round.id == round_id).first()
        if round_obj:
            if round_obj.status in self._TERMINAL_STATUSES:
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
                terminal_event=terminal_event,
                continuation_fence=continuation_fence,
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
        assistant_file_references: list[dict] | None = None,
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

        def append_assistant_file_reference(value: Any) -> None:
            if assistant_file_references is None or not isinstance(value, dict):
                return
            source = value.get("source")
            if (
                source == "session"
                and str(value.get("operation") or "").upper() == "DELETED"
                and isinstance(value.get("session_id"), str)
                and isinstance(value.get("path"), str)
            ):
                assistant_file_references[:] = [
                    reference
                    for reference in assistant_file_references
                    if reference.get("source") != "session"
                    or reference.get("session_id") != value["session_id"]
                    or reference.get("path") != value["path"]
                ]
                return
            ref_id = value.get("ref_id")
            path = value.get("path")
            name = value.get("name")
            revision = value.get("revision")
            if source not in {"session", "workspace"}:
                return
            if not all(
                isinstance(item, str) and item
                for item in (ref_id, path, name, revision)
            ):
                return
            assistant_file_references.append({
                "ref_id": ref_id,
                "source": source,
                "name": name,
                "path": path,
                "size": int(value.get("size") or 0),
                "modified": str(value.get("modified") or ""),
                "type": str(value.get("type") or ""),
                "revision": revision,
                "operation": value.get("operation"),
                "tool_call_id": value.get("toolCallId"),
                "sha256": value.get("sha256"),
                "session_id": value.get("session_id"),
                "snapshot_path": value.get("snapshot_path"),
                "entry_id": value.get("entry_id"),
                "workspace_path": value.get("workspace_path"),
                "version_id": value.get("version_id"),
            })

        def remove_workspace_assistant_file_reference(entry_id: str) -> None:
            if assistant_file_references is None:
                return
            assistant_file_references[:] = [
                reference
                for reference in assistant_file_references
                if reference.get("source") != "workspace"
                or reference.get("entry_id") != entry_id
            ]

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

                elif (
                    event_type == "CUSTOM"
                    and event_data.get("name") == "interaction_requested"
                    and current_step
                ):
                    # Same-Round interaction_requested is the durable waiting
                    # boundary for the step. Agent.run_agui emits an explicit
                    # STEP_FINISHED immediately afterwards, but a process may
                    # die before that second event commits. Treat the request
                    # as an implicit finish so history never resurrects the
                    # accepted waiting step as running.
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
                    existing_result = next(
                        (
                            item
                            for item in current_step["tool_results"]
                            if item.get("tool_call_id") == result["tool_call_id"]
                        ),
                        None,
                    )
                    if existing_result is None:
                        current_step["tool_results"].append(result)
                    else:
                        existing_result.update(result)

                elif (
                    event_type == "CUSTOM"
                    and event_data.get("name") == "interaction_resolved"
                    and current_step
                ):
                    value = (
                        event_data.get("value")
                        if isinstance(event_data.get("value"), dict)
                        else {}
                    )
                    tool_call_id = value.get("toolCallId", "")
                    if tool_call_id and not any(
                        item.get("tool_call_id") == tool_call_id
                        for item in current_step["tool_results"]
                    ):
                        current_step["tool_results"].append({
                            "tool_call_id": tool_call_id,
                            "success": True,
                            "content": value.get("toolResultContent", ""),
                            "error": None,
                            "received_at_ts": timestamp,
                            "execution_time_ms": 0,
                        })

                elif (
                    event_type == "CUSTOM"
                    and event_data.get("name") == "workspace_resource_changed"
                ):
                    value = event_data.get("value")
                    if not isinstance(value, dict):
                        continue
                    operation_name = str(value.get("operation") or "").upper()
                    if (
                        operation_name == "DELETED"
                        and isinstance(value.get("entry_id"), str)
                    ):
                        for deleted_id in value.get("affected_entry_ids") or [value["entry_id"]]:
                            remove_workspace_assistant_file_reference(deleted_id)
                    projected_reference = value.get("assistant_file_reference")
                    if (
                        not isinstance(projected_reference, dict)
                        and value.get("kind") == "file"
                        and str(value.get("status") or "active") == "active"
                        and operation_name not in {"NO_CHANGE", "DELETED"}
                        and value.get("entry_id")
                        and value.get("current_version_id")
                    ):
                        # Safe legacy backfill: old events already carried a
                        # stable Workspace entry + immutable version.  We do
                        # not infer Session identity from assistant prose.
                        entry_id = str(value["entry_id"])
                        version_id = str(value["current_version_id"])
                        path = str(value.get("path") or "")
                        name = str(value.get("name") or posixpath.basename(path))
                        projected_reference = {
                            "ref_id": f"workspace:{entry_id}:{version_id}",
                            "source": "workspace",
                            "entry_id": entry_id,
                            "version_id": version_id,
                            "name": name,
                            "path": path,
                            "workspace_path": path,
                            "size": int(value.get("size_bytes") or 0),
                            "modified": "",
                            "type": (
                                name.rsplit(".", 1)[-1].lower()
                                if "." in name
                                else ""
                            ),
                            "revision": str(value.get("revision") or ""),
                            "operation": value.get("operation"),
                            "toolCallId": value.get("toolCallId"),
                            "sha256": value.get("sha256"),
                        }
                    append_assistant_file_reference(projected_reference)
                elif (
                    event_type == "CUSTOM"
                    and event_data.get("name") == "assistant_file_referenced"
                    and assistant_file_references is not None
                ):
                    append_assistant_file_reference(event_data.get("value"))
                    
            except (json.JSONDecodeError, KeyError) as e:
                print(f"⚠️ 解析事件失败: {e} (run_id={run_id}, id={event_log.id})")
                continue
        
        if return_last_sequence:
            return steps, last_event_sequence
        return steps

    def recover_expired_interaction_continuations(
        self,
        session_id: str,
    ) -> list[str]:
        """Reclaim pre-start leases and fail expired started continuations."""
        from src.api.services.agent_interaction_service import AgentInteractionService

        if not isinstance(self.db, DBSession):
            return []
        AgentInteractionService.repark_expired_continuation_claims(
            self.db,
            session_id=session_id,
        )
        irrecoverable_run_ids = (
            AgentInteractionService.load_irrecoverable_continuation_round_ids(
                self.db,
                session_id=session_id,
            )
        )
        failed_run_ids: list[str] = []
        for run_id in irrecoverable_run_ids:
            interaction_kind = (
                AgentInteractionService.lock_irrecoverable_continuation_round_for_failure(
                    self.db,
                    round_id=run_id,
                )
            )
            if interaction_kind is None:
                self.db.rollback()
                continue
            final_response = (
                "[工具审批续跑进程中断；为避免重复副作用，本轮不会自动重试]"
                if interaction_kind == "tool_approval"
                else "[交互续跑在持久化启动后中断；本轮不会重新提交已接受的回答]"
            )
            stored_terminal = RunCompletionService(self.db).complete_sync(
                run_id=run_id,
                status="failed",
                final_response=final_response,
            )
            if stored_terminal is not None:
                get_agui_event_bus().publish_committed_nowait(
                    run_id,
                    stored_terminal.event,
                )
                failed_run_ids.append(run_id)
        return failed_run_ids

    def get_session_rounds(self, session_id: str) -> List[Dict]:
        """获取会话的所有轮次

        步骤(steps)从 AG-UI 事件日志动态重建，而非单独存储。
        """
        if isinstance(self.db, DBSession):
            self.recover_expired_interaction_continuations(session_id)
        subagent_child_round_ids = self._get_subagent_child_round_ids(session_id)
        rounds = (
            self.db.query(Round)
            .filter(Round.session_id == session_id)
            .order_by(Round.created_at)
            .all()
        )
        resumable_approval_ids: set[str] | None = None
        if isinstance(self.db, DBSession):
            from src.api.services.tool_permission_service import (
                APPROVAL_CONTINUATION_RESUMABLE_STATUSES,
            )

            resumable_approval_ids = {
                str(row[0])
                for row in (
                    self.db.query(ToolApprovalRequest.id)
                    .filter(
                        ToolApprovalRequest.session_id == session_id,
                        ToolApprovalRequest.status.in_(
                            APPROVAL_CONTINUATION_RESUMABLE_STATUSES
                        ),
                    )
                    .all()
                )
                if isinstance(row[0], str) and row[0]
            }
        pending_interactions = {
            interaction.round_id: interaction
            for interaction in (
                self.db.query(AgentInteraction)
                .filter(
                    AgentInteraction.session_id == session_id,
                    AgentInteraction.status == "pending",
                )
                .all()
            )
            if (
                interaction.kind != "tool_approval"
                or resumable_approval_ids is None
                or interaction.id in resumable_approval_ids
            )
        }
        if subagent_child_round_ids:
            rounds = [round_obj for round_obj in rounds if round_obj.id not in subagent_child_round_ids]

        result = []
        for round_obj in rounds:
            # 从 AG-UI 事件重建步骤
            assistant_file_references: List[Dict] = []
            steps, last_event_sequence = self._rebuild_steps_from_events(
                round_obj.id,
                return_last_sequence=True,
                assistant_file_references=assistant_file_references,
            )
            deduplicated_file_references: dict[str, Dict] = {}
            for reference in assistant_file_references:
                identity = (
                    f"workspace:{reference.get('entry_id')}"
                    if reference.get("source") == "workspace"
                    else f"session:{reference.get('session_id')}:{reference.get('path')}"
                )
                deduplicated_file_references[identity] = reference
            attachments: List[Dict] = []
            if round_obj.user_attachments:
                try:
                    parsed = json.loads(round_obj.user_attachments)
                    if isinstance(parsed, list):
                        attachments = parsed
                except json.JSONDecodeError:
                    attachments = []

            preferred_skills: List[Dict[str, str]] = []
            if round_obj.preferred_skills:
                try:
                    parsed_preferred_skills = json.loads(round_obj.preferred_skills)
                    if isinstance(parsed_preferred_skills, list) and all(
                        isinstance(item, dict)
                        and isinstance(item.get("key"), str)
                        and isinstance(item.get("display_name"), str)
                        for item in parsed_preferred_skills
                    ):
                        preferred_skills = [
                            {
                                "key": item["key"],
                                "display_name": item["display_name"],
                            }
                            for item in parsed_preferred_skills
                        ]
                except (json.JSONDecodeError, TypeError):
                    preferred_skills = []

            preferred_mcp_connections: List[Dict[str, str]] = []
            if round_obj.preferred_mcp_connections:
                try:
                    parsed_preferred_mcp_connections = json.loads(
                        round_obj.preferred_mcp_connections
                    )
                    if isinstance(parsed_preferred_mcp_connections, list) and all(
                        isinstance(item, dict)
                        and isinstance(item.get("server_id"), str)
                        and isinstance(item.get("display_name"), str)
                        for item in parsed_preferred_mcp_connections
                    ):
                        preferred_mcp_connections = [
                            {
                                "server_id": item["server_id"],
                                "display_name": item["display_name"],
                            }
                            for item in parsed_preferred_mcp_connections
                        ]
                except (json.JSONDecodeError, TypeError):
                    preferred_mcp_connections = []

            interrupt_details = None
            if round_obj.status == "waiting_interaction":
                interaction = pending_interactions.get(round_obj.id)
                if interaction is not None:
                    try:
                        request_payload = json.loads(interaction.request_payload)
                    except (TypeError, json.JSONDecodeError):
                        request_payload = {}
                    nested_payload = (
                        request_payload.get("payload")
                        if isinstance(request_payload, dict)
                        and isinstance(request_payload.get("payload"), dict)
                        else {}
                    )
                    interrupt_details = {
                        "id": interaction.id,
                        "reason": (
                            "human_approval"
                            if interaction.kind == "tool_approval"
                            else "input_required"
                        ),
                        "payload": {
                            **nested_payload,
                            "kind": interaction.kind,
                            "tool_call_id": interaction.tool_call_id,
                        },
                    }

            result.append(
                {
                    "round_id": round_obj.id,
                    "parent_run_id": round_obj.parent_run_id,
                    "idempotency_key": round_obj.idempotency_key,
                    "last_event_sequence": last_event_sequence,
                    "user_message": round_obj.user_message,
                    "user_attachments": attachments,
                    "assistant_file_references": list(
                        deduplicated_file_references.values()
                    ),
                    "preferred_skills": preferred_skills,
                    "preferred_mcp_connections": preferred_mcp_connections,
                    "thinking_mode": round_obj.thinking_mode,
                    "reasoning_effort": round_obj.reasoning_effort,
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

        session_user_id = self.db.query(Session.user_id).filter(
            Session.id == session_id,
        ).scalar()
        workspace_attachment_ids = {
            str(attachment.get("entry_id"))
            for item in result for attachment in item.get("user_attachments", [])
            if attachment.get("source") == "workspace" and attachment.get("entry_id")
        }
        workspace_reference_entry_ids = {
            str(reference.get("entry_id"))
            for item in result
            for reference in item.get("assistant_file_references", [])
            if reference.get("source") == "workspace" and reference.get("entry_id")
        }
        workspace_entry_ids = workspace_attachment_ids | workspace_reference_entry_ids
        active_ids: set[str] = set()
        if workspace_entry_ids:
            from src.api.models.workspace import WorkspaceEntry
            active_ids = {
                str(row[0]) for row in self.db.query(WorkspaceEntry.entry_id)
                .filter(
                    WorkspaceEntry.user_id == session_user_id,
                    WorkspaceEntry.entry_id.in_(tuple(workspace_entry_ids)),
                    WorkspaceEntry.status == "active",
                )
                .all()
            }
        if workspace_attachment_ids:
            for item in result:
                item["user_attachments"] = [
                    attachment for attachment in item["user_attachments"]
                    if attachment.get("source") != "workspace"
                    or str(attachment.get("entry_id")) in active_ids
                ]

        workspace_version_ids = {
            str(reference.get("version_id"))
            for item in result
            for reference in item.get("assistant_file_references", [])
            if reference.get("source") == "workspace" and reference.get("version_id")
        }
        if workspace_version_ids:
            materialized_versions = {
                str(row[0])
                for row in self.db.query(WorkspaceFileVersion.version_id).filter(
                    WorkspaceFileVersion.user_id == session_user_id,
                    WorkspaceFileVersion.version_id.in_(tuple(workspace_version_ids)),
                    WorkspaceFileVersion.state == "materialized",
                ).all()
            }
            for item in result:
                item["assistant_file_references"] = [
                    reference
                    for reference in item.get("assistant_file_references", [])
                    if reference.get("source") != "workspace"
                    or (
                        str(reference.get("entry_id")) in active_ids
                        and str(reference.get("version_id")) in materialized_versions
                    )
                ]

        return result

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

    async def save_agui_event(
        self,
        run_id: str,
        event: AGUIEvent,
        *,
        continuation_fence: ContinuationWriteFence | None = None,
    ) -> Optional[StoredEvent]:
        """Store one non-terminal AG-UI event via AguiEventBus."""
        event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
        if event_type in (EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value):
            if self.is_round_terminal(run_id):
                logger.info("Run %s 已终态，丢弃迟到 terminal 事件: %s", run_id, event_type)
                return None
            raise ValueError("terminal events must be written via complete_round/RunCompletionService")
        return await AguiEventBus(self.db).publish(
            run_id,
            event,
            continuation_fence=continuation_fence,
        )
    
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
        history_strategy: str | None = None,
        checkpoint_id: str | None = None,
        call_kind: str = "agent_step",
    ) -> LLMCallRecord:
        """持久化单次 LLM 调用快照。"""
        request_message_count = len(request_messages)
        if (
            len(request_messages) == 1
            and isinstance(request_messages[0], dict)
            and isinstance(request_messages[0].get("messages"), list)
        ):
            request_message_count = len(request_messages[0]["messages"])

        provider_messages = request_messages
        if (
            len(request_messages) == 1
            and isinstance(request_messages[0], dict)
            and isinstance(request_messages[0].get("messages"), list)
        ):
            provider_messages = request_messages[0]["messages"]

        breakdown = {
            "real_user": 0,
            "assistant": 0,
            "assistant_with_tool_calls": 0,
            "tool_results": 0,
            "synthetic_user": 0,
            "automatic_image_context": 0,
            "compaction_summary": 0,
        }
        for message in provider_messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            is_synthetic = bool(message.get("is_synthetic", False))
            if role == "user":
                breakdown["synthetic_user" if is_synthetic else "real_user"] += 1
                if isinstance(content, str) and content.startswith(SUMMARY_PREFIX):
                    breakdown["compaction_summary"] += 1
                if isinstance(content, list):
                    breakdown["automatic_image_context"] += sum(
                        1
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "image_url"
                    )
            elif role == "assistant":
                breakdown["assistant"] += 1
                if message.get("tool_calls"):
                    breakdown["assistant_with_tool_calls"] += 1
                if isinstance(content, str) and content.startswith("[Cumulative Conversation Summary"):
                    breakdown["compaction_summary"] += 1
            elif role == "tool":
                breakdown["tool_results"] += 1

        request_json = json.dumps(request_messages, ensure_ascii=False)
        payload_sha256 = hashlib.sha256(request_json.encode("utf-8")).hexdigest()

        row = LLMCallRecord(
            session_id=session_id,
            round_id=round_id,
            step_index=step_index,
            call_kind=call_kind,
            request_message_count=request_message_count,
            manual_review_status="没问题",
            request_messages=request_json,
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
            history_strategy=history_strategy,
            checkpoint_id=checkpoint_id,
            history_payload_sha256=payload_sha256,
            history_breakdown_json=json.dumps(breakdown, ensure_ascii=False, separators=(",", ":")),
        )
        self.db.add(row)
        self.db.commit()
        self._refresh_detached(row)
        return row
