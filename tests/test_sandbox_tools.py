"""Sandbox 工具測試

測試 SandboxBashTool / SandboxFileTools / SandboxNoteTool
全部使用 mock Sandbox，不依賴真實遠程沙箱。
"""
import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from opensandbox.models.execd import CommandStatus

from src.agent.tools.sandbox_bash_tool import (
    SandboxBashTool,
    SandboxBashOutputTool,
    SandboxBashKillTool,
    SandboxBashOutputResult,
    _BackgroundCommandTracker,
)
from src.agent.tools.sandbox_file_tools import (
    READ_DEFAULT_LIMIT,
    READ_MAX_BYTES,
    READ_MAX_LINE_LENGTH,
    SandboxReadTool,
    SandboxReadImageTool,
    SandboxWriteTool,
    SandboxEditTool,
)
from src.api.services.vision_fallback_service import VisionFallbackError
from src.agent.tools.sandbox_note_tool import (
    SandboxSessionNoteTool,
    SandboxRecallNoteTool,
)
from tests.helpers import make_fake_execution


# ============== Fixtures ==============

# mock_sandbox 已在 conftest.py 中统一定义


@pytest.fixture
def bg_tracker():
    """每個測試都使用獨立的 tracker 實例"""
    return _BackgroundCommandTracker()


# ============== SandboxBashOutputResult ==============

class TestSandboxBashOutputResult:
    """SandboxBashOutputResult 測試"""

    def test_basic_result(self):
        """基本成功結果"""
        r = SandboxBashOutputResult(
            success=True, stdout="hello", stderr="", exit_code=0
        )
        assert r.success is True
        assert "hello" in r.content
        assert r.exit_code == 0

    def test_result_with_stderr(self):
        """帶 stderr"""
        r = SandboxBashOutputResult(
            success=False, stdout="", stderr="Error: not found", exit_code=1
        )
        assert r.success is False
        assert "[stderr]" in r.content
        assert "Error: not found" in r.content

    def test_result_with_bash_id(self):
        """帶 bash_id（後台命令）"""
        r = SandboxBashOutputResult(
            success=True, stdout="started", stderr="", exit_code=0, bash_id="bg-123"
        )
        assert "[bash_id]" in r.content
        assert "bg-123" in r.content

    def test_empty_output(self):
        """空輸出 — 成功時顯示區分文本"""
        r = SandboxBashOutputResult(
            success=True, stdout="", stderr="", exit_code=0
        )
        assert r.success is True
        assert "(command completed with no output)" in r.content

    def test_failed_empty_output_shows_error(self):
        """失敗且無 stdout/stderr 時，error 信息應出現在 content 中"""
        r = SandboxBashOutputResult(
            success=False,
            stdout="",
            stderr="",
            exit_code=-1,
            error="Sandbox returned null execution",
        )
        assert r.success is False
        assert "Sandbox returned null execution" in r.content
        assert "[error]" in r.content


# ============== SandboxBashTool ==============

class TestSandboxBashTool:
    """SandboxBashTool 測試"""

    def test_properties(self, mock_sandbox):
        """測試工具屬性"""
        tool = SandboxBashTool(mock_sandbox)
        assert tool.name == "bash"
        assert len(tool.description) > 0
        assert "current Session directory" in tool.description
        assert "configured sandbox workspace root" not in tool.description
        assert "command" in tool.parameters.get("properties", {})


    def test_description_tells_model_to_set_timeout_for_tests(self, mock_sandbox):
        """工具描述应提醒模型 pytest/build/install 需要显式传 timeout。"""
        tool = SandboxBashTool(mock_sandbox)
        timeout_desc = tool.parameters["properties"]["timeout"]["description"]

        assert "pytest" in tool.description
        assert "timeout=600" in tool.description
        assert "Always set explicitly for tests" in timeout_desc
        assert "pytest --timeout does not change this sandbox command timeout" in timeout_desc

    @pytest.mark.asyncio
    async def test_foreground_command_success(self, mock_sandbox):
        """測試前台成功執行"""
        # Mock execution result
        execution = MagicMock()
        execution.exit_code = 0
        stdout_line = MagicMock()
        stdout_line.text = "Hello World"
        execution.logs.stdout = [stdout_line]
        execution.logs.stderr = []
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="echo Hello World")

        assert result.success is True
        assert "Hello World" in result.content
        assert any(
            call.args and call.args[0] == "echo Hello World"
            for call in mock_sandbox.commands.run.await_args_list
        )
        assert result.assistant_file_references is None

    @pytest.mark.asyncio
    async def test_foreground_uses_custom_workspace(self, mock_sandbox):
        """前台命令應使用自定義工作目錄"""
        execution = MagicMock()
        execution.exit_code = 0
        execution.logs.stdout = []
        execution.logs.stderr = []
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox, workspace_dir="/workspace/session-root")
        await tool.execute(command="pwd")

        call_args = mock_sandbox.commands.run.call_args
        opts = call_args.kwargs.get("opts")
        assert opts is not None
        assert getattr(opts, "working_directory", None) == "/workspace/session-root"

    @pytest.mark.asyncio
    async def test_foreground_command_failure(self, mock_sandbox):
        """測試前台執行失敗（非零退出碼）"""
        execution = MagicMock()
        execution.exit_code = 1
        execution.logs.stdout = []
        stderr_line = MagicMock()
        stderr_line.text = "file not found"
        execution.logs.stderr = [stderr_line]
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="cat missing.txt")

        assert result.success is False
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_background_command(self, mock_sandbox, bg_tracker):
        """測試後台命令"""
        execution = MagicMock()
        execution.id = "cmd-abc123"
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox, tracker=bg_tracker)
        result = await tool.execute(command="sleep 100", run_in_background=True)

        assert result.success is True
        assert result.bash_id is not None
        assert len(result.bash_id) > 0
        # 確認追蹤器中有記錄
        assert bg_tracker.get(result.bash_id) is not None

        opts = mock_sandbox.commands.run.call_args.kwargs["opts"]
        assert getattr(opts, "timeout", None) == timedelta(seconds=21600)

    @pytest.mark.asyncio
    async def test_background_command_timeout_can_be_disabled(self, mock_sandbox, bg_tracker):
        """background_timeout_seconds=0 時不設置服務端 timeout"""
        execution = MagicMock()
        execution.id = "cmd-no-timeout"
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(
            mock_sandbox,
            tracker=bg_tracker,
            background_timeout_seconds=0,
        )
        result = await tool.execute(command="sleep 100", run_in_background=True)

        assert result.success is True
        opts = mock_sandbox.commands.run.call_args.kwargs["opts"]
        assert getattr(opts, "timeout", None) is None

    def test_background_command_timeout_rejects_negative(self, mock_sandbox):
        """负数 timeout 是错误配置，不应静默变成禁用 timeout。"""
        with pytest.raises(ValueError, match="background_timeout_seconds must be >= 0"):
            SandboxBashTool(mock_sandbox, background_timeout_seconds=-1)

    @pytest.mark.asyncio
    async def test_timeout_clamped(self, mock_sandbox):
        """測試 timeout 被限制在 [1, 600]"""
        execution = MagicMock()
        execution.exit_code = 0
        execution.logs.stdout = []
        execution.logs.stderr = []
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        # timeout=9999 should be clamped to 600
        await tool.execute(command="ls", timeout=9999)
        call_kwargs = mock_sandbox.commands.run.call_args
        assert call_kwargs.kwargs.get("timeout", call_kwargs[1].get("timeout", 600)) <= 600

    @pytest.mark.asyncio
    async def test_exception_returns_error_result(self, mock_sandbox):
        """測試異常時返回錯誤結果"""
        mock_sandbox.commands.run = AsyncMock(side_effect=RuntimeError("sandbox crashed"))

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="whoami")

        assert result.success is False
        assert "sandbox crashed" in result.content or "sandbox crashed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_foreground_none_logs_returns_failure(self, mock_sandbox):
        """execution.logs 為 None 時應返回 success=False + 診斷信息"""
        execution = MagicMock(spec=[])  # spec=[] → no attributes by default
        execution.logs = None
        execution.exit_code = None
        execution.error = None
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="echo test")

        assert result.success is False
        assert result.exit_code == -1
        assert "no logs" in (result.error or "").lower()
        assert "not be responsive" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_foreground_none_execution_returns_failure(self, mock_sandbox):
        """execution 本身為 None 時應返回 success=False + 診斷信息"""
        mock_sandbox.commands.run = AsyncMock(return_value=None)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="ls -la")

        assert result.success is False
        assert result.exit_code == -1
        assert "null execution" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_foreground_success_empty_output_text(self, mock_sandbox):
        """前台成功但無輸出時，content 應顯示 '(command completed with no output)'"""
        execution = MagicMock()
        execution.exit_code = 0
        execution.logs.stdout = []
        execution.logs.stderr = []
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="mkdir -p /tmp/test")

        assert result.success is True
        assert "(command completed with no output)" in result.content

    @pytest.mark.asyncio
    async def test_foreground_no_exit_code_empty_output_is_success(self, mock_sandbox):
        """exit_code 不可知且无 error → 视为成功（SDK Execution 本身没有 exit_code 字段）"""
        execution = MagicMock(spec=[])
        execution.logs = MagicMock()
        execution.logs.stdout = []
        execution.logs.stderr = []
        # 无 exit_code、无 error 属性
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="ls")

        assert result.success is True
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_foreground_no_exit_code_with_output_is_success(self, mock_sandbox):
        """exit_code 不可知但有 stdout → 仍视为成功"""
        execution = MagicMock(spec=[])
        execution.logs = MagicMock()
        execution.logs.stdout = [MagicMock(text="file.txt")]
        execution.logs.stderr = []
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="ls")

        assert result.success is True
        assert result.exit_code == 0
        assert "file.txt" in result.content

    @pytest.mark.asyncio
    async def test_foreground_execution_error_captured(self, mock_sandbox):
        """execution.error 携带错误详情时，应反馈给模型"""
        execution = MagicMock()
        execution.exit_code = 1
        execution.logs = MagicMock()
        execution.logs.stdout = []
        execution.logs.stderr = []
        err_obj = MagicMock()
        err_obj.name = "CommandExecError"
        err_obj.value = "fork/exec /usr/bin/bash: no such file or directory"
        # name/value 通过 getattr 正常路径访问
        err_obj.ename = None
        err_obj.evalue = None
        execution.error = err_obj
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="echo hello")

        assert result.success is False
        assert "CommandExecError" in (result.error or "")
        assert "no such file or directory" in (result.error or "")

    @pytest.mark.asyncio
    async def test_foreground_signal_killed_surfaces_traceback_and_timeout_hint(self, mock_sandbox):
        """生产中的 signal:killed 不能只返回 CommandExecError: -1。"""
        execution = MagicMock()
        execution.exit_code = None
        execution.logs = MagicMock()
        execution.logs.stdout = []
        execution.logs.stderr = []
        err_obj = MagicMock()
        err_obj.name = "CommandExecError"
        err_obj.value = "-1"
        err_obj.ename = None
        err_obj.evalue = None
        err_obj.traceback = ["signal: killed"]
        execution.error = err_obj
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(
            command=(
                "cd /home/user/zhigong && python3 -m pytest tests/ -q "
                "--timeout=60 > /tmp/pytest_out.txt 2>&1 || true; "
                "tail -25 /tmp/pytest_out.txt"
            )
        )

        assert result.success is False
        assert "CommandExecError: -1" in (result.error or "")
        assert "signal: killed" in (result.error or "")
        assert "Foreground command timeout was 10s" in (result.error or "")

    @pytest.mark.asyncio
    async def test_foreground_long_traceback_is_truncated_but_keeps_signal_killed(self, mock_sandbox):
        """OpenSandbox 返回长 traceback 时应控长，同时保留关键 kill 信号。"""
        execution = MagicMock()
        execution.exit_code = None
        execution.logs = MagicMock()
        execution.logs.stdout = []
        execution.logs.stderr = []
        err_obj = MagicMock()
        err_obj.name = "CommandExecError"
        err_obj.value = "-1"
        err_obj.ename = None
        err_obj.evalue = None
        err_obj.traceback = [
            f"frame {index}: {'x' * 300}"
            for index in range(50)
        ] + ["signal: killed"]
        execution.error = err_obj
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxBashTool(mock_sandbox)
        result = await tool.execute(command="python3 -m pytest tests/ -q")

        assert result.success is False
        assert "signal: killed" in (result.error or "")
        assert "... truncated:" in (result.error or "")
        assert len(result.error or "") < 2400


