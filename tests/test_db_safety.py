import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests import db_safety
from tests.db_safety import (
    configure_pytest_database_urls,
    create_all_for_test_engine,
    ensure_safe_test_database_url,
)


def test_rejects_non_postgres_test_database():
    with pytest.raises(RuntimeError, match="postgresql"):
        ensure_safe_test_database_url(
            "sqlite:///./data/database/open_capy_box_test.db",
            "postgresql://app@host/prod",
        )


def test_rejects_same_postgres_database_as_production():
    url = "postgresql://app:secret@db.example.com:5432/bsbox"

    with pytest.raises(RuntimeError, match="生产 DATABASE_URL"):
        ensure_safe_test_database_url(url, url)


def test_rejects_postgres_database_without_test_marker():
    with pytest.raises(RuntimeError, match="test/pytest/ci"):
        ensure_safe_test_database_url(
            "postgresql://app:secret@db.example.com:5432/bsbox_shadow",
            "postgresql://app:secret@db.example.com:5432/bsbox",
        )


def test_allows_distinct_postgres_test_database():
    ensure_safe_test_database_url(
        "postgresql://app:secret@db.example.com:5432/bsbox_test",
        "postgresql://app:secret@db.example.com:5432/bsbox",
    )


def test_configure_pytest_database_urls_requires_test_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://app:secret@db.example.com:5432/bsbox\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError):
        configure_pytest_database_urls(Path(tmp_path))


def test_configure_pytest_database_urls_uses_dotenv_test_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    test_url = "postgresql://app:secret@db.example.com:5432/bsbox_test"
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://app:secret@db.example.com:5432/bsbox\n"
        f"TEST_DATABASE_URL={test_url}\n",
        encoding="utf-8",
    )

    configured = configure_pytest_database_urls(Path(tmp_path))

    assert configured == test_url
    assert os.environ["DATABASE_URL"] == test_url
    assert os.environ["TEST_DATABASE_URL"] == test_url


def test_create_all_for_postgres_test_engine_installs_extensions(monkeypatch):
    calls = []
    connection = object()
    engine = MagicMock()
    engine.url.get_backend_name.return_value = "postgresql"
    engine.begin.return_value.__enter__.return_value = connection
    metadata = MagicMock()

    monkeypatch.setattr(
        db_safety,
        "_ensure_test_engine_postgres_extensions",
        lambda conn: calls.append(conn),
    )

    create_all_for_test_engine(engine, metadata)

    assert calls == [connection]
    metadata.create_all.assert_called_once_with(bind=engine)
