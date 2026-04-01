"""安全性測試 — 測試沙箱工具的路徑解析安全性

沙箱遷移後，安全模型由「本地路徑穿越防護」轉為「沙箱容器隔離 + 路徑歸一化」。
本文件驗證沙箱工具的路徑處理函數不會產生非預期的路徑跳轉。
"""
import posixpath
import pytest

from src.agent.tools.sandbox_file_tools import (
    _normalize_workspace_dir,
    _resolve_workspace_path,
)


class TestNormalizeWorkspaceDir:
    """測試工作空間目錄歸一化"""

    def test_default_for_empty(self):
        """空字串回退到預設值"""
        assert _normalize_workspace_dir("") == "/home/user"

    def test_default_for_relative(self):
        """相對路徑回退到預設值"""
        assert _normalize_workspace_dir("workspace") == "/home/user"
        assert _normalize_workspace_dir("./data") == "/home/user"

    def test_absolute_path_preserved(self):
        """絕對路徑保持不變"""
        assert _normalize_workspace_dir("/home/user") == "/home/user"
        assert _normalize_workspace_dir("/workspace") == "/workspace"

    def test_trailing_slash_removed(self):
        """尾隨斜線被消除"""
        assert _normalize_workspace_dir("/home/user/") == "/home/user"

    def test_double_dots_normalized(self):
        """.. 路徑被歸一化"""
        assert _normalize_workspace_dir("/home/user/../other") == "/home/other"

    def test_double_slash_normalized(self):
        """多重斜線被歸一化"""
        assert _normalize_workspace_dir("/home//user///data") == "/home/user/data"


class TestResolveWorkspacePath:
    """測試工作空間路徑解析"""

    def test_empty_path_returns_workspace(self):
        """空路徑返回工作空間根目錄"""
        assert _resolve_workspace_path("", "/home/user") == "/home/user"

    def test_relative_path_joined(self):
        """相對路徑與工作空間拼接"""
        assert _resolve_workspace_path("file.txt", "/home/user") == "/home/user/file.txt"

    def test_nested_relative_path(self):
        """嵌套相對路徑正確拼接"""
        result = _resolve_workspace_path("src/main.py", "/home/user")
        assert result == "/home/user/src/main.py"

    def test_absolute_path_not_joined(self):
        """絕對路徑不與工作空間拼接（由沙箱隔離保證安全）"""
        result = _resolve_workspace_path("/etc/passwd", "/home/user")
        assert result == "/etc/passwd"

    def test_dot_dot_in_relative_normalized(self):
        """相對路徑中的 .. 被正確歸一化"""
        result = _resolve_workspace_path("../secret.txt", "/home/user")
        # posixpath.normpath("/home/user/../secret.txt") = "/home/secret.txt"
        assert result == "/home/secret.txt"

    def test_dot_current_dir(self):
        """當前目錄 . 被歸一化"""
        result = _resolve_workspace_path("./file.txt", "/home/user")
        assert result == "/home/user/file.txt"

    def test_complex_traversal_normalized(self):
        """複雜路徑穿越被歸一化"""
        result = _resolve_workspace_path("../../etc/passwd", "/home/user")
        assert result == "/etc/passwd"

    def test_double_slash_in_path(self):
        """多重斜線被歸一化"""
        result = _resolve_workspace_path("src//main.py", "/home/user")
        assert result == "/home/user/src/main.py"


class TestSandboxSecurityModel:
    """驗證沙箱安全模型的設計原則"""

    def test_sandbox_bash_tool_no_local_command_validation(self):
        """沙箱 BashTool 不需要本地命令安全校驗（由容器隔離保證）"""
        from src.agent.tools.sandbox_bash_tool import SandboxBashTool

        # 沙箱版不應有 _validate_command_safety 方法
        assert not hasattr(SandboxBashTool, "_validate_command_safety"), (
            "沙箱 BashTool 不應有本地命令安全校驗 — 安全由容器隔離保證"
        )

    def test_sandbox_file_tools_no_local_path_validation(self):
        """沙箱文件工具不需要本地路徑穿越校驗"""
        from src.agent.tools.sandbox_file_tools import SandboxReadTool

        # 沙箱版不應有 validate_path_safety — 沙箱內的路徑由容器隔離
        assert not hasattr(SandboxReadTool, "validate_path_safety"), (
            "沙箱 ReadTool 不應有本地路徑穿越校驗 — 安全由容器隔離保證"
        )

    def test_workspace_dir_defaults_to_home_user(self):
        """workspace_dir 預設為 /home/user（沙箱標準）"""
        assert _normalize_workspace_dir("") == "/home/user"
        assert _normalize_workspace_dir("relative") == "/home/user"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
