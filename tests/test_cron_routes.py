"""Cron API 路由测试

覆盖：
- GET /api/cron/runs 分页 + 新字段
- GET /api/cron/runs/unread-count
- POST /api/cron/runs/mark-read
- GET /api/cron/runs/{run_id} 补充字段
- GET /api/cron/runs/{run_id}/files 列出产物
- GET /api/cron/runs/{run_id}/files/{path} 路径安全校验
"""
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from tests.helpers import make_test_client


def _make_client():
    """创建挂载 cron 路由的测试客户端"""
    from src.api.routes.cron import router
    mock_db = MagicMock()
    client = make_test_client(router, "/cron", db=mock_db)
    return client, mock_db


def _make_run_record(
    *,
    id="run-1",
    user_id="testuser",
    job_name="daily",
    cron_expr="0 9 * * *",
    status="success",
    output="ok",
    is_read=False,
    artifacts=None,
    run_workspace="/home/user/cron/runs/run-1",
    started_at=None,
    completed_at=None,
):
    run = MagicMock()
    run.id = id
    run.user_id = user_id
    run.job_name = job_name
    run.cron_expr = cron_expr
    run.status = status
    run.output = output
    run.is_read = is_read
    run.artifacts = json.dumps(artifacts) if artifacts else None
    run.run_workspace = run_workspace
    run.started_at = started_at
    run.completed_at = completed_at
    # to_dict() 与 CronJobRun.to_dict() 保持一致
    run.to_dict.return_value = {
        "id": id,
        "job_name": job_name,
        "cron_expr": cron_expr,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "status": status,
        "output": output,
        "is_read": bool(is_read),
        "artifacts": artifacts,
        "run_workspace": run_workspace,
    }
    return run


class TestGetRunHistory:
    """GET /cron/runs 分页"""

    def test_returns_runs_with_pagination(self):
        client, mock_db = _make_client()

        svc_mock = MagicMock()
        svc_mock.get_run_history.return_value = (
            [{"id": "r1", "job_name": "daily", "status": "success",
              "is_read": True, "artifacts": None}],
            5,
        )

        with patch("src.api.routes.cron.CronService", return_value=svc_mock):
            resp = client.get("/cron/runs", params={"limit": 10, "offset": 0})

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["offset"] == 0
        assert data["limit"] == 10
        assert len(data["runs"]) == 1


class TestUnreadCount:
    """GET /cron/runs/unread-count"""

    def test_returns_count(self):
        client, mock_db = _make_client()
        mock_db.query.return_value.filter.return_value.count.return_value = 3

        resp = client.get("/cron/runs/unread-count")
        assert resp.status_code == 200
        assert resp.json() == {"count": 3}
        # user_id + is_read=false（失败记录也需要计入未读）
        filter_args = mock_db.query.return_value.filter.call_args[0]
        assert len(filter_args) == 2


class TestMarkRead:
    """POST /cron/runs/mark-read"""

    def test_marks_all_unread(self):
        client, mock_db = _make_client()
        mock_db.query.return_value.filter.return_value.update.return_value = 5

        resp = client.post("/cron/runs/mark-read")
        assert resp.status_code == 200
        assert resp.json() == {"marked": 5}
        mock_db.commit.assert_called_once()
        # user_id + is_read=false（全量标记不区分 status）
        filter_args = mock_db.query.return_value.filter.call_args[0]
        assert len(filter_args) == 2

    def test_marks_specific_run(self):
        client, mock_db = _make_client()
        mock_db.query.return_value.filter.return_value.filter.return_value.update.return_value = 1

        resp = client.post("/cron/runs/mark-read", params={"run_id": "run-1"})
        assert resp.status_code == 200
        assert resp.json() == {"marked": 1}
        mock_db.commit.assert_called_once()


