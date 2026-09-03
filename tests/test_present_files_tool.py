from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.tools import present_files_tool
from src.agent.tools.present_files_tool import SandboxPresentFilesTool


@pytest.mark.asyncio
async def test_present_files_returns_only_explicit_existing_paths(monkeypatch):
    stat = AsyncMock(side_effect=[
        {
            "source": "session",
            "name": "report.md",
            "path": "final/report.md",
            "size": 42,
            "modified": "2026-09-03T00:00:00+00:00",
            "type": "md",
            "revision": "v1:42:100",
        },
        None,
    ])
    monkeypatch.setattr(present_files_tool, "stat_session_file_reference", stat)
    tool = SandboxPresentFilesTool(
        MagicMock(),
        workspace_dir="/home/user/sessions/session-1",
    )

    result = await tool.execute(["final/report.md"])
    missing = await tool.execute(["tmp/process.json"])

    assert result.success is True
    assert result.assistant_file_references == [{
        "source": "session",
        "name": "report.md",
        "path": "final/report.md",
        "size": 42,
        "modified": "2026-09-03T00:00:00+00:00",
        "type": "md",
        "revision": "v1:42:100",
        "operation": "PRESENTED",
    }]
    assert missing.success is False
    assert missing.assistant_file_references is None