# ============== EventNode Monkey-Patch 验证 ==============

class TestEventNodeMonkeyPatch:
    """验证 OpenSandbox SDK 的 EventNode monkey-patch 正确生效"""

    def test_patched_event_node_accepts_null_timestamp(self):
        """timestamp=None 不应导致 Pydantic 校验失败"""
        from src.agent.tools.sandbox_bash_tool import _PatchedEventNode
        node = _PatchedEventNode(type="stdout", text="hello", timestamp=None)
        assert node.type == "stdout"
        assert node.timestamp is None

    def test_patched_event_node_accepts_missing_timestamp(self):
        """缺失 timestamp 不应导致 Pydantic 校验失败"""
        from src.agent.tools.sandbox_bash_tool import _PatchedEventNode
        node = _PatchedEventNode(type="stdout", text="hello")
        assert node.timestamp is None

    def test_patched_event_node_accepts_null_traceback(self):
        """error.traceback=None 不应导致 Pydantic 校验失败"""
        from src.agent.tools.sandbox_bash_tool import _PatchedEventNode
        node = _PatchedEventNode(
            type="error", timestamp=123,
            error={"ename": "Err", "evalue": "msg", "traceback": None},
        )
        assert node.error is not None
        assert node.error.traceback is None

    def test_patched_event_node_ignores_extra_fields(self):
        """未知字段应被忽略而非报错"""
        from src.agent.tools.sandbox_bash_tool import _PatchedEventNode
        node = _PatchedEventNode(type="stdout", text="x", timestamp=1, unknown_field="bar")
        assert node.type == "stdout"

    def test_command_adapter_uses_patched_event_node(self):
        """command_adapter 模块应使用被替换后的 _PatchedEventNode"""
        from src.agent.tools.sandbox_bash_tool import _PatchedEventNode
        import opensandbox.adapters.command_adapter as cm
        assert cm.EventNode is _PatchedEventNode

    @pytest.mark.asyncio
    async def test_dispatcher_handles_null_timestamp(self):
        """dispatch 收到 timestamp=None 的事件应正常处理"""
        from src.agent.tools.sandbox_bash_tool import _PatchedEventNode
        from opensandbox.adapters.converter.execution_event_dispatcher import ExecutionEventDispatcher
        from opensandbox.models.execd import Execution

        execution = Execution(id=None, execution_count=None, result=[], error=None)
        dispatcher = ExecutionEventDispatcher(execution, None)

        evt = _PatchedEventNode(type="stdout", text="hello sandbox", timestamp=None)
        await dispatcher.dispatch(evt)

        assert len(execution.logs.stdout) == 1
        assert execution.logs.stdout[0].text == "hello sandbox"

    @pytest.mark.asyncio
    async def test_dispatcher_handles_null_traceback_in_error(self):
        """dispatch 收到 traceback=None 的 error 事件应正常处理"""
        from src.agent.tools.sandbox_bash_tool import _PatchedEventNode
        from opensandbox.adapters.converter.execution_event_dispatcher import ExecutionEventDispatcher
        from opensandbox.models.execd import Execution

        execution = Execution(id=None, execution_count=None, result=[], error=None)
        dispatcher = ExecutionEventDispatcher(execution, None)

        evt = _PatchedEventNode(
            type="error", timestamp=None,
            error={"ename": "RuntimeError", "evalue": "boom", "traceback": None},
        )
        await dispatcher.dispatch(evt)

        assert execution.error is not None
        assert execution.error.name == "RuntimeError"
        assert execution.error.traceback == []


# ============== SandboxBashOutputTool ==============

class TestSandboxBashOutputTool:
    """SandboxBashOutputTool 測試"""

    def test_properties(self, bg_tracker):
        tool = SandboxBashOutputTool(tracker=bg_tracker)
        assert tool.name == "bash_output"
        assert "bash_id" in tool.parameters.get("properties", {})

    @pytest.mark.asyncio
    async def test_output_unknown_id(self, bg_tracker):
        """查詢不存在的 bash_id"""
        tool = SandboxBashOutputTool(tracker=bg_tracker)
        result = await tool.execute(bash_id="nonexistent")
        assert result.success is False


