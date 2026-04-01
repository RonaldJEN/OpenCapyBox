"""AG-UI 事件發射器 - Agent 原生輸出 AG-UI 事件

此模組封裝了 AG-UI 事件的生成邏輯，使 Agent 能夠直接輸出標準 AG-UI 事件流。
"""

from typing import Optional, Any
import uuid
import time

from .schema.agui_events import (
    AGUIEvent, EventType,
    RunStartedEvent, RunFinishedEvent, RunErrorEvent,
    StepStartedEvent, StepFinishedEvent,
    TextMessageStartEvent, TextMessageContentEvent, TextMessageEndEvent,
    ThinkingTextMessageStartEvent, ThinkingTextMessageContentEvent, ThinkingTextMessageEndEvent,
    ToolCallStartEvent, ToolCallArgsEvent, ToolCallEndEvent, ToolCallResultEvent,
    StateSnapshotEvent, StateDeltaEvent, AgentState,
    ActivitySnapshotEvent, ActivityDeltaEvent,
    CustomEvent,
)


class AGUIEventEmitter:
    """AG-UI 事件發射器
    
    封裝事件生成邏輯，追蹤流式狀態，確保事件序列的正確性。
    
    使用方式:
        emitter = AGUIEventEmitter(thread_id="thread_123", run_id="run_456")
        
        yield emitter.run_started()
        yield emitter.text_message_start(role="assistant")
        yield emitter.text_message_content("Hello")
        yield emitter.text_message_end()
        yield emitter.run_finished()
    """
    
    def __init__(self, thread_id: str, run_id: str):
        """初始化事件發射器
        
        Args:
            thread_id: 對話線程 ID（等同於 session_id）
            run_id: 運行 ID（等同於 round_id）
        """
        self.thread_id = thread_id
        self.run_id = run_id
        self._sequence = 0
        
        # 流式狀態追蹤
        self._current_message_id: Optional[str] = None
        self._current_thinking_id: Optional[str] = None
        self._current_activity_id: Optional[str] = None
        self._tool_call_states: dict[str, dict] = {}  # tool_call_id -> {started, args_sent}
        
    def _next_seq(self) -> int:
        """獲取下一個序列號"""
        self._sequence += 1
        return self._sequence
    
    def _gen_id(self, prefix: str = "id") -> str:
        """生成唯一 ID"""
        return f"{prefix}_{uuid.uuid4().hex[:12]}"
    
    # =========================================================================
    # 生命週期事件
    # =========================================================================
    
    def run_started(self) -> RunStartedEvent:
        """發射運行開始事件"""
        return RunStartedEvent(
            thread_id=self.thread_id,
            run_id=self.run_id,
        )
    
    def run_finished(self, outcome: str = "success", result: any = None) -> RunFinishedEvent:
        """發射運行結束事件
        
        Args:
            outcome: 運行結果，"success" 或 "interrupt"
            result: 可選的運行結果數據
        """
        return RunFinishedEvent(
            thread_id=self.thread_id,
            run_id=self.run_id,
            outcome=outcome,
            result=result,
        )
    
    def run_error(self, message: str, code: Optional[str] = None) -> RunErrorEvent:
        """發射運行錯誤事件
        
        Args:
            message: 錯誤消息
            code: 錯誤代碼（可選）
        """
        return RunErrorEvent(message=message, code=code)
    
    def step_started(self, step_name: str) -> StepStartedEvent:
        """發射步驟開始事件
        
        Args:
            step_name: 步驟名稱，如 "step_1"
        """
        return StepStartedEvent(step_name=step_name)
    
    def step_finished(self, step_name: str) -> StepFinishedEvent:
        """發射步驟結束事件
        
        Args:
            step_name: 步驟名稱
        """
        return StepFinishedEvent(step_name=step_name)
    
    # =========================================================================
    # 文本消息事件
    # =========================================================================
    
    def text_message_start(self, role: str = "assistant", message_id: Optional[str] = None) -> TextMessageStartEvent:
        """發射文本消息開始事件
        
        Args:
            role: 消息角色，默認 "assistant"
            message_id: 可選的消息 ID，未提供則自動生成
        """
        self._current_message_id = message_id or self._gen_id("msg")
        return TextMessageStartEvent(
            message_id=self._current_message_id,
            role=role,
        )
    
    def text_message_content(self, delta: str) -> Optional[TextMessageContentEvent]:
        """發射文本消息內容事件
        
        Args:
            delta: 增量文本內容
            
        Returns:
            TextMessageContentEvent 或 None（如果 delta 為空或無活動消息）
        """
        if not delta or not self._current_message_id:
            return None
        return TextMessageContentEvent(
            message_id=self._current_message_id,
            delta=delta,
        )
    
    def text_message_end(self) -> Optional[TextMessageEndEvent]:
        """發射文本消息結束事件
        
        Returns:
            TextMessageEndEvent 或 None（如果無活動消息）
        """
        if not self._current_message_id:
            return None
        event = TextMessageEndEvent(message_id=self._current_message_id)
        self._current_message_id = None
        return event
    
    @property
    def current_message_id(self) -> Optional[str]:
        """獲取當前活動消息 ID"""
        return self._current_message_id
    
    # =========================================================================
    # 思考過程事件
    # =========================================================================
    
    def thinking_start(self, message_id: Optional[str] = None) -> ThinkingTextMessageStartEvent:
        """發射思考開始事件
        
        Args:
            message_id: 可選的消息 ID，未提供則自動生成
        """
        self._current_thinking_id = message_id or self._gen_id("think")
        return ThinkingTextMessageStartEvent(message_id=self._current_thinking_id)
    
    def thinking_content(self, delta: str) -> Optional[ThinkingTextMessageContentEvent]:
        """發射思考內容事件
        
        Args:
            delta: 增量思考內容
            
        Returns:
            ThinkingTextMessageContentEvent 或 None（如果 delta 為空或無活動思考）
        """
        if not delta or not self._current_thinking_id:
            return None
        return ThinkingTextMessageContentEvent(
            message_id=self._current_thinking_id,
            delta=delta,
        )
    
    def thinking_end(self) -> Optional[ThinkingTextMessageEndEvent]:
        """發射思考結束事件
        
        Returns:
            ThinkingTextMessageEndEvent 或 None（如果無活動思考）
        """
        if not self._current_thinking_id:
            return None
        event = ThinkingTextMessageEndEvent(message_id=self._current_thinking_id)
        self._current_thinking_id = None
        return event
    
    @property
    def current_thinking_id(self) -> Optional[str]:
        """獲取當前活動思考 ID"""
        return self._current_thinking_id
    
    # =========================================================================
    # 工具調用事件
    # =========================================================================
    
    def tool_call_start(
        self, 
        tool_call_id: str, 
        tool_name: str,
        parent_message_id: Optional[str] = None,
    ) -> ToolCallStartEvent:
        """發射工具調用開始事件
        
        Args:
            tool_call_id: 工具調用 ID
            tool_name: 工具名稱
            parent_message_id: 父消息 ID（可選，默認使用當前消息 ID）
        """
        self._tool_call_states[tool_call_id] = {"started": True, "args_buffer": ""}
        return ToolCallStartEvent(
            tool_call_id=tool_call_id,
            tool_call_name=tool_name,
            parent_message_id=parent_message_id or self._current_message_id,
        )
    
    def tool_call_args(self, tool_call_id: str, delta: str) -> Optional[ToolCallArgsEvent]:
        """發射工具調用參數事件
        
        Args:
            tool_call_id: 工具調用 ID
            delta: 參數 JSON 片段
            
        Returns:
            ToolCallArgsEvent 或 None（如果 delta 為空）
        """
        if not delta:
            return None
        
        # 累積參數以便追蹤
        if tool_call_id in self._tool_call_states:
            self._tool_call_states[tool_call_id]["args_buffer"] += delta
            
        return ToolCallArgsEvent(tool_call_id=tool_call_id, delta=delta)
    
    def tool_call_end(self, tool_call_id: str) -> ToolCallEndEvent:
        """發射工具調用結束事件
        
        Args:
            tool_call_id: 工具調用 ID
        """
        return ToolCallEndEvent(tool_call_id=tool_call_id)
    
    def tool_call_result(
        self, 
        tool_call_id: str, 
        content: str,
        message_id: Optional[str] = None,
        execution_time_ms: Optional[int] = None,
    ) -> ToolCallResultEvent:
        """發射工具調用結果事件
        
        Args:
            tool_call_id: 工具調用 ID
            content: 工具執行結果
            message_id: 可選的結果消息 ID，未提供則自動生成
            execution_time_ms: 工具執行耗時（毫秒）
        """
        return ToolCallResultEvent(
            message_id=message_id or self._gen_id("result"),
            tool_call_id=tool_call_id,
            content=content,
            role="tool",
            execution_time_ms=execution_time_ms,
        )
    
    def get_tool_call_args(self, tool_call_id: str) -> str:
        """獲取工具調用累積的參數"""
        if tool_call_id in self._tool_call_states:
            return self._tool_call_states[tool_call_id].get("args_buffer", "")
        return ""
    
    # =========================================================================
    # 狀態事件
    # =========================================================================
    
    def state_snapshot(self, state: AgentState) -> StateSnapshotEvent:
        """發射狀態快照事件
        
        Args:
            state: Agent 狀態對象
        """
        return StateSnapshotEvent(snapshot=state.model_dump(by_alias=True))
    
    def state_delta(self, delta: list[dict]) -> StateDeltaEvent:
        """發射狀態增量事件
        
        Args:
            delta: JSON Patch 操作列表 (RFC 6902)
                   例: [{"op": "replace", "path": "/currentStep", "value": 2}]
        """
        return StateDeltaEvent(delta=delta)
    
    # =========================================================================
    # 便捷方法
    # =========================================================================
    
    def create_state(
        self,
        current_step: int = 0,
        total_steps: Optional[int] = None,
        status: str = "running",
    ) -> AgentState:
        """創建 AgentState 對象
        
        Args:
            current_step: 當前步驟編號
            total_steps: 總步驟數（可選）
            status: 狀態，可選 "idle", "running", "waiting", "completed", "error"
        """
        return AgentState(
            current_step=current_step,
            total_steps=total_steps,
            status=status,
        )
    
    def reset(self):
        """重置發射器狀態（用於新的運行）"""
        self._sequence = 0
        self._current_message_id = None
        self._current_thinking_id = None
        self._tool_call_states.clear()
        self._current_activity_id = None
    
    # =========================================================================
    # 活動事件 (ACTIVITY) - 用於展示 Agent 當前狀態/進度
    # =========================================================================
    
    def activity_snapshot(
        self,
        activity_type: str,
        content: dict,
        message_id: Optional[str] = None,
        replace: bool = True,
    ) -> ActivitySnapshotEvent:
        """發射活動快照事件
        
        用於展示 Agent 的當前活動狀態，如工具執行進度、搜索結果等。
        
        Args:
            activity_type: 活動類型，如 "tool_execution", "search", "planning"
            content: 活動內容（任意字典）
            message_id: 可選的消息 ID，未提供則自動生成
            replace: 是否替換現有活動（默認 True）
            
        Example:
            yield emitter.activity_snapshot(
                activity_type="tool_execution",
                content={"tool": "bash", "status": "running", "progress": 50}
            )
        """
        self._current_activity_id = message_id or self._gen_id("activity")
        return ActivitySnapshotEvent(
            message_id=self._current_activity_id,
            activity_type=activity_type,
            content=content,
            replace=replace,
        )
    
    def activity_delta(
        self,
        activity_type: str,
        patch: list[dict],
        message_id: Optional[str] = None,
    ) -> Optional[ActivityDeltaEvent]:
        """發射活動增量事件 (JSON Patch)
        
        用於增量更新活動狀態，減少傳輸數據量。
        
        Args:
            activity_type: 活動類型
            patch: JSON Patch 操作列表 (RFC 6902)
                   例: [{"op": "replace", "path": "/progress", "value": 75}]
            message_id: 可選的消息 ID，未提供則使用當前活動 ID
            
        Returns:
            ActivityDeltaEvent 或 None（如果無活動 ID）
        """
        target_id = message_id or self._current_activity_id
        if not target_id:
            return None
        return ActivityDeltaEvent(
            message_id=target_id,
            activity_type=activity_type,
            patch=patch,
        )
    
    @property
    def current_activity_id(self) -> Optional[str]:
        """獲取當前活動 ID"""
        return self._current_activity_id
    
    # =========================================================================
    # 自定義事件 (CUSTOM) - 用於擴展功能
    # =========================================================================
    
    def custom_event(self, name: str, value: Any) -> CustomEvent:
        """發射自定義事件
        
        用於傳遞自定義數據，如標題更新、心跳等。
        
        Args:
            name: 事件名稱
            value: 事件值（任意類型）
            
        Example:
            yield emitter.custom_event("title_updated", {"title": "新標題"})
        """
        return CustomEvent(name=name, value=value)
    
    def heartbeat(self) -> CustomEvent:
        """發射心跳事件"""
        return CustomEvent(
            name="heartbeat",
            value={"timestamp": int(time.time() * 1000)},
        )
