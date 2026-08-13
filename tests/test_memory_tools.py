"""记忆工具 (memory_tools) 单元测试

覆盖：
- UpdateLongTermMemoryTool: 读/写/追加 MEMORY.md
- SearchMemoryTool: 语义/关键词检索
- ReadUserProfileTool: 只读 USER.md
- UpdateUserProfileTool: 读写 USER.md
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.helpers import make_mock_sandbox


# ── 参数化：UpdateLongTermMemoryTool 与 UpdateUserProfileTool ──


_TOOL_PARAMS = [
    pytest.param(
        "UpdateLongTermMemoryTool",
        "update_long_term_memory",
        "MEMORY.md",
        id="LongTermMemory",
    ),
    pytest.param(
        "UpdateUserProfileTool",
        "update_user",
        "USER.md",
        id="UserProfile",
    ),
]


class TestUpdateMemoryTools:
    """UpdateLongTermMemoryTool / UpdateUserProfileTool 共用测试（结构相同，仅目标文件不同）"""

    @staticmethod
    def _get_tool_cls(cls_name):
        import src.agent.tools.memory_tools as mod
        return getattr(mod, cls_name)

    @pytest.mark.parametrize("cls_name,expected_name,_target_file", _TOOL_PARAMS)
    def test_tool_metadata(self, cls_name, expected_name, _target_file):
        cls = self._get_tool_cls(cls_name)
        tool = cls(sandbox=MagicMock())
        assert tool.name == expected_name
        assert "mode" in tool.parameters["properties"]
        assert "mode" in tool.parameters["required"]
        assert "explicitly asks" in tool.description
        assert "proactively" not in tool.description.lower()

    @pytest.mark.parametrize("cls_name,_name,_target_file", _TOOL_PARAMS)
    @pytest.mark.asyncio
    async def test_read_mode(self, cls_name, _name, _target_file):
        cls = self._get_tool_cls(cls_name)
        sandbox = make_mock_sandbox(read_return=f"# Content of {_target_file}")
        tool = cls(sandbox=sandbox)

        result = await tool.execute(mode="read")
        assert result.success is True
        assert f"Content of {_target_file}" in result.content

    @pytest.mark.parametrize("cls_name,_name,target_file", _TOOL_PARAMS)
    @pytest.mark.asyncio
    async def test_read_mode_no_file(self, cls_name, _name, target_file):
        cls = self._get_tool_cls(cls_name)
        sandbox = make_mock_sandbox(read_side_effect=FileNotFoundError)
        tool = cls(sandbox=sandbox)

        result = await tool.execute(mode="read")
        assert result.success is True
        assert "does not exist" in result.content or "empty" in result.content

    @pytest.mark.parametrize("cls_name,_name,_target_file", _TOOL_PARAMS)
    @pytest.mark.asyncio
    async def test_write_mode(self, cls_name, _name, _target_file):
        cls = self._get_tool_cls(cls_name)
        sandbox = make_mock_sandbox()
        tool = cls(sandbox=sandbox)

        result = await tool.execute(mode="write", content="# New content")
        assert result.success is True
        sandbox.files.write_file.assert_called_once()

    @pytest.mark.parametrize("cls_name,_name,_target_file", _TOOL_PARAMS)
    @pytest.mark.asyncio
    async def test_write_mode_allows_empty_content(self, cls_name, _name, _target_file):
        cls = self._get_tool_cls(cls_name)
        sandbox = make_mock_sandbox()
        sync = AsyncMock()
        tool = cls(sandbox=sandbox, workspace_dir="/home/user", agent_config_sync=sync)

        result = await tool.execute(mode="write", content="")
        assert result.success is True
        sandbox.files.write_file.assert_called_once_with(f"/home/user/{_target_file}", "")
        sync.assert_awaited_once_with(f"/home/user/{_target_file}", "")

    @pytest.mark.parametrize("cls_name,_name,_target_file", _TOOL_PARAMS)
    @pytest.mark.asyncio
    async def test_write_mode_requires_explicit_content(self, cls_name, _name, _target_file):
        cls = self._get_tool_cls(cls_name)
        sandbox = make_mock_sandbox()
        sync = AsyncMock()
        tool = cls(sandbox=sandbox, workspace_dir="/home/user", agent_config_sync=sync)

        result = await tool.execute(mode="write")

        assert result.success is False
        assert result.error == "content is required for write mode"
        sandbox.files.write_file.assert_not_called()
        sync.assert_not_awaited()

    @pytest.mark.parametrize("cls_name,_name,_target_file", _TOOL_PARAMS)
    @pytest.mark.asyncio
    async def test_append_mode(self, cls_name, _name, _target_file):
        cls = self._get_tool_cls(cls_name)
        sandbox = make_mock_sandbox(read_return="# Current\n")
        tool = cls(sandbox=sandbox)

        result = await tool.execute(mode="append", content="New section")
        assert result.success is True
        sandbox.files.write_file.assert_called_once()

    @pytest.mark.parametrize("cls_name,_name,_target_file", _TOOL_PARAMS)
    @pytest.mark.asyncio
    async def test_unknown_mode(self, cls_name, _name, _target_file):
        cls = self._get_tool_cls(cls_name)
        tool = cls(sandbox=MagicMock())

        result = await tool.execute(mode="delete")
        assert result.success is False
        assert "Unknown mode" in result.error


class TestSearchMemoryTool:
    """SearchMemoryTool 测试"""

    def test_tool_metadata(self):
        from src.agent.tools.memory_tools import SearchMemoryTool

        tool = SearchMemoryTool(db_session_factory=MagicMock, user_id="u1")
        assert tool.name == "search_memory"
        assert "query" in tool.parameters["properties"]
        assert "query" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_search_no_results(self):
        from src.agent.tools.memory_tools import SearchMemoryTool
        from src.api.services.memory_service import MemoryService

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        factory = MagicMock(return_value=mock_db)

        tool = SearchMemoryTool(db_session_factory=factory, user_id="u1")

        with patch.object(MemoryService, "search_memory", new_callable=AsyncMock, return_value=[]):
            result = await tool.execute(query="test query")
        assert result.success is True
        assert "No matching" in result.content

    @pytest.mark.asyncio
    async def test_search_with_results(self):
        from src.agent.tools.memory_tools import SearchMemoryTool
        from src.api.services.memory_service import MemoryService

        mock_db = MagicMock()
        factory = MagicMock(return_value=mock_db)
        tool = SearchMemoryTool(db_session_factory=factory, user_id="u1")

        mock_results = [
            {"file_path": "memory/2026-03-30.md", "chunk_index": 0, "text": "用户偏好深色", "score": 0.92}
        ]
        with patch.object(MemoryService, "search_memory", new_callable=AsyncMock, return_value=mock_results):
            result = await tool.execute(query="用户偏好")
        assert result.success is True
        assert "用户偏好深色" in result.content
        assert "0.92" in result.content

    @pytest.mark.asyncio
    async def test_search_output_includes_timestamp(self):
        from datetime import datetime
        from src.agent.tools.memory_tools import SearchMemoryTool
        from src.api.services.memory_service import MemoryService

        mock_db = MagicMock()
        factory = MagicMock(return_value=mock_db)
        tool = SearchMemoryTool(db_session_factory=factory, user_id="u1")

        ts = datetime(2026, 4, 22, 10, 30)
        mock_results = [
            {"file_path": "conversation/sess/round1", "chunk_index": 0, "text": "聊天内容", "score": 0.85, "created_at": ts}
        ]
        with patch.object(MemoryService, "search_memory", new_callable=AsyncMock, return_value=mock_results):
            result = await tool.execute(query="test")
        assert "2026-04-22 10:30" in result.content


class TestReadUserProfileTool:
    """ReadUserProfileTool 测试"""

    def test_tool_metadata(self):
        from src.agent.tools.memory_tools import ReadUserProfileTool

        tool = ReadUserProfileTool(sandbox=MagicMock())
        assert tool.name == "read_user"
        assert tool.parameters["properties"] == {}

    @pytest.mark.asyncio
    async def test_read_existing_profile(self):
        from src.agent.tools.memory_tools import ReadUserProfileTool

        sandbox = make_mock_sandbox(read_return="# User: Alice\n偏好：深色模式")
        tool = ReadUserProfileTool(sandbox=sandbox)

        result = await tool.execute()
        assert result.success is True
        assert "Alice" in result.content

    @pytest.mark.asyncio
    async def test_read_nonexistent_profile(self):
        from src.agent.tools.memory_tools import ReadUserProfileTool

        sandbox = make_mock_sandbox(read_side_effect=FileNotFoundError)
        tool = ReadUserProfileTool(sandbox=sandbox)

        result = await tool.execute()
        assert result.success is True
        assert "does not exist" in result.content


class TestFileTypeToFilename:
    """FILE_TYPE_TO_FILENAME 映射测试"""

    def test_user_md_maps_to_user_md(self):
        """user_md 应映射到 USER.md"""
        from src.api.services.memory_service import FILE_TYPE_TO_FILENAME

        assert FILE_TYPE_TO_FILENAME["user_md"] == "USER.md"

    def test_all_file_types_present(self):
        """所有已知 file_type 都应有映射"""
        from src.api.services.memory_service import FILE_TYPE_TO_FILENAME

        expected_types = {"user_md", "memory_md", "soul_md", "agents_md"}
        assert set(FILE_TYPE_TO_FILENAME.keys()) == expected_types


class TestMemoryToolsUserPath:
    """验证记忆工具使用 USER.md"""

    @pytest.mark.asyncio
    async def test_read_user_uses_user_md(self):
        from src.agent.tools.memory_tools import ReadUserProfileTool

        sandbox = make_mock_sandbox(read_return="# Profile")
        tool = ReadUserProfileTool(sandbox=sandbox, workspace_dir="/home/user")

        await tool.execute()
        sandbox.files.read_file.assert_called_once_with("/home/user/USER.md")

    @pytest.mark.asyncio
    async def test_update_user_uses_user_md(self):
        from src.agent.tools.memory_tools import UpdateUserProfileTool

        sandbox = make_mock_sandbox()
        tool = UpdateUserProfileTool(sandbox=sandbox, workspace_dir="/home/user")

        await tool.execute(mode="write", content="# New Profile")
        sandbox.files.write_file.assert_called_once_with("/home/user/USER.md", "# New Profile")
