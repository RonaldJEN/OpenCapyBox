"""Sandbox-based shell command execution tools.

通過 OpenSandbox SDK 在遠端沙箱中執行命令，
取代原有的本地 subprocess 方式（bash_tool.py）。

提供三個工具：
- SandboxBashTool: 在沙箱中執行命令（前台/後台）
- SandboxBashOutputTool: 讀取後台命令的輸出
- SandboxBashKillTool: 終止後台命令
"""

import logging
import posixpath
import uuid
from datetime import timedelta
from typing import Any

from opensandbox import Sandbox
from opensandbox.models.execd import RunCommandOpts

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Monkey-patch: OpenSandbox SDK 的 EventNode / EventNodeError 存在多个
# Pydantic 校验问题，沙箱 execd 实际返回的 SSE 事件可能包含：
#   1. traceback: null（EventNodeError 声明 list[str]，不接受 None）
#   2. timestamp: null 或缺失（EventNode 声明 int，必填）
# 任一校验失败都会导致 SDK 的 CommandsAdapter.run() 中静默丢弃事件，
# 最终 Execution 对象无 stdout/stderr/exit_code，表现为
# "Command produced no output and no exit code"。
#
# 修复方式：创建放宽校验的替换类，替换所有使用方的模块级引用。
# ---------------------------------------------------------------------------
try:
    import opensandbox.adapters.converter.event_node as _en_module
    import opensandbox.adapters.command_adapter as _cmd_module
    from pydantic import BaseModel as _BaseModel, Field as _Field, ConfigDict as _ConfigDict

    class _PatchedEventNodeError(_BaseModel):
        """EventNodeError with traceback accepting None."""
        name: str | None = _Field(default=None, alias="ename")
        value: str | None = _Field(default=None, alias="evalue")
        traceback: list[str] | None = _Field(default=None)
        model_config = _ConfigDict(populate_by_name=True)

    class _PatchedEventNode(_BaseModel):
        """EventNode with relaxed validation for sandbox compatibility."""
        type: str
        text: str | None = None
        execution_count: int | None = _Field(default=None, alias="execution_count")
        execution_time_in_millis: int | None = _Field(default=None, alias="execution_time")
        timestamp: int | None = _Field(default=None)  # 沙箱可能发 null 或缺失
        results: _en_module.EventNodeResults | None = None
        error: _PatchedEventNodeError | None = None
        model_config = _ConfigDict(extra="ignore")  # 忽略未知字段

    # Replace at module level (for new imports)
    _en_module.EventNodeError = _PatchedEventNodeError
    _en_module.EventNode = _PatchedEventNode
    # Replace in command_adapter (already imported reference)
    _cmd_module.EventNode = _PatchedEventNode
    # Replace in execution_event_dispatcher (also caches reference at import)
    try:
        import opensandbox.adapters.converter.execution_event_dispatcher as _disp_module
        _disp_module.EventNode = _PatchedEventNode
    except Exception as _e:
        logger.debug("execution_event_dispatcher patch 跳过: %s", _e)

    # 同时 patch ExecutionEventDispatcher.dispatch:
    # 沙箱事件 timestamp 可能为 None，但下游 OutputMessage/ExecutionInit 等
    # 均要求 timestamp: int，需要在分发前提供默认值。
    # 另外 _handle_error 将 traceback=None 传给 ExecutionError 也会失败。
    try:
        import time as _time
        _orig_dispatch = _disp_module.ExecutionEventDispatcher.dispatch
        _orig_handle_error = _disp_module.ExecutionEventDispatcher._handle_error

        async def _patched_dispatch(self, event_node):
            if event_node.timestamp is None:
                event_node.timestamp = int(_time.time() * 1000)
            return await _orig_dispatch(self, event_node)

        async def _patched_handle_error(self, event_node, timestamp):
            # 确保 error.traceback 不为 None（ExecutionError 要求 list[str]）
            if event_node.error and event_node.error.traceback is None:
                event_node.error.traceback = []
            return await _orig_handle_error(self, event_node, timestamp)

        _disp_module.ExecutionEventDispatcher.dispatch = _patched_dispatch
        _disp_module.ExecutionEventDispatcher._handle_error = _patched_handle_error
    except Exception as _e:
        logger.warning("dispatch/handle_error patch 失败（bash 可能返回空结果）: %s", _e)

    logger.info("已修补 OpenSandbox EventNode: timestamp 允许 None, traceback 允许 None")
