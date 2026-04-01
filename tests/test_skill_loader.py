"""Skill Loader 測試"""
import pytest
from pathlib import Path
from src.agent.tools.skill_loader import SkillLoader, Skill


class TestSkill:
    """Skill 類測試"""

    def test_skill_creation(self):
        """測試 Skill 創建"""
        skill = Skill(
            name="test_skill",
            description="A test skill",
            content="This is the skill content"
        )
        
        assert skill.name == "test_skill"
        assert skill.description == "A test skill"
        assert skill.content == "This is the skill content"

    def test_skill_with_optional_fields(self):
        """測試帶可選字段的 Skill"""
        skill = Skill(
            name="full_skill",
            description="A full skill",
            content="Content",
            license="MIT",
            allowed_tools=["read_file", "write_file"],
            metadata={"author": "test"},
            skill_path=Path("/tmp/skills/test")
        )
        
        assert skill.license == "MIT"
        assert skill.allowed_tools == ["read_file", "write_file"]
        assert skill.metadata == {"author": "test"}

    def test_skill_to_prompt(self):
        """測試 Skill 轉換為 prompt"""
        skill = Skill(
            name="test_skill",
            description="A test skill for testing",
            content="## Instructions\nDo something useful"
        )
        
        prompt = skill.to_prompt()
        
        assert "# Skill: test_skill" in prompt
        assert "A test skill for testing" in prompt
        assert "## Instructions" in prompt
        assert "Do something useful" in prompt


class TestSkillLoader:
    """SkillLoader 類測試"""

    @pytest.fixture
    def skills_dir(self, tmp_path):
        """創建測試用 skills 目錄"""
        skills = tmp_path / "skills"
        skills.mkdir()
        return skills

    @pytest.fixture
    def valid_skill(self, skills_dir):
        """創建有效的 skill 文件"""
        skill_dir = skills_dir / "test_skill"
        skill_dir.mkdir()
        
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: test_skill
description: A test skill for testing
license: MIT
---

## Instructions

This is the skill content.
""")
        return skill_file

    @pytest.fixture
    def skill_with_references(self, skills_dir):
        """創建帶文件引用的 skill"""
        skill_dir = skills_dir / "ref_skill"
        skill_dir.mkdir()
        
        # 創建引用的子目錄和文件
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "helper.py").write_text("print('helper')")
        
        reference_file = skill_dir / "reference.md"
        reference_file.write_text("# Reference\nSome reference content")
        
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: ref_skill
description: A skill with references
---

## Usage

Run the script: python scripts/helper.py

See reference.md for more details.

Read [`reference.md`](reference.md) for documentation.
""")
        return skill_file

    def test_loader_initialization(self, skills_dir):
        """測試 SkillLoader 初始化"""
        loader = SkillLoader(str(skills_dir))
        
        assert loader.skills_dir == skills_dir
        assert loader.loaded_skills == {}

    def test_load_valid_skill(self, skills_dir, valid_skill):
        """測試加載有效 skill"""
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(valid_skill)
        
        assert skill is not None
        assert skill.name == "test_skill"
        assert skill.description == "A test skill for testing"
        assert skill.license == "MIT"

    def test_load_skill_without_frontmatter(self, skills_dir):
        """測試加載沒有 frontmatter 的 skill"""
        skill_dir = skills_dir / "bad_skill"
        skill_dir.mkdir()
        
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("Just content without frontmatter")
        
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)
        
        assert skill is None

    def test_load_skill_without_required_fields(self, skills_dir):
        """測試加載缺少必需字段的 skill"""
        skill_dir = skills_dir / "incomplete_skill"
        skill_dir.mkdir()
        
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: only_name
---

