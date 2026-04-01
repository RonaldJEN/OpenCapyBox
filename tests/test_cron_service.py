"""Cron 服务 (cron_service) 单元测试

覆盖：
- HEARTBEAT.md 解析
- CronTask 数据结构
- parse_cron_fields
- CronService CRUD
"""
import pytest
from unittest.mock import MagicMock, patch

from tests.helpers import make_query_db


# ── 共享工厂 ────────────────────────────────────────────────


def _make_cron_service(*, query_return=None, first_return=None):
    """构建 CronService + mock_db，减少样板"""
    from src.api.services.cron_service import CronService

    mock_db = make_query_db(first=first_return, all_results=query_return)
    # CronService 还会用到 .order_by().limit().all()
    if query_return is not None:
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = query_return
    return CronService(mock_db), mock_db


def _make_cron_db(jobs):
    """构建只含 CronJob 查询链的 mock_db（用于 register_user_jobs）"""
    return make_query_db(all_results=jobs)


class TestParseHeartbeatMd:
    """parse_heartbeat_md 解析测试"""

    def test_empty_content(self):
        from src.api.services.cron_service import parse_heartbeat_md

        tasks = parse_heartbeat_md("")
        assert tasks == []

    def test_single_enabled_task(self):
        from src.api.services.cron_service import parse_heartbeat_md

        content = "- [ ] daily_report 0 9 * * * - 每天9点生成日报"
        tasks = parse_heartbeat_md(content)
        assert len(tasks) == 1
        assert tasks[0].name == "daily_report"
        assert tasks[0].cron_expr == "0 9 * * *"
        assert tasks[0].description == "每天9点生成日报"
        assert tasks[0].enabled is True

    def test_disabled_task(self):
        from src.api.services.cron_service import parse_heartbeat_md

        content = "- [x] cleanup 0 0 * * 0 - 每周清理"
        tasks = parse_heartbeat_md(content)
        assert len(tasks) == 1
        assert tasks[0].name == "cleanup"
        assert tasks[0].enabled is False

    def test_multiple_tasks(self):
        from src.api.services.cron_service import parse_heartbeat_md

        content = """# 定时任务

- [ ] daily_report 0 9 * * * - 每天9点
- [x] weekly_clean 0 0 * * 0 - 每周清理
- [ ] hourly_check */5 * * * * - 每5分钟检查
"""
        tasks = parse_heartbeat_md(content)
        assert len(tasks) == 3
        assert tasks[0].enabled is True
        assert tasks[1].enabled is False
        assert tasks[2].name == "hourly_check"

    def test_task_without_description(self):
        from src.api.services.cron_service import parse_heartbeat_md

        content = "- [ ] simple_task 0 0 * * *"
        tasks = parse_heartbeat_md(content)
        assert len(tasks) == 1
        assert tasks[0].description == ""

    def test_ignores_non_task_lines(self):
        from src.api.services.cron_service import parse_heartbeat_md

        content = """# HEARTBEAT.md
这是说明文字

- 普通列表项
- [ ] valid_task 0 0 * * * - 有效任务

> 引用文字
"""
        tasks = parse_heartbeat_md(content)
        assert len(tasks) == 1
        assert tasks[0].name == "valid_task"


class TestParseCronFields:
    """parse_cron_fields 测试"""

    def test_valid_5_field_cron(self):
        from src.api.services.cron_service import parse_cron_fields

        result = parse_cron_fields("0 9 * * *")
        assert result == {
            "minute": "0",
            "hour": "9",
            "day": "*",
            "month": "*",
            "day_of_week": "*",
        }

    def test_invalid_cron_too_few_fields(self):
        from src.api.services.cron_service import parse_cron_fields

        assert parse_cron_fields("0 9 *") is None

    def test_invalid_cron_too_many_fields(self):
        from src.api.services.cron_service import parse_cron_fields

        assert parse_cron_fields("0 9 * * * *") is None


class TestCronTask:
    """CronTask 数据结构测试"""

    def test_to_dict(self):
        from src.api.services.cron_service import CronTask

        task = CronTask(
            name="test",
            cron_expr="0 0 * * *",
            description="Test task",
            enabled=True,
        )
        d = task.to_dict()
        assert d["name"] == "test"
        assert d["cron_expr"] == "0 0 * * *"
        assert d["description"] == "Test task"
        assert d["enabled"] is True


