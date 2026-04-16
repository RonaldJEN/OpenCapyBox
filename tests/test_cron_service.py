"""Cron 服务 (cron_service) 单元测试

覆盖：
- CronTask 数据结构
- parse_cron_fields
- CronService CRUD
- Cron 路由端点（trigger_job run_id、get_run_status）
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import make_query_db, make_test_client


# ── 共享工厂 ────────────────────────────────────────────────


def _make_cron_service(*, query_return=None, first_return=None):
    """构建 CronService + mock_db，减少样板"""
    from src.api.services.cron_service import CronService

    mock_db = make_query_db(first=first_return, all_results=query_return)
    # CronService 还会用到 .order_by().limit().all() 和 .order_by().offset().limit().all()
    if query_return is not None:
        mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = query_return
        mock_db.query.return_value.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = query_return
    return CronService(mock_db), mock_db


def _make_cron_db(jobs):
    """构建只含 CronJob 查询链的 mock_db（用于 register_user_jobs）"""
    return make_query_db(all_results=jobs)


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
        run1.is_read = True
        run1.artifacts = None
        run1.run_workspace = None
        run1.to_dict.return_value = {
            "id": "r1", "job_name": "daily", "cron_expr": "0 9 * * *",
            "started_at": None, "completed_at": None, "status": "success",
            "output": "ok", "is_read": True, "artifacts": None, "run_workspace": None,
        }

        svc, mock_db = _make_cron_service(query_return=[run1])
        # get_run_history 还需要 count() 支持
        mock_db.query.return_value.filter.return_value.count.return_value = 1
        runs, total = svc.get_run_history("user-1")
        assert total == 1
        assert len(runs) == 1
        assert runs[0]["job_name"] == "daily"
        assert runs[0]["is_read"] is True


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


class TestCronRoutes:
    """Cron 路由端点测试"""

    @pytest.fixture
    def client(self):
        from src.api.routes import cron as cron_routes
        return make_test_client(cron_routes.router, "/cron")

    def test_trigger_job_returns_run_id(self, client):
        """POST /jobs/{name}/run 应返回 run_id"""
        mock_job = MagicMock()
        mock_job.name = "daily"
        mock_job.cron_expr = "0 9 * * *"

        client.mock_db.query.return_value.filter.return_value.first.return_value = mock_job

        with patch("src.api.routes.cron.run_cron_job"):
            response = client.post("/cron/jobs/daily/run")

        assert response.status_code == 200
        data = response.json()
        assert "run_id" in data
        assert data["status"] == "accepted"
        assert data["job_name"] == "daily"
        # run_id 应该是 UUID 格式
        import uuid
        uuid.UUID(data["run_id"])  # 不抛异常即合法

    def test_trigger_job_not_found(self, client):
        """POST /jobs/{name}/run 任务不存在返回 404"""
        client.mock_db.query.return_value.filter.return_value.first.return_value = None

        response = client.post("/cron/jobs/nonexistent/run")
        assert response.status_code == 404

    def test_get_run_status_found(self, client):
        """GET /runs/{run_id} 返回执行记录"""
        mock_run = MagicMock()
        mock_run.id = "run-uuid-123"
        mock_run.job_name = "daily"
        mock_run.cron_expr = "0 9 * * *"
        mock_run.started_at = None
        mock_run.completed_at = None
        mock_run.status = "running"
        mock_run.output = None
        mock_run.is_read = False
        mock_run.artifacts = None
        mock_run.run_workspace = None
        mock_run.to_dict.return_value = {
            "id": "run-uuid-123", "job_name": "daily", "cron_expr": "0 9 * * *",
            "started_at": None, "completed_at": None, "status": "running",
            "output": None, "is_read": False, "artifacts": None, "run_workspace": None,
        }

        client.mock_db.query.return_value.filter.return_value.first.return_value = mock_run

        response = client.get("/cron/runs/run-uuid-123")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "run-uuid-123"
        assert data["status"] == "running"
        assert data["job_name"] == "daily"

    def test_get_run_status_not_found(self, client):
        """GET /runs/{run_id} 记录不存在返回 404"""
        client.mock_db.query.return_value.filter.return_value.first.return_value = None

        response = client.get("/cron/runs/nonexistent")
        assert response.status_code == 404


class TestRunCronJobFallback:
    """run_cron_job 兜底逻辑测试"""

    @pytest.mark.asyncio
    async def test_marks_preexisting_run_as_failed_when_job_missing(self):
        """job 被删除后，预创建的 CronJobRun 应被标记为 failed"""
        from src.api.services.cron_service import run_cron_job

        pre_run = MagicMock()
        pre_run.id = "pre-run-id"
        pre_run.status = "running"

        call_count = {"n": 0}

        def fake_first():
            call_count["n"] += 1
            if call_count["n"] == 1:
                # 第一次调用：CronJob 查询 → 不存在
                return None
            # 第二次调用：CronJobRun 查询 → 预创建记录
            return pre_run

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.side_effect = fake_first
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)

        with patch("src.api.models.database.SessionLocal", return_value=mock_db):
            result = await run_cron_job("user-1", "deleted_job", run_id="pre-run-id")

        assert result is None
        assert pre_run.status == "failed"
        assert pre_run.output == "任务不存在"
        mock_db.commit.assert_called()


class TestScanRunArtifacts:
    """_scan_run_artifacts 扫描兼容性测试"""

    @pytest.mark.asyncio
    async def test_extracts_files_from_logs_stdout(self):
        """OpenSandbox 若仅在 logs.stdout 返回内容，仍应正确提取产物。"""
        from src.api.services.cron_service import _scan_run_artifacts

        run_workspace = "/home/user/cron/runs/run-1"
        line = MagicMock()
        line.text = f"{run_workspace}/report.md\t128\n"

        cmd_result = MagicMock()
        cmd_result.logs = MagicMock(stdout=[line])
        cmd_result.stdout = ""

        sandbox = MagicMock()
        sandbox.commands = MagicMock()
        sandbox.commands.run = AsyncMock(return_value=cmd_result)

        artifacts_json = await _scan_run_artifacts(sandbox, run_workspace)
        assert artifacts_json is not None

        artifacts = json.loads(artifacts_json)
        assert len(artifacts) == 1
        assert artifacts[0]["name"] == "report.md"
        assert artifacts[0]["path"] == "report.md"
        assert artifacts[0]["size"] == 128

    @pytest.mark.asyncio
    async def test_fallback_to_plain_find_when_printf_unavailable(self):
        """当 -printf/stat 输出不可用时，仍可回退到基础 find 列表。"""
        from src.api.services.cron_service import _scan_run_artifacts

        run_workspace = "/home/user/cron/runs/run-2"

        empty_result_1 = MagicMock()
        empty_result_1.logs = MagicMock(stdout=[])
        empty_result_1.stdout = ""

        empty_result_2 = MagicMock()
        empty_result_2.logs = MagicMock(stdout=[])
        empty_result_2.stdout = ""

        plain_line = MagicMock()
        plain_line.text = f"{run_workspace}/news.md\n"
        plain_result = MagicMock()
        plain_result.logs = MagicMock(stdout=[plain_line])
        plain_result.stdout = ""

        sandbox = MagicMock()
        sandbox.commands = MagicMock()
        sandbox.commands.run = AsyncMock(side_effect=[empty_result_1, empty_result_2, plain_result])

        artifacts_json = await _scan_run_artifacts(sandbox, run_workspace)
        assert artifacts_json is not None

        artifacts = json.loads(artifacts_json)
        assert len(artifacts) == 1
        assert artifacts[0]["name"] == "news.md"
        assert artifacts[0]["path"] == "news.md"
        assert artifacts[0]["size"] == 0
