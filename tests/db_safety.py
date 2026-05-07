"""pytest 数据库安全约束。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url


SAFE_SQLITE_TEST_DATABASE_URL = "sqlite:///./data/database/open_capy_box_test.db"
_SAFE_PG_NAME_MARKERS = ("test", "pytest", "ci")


def load_dotenv_database_url(project_root: Path) -> str:
    env_path = project_root / ".env"
    if not env_path.exists():
        return ""
    return str(dotenv_values(env_path).get("DATABASE_URL") or "")


def load_dotenv_test_database_url(project_root: Path) -> str:
    env_path = project_root / ".env"
    if not env_path.exists():
        return ""
    return str(dotenv_values(env_path).get("TEST_DATABASE_URL") or "")


def _same_database(left_url: str, right_url: str) -> bool:
    if not left_url or not right_url:
        return False

    left = make_url(left_url)
    right = make_url(right_url)
    return (
        left.get_backend_name() == right.get_backend_name()
        and left.username == right.username
        and left.host == right.host
        and left.port == right.port
        and left.database == right.database
    )


def ensure_safe_test_database_url(test_url: str, production_url: str = "") -> None:
    if not test_url:
        raise RuntimeError("TEST_DATABASE_URL 不能为空")

    url = make_url(test_url)
    backend = url.get_backend_name()
    if backend == "sqlite":
        return

    if backend != "postgresql":
        raise RuntimeError(f"测试数据库仅允许 sqlite/postgresql，当前是: {backend}")

    if _same_database(test_url, production_url):
        raise RuntimeError("TEST_DATABASE_URL 指向了生产 DATABASE_URL，已阻止测试运行")

    database_name = (url.database or "").lower()
    if not any(marker in database_name for marker in _SAFE_PG_NAME_MARKERS):
        raise RuntimeError(
            "PostgreSQL 测试库名称必须包含 test/pytest/ci，"
            f"当前数据库名: {url.database}"
        )


def configure_pytest_database_urls(project_root: Path) -> str:
    production_url = os.environ.get("DATABASE_URL") or load_dotenv_database_url(project_root)
    test_url = os.environ.get("TEST_DATABASE_URL") or load_dotenv_test_database_url(project_root) or SAFE_SQLITE_TEST_DATABASE_URL

    ensure_safe_test_database_url(test_url, production_url)
    os.environ["TEST_DATABASE_URL"] = test_url
    os.environ["DATABASE_URL"] = test_url
    return test_url


def _ensure_test_engine_postgres_extensions(conn) -> None:
    from src.api.models.database import _ensure_postgres_vector_extension

    _ensure_postgres_vector_extension(conn)


def create_all_for_test_engine(engine, metadata) -> None:
    if engine.url.get_backend_name() == "postgresql":
        with engine.begin() as conn:
            _ensure_test_engine_postgres_extensions(conn)
    metadata.create_all(bind=engine)
