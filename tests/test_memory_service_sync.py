"""sync_to_sandbox 沙箱优先策略测试"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.helpers import make_query_db


def _make_memory_service(records: dict[str, str]):
    """创建 MemoryService 并 mock get_all_memory_files"""
    from src.api.services.memory_service import MemoryService

    db = make_query_db(count=1)
    svc = MemoryService(db)
    svc.get_all_memory_files = MagicMock(return_value=records)
    svc.upsert_memory_file = MagicMock()
    return svc


def _make_sandbox(read_results: dict[str, str | Exception]):
    """创建 mock sandbox，read_file 按路径返回不同结果"""
    sandbox = AsyncMock()

    async def _read_file(path: str):
        for filename, val in read_results.items():
            if filename in path:
                if isinstance(val, Exception):
                    raise val
                return val
        raise FileNotFoundError(f"not found: {path}")

    sandbox.files.read_file = AsyncMock(side_effect=_read_file)
    sandbox.files.write_file = AsyncMock()
    return sandbox


@pytest.mark.asyncio
async def test_sandbox_first_when_not_forced():
    """非 force 模式：沙箱有内容 → 保留沙箱版本并回写 DB"""
    svc = _make_memory_service({"soul_md": "short default template"})
    sandbox = _make_sandbox({"SOUL.md": "rich 263-line content from agent"})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=False)

    # 不应写入沙箱
    assert count == 0
    sandbox.files.write_file.assert_not_called()
    # 应回写 DB
    svc.upsert_memory_file.assert_called_once_with(
        "user-1", "soul_md", "rich 263-line content from agent"
    )


@pytest.mark.asyncio
async def test_sandbox_first_skip_writeback_when_identical():
    """非 force 模式：沙箱内容与 DB 相同 → 跳过写入但也不回写 DB"""
    same_content = "identical content"
    svc = _make_memory_service({"soul_md": same_content})
    sandbox = _make_sandbox({"SOUL.md": same_content})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=False)

    assert count == 0
    sandbox.files.write_file.assert_not_called()
    svc.upsert_memory_file.assert_not_called()


@pytest.mark.asyncio
async def test_writes_db_content_when_sandbox_empty():
    """非 force 模式：沙箱文件不存在 → 正常推送 DB 版本"""
    svc = _make_memory_service({"soul_md": "default template"})
    sandbox = _make_sandbox({"SOUL.md": FileNotFoundError("not found")})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=False)

    assert count == 1
    sandbox.files.write_file.assert_called_once()
    call_args = sandbox.files.write_file.call_args[0]
    assert "SOUL.md" in call_args[0]
    assert call_args[1] == "default template"


@pytest.mark.asyncio
async def test_force_overwrites_sandbox():
    """force=True：无条件 DB → 沙箱推送，即使沙箱有内容"""
    svc = _make_memory_service({"soul_md": "user edited content"})
    sandbox = _make_sandbox({"SOUL.md": "old sandbox content"})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=True)

    assert count == 1
    sandbox.files.write_file.assert_called_once()
    call_args = sandbox.files.write_file.call_args[0]
    assert call_args[1] == "user edited content"
    # force 模式不回写 DB
    svc.upsert_memory_file.assert_not_called()


@pytest.mark.asyncio
async def test_network_error_skips_file_instead_of_overwriting():
    """非 force 模式：沙箱读取遇到非 404 错误 → 跳过该文件，不用 DB 覆盖"""
    svc = _make_memory_service({"soul_md": "old db content"})

    # 模拟 SDK 500 错误（带 status_code 属性）
    api_error = Exception("Internal Server Error")
    api_error.status_code = 500
    sandbox = _make_sandbox({"SOUL.md": api_error})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=False)

    # 不应写入沙箱（跳过），也不应回写 DB
    assert count == 0
    sandbox.files.write_file.assert_not_called()
    svc.upsert_memory_file.assert_not_called()


@pytest.mark.asyncio
async def test_sandbox_whitespace_only_treated_as_empty():
    """非 force 模式：沙箱文件仅空白字符 → 视为无内容，推送 DB 版本"""
    svc = _make_memory_service({"soul_md": "real content"})
    sandbox = _make_sandbox({"SOUL.md": "   \n  \n  "})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=False)

    assert count == 1
    sandbox.files.write_file.assert_called_once()
