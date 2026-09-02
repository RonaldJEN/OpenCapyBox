import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.tools.base import ToolRuntimeContext
from src.agent.tools.workspace_tools import (
    WorkspaceListTool,
    WorkspacePublishTool,
    WorkspaceStageTool,
)
from src.api.services.workspace_service import WorkspaceError


def _tool(cls):
    return cls(
        db_session_factory=MagicMock(),
        user_id="user-1",
        execution_root="/home/user/sessions/session-1",
        actor="chat",
    )


def _entry(**overrides):
    values = {
        "entry_id": "entry-1",
        "parent_id": None,
        "name": "report.md",
        "kind": "file",
        "relative_path": "report.md",
        "size_bytes": 12,
        "mime_type": "text/markdown",
        "sha256": "a" * 64,
        "revision": 2,
        "status": "active",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_workspace_list_returns_stable_identity_and_follows_cursor():
    tool = _tool(WorkspaceListTool)
    tool._call = AsyncMock(return_value=SimpleNamespace(
        workspace_revision=9,
        items=[_entry()],
        next_cursor="opaque-next-page",
    ))

    result = await tool.execute(query="report")

    assert result.success is True
    assert '"entry_id": "entry-1"' in result.content
    assert '"revision": 2' in result.content
    tool._call.assert_awaited_once_with(
        "list_entries",
        "user-1",
        parent_id=None,
        q="report",
        limit=50,
        cursor=None,
    )

    assert tool.parameters["properties"]["cursor"]["type"] == "string"
    first_page = json.loads(result.content)
    tool._call.return_value = SimpleNamespace(
        workspace_revision=9,
        items=[_entry(entry_id="entry-2", name="report-2.md", relative_path="report-2.md")],
        next_cursor=None,
    )
    next_result = await tool.execute(query="report", cursor=first_page["next_cursor"])

    assert next_result.success is True
    tool._call.assert_awaited_with(
        "list_entries", "user-1", parent_id=None, q="report", limit=50,
        cursor="opaque-next-page",
    )
    second_page = json.loads(next_result.content)
    assert [item["entry_id"] for item in first_page["items"] + second_page["items"]] == [
        "entry-1", "entry-2",
    ]
    assert second_page["next_cursor"] is None


@pytest.mark.asyncio
async def test_workspace_stage_uses_execution_root():
    tool = _tool(WorkspaceStageTool)
    tool._call = AsyncMock(return_value=SimpleNamespace(
        entry=_entry(),
        destination_relative_path=".workspace-snapshots/entry-1/2/report.md",
        destination_path="/home/user/sessions/session-1/.workspace-snapshots/entry-1/2/report.md",
        source_revision=2,
        sha256="a" * 64,
    ))

    result = await tool.execute("entry-1", 2)

    assert result.success is True
    payload = json.loads(result.content)
    assert payload == {
        "status": "STAGED",
        "workspace_path": "report.md",
        "snapshot_path": "/home/user/sessions/session-1/.workspace-snapshots/entry-1/2/report.md",
        "publish_source_path": ".workspace-snapshots/entry-1/2/report.md",
        "entry_id": "entry-1",
        "revision": 2,
        "version_id": None,
        "base_version_id": None,
        "tree_revision": None,
        "sha256": "a" * 64,
        "manifest_sha256": None,
    }
    assert "current execution Workspace" in tool.description
    assert "current execution Workspace" in tool.parameters["properties"]["destination_path"]["description"]
    tool._call.assert_awaited_once_with(
        "stage_entry",
        "user-1",
        "entry-1",
        expected_revision=2,
        version_id=None,
        expected_tree_revision=None,
        destination_root="/home/user/sessions/session-1",
        destination_relative_path=None,
    )


@pytest.mark.asyncio
async def test_workspace_stage_revision_race_retries_current_head_once():
    tool = _tool(WorkspaceStageTool)
    staged = SimpleNamespace(
        entry=_entry(revision=3),
        destination_relative_path=".workspace-snapshots/entry-1/3/report.md",
        destination_path="/home/user/sessions/session-1/.workspace-snapshots/entry-1/3/report.md",
        source_revision=3,
        sha256="b" * 64,
    )
    tool._call = AsyncMock(side_effect=[
        WorkspaceError(409, "REVISION_CONFLICT", "工作区条目已被修改", entry=_entry(revision=3)),
        staged,
    ])

    result = await tool.execute("entry-1", 2)

    assert result.success is True
    assert json.loads(result.content)["revision"] == 3
    assert tool._call.await_args_list[1].kwargs["expected_revision"] is None


@pytest.mark.asyncio
async def test_workspace_stage_directory_returns_entry_and_tree_revisions_separately():
    tool = _tool(WorkspaceStageTool)
    tool._call = AsyncMock(return_value=SimpleNamespace(
        entry=_entry(
            entry_id="folder-1",
            name="research",
            kind="directory",
            relative_path="research",
            revision=1,
            tree_revision=6,
        ),
        destination_relative_path=".workspace-snapshots/folder-1/capture/research",
        destination_path=(
            "/home/user/sessions/session-1/"
            ".workspace-snapshots/folder-1/capture/research"
        ),
        source_revision=1,
        tree_revision=6,
        sha256="c" * 64,
        version_id=None,
    ))

    result = await tool.execute("folder-1", revision=1, tree_revision=6)

    assert result.success is True
    payload = json.loads(result.content)
    assert payload["revision"] == 1
    assert payload["tree_revision"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("source_path", ["../secret", "/etc/passwd", "..", "."])
async def test_workspace_publish_rejects_source_outside_execution_root(source_path):
    tool = _tool(WorkspacePublishTool)
    tool._call = AsyncMock()

    result = await tool.execute(source_path, "report.md")

    assert result.success is False
    assert "INVALID_SOURCE_PATH" in (result.error or "")
    tool._call.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_publish_returns_structured_resource_change():
    tool = _tool(WorkspacePublishTool)
    tool._call = AsyncMock(return_value=SimpleNamespace(
        status="UPDATED",
        entry=_entry(),
        mutation_id="mutation-1",
    ))

    result = await tool.execute(
        "candidate/report.md",
        "report.md",
        conflict_policy="overwrite",
        expected_destination_revision=1,
    )

    assert result.success is True
    assert result.resource_changes == [{
        "entry_id": "entry-1",
        "parent_id": None,
        "name": "report.md",
        "kind": "file",
        "path": "report.md",
        "size_bytes": 12,
        "mime_type": "text/markdown",
        "sha256": "a" * 64,
        "revision": 2,
        "current_version_id": None,
        "tree_revision": 1,
        "status": "active",
        "operation": "UPDATED",
        "mutation_id": "mutation-1",
    }]
    tool._call.assert_awaited_once()


@pytest.mark.asyncio
async def test_workspace_publish_conflict_stays_internal_and_retries_in_background():
    tool = _tool(WorkspacePublishTool)
    tool._call = AsyncMock(return_value=SimpleNamespace(
        status="CONFLICT",
        change_set_id="change-set-1",
        entry=None,
        mutation_id=None,
        base_version_id="version-1",
        proposed_version_id=None,
        applied_version_id=None,
        error_code="BASE_VERSION_CONFLICT",
        error_message="目标文件版本已变化",
    ))

    result = await tool.execute(
        "candidate/report.md",
        "report.md",
        conflict_policy="overwrite",
        expected_destination_revision=1,
        base_version_id="version-1",
    )

    assert result.success is True
    assert result.resource_changes is None
    assert result.workspace_change_events is None
    assert "后台重新合并" in result.content
    assert "不要删除或重建" in result.content

    tool._call = AsyncMock(return_value=SimpleNamespace(
        status="FAILED",
        change_set_id="change-set-2",
        entry=None,
        mutation_id=None,
        error_code="SANDBOX_READ_FAILED",
        error_message="内部版本读取失败",
    ))
    failed = await tool.execute("candidate/report.md", "report.md")
    assert failed.success is False
    assert "SANDBOX_READ_FAILED" in failed.error
    assert "不要删除或重建" in failed.error
    assert failed.workspace_change_events is None


@pytest.mark.asyncio
async def test_workspace_tool_checks_execution_fence_before_service_call():
    fence = MagicMock(side_effect=PermissionError("lease lost"))
    db = MagicMock()
    db_factory = MagicMock(return_value=db)
    tool = WorkspaceListTool(
        db_session_factory=db_factory,
        user_id="user-1",
        execution_root="/home/user/sessions/session-1",
        actor="cron",
        fence=fence,
    )

    with pytest.raises(PermissionError, match="lease lost"):
        await tool._call("list_entries", "user-1")

    fence.assert_called_once_with(db)


def test_cron_workspace_context_uses_cron_session_as_run_id_and_records_change():
    recorder = MagicMock()
    db = MagicMock()
    tool = WorkspacePublishTool(
        db_session_factory=MagicMock(return_value=db),
        user_id="user-1",
        execution_root="/home/user/cron/runs/cron-run-1",
        actor="cron",
        change_recorder=recorder,
        base_context={"cron_job_id": "17", "cron_run_id": "cron-run-1"},
    )
    tool.set_runtime_context(ToolRuntimeContext(
        thread_id="cron-run-1",
        run_id="agent-round-1",
        tool_call_id="tool-call-1",
        tool_name="workspace_publish",
    ))

    assert tool._mutation_context() == {
        "cron_job_id": "17",
        "round_id": "agent-round-1",
        "tool_call_id": "tool-call-1",
        "cron_run_id": "cron-run-1",
    }
    result = tool._mutation_result(SimpleNamespace(
        status="UPDATED",
        entry=_entry(),
        mutation_id="mutation-1",
    ))

    assert result.success is True
    recorder.assert_called_once_with(db, result.resource_changes[0])