except Exception as _patch_err:
    logger.warning("OpenSandbox EventNode monkey-patch 失败: %s", _patch_err)


def _normalize_workspace_dir(workspace_dir: str) -> str:
    if not workspace_dir or not workspace_dir.startswith("/"):
        return "/home/user"
    normalized = posixpath.normpath(workspace_dir)
    return normalized if normalized.startswith("/") else "/home/user"


def _join_log_lines(lines: Any) -> str:
    if not lines:
        return ""
    if isinstance(lines, str):
        return lines
    result: list[str] = []
    for line in lines:
        text = getattr(line, "text", None)
        result.append(text if text is not None else str(line))
    return "\n".join(result)


def _extract_exit_code(execution: Any) -> int | None:
    """Extract exit code from execution result.

    Returns None when exit_code cannot be determined (execution object
    lacks both exit_code and error attributes), signalling an abnormal
    response that callers should treat as a failure.
    """
    exit_code = getattr(execution, "exit_code", None)
    if isinstance(exit_code, int):
        return exit_code
    if getattr(execution, "error", None):
        return 1
    # Cannot determine — caller should treat as suspicious
    return None


class SandboxBashOutputResult(ToolResult):
    """沙箱命令執行結果"""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    bash_id: str | None = None

    def model_post_init(self, __context: Any) -> None:
        """自動格式化 content"""
        output = ""
        if self.stdout:
            output += self.stdout
        if self.stderr:
            output += f"\n[stderr]:\n{self.stderr}"
        if self.bash_id:
            output += f"\n[bash_id]:\n{self.bash_id}"
        if self.exit_code:
            output += f"\n[exit_code]:\n{self.exit_code}"
        # Surface error info when output would otherwise be empty or only metadata
        if not self.success and self.error and not self.stdout and not self.stderr:
            output += f"\n[error]:\n{self.error}"
        if not output:
            if self.success:
                output = "(command completed with no output)"
            else:
                output = "(no output)"
        self.content = output


# 向後兼容別名：保留舊名稱，避免上層代碼改名成本
BashOutputResult = SandboxBashOutputResult


class _BackgroundCommandTracker:
    """追蹤沙箱中的後台命令（按實例隔離，避免跨用戶/跨會話泄露）

    由於 OpenSandbox 的 commands.run(background=True) 會返回 command_id，
    我們用 bash_id → (sandbox, command_id) 做映射。
    """

    def __init__(self) -> None:
        self._commands: dict[str, tuple[Sandbox, str]] = {}

    def add(self, bash_id: str, sandbox: Sandbox, command_id: str) -> None:
        self._commands[bash_id] = (sandbox, command_id)

    def get(self, bash_id: str) -> tuple[Sandbox, str] | None:
        return self._commands.get(bash_id)

    def remove(self, bash_id: str) -> tuple[Sandbox, str] | None:
        return self._commands.pop(bash_id, None)

    def get_available_ids(self) -> list[str]:
        return list(self._commands.keys())

    def cleanup_by_sandbox(self, sandbox: Sandbox) -> int:
        """清理指定 sandbox 關聯的所有後台命令追蹤"""
        to_remove = [bid for bid, (sbx, _) in self._commands.items() if sbx is sandbox]
        for bid in to_remove:
            del self._commands[bid]
        return len(to_remove)


