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
    svc.get_agents_template_content = MagicMock(return_value="template agents")
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


def test_agents_template_routes_connected_data_before_web_search():
    from src.api.services.memory_service import MemoryService

    content = MemoryService(MagicMock()).get_agents_template_content()
    data_route = content.index("`tool_search`（按连接名或能力词发现）")
    web_fallback = content.index("数据连接无匹配 / 调用失败")

    assert data_route < web_fallback
    assert "`search` / `batch_search`" in content[web_fallback:]


@pytest.mark.asyncio
async def test_sandbox_first_when_not_forced():
    """非 force 模式：沙箱有内容 → 保留沙箱版本并回写 DB"""
    svc = _make_memory_service({"soul_md": "short default template"})
    sandbox = _make_sandbox({"SOUL.md": "rich 263-line content from agent"})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=False)

    # 不应覆盖 SOUL.md；AGENTS.md 仍由平台模板覆盖
    assert count == 1
    sandbox.files.write_file.assert_awaited_once_with("/home/user/AGENTS.md", "template agents")
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

    assert count == 1
    sandbox.files.write_file.assert_awaited_once_with("/home/user/AGENTS.md", "template agents")
    svc.upsert_memory_file.assert_not_called()


@pytest.mark.asyncio
async def test_writes_db_content_when_sandbox_empty():
    """非 force 模式：沙箱文件不存在 → 正常推送 DB 版本"""
    svc = _make_memory_service({"soul_md": "default template"})
    sandbox = _make_sandbox({"SOUL.md": FileNotFoundError("not found")})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=False)

    assert count == 2
    writes = {call.args[0]: call.args[1] for call in sandbox.files.write_file.await_args_list}
    assert writes["/home/user/SOUL.md"] == "default template"
    assert writes["/home/user/AGENTS.md"] == "template agents"


@pytest.mark.asyncio
async def test_force_overwrites_sandbox():
    """force=True：无条件 DB → 沙箱推送，即使沙箱有内容"""
    svc = _make_memory_service({"soul_md": "user edited content"})
    sandbox = _make_sandbox({"SOUL.md": "old sandbox content"})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=True)

    assert count == 2
    writes = {call.args[0]: call.args[1] for call in sandbox.files.write_file.await_args_list}
    assert writes["/home/user/SOUL.md"] == "user edited content"
    assert writes["/home/user/AGENTS.md"] == "template agents"
    # force 模式不回写 DB
    svc.upsert_memory_file.assert_not_called()


@pytest.mark.asyncio
async def test_selective_force_sync_skips_agents_template():
    """配置面板保存单文件时，只同步该 DB-backed 文件，不重传 AGENTS.md。"""
    svc = _make_memory_service({
        "soul_md": "new soul",
        "memory_md": "long memory",
    })
    sandbox = _make_sandbox({})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox(
            "user-1",
            sandbox,
            force=True,
            file_types={"soul_md"},
            include_agents_template=False,
        )

    assert count == 1
    sandbox.files.read_file.assert_not_called()
    sandbox.files.write_file.assert_awaited_once_with("/home/user/SOUL.md", "new soul")
    svc.get_agents_template_content.assert_not_called()


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

    # 不应覆盖 SOUL.md（跳过），但 AGENTS.md 仍由平台模板覆盖
    assert count == 1
    sandbox.files.write_file.assert_awaited_once_with("/home/user/AGENTS.md", "template agents")
    svc.upsert_memory_file.assert_not_called()


@pytest.mark.asyncio
async def test_sandbox_whitespace_only_treated_as_empty():
    """非 force 模式：沙箱文件仅空白字符 → 视为无内容，推送 DB 版本"""
    svc = _make_memory_service({"soul_md": "real content"})
    sandbox = _make_sandbox({"SOUL.md": "   \n  \n  "})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        count = await svc.sync_to_sandbox("user-1", sandbox, force=False)

    assert count == 2
    writes = {call.args[0]: call.args[1] for call in sandbox.files.write_file.await_args_list}
    assert writes["/home/user/SOUL.md"] == "real content"
    assert writes["/home/user/AGENTS.md"] == "template agents"


