"""对话历史服务

負責管理對話歷史記錄，使用 Round + AGUIEventLog 雙表結構：
- Round: 對話輪次（用戶輸入 + 最終響應）
- AGUIEventLog: AG-UI 事件流（包含完整的步驟細節，用於 SSE 重連和歷史重建）
"""
from sqlalchemy.orm import Session as DBSession
from src.api.models.session import Session
from src.api.models.round import Round
from src.api.models.agui_event import AGUIEventLog
from src.agent.schema.agui_events import AGUIEvent, EventType
from typing import List, Dict, Optional, AsyncIterator
from datetime import datetime
from src.api.utils.timezone import now_naive
import json


class HistoryService:
    """对话历史服务"""

    def __init__(self, db: DBSession):
        self.db = db
        # per-instance 状态，避免类级别共享导致跨会话数据混乱
        self._event_sequences: Dict[str, int] = {}
        self._stream_buffers: Dict[str, Dict[str, str]] = {}

    # 🆕 Round 相关方法

    def create_round(
        self,
        session_id: str,
        round_id: str,
        user_message: str,
        user_attachments: Optional[List[Dict]] = None,
    ) -> Round:
        """创建新的对话轮次"""
        round_obj = Round(
            id=round_id,
            session_id=session_id,
            user_message=user_message,
            user_attachments=json.dumps(user_attachments or [], ensure_ascii=False),
            status="running",
        )
        self.db.add(round_obj)
        self.db.commit()
        self.db.refresh(round_obj)
        return round_obj

    def resolve_interrupted_rounds(self, session_id: str) -> int:
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
        if updated:
            self.db.commit()
        return updated

    def complete_round(
        self, round_id: str, final_response: str, step_count: int,
        status: str = "completed", interrupt_payload: str | None = None,
    ) -> Round:
        """完成对话轮次"""
        round_obj = self.db.query(Round).filter(Round.id == round_id).first()
        if round_obj:
            round_obj.final_response = final_response
            round_obj.step_count = step_count
            round_obj.status = status
            round_obj.interrupt_payload = interrupt_payload
            round_obj.completed_at = now_naive() if status != "interrupted" else None
            self.db.commit()
            self.db.refresh(round_obj)
        return round_obj

    def _rebuild_steps_from_events(self, run_id: str) -> List[Dict]:
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
        
        steps = []
        current_step = None
        current_tool_call = None
        
        for event_log in events:
            try:
                event_data = json.loads(event_log.payload)
                event_type = event_log.event_type
                
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
                    }
                    steps.append(current_step)
                    
                elif event_type == "STEP_FINISHED" and current_step:
                    current_step["status"] = "completed"
                    
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
                    }
                    current_step["tool_results"].append(result)
                    
            except (json.JSONDecodeError, KeyError) as e:
                print(f"⚠️ 解析事件失败: {e} (run_id={run_id}, id={event_log.id})")
                continue
        
        return steps

    def get_session_rounds(self, session_id: str) -> List[Dict]:
        """获取会话的所有轮次
        
        步骤(steps)从 AG-UI 事件日志动态重建，而非单独存储。
        """
        rounds = (
            self.db.query(Round)
            .filter(Round.session_id == session_id)
            .order_by(Round.created_at)
            .all()
        )

        result = []
        for round_obj in rounds:
            # 从 AG-UI 事件重建步骤
            steps = self._rebuild_steps_from_events(round_obj.id)
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

    # =========================================================================
    # AG-UI 事件相關方法
    # =========================================================================

    # 需要聚合的流式 delta 事件類型
    _STREAM_DELTA_EVENTS = {
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.THINKING_TEXT_MESSAGE_CONTENT,
        EventType.TOOL_CALL_ARGS,
    }
    
    # delta 事件 -> 結束事件的映射
    _STREAM_END_EVENTS = {
        EventType.TEXT_MESSAGE_END: (EventType.TEXT_MESSAGE_CONTENT, "_TEXT"),
        EventType.THINKING_TEXT_MESSAGE_END: (EventType.THINKING_TEXT_MESSAGE_CONTENT, "_THINKING"),
        EventType.TOOL_CALL_END: (EventType.TOOL_CALL_ARGS, "_TOOL"),
    }
    
    async def save_agui_event(self, run_id: str, event: AGUIEvent) -> Optional[AGUIEventLog]:
        """存儲 AG-UI 事件（流式事件聚合優化）
        
        對於流式 delta 事件（TEXT_MESSAGE_CONTENT, THINKING_TEXT_MESSAGE_CONTENT, TOOL_CALL_ARGS），
        在內存中累積，等 *_END 事件時聚合為單條記錄寫入，減少數據庫寫入量。
        
        Args:
            run_id: 運行 ID
            event: AG-UI 事件對象
            
        Returns:
            AGUIEventLog: 存儲的事件日誌記錄（流式 delta 事件返回 None）
        """
        # 初始化緩衝區
        if run_id not in self._stream_buffers:
            self._stream_buffers[run_id] = {}
        
        # === 流式 delta 事件：累積到緩衝區，不寫入數據庫 ===
        if event.type in self._STREAM_DELTA_EVENTS:
            buffer_key = self._get_buffer_key(event)
            if buffer_key:
                delta = getattr(event, 'delta', '')
                if buffer_key not in self._stream_buffers[run_id]:
                    self._stream_buffers[run_id][buffer_key] = ""
                self._stream_buffers[run_id][buffer_key] += delta
            return None  # 不寫入數據庫
        
        # === *_END 事件：先寫入合成的 CONTENT 事件，再寫入乾淨的 END 事件 ===
        if event.type in self._STREAM_END_EVENTS:
            content_event_type, _suffix = self._STREAM_END_EVENTS[event.type]
            buffer_key = self._get_buffer_key(event)
            if buffer_key and buffer_key in self._stream_buffers.get(run_id, {}):
                accumulated_content = self._stream_buffers[run_id].pop(buffer_key, "")
                if accumulated_content:
                    # 構建標準 AG-UI CONTENT 事件 payload
                    synthetic_payload = {
                        "type": content_event_type.value if hasattr(content_event_type, 'value') else str(content_event_type),
                        "delta": accumulated_content,
                    }
                    # 根據事件類型添加 messageId 或 toolCallId
                    msg_id = getattr(event, 'message_id', None)
                    tool_id = getattr(event, 'tool_call_id', None)
                    if msg_id:
                        synthetic_payload["messageId"] = msg_id
                    if tool_id:
                        synthetic_payload["toolCallId"] = tool_id
                    
                    # 先寫入合成的 CONTENT 事件（標準 AG-UI 協議格式）
                    await self._write_synthetic_event_to_db(
                        run_id=run_id,
                        event_type_str=content_event_type.value if hasattr(content_event_type, 'value') else str(content_event_type),
                        payload_dict=synthetic_payload,
                        message_id=msg_id,
                        tool_call_id=tool_id,
                    )
        
        # === END 事件和其他事件：正常寫入（END 事件不帶 fullContent）===
        return await self._write_event_to_db(run_id, event)
    
    def _get_buffer_key(self, event: AGUIEvent) -> Optional[str]:
        """根據事件類型獲取緩衝區 key"""
        message_id = getattr(event, 'message_id', None)
        tool_call_id = getattr(event, 'tool_call_id', None)
        
        if event.type in {EventType.TEXT_MESSAGE_CONTENT, EventType.TEXT_MESSAGE_END}:
            return f"{message_id}_TEXT" if message_id else None
        elif event.type in {EventType.THINKING_TEXT_MESSAGE_CONTENT, EventType.THINKING_TEXT_MESSAGE_END}:
            return f"{message_id}_THINKING" if message_id else None
        elif event.type in {EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END}:
            return f"{tool_call_id}_TOOL" if tool_call_id else None
        return None
    
    async def _write_synthetic_event_to_db(
        self,
        run_id: str,
        event_type_str: str,
        payload_dict: dict,
        message_id: str = None,
        tool_call_id: str = None,
    ) -> AGUIEventLog:
        """寫入合成事件到數據庫（用於流式聚合後的 CONTENT 事件）
        
        在 *_END 事件之前插入一條標準的 CONTENT/ARGS 事件，
        使 DB 中的事件序列符合 AG-UI 協議（START → CONTENT → END）。
        """
        if run_id not in self._event_sequences:
            self._event_sequences[run_id] = 0
        self._event_sequences[run_id] += 1
        sequence = self._event_sequences[run_id]
        
        event_log = AGUIEventLog(
            run_id=run_id,
            event_type=event_type_str,
            message_id=message_id,
            tool_call_id=tool_call_id,
            timestamp=None,
            payload=json.dumps(payload_dict, ensure_ascii=False),
            sequence=sequence,
        )
        self.db.add(event_log)
        # 不單獨 commit，跟隨後面的 END 事件一起提交
        return event_log
    
    async def _write_event_to_db(self, run_id: str, event: AGUIEvent, payload_override: str = None) -> AGUIEventLog:
        """實際寫入事件到數據庫"""
        # 獲取下一個序列號
        if run_id not in self._event_sequences:
            self._event_sequences[run_id] = 0
        self._event_sequences[run_id] += 1
        sequence = self._event_sequences[run_id]
        
        # 提取可選字段
        message_id = getattr(event, 'message_id', None)
        tool_call_id = getattr(event, 'tool_call_id', None)
        timestamp = getattr(event, 'timestamp', None)
        
        # 創建事件日誌記錄
        event_log = AGUIEventLog(
            run_id=run_id,
            event_type=event.type.value if hasattr(event.type, 'value') else str(event.type),
            message_id=message_id,
            tool_call_id=tool_call_id,
            timestamp=timestamp,  # 🔥 修復：提取 event.timestamp
            payload=payload_override or event.model_dump_json(by_alias=True),
            sequence=sequence,
        )
        
        self.db.add(event_log)
        
        # 關鍵事件立即提交，防止崩潰時丟失
        critical_events = {
            EventType.RUN_STARTED,
            EventType.RUN_FINISHED,
            EventType.RUN_ERROR,
            EventType.STEP_STARTED,
            EventType.STEP_FINISHED,
        }
        if event.type in critical_events:
            self.db.commit()
        # 其他事件每10個批量提交
        elif sequence % 10 == 0:
            self.db.commit()
        
        return event_log
    
    def flush_agui_events(self, run_id: str):
        """刷新未提交的事件到數據庫
        
        Args:
            run_id: 運行 ID
        """
        self.db.commit()
        # 清理序列號追蹤
        if run_id in self._event_sequences:
            del self._event_sequences[run_id]
        # 清理流式緩衝區
        if run_id in self._stream_buffers:
            del self._stream_buffers[run_id]
    
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
                # 不注入 _sequence，保持 AG-UI 协议一致性
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
        rounds = (
            self.db.query(Round)
            .filter(Round.session_id == session_id, Round.status == "completed")
            .order_by(Round.created_at)
            .all()
        )
        
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

    # =========================================================================
    # 系统消息注入（Cron 结果等）
    # =========================================================================

    def inject_system_round(
        self,
        session_id: str,
        content: str,
        source: str = "system",
    ) -> str:
        """向 Session 注入一个系统消息 Round（用于 Cron 结果推送等）

        创建一个完整的 Round + AG-UI 事件序列，前端可通过轮询发现。

        Args:
            session_id: 目标 Session ID
            content: 消息正文（Markdown）
            source: 来源标识（如 "cron:daily_report"）

        Returns:
            新创建的 round_id
        """
        import uuid
        import time

        round_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        now_ts = int(time.time() * 1000)

        # 1. 创建 Round（直接标记为 completed）
        round_obj = Round(
            id=round_id,
            session_id=session_id,
            user_message=f"[{source}]",
            status="completed",
            final_response=content,
            step_count=1,
            completed_at=now_naive(),
        )
        self.db.add(round_obj)

        # 2. 写入完整的 AG-UI 事件序列
        events_data = [
            # RUN_STARTED
            {
                "type": "RUN_STARTED",
                "runId": round_id,
                "threadId": session_id,
                "timestamp": now_ts,
            },
            # STEP_STARTED
            {
                "type": "STEP_STARTED",
                "timestamp": now_ts,
            },
            # TEXT_MESSAGE_START
            {
                "type": "TEXT_MESSAGE_START",
                "messageId": message_id,
                "role": "assistant",
                "timestamp": now_ts,
            },
            # TEXT_MESSAGE_CONTENT (aggregated)
            {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": message_id,
                "delta": content,
            },
            # TEXT_MESSAGE_END
            {
                "type": "TEXT_MESSAGE_END",
                "messageId": message_id,
            },
            # STEP_FINISHED
            {
                "type": "STEP_FINISHED",
                "timestamp": now_ts,
            },
            # RUN_FINISHED
            {
                "type": "RUN_FINISHED",
                "runId": round_id,
                "threadId": session_id,
                "outcome": "success",
                "timestamp": now_ts,
            },
        ]

        for seq, payload in enumerate(events_data, start=1):
            event_type = payload["type"]
            event_log = AGUIEventLog(
                run_id=round_id,
                event_type=event_type,
                message_id=payload.get("messageId"),
                tool_call_id=None,
                timestamp=payload.get("timestamp"),
                payload=json.dumps(payload, ensure_ascii=False),
                sequence=seq,
            )
            self.db.add(event_log)

        self.db.commit()
        return round_id