class SandboxBashTool(Tool):
    """在遠端沙箱中執行 shell 命令

    沙箱環境為 Linux，使用 bash shell。
    所有命令都在隔離的容器中執行，天然安全。
    """

    def __init__(self, sandbox: Sandbox, workspace_dir: str = "/home/user", tracker: '_BackgroundCommandTracker | None' = None):
        """初始化

        Args:
            sandbox: 已連接的 OpenSandbox 實例
            tracker: 後台命令追蹤器（可選，用於跨工具共享同一會話的後台命令）
        """
        self._sandbox = sandbox
        self._workspace_dir = _normalize_workspace_dir(workspace_dir)
        self._tracker = tracker or _BackgroundCommandTracker()

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return """Execute bash commands in a remote sandbox (Linux container).

For terminal operations like git, python, node, pip install, etc.
DO NOT use for file read/write - use specialized file tools.

The sandbox is an isolated Linux container with Python, Node.js, and common tools pre-installed.
You can freely install packages with pip or npm.

Parameters:
  - command (required): Bash command to execute
  - timeout (optional): Timeout in seconds (default: 10, max: 600) for foreground commands
  - run_in_background (optional): Set true for long-running commands (servers, etc.)

Tips:
  - This is a Linux environment (not Windows)
    - Working directory is the configured sandbox workspace root
    - Skills are available at <workspace_root>/skills/
  - You can install any package: pip install xxx, npm install xxx
  - Chain commands with &&: cd project && python app.py
  - For background commands, monitor with bash_output and terminate with bash_kill

Examples:
  - pip install pandas matplotlib
  - python script.py
  - git clone https://github.com/user/repo.git
  - node server.js (with run_in_background=true)"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute in the sandbox.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 10, max: 600). Only for foreground commands.",
                    "default": 10,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Set to true for long-running commands. Monitor with bash_output.",
                    "default": False,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        timeout: int = 10,
        run_in_background: bool = False,
    ) -> ToolResult:
        """在沙箱中執行命令"""
        try:
            # 限制 timeout 範圍
            timeout = max(1, min(timeout, 600))

            if run_in_background:
                return await self._run_background(command)
            else:
                return await self._run_foreground(command, timeout)

        except Exception as e:
            logger.exception("沙箱命令執行異常: %s", command)
            return SandboxBashOutputResult(
                success=False,
                stdout="",
                stderr=str(e),
                exit_code=-1,
                error=f"Sandbox execution error: {type(e).__name__}: {e}",
            )

    async def _run_foreground(self, command: str, timeout: int) -> SandboxBashOutputResult:
        """前台執行命令"""
        logger.debug("沙箱前台命令: %s (timeout=%ds)", command, timeout)

        opts = RunCommandOpts(
            background=False,
            timeout=timedelta(seconds=timeout),
            working_directory=self._workspace_dir,
        )
        execution = await self._sandbox.commands.run(command, opts=opts)

        # --- Defensive checks: detect abnormal SDK responses ---
        if execution is None:
            logger.warning("沙箱命令返回 None execution: %s", command)
            return SandboxBashOutputResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=-1,
                error="Sandbox returned null execution — sandbox may not be responsive",
            )

        logs = getattr(execution, "logs", None)
        if logs is None:
            logger.warning(
                "沙箱命令返回 None logs (execution=%r): %s", execution, command
            )
            exit_code = _extract_exit_code(execution)
            return SandboxBashOutputResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=exit_code if exit_code is not None else -1,
                error=(
                    "Sandbox execution returned no logs — "
                    "sandbox may not be responsive or the command was not executed"
                ),
            )

        stdout = _join_log_lines(getattr(logs, "stdout", ""))
        stderr = _join_log_lines(getattr(logs, "stderr", ""))

        # SDK Execution 模型没有 exit_code 属性，只有 error。
        # 通过 error 存在与否 + 输出内容推断执行结果。
        exit_code = _extract_exit_code(execution)

        # Check execution.error for details the SDK may have captured
        exec_error = getattr(execution, "error", None)
        exec_error_msg = ""
        if exec_error:
            ename = getattr(exec_error, "name", None) or getattr(exec_error, "ename", None) or ""
            evalue = getattr(exec_error, "value", None) or getattr(exec_error, "evalue", None) or ""
            if ename or evalue:
                exec_error_msg = f"{ename}: {evalue}".strip(": ")

        if exit_code is None:
            if exec_error:
                exit_code = 1
            else:
                # SDK Execution 没有 exit_code 字段，命令正常执行完毕时
                # 只有 stdout/stderr/result，没有 error → 视为成功
                exit_code = 0

        is_success = exit_code == 0

        # 诊断日志：当结果为空时记录 execution 对象细节，便于排查
        if not stdout and not stderr and not is_success:
            logger.warning(
                "沙箱命令无输出且失败 (cmd=%s): execution.id=%s, "
                "execution.error=%r, execution.result=%r, logs.stdout=%r, logs.stderr=%r",
                command[:80],
                getattr(execution, "id", None),
                exec_error,
                getattr(execution, "result", None),
                getattr(logs, "stdout", None),
                getattr(logs, "stderr", None),
            )

        error_msg = None
        if not is_success:
            if exec_error_msg:
                error_msg = f"Command failed: {exec_error_msg}"
            elif not stdout and not stderr:
                error_msg = (
                    "Command produced no output and no exit code — "
                    "it may have failed to execute (e.g. shell not found in sandbox)"
                )
            else:
                error_msg = f"Command failed with exit code {exit_code}"
            if stderr:
                error_msg += f"\n{stderr.strip()}"

        return SandboxBashOutputResult(
            success=is_success,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            error=error_msg,
        )

    async def _run_background(self, command: str) -> SandboxBashOutputResult:
        """後台執行命令"""
        bash_id = str(uuid.uuid4())[:8]
        logger.debug("沙箱後台命令: %s (bash_id=%s)", command, bash_id)

        opts = RunCommandOpts(background=True, working_directory=self._workspace_dir)
        execution = await self._sandbox.commands.run(command, opts=opts)

        if execution is None:
            logger.warning("沙箱後台命令返回 None execution: %s", command)
            return SandboxBashOutputResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=-1,
                error="Sandbox returned null execution for background command",
            )

        # 追蹤後台命令
        command_id = execution.id if hasattr(execution, 'id') else bash_id
        self._tracker.add(bash_id, self._sandbox, command_id)
        return SandboxBashOutputResult(
            success=True,
            stdout=f"Background command started with ID: {bash_id}",
            stderr="",
            exit_code=0,
            bash_id=bash_id,
        )


class SandboxBashOutputTool(Tool):
    """讀取沙箱後台命令的輸出"""

    def __init__(self, tracker: '_BackgroundCommandTracker | None' = None):
        self._tracker = tracker or _BackgroundCommandTracker()

    @property
    def name(self) -> str:
        return "bash_output"

    @property
    def description(self) -> str:
        return """Retrieves output from a running or completed background bash command in the sandbox.

