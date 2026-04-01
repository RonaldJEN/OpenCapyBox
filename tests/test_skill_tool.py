"""Skill Tool 測試"""
import pytest
from unittest.mock import MagicMock
from src.agent.tools.skill_tool import GetSkillTool, create_skill_tools
from src.agent.tools.skill_loader import SkillLoader, Skill
from src.agent.tools.base import ToolResult


class TestGetSkillTool:
    """GetSkillTool 測試"""

    @pytest.fixture
    def mock_skill_loader(self):
        """創建模擬 SkillLoader"""
        loader = MagicMock(spec=SkillLoader)
        loader.loaded_skills = {
            "test_skill": Skill(
                name="test_skill",
                description="A test skill",
                content="## Test Content\nThis is test content"
            ),
            "another_skill": Skill(
                name="another_skill",
                description="Another skill",
                content="## Another Content"
            ),
        }
        loader.get_skill = MagicMock(side_effect=lambda name: loader.loaded_skills.get(name))
        loader.list_skills = MagicMock(return_value=["test_skill", "another_skill"])
        return loader

    @pytest.fixture
    def skill_tool(self, mock_skill_loader):
        """創建 GetSkillTool 實例"""
        return GetSkillTool(mock_skill_loader)

    def test_tool_name(self, skill_tool):
        """測試工具名稱"""
        assert skill_tool.name == "get_skill"

    def test_tool_description(self, skill_tool):
        """測試工具描述"""
        assert "skill" in skill_tool.description.lower()
        assert len(skill_tool.description) > 0

    def test_tool_parameters(self, skill_tool):
        """測試工具參數定義"""
        params = skill_tool.parameters
        
        assert params["type"] == "object"
        assert "properties" in params
        assert "skill_name" in params["properties"]
        assert "required" in params
        assert "skill_name" in params["required"]

    @pytest.mark.asyncio
    async def test_execute_existing_skill(self, skill_tool):
        """測試執行獲取存在的 skill"""
        result = await skill_tool.execute(skill_name="test_skill")
        
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert "test_skill" in result.content
        assert "Test Content" in result.content

    @pytest.mark.asyncio
    async def test_execute_nonexistent_skill(self, skill_tool, mock_skill_loader):
        """測試執行獲取不存在的 skill"""
        mock_skill_loader.get_skill = MagicMock(return_value=None)
        
        result = await skill_tool.execute(skill_name="nonexistent")
        
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "nonexistent" in result.error
        assert "test_skill" in result.error or "another_skill" in result.error

    @pytest.mark.asyncio
    async def test_execute_returns_full_prompt(self, skill_tool):
        """測試執行返回完整的 prompt"""
        result = await skill_tool.execute(skill_name="test_skill")
        
        assert result.success
        # 應該包含 to_prompt() 的輸出格式
        assert "# Skill:" in result.content

    @pytest.mark.asyncio
    async def test_execute_calls_ensure_skill_ready(self, mock_skill_loader):
        """測試執行前會檢查 skill 是否已推送"""
        ensure_cb = MagicMock(return_value=True)

        async def _ensure(skill_name: str) -> bool:
            ensure_cb(skill_name)
            return True

        skill_tool = GetSkillTool(mock_skill_loader, ensure_skill_ready=_ensure)
        result = await skill_tool.execute(skill_name="test_skill")

        assert result.success is True
        ensure_cb.assert_called_once_with("test_skill")

    @pytest.mark.asyncio
    async def test_execute_when_skill_not_ready(self, mock_skill_loader):
        """測試 skill 尚未就緒時返回錯誤"""

        async def _ensure(_skill_name: str) -> bool:
            return False

        skill_tool = GetSkillTool(mock_skill_loader, ensure_skill_ready=_ensure)
        result = await skill_tool.execute(skill_name="test_skill")

        assert result.success is False
        assert "not ready in sandbox" in (result.error or "")


class TestCreateSkillTools:
    """create_skill_tools 函數測試"""

    def test_create_skill_tools_with_valid_dir(self, tmp_path):
        """測試使用有效目錄創建 skill tools"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        
        # 創建一個有效的 skill
        skill_dir = skills_dir / "test_skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: test_skill
description: A test skill
---

Content
""")
        
        tools, loader = create_skill_tools(str(skills_dir))
        
        assert len(tools) == 1
        assert isinstance(tools[0], GetSkillTool)
        assert loader is not None
        assert len(loader.loaded_skills) == 1

    def test_create_skill_tools_with_empty_dir(self, tmp_path):
        """測試使用空目錄創建 skill tools"""
        skills_dir = tmp_path / "empty_skills"
        skills_dir.mkdir()
        
        tools, loader = create_skill_tools(str(skills_dir))
        
        assert len(tools) == 1
        assert loader is not None
        assert len(loader.loaded_skills) == 0

    def test_create_skill_tools_with_nonexistent_dir(self, tmp_path):
        """測試使用不存在的目錄創建 skill tools"""
        tools, loader = create_skill_tools(str(tmp_path / "nonexistent"))
        
        assert len(tools) == 1
        assert loader is not None
        assert len(loader.loaded_skills) == 0

    def test_create_skill_tools_with_multiple_skills(self, tmp_path):
        """測試創建多個 skills"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        
        # 創建多個 skills
        for i in range(5):
            skill_dir = skills_dir / f"skill_{i}"
            skill_dir.mkdir()
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(f"""---
name: skill_{i}
description: Skill number {i}
---