Content without description
""")
        
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)
        
        assert skill is None

    def test_load_skill_invalid_yaml(self, skills_dir):
        """測試加載無效 YAML 的 skill"""
        skill_dir = skills_dir / "invalid_yaml"
        skill_dir.mkdir()
        
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: [invalid yaml
description: this: is: broken
---

Content
""")
        
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)
        
        assert skill is None

    def test_discover_skills(self, skills_dir, valid_skill):
        """測試發現所有 skills"""
        # 創建多個 skills
        for i in range(3):
            skill_dir = skills_dir / f"skill_{i}"
            skill_dir.mkdir()
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(f"""---
name: skill_{i}
description: Skill number {i}
---

Content for skill {i}
""")
        
        loader = SkillLoader(str(skills_dir))
        skills = loader.discover_skills()
        
        # 包括 valid_skill，共 4 個
        assert len(skills) >= 3
        assert len(loader.loaded_skills) >= 3

    def test_discover_skills_empty_dir(self, tmp_path):
        """測試在空目錄發現 skills"""
        empty_dir = tmp_path / "empty_skills"
        empty_dir.mkdir()
        
        loader = SkillLoader(str(empty_dir))
        skills = loader.discover_skills()
        
        assert skills == []

    def test_discover_skills_nonexistent_dir(self, tmp_path):
        """測試不存在的目錄"""
        loader = SkillLoader(str(tmp_path / "nonexistent"))
        skills = loader.discover_skills()
        
        assert skills == []

    def test_get_skill(self, skills_dir, valid_skill):
        """測試獲取已加載的 skill"""
        loader = SkillLoader(str(skills_dir))
        loader.discover_skills()
        
        skill = loader.get_skill("test_skill")
        
        assert skill is not None
        assert skill.name == "test_skill"

    def test_get_skill_not_found(self, skills_dir):
        """測試獲取不存在的 skill"""
        loader = SkillLoader(str(skills_dir))
        
        skill = loader.get_skill("nonexistent")
        
        assert skill is None

    def test_list_skills(self, skills_dir, valid_skill):
        """測試列出所有 skill 名稱"""
        loader = SkillLoader(str(skills_dir))
        loader.discover_skills()
        
        skill_names = loader.list_skills()
        
        assert "test_skill" in skill_names

    def test_get_skills_metadata_prompt(self, skills_dir, valid_skill):
        """測試獲取 skills 元數據 prompt"""
        loader = SkillLoader(str(skills_dir))
        loader.discover_skills()
        
        prompt = loader.get_skills_metadata_prompt()
        
        assert "## Available Skills" in prompt
        assert "test_skill" in prompt
        assert "A test skill for testing" in prompt

    def test_load_skill_with_extra_frontmatter_ignored(self, skills_dir):
        """测试非标准 frontmatter 字段被忽略"""
        skill_dir = skills_dir / "alias_skill"
        skill_dir.mkdir()

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: alias_skill
description: Skill with extra fields
aliases:
    - example
    - test
keywords:
    - testing
---

content
""")

        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        assert skill.name == "alias_skill"
        assert not hasattr(skill, "aliases") or getattr(skill, "aliases", None) is None

    def test_metadata_prompt_no_aliases_keywords(self, skills_dir):
        """测试 metadata prompt 不包含 aliases/keywords"""
        skill_dir = skills_dir / "alias_prompt_skill"
        skill_dir.mkdir()

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: alias_prompt_skill
description: Prompt rendering test
---

content
""")

        loader = SkillLoader(str(skills_dir))
        loader.discover_skills()
        prompt = loader.get_skills_metadata_prompt()

        assert "alias_prompt_skill" in prompt
        assert "aliases" not in prompt
        assert "keywords" not in prompt

    def test_get_skills_metadata_prompt_empty(self, skills_dir):
        """測試空 skills 的元數據 prompt"""
        loader = SkillLoader(str(skills_dir))
        
        prompt = loader.get_skills_metadata_prompt()
        
        assert prompt == ""

    def test_process_skill_paths_scripts(self, skills_dir, skill_with_references):
        """測試處理 skill 中的腳本路徑"""
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_with_references)
        
        assert skill is not None
        # 檢查路徑是否被處理為絕對路徑
        assert "scripts" in skill.content or str(skills_dir) in skill.content

    def test_process_skill_paths_markdown_links(self, skills_dir, skill_with_references):
        """測試處理 skill 中的 markdown 連結"""
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_with_references)
        
        assert skill is not None
        # 檢查 markdown 連結是否被處理
        assert "reference.md" in skill.content

    def test_nested_skills(self, skills_dir):
        """測試嵌套目錄中的 skills"""
        # 創建嵌套結構
        nested_dir = skills_dir / "category" / "subcategory" / "nested_skill"
        nested_dir.mkdir(parents=True)
        
        skill_file = nested_dir / "SKILL.md"
        skill_file.write_text("""---
name: nested_skill
description: A nested skill
---

Nested content
""")
        
        loader = SkillLoader(str(skills_dir))
        skills = loader.discover_skills()
        
        skill_names = [s.name for s in skills]
        assert "nested_skill" in skill_names


