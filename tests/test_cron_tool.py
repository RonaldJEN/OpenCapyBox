"""ManageCronTool 单元测试

覆盖：
- add: 创建任务、重复检测、参数校验
- remove: 删除任务、不存在检测
- list: 列出任务
- toggle: 切换启用/禁用
- history: 查看执行历史
- cron 表达式校验
"""
import pytest
from unittest.mock import MagicMock, patch


# ============== 模块级 Fixtures ==============

@pytest.fixture
def tool_and_db():
    """创建带 mock DB 的 ManageCronTool（所有 TestClass 共用）"""
    from src.agent.tools.cron_tool import ManageCronTool

    mock_db = MagicMock()
    mock_factory = MagicMock(return_value=mock_db)
    tool = ManageCronTool(db_session_factory=mock_factory, user_id="test-user")
    return tool, mock_db


class TestValidateCronExpr:
    """cron 表达式校验"""

    def test_valid_5_fields(self):
        from src.agent.tools.cron_tool import _validate_cron_expr

        assert _validate_cron_expr("0 9 * * *") is None

    def test_valid_complex(self):
        from src.agent.tools.cron_tool import _validate_cron_expr

        assert _validate_cron_expr("*/30 * * * *") is None

    def test_invalid_too_few(self):
        from src.agent.tools.cron_tool import _validate_cron_expr

        err = _validate_cron_expr("0 9 *")
        assert err is not None
        assert "3" in err

    def test_invalid_too_many(self):
        from src.agent.tools.cron_tool import _validate_cron_expr

        err = _validate_cron_expr("0 9 * * * *")
        assert err is not None
        assert "6" in err

    def test_empty(self):
        from src.agent.tools.cron_tool import _validate_cron_expr

        err = _validate_cron_expr("")
        assert err is not None


class TestManageCronToolAdd:
    """add action 测试"""

    @pytest.mark.asyncio
    async def test_add_success(self, tool_and_db):
        tool, mock_db = tool_and_db
        # 模拟不存在同名任务
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = await tool.execute(
            action="add",
            name="daily_greeting",
            cron="0 21 * * *",
            description="跟用户说晚安",
        )

        assert result.success is True
        assert "daily_greeting" in result.content
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_duplicate(self, tool_and_db):
        tool, mock_db = tool_and_db
        # 模拟已存在同名任务
        mock_db.query.return_value.filter.return_value.first.return_value = MagicMock()

        result = await tool.execute(
            action="add",
            name="daily_greeting",
            cron="0 21 * * *",
            description="跟用户说晚安",
        )

        assert result.success is False
        assert "已存在" in result.error

    @pytest.mark.asyncio
    async def test_add_missing_name(self, tool_and_db):
        tool, _ = tool_and_db
        result = await tool.execute(action="add", name="", cron="0 9 * * *", description="test")
        assert result.success is False
        assert "name" in result.error

    @pytest.mark.asyncio
    async def test_add_missing_cron(self, tool_and_db):
        tool, _ = tool_and_db
        result = await tool.execute(action="add", name="test", cron="", description="test")
        assert result.success is False
        assert "cron" in result.error

    @pytest.mark.asyncio
    async def test_add_missing_description(self, tool_and_db):
        tool, _ = tool_and_db
        result = await tool.execute(action="add", name="test", cron="0 9 * * *", description="")
        assert result.success is False
        assert "description" in result.error

    @pytest.mark.asyncio
    async def test_add_invalid_cron(self, tool_and_db):
        tool, _ = tool_and_db
        result = await tool.execute(
            action="add", name="test", cron="invalid", description="test"
        )
        assert result.success is False
        assert "5" in result.error

    @pytest.mark.asyncio
    async def test_add_never_firing_cron_does_not_persist_job(self, tool_and_db):
        tool, mock_db = tool_and_db

        result = await tool.execute(
            action="add",
            name="never",
            cron="0 0 31 2 *",
            description="这个任务永远不会触发",
        )

        assert result.success is False
        assert "无法产生未来执行时间" in result.error
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

class TestManageCronToolRemove:
    """remove action 测试"""

    @pytest.mark.asyncio
    async def test_remove_success(self, tool_and_db):
        tool, _ = tool_and_db

        with patch("src.api.services.cron_service.CronService") as svc_cls:
            svc = svc_cls.return_value
            result = await tool.execute(action="remove", name="daily_greeting")

        assert result.success is True
        svc.delete_job.assert_called_once_with("test-user", "daily_greeting")

    @pytest.mark.asyncio
    async def test_remove_not_found(self, tool_and_db):
        tool, _ = tool_and_db
        from src.api.services.cron_service import CronJobNotFoundError

        with patch("src.api.services.cron_service.CronService") as svc_cls:
            svc = svc_cls.return_value
            svc.delete_job.side_effect = CronJobNotFoundError("任务 'nonexistent' 不存在")
            result = await tool.execute(action="remove", name="nonexistent")

        assert result.success is False
        assert "不存在" in result.error

    @pytest.mark.asyncio
    async def test_remove_empty_name(self, tool_and_db):
        tool, _ = tool_and_db
        result = await tool.execute(action="remove", name="")
        assert result.success is False


