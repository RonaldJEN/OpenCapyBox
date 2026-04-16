"""OpenSandbox 命令结果解析工具函数"""


def extract_command_stdout(result: object) -> str:
    """兼容 OpenSandbox 命令结果的两种 stdout 结构。

    优先读取 ``result.logs.stdout[*].text``（SDK 标准输出），
    回退到 ``result.stdout``（旧版 / 单字符串）。
    """
    logs = getattr(result, "logs", None)
    stdout_lines = getattr(logs, "stdout", None)
    if stdout_lines:
        return "".join(getattr(line, "text", str(line)) for line in stdout_lines)
    direct = getattr(result, "stdout", None)
    return direct if isinstance(direct, str) else ""