class TestGetRunStatus:
    """GET /cron/runs/{run_id} 补充字段"""

    def test_returns_new_fields(self):
        client, mock_db = _make_client()
        run = _make_run_record(
            artifacts=[{"name": "report.md", "path": "report.md", "size": 100, "type": "md"}],
            is_read=True,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = run

        resp = client.get("/cron/runs/run-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_read"] is True
        assert data["artifacts"] is not None
        assert data["artifacts"][0]["name"] == "report.md"
        assert data["run_workspace"] == "/home/user/cron/runs/run-1"

    def test_404_when_not_found(self):
        client, mock_db = _make_client()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        resp = client.get("/cron/runs/nonexistent")
        assert resp.status_code == 404


class TestListRunFiles:
    """GET /cron/runs/{run_id}/files"""

    def test_returns_artifacts_from_db(self):
        client, mock_db = _make_client()
        artifacts = [{"name": "data.csv", "path": "data.csv", "size": 200, "type": "csv"}]
        run = _make_run_record(artifacts=artifacts)
        mock_db.query.return_value.filter.return_value.first.return_value = run

        resp = client.get("/cron/runs/run-1/files")
        assert resp.status_code == 200
        files = resp.json()["files"]
        assert len(files) == 1
        assert files[0]["name"] == "data.csv"

    def test_empty_when_no_workspace(self):
        client, mock_db = _make_client()
        run = _make_run_record(artifacts=None, run_workspace=None)
        mock_db.query.return_value.filter.return_value.first.return_value = run

        resp = client.get("/cron/runs/run-1/files")
        assert resp.status_code == 200
        assert resp.json()["files"] == []

    def test_scan_uses_persisted_sandbox_id(self):
        client, mock_db = _make_client()
        run = _make_run_record(artifacts=None, run_workspace="/home/user/cron/runs/run-1")
        user_sandbox = MagicMock()
        user_sandbox.sandbox_id = "sandbox-1"
        mock_db.query.return_value.filter.return_value.first.side_effect = [run, user_sandbox]

        sandbox = MagicMock()
        sandbox_service = MagicMock()
        sandbox_service.get_or_resume = AsyncMock(return_value=sandbox)
        sandbox_service.get_sandbox_id.return_value = "sandbox-1"

        artifacts_json = json.dumps([
            {"name": "cached.md", "path": "cached.md", "size": 123, "type": "md"},
        ])

        with patch("src.api.services.sandbox_service.get_sandbox_service", return_value=sandbox_service), patch(
            "src.api.services.cron_service._scan_run_artifacts",
            new_callable=AsyncMock,
            return_value=artifacts_json,
        ):
            resp = client.get("/cron/runs/run-1/files")

        assert resp.status_code == 200
        assert resp.json()["files"][0]["name"] == "cached.md"
        sandbox_service.get_or_resume.assert_awaited_once_with("testuser", "sandbox-1")


class TestDownloadRunFile:
    """GET /cron/runs/{run_id}/files/{path} 安全校验"""

    def test_path_traversal_blocked(self):
        """路径穿越应返回 403"""
        client, mock_db = _make_client()
        run = _make_run_record()
        mock_db.query.return_value.filter.return_value.first.return_value = run

        # 使用 %2e%2e 避免 HTTP 客户端预处理路径
        with patch("src.api.routes.cron.verify_access_token", return_value="testuser"):
            resp = client.get(
                "/cron/runs/run-1/files/sub/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
                params={"token": "fake"},
            )
        assert resp.status_code == 403

    def test_404_when_no_run(self):
        client, mock_db = _make_client()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with patch("src.api.routes.cron.verify_access_token", return_value="testuser"):
            resp = client.get(
                "/cron/runs/nonexistent/files/test.txt",
                params={"token": "fake"},
            )
        assert resp.status_code == 404

    def test_404_when_no_workspace(self):
        client, mock_db = _make_client()
        run = _make_run_record(run_workspace=None)
        mock_db.query.return_value.filter.return_value.first.return_value = run

        with patch("src.api.routes.cron.verify_access_token", return_value="testuser"):
            resp = client.get(
                "/cron/runs/run-1/files/test.txt",
                params={"token": "fake"},
            )
        assert resp.status_code == 404

    def test_401_when_no_token(self):
        """未提供 token 必须 401。"""
        client, _mock_db = _make_client()
        resp = client.get("/cron/runs/run-1/files/test.txt")
        assert resp.status_code == 401

    def test_download_uses_persisted_sandbox_id(self):
        client, mock_db = _make_client()
        run = _make_run_record()
        user_sandbox = MagicMock()
        user_sandbox.sandbox_id = "sandbox-1"
        mock_db.query.return_value.filter.return_value.first.side_effect = [run, user_sandbox]

        sandbox = MagicMock()
        sandbox.files.read_bytes = AsyncMock(return_value=b"hello")

        sandbox_service = MagicMock()
        sandbox_service.get_or_resume = AsyncMock(return_value=sandbox)
        sandbox_service.get_sandbox_id.return_value = "sandbox-1"

        with patch("src.api.routes.cron.verify_access_token", return_value="testuser"), patch(
            "src.api.services.sandbox_service.get_sandbox_service",
            return_value=sandbox_service,
        ):
            resp = client.get("/cron/runs/run-1/files/test.txt", params={"token": "fake"})

        assert resp.status_code == 200
        assert resp.content == b"hello"
        sandbox_service.get_or_resume.assert_awaited_once_with("testuser", "sandbox-1")
        sandbox.files.read_bytes.assert_awaited_once()


class TestTriggerJob:
    """POST /cron/jobs/{job_name}/run"""

    def test_trigger_uses_worker_background_spawn(self):
        client, mock_db = _make_client()

        mock_job = MagicMock()
        mock_job.user_id = "testuser"
        mock_job.id = 17
        mock_job.name = "daily"
        mock_job.cron_expr = "0 9 * * *"
        mock_job.rule_version = 4
        mock_db.query.return_value.filter.return_value.first.return_value = mock_job

        # 模拟预创建 run 记录提交后，另一个事务立即修改同名任务。
        # 路由后续必须继续使用 commit 前捕获的提交快照。
        def mutate_job_after_commit():
            mock_job.id = 99
            mock_job.cron_expr = "0 18 * * *"
            mock_job.rule_version = 5

        mock_db.commit.side_effect = mutate_job_after_commit
        client.app.state.cron_worker_id = "worker-test"

        with patch(
            "src.api.routes.cron.trigger_manual_run",
            new_callable=AsyncMock,
        ) as mock_trigger:
            resp = client.post("/cron/jobs/daily/run")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["job_name"] == "daily"
        assert "run_id" in body

        mock_trigger.assert_awaited_once()
        call_args, call_kwargs = mock_trigger.call_args
        # 回归：路由必须只通过公开 API 与 worker 交互，
        # 仅传纯数据（user_id / job_name / run_id），不传 ORM 实例。
        assert call_args[0] is client.app
        assert call_args[1] == "testuser"
        assert call_args[2] == "daily"
        assert call_args[3] == body["run_id"]
        assert call_args[4] == 17
        assert call_args[5] == 4
        run_record = mock_db.add.call_args.args[0]
        assert run_record.cron_expr == "0 9 * * *"
        assert run_record.rule_version == 4

    def test_marks_run_failed_when_worker_unavailable(self):
        """worker 未启动时，trigger_manual_run 抛 503 → run 记录必须被标记为 failed。

        否则已落库的 running 记录要等 startup 1 小时清理才会变 failed，
        前端会一直转圈。
        """
        from fastapi import HTTPException

        client, mock_db = _make_client()

        mock_job = MagicMock()
        mock_job.user_id = "testuser"
        mock_job.name = "daily"
        mock_job.cron_expr = "0 9 * * *"

        mock_run_record = MagicMock()
        mock_run_record.status = "running"

        # 路由内查询顺序：CronJob → CronJobRun（失败兜底路径）
        mock_db.query.return_value.filter.return_value.first.side_effect = [
            mock_job,
            mock_run_record,
        ]

        with patch(
            "src.api.routes.cron.trigger_manual_run",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=503, detail="cron worker 未启动"),
        ):
            resp = client.post("/cron/jobs/daily/run")

        assert resp.status_code == 503
        assert mock_run_record.status == "failed"
        assert "cron worker" in (mock_run_record.output or "")
        assert mock_run_record.completed_at is not None

    def test_marks_run_failed_when_trigger_raises_runtime_error(self):
        """非 HTTPException（如 RuntimeError）也必须收拢 running 记录为 failed。

        spec：trigger_manual_run 抛出"任意异常"时都要兜底，
        否则失败信号被 500 吞掉，run 记录残留 running → 前端转圈。
        """
        client, mock_db = _make_client()

        mock_job = MagicMock()
        mock_job.user_id = "testuser"
        mock_job.name = "daily"
        mock_job.cron_expr = "0 9 * * *"

        mock_run_record = MagicMock()
        mock_run_record.status = "running"

        mock_db.query.return_value.filter.return_value.first.side_effect = [
            mock_job,
            mock_run_record,
        ]

        with patch(
            "src.api.routes.cron.trigger_manual_run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("event loop is closed"),
        ):
            with pytest.raises(RuntimeError, match="event loop"):
                client.post("/cron/jobs/daily/run")

        # 即使异常向上抛，路由也必须先把 run 记录收拢为 failed
        assert mock_run_record.status == "failed"
        assert "RuntimeError" in (mock_run_record.output or "")
        assert "event loop" in (mock_run_record.output or "")
        assert mock_run_record.completed_at is not None


