"""Agent run logger"""

import json
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import Message, ToolCall


class AgentLogger:
    """Agent run logger

    Responsible for recording the complete interaction process of each agent run, including:
    - LLM requests and responses
    - Tool calls and results
    """

    def __init__(self):
        """Initialize logger

        Logs are stored in ~/.OpenCapyBox/log/ directory
        """
        # Use ~/.OpenCapyBox/log/ directory for logs
        self.log_dir = Path.home() / ".OpenCapyBox" / "log"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = None
        self.log_index = 0

    def start_new_run(self):
        """Start new run, create new log file"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"agent_run_{timestamp}.log"
        self.log_file = self.log_dir / log_filename
        self.log_index = 0

        # Write log header
        with open(self.log_file, "w", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(f"Agent Run Log - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")

    def log_request(self, messages: list[Message], tools: list[Any] | None = None):
        """Log LLM request

        Args:
            messages: Message list
            tools: Tool list (optional)
        """
        self.log_index += 1

        # Build complete request data structure
        request_data = {
            "messages": [],
            "tools": [],
        }

        # Convert messages to JSON serializable format
        for msg in messages:
            msg_dict = {
                "role": msg.role,
                "content": msg.content,
            }
            if msg.thinking:
                msg_dict["thinking"] = msg.thinking
            if msg.tool_calls:
                msg_dict["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id
            if msg.name:
                msg_dict["name"] = msg.name

            request_data["messages"].append(msg_dict)

        # Only record tool names
        if tools:
            request_data["tools"] = [tool.name for tool in tools]

        # Format as JSON
        content = "LLM Request:\n\n"
        content += json.dumps(request_data, indent=2, ensure_ascii=False)

        self._write_log("REQUEST", content)

    def log_response(
        self,
        content: str,
        thinking: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        finish_reason: str | None = None,
    ):
        """Log LLM response

        Args:
            content: Response content
            thinking: Thinking content (optional)
            tool_calls: Tool call list (optional)
            finish_reason: Finish reason (optional)
        """
        self.log_index += 1

        # Build complete response data structure
        response_data = {
            "content": content,
        }

        if thinking:
            response_data["thinking"] = thinking

        if tool_calls:
            response_data["tool_calls"] = [tc.model_dump() for tc in tool_calls]

        if finish_reason:
            response_data["finish_reason"] = finish_reason

        # Format as JSON
        log_content = "LLM Response:\n\n"
        log_content += json.dumps(response_data, indent=2, ensure_ascii=False)

        self._write_log("RESPONSE", log_content)

    def log_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result_success: bool,
        result_content: str | None = None,
        result_error: str | None = None,
    ):
        """Log tool execution result

        Args:
            tool_name: Tool name
            arguments: Tool arguments
            result_success: Whether successful
            result_content: Result content (on success)
            result_error: Error message (on failure)
        """
        self.log_index += 1

        # Build complete tool execution result data structure
        tool_result_data = {
            "tool_name": tool_name,
            "arguments": arguments,
            "success": result_success,
        }

        if result_success:
            tool_result_data["result"] = result_content
        else:
            tool_result_data["error"] = result_error

        # Format as JSON
        content = "Tool Execution:\n\n"
        content += json.dumps(tool_result_data, indent=2, ensure_ascii=False)

        self._write_log("TOOL_RESULT", content)

    async def _write_log_async(self, log_type: str, content: str):
        """异步写入日志条目（不阻塞主流程）

        Args:
            log_type: Log type (REQUEST, RESPONSE, TOOL_RESULT)
            content: Log content
        """
        if self.log_file is None:
            return

        # 🔥 使用 asyncio 在后台线程中执行文件I/O
        def _sync_write():
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write("\n" + "-" * 80 + "\n")
                    f.write(f"[{self.log_index}] {log_type}\n")
                    f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}\n")
                    f.write("-" * 80 + "\n")
                    f.write(content + "\n")
            except Exception:
                # 忽略日志写入错误，不影响主流程
                pass

        # 在默认的 executor (线程池) 中执行同步I/O
        await asyncio.get_event_loop().run_in_executor(None, _sync_write)

    def _write_log(self, log_type: str, content: str):
        """同步写入日志条目（向后兼容）

        Args:
            log_type: Log type (REQUEST, RESPONSE, TOOL_RESULT)
            content: Log content
        """
        if self.log_file is None:
            return

        # 🔥 优化：尝试异步写入，如果失败则同步写入
        try:
            # 尝试获取当前事件循环
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果事件循环正在运行，创建后台任务异步写入（不等待）
                asyncio.create_task(self._write_log_async(log_type, content))
                return
        except RuntimeError:
            # 没有事件循环，使用同步写入
            pass

        # 同步写入（向后兼容）
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write("\n" + "-" * 80 + "\n")
                f.write(f"[{self.log_index}] {log_type}\n")
                f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}\n")
                f.write("-" * 80 + "\n")
                f.write(content + "\n")
        except Exception:
            # 忽略日志写入错误
            pass

    def get_log_file_path(self) -> Path:
        """Get current log file path"""
        return self.log_file
