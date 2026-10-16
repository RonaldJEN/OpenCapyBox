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
    assert responses_schema["description"].startswith(
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
@pytest.mark.parametrize("second_chunk", [
    "@@\n-missing\n+replacement\n",
    "@@\n missing\n@@\n-actual\n+replacement\n",
])
async def test_full_patch_is_verified_before_the_first_write(monkeypatch, second_chunk):
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
        + second_chunk + "*** End Patch"
    )

    assert result.success is False
    assert "Failed to find expected lines" in (result.error or "")
    assert result.error_code is None
    write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_chunk", ["", "@@\n-old\n+new\n"])
@pytest.mark.parametrize("tail", ["", "@@\n+    normalize_dates()\n     return rows\n"])
async def test_empty_chunk_reports_location_before_any_file_access(prior_chunk, tail):
    tool = SandboxApplyPatchTool(MagicMock())
    tool._prepare_patch = AsyncMock()
    tool._apply_action = AsyncMock()
    patch = (
        "*** Begin Patch\n*** Add File: first.txt\n+created\n"
        "*** Update File: demo.py\n" + prior_chunk
        + "@@\n" + tail + "*** End Patch"
    )

    result = await tool.execute(patch)

    assert not result.success
    assert result.error_code == "PATCH_EMPTY_CHUNK"
    assert "demo.py" in result.error
    assert f"第{2 if prior_chunk else 1}个修改块" in result.error
    assert f"第{8 if prior_chunk else 5}行" in result.error
    assert "没有正文" in result.error
    assert "文件尚未修改" in result.content
    tool._prepare_patch.assert_not_awaited()
    tool._apply_action.assert_not_awaited()


@pytest.mark.parametrize("chunk", ["@@\n", "@@ anchor\n"])
def test_empty_chunks_cannot_be_silently_dropped(chunk):
    with pytest.raises(apply_patch_tool.PatchSyntaxError) as error:
        parse_patch(
            "*** Begin Patch\n*** Update File: demo.py\n"
            + chunk + "@@\n-old\n+new\n*** End Patch"
        )
    assert error.value.code == "PATCH_EMPTY_CHUNK"


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["", "@@\n"])
async def test_context_updates_only_the_intended_repeated_text(monkeypatch, tmp_path, separator):
    source = tmp_path / "demo.py"
    original = "def other():\n    return rows\ndef build_report():\n    return rows\n"
    source.write_text(original, encoding="utf-8")

    async def read(_sandbox, _path):
        return source.read_text(encoding="utf-8")

    async def write(_path, content, **_kwargs):
        source.write_text(content, encoding="utf-8")

    monkeypatch.setattr(apply_patch_tool, "_sandbox_read_text", read)
    tool = SandboxApplyPatchTool(MagicMock())
    tool._write = write
    result = await tool.execute(
        "*** Begin Patch\n*** Update File: demo.py\n@@\n"
        " def build_report():\n" + separator
        + "+    normalize_dates()\n     return rows\n*** End Patch"
    )
    assert result.success
    assert source.read_text(encoding="utf-8") == original.replace(
        "def build_report():\n", "def build_report():\n    normalize_dates()\n"
    )


@pytest.mark.asyncio
async def test_move_only_patch_preserves_content_and_deletes_source(monkeypatch):
    tool = SandboxApplyPatchTool(MagicMock())
    tool._write = AsyncMock()
    monkeypatch.setattr(apply_patch_tool, "_sandbox_read_text", AsyncMock(return_value="old\r\n"))
    delete = AsyncMock()
    monkeypatch.setattr(apply_patch_tool, "_delete_text", delete)

    result = await tool.execute(
        "*** Begin Patch\n*** Update File: old.txt\n"
        "*** Move to: moved.txt\n*** End Patch"
    )

    assert result.success
    tool._write.assert_awaited_once_with("/home/user/moved.txt", "old\r\n", expected_sha256=None)
    assert delete.await_args.args[1] == "/home/user/old.txt"