# ============== SandboxBashKillTool ==============

class TestSandboxBashKillTool:
    """SandboxBashKillTool 測試"""

    def test_properties(self, bg_tracker):
        tool = SandboxBashKillTool(tracker=bg_tracker)
        assert tool.name == "bash_kill"

    @pytest.mark.asyncio
    async def test_kill_unknown_id(self, bg_tracker):
        """終止不存在的 bash_id"""
        tool = SandboxBashKillTool(tracker=bg_tracker)
        result = await tool.execute(bash_id="ghost")
        assert result.success is False


# ============== BackgroundCommandTracker ==============

class TestBackgroundCommandTracker:
    """後台命令追蹤器測試"""

    def test_add_and_get(self, mock_sandbox):
        tracker = _BackgroundCommandTracker()
        tracker.add("bg-1", mock_sandbox, "cmd-1")
        entry = tracker.get("bg-1")
        assert entry is not None
        assert entry.sandbox is mock_sandbox
        assert entry.command_id == "cmd-1"

    def test_remove(self, mock_sandbox):
        tracker = _BackgroundCommandTracker()
        tracker.add("bg-2", mock_sandbox, "cmd-2")
        tracker.remove("bg-2")
        assert tracker.get("bg-2") is None

    def test_remove_nonexistent(self):
        """移除不存在的 key 不報錯"""
        tracker = _BackgroundCommandTracker()
        tracker.remove("bg-nope")  # should not raise

    @pytest.mark.asyncio
    async def test_interrupt_by_sandbox_interrupts_running_command(self, mock_sandbox):
        tracker = _BackgroundCommandTracker()
        tracker.add("bg-run", mock_sandbox, "cmd-run")
        status = CommandStatus(running=True, exit_code=None)
        mock_sandbox.commands.get_command_status = AsyncMock(return_value=status)
        mock_sandbox.commands.interrupt = AsyncMock()

        stats = await tracker.interrupt_by_sandbox(mock_sandbox)

        assert stats == {"matched": 1, "interrupted": 1, "skipped": 0, "failed": 0}
        mock_sandbox.commands.interrupt.assert_awaited_once_with("cmd-run")
        assert tracker.get("bg-run") is None

    @pytest.mark.asyncio
    async def test_interrupt_by_sandbox_skips_finished_command(self, mock_sandbox):
        tracker = _BackgroundCommandTracker()
        tracker.add("bg-done", mock_sandbox, "cmd-done")
        status = CommandStatus(running=False, exit_code=None)
        mock_sandbox.commands.get_command_status = AsyncMock(return_value=status)
        mock_sandbox.commands.interrupt = AsyncMock()

        stats = await tracker.interrupt_by_sandbox(mock_sandbox)

        assert stats == {"matched": 1, "interrupted": 0, "skipped": 1, "failed": 0}
        mock_sandbox.commands.interrupt.assert_not_awaited()
        assert tracker.get("bg-done") is None

    @pytest.mark.asyncio
    async def test_interrupt_by_sandbox_status_failure_best_effort_interrupt(self, mock_sandbox):
        tracker = _BackgroundCommandTracker()
        tracker.add("bg-err", mock_sandbox, "cmd-err")
        mock_sandbox.commands.get_command_status = AsyncMock(side_effect=RuntimeError("status down"))
        mock_sandbox.commands.interrupt = AsyncMock(side_effect=RuntimeError("interrupt down"))

        stats = await tracker.interrupt_by_sandbox(mock_sandbox)

        assert stats == {"matched": 1, "interrupted": 0, "skipped": 0, "failed": 1}
        mock_sandbox.commands.interrupt.assert_awaited_once_with("cmd-err")
        assert tracker.get("bg-err") is None

    @pytest.mark.asyncio
    async def test_interrupt_by_sandbox_status_never_returns_falls_back_to_interrupt(
        self, mock_sandbox, monkeypatch
    ):
        """状态查询永远不返回时，应被 wait_for 超时打断并 best-effort interrupt，而非永久挂起。"""
        import src.agent.tools.sandbox_bash_tool as sandbox_bash_tool

        monkeypatch.setattr(
            sandbox_bash_tool, "BACKGROUND_STATUS_TIMEOUT_SECONDS", 0.05
        )

        tracker = _BackgroundCommandTracker()
        tracker.add("bg-hang", mock_sandbox, "cmd-hang")

        async def _never_returns(_command_id):
            await asyncio.Event().wait()

        mock_sandbox.commands.get_command_status = AsyncMock(side_effect=_never_returns)
        mock_sandbox.commands.interrupt = AsyncMock()

        stats = await asyncio.wait_for(
            tracker.interrupt_by_sandbox(mock_sandbox), timeout=2
        )

        assert stats == {"matched": 1, "interrupted": 1, "skipped": 0, "failed": 0}
        mock_sandbox.commands.interrupt.assert_awaited_once_with("cmd-hang")
        assert tracker.get("bg-hang") is None



# ============== SandboxReadTool ==============