# ════════════════════════════════════════════════════════════════
# CRUD 路由（POST/PUT/DELETE/preview）—— 给前端表单调用
# ════════════════════════════════════════════════════════════════


def _job_with_to_dict(**fields):
    """构造一个带 to_dict() 的 mock CronJob ORM 对象。"""
    job = MagicMock()
    defaults = {
        "id": 1,
        "name": "daily",
        "cron_expr": "30 9 * * *",
        "schedule": {"kind": "daily", "time": "09:30"},
        "description": "晨报",
        "content": "",
        "enabled": True,
    }
    defaults.update(fields)
    for k, v in defaults.items():
        setattr(job, k, v)
    job.to_dict.return_value = defaults
    return job


class TestCreateCronJob:
    def test_201_with_schedule(self):
        client, _ = _make_client()
        svc = MagicMock()
        svc.create_job.return_value = _job_with_to_dict()
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.post("/cron/jobs", json={
                "name": "daily",
                "description": "晨报",
                "content": "汇总今天日程",
                "schedule": {"kind": "daily", "time": "09:30"},
                "enabled": True,
            })
        assert resp.status_code == 201
        body = resp.json()
        assert body["job"]["name"] == "daily"
        assert set(body["job"].keys()) == {
            "id", "name", "cron_expr", "schedule", "description", "content", "enabled",
            "rule_version",
        }
        assert "user_id" not in body["job"]
        assert "created_at" not in body["job"]
        assert "updated_at" not in body["job"]
        # 服务层收到 schedule 而非 cron_expr
        kwargs = svc.create_job.call_args.kwargs
        assert kwargs["schedule"] == {"kind": "daily", "time": "09:30"}
        assert kwargs["cron_expr"] is None
        assert kwargs["content"] == "汇总今天日程"

    def test_201_with_cron_expr(self):
        client, _ = _make_client()
        svc = MagicMock()
        svc.create_job.return_value = _job_with_to_dict(schedule=None, cron_expr="0 8 * * *")
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.post("/cron/jobs", json={
                "name": "morning",
                "cron_expr": "0 8 * * *",
            })
        assert resp.status_code == 201

    def test_400_when_validation_error(self):
        from src.api.services.cron_service import CronJobValidationError
        client, _ = _make_client()
        svc = MagicMock()
        svc.create_job.side_effect = CronJobValidationError("任务名非法")
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.post("/cron/jobs", json={
                "name": "bad name!",
                "schedule": {"kind": "daily", "time": "09:30"},
            })
        assert resp.status_code == 400
        assert "任务名" in resp.json()["detail"]

    def test_400_for_never_firing_cron_without_persisting(self):
        client, mock_db = _make_client()

        resp = client.post("/cron/jobs", json={
            "name": "never",
            "cron_expr": "0 0 31 2 *",
        })

        assert resp.status_code == 400
        assert "无法产生未来执行时间" in resp.json()["detail"]
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_503_when_db_busy(self):
        from src.api.services.cron_service import CronJobBusyError
        client, _ = _make_client()
        svc = MagicMock()
        svc.create_job.side_effect = CronJobBusyError("数据库繁忙，请稍后重试")
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.post("/cron/jobs", json={
                "name": "daily",
                "schedule": {"kind": "daily", "time": "09:30"},
            })
        assert resp.status_code == 503
        assert "数据库繁忙" in resp.json()["detail"]

    def test_422_when_name_missing(self):
        client, _ = _make_client()
        # Pydantic 校验：name 缺失
        resp = client.post("/cron/jobs", json={"schedule": {"kind": "daily", "time": "09:30"}})
        assert resp.status_code == 422

    def test_201_when_content_len_is_8000(self):
        client, _ = _make_client()
        svc = MagicMock()
        svc.create_job.return_value = _job_with_to_dict(content="x" * 8000)
        long_content = "x" * 8000
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.post("/cron/jobs", json={
                "name": "daily",
                "content": long_content,
                "schedule": {"kind": "daily", "time": "09:30"},
            })
        assert resp.status_code == 201
        kwargs = svc.create_job.call_args.kwargs
        assert len(kwargs["content"]) == 8000

    def test_422_when_content_len_exceeds_8000(self):
        client, _ = _make_client()
        resp = client.post("/cron/jobs", json={
            "name": "daily",
            "content": "x" * 8001,
            "schedule": {"kind": "daily", "time": "09:30"},
        })
        assert resp.status_code == 422