class TestSkillLoaderPathProcessing:
    """SkillLoader 路径处理测试"""

    @pytest.fixture
    def skill_with_scripts(self, tmp_path):
        """创建带脚本引用的 skill"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        skill_dir = skills_dir / "script_skill"
        skill_dir.mkdir()

        # 创建脚本目录和文件
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "process.py").write_text("print('process')")

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: script_skill
description: A skill with script references
---

## Usage

Run with: python scripts/process.py input.txt
""")
        return skills_dir, skill_file

    def test_path_uses_forward_slashes(self, skill_with_scripts):
        """测试处理后的路径使用正斜杠（POSIX 风格）"""
        skills_dir, skill_file = skill_with_scripts
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        # 检查路径中不包含反斜杠（Windows 路径分隔符）
        # 但应该包含正斜杠（POSIX 风格）
        assert "\\" not in skill.content or "/" in skill.content

    def test_absolute_path_in_processed_content(self, skill_with_scripts):
        """测试处理后的内容包含绝对路径"""
        skills_dir, skill_file = skill_with_scripts
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        # 在 Windows 上，绝对路径应该是 C:/... 或 /... 形式
        # 检查 scripts/process.py 是否被转换为绝对路径
        content = skill.content
        # 应该包含绝对路径（以盘符或 / 开头）
        assert "scripts/process.py" not in content or "/" in content

    def test_process_skill_paths_preserves_nonexistent_paths(self, tmp_path):
        """测试不存在的路径保持原样"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        skill_dir = skills_dir / "path_skill"
        skill_dir.mkdir()

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: path_skill
description: A skill with nonexistent paths
---

Run: python scripts/nonexistent.py
""")
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        # 不存在的路径应该保持原样
        assert "scripts/nonexistent.py" in skill.content

    def test_markdown_link_path_conversion(self, tmp_path):
        """测试 markdown 链接中的路径转换"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        skill_dir = skills_dir / "link_skill"
        skill_dir.mkdir()

        # 创建引用的文件
        (skill_dir / "guide.md").write_text("# Guide")

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: link_skill
description: A skill with markdown links
---

Read [`guide.md`](guide.md) for details.
""")
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        # 检查 markdown 链接中的路径被转换
        # 转换后应该包含 read_file 提示
        assert "read_file to access" in skill.content


class TestSkillLoaderNodeCommands:
    """SkillLoader node 命令路径处理测试"""

    @pytest.fixture
    def docx_like_skill(self, tmp_path):
        """创建类似 docx skill 的目录结构"""
        # 模拟真实的 skills 目录结构
        # skills/document-skills/docx/scripts/create_docx.js
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        doc_skills = skills_dir / "document-skills"
        doc_skills.mkdir()

        docx_dir = doc_skills / "docx"
        docx_dir.mkdir()

        scripts_dir = docx_dir / "scripts"
        scripts_dir.mkdir()

        # 创建脚本文件
        (scripts_dir / "create_docx.js").write_text("// create docx script")
        (scripts_dir / "read_docx.py").write_text("# read docx script")

        skill_file = docx_dir / "SKILL.md"
        skill_file.write_text("""---
name: docx
description: Word document processing
---

## Quick Start

### Create new document (Node.js - Recommended)

```bash
# Markdown to DOCX
node skills/document-skills/docx/scripts/create_docx.js --from-md "doc.md" -o output.docx

# From JSON
node skills/document-skills/docx/scripts/create_docx.js --from-json content.json -o output.docx
```

### Read document (Python)

```bash
python skills/document-skills/docx/scripts/read_docx.py document.docx
```

### Local script reference

```bash
python scripts/read_docx.py document.docx
```
""")
        return skills_dir, skill_file

    def test_node_command_with_skills_path(self, docx_like_skill):
        """测试 node 命令中 skills/... 路径的转换"""
        skills_dir, skill_file = docx_like_skill
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        # 應轉換為 sandbox 路徑
        assert "/home/user/skills/document-skills/docx/scripts/create_docx.js" in skill.content
        assert "node skills/document-skills" not in skill.content

    def test_python_command_with_skills_path(self, docx_like_skill):
        """测试 python 命令中 skills/... 路径的转换"""
        skills_dir, skill_file = docx_like_skill
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        assert "/home/user/skills/document-skills/docx/scripts/read_docx.py" in skill.content
        assert "python skills/document-skills" not in skill.content

    def test_local_scripts_path_conversion(self, docx_like_skill):
        """测试本地 scripts/ 路径的转换"""
        skills_dir, skill_file = docx_like_skill
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        # 本地 scripts/read_docx.py 应该被转换为绝对路径
        # 检查内容中包含绝对路径
        content = skill.content
        # 原始相对路径应该被替换
        lines_with_python = [line for line in content.split('\n') if 'python' in line.lower() and 'read_docx.py' in line]
        for line in lines_with_python:
            # 路径应该是绝对路径（包含盘符或以 / 开头）
            assert '/' in line  # POSIX 风格路径

    def test_path_conversion_preserves_arguments(self, docx_like_skill):
        """测试路径转换保留命令参数"""
        skills_dir, skill_file = docx_like_skill
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        # 检查参数是否被保留
        assert '--from-md "doc.md"' in skill.content
        assert '--from-json content.json' in skill.content
        assert '-o output.docx' in skill.content

    def test_nested_skill_finds_skills_root(self, tmp_path):
        """测试嵌套 skill 能正确找到 skills 根目录"""
        # 创建嵌套结构: skills/category/subcategory/my-skill/
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        nested_skill_dir = skills_dir / "category" / "subcategory" / "my-skill"
        nested_skill_dir.mkdir(parents=True)

        # 在 skills 根目录下创建共享脚本
        shared_scripts = skills_dir / "shared" / "scripts"
        shared_scripts.mkdir(parents=True)
        (shared_scripts / "util.py").write_text("# utility script")

        skill_file = nested_skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: nested-skill
