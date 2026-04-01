"""
Skill Tool - Tool for Agent to load Skills on-demand

Implements Progressive Disclosure (Level 2): Load full skill content when needed
"""

from typing import Any, Dict, List, Optional

from .base import Tool, ToolResult
from .skill_loader import SkillLoader


class GetSkillTool(Tool):
    """Tool to get detailed information about a specific skill"""

    def __init__(self, skill_loader: SkillLoader, ensure_skill_ready=None, read_sandbox_skill=None):
        self.skill_loader = skill_loader
        self.ensure_skill_ready = ensure_skill_ready
        self.read_sandbox_skill = read_sandbox_skill

    @property
    def name(self) -> str:
        return "get_skill"

    @property
    def description(self) -> str:
        return "Get complete content and guidance for a specified skill, used for executing specific types of tasks"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to retrieve (use list_skills to view available skills)",
                }
            },
            "required": ["skill_name"],
        }

    async def execute(self, skill_name: str) -> ToolResult:
        """Get detailed information about specified skill"""
        if callable(self.ensure_skill_ready):
            is_ready = await self.ensure_skill_ready(skill_name)
            if not is_ready:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Skill '{skill_name}' is not ready in sandbox. Please retry.",
                )

        skill = self.skill_loader.get_skill(skill_name)

        if not skill:
            available = ", ".join(self.skill_loader.list_skills())
            return ToolResult(
                success=False,
                content="",
                error=f"Skill '{skill_name}' does not exist. Available skills: {available}",
            )

        # User skills: load content from sandbox on-demand
        if skill.source == "user" and not skill.content and callable(self.read_sandbox_skill):
            raw_content = await self.read_sandbox_skill(skill_name)
            if raw_content:
                skill.content = SkillLoader.process_sandbox_skill_paths(
                    raw_content, skill.sandbox_skill_dir or "",
                )
            else:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Failed to read user skill '{skill_name}' from sandbox.",
                )

        # Return complete skill content
        result = skill.to_prompt()
        return ToolResult(success=True, content=result)


def create_skill_tools(
    skills_dir: str = "./skills",
) -> tuple[List[Tool], Optional[SkillLoader]]:
    """
    Create skill tool for Progressive Disclosure

    Only provides get_skill tool - the agent uses metadata in system prompt
    to know what skills are available, then loads them on-demand.

    Args:
        skills_dir: Skills directory path

    Returns:
        Tuple of (list of tools, skill loader)
    """
    # Create skill loader
    loader = SkillLoader(skills_dir)

    # Discover and load skills
    skills = loader.discover_skills()
    print(f"✅ Discovered {len(skills)} Claude Skills")

    # Create only the get_skill tool (Progressive Disclosure Level 2)
    tools = [
        GetSkillTool(loader),
    ]

    return tools, loader
