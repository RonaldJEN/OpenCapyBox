"""
Skill Loader - Load Claude Skills

Supports loading skills from SKILL.md files and providing them to Agent
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass
class Skill:
    """Skill data structure"""

    name: str
    description: str
    content: str
    license: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    metadata: Optional[Dict[str, str]] = None
    skill_path: Optional[Path] = None
    source: str = "official"  # "official" | "user"
    sandbox_skill_dir: Optional[str] = None  # e.g. "/home/user/skills/my-skill"

    def to_prompt(self) -> str:
        """Convert skill to prompt format"""
        return f"""
# Skill: {self.name}

{self.description}

---

{self.content}
"""


class SkillLoader:
    """Skill loader"""

    def __init__(self, skills_dir: str = "./skills"):
        """
        Initialize Skill Loader

        Args:
            skills_dir: Skills directory path
        """
        self.skills_dir = Path(skills_dir)
        self.loaded_skills: Dict[str, Skill] = {}
        self.sandbox_skills: Dict[str, Skill] = {}

    def load_skill(self, skill_path: Path) -> Optional[Skill]:
        """
        Load single skill from SKILL.md file

        Args:
            skill_path: SKILL.md file path

        Returns:
            Skill object, or None if loading fails
        """
        try:
            content = skill_path.read_text(encoding="utf-8")

            # Parse YAML frontmatter
            frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)

            if not frontmatter_match:
                print(f"⚠️  {skill_path} missing YAML frontmatter")
                return None

            frontmatter_text = frontmatter_match.group(1)
            skill_content = frontmatter_match.group(2).strip()

            # Parse YAML
            try:
                frontmatter = yaml.safe_load(frontmatter_text)
            except yaml.YAMLError as e:
                print(f"❌ Failed to parse YAML frontmatter: {e}")
                return None

            # Required fields
            if "name" not in frontmatter or "description" not in frontmatter:
                print(f"⚠️  {skill_path} missing required fields (name or description)")
                return None

            # Get skill directory (parent of SKILL.md)
            skill_dir = skill_path.parent

            # Replace relative paths in content with absolute paths
            # This ensures scripts and resources can be found from any working directory
            processed_content = self._process_skill_paths(skill_content, skill_dir)

            # Create Skill object
            skill = Skill(
                name=frontmatter["name"],
                description=frontmatter["description"],
                content=processed_content,
                license=frontmatter.get("license"),
                allowed_tools=frontmatter.get("allowed-tools"),
                metadata=frontmatter.get("metadata"),
                skill_path=skill_path,
            )

            return skill

        except Exception as e:
            print(f"❌ Failed to load skill ({skill_path}): {e}")
            return None

    def _process_skill_paths(self, content: str, skill_dir: Path) -> str:
        """
        Process skill content to replace relative paths with absolute paths.

        Supports Progressive Disclosure Level 3+: converts relative file references
        to absolute paths so Agent can easily read nested resources.

        Args:
            content: Original skill content
            skill_dir: Skill directory path

        Returns:
            Processed content with absolute paths
        """
        import re

        def skills_root_of(path: Path) -> Path | None:
            current = path
            while current.parent != current:
                if current.name == "skills":
                    return current
                current = current.parent
            return None

        def to_sandbox_skills_path(path: Path) -> str:
            root = skills_root_of(skill_dir)
            if root is None:
                return path.as_posix()
            rel = path.relative_to(root).as_posix()
            return f"/home/user/skills/{rel}"

        # Pattern 1: Directory-based paths (scripts/, examples/, templates/, reference/)
        # Also handles paths starting from skills/ directory (e.g., skills/document-skills/docx/scripts/...)
        def replace_dir_path(match):
            prefix = match.group(1)  # e.g., "python ", "node ", or "`"
            rel_path = match.group(2)  # e.g., "scripts/with_server.py" or "skills/document-skills/docx/scripts/create_docx.js"

            # For paths starting with "skills/", resolve from skills_dir parent
            if rel_path.startswith("skills/"):
                # Remove "skills/" prefix and resolve from skill_dir's grandparent (which should be skills/)
                # skill_dir is like: .../skills/document-skills/docx
                # We need to resolve: skills/document-skills/docx/scripts/...
                # So we go up to the skills directory and then resolve
                skills_root = skill_dir
                while skills_root.name != "skills" and skills_root.parent != skills_root:
                    skills_root = skills_root.parent
                if skills_root.name == "skills":
                    # Remove "skills/" prefix from rel_path
                    path_without_skills = rel_path[7:]  # len("skills/") = 7
                    abs_path = skills_root / path_without_skills
                else:
                    abs_path = skill_dir / rel_path
            else:
                abs_path = skill_dir / rel_path

            if abs_path.exists():
                return f"{prefix}{to_sandbox_skills_path(abs_path)}"
            return match.group(0)

        # Extended pattern to match:
        # - python scripts/...
        # - node scripts/...
        # - `scripts/...`
        # - python skills/document-skills/.../scripts/...
        # - node skills/document-skills/.../scripts/...
        pattern_dirs = r"(python\s+|python3\s+|node\s+|`)((?:skills/[^\s`\)]+/)?(?:scripts|examples|templates|reference)/[^\s`\)]+)"
        content = re.sub(pattern_dirs, replace_dir_path, content)

        # Pattern 2: Direct markdown/document references (forms.md, reference.md, etc.)
        # Matches phrases like "see reference.md" or "read forms.md"
        def replace_doc_path(match):
            prefix = match.group(1)  # e.g., "see ", "read "
            filename = match.group(2)  # e.g., "reference.md"
            suffix = match.group(3)  # e.g., punctuation

            abs_path = skill_dir / filename
            if abs_path.exists():
                # Add helpful instruction for Agent
                return f"{prefix}`{to_sandbox_skills_path(abs_path)}` (use read_file to access){suffix}"
            return match.group(0)

        # Match patterns like: "see reference.md" or "read forms.md"
        pattern_docs = r"(see|read|refer to|check)\s+([a-zA-Z0-9_-]+\.(?:md|txt|json|yaml))([.,;\s])"
        content = re.sub(pattern_docs, replace_doc_path, content, flags=re.IGNORECASE)

        # Pattern 3: Markdown links - supports multiple formats:
        # - [`filename.md`](filename.md) - simple filename
        # - [text](./reference/file.md) - relative path with ./
        # - [text](scripts/file.js) - directory-based path
        # Matches patterns like: "Read [`docx-js.md`](docx-js.md)" or "Load [Guide](./reference/guide.md)"
        def replace_markdown_link(match):
            prefix = match.group(1) if match.group(1) else ""  # e.g., "Read ", "Load ", or empty
            link_text = match.group(2)  # e.g., "`docx-js.md`" or "Guide"
            filepath = match.group(3)  # e.g., "docx-js.md", "./reference/file.md", "scripts/file.js"

            # Remove leading ./ if present
            clean_path = filepath[2:] if filepath.startswith("./") else filepath

            abs_path = skill_dir / clean_path
            if abs_path.exists():
                # Preserve the link text style (with or without backticks)
                return f"{prefix}[{link_text}](`{to_sandbox_skills_path(abs_path)}`) (use read_file to access)"
            return match.group(0)

        # Match markdown link patterns with optional prefix words
        # Captures: (optional prefix word) [link text] (complete file path including ./)
        pattern_markdown = (
            r"(?:(Read|See|Check|Refer to|Load|View)\s+)?\[(`?[^`\]]+`?)\]\(((?:\./)?[^)]+\.(?:md|txt|json|yaml|js|py|html))\)"
        )
        content = re.sub(pattern_markdown, replace_markdown_link, content, flags=re.IGNORECASE)

        return content

    def discover_skills(self) -> List[Skill]:
        """
        Discover and load all skills in the skills directory

        Returns:
            List of Skills
        """
        skills = []

        if not self.skills_dir.exists():
            print(f"⚠️  Skills directory does not exist: {self.skills_dir}")
            return skills

        # Recursively find all SKILL.md files
        for skill_file in self.skills_dir.rglob("SKILL.md"):
            skill = self.load_skill(skill_file)
            if skill:
                skills.append(skill)
                self.loaded_skills[skill.name] = skill

        return skills

    def register_sandbox_skill(self, skill: Skill) -> None:
        """Register a user skill discovered from sandbox.

        Official skills with the same name take priority — duplicates are
        silently ignored.
        """
        if skill.name in self.loaded_skills:
            return  # official takes priority
        self.sandbox_skills[skill.name] = skill

    def get_skill(self, name: str) -> Optional[Skill]:
        """
        Get loaded skill (official first, then sandbox user skills)

        Args:
            name: Skill name

        Returns:
            Skill object, or None if not found
        """
        return self.loaded_skills.get(name) or self.sandbox_skills.get(name)

    def list_skills(self) -> List[str]:
        """
        List all loaded skill names (official + sandbox user skills)

        Returns:
            List of skill names
        """
        return list({**self.sandbox_skills, **self.loaded_skills}.keys())

    def get_skills_metadata_prompt(self) -> str:
        """
        Generate prompt containing ONLY metadata (name + description) for all skills.
        This implements Progressive Disclosure - Level 1.

        Returns:
            Metadata-only prompt string
        """
        all_skills = {**self.sandbox_skills, **self.loaded_skills}  # official overrides
        if not all_skills:
            return ""

        prompt_parts = ["## Available Skills\n"]
        prompt_parts.append("You have access to specialized skills. Each skill provides expert guidance for specific tasks.\n")
        prompt_parts.append("Load a skill's full content using the appropriate skill tool when needed.\n")

        for skill in all_skills.values():
            tag = " [用户]" if skill.source == "user" else ""
            line = f"- `{skill.name}`{tag}: {skill.description}"
            prompt_parts.append(line)

        return "\n".join(prompt_parts)

    @staticmethod
    def process_sandbox_skill_paths(content: str, sandbox_skill_dir: str) -> str:
        """Process user skill content to resolve relative paths within the sandbox.

        Unlike ``_process_skill_paths`` (which maps *local* paths → sandbox paths),
        this works entirely within the sandbox filesystem: relative references like
        ``scripts/analyze.py`` are resolved to ``{sandbox_skill_dir}/scripts/analyze.py``.

        Args:
            content: Raw skill body (after frontmatter)
            sandbox_skill_dir: Absolute sandbox path, e.g. ``/home/user/skills/my-skill``
        """
        import re
        import posixpath

        base = sandbox_skill_dir.rstrip("/")

        def _resolve(rel: str) -> str:
            return posixpath.join(base, rel)

        # Pattern 1: python/node/backtick + directory-based paths
        def replace_dir_path(match: re.Match) -> str:
            prefix = match.group(1)
            rel_path = match.group(2)
            return f"{prefix}{_resolve(rel_path)}"

        pattern_dirs = r"(python\s+|python3\s+|node\s+|`)(?!/)(\b(?:scripts|examples|templates|reference)/[^\s`\)]+)"
        content = re.sub(pattern_dirs, replace_dir_path, content)

        # Pattern 2: "see reference.md" style
        def replace_doc_path(match: re.Match) -> str:
            prefix, filename, suffix = match.group(1), match.group(2), match.group(3)
            return f"{prefix}`{_resolve(filename)}` (use read_file to access){suffix}"

        pattern_docs = r"(see|read|refer to|check)\s+([a-zA-Z0-9_-]+\.(?:md|txt|json|yaml))([.,;\s])"
        content = re.sub(pattern_docs, replace_doc_path, content, flags=re.IGNORECASE)

        # Pattern 3: Markdown links with relative paths
        def replace_markdown_link(match: re.Match) -> str:
            prefix = match.group(1) or ""
            link_text = match.group(2)
            filepath = match.group(3)
            if filepath.startswith("/"):
                return match.group(0)
            clean = filepath[2:] if filepath.startswith("./") else filepath
            return f"{prefix}[{link_text}](`{_resolve(clean)}`) (use read_file to access)"

        pattern_markdown = (
            r"(?:(Read|See|Check|Refer to|Load|View)\s+)?\[(`?[^`\]]+`?)\]\(((?:\./)?[^)]+\.(?:md|txt|json|yaml|js|py|html))\)"
        )
        content = re.sub(pattern_markdown, replace_markdown_link, content, flags=re.IGNORECASE)

        return content