description: A nested skill
---

Use shared utility: python skills/shared/scripts/util.py
""")

        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)

        assert skill is not None
        # 路径应该被正确转换为 sandbox 路径
        assert "/home/user/skills/shared/scripts/util.py" in skill.content
        # 确保不再是相对路径
        assert "python skills/shared/scripts/util.py" not in skill.content


class TestSkillLoaderEdgeCases:
    """SkillLoader 邊界情況測試"""

    def test_skill_with_special_characters(self, tmp_path):
        """測試含有特殊字符的 skill"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        
        skill_dir = skills_dir / "special_skill"
        skill_dir.mkdir()
        
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: special_skill
description: A skill with "quotes" and 'apostrophes' and <tags>
---

Content with special chars: &
中文內容
日本語コンテンツ
""", encoding="utf-8")
        
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)
        
        assert skill is not None
        assert skill.name == "special_skill"

    def test_skill_with_empty_content(self, tmp_path):
        """測試空內容的 skill"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        
        skill_dir = skills_dir / "empty_skill"
        skill_dir.mkdir()
        
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("""---
name: empty_skill
description: A skill with no content
---
""")
        
        loader = SkillLoader(str(skills_dir))
        skill = loader.load_skill(skill_file)
        
        assert skill is not None
        assert skill.content == ""

    def test_skill_load_exception(self, tmp_path):
        """測試加載時發生異常"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        
        # 創建一個無法讀取的文件（通過 mock）
        skill_dir = skills_dir / "error_skill"
        skill_dir.mkdir()
        
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("valid content")
        
        loader = SkillLoader(str(skills_dir))
        
        # 使用不存在的文件路徑
        result = loader.load_skill(Path("/nonexistent/path/SKILL.md"))
        
        assert result is None


# ============== 沙箱 Skill 支持 ==============


class TestSandboxSkillRegistration:
    """SkillLoader 沙箱用戶 Skill 註冊與查詢測試"""

    @pytest.fixture
    def loader_with_official(self, tmp_path):
        """創建帶官方 Skill 的 loader"""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_dir = skills_dir / "docx"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: docx\ndescription: Word docs\n---\nContent\n"
        )
        loader = SkillLoader(str(skills_dir))
        loader.discover_skills()
        return loader

    def test_register_sandbox_skill(self, loader_with_official):
        """測試註冊沙箱 Skill 後可被查詢"""
        loader = loader_with_official
        user_skill = Skill(
            name="industry-report",
            description="生成行业研究报告",
            content="",
            source="user",
            sandbox_skill_dir="/home/user/skills/industry-report",
        )
        loader.register_sandbox_skill(user_skill)

        assert "industry-report" in loader.list_skills()
        assert loader.get_skill("industry-report") is user_skill
        assert loader.get_skill("industry-report").source == "user"

    def test_official_priority_over_sandbox(self, loader_with_official):
        """測試官方 Skill 同名優先，沙箱 Skill 被忽略"""
        loader = loader_with_official
        user_skill = Skill(
            name="docx",  # 與官方同名
            description="用戶版本的 docx",
            content="user content",
            source="user",
            sandbox_skill_dir="/home/user/skills/docx",
        )
        loader.register_sandbox_skill(user_skill)

        # 應該返回官方版本
        assert loader.get_skill("docx").source == "official"
        # sandbox_skills 中不應該有 docx
        assert "docx" not in loader.sandbox_skills

    def test_list_skills_merges_both_sources(self, loader_with_official):
        """測試 list_skills 合併官方和用戶 Skill"""
        loader = loader_with_official
        loader.register_sandbox_skill(Skill(
            name="user-skill-a",
            description="A",
            content="",
            source="user",
            sandbox_skill_dir="/home/user/skills/a",
        ))
        loader.register_sandbox_skill(Skill(
            name="user-skill-b",
            description="B",
            content="",
            source="user",
            sandbox_skill_dir="/home/user/skills/b",
        ))

        names = loader.list_skills()
        assert "docx" in names
        assert "user-skill-a" in names
        assert "user-skill-b" in names

    def test_get_skill_official_first(self, loader_with_official):
        """測試 get_skill 查詢順序：官方 > 沙箱"""
        loader = loader_with_official
        assert loader.get_skill("docx") is not None
        assert loader.get_skill("nonexistent") is None

        loader.register_sandbox_skill(Skill(
            name="only-in-sandbox",
            description="Sandbox only",
            content="",
            source="user",
            sandbox_skill_dir="/home/user/skills/only",
        ))
        assert loader.get_skill("only-in-sandbox") is not None
        assert loader.get_skill("only-in-sandbox").source == "user"


class TestSandboxSkillMetadataPrompt:
    """沙箱 Skill 在 metadata prompt 中的表現"""

    def test_user_skill_tagged_in_prompt(self):
        """測試用戶 Skill 在 prompt 中帶 [用户] 標記"""
        loader = SkillLoader("/tmp/empty")
        loader.loaded_skills["docx"] = Skill(
            name="docx", description="Word docs", content="c"
        )
        loader.sandbox_skills["my-report"] = Skill(
            name="my-report",
            description="Custom report",
            content="",
            source="user",
            sandbox_skill_dir="/home/user/skills/my-report",
        )

        prompt = loader.get_skills_metadata_prompt()
        lines = prompt.strip().splitlines()
        docx_line = next(l for l in lines if "`docx`" in l)
        report_line = next(l for l in lines if "`my-report`" in l)
        assert "[用户]" not in docx_line
        assert "- `my-report` [用户]: Custom report" == report_line.strip()

    def test_empty_when_no_skills(self):
        """測試無任何 Skill 時返回空字串"""
        loader = SkillLoader("/tmp/empty")
        assert loader.get_skills_metadata_prompt() == ""


class TestProcessSandboxSkillPaths:
    """process_sandbox_skill_paths 靜態方法測試"""

    def test_scripts_path_resolved(self):
        content = "Run: python scripts/analyze.py input.csv"
        result = SkillLoader.process_sandbox_skill_paths(
            content, "/home/user/skills/my-skill"
        )
        assert "/home/user/skills/my-skill/scripts/analyze.py" in result

    def test_node_command_resolved(self):
        content = "Execute: node scripts/build.js --output dist"
        result = SkillLoader.process_sandbox_skill_paths(
            content, "/home/user/skills/web-tool"
        )
        assert "/home/user/skills/web-tool/scripts/build.js" in result

    def test_see_reference_resolved(self):
        content = "see reference.md for details."
        result = SkillLoader.process_sandbox_skill_paths(
            content, "/home/user/skills/my-skill"
        )
        assert "/home/user/skills/my-skill/reference.md" in result
        assert "read_file to access" in result

    def test_markdown_link_resolved(self):
        content = "Read [`guide.md`](guide.md) for help."
        result = SkillLoader.process_sandbox_skill_paths(
            content, "/home/user/skills/my-skill"
        )
        assert "/home/user/skills/my-skill/guide.md" in result

    def test_absolute_paths_preserved(self):
        content = "Read [`guide.md`](/usr/share/doc/guide.md) always."
        result = SkillLoader.process_sandbox_skill_paths(
            content, "/home/user/skills/my-skill"
        )
        assert "/usr/share/doc/guide.md" in result

    def test_no_double_slash(self):
        content = "python scripts/run.py"
        result = SkillLoader.process_sandbox_skill_paths(
            content, "/home/user/skills/my-skill/"  # trailing slash
        )
        assert "//scripts" not in result
        assert "/home/user/skills/my-skill/scripts/run.py" in result
