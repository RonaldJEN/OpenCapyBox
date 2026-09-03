from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.tools import apply_patch_tool
from src.agent.tools.apply_patch_tool import (
    PatchError,
    SandboxApplyPatchTool,
    apply_update,
    parse_patch,
)


def test_multi_hunk_update_preserves_crlf_and_batches_one_file():
    patch = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: report.md\n"
        "@@\n"
        "-alpha\n"
        "+ALPHA\n"
        "@@\n"
        "-gamma\n"
        "+GAMMA\n"
        "*** End Patch"
    )

    assert apply_update("alpha\r\nbeta\r\ngamma\r\n", patch.actions[0]) == (
        "ALPHA\r\nbeta\r\nGAMMA\r\n"
    )


def test_parser_accepts_codex_add_move_and_delete_sequence():
    patch = parse_patch(
        "*** Begin Patch\n"
        "*** Add File: created.txt\n"
        "+created\n"
        "*** Update File: old.txt\n"
        "*** Move to: moved.txt\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** Delete File: obsolete.txt\n"
        "*** End Patch"
    )

    assert [(action.kind, action.path, action.move_path) for action in patch.actions] == [
        ("add", "created.txt", None),
        ("update", "old.txt", "moved.txt"),
        ("delete", "obsolete.txt", None),
    ]


def test_update_rejects_missing_context_before_write():
    patch = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: report.md\n"
        "@@ missing\n"
        "-old\n"
        "+new\n"
        "*** End Patch"
    )

    with pytest.raises(PatchError, match="Failed to find context"):
        apply_update("actual\n", patch.actions[0])


def test_unicode_punctuation_matching_matches_codex():
    patch = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: report.md\n"
        "@@\n"
        "-value - \"quoted\" text\n"
        "+updated\n"
        "*** End Patch"
    )

    assert apply_update(
        "value – “quoted”\u00a0text\n",
        patch.actions[0],
    ) == "updated\n"


def test_apply_patch_is_freeform_for_responses_and_json_for_legacy_clients():
    tool = SandboxApplyPatchTool(MagicMock())

    responses_schema = tool.to_responses_schema()
    function_schema = tool.to_openai_schema()["function"]

    assert responses_schema["type"] == "custom"
    assert responses_schema["format"]["syntax"] == "lark"
    assert responses_schema["description"] == (
        "The `apply_patch` tool can be used to edit files. This is a FREEFORM tool, "
        "so do not wrap the patch in JSON."
    )
    assert function_schema["parameters"]["required"] == ["patch"]
    assert "*** Begin Patch" in function_schema["description"]
    assert "unified-diff `---`/`+++`" in function_schema["description"]


@pytest.mark.asyncio
async def test_update_write_uses_digest_from_the_content_that_was_patched(monkeypatch):
    sandbox = MagicMock()
    sandbox.commands.run = AsyncMock()
    write = AsyncMock()
    monkeypatch.setattr(
        apply_patch_tool,
        "_classify_text_write",
        AsyncMock(return_value=("UPDATED", "newer-digest")),
    )
    monkeypatch.setattr(apply_patch_tool, "_sandbox_write_text", write)
    tool = SandboxApplyPatchTool(
        sandbox,
        workspace_dir="/home/user/sessions/session-1",
    )

    await tool._write(
        "/home/user/sessions/session-1/report.md",
        "updated\n",
        expected_sha256="patched-source-digest",
    )

    assert write.await_args.kwargs["expected_sha256"] == "patched-source-digest"


@pytest.mark.asyncio
async def test_full_patch_is_verified_before_the_first_write(monkeypatch):
    sandbox = MagicMock()
    sandbox.commands.run = AsyncMock()
    write = AsyncMock()
    monkeypatch.setattr(
        apply_patch_tool,
        "_sandbox_read_text",
        AsyncMock(side_effect=["old\n", "actual\n"]),
    )
    monkeypatch.setattr(apply_patch_tool, "_sandbox_write_text", write)
    tool = SandboxApplyPatchTool(
        sandbox,
        workspace_dir="/home/user/sessions/session-1",
    )

    result = await tool.execute(
        "*** Begin Patch\n"
        "*** Update File: first.txt\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** Update File: second.txt\n"
        "@@\n"
        "-missing\n"
        "+replacement\n"
        "*** End Patch"
    )

    assert result.success is False
    assert "Failed to find expected lines" in (result.error or "")
    write.assert_not_awaited()
