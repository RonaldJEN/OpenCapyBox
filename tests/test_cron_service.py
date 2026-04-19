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

        svc, mock_db = _make_cron_service(query_return=[run1])
        # get_run_history 还需要 count() 支持
        mock_db.query.return_value.filter.return_value.count.return_value = 1
        runs, total = svc.get_run_history("user-1")
        assert total == 1
        assert len(runs) == 1
        assert runs[0]["job_name"] == "daily"
        assert runs[0]["is_read"] is True


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
        client.app.state.cron_user_locks = {}
        client.app.state.cron_worker_id = "worker-test"

        with patch("src.api.routes.cron.trigger_manual_run", new_callable=AsyncMock):
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
    async def test_quotes_workspace_path_with_spaces(self):
        """workspace 含空格时所有 find 命令必须 shlex.quote，否则会拆词失败。"""
        from src.api.services.cron_service import _scan_run_artifacts

        run_workspace = "/mnt/with space/cron/runs/run-x"

        empty = MagicMock()
        empty.logs = MagicMock(stdout=[])
        empty.stdout = ""

        sandbox = MagicMock()
        sandbox.commands = MagicMock()
        sandbox.commands.run = AsyncMock(return_value=empty)

        await _scan_run_artifacts(sandbox, run_workspace)

        assert sandbox.commands.run.await_count >= 1
        for call in sandbox.commands.run.await_args_list:
            cmd = call.args[0]
            # quoted 形式应出现，裸路径不应直接出现在命令里
            assert "'/mnt/with space/cron/runs/run-x'" in cmd
            assert " /mnt/with space/" not in cmd

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
        """当 -printf/stat 输出均为空时，仍可回退到基础 find 列表（size=0）。"""
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


class TestCronAgentConstruction:
    """Cron Agent 构造参数测试 — 强约束 fail-hard，不允许悄悄回退到 .env。"""

    @pytest.mark.asyncio
    async def test_registry_unavailable_fails_hard_and_skips_agent(self):
        """model registry 不可用时应 fail-hard，且不应创建 Agent。"""
        with (
            patch("src.api.model_registry.get_model_registry", side_effect=FileNotFoundError("no registry")),
            patch("src.api.models.database.SessionLocal") as mock_session_local,
            patch("src.api.services.sandbox_service.get_sandbox_service") as mock_svc,
            patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/mnt"),
            patch("src.api.services.tool_factory.create_agent_tools", new_callable=AsyncMock, return_value=([], None)),
            patch("src.api.services.cron_service._scan_run_artifacts", new_callable=AsyncMock, return_value=None),
            patch("src.agent.agent.Agent") as MockAgent,
        ):
            mock_db = MagicMock()
            mock_session_local.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session_local.return_value.__exit__ = MagicMock(return_value=False)

            mock_job = MagicMock()
            mock_job.name = "test-job"
            mock_job.description = "desc"
            mock_job.cron_expr = "0 * * * *"
            mock_job.enabled = True
            mock_run_record = MagicMock()
            mock_run_record.status = "running"
            mock_run_record.output = None
            mock_db.query.return_value.filter.return_value.first.side_effect = [
                mock_job,
                mock_run_record,
                MagicMock(sandbox_id="sb-1"),
                mock_run_record,
            ]

            mock_sandbox = AsyncMock()
            mock_sandbox.commands.run = AsyncMock(return_value=MagicMock(logs=MagicMock(stdout=[])))
            mock_svc.return_value.get_or_resume = AsyncMock(return_value=mock_sandbox)

            mock_agent_instance = MagicMock()

            async def _empty_gen(*a, **kw):
                return
                yield  # noqa: make it an async generator

            mock_agent_instance.run_agui = _empty_gen
            mock_agent_instance.add_user_message = MagicMock()
            MockAgent.return_value = mock_agent_instance

            from src.api.services.cron_service import run_cron_job

            result = await run_cron_job("user-1", "test-job", run_id="run-1")

            assert result is None
            MockAgent.assert_not_called()
            assert mock_run_record.status == "failed"
            assert mock_run_record.output is not None
            assert "Model Registry 不可用" in mock_run_record.output

    @pytest.mark.asyncio
    async def test_registry_agent_uses_model_config_values(self):
        """model registry 可用时，Agent 参数来自 model_config（不允许硬编码）。"""
        mock_model_config = MagicMock()
        mock_model_config.context_window = 200000
        mock_model_config.max_tokens = 32768
        mock_model_config.compute_token_limit.return_value = 164232

        mock_registry = MagicMock()
        mock_registry.get_cron_default.return_value = mock_model_config

        with (
            patch("src.api.model_registry.get_model_registry", return_value=mock_registry),
            patch("src.agent.llm.LLMClient") as MockLLM,
            patch("src.api.models.database.SessionLocal") as mock_session_local,
            patch("src.api.services.sandbox_service.get_sandbox_service") as mock_svc,
            patch("src.api.services.sandbox_service.get_sandbox_mount_path", return_value="/mnt"),
            patch("src.api.services.tool_factory.create_agent_tools", new_callable=AsyncMock, return_value=([], None)),
            patch("src.api.services.cron_service._scan_run_artifacts", new_callable=AsyncMock, return_value=None),
            patch("src.agent.agent.Agent") as MockAgent,
        ):
            MockLLM.from_model_config.return_value = MagicMock()

            mock_db = MagicMock()
            mock_session_local.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session_local.return_value.__exit__ = MagicMock(return_value=False)

            mock_job = MagicMock()
            mock_job.name = "test-job"
            mock_job.description = "desc"
            mock_job.cron_expr = "0 * * * *"
            mock_job.enabled = True
            mock_run_record = MagicMock()
            mock_db.query.return_value.filter.return_value.first.side_effect = [
                mock_job, None,
                MagicMock(sandbox_id="sb-1"),
                mock_run_record,
            ]

            mock_sandbox = AsyncMock()
            mock_sandbox.commands.run = AsyncMock(return_value=MagicMock(logs=MagicMock(stdout=[])))
            mock_svc.return_value.get_or_resume = AsyncMock(return_value=mock_sandbox)

            mock_agent_instance = MagicMock()

            async def _empty_gen(*a, **kw):
                return
                yield

            mock_agent_instance.run_agui = _empty_gen
            mock_agent_instance.add_user_message = MagicMock()
            MockAgent.return_value = mock_agent_instance

            from src.api.services.cron_service import run_cron_job

            await run_cron_job("user-1", "test-job")

            call_kwargs = MockAgent.call_args[1]
            assert call_kwargs["token_limit"] == 164232
            assert call_kwargs["context_window"] == 200000
            assert call_kwargs["max_output_tokens"] == 32768