class TestSandboxReadTool:
    """SandboxReadTool 測試"""

    def test_properties(self, mock_sandbox):
        tool = SandboxReadTool(mock_sandbox)
        assert tool.name == "read_file"
        assert "path" in tool.parameters.get("properties", {})
        assert tool.parameters["properties"]["offset"]["minimum"] == 1
        assert tool.parameters["properties"]["limit"]["minimum"] == 1
        assert tool.parameters["properties"]["limit"]["maximum"] == READ_DEFAULT_LIMIT
        assert tool.manages_model_result_size is True

    @pytest.mark.asyncio
    async def test_read_binary_docx_quick_fix_preserves_exact_chinese_etf_path(self, mock_sandbox):
        filename = "xxxx.docx"

        tool = SandboxReadTool(mock_sandbox, workspace_dir="/home/user/sessions/s1")
        result = await tool.execute(path=filename)

        assert result.success is False
        assert "python skills/document-skills/docx/scripts/read_docx.py" in (result.error or "")
        assert f"'/home/user/sessions/s1/{filename}'" in (result.error or "")
        assert "xxxx .docx" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_read_file_success(self, mock_sandbox):
        """成功讀取文件（含 HEADER + FOOTER 閉合邊界）"""
        mock_sandbox.files.read_file = AsyncMock(return_value="line1\nline2\nline3\n")

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="/home/user/test.txt")

        assert result.success is True
        assert "line1" in result.content
        # 驗證頭部邊界和完整性摘要
        assert "=== FILE:" in result.content
        assert "COMPLETE ===" in result.content
        # 驗證 EOF footer
        assert "=== END OF FILE ===" in result.content

    @pytest.mark.asyncio
    async def test_read_file_with_offset_limit(self, mock_sandbox):
        """讀取帶 offset/limit"""
        content = "\n".join(f"line{i}" for i in range(1, 21))
        mock_sandbox.files.read_file = AsyncMock(return_value=content)

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="test.txt", offset=5, limit=3)

        assert result.success is True
        assert "Lines 5-7 of 20 total | PARTIAL ===" in result.content
        assert "     5|line5" in result.content
        assert "     7|line7" in result.content
        assert "     4|line4" not in result.content
        assert "     8|line8" not in result.content
        assert result.content.endswith("=== MORE: use offset=8 to continue ===")
        assert "COMPLETE" not in result.content
        assert "END OF FILE" not in result.content

    @pytest.mark.asyncio
    async def test_read_file_completeness_summary_full_file(self, mock_sandbox):
        """完整性摘要：讀取全部行時應顯示 COMPLETE"""
        lines = "\n".join(f"line{i}" for i in range(1, 86))
        mock_sandbox.files.read_file = AsyncMock(return_value=lines)

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="chat_template.txt")

        assert result.success is True
        assert "=== FILE: chat_template.txt" in result.content
        assert "All 85 lines | COMPLETE ===" in result.content

    @pytest.mark.asyncio
    async def test_read_file_completeness_summary_ignores_trailing_newline(self, mock_sandbox):
        """檔尾換行不應被誤算為額外一行"""
        lines = "\n".join(f"line{i}" for i in range(1, 86)) + "\n"
        mock_sandbox.files.read_file = AsyncMock(return_value=lines)

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="chat_template.txt")

        assert result.success is True
        assert "All 85 lines | COMPLETE ===" in result.content

    @pytest.mark.asyncio
    async def test_read_file_completeness_summary_partial(self, mock_sandbox):
        """完整性摘要：部分讀取時應顯示行範圍"""
        lines = "\n".join(f"line{i}" for i in range(1, 101))
        mock_sandbox.files.read_file = AsyncMock(return_value=lines)

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="big.txt", offset=10, limit=20)

        assert result.success is True
        assert "Lines 10-29 of 100 total | PARTIAL ===" in result.content
        assert result.content.endswith("=== MORE: use offset=30 to continue ===")

    @pytest.mark.asyncio
    async def test_read_file_default_line_limit_returns_exact_continuation(self, mock_sandbox):
        lines = "\n".join(f"line{i}" for i in range(1, READ_DEFAULT_LIMIT + 2))
        mock_sandbox.files.read_file = AsyncMock(return_value=lines)

        result = await SandboxReadTool(mock_sandbox).execute(path="large.md")

        assert result.success is True
        assert (
            f"Lines 1-{READ_DEFAULT_LIMIT} of {READ_DEFAULT_LIMIT + 1} total | PARTIAL ==="
            in result.content
        )
        assert f"{READ_DEFAULT_LIMIT:6d}|line{READ_DEFAULT_LIMIT}" in result.content
        assert f"{READ_DEFAULT_LIMIT + 1:6d}|line{READ_DEFAULT_LIMIT + 1}" not in result.content
        assert result.content.endswith(
            f"=== MORE: use offset={READ_DEFAULT_LIMIT + 1} to continue ==="
        )
        assert "COMPLETE" not in result.content

    @pytest.mark.asyncio
    async def test_read_file_cjk_byte_cap_stops_before_next_complete_line(self, mock_sandbox):
        # Nine rendered CJK lines exceed 50 KiB, while eight fit. The ninth line
        # must be wholly absent rather than appearing as a head/tail fragment.
        cjk_line = "中" * 1900
        content = "\n".join(f"row-{i}:{cjk_line}" for i in range(1, 11))
        mock_sandbox.files.read_file = AsyncMock(return_value=content)

        result = await SandboxReadTool(mock_sandbox).execute(path="rules.md")

        assert result.success is True
        assert "| PARTIAL | BYTE LIMIT ===" in result.content
        assert "     8|row-8:" in result.content
        assert "     9|row-9:" not in result.content
        assert result.content.endswith("=== MORE: use offset=9 to continue ===")
        assert "chars truncated" not in result.content
        body = result.content.split("\n", 1)[1].rsplit("\n", 1)[0]
        assert len(body.encode("utf-8")) <= READ_MAX_BYTES

        continuation = await SandboxReadTool(mock_sandbox).execute(
            path="rules.md",
            offset=9,
        )
        assert continuation.success is True
        assert "     8|row-8:" not in continuation.content
        assert "     9|row-9:" in continuation.content
        assert "    10|row-10:" in continuation.content
        assert "EOF | OMITTED | STARTED AT OFFSET" in continuation.content
        assert continuation.content.endswith("=== END OF FILE ===")

    @pytest.mark.asyncio
    async def test_read_file_truncates_one_long_line_without_claiming_complete(self, mock_sandbox):
        content = "🙂" * (READ_MAX_LINE_LENGTH + 1) + "TAIL"
        mock_sandbox.files.read_file = AsyncMock(return_value=content)

        result = await SandboxReadTool(mock_sandbox).execute(path="one-line.md")

        assert result.success is True
        assert "| EOF | OMITTED | LINE LIMIT ===" in result.content
        assert f"line truncated to {READ_MAX_LINE_LENGTH} chars" in result.content
        assert "TAIL" not in result.content
        assert "\ufffd" not in result.content
        assert "COMPLETE" not in result.content
        assert result.content.endswith("=== END OF FILE ===")

    @pytest.mark.asyncio
    async def test_read_file_tail_window_reaches_eof_without_claiming_whole_file(self, mock_sandbox):
        mock_sandbox.files.read_file = AsyncMock(return_value="one\ntwo\nthree")

        result = await SandboxReadTool(mock_sandbox).execute(
            path="tail.md",
            offset=2,
            limit=2,
        )

        assert result.success is True
        assert "Lines 2-3 of 3 total | EOF | OMITTED | STARTED AT OFFSET ===" in result.content
        assert "COMPLETE" not in result.content
        assert result.content.endswith("=== END OF FILE ===")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("offset", "limit", "error"),
        [
            (0, None, "offset must be a positive integer"),
            (1.5, None, "offset must be a positive integer"),
            (None, 0, "limit must be a positive integer"),
            (None, READ_DEFAULT_LIMIT + 1, f"limit must be less than or equal to {READ_DEFAULT_LIMIT}"),
            (4, None, "offset 4 is out of range"),
        ],
    )
    async def test_read_file_rejects_invalid_windows(self, mock_sandbox, offset, limit, error):
        mock_sandbox.files.read_file = AsyncMock(return_value="one\ntwo\nthree")

        result = await SandboxReadTool(mock_sandbox).execute(
            path="small.md",
            offset=offset,
            limit=limit,
        )

        assert result.success is False
        assert error in (result.error or "")
        if offset != 4:
            mock_sandbox.commands.run.assert_not_awaited()
            mock_sandbox.files.read_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_read_file_accepts_legacy_numeric_string_window(self, mock_sandbox):
        mock_sandbox.files.read_file = AsyncMock(return_value="one\ntwo\nthree")

        result = await SandboxReadTool(mock_sandbox).execute(
            path="small.md",
            offset="2",
            limit="1",
        )

        assert result.success is True
        assert "Lines 2-2 of 3 total | PARTIAL ===" in result.content
        assert result.content.endswith("=== MORE: use offset=3 to continue ===")

    @pytest.mark.asyncio
    async def test_read_file_header_before_content(self, mock_sandbox):
        """頭部摘要必須出現在文件內容之前"""
        mock_sandbox.files.read_file = AsyncMock(return_value="[gMASK]<sop>\n<|system|>\nhello")

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="template.txt")

        assert result.success is True
        # header 應在 content 中先出現
        header_pos = result.content.index("=== FILE:")
        content_pos = result.content.index("[gMASK]")
        assert header_pos < content_pos, "Header must appear before file content"

    @pytest.mark.asyncio
    async def test_read_file_has_eof_footer(self, mock_sandbox):
        """輸出必須同時包含 HEADER 和 FOOTER（閉合邊界）"""
        mock_sandbox.files.read_file = AsyncMock(return_value="hello\nworld")

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="test.txt")

        assert result.success is True
        # HEADER: === FILE: ... ===
        assert "=== FILE: test.txt" in result.content
        assert "COMPLETE ===" in result.content
        # FOOTER: === END OF FILE ===
        assert "=== END OF FILE ===" in result.content
        # footer 在最後一行
        lines_out = result.content.strip().split("\n")
        assert lines_out[-1] == "=== END OF FILE ==="



    @pytest.mark.asyncio
    async def test_read_relative_path_with_custom_workspace(self, mock_sandbox):
        """相對路徑應解析到自定義 workspace 根目錄"""
        mock_sandbox.files.read_file = AsyncMock(return_value="ok")

        tool = SandboxReadTool(mock_sandbox, workspace_dir="/workspace/session-root")
        result = await tool.execute(path="test.txt")

        assert result.success is True
        read_path = mock_sandbox.files.read_file.call_args.args[0]
        assert read_path == "/workspace/session-root/test.txt"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, mock_sandbox):
        """讀取不存在的文件"""
        mock_sandbox.files.read_file = AsyncMock(side_effect=Exception("file not found"))
        mock_sandbox.files.read = AsyncMock(side_effect=Exception("file not found"))

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="/nope.txt")

        assert result.success is False
        # 僅應觸發 base64 主路徑命令，不再走 cat 第三層回退
        assert mock_sandbox.commands.run.await_count == 1

    @pytest.mark.asyncio
    async def test_read_file_base64_primary_path(self, mock_sandbox):
        """base64 命令主路徑：byte-exact 保真，空行不丟失"""
        mock_sandbox.files.read_file = AsyncMock(side_effect=Exception("not supported"))
        mock_sandbox.files.read = AsyncMock(side_effect=Exception("not supported"))

        execution = MagicMock(spec=[])
        execution.error = None
        execution.logs = MagicMock()
        raw = "line-1\n\nline-3\n".encode("utf-8")
        line = MagicMock()
        line.text = json.dumps(
            {
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "data": base64.b64encode(raw).decode("ascii"),
            }
        )
        execution.logs.stdout = [line]
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="/home/user/test.txt")

        assert result.success is True
        assert "line-1" in result.content
        assert "line-3" in result.content
        assert "All 3 lines | COMPLETE ===" in result.content
        # base64 保真：空行不丟失
        # line 2 should be empty string (preserved)
        assert "     2|" in result.content
        # EOF footer
        assert "=== END OF FILE ===" in result.content

    @pytest.mark.asyncio
    async def test_read_empty_file_base64_primary_path(self, mock_sandbox):
        """base64 主路徑讀取空檔案應成功，且顯示 0 行 COMPLETE"""
        mock_sandbox.files.read_file = AsyncMock(side_effect=Exception("not supported"))
        mock_sandbox.files.read = AsyncMock(side_effect=Exception("not supported"))

        execution = MagicMock(spec=[])
        execution.error = None
        execution.logs = MagicMock()
        line = MagicMock()
        line.text = json.dumps(
            {
                "size": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
                "data": "",
            }
        )
        execution.logs.stdout = [line]
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="/home/user/empty.txt")

        assert result.success is True
        assert "=== FILE: /home/user/empty.txt | All 0 lines | COMPLETE ===" in result.content
        assert "=== END OF FILE ===" in result.content

    @pytest.mark.asyncio
    async def test_read_file_rejects_truncated_base64_and_sdk_size_mismatch(self, mock_sandbox):
        full = b"abcdef"
        truncated_payload = json.dumps(
            {
                "size": len(full),
                "sha256": hashlib.sha256(full).hexdigest(),
                "data": base64.b64encode(full[:3]).decode("ascii"),
            }
        )
        mock_sandbox.commands.run = AsyncMock(
            side_effect=[
                make_fake_execution(stdout_text=truncated_payload),
                make_fake_execution(stdout_text=str(len(full))),
            ]
        )
        mock_sandbox.files.read_file = AsyncMock(return_value="abc")

        result = await SandboxReadTool(mock_sandbox).execute(path="truncated.md")

        assert result.success is False
        assert "size mismatch" in (result.error or "")
        assert "COMPLETE" not in result.content

    @pytest.mark.asyncio
    async def test_read_file_rejects_successful_stat_with_invalid_size(self, mock_sandbox):
        invalid_payload = "{truncated-json"
        mock_sandbox.commands.run = AsyncMock(
            side_effect=[
                make_fake_execution(stdout_text=invalid_payload),
                make_fake_execution(stdout_text="not-a-size"),
            ]
        )
        mock_sandbox.files.read_file = AsyncMock(return_value="possibly truncated")

        result = await SandboxReadTool(mock_sandbox).execute(path="invalid-stat.md")

        assert result.success is False
        assert "stat returned invalid size" in (result.error or "")
        assert "COMPLETE" not in result.content