Parameters:
  - bash_id (required): The ID returned when starting a background command

Example: bash_output(bash_id="abc12345")"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bash_id": {
                    "type": "string",
                    "description": "The ID of the background command.",
                },
            },
            "required": ["bash_id"],
        }

    async def execute(self, bash_id: str) -> ToolResult:
        """讀取後台命令輸出"""
        try:
            tracked = self._tracker.get(bash_id)
            if not tracked:
                available = self._tracker.get_available_ids()
                return SandboxBashOutputResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=-1,
                    error=f"Background command not found: {bash_id}. Available: {available or 'none'}",
                )

            sandbox, command_id = tracked

            command_logs = await sandbox.commands.get_background_command_logs(command_id)
            command_status = await sandbox.commands.get_command_status(command_id)

            stdout = getattr(command_logs, "content", "") or ""
            stderr = ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")

            exit_code = getattr(command_status, "exit_code", 0)
            if exit_code is None:
                exit_code = 0

            return SandboxBashOutputResult(
                success=True,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                bash_id=bash_id,
            )

        except Exception as e:
            return SandboxBashOutputResult(
                success=False,
                stdout="",
                stderr=str(e),
                exit_code=-1,
                error=f"Failed to get output: {e}",
            )


class SandboxBashKillTool(Tool):
    """終止沙箱中的後台命令"""

    def __init__(self, tracker: '_BackgroundCommandTracker | None' = None):
        self._tracker = tracker or _BackgroundCommandTracker()

    @property
    def name(self) -> str:
        return "bash_kill"

    @property
    def description(self) -> str:
        return """Kills a running background bash command in the sandbox.

Parameters:
  - bash_id (required): The ID of the background command to terminate

Example: bash_kill(bash_id="abc12345")"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "bash_id": {
                    "type": "string",
                    "description": "The ID of the background command to kill.",
                },
            },
            "required": ["bash_id"],
        }

    async def execute(self, bash_id: str) -> ToolResult:
        """終止後台命令"""
        try:
            tracked = self._tracker.remove(bash_id)
            if not tracked:
                available = self._tracker.get_available_ids()
                return SandboxBashOutputResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=-1,
                    error=f"Background command not found: {bash_id}. Available: {available or 'none'}",
                )

            sandbox, command_id = tracked

            # 嘗試終止命令
            try:
                await sandbox.commands.interrupt(command_id)
            except Exception as e:
                logger.warning("interrupt 後台命令失敗 (bash_id=%s): %s", bash_id, e)
                return SandboxBashOutputResult(
                    success=False,
                    stdout="",
                    stderr=str(e),
                    exit_code=-1,
                    bash_id=bash_id,
                    error=f"Failed to interrupt background command: {e}",
                )

            return SandboxBashOutputResult(
                success=True,
                stdout=f"Background command {bash_id} terminated.",
                stderr="",
                exit_code=0,
                bash_id=bash_id,
            )

        except Exception as e:
            return SandboxBashOutputResult(
                success=False,
                stdout="",
                stderr=str(e),
                exit_code=-1,
                error=f"Failed to kill command: {e}",
            )