class TestUpdateCronJob:
    def test_200_partial_update(self):
        client, _ = _make_client()
        svc = MagicMock()
        svc.update_job.return_value = _job_with_to_dict(enabled=False)
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.put("/cron/jobs/daily", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["job"]["enabled"] is False
        kwargs = svc.update_job.call_args.kwargs
        # 未传字段必须是 None（表示"不变"）
        assert kwargs["enabled"] is False
        assert kwargs["description"] is None
        assert kwargs["schedule"] is None

    def test_404_when_not_found(self):
        from src.api.services.cron_service import CronJobNotFoundError
        client, _ = _make_client()
        svc = MagicMock()
        svc.update_job.side_effect = CronJobNotFoundError("任务 'x' 不存在")
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.put("/cron/jobs/x", json={"enabled": False})
        assert resp.status_code == 404

    def test_400_when_other_validation(self):
        from src.api.services.cron_service import CronJobValidationError
        client, _ = _make_client()
        svc = MagicMock()
        svc.update_job.side_effect = CronJobValidationError("schedule 格式错误")
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.put("/cron/jobs/daily", json={
                "schedule": {"kind": "daily", "time": "bad"},
            })
        assert resp.status_code == 400

    def test_503_when_db_busy(self):
        from src.api.services.cron_service import CronJobBusyError
        client, _ = _make_client()
        svc = MagicMock()
        svc.update_job.side_effect = CronJobBusyError("数据库繁忙，请稍后重试")
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.put("/cron/jobs/daily", json={"enabled": False})
        assert resp.status_code == 503
        assert "数据库繁忙" in resp.json()["detail"]

    def test_422_when_update_content_len_exceeds_8000(self):
        client, _ = _make_client()
        resp = client.put("/cron/jobs/daily", json={"content": "x" * 8001})
        assert resp.status_code == 422


class TestDeleteCronJob:
    def test_204_on_success(self):
        client, _ = _make_client()
        svc = MagicMock()
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.delete("/cron/jobs/daily")
        assert resp.status_code == 204
        assert resp.content == b""
        svc.delete_job.assert_called_once_with("testuser", "daily")

    def test_404_when_not_found(self):
        from src.api.services.cron_service import CronJobNotFoundError
        client, _ = _make_client()
        svc = MagicMock()
        svc.delete_job.side_effect = CronJobNotFoundError("任务 'x' 不存在")
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.delete("/cron/jobs/x")
        assert resp.status_code == 404

    def test_503_when_db_busy(self):
        from src.api.services.cron_service import CronJobBusyError
        client, _ = _make_client()
        svc = MagicMock()
        svc.delete_job.side_effect = CronJobBusyError("数据库繁忙，请稍后重试")
        with patch("src.api.routes.cron.CronService", return_value=svc):
            resp = client.delete("/cron/jobs/daily")
        assert resp.status_code == 503
        assert "数据库繁忙" in resp.json()["detail"]


class TestSchedulePreview:
    def test_preview_with_schedule(self):
        client, _ = _make_client()
        resp = client.post("/cron/jobs/preview", json={
            "schedule": {"kind": "daily", "time": "09:00"},
            "n": 3,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["cron_expr"] == "0 9 * * *"
        assert len(data["next_fires"]) == 3
        # ISO 字符串格式
        assert "T" in data["next_fires"][0]

    def test_preview_with_cron_expr(self):
        client, _ = _make_client()
        resp = client.post("/cron/jobs/preview", json={
            "cron_expr": "0 8 * * *",
            "n": 2,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["cron_expr"] == "0 8 * * *"
        assert len(data["next_fires"]) == 2

    def test_400_when_neither_provided(self):
        client, _ = _make_client()
        resp = client.post("/cron/jobs/preview", json={"n": 5})
        assert resp.status_code == 400

    def test_400_when_both_schedule_and_cron_expr_provided(self):
        client, _ = _make_client()
        resp = client.post("/cron/jobs/preview", json={
            "schedule": {"kind": "daily", "time": "09:00"},
            "cron_expr": "0 8 * * *",
            "n": 5,
        })
        assert resp.status_code == 400
        assert "不能同时" in resp.json()["detail"]

    def test_400_when_schedule_invalid(self):
        client, _ = _make_client()
        resp = client.post("/cron/jobs/preview", json={
            "schedule": {"kind": "daily", "time": "25:99"},
        })
        assert resp.status_code == 400

    def test_400_when_cron_expr_invalid(self):
        client, _ = _make_client()
        resp = client.post("/cron/jobs/preview", json={"cron_expr": "* * *"})
        assert resp.status_code == 400