Content for skill {i}
""")
        
        tools, loader = create_skill_tools(str(skills_dir))
        
        assert len(loader.loaded_skills) == 5
        assert all(f"skill_{i}" in loader.loaded_skills for i in range(5))


class TestSkillToolIntegration:
    """Skill Tool 整合測試"""

    @pytest.fixture
    def real_skill_setup(self, tmp_path):
        """設置真實的 skill 環境"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        
        # 創建一個完整的 skill
        skill_dir = skills_dir / "pdf_skill"
        skill_dir.mkdir()
        
        # 創建 scripts 目錄
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "process_pdf.py").write_text("# PDF processing script")
        
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: pdf_skill
description: Process and generate PDF documents
license: MIT
allowed-tools:
  - read_file
  - write_file
  - bash
---

## PDF Processing Skill

This skill helps you work with PDF files.

### Usage

1. Run the script: python scripts/process_pdf.py
2. Check the output

### API Reference

See the documentation for more details.
""")
        
        return skills_dir

    def test_full_workflow(self, real_skill_setup):
        """測試完整工作流程"""
        # 創建 tools
        tools, loader = create_skill_tools(str(real_skill_setup))
        
        # 驗證 loader
        assert "pdf_skill" in loader.loaded_skills
        
        # 獲取 skill
        skill = loader.get_skill("pdf_skill")
        assert skill.license == "MIT"
        assert skill.allowed_tools == ["read_file", "write_file", "bash"]
        
        # 獲取 metadata prompt
        metadata = loader.get_skills_metadata_prompt()
        assert "pdf_skill" in metadata
        assert "PDF documents" in metadata

    @pytest.mark.asyncio
    async def test_tool_execution_workflow(self, real_skill_setup):
        """測試工具執行工作流程"""
        tools, loader = create_skill_tools(str(real_skill_setup))
        skill_tool = tools[0]
        
        # 執行工具
        result = await skill_tool.execute(skill_name="pdf_skill")
        
        assert result.success
        assert "PDF Processing Skill" in result.content
        assert "scripts" in result.content or "process_pdf" in result.content


# ============== 沙箱用戶 Skill 支持 ==============


class TestGetSkillToolSandboxUser:
    """GetSkillTool 對用戶沙箱 Skill 的處理"""

    @pytest.fixture
    def loader_with_user_skill(self):
        """帶用戶 Skill 的 loader"""
        loader = SkillLoader("/tmp/empty-skills")
        loader.loaded_skills["docx"] = Skill(
            name="docx",
            description="Word docs",
            content="## Official docx content",
        )
        loader.sandbox_skills["industry-report"] = Skill(
            name="industry-report",
            description="行业研究报告生成",
            content="",  # 延遲加載
            source="user",
            sandbox_skill_dir="/home/user/skills/industry-report",
        )
        return loader

    @pytest.mark.asyncio
    async def test_user_skill_loads_from_sandbox(self, loader_with_user_skill):
        """測試用戶 Skill 從沙箱按需讀取內容"""

        async def _read_sandbox(skill_name: str) -> str | None:
            if skill_name == "industry-report":
                return "## Usage\n\npython scripts/generate.py --industry AI"
            return None

        async def _ensure(skill_name: str) -> bool:
            return True

        tool = GetSkillTool(
            loader_with_user_skill,
            ensure_skill_ready=_ensure,
            read_sandbox_skill=_read_sandbox,
        )
        result = await tool.execute(skill_name="industry-report")

        assert result.success is True
        assert "industry-report" in result.content
        # 路径应该被 process_sandbox_skill_paths 处理
        assert "/home/user/skills/industry-report/scripts/generate.py" in result.content

    @pytest.mark.asyncio
    async def test_user_skill_read_failure(self, loader_with_user_skill):
        """測試用戶 Skill 讀取失敗時返回錯誤"""

        async def _read_sandbox(skill_name: str) -> str | None:
            return None  # 讀取失敗

        async def _ensure(skill_name: str) -> bool:
            return True

        tool = GetSkillTool(
            loader_with_user_skill,
            ensure_skill_ready=_ensure,
            read_sandbox_skill=_read_sandbox,
        )
        result = await tool.execute(skill_name="industry-report")

        assert result.success is False
        assert "Failed to read" in (result.error or "")

    @pytest.mark.asyncio
    async def test_official_skill_ignores_sandbox_callback(self, loader_with_user_skill):
        """測試官方 Skill 不走沙箱讀取路徑"""
        sandbox_called = False

        async def _read_sandbox(skill_name: str) -> str | None:
            nonlocal sandbox_called
            sandbox_called = True
            return "should not be used"

        async def _ensure(skill_name: str) -> bool:
            return True

        tool = GetSkillTool(
            loader_with_user_skill,
            ensure_skill_ready=_ensure,
            read_sandbox_skill=_read_sandbox,
        )
        result = await tool.execute(skill_name="docx")

        assert result.success is True
        assert "Official docx content" in result.content
        assert not sandbox_called

    @pytest.mark.asyncio
    async def test_user_skill_ensure_ready_returns_true(self, loader_with_user_skill):
        """測試用戶 Skill 的 ensure_ready 不需要推送"""
        ensure_called_with = []

        async def _ensure(skill_name: str) -> bool:
            ensure_called_with.append(skill_name)
            return True  # 用戶 Skill 直接返回 True

        async def _read_sandbox(skill_name: str) -> str | None:
            return "## Content"

        tool = GetSkillTool(
            loader_with_user_skill,
            ensure_skill_ready=_ensure,
            read_sandbox_skill=_read_sandbox,
        )
        result = await tool.execute(skill_name="industry-report")

        assert result.success is True
        assert "industry-report" in ensure_called_with