class TestCronServiceDB:
    """CronService 数据库操作测试"""

    def test_get_heartbeat_content_empty(self):
        svc, mock_db = _make_cron_service()
        # get_heartbeat_content queries UserMemory; explicitly set first()=None
        mock_db.query.return_value.filter.return_value.first.return_value = None
        assert svc.get_heartbeat_content("user-1") == ""

    def test_get_heartbeat_content_exists(self):
        record = MagicMock()
        record.content = "- [ ] test 0 0 * * *"
        svc, _ = _make_cron_service(first_return=record)
        assert svc.get_heartbeat_content("user-1") == "- [ ] test 0 0 * * *"

    def test_get_tasks_from_db(self):
        """get_tasks 现在从 CronJob 表查询"""
        job1 = MagicMock()
        job1.name = "daily"
        job1.cron_expr = "0 9 * * *"
        job1.description = "every day"
        job1.enabled = True

        svc, _ = _make_cron_service(query_return=[job1])
        tasks = svc.get_tasks("user-1")
        assert len(tasks) == 1
        assert tasks[0].name == "daily"

    def test_get_run_history(self):
        run1 = MagicMock()
        run1.id = "r1"
        run1.job_name = "daily"
        run1.cron_expr = "0 9 * * *"
        run1.started_at = None
        run1.completed_at = None
        run1.status = "success"
        run1.output = "ok"

        svc, _ = _make_cron_service(query_return=[run1])
        runs = svc.get_run_history("user-1")
        assert len(runs) == 1
        assert runs[0]["job_name"] == "daily"


class TestRegisterUserJobs:
    """register_user_jobs / reload_user_jobs 测试（DB 驱动）"""

    def test_register_enabled_tasks(self):
        from src.api.services.cron_service import register_user_jobs

        job1 = MagicMock(); job1.name = "daily"; job1.cron_expr = "0 9 * * *"
        job1.description = "every day"; job1.enabled = True

        job2 = MagicMock(); job2.name = "disabled_task"; job2.cron_expr = "0 0 * * *"
        job2.description = "paused"; job2.enabled = False

        job3 = MagicMock(); job3.name = "hourly"; job3.cron_expr = "*/5 * * * *"
        job3.description = "every 5 min"; job3.enabled = True

        mock_db = _make_cron_db([job1, job2, job3])

        mock_scheduler = MagicMock()
        count = register_user_jobs(mock_db, "user-1", mock_scheduler)

        # 2 enabled tasks, 1 disabled → 2 registered
        assert count == 2
        assert mock_scheduler.add_job.call_count == 2

        # 检查 job id 格式
        call_args_list = mock_scheduler.add_job.call_args_list
        job_ids = [call.kwargs.get("id") or call[1].get("id") for call in call_args_list]
        assert "cron-user-1-daily" in job_ids
        assert "cron-user-1-hourly" in job_ids

        # 确认传递 job_name 而非 task_dict
        for call in call_args_list:
            kwargs_passed = call.kwargs.get("kwargs") or call[1].get("kwargs")
            assert "job_name" in kwargs_passed
            assert "task_dict" not in kwargs_passed

    def test_register_no_tasks(self):
        from src.api.services.cron_service import register_user_jobs

        mock_db = _make_cron_db([])
        mock_scheduler = MagicMock()
        count = register_user_jobs(mock_db, "user-1", mock_scheduler)
        assert count == 0
        mock_scheduler.add_job.assert_not_called()

    def test_register_invalid_cron_skipped(self):
        from src.api.services.cron_service import register_user_jobs

        job = MagicMock()
        job.name = "bad_cron"; job.cron_expr = "invalid"
        job.description = "bad"; job.enabled = True

        mock_db = _make_cron_db([job])
        mock_scheduler = MagicMock()
        count = register_user_jobs(mock_db, "user-1", mock_scheduler)
        assert count == 0

    def test_reload_removes_old_jobs(self):
        from src.api.services.cron_service import reload_user_jobs

        mock_job1 = MagicMock()
        mock_job1.id = "cron-user-1-old_task"
        mock_job2 = MagicMock()
        mock_job2.id = "cron-user-2-other"  # different user, should NOT be removed

        mock_scheduler = MagicMock()
        mock_scheduler.get_jobs.return_value = [mock_job1, mock_job2]

        with patch("src.api.services.cron_service.register_user_jobs", return_value=1) as mock_reg:
            count = reload_user_jobs("user-1", mock_scheduler)

        # only user-1's job removed
        mock_job1.remove.assert_called_once()
        mock_job2.remove.assert_not_called()
        assert count == 1
