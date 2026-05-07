from collections import namedtuple

from sqlalchemy import MetaData, create_engine, text

from pgloader import build_fk_filters, build_record, filter_orphan_rows, select_missing_key_rows
from src.api.utils.embedding_vector import MEMORY_EMBEDDING_DIMENSIONS, normalize_embedding_vector


def test_build_record_converts_memory_embedding_json_to_pgvector_literal():
    row_type = namedtuple("Row", ["id", "embedding"])
    row = row_type(1, "[0.1, -0.2, 0.3]")

    record = build_record(
        "memory_embeddings",
        row,
        ["id", "embedding"],
        {"id": 0, "embedding": 1},
    )

    vector = normalize_embedding_vector(record["embedding"])
    assert record["id"] == 1
    assert isinstance(record["embedding"], str)
    assert len(vector) == MEMORY_EMBEDDING_DIMENSIONS
    assert vector[:3] == [0.1, -0.2, 0.3]
    assert vector[3:] == [0.0] * (MEMORY_EMBEDDING_DIMENSIONS - 3)


def test_build_record_keeps_other_tables_unchanged():
    row_type = namedtuple("Row", ["id", "embedding"])
    row = row_type(1, "[0.1]")

    record = build_record(
        "user_memory",
        row,
        ["id", "embedding"],
        {"id": 0, "embedding": 1},
    )

    assert record == {"id": 1, "embedding": "[0.1]"}


def test_filter_orphan_rows_rejects_children_when_parent_set_empty():
    row_type = namedtuple("Row", ["id", "job_id"])
    rows = [row_type("fire-1", 1), row_type("fire-2", None)]

    filtered_rows, skipped = filter_orphan_rows(
        rows,
        ["id", "job_id"],
        {"job_id": set()},
    )

    assert filtered_rows == [row_type("fire-2", None)]
    assert skipped == 1


def test_build_fk_filters_rejects_rounds_with_missing_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sessions (id TEXT PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE rounds (id TEXT PRIMARY KEY, session_id TEXT, thread_id TEXT)"))
        conn.execute(text("INSERT INTO sessions (id) VALUES ('session-ok')"))

    metadata = MetaData()
    metadata.reflect(bind=engine)
    fk_filters = build_fk_filters(engine, metadata)

    row_type = namedtuple("Row", ["id", "session_id", "thread_id"])
    rows = [
        row_type("round-ok", "session-ok", "session-ok"),
        row_type("round-missing-session", "missing", "session-ok"),
        row_type("round-missing-thread", "session-ok", "missing"),
    ]

    filtered_rows, skipped = filter_orphan_rows(
        rows,
        ["id", "session_id", "thread_id"],
        fk_filters["rounds"],
    )

    assert filtered_rows == [row_type("round-ok", "session-ok", "session-ok")]
    assert skipped == 2


def test_select_missing_key_rows_detects_same_count_different_keys():
    row_type = namedtuple("Row", ["id", "content"])
    rows = [row_type(1, "kept"), row_type(2, "missing")]

    missing_rows, source_keys, missing_keys = select_missing_key_rows(
        rows,
        ["id", "content"],
        ["id"],
        {(1,), (3,)},
    )

    assert missing_rows == [row_type(2, "missing")]
    assert source_keys == {(1,), (2,)}
    assert missing_keys == {(2,)}
