"""Sandbox 工具測試

測試 SandboxBashTool / SandboxFileTools / SandboxNoteTool
全部使用 mock Sandbox，不依賴真實遠程沙箱。
"""
import base64
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.agent.tools.sandbox_bash_tool import (
    SandboxBashTool,
    SandboxBashOutputTool,
    SandboxBashKillTool,
    SandboxBashOutputResult,
    _BackgroundCommandTracker,
)
from src.agent.tools.sandbox_file_tools import (
    SandboxReadTool,
    SandboxWriteTool,
    SandboxEditTool,
)
from src.agent.tools.sandbox_note_tool import (
    SandboxSessionNoteTool,
    SandboxRecallNoteTool,
)


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
        assert "command" in tool.parameters.get("properties", {})

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
        mock_sandbox.commands.run.assert_awaited_once()

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
        assert entry[0] is mock_sandbox
        assert entry[1] == "cmd-1"

    def test_remove(self, mock_sandbox):
        tracker = _BackgroundCommandTracker()
        tracker.add("bg-2", mock_sandbox, "cmd-2")
        tracker.remove("bg-2")
        assert tracker.get("bg-2") is None

    def test_remove_nonexistent(self):
        """移除不存在的 key 不報錯"""
        tracker = _BackgroundCommandTracker()
        tracker.remove("bg-nope")  # should not raise


# ============== SandboxReadTool ==============

class TestSandboxReadTool:
    """SandboxReadTool 測試"""

    def test_properties(self, mock_sandbox):
        tool = SandboxReadTool(mock_sandbox)
        assert tool.name == "read_file"
        assert "path" in tool.parameters.get("properties", {})

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
        # 應該包含第 5-7 行的內容
        assert "line5" in result.content or "line6" in result.content
        # 驗證摘要顯示正確的行範圍（部分讀取）
        assert "Lines 5-7 of 20 total ===" in result.content

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
        assert "Lines 10-29 of 100 total ===" in result.content

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
        line = MagicMock()
        line.text = base64.b64encode("line-1\n\nline-3\n".encode("utf-8")).decode("ascii")
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
        execution.logs.stdout = []
        execution.stdout = ""
        mock_sandbox.commands.run = AsyncMock(return_value=execution)

        tool = SandboxReadTool(mock_sandbox)
        result = await tool.execute(path="/home/user/empty.txt")

        assert result.success is True
        assert "=== FILE: /home/user/empty.txt | All 0 lines | COMPLETE ===" in result.content
        assert "=== END OF FILE ===" in result.content


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
        # mkdir -p 模擬
        mock_cmd_result = MagicMock()
        mock_cmd_result.exit_code = 0
        mock_sandbox.commands.run = AsyncMock(return_value=mock_cmd_result)

        tool = SandboxWriteTool(mock_sandbox)
        result = await tool.execute(path="/home/user/out.txt", content="hello world")

        assert result.success is True
        mock_sandbox.files.write_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_write_file_failure(self, mock_sandbox):
        """寫入失敗"""
        mock_sandbox.commands.run = AsyncMock()
        mock_sandbox.files.write_file = AsyncMock(side_effect=Exception("disk full"))
        mock_sandbox.files.write = AsyncMock(side_effect=Exception("disk full"))

        tool = SandboxWriteTool(mock_sandbox)
        result = await tool.execute(path="/out.txt", content="data")

        assert result.success is False

    @pytest.mark.asyncio
    async def test_write_relative_path_with_custom_workspace(self, mock_sandbox):
        """write_file 相對路徑應寫到自定義 workspace 根目錄"""
        mock_cmd_result = MagicMock()
        mock_cmd_result.exit_code = 0
        mock_sandbox.commands.run = AsyncMock(return_value=mock_cmd_result)

        tool = SandboxWriteTool(mock_sandbox, workspace_dir="/workspace/session-root")
        result = await tool.execute(path="out.txt", content="hello")

        assert result.success is True
        written_path = mock_sandbox.files.write_file.call_args.args[0]
        assert written_path == "/workspace/session-root/out.txt"


# ============== SandboxEditTool ==============

class TestSandboxEditTool:
    """SandboxEditTool 測試"""

    def test_properties(self, mock_sandbox):
        tool = SandboxEditTool(mock_sandbox)
        assert tool.name == "edit_file"
        assert "old_str" in tool.parameters.get("properties", {})

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