class TestSandboxReadImageTool:
    """SandboxReadImageTool 測試"""

    def test_properties(self, mock_sandbox):
        tool = SandboxReadImageTool(mock_sandbox, supports_image=True, max_images=5)
        assert tool.name == "read_image_file"
        assert "paths" in tool.parameters.get("properties", {})

    def test_description_tells_model_limit_and_resolution_upfront(self, mock_sandbox):
        """主模型读图前就要知道上限和页面要渲染到多少像素，不靠撞报错学习。

        原生模式的上限按整个对话累计（历史图片每轮重发），不能写"分几次读"：
        Agent 对超限的那批整批不注入，而工具已报成功。
        """
        tool = SandboxReadImageTool(mock_sandbox, supports_image=True, max_images=5)

        assert "at most 5 images in the conversation in total" in tool.description
        assert "images read in one step" in tool.description
        assert "none of them are delivered" in tool.description
        assert "split larger sets" not in tool.description
        assert "200 DPI" in tool.description
        max_images = tool.parameters["properties"]["max_images"]
        assert max_images["default"] == 5
        assert max_images["minimum"] == 1
        assert max_images["maximum"] == 5
        assert "question" not in tool.parameters["properties"]
        assert "joint" not in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_read_image_success_returns_content_blocks(self, mock_sandbox):
        raw = b"\x89PNG\r\n\x1a\n"
        payload = json.dumps({
            "size": len(raw),
            "data": base64.b64encode(raw).decode("ascii"),
        })
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=payload))

        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=True,
            max_images=5,
        )
        result = await tool.execute(paths=["chart.png"])

        assert result.success is True
        assert result.content_blocks is not None
        assert result.content_blocks[0]["type"] == "image_url"
        assert result.content_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert result.content_blocks[0]["file"]["path"] == "chart.png"

    @pytest.mark.asyncio
    async def test_read_image_rejects_unsupported_extension(self, mock_sandbox):
        tool = SandboxReadImageTool(mock_sandbox, supports_image=True, max_images=5)
        result = await tool.execute(paths=["notes.txt"])

        assert result.success is False
        assert "Unsupported image format" in (result.error or "")

    @pytest.mark.asyncio
    async def test_read_image_rejects_too_many(self, mock_sandbox):
        tool = SandboxReadImageTool(mock_sandbox, supports_image=True, max_images=1)
        result = await tool.execute(paths=["a.png", "b.png"])

        assert result.success is False
        assert "最多读取 1 张图片" in (result.error or "")

    @pytest.mark.asyncio
    async def test_zero_max_images_is_rejected_not_treated_as_default(self, mock_sandbox):
        tool = SandboxReadImageTool(mock_sandbox, supports_image=True, max_images=5)
        result = await tool.execute(paths=["a.png"], max_images=0)

        assert result.success is False
        assert result.error == "max_images must be greater than 0"

    @pytest.mark.asyncio
    async def test_explicit_max_images_tightens_the_limit(self, mock_sandbox):
        tool = SandboxReadImageTool(mock_sandbox, supports_image=True, max_images=5)
        result = await tool.execute(paths=["a.png", "b.png"], max_images=1)

        assert result.success is False
        assert "最多读取 1 张图片，当前请求 2 张" in (result.error or "")

    @pytest.mark.asyncio
    async def test_read_image_rejects_data_url_over_single_limit(self, mock_sandbox):
        raw = b"x" * 80
        payload = json.dumps({
            "size": len(raw),
            "data": base64.b64encode(raw).decode("ascii"),
        })
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=payload))

        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=True,
            max_images=5,
            max_single_image_bytes=64,
            max_total_image_bytes=1024,
        )
        result = await tool.execute(paths=["chart.png"])

        assert result.success is False
        assert "Data URL" in (result.error or "")

    @pytest.mark.asyncio
    async def test_read_image_rejects_outside_workspace(self, mock_sandbox):
        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=True,
            max_images=5,
        )
        result = await tool.execute(paths=["../other/a.png"])

        assert result.success is False
        assert "outside workspace" in (result.error or "")


def _png_payload() -> str:
    raw = b"\x89PNG\r\n\x1a\n"
    return json.dumps({"size": len(raw), "data": base64.b64encode(raw).decode("ascii")})


def _vision_fallback(answer: AsyncMock, max_images: int = 3) -> MagicMock:
    fallback = MagicMock()
    fallback.config.display_name = "视觉模型"
    fallback.config.max_images = max_images
    fallback.answer = answer
    return fallback


