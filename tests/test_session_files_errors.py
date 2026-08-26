from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.routes import sessions


def _owned_session_db():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock(
        id="s1",
        user_id="u1",
    )
    return db


@pytest.mark.asyncio
async def test_session_file_list_reconnect_failure_is_not_an_empty_directory():
    db = _owned_session_db()
    sandbox_service = MagicMock()
    sandbox_service.get_mount_path.return_value = "/home/user"
    sandbox = MagicMock()
    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=sandbox_service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)) as ensure,
        patch(
            "src.api.routes.sessions._sandbox_list_dir",
            new=AsyncMock(side_effect=[RuntimeError("bad response"), RuntimeError("still bad")]),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await sessions.get_session_files(
                chat_session_id="s1",
                path="",
                user_id="u1",
                db=db,
            )

    assert exc.value.status_code == 503
    assert exc.value.detail == "无法读取会话文件"
    assert ensure.await_count == 2


@pytest.mark.asyncio
async def test_session_file_list_connection_failure_is_not_an_empty_directory():
    db = _owned_session_db()
    sandbox_service = MagicMock()
    sandbox_service.get_mount_path.return_value = "/home/user"
    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=sandbox_service),
        patch(
            "src.api.routes.sessions._ensure_sandbox",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await sessions.get_session_files(
                chat_session_id="s1",
                path="",
                user_id="u1",
                db=db,
            )

    assert exc.value.status_code == 503
    assert exc.value.detail == "沙箱不可用"


@pytest.mark.asyncio
async def test_successfully_read_empty_session_directory_remains_empty():
    db = _owned_session_db()
    sandbox_service = MagicMock()
    sandbox_service.get_mount_path.return_value = "/home/user"
    sandbox = MagicMock()
    with (
        patch("src.api.routes.sessions.get_sandbox_service", return_value=sandbox_service),
        patch("src.api.routes.sessions._ensure_sandbox", new=AsyncMock(return_value=sandbox)),
        patch("src.api.routes.sessions._sandbox_list_dir", new=AsyncMock(return_value=[])),
    ):
        response = await sessions.get_session_files(
            chat_session_id="s1",
            path="",
            user_id="u1",
            db=db,
        )

    assert response.files == []
    assert response.total == 0
