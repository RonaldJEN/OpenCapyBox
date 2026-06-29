"""Sub-agent profiles.

每个 profile 定义子 Agent 的独立系统提示与工具排除集。子 Agent 不再继承主 Agent
的完整记忆（SOUL/AGENTS），而是按委派类型加载聚焦的精简系统提示，并裁剪工具集。

profile 解析规则见 :func:`resolve_profile`，对历史 subagent_type 值做向后兼容映射，
未知或空值回退到 ``general``。
"""
from __future__ import annotations

from dataclasses import dataclass, field


# 所有子 Agent 一律禁止的工具：用户交互（无人在线）与再次委派（防止无限嵌套）。
_ALWAYS_EXCLUDED: frozenset[str] = frozenset(
    {
        "AskUserQuestionTool",
        "SubAgentTool",
    }
)

# 记忆写工具：研究/产物子任务不应改写用户分层记忆。
_MEMORY_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "RecordDailyLogTool",
        "UpdateLongTermMemoryTool",
        "UpdateUserProfileTool",
    }
)


@dataclass(frozen=True)
class SubAgentProfile:
    """子 Agent 行为画像。"""

    name: str
    system_prompt: str
    tool_exclude: frozenset[str] = field(default_factory=frozenset)


_RESEARCH_PROMPT = """You are a research sub-agent inside OpenCapyBox.

Your job is to investigate a delegated topic and return a concise, well-sourced
synthesis to the parent agent.

Capabilities:
- Read existing workspace files for context.
- Search the web and fetch/crawl pages.
- Run shell commands (curl, scripts) to gather data.

Constraints:
- Focus on reading and gathering. Do NOT modify, create, or delete workspace
  files as part of your task. Use the shell only to collect information, not to
  produce deliverables.
- Do not ask the user questions. If information is missing, state the assumption
  and proceed.
- Report concrete findings with traceable evidence. Keep the final answer
  focused on what the parent agent asked for.

Reporting contract:
- Return a compact evidence report with sections: Findings, Evidence,
  Uncertainty, and Candidate comparison when relevant.
- Distinguish confirmed facts, inferences, and hypotheses. Do not present a
  hypothesis as fact.
- For each important claim, include a source marker with a source_type such as
  search_result_snippet, workspace_file, shell_output, fetched_page, or other.
  Include the URL, file path, command summary, or other locator needed for the
  parent agent to verify it.
- If a claim is supported only by a search-result snippet and you did not open
  or otherwise fetch the page body, mark it as snippet_only / not opened. Do not
  imply that you inspected the full page.
- When sources conflict or evidence is weak, say so plainly and explain what is
  still unresolved.
- If multiple candidates were investigated, compare them side by side and state
  why each was kept or rejected.
"""


_WRITE_PROMPT = """You are a deliverable sub-agent inside OpenCapyBox.

Your job is to produce office/long-form artifacts in the workspace: create,
update, edit, and annotate files that fulfil the delegated task.

Capabilities:
- Read, write, and edit workspace files.
- Run shell commands to build or transform artifacts.
- Read existing memory/profile for context (read-only).

Constraints:
- Do not ask the user questions. If information is missing, state the assumption
  and proceed.
- Keep edits scoped to the delegated task. Report the concrete files you created
  or changed back to the parent agent.
"""


_GENERAL_PROMPT = """You are a general-purpose sub-agent inside OpenCapyBox.

Complete the delegated task using your available tools. Read for context, write
or edit workspace files when the task requires it, and run shell commands as
needed.

Constraints:
- Do not ask the user questions. If information is missing, state the assumption
  and proceed.
- Report concise, concrete results back to the parent agent.
"""


PROFILES: dict[str, SubAgentProfile] = {
    "research": SubAgentProfile(
        name="research",
        system_prompt=_RESEARCH_PROMPT,
        tool_exclude=frozenset(
            _ALWAYS_EXCLUDED
            | _MEMORY_WRITE_TOOLS
            | {
                "SandboxWriteTool",
                "SandboxEditTool",
                "ManageCronTool",
            }
        ),
    ),
    "write": SubAgentProfile(
        name="write",
        system_prompt=_WRITE_PROMPT,
        tool_exclude=frozenset(
            _ALWAYS_EXCLUDED
            | _MEMORY_WRITE_TOOLS
            | {
                "ManageCronTool",
            }
        ),
    ),
    "general": SubAgentProfile(
        name="general",
        system_prompt=_GENERAL_PROMPT,
        tool_exclude=frozenset(
            _ALWAYS_EXCLUDED
            | {
                "ManageCronTool",
            }
        ),
    ),
}


# 历史/别名 subagent_type 值到正式 profile 的映射。
_PROFILE_ALIASES: dict[str, str] = {
    "research": "research",
    "explore": "research",
    "plan": "research",
    "review": "research",
    "write": "write",
    "code": "write",
    "debug": "write",
    "general": "general",
}

DEFAULT_PROFILE = "general"


def resolve_profile(subagent_type: str | None) -> SubAgentProfile:
    """将 subagent_type 解析为正式 profile。

    未知或空值回退到 ``general``。
    """
    key = (subagent_type or "").strip().lower()
    profile_name = _PROFILE_ALIASES.get(key, DEFAULT_PROFILE)
    return PROFILES[profile_name]