class TestSandboxReadImageToolVisionFallback:
    """主模型不支持图片、配置了视觉挂件时 read_image_file 的行为。"""

    def test_fallback_mode_exposes_question_parameter(self, mock_sandbox):
        tool = SandboxReadImageTool(
            mock_sandbox,
            supports_image=False,
            vision_fallback=_vision_fallback(AsyncMock()),
        )

        assert "question" in tool.parameters["properties"]
        assert "vision assistant" in tool.description
        assert tool.parameters["properties"]["joint"]["default"] is False
        assert "`joint`" in tool.description
        assert "At most 3 images per call" in tool.description
        assert tool.parameters["properties"]["max_images"]["maximum"] == 3

    @pytest.mark.parametrize(
        ("truncation_bytes", "expected_limit"),
        [
            (42667, 8),   # 默认预算 ×1.2 ≈ 51 KB，每张 6 KB
            (10000, 2),   # 开发库文本模型的实际预算：12 KB
            (1000, 1),    # 再小也至少能读一张
        ],
    )
    def test_fallback_limit_follows_main_model_output_budget(self, mock_sandbox, truncation_bytes, expected_limit):
        """挂件允许 20 张，但逐图输出受主模型工具输出预算约束：上限按预算算出，不引导主模型一次读到被截断。"""
        tool = SandboxReadImageTool(
            mock_sandbox,
            supports_image=False,
            vision_fallback=_vision_fallback(AsyncMock(), max_images=20),
            tool_output_truncation_bytes=truncation_bytes,
        )

        assert f"At most {expected_limit} images per call" in tool.description
        assert tool.parameters["properties"]["max_images"]["default"] == expected_limit
        assert tool.parameters["properties"]["max_images"]["maximum"] == expected_limit

    @pytest.mark.asyncio
    async def test_question_is_answered_by_vision_fallback(self, mock_sandbox):
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=_png_payload()))
        answer = AsyncMock(return_value="第三季度为 42")
        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=False,
            vision_fallback=_vision_fallback(answer),
        )

        result = await tool.execute(paths=["chart.png"], question="第三季度是多少")

        assert result.success is True
        assert result.content_blocks is None
        assert result.content == (
            "[视觉助手 视觉模型 读取了 1 张图片：chart.png]\n"
            "[问题] 第三季度是多少\n"
            "[回答]\n第三季度为 42"
        )
        images, question = answer.await_args.args
        assert question == "第三季度是多少"
        assert images[0].name == "chart.png"
        assert images[0].data_url.startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_without_question_requests_full_description(self, mock_sandbox):
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=_png_payload()))
        answer = AsyncMock(return_value="一张柱状图")
        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=False,
            vision_fallback=_vision_fallback(answer),
        )

        result = await tool.execute(paths=["chart.png"])

        assert "[问题] （完整描述）" in result.content
        assert answer.await_args.args[1] is None

    @pytest.mark.asyncio
    async def test_multiple_images_are_read_one_by_one_in_path_order(self, mock_sandbox):
        """默认逐图：每张图单独带问题调用一次，结果按 paths 顺序分段，每张独享输出预算。"""
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=_png_payload()))
        texts = {"a.png": "A 页全文", "b.png": "B 页全文"}
        answer = AsyncMock(side_effect=lambda images, question: texts[images[0].name])
        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=False,
            vision_fallback=_vision_fallback(answer),
        )

        result = await tool.execute(paths=["a.png", "b.png"], question="逐字转写")

        assert result.success is True
        assert result.content == (
            "[视觉助手 视觉模型 读取了 2 张图片：a.png, b.png]\n"
            "[问题] 逐字转写\n"
            "[回答 a.png]\nA 页全文\n"
            "[回答 b.png]\nB 页全文"
        )
        assert answer.await_count == 2
        for call in answer.await_args_list:
            images, question = call.args
            assert len(images) == 1
            assert question == "逐字转写"

    @pytest.mark.asyncio
    async def test_joint_reads_all_images_in_one_call(self, mock_sandbox):
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=_png_payload()))
        answer = AsyncMock(return_value="改后多了一个弹窗")
        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=False,
            vision_fallback=_vision_fallback(answer),
        )

        result = await tool.execute(paths=["before.png", "after.png"], question="对比差异", joint=True)

        assert result.success is True
        assert result.content.endswith("[回答]\n改后多了一个弹窗")
        assert answer.await_count == 1
        assert [image.name for image in answer.await_args.args[0]] == ["before.png", "after.png"]

    @pytest.mark.asyncio
    async def test_partial_failure_keeps_successful_sections(self, mock_sandbox):
        """逐图模式一张失败：error 说明失败的图，content 保留成功部分，主模型只需重读失败的文件。"""
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=_png_payload()))

        async def answer_one(images, question):
            if images[0].name == "b.png":
                raise VisionFallbackError("视觉助手 视觉模型 识别图片 b.png 失败：TimeoutError: read timed out")
            return "A 页全文"

        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=False,
            vision_fallback=_vision_fallback(AsyncMock(side_effect=answer_one)),
        )

        result = await tool.execute(paths=["a.png", "b.png"], question="逐字转写")

        assert result.success is False
        assert result.error == "视觉助手 视觉模型 识别图片 b.png 失败：TimeoutError: read timed out"
        assert "[回答 a.png]\nA 页全文" in result.content
        assert "[回答 b.png]" not in result.content

    @pytest.mark.asyncio
    async def test_non_vision_exception_propagates_instead_of_becoming_error_text(self, mock_sandbox):
        """逐图模式只把挂件失败收进 error；代码问题原样抛出，交给 Agent 的通用工具异常路径。"""
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=_png_payload()))

        async def answer_one(images, question):
            if images[0].name == "b.png":
                raise RuntimeError("bug in response handling")
            return "A 页全文"

        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=False,
            vision_fallback=_vision_fallback(AsyncMock(side_effect=answer_one)),
        )

        with pytest.raises(RuntimeError, match="bug in response handling"):
            await tool.execute(paths=["a.png", "b.png"], question="逐字转写")

    @pytest.mark.asyncio
    async def test_all_images_failing_returns_only_errors_in_order(self, mock_sandbox):
        """逐图模式全部失败：content 为空，error 按 paths 顺序拼接每张图的失败原因。"""
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=_png_payload()))

        async def answer_one(images, question):
            raise VisionFallbackError(f"视觉助手 视觉模型 识别图片 {images[0].name} 失败：超时")

        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=False,
            vision_fallback=_vision_fallback(AsyncMock(side_effect=answer_one)),
        )

        result = await tool.execute(paths=["a.png", "b.png"], question="逐字转写")

        assert result.success is False
        assert result.content == ""
        assert result.error == (
            "视觉助手 视觉模型 识别图片 a.png 失败：超时\n"
            "视觉助手 视觉模型 识别图片 b.png 失败：超时"
        )

    @pytest.mark.asyncio
    async def test_sections_and_failures_are_named_by_requested_path(self, mock_sandbox):
        """不同目录下的同名文件按请求路径区分，主模型才知道该重读哪一个。"""
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=_png_payload()))

        async def answer_one(images, question):
            if images[0].name == "q2/page.png":
                raise VisionFallbackError(f"视觉助手 视觉模型 识别图片 {images[0].name} 失败：超时")
            return "第一季度页"

        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=False,
            vision_fallback=_vision_fallback(AsyncMock(side_effect=answer_one)),
        )

        result = await tool.execute(paths=["q1/page.png", "q2/page.png"], question="逐字转写")

        assert result.success is False
        assert "[回答 q1/page.png]\n第一季度页" in result.content
        assert result.error == "视觉助手 视觉模型 识别图片 q2/page.png 失败：超时"

    @pytest.mark.asyncio
    async def test_multiple_code_errors_are_all_logged_before_raising(self, mock_sandbox, caplog):
        """多张图各自抛出不同代码异常时只能抛一个，其余必须进日志，不能无声丢失。"""
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(stdout_text=_png_payload()))

        async def answer_one(images, question):
            if images[0].name == "a.png":
                raise RuntimeError("parse bug")
            raise KeyError("missing field")

        tool = SandboxReadImageTool(
            mock_sandbox,
            workspace_dir="/home/user/sessions/s1",
            supports_image=False,
            vision_fallback=_vision_fallback(AsyncMock(side_effect=answer_one)),
        )

        with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="parse bug"):
            await tool.execute(paths=["a.png", "b.png"], question="逐字转写")

        assert "missing field" in caplog.text

# ============== SandboxWriteTool ==============

