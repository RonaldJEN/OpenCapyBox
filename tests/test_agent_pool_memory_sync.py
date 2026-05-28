"""AgentPoolService 初始化记忆同步并发行为测试"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


MEMORY_RECORDS = {
    "user_md": "db user",
    "memory_md": "db memory",
    "soul_md": "db soul",
    "agents_md": "db agents",
}


def _mock_session_local(count: int = 1):
    dbs = [MagicMock() for _ in range(count)]
    sessions = []
    for db in dbs:
        session = MagicMock()
        session.__enter__.return_value = db
        session.__exit__.return_value = False
        sessions.append(session)
    return MagicMock(side_effect=sessions), dbs


@pytest.mark.asyncio
async def test_sync_memory_reads_all_files_concurrently_then_applies_sandbox_wins():
    from src.api.services.agent_pool_service import AgentPoolService

    pool = AgentPoolService(ttl=3600)
    sandbox = MagicMock()
    all_reads_started = asyncio.Event()
    read_paths: list[str] = []
    server_error = Exception("server error")
    server_error.status_code = 500

    responses = {
        "/home/user/USER.md": FileNotFoundError("missing"),
        "/home/user/MEMORY.md": "   \n",
        "/home/user/SOUL.md": "sandbox soul",
        "/home/user/AGENTS.md": server_error,
    }

    async def _read_file(path: str):
        read_paths.append(path)
        if len(read_paths) == 4:
            all_reads_started.set()
        await all_reads_started.wait()
        result = responses[path]
        if isinstance(result, Exception):
            raise result
        return result

    sandbox.files.read_file = AsyncMock(side_effect=_read_file)
    sandbox.files.write_file = AsyncMock()
    memory_service = MagicMock()
    memory_service.get_all_memory_files.return_value = MEMORY_RECORDS
    session_local, dbs = _mock_session_local(2)
    read_db, writeback_db = dbs

    with (
        patch("src.api.services.agent_pool_service.SessionLocal", session_local),
        patch("src.api.services.memory_service.MemoryService", return_value=memory_service),
        patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"),
    ):
        count = await asyncio.wait_for(
            pool._sync_memory_to_sandbox(user_id="user-1", sandbox=sandbox, force=False),
            timeout=2,
        )

    assert count == 2
    assert read_paths == [
        "/home/user/USER.md",
        "/home/user/MEMORY.md",
        "/home/user/SOUL.md",
        "/home/user/AGENTS.md",
    ]
    write_paths = [call.args[0] for call in sandbox.files.write_file.await_args_list]
    assert write_paths == ["/home/user/USER.md", "/home/user/MEMORY.md"]
    memory_service.upsert_memory_file.assert_called_once_with("user-1", "soul_md", "sandbox soul")
    read_db.rollback.assert_called_once()
    writeback_db.commit.assert_called_once()
    writeback_db.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_sync_memory_force_skips_reads_and_writes_all_files_concurrently():
    from src.api.services.agent_pool_service import AgentPoolService

    pool = AgentPoolService(ttl=3600)
    sandbox = MagicMock()
    all_writes_started = asyncio.Event()
    write_paths: list[str] = []

    async def _write_file(path: str, _content: str):
        write_paths.append(path)
        if len(write_paths) == 4:
            all_writes_started.set()
        await all_writes_started.wait()

    sandbox.files.read_file = AsyncMock()
    sandbox.files.write_file = AsyncMock(side_effect=_write_file)
    memory_service = MagicMock()
    memory_service.get_all_memory_files.return_value = MEMORY_RECORDS
    session_local, _dbs = _mock_session_local()

    with (
        patch("src.api.services.agent_pool_service.SessionLocal", session_local),
        patch("src.api.services.memory_service.MemoryService", return_value=memory_service),
        patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"),
    ):
        count = await asyncio.wait_for(
            pool._sync_memory_to_sandbox(user_id="user-1", sandbox=sandbox, force=True),
            timeout=2,
        )

    assert count == 4
    sandbox.files.read_file.assert_not_called()
    assert write_paths == [
        "/home/user/USER.md",
        "/home/user/MEMORY.md",
        "/home/user/SOUL.md",
        "/home/user/AGENTS.md",
    ]


@pytest.mark.asyncio
async def test_sync_memory_writeback_failure_skips_that_file_and_continues():
    from src.api.services.agent_pool_service import AgentPoolService

    pool = AgentPoolService(ttl=3600)
    sandbox = MagicMock()

    async def _read_file(path: str):
        if path.endswith("SOUL.md"):
            return "sandbox soul"
        raise FileNotFoundError("missing")

    sandbox.files.read_file = AsyncMock(side_effect=_read_file)
    sandbox.files.write_file = AsyncMock()
    memory_service = MagicMock()
    memory_service.get_all_memory_files.return_value = {
        "soul_md": "db soul",
        "user_md": "db user",
    }
    memory_service.upsert_memory_file.side_effect = RuntimeError("db write failed")
    session_local, dbs = _mock_session_local(2)
    read_db, writeback_db = dbs

    with (
        patch("src.api.services.agent_pool_service.SessionLocal", session_local),
        patch("src.api.services.memory_service.MemoryService", return_value=memory_service),
        patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"),
    ):
        count = await pool._sync_memory_to_sandbox(user_id="user-1", sandbox=sandbox, force=False)

    assert count == 1
    sandbox.files.write_file.assert_awaited_once_with("/home/user/USER.md", "db user")
    memory_service.upsert_memory_file.assert_called_once_with("user-1", "soul_md", "sandbox soul")
    read_db.rollback.assert_called_once()
    writeback_db.rollback.assert_called_once()
    writeback_db.commit.assert_not_called()
