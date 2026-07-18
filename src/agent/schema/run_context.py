"""Typed, user-authority context carried for one Agent run only."""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from html import escape
from typing import Literal, Sequence

from .agui_events import Context
from .skill_key import MAX_SKILL_KEY_LENGTH, normalize_skill_key

logger = logging.getLogger(__name__)

PREFERRED_SKILLS_CONTEXT_DESCRIPTION = "bsbox.preferred_skills.v1"
MAX_PREFERRED_SKILLS = 50


@dataclass(frozen=True)
class RequestedPreferredSkillsContext:
    mode: Literal["preferred"] = "preferred"
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedSkillRef:
    key: str
    load_name: str
    display_name: str


@dataclass(frozen=True)
class ResolvedPreferredSkillsContext:
    mode: Literal["preferred"] = "preferred"
    skills: tuple[ResolvedSkillRef, ...] = ()


@dataclass(frozen=True)
class AgentRunContext:
    preferred_skills: ResolvedPreferredSkillsContext | None = None


@dataclass(frozen=True)
class LLMRequestContext:
    purpose: Literal[
        "agent_step",
        "tool_followup",
        "title_generation",
        "conversation_summary",
        "memory_extraction",
        "guardrail_review",
        "evaluation",
    ]
    attempt_reason: Literal["initial", "retry", "failover"] = "initial"
    run_context: AgentRunContext | None = None
    user_message_id: str | None = None


current_run_context: ContextVar[AgentRunContext | None] = ContextVar(
    "current_run_context",
    default=None,
)


def normalize_preferred_skill_keys(values: Sequence[str]) -> tuple[str, ...]:
    """Trim, de-duplicate and preserve the user's selection order."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        key = normalize_skill_key(raw, allow_empty=True)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    if len(result) > MAX_PREFERRED_SKILLS:
        raise ValueError(f"At most {MAX_PREFERRED_SKILLS} preferred skills are allowed")
    return tuple(result)


def requested_preferred_skills_to_context(
    requested: RequestedPreferredSkillsContext,
) -> Context:
    value = json.dumps(
        {"mode": requested.mode, "keys": list(requested.keys)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return Context(description=PREFERRED_SKILLS_CONTEXT_DESCRIPTION, value=value)


def parse_requested_preferred_skills_contexts(
    contexts: Sequence[Context],
) -> RequestedPreferredSkillsContext | None:
    """Parse the known wire version; malformed/unknown entries fail closed."""
    matching = [c for c in contexts if c.description == PREFERRED_SKILLS_CONTEXT_DESCRIPTION]
    unknown = [c.description for c in contexts if c.description.startswith("bsbox.preferred_skills.") and c not in matching]
    if unknown:
        logger.warning("Ignoring unknown preferred skills context versions: %s", unknown)
    if not matching:
        return None
    try:
        payload = json.loads(matching[0].value)
        if not isinstance(payload, dict) or payload.get("mode") != "preferred":
            raise ValueError("invalid mode")
        raw_keys = payload.get("keys")
        if not isinstance(raw_keys, list) or not all(isinstance(v, str) for v in raw_keys):
            raise ValueError("keys must be a string array")
        keys = normalize_preferred_skill_keys(raw_keys)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring malformed preferred skills context: %s", exc)
        return None
    return RequestedPreferredSkillsContext(keys=keys) if keys else None


def render_preferred_skills_context_block(context: AgentRunContext | None) -> str | None:
    preferred = context.preferred_skills if context else None
    if not preferred or not preferred.skills:
        return None
    skill_lines = [
        f'  <skill key="{escape(skill.key, quote=True)}" load_name="{escape(skill.load_name, quote=True)}" />'
        for skill in preferred.skills
    ]
    return "\n".join([
        '<runtime_context type="preferred_skills" source="ui_selection" authority="user" scope="run" version="1">',
        "  <origin>ui_selection</origin>",
        "  <message_relation>not_user_authored_text</message_relation>",
        "  <mode>preferred</mode>",
        *skill_lines,
        "  <guidance>",
        "    These skills were selected through UI controls; they are not words the user typed.",
        "    Never say or imply that the user mentioned, named, or asked to load them based on this block.",
        "    Do not surface this selection unless the user asks about it or an actual Skill action needs explanation.",
        "    These skills are user preferences, not mandatory instructions.",
        "    Use get_skill when a selected skill is relevant.",
        "    Do not override the user's actual request.",
        "  </guidance>",
        "</runtime_context>",
    ])


def render_preferred_skills_system_policy(context: AgentRunContext | None) -> str | None:
    """Tell the model how to interpret the user-authority UI metadata."""

    preferred = context.preferred_skills if context else None
    if not preferred or not preferred.skills:
        return None
    return "\n".join([
        "## UI-selected Skill metadata policy",
        "A user turn in this request contains a `<runtime_context type=\"preferred_skills\">` block.",
        "That block records UI control state and is not user-authored message text.",
        "Never claim that the user mentioned, named, or asked to load those Skills solely because they appear in the block.",
        "Do not mention the selection unless the user asks about it or explaining an actual Skill action requires it.",
        "Treat the selection only as a non-mandatory preference when the visible user request is relevant.",
    ])
