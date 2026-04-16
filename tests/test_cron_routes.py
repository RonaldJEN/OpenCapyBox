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
from unittest.mock import MagicMock, patch

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
        # user_id + status=success + is_read=false
        filter_args = mock_db.query.return_value.filter.call_args[0]
        assert len(filter_args) == 3


class TestMarkRead:
    """POST /cron/runs/mark-read"""

    def test_marks_all_unread(self):
        client, mock_db = _make_client()
        mock_db.query.return_value.filter.return_value.update.return_value = 5

        resp = client.post("/cron/runs/mark-read")
        assert resp.status_code == 200
        assert resp.json() == {"marked": 5}
        mock_db.commit.assert_called_once()
        # user_id + status=success + is_read=false
        filter_args = mock_db.query.return_value.filter.call_args[0]
        assert len(filter_args) == 3

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


class TestDownloadRunFile:
    """GET /cron/runs/{run_id}/files/{path} 安全校验"""

    def test_path_traversal_blocked(self):
        """路径穿越应返回 403"""
        client, mock_db = _make_client()
        run = _make_run_record()
        mock_db.query.return_value.filter.return_value.first.return_value = run

        # 使用 %2e%2e 避免 HTTP 客户端预处理路径
        resp = client.get("/cron/runs/run-1/files/sub/%2e%2e/%2e%2e/%2e%2e/etc/passwd")
        assert resp.status_code == 403

    def test_404_when_no_run(self):
        client, mock_db = _make_client()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        resp = client.get("/cron/runs/nonexistent/files/test.txt")
        assert resp.status_code == 404

    def test_404_when_no_workspace(self):
        client, mock_db = _make_client()
        run = _make_run_record(run_workspace=None)
        mock_db.query.return_value.filter.return_value.first.return_value = run

        resp = client.get("/cron/runs/run-1/files/test.txt")
        assert resp.status_code == 404
