"""Tests for sub-agent profile resolution and tool restrictions."""
from src.agent.subagent_profiles import (
    DEFAULT_PROFILE,
    PROFILES,
    resolve_profile,
)


def test_three_profiles_exist():
    assert set(PROFILES.keys()) == {"research", "write", "general"}


def test_default_profile_is_general():
    assert DEFAULT_PROFILE == "general"


def test_unknown_and_empty_fall_back_to_general():
    assert resolve_profile("nope").name == "general"
    assert resolve_profile("").name == "general"
    assert resolve_profile(None).name == "general"


def test_legacy_aliases_map_to_research():
    for alias in ("research", "explore", "plan", "review"):
        assert resolve_profile(alias).name == "research"


def test_legacy_aliases_map_to_write():
    for alias in ("write", "code", "debug"):
        assert resolve_profile(alias).name == "write"


def test_alias_resolution_is_case_insensitive():
    assert resolve_profile("Review").name == "research"
    assert resolve_profile("  CODE  ").name == "write"


def test_all_profiles_forbid_ask_and_sub():
    for profile in PROFILES.values():
        assert "AskUserQuestionTool" in profile.tool_exclude
        assert "SubAgentTool" in profile.tool_exclude


def test_all_profiles_forbid_cron():
    for profile in PROFILES.values():
        assert "ManageCronTool" in profile.tool_exclude


def test_research_forbids_workspace_writes_and_memory_writes():
    exclude = resolve_profile("research").tool_exclude
    assert {"SandboxWriteTool", "SandboxEditTool"}.issubset(exclude)
    assert {
        "RecordDailyLogTool",
        "UpdateLongTermMemoryTool",
        "UpdateUserProfileTool",
    }.issubset(exclude)


def test_research_allows_read_and_bash():
    exclude = resolve_profile("research").tool_exclude
    assert "SandboxReadTool" not in exclude
    assert "SandboxBashTool" not in exclude


def test_write_allows_workspace_writes_but_forbids_memory_writes():
    exclude = resolve_profile("write").tool_exclude
    assert "SandboxWriteTool" not in exclude
    assert "SandboxEditTool" not in exclude
    assert {
        "RecordDailyLogTool",
        "UpdateLongTermMemoryTool",
        "UpdateUserProfileTool",
    }.issubset(exclude)


def test_general_allows_writes_and_memory_but_forbids_cron():
    exclude = resolve_profile("general").tool_exclude
    assert "SandboxWriteTool" not in exclude
    assert "RecordDailyLogTool" not in exclude
    assert "ManageCronTool" in exclude


def test_each_profile_has_distinct_system_prompt():
    prompts = {p.name: p.system_prompt for p in PROFILES.values()}
    assert len(set(prompts.values())) == 3
    assert "research" in prompts["research"].lower()
    assert "deliverable" in prompts["write"].lower()


def test_research_prompt_requires_traceable_evidence_report():
    prompt = resolve_profile("research").system_prompt

    assert "Reporting contract" in prompt
    assert "Findings" in prompt
    assert "Evidence" in prompt
    assert "Uncertainty" in prompt
    assert "Candidate comparison" in prompt
    assert "source_type" in prompt


def test_research_prompt_marks_search_snippets_as_weak_evidence():
    prompt = resolve_profile("research").system_prompt

    assert "search_result_snippet" in prompt
    assert "snippet_only" in prompt
    assert "not opened" in prompt
    assert "imply that you inspected the full page" in prompt