def test_agent_config_path_matches_only_mount_root():
    """仅沙箱根目录 DB-backed 配置文件会映射为 agent 配置"""
    from src.api.services.memory_service import get_agent_config_file_type_for_path

    assert get_agent_config_file_type_for_path("/home/user/USER.md", "/home/user") == "user_md"
    assert get_agent_config_file_type_for_path("/home/user/MEMORY.md", "/home/user") == "memory_md"
    assert get_agent_config_file_type_for_path("/home/user/SOUL.md", "/home/user") == "soul_md"
    assert get_agent_config_file_type_for_path("/home/user/AGENTS.md", "/home/user") is None
    assert get_agent_config_file_type_for_path("/home/user/sessions/run-1/AGENTS.md", "/home/user") is None
    assert get_agent_config_file_type_for_path("/home/user/project/AGENTS.md", "/home/user") is None


@pytest.mark.asyncio
async def test_sync_from_sandbox_persists_empty_string():
    """从沙箱读到空字符串也应入库"""
    from src.api.services.memory_service import MemoryService

    record = MagicMock()
    record.content = "old content"
    record.version = 1
    db = make_query_db(first=record)
    svc = MemoryService(db)
    sandbox = _make_sandbox({"MEMORY.md": ""})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        result = await svc.sync_from_sandbox("user-1", sandbox, "memory_md")

    assert result == ("", True)
    content, changed = result
    assert content == ""
    assert changed is True
    assert record.content == ""
    assert record.version == 2
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_sync_from_sandbox_same_content_does_not_bump_version():
    """沙箱内容与 DB 相同则不重复 upsert"""
    from src.api.services.memory_service import MemoryService

    record = MagicMock()
    record.content = "same content"
    record.version = 7
    db = make_query_db(first=record)
    svc = MemoryService(db)
    sandbox = _make_sandbox({"USER.md": "same content"})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        result = await svc.sync_from_sandbox("user-1", sandbox, "user_md")

    assert result == ("same content", False)
    content, changed = result
    assert content == "same content"
    assert changed is False
    assert record.version == 7
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_sync_from_sandbox_missing_file_does_not_modify_db():
    """文件缺失或读取失败不应改 DB"""
    from src.api.services.memory_service import MemoryService

    record = MagicMock()
    record.content = "old content"
    record.version = 3
    db = make_query_db(first=record)
    svc = MemoryService(db)
    sandbox = _make_sandbox({"SOUL.md": FileNotFoundError("not found")})

    with patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/home/user"):
        content = await svc.sync_from_sandbox("user-1", sandbox, "soul_md")

    assert content is None
    assert record.content == "old content"
    assert record.version == 3
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_sync_agent_config_content_rebuilds_empty_memory_embeddings():
    """USER/MEMORY 变更为空内容时也会清理旧 embedding"""
    from src.api.services.memory_service import MemoryService

    record = MagicMock()
    svc = MemoryService(make_query_db())
    svc.upsert_memory_file_if_changed = MagicMock(return_value=(record, True))
    svc.rebuild_embeddings = AsyncMock(return_value=0)

    returned, changed = await svc.sync_agent_config_content("user-1", "memory_md", "")

    assert returned is record
    assert changed is True
    svc.rebuild_embeddings.assert_awaited_once_with("user-1", "MEMORY.md", "")


@pytest.mark.asyncio
async def test_tool_factory_agent_config_sync_filters_and_persists_root_file():
    """工具层同步回调只处理根目录配置文件"""
    from src.api.services.tool_factory import _build_agent_config_sync

    db = MagicMock()
    db_session_factory = MagicMock(return_value=db)

    with patch("src.api.services.memory_service.MemoryService") as MockMemoryService:
        svc = MockMemoryService.return_value
        svc.sync_agent_config_content = AsyncMock()
        sync = _build_agent_config_sync(
            user_id="user-1",
            db_session_factory=db_session_factory,
            mount="/home/user",
        )

        await sync("/home/user/AGENTS.md", "root rules")
        await sync("/home/user/USER.md", "root user")
        await sync("/home/user/sessions/run-1/AGENTS.md", "session rules")

    svc.sync_agent_config_content.assert_awaited_once_with("user-1", "user_md", "root user")
    db_session_factory.assert_called_once()
    db.close.assert_called_once()