class TestManageCronToolList:
    """list action 测试"""

    @pytest.mark.asyncio
    async def test_list_empty(self, tool_and_db):
        tool, mock_db = tool_and_db
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

        result = await tool.execute(action="list")
        assert result.success is True
        assert "没有" in result.content

    @pytest.mark.asyncio
    async def test_list_with_jobs(self, tool_and_db):
        tool, mock_db = tool_and_db

        job1 = MagicMock()
        job1.name = "daily_greeting"
        job1.cron_expr = "0 21 * * *"
        job1.description = "晚安"
        job1.enabled = True

        job2 = MagicMock()
        job2.name = "weekly_report"
        job2.cron_expr = "0 9 * * 1"
        job2.description = "周报"
        job2.enabled = False

        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            job1, job2
        ]

        result = await tool.execute(action="list")
        assert result.success is True
        assert "daily_greeting" in result.content
        assert "weekly_report" in result.content
        assert "启用" in result.content
        assert "暂停" in result.content


class TestManageCronToolToggle:
    """toggle action 测试"""

    @pytest.mark.asyncio
    async def test_toggle_enable(self, tool_and_db):
        tool, _ = tool_and_db

        task = MagicMock()
        task.name = "test_job"
        task.enabled = False
        updated = MagicMock()
        updated.enabled = True

        with patch("src.api.services.cron_service.CronService") as svc_cls:
            svc = svc_cls.return_value
            svc.get_jobs.return_value = [task]
            svc.update_job.return_value = updated
            result = await tool.execute(action="toggle", name="test_job")

        assert result.success is True
        svc.update_job.assert_called_once_with("test-user", "test_job", enabled=True)
        assert "启用" in result.content

    @pytest.mark.asyncio
    async def test_toggle_disable(self, tool_and_db):
        tool, _ = tool_and_db

        task = MagicMock()
        task.name = "test_job"
        task.enabled = True
        updated = MagicMock()
        updated.enabled = False

        with patch("src.api.services.cron_service.CronService") as svc_cls:
            svc = svc_cls.return_value
            svc.get_jobs.return_value = [task]
            svc.update_job.return_value = updated
            result = await tool.execute(action="toggle", name="test_job")

        assert result.success is True
        svc.update_job.assert_called_once_with("test-user", "test_job", enabled=False)
        assert "暂停" in result.content

    @pytest.mark.asyncio
    async def test_toggle_not_found(self, tool_and_db):
        tool, _ = tool_and_db

        with patch("src.api.services.cron_service.CronService") as svc_cls:
            svc = svc_cls.return_value
            svc.get_jobs.return_value = []
            result = await tool.execute(action="toggle", name="nonexistent")

        assert result.success is False
        assert "不存在" in result.error

    @pytest.mark.asyncio
    async def test_toggle_busy_error(self, tool_and_db):
        tool, _ = tool_and_db
        from src.api.services.cron_service import CronJobBusyError

        task = MagicMock()
        task.name = "test_job"
        task.enabled = True

        with patch("src.api.services.cron_service.CronService") as svc_cls:
            svc = svc_cls.return_value
            svc.get_jobs.return_value = [task]
            svc.update_job.side_effect = CronJobBusyError("数据库繁忙，请稍后重试")
            result = await tool.execute(action="toggle", name="test_job")

        assert result.success is False
        assert "数据库繁忙" in result.error


class TestManageCronToolHistory:
    """history action 测试"""

    @pytest.mark.asyncio
    async def test_history_empty(self, tool_and_db):
        tool, mock_db = tool_and_db
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        result = await tool.execute(action="history")
        assert result.success is True
        assert "暂无" in result.content

    @pytest.mark.asyncio
    async def test_history_with_runs(self, tool_and_db):
        tool, mock_db = tool_and_db
        from datetime import datetime

        run1 = MagicMock()
        run1.status = "success"
        run1.job_name = "daily_greeting"
        run1.started_at = datetime(2026, 3, 31, 21, 0)
        run1.output = "晚安！祝你好梦。"

        # Mock the chain: query().filter().filter().order_by().limit().all()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = [run1]
        mock_db.query.return_value = mock_query

        result = await tool.execute(action="history", name="daily_greeting")
        assert result.success is True
        assert "daily_greeting" in result.content
        assert "success" in result.content


class TestManageCronToolSchema:
    """工具 schema 测试"""

    def test_name(self):
        from src.agent.tools.cron_tool import ManageCronTool

        tool = ManageCronTool(db_session_factory=MagicMock(), user_id="test")
        assert tool.name == "manage_cron"

    def test_parameters_schema(self):
        from src.agent.tools.cron_tool import ManageCronTool

        tool = ManageCronTool(db_session_factory=MagicMock(), user_id="test")
        params = tool.parameters
        assert params["type"] == "object"
        assert "action" in params["properties"]
        assert params["properties"]["action"]["enum"] == [
            "add", "remove", "list", "toggle", "history"
        ]

    def test_to_openai_schema(self):
        from src.agent.tools.cron_tool import ManageCronTool

        tool = ManageCronTool(db_session_factory=MagicMock(), user_id="test")
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "manage_cron"

    def test_description_uses_linux_day_of_week(self):
        from src.agent.tools.cron_tool import ManageCronTool

        tool = ManageCronTool(db_session_factory=MagicMock(), user_id="test")
        desc = tool.description
        assert "0/7=Sunday, 1=Monday" in desc
        assert "'0 9 * * 1' (Monday 9am)" in desc

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        from src.agent.tools.cron_tool import ManageCronTool

        tool = ManageCronTool(db_session_factory=MagicMock(), user_id="test")
        result = await tool.execute(action="invalid_action")
        assert result.success is False
        assert "未知" in result.error