class TestSandboxWriteTool:
    """SandboxWriteTool 測試"""

    def test_properties(self, mock_sandbox):
        tool = SandboxWriteTool(mock_sandbox)
        assert tool.name == "write_file"
        assert "path" in tool.parameters.get("properties", {})

    @pytest.mark.asyncio
    async def test_write_file_success(self, mock_sandbox):
        """成功寫入文件"""
        mock_sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text='{"exists":false}')
        )

        tool = SandboxWriteTool(mock_sandbox)
        result = await tool.execute(path="/home/user/out.txt", content="hello world")

        assert result.success is True
        assert result.content == "CREATED /home/user/out.txt"
        mock_sandbox.files.write_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_write_file_failure(self, mock_sandbox):
        """寫入失敗"""
        mock_sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text='{"exists":false}')
        )
        mock_sandbox.files.write_file = AsyncMock(side_effect=TimeoutError("lost response"))
        mock_sandbox.files.write = AsyncMock(side_effect=TimeoutError("lost response"))

        tool = SandboxWriteTool(mock_sandbox)
        result = await tool.execute(path="/out.txt", content="data")

        assert result.success is False
        assert result.outcome_uncertain is True
        assert "read_file" in result.content
        assert "/out.txt" in result.content

    @pytest.mark.asyncio
    async def test_write_file_without_write_api_is_certain_failure(self, mock_sandbox):
        """沒有可調用寫 API 時尚未派發，不應標記結果不確定。"""
        mock_sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text='{"exists":false}')
        )
        mock_sandbox.files.write_file = None
        mock_sandbox.files.write = None

        tool = SandboxWriteTool(mock_sandbox)
        result = await tool.execute(path="/out.txt", content="data")

        assert result.success is False
        assert result.outcome_uncertain is False
        assert "does not provide" in (result.error or "")
        assert "read_file" not in result.content

    @pytest.mark.asyncio
    async def test_write_file_updated(self, mock_sandbox):
        """現有內容不同時返回 UPDATED"""
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(
            stdout_text=json.dumps({
                "exists": True,
                "size": 3,
                "sha256": hashlib.sha256(b"old").hexdigest(),
            })
        ))

        tool = SandboxWriteTool(mock_sandbox)
        result = await tool.execute(path="/home/user/out.txt", content="new")

        assert result.success is True
        assert result.content == "UPDATED /home/user/out.txt"
        mock_sandbox.files.write_file.assert_awaited_once_with(
            "/home/user/out.txt",
            "new",
        )

    @pytest.mark.asyncio
    async def test_write_file_no_change_skips_sandbox_write(self, mock_sandbox):
        """內容完全相同時返回 NO CHANGE 且不重寫文件"""
        content = "already current"
        raw = content.encode("utf-8")
        mock_sandbox.commands.run = AsyncMock(return_value=make_fake_execution(
            stdout_text=json.dumps({
                "exists": True,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
        ))
        sync = AsyncMock()

        tool = SandboxWriteTool(mock_sandbox, agent_config_sync=sync)
        result = await tool.execute(path="/home/user/out.txt", content=content)

        assert result.success is True
        assert result.content == "NO CHANGE /home/user/out.txt"
        mock_sandbox.files.write_file.assert_not_awaited()
        sync.assert_awaited_once_with("/home/user/out.txt", content)

    @pytest.mark.asyncio
    async def test_write_file_inspection_failure_is_certain_and_does_not_write(self, mock_sandbox):
        """寫前無法檢查狀態時應安全失敗，不得假定文件不存在"""
        mock_sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(exit_code=1, error="probe failed")
        )
        mock_sandbox.files.read_file = AsyncMock(side_effect=PermissionError("denied"))

        tool = SandboxWriteTool(mock_sandbox)
        result = await tool.execute(path="/home/user/out.txt", content="new")

        assert result.success is False
        assert result.outcome_uncertain is False
        assert "inspect existing file" in (result.error or "")
        mock_sandbox.files.write_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_file_does_not_treat_endpoint_not_found_as_missing_file(self, mock_sandbox):
        """模糊的 SDK endpoint 錯誤不得被誤判為 CREATED。"""
        mock_sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(exit_code=1, error="probe failed")
        )
        mock_sandbox.files.read_file = AsyncMock(
            side_effect=RuntimeError("SDK endpoint not found")
        )

        tool = SandboxWriteTool(mock_sandbox)
        result = await tool.execute(path="/home/user/out.txt", content="new")

        assert result.success is False
        assert result.outcome_uncertain is False
        assert "inspect existing file" in (result.error or "")
        mock_sandbox.files.write_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_relative_path_with_custom_workspace(self, mock_sandbox):
        """write_file 相對路徑應寫到自定義 workspace 根目錄"""
        mock_sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text='{"exists":false}')
        )

        tool = SandboxWriteTool(mock_sandbox, workspace_dir="/workspace/session-root")
        result = await tool.execute(path="out.txt", content="hello")

        assert result.success is True
        written_path = mock_sandbox.files.write_file.call_args.args[0]
        assert written_path == "/workspace/session-root/out.txt"

    @pytest.mark.asyncio
    async def test_write_file_calls_agent_config_sync(self, mock_sandbox):
        """write_file 寫入成功後應把完整內容交給同步回調"""
        mock_sandbox.commands.run = AsyncMock(
            return_value=make_fake_execution(stdout_text='{"exists":false}')
        )
        sync = AsyncMock()

        tool = SandboxWriteTool(mock_sandbox, agent_config_sync=sync)
        result = await tool.execute(path="/home/user/USER.md", content="")

        assert result.success is True
        sync.assert_awaited_once_with("/home/user/USER.md", "")

    @pytest.mark.asyncio
    async def test_write_file_blocks_read_only_path(self, mock_sandbox):
        """write_file 不應修改平台模板管理的根 AGENTS.md"""
        sync = AsyncMock()
        tool = SandboxWriteTool(
            mock_sandbox,
            agent_config_sync=sync,
            read_only_paths={"/home/user/AGENTS.md"},
        )

        result = await tool.execute(path="/home/user/AGENTS.md", content="new rules")

        assert result.success is False
        mock_sandbox.files.write_file.assert_not_awaited()
        sync.assert_not_awaited()


# ============== SandboxEditTool ==============

class TestSandboxEditTool:
    """SandboxEditTool 測試"""

    def test_properties(self, mock_sandbox):
        tool = SandboxEditTool(mock_sandbox)
        assert tool.name == "edit_file"
        assert "old_str" in tool.parameters.get("properties", {})
        assert tool.parameters["properties"]["replace_all"]["default"] is False

    @pytest.mark.asyncio
    async def test_edit_success(self, mock_sandbox):
        """成功編輯"""
        mock_sandbox.files.read_file = AsyncMock(return_value="hello world")

        tool = SandboxEditTool(mock_sandbox)
        result = await tool.execute(
            path="/home/user/test.txt",
            old_str="hello",
            new_str="goodbye",
        )

        assert result.success is True
        # 應該寫回修改後的內容
        mock_sandbox.files.write_file.assert_awaited_once()
        written_content = mock_sandbox.files.write_file.call_args[0][1]
        assert "goodbye" in written_content

    @pytest.mark.asyncio
    async def test_edit_old_str_not_found(self, mock_sandbox):
        """old_str 找不到"""
        mock_sandbox.files.read_file = AsyncMock(return_value="hello world")

        tool = SandboxEditTool(mock_sandbox)
        result = await tool.execute(
            path="/test.txt",
            old_str="nonexistent",
            new_str="replacement",
        )

        assert result.success is False

    @pytest.mark.asyncio
    async def test_edit_rejects_empty_old_str_before_reading(self, mock_sandbox):
        tool = SandboxEditTool(mock_sandbox)

        result = await tool.execute(
            path="/test.txt",
            old_str="",
            new_str="replacement",
        )

        assert result.success is False
        assert "must not be empty" in (result.error or "")
        mock_sandbox.files.read_file.assert_not_awaited()
        mock_sandbox.files.write_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edit_rejects_identical_old_and_new_before_reading(self, mock_sandbox):
        tool = SandboxEditTool(mock_sandbox)

        result = await tool.execute(
            path="/test.txt",
            old_str="same",
            new_str="same",
        )

        assert result.success is False
        assert "must be different" in (result.error or "")
        mock_sandbox.files.read_file.assert_not_awaited()
        mock_sandbox.files.write_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edit_multiple_matches_requires_explicit_replace_all(self, mock_sandbox):
        mock_sandbox.files.read_file = AsyncMock(return_value="one old\ntwo old\n")

        tool = SandboxEditTool(mock_sandbox)
        result = await tool.execute(
            path="/test.txt",
            old_str="old",
            new_str="new",
        )

        assert result.success is False
        assert "Found 2 matches" in (result.error or "")
        assert "replace_all=true" in (result.error or "")
        mock_sandbox.files.write_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edit_replace_all_replaces_every_match_once(self, mock_sandbox):
        mock_sandbox.files.read_file = AsyncMock(return_value="one old\ntwo old\n")

        tool = SandboxEditTool(mock_sandbox)
        result = await tool.execute(
            path="/test.txt",
            old_str="old",
            new_str="new",
            replace_all=True,
        )

        assert result.success is True
        assert "replacements=2" in result.content
        mock_sandbox.files.write_file.assert_awaited_once_with(
            "/test.txt",
            "one new\ntwo new\n",
        )

    @pytest.mark.asyncio
    async def test_edit_multiline_lf_input_matches_and_preserves_crlf(self, mock_sandbox):
        mock_sandbox.files.read_file = AsyncMock(return_value="one\r\ntwo\r\n")

        tool = SandboxEditTool(mock_sandbox)
        result = await tool.execute(
            path="/test.txt",
            old_str="one\ntwo",
            new_str="ONE\nTWO",
        )

        assert result.success is True
        assert "replacements=1" in result.content
        mock_sandbox.files.write_file.assert_awaited_once_with(
            "/test.txt",
            "ONE\r\nTWO\r\n",
        )

    @pytest.mark.asyncio
    async def test_edit_replace_all_preserves_crlf(self, mock_sandbox):
        mock_sandbox.files.read_file = AsyncMock(
            return_value="one\r\ntwo\r\none\r\ntwo\r\n"
        )

        tool = SandboxEditTool(mock_sandbox)
        result = await tool.execute(
            path="/test.txt",
            old_str="one\ntwo",
            new_str="ONE\nTWO",
            replace_all=True,
        )

        assert result.success is True
        assert "replacements=2" in result.content
        mock_sandbox.files.write_file.assert_awaited_once_with(
            "/test.txt",
            "ONE\r\nTWO\r\nONE\r\nTWO\r\n",
        )

    @pytest.mark.asyncio
    async def test_edit_preserves_unmatched_mixed_line_endings(self, mock_sandbox):
        mock_sandbox.files.read_file = AsyncMock(
            return_value="head\r\ntarget\r\ntail\n"
        )

        tool = SandboxEditTool(mock_sandbox)
        result = await tool.execute(
            path="/test.txt",
            old_str="target",
            new_str="TARGET",
        )

        assert result.success is True
        mock_sandbox.files.write_file.assert_awaited_once_with(
            "/test.txt",
            "head\r\nTARGET\r\ntail\n",
        )

    @pytest.mark.asyncio
    async def test_edit_write_failure_requires_read_verification(self, mock_sandbox):
        mock_sandbox.files.read_file = AsyncMock(return_value="old content")
        mock_sandbox.files.write_file = AsyncMock(side_effect=TimeoutError("lost response"))

        tool = SandboxEditTool(mock_sandbox)
        result = await tool.execute(
            path="/test.txt",
            old_str="old",
            new_str="new",
        )

        assert result.success is False
        assert result.outcome_uncertain is True
        assert "read_file" in result.content
        assert "/test.txt" in result.content

    @pytest.mark.asyncio
    async def test_edit_relative_path_with_custom_workspace(self, mock_sandbox):
        """edit_file 相對路徑應使用自定義 workspace 根目錄"""
        mock_sandbox.files.read_file = AsyncMock(return_value="hello world")

        tool = SandboxEditTool(mock_sandbox, workspace_dir="/workspace/session-root")
        result = await tool.execute(
            path="edit.txt",
            old_str="hello",
            new_str="hi",
        )

        assert result.success is True
        read_path = mock_sandbox.files.read_file.call_args.args[0]
        written_path = mock_sandbox.files.write_file.call_args.args[0]
        assert read_path == "/workspace/session-root/edit.txt"
        assert written_path == "/workspace/session-root/edit.txt"

    @pytest.mark.asyncio
    async def test_edit_file_calls_agent_config_sync_with_new_content(self, mock_sandbox):
        """edit_file 寫入成功後應同步替換後的完整內容"""
        mock_sandbox.files.read_file = AsyncMock(return_value="alpha beta")
        sync = AsyncMock()

        tool = SandboxEditTool(mock_sandbox, agent_config_sync=sync)
        result = await tool.execute(
            path="/home/user/SOUL.md",
            old_str="beta",
            new_str="gamma",
        )

        assert result.success is True
        sync.assert_awaited_once_with("/home/user/SOUL.md", "alpha gamma")

    @pytest.mark.asyncio
    async def test_edit_file_blocks_read_only_path(self, mock_sandbox):
        """edit_file 不應修改平台模板管理的根 AGENTS.md"""
        sync = AsyncMock()
        tool = SandboxEditTool(
            mock_sandbox,
            agent_config_sync=sync,
            read_only_paths={"/home/user/AGENTS.md"},
        )

        result = await tool.execute(
            path="/home/user/AGENTS.md",
            old_str="old",
            new_str="new",
        )

        assert result.success is False
        mock_sandbox.files.read_file.assert_not_awaited()
        mock_sandbox.files.write_file.assert_not_awaited()
        sync.assert_not_awaited()


# ============== SandboxSessionNoteTool ==============

class TestSandboxSessionNoteTool:
    """SandboxSessionNoteTool 測試"""

    def test_properties(self, mock_sandbox):
        tool = SandboxSessionNoteTool(mock_sandbox)
        assert tool.name == "record_note"

    @pytest.mark.asyncio
    async def test_record_note(self, mock_sandbox):
        """記錄筆記"""
        mock_sandbox.files.read_file = AsyncMock(side_effect=Exception("not found"))

        tool = SandboxSessionNoteTool(mock_sandbox)
        result = await tool.execute(content="remember this", category="general")

        assert result.success is True
        mock_sandbox.files.write_file.assert_awaited_once()
        written = mock_sandbox.files.write_file.call_args[0][1]
        data = json.loads(written)
        assert len(data) == 1
        assert data[0]["content"] == "remember this"


# ============== SandboxRecallNoteTool ==============

class TestSandboxRecallNoteTool:
    """SandboxRecallNoteTool 測試"""

    def test_properties(self, mock_sandbox):
        tool = SandboxRecallNoteTool(mock_sandbox)
        assert tool.name == "recall_notes"

    @pytest.mark.asyncio
    async def test_recall_empty(self, mock_sandbox):
        """無筆記時召回"""
        mock_sandbox.files.read_file = AsyncMock(side_effect=Exception("not found"))

        tool = SandboxRecallNoteTool(mock_sandbox)
        result = await tool.execute()

        # 應該成功但提示沒有筆記
        assert result.success is True or "no" in result.content.lower() or "沒有" in result.content

    @pytest.mark.asyncio
    async def test_recall_with_notes(self, mock_sandbox):
        """有筆記時召回"""
        notes = json.dumps([
            {"content": "note1", "category": "general", "timestamp": "2025-01-01T00:00:00"},
            {"content": "note2", "category": "code", "timestamp": "2025-01-01T00:01:00"},
        ])
        mock_sandbox.files.read_file = AsyncMock(return_value=notes)

        tool = SandboxRecallNoteTool(mock_sandbox)
        result = await tool.execute()

        assert result.success is True
        assert "note1" in result.content
        assert "note2" in result.content

    @pytest.mark.asyncio
    async def test_recall_filtered_by_category(self, mock_sandbox):
        """按分類過濾召回"""
        notes = json.dumps([
            {"content": "note1", "category": "general", "timestamp": "2025-01-01T00:00:00"},
            {"content": "note2", "category": "code", "timestamp": "2025-01-01T00:01:00"},
        ])
        mock_sandbox.files.read_file = AsyncMock(return_value=notes)

        tool = SandboxRecallNoteTool(mock_sandbox)
        result = await tool.execute(category="code")

        assert result.success is True
        assert "note2" in result.content

    @pytest.mark.asyncio
    async def test_recall_fallback_without_exit_code_field(self, mock_sandbox):
        """SDK 回傳缺少 exit_code 欄位時，回退路徑仍可讀取筆記"""
        mock_sandbox.files.read_file = AsyncMock(side_effect=Exception("no file api"))
        mock_sandbox.files.read = AsyncMock(side_effect=Exception("no file api"))

        note_payload = json.dumps([
            {"content": "note-from-fallback", "category": "general", "timestamp": "2025-01-01T00:00:00"}
        ])
        execution = MagicMock(spec=[])
        execution.error = None
        execution.logs = MagicMock()
        line = MagicMock()
        line.text = note_payload
        execution.logs.stdout = [line]
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxRecallNoteTool(mock_sandbox)
        result = await tool.execute()

        assert result.success is True
        assert "note-from-fallback" in result.content
