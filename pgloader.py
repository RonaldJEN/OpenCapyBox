"""SQLite → PostgreSQL 数据迁移脚本

用法:
    uv run python pgloader.py

从环境变量或 .env 读取:
    - SQLITE_SOURCE: SQLite 源文件路径（默认 ./data/database/open_capy_box.db）
    - DATABASE_URL: 目标 PostgreSQL 连接字符串

幂等：目标已包含源端数据时跳过；若目标有运行期新增数据，会保留新增数据。
"""

import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

import os
from sqlalchemy import create_engine, text, inspect, MetaData

from src.api.utils.embedding_vector import serialize_pgvector


SERIAL_TABLES = [
    "agui_events",
    "conversation_messages",
    "llm_call_records",
    "user_memory",
    "memory_embeddings",
    "cron_jobs",
    "user_skill_configs",
]

# 表迁移顺序：先无外键，再有外键
TABLE_ORDER = [
    "sessions",
    "rounds",
    "agui_events",
    "conversation_messages",
    "llm_call_records",
    "user_run_locks",
    "run_cancel_requests",
    "user_memory",
    "memory_embeddings",
    "cron_jobs",
    "cron_job_runs",
    "cron_fires",
    "user_sandboxes",
    "user_skill_configs",
]


def build_fk_filters(src_engine, src_meta: MetaData) -> dict[str, dict[str, set]]:
    with src_engine.connect() as conn:
        round_ids = {r[0] for r in conn.execute(text("SELECT id FROM rounds")).fetchall()}
        session_ids = {r[0] for r in conn.execute(text("SELECT id FROM sessions")).fetchall()}
        cron_job_ids = {r[0] for r in conn.execute(text("SELECT id FROM cron_jobs")).fetchall()} if "cron_jobs" in src_meta.tables else set()

    return {
        "rounds": {"session_id": session_ids, "thread_id": session_ids},
        "agui_events": {"run_id": round_ids},
        "conversation_messages": {"session_id": session_ids},
        "llm_call_records": {"session_id": session_ids, "round_id": round_ids},
        "cron_fires": {"job_id": cron_job_ids},
    }


def migrate():
    sqlite_path = os.environ.get("SQLITE_SOURCE", "./data/database/open_capy_box.db")
    pg_url = os.environ.get("DATABASE_URL", "")
    batch_size = int(os.environ.get("PGLOADER_BATCH_SIZE", "500"))
    workers = int(os.environ.get("PGLOADER_WORKERS", "12"))

    if not pg_url.startswith("postgresql"):
        print(f"❌ DATABASE_URL 必须是 PostgreSQL 连接串，当前值: {pg_url[:30]}...")
        sys.exit(1)

    if not Path(sqlite_path).exists():
        print(f"❌ SQLite 源文件不存在: {sqlite_path}")
        sys.exit(1)

    print(f"📂 源: {sqlite_path}")
    print(f"🐘 目标: {pg_url.split('@')[1] if '@' in pg_url else pg_url[:40]}...")
    print(f"⚙️  batch_size={batch_size}, workers={workers}")
    print(flush=True)

    # 连接 SQLite 源
    src_engine = create_engine(f"sqlite:///{sqlite_path}")
    src_meta = MetaData()
    src_meta.reflect(bind=src_engine)

    # 连接 PostgreSQL 目标
    dst_engine = create_engine(
        pg_url,
        pool_pre_ping=True,
        pool_size=max(5, workers),
        max_overflow=max(10, workers),
    )
    dst_inspector = inspect(dst_engine)

    # 预加载有效的 FK 父表 ID 集合（用于过滤孤儿行）
    # SQLite 默认不强制 FK，所以源数据可能有孤儿引用
    # 从源 SQLite 读取（父表数据一定会被迁移）
    _FK_FILTERS = build_fk_filters(src_engine, src_meta)

    _PRIMARY_KEYS = {
        "sessions": "id",
        "rounds": "id",
        "conversation_messages": "id",
        "llm_call_records": "id",
        "user_run_locks": "user_id",
        "run_cancel_requests": "session_id",
        "user_memory": "id",
        "memory_embeddings": "id",
        "cron_jobs": "id",
        "cron_job_runs": "id",
        "cron_fires": "id",
        "user_sandboxes": "id",
        "user_skill_configs": "id",
    }

    total_migrated = 0

    try:
        for table_name in TABLE_ORDER:
            if table_name not in src_meta.tables:
                print(f"  ⏭️  {table_name} — 源表不存在，跳过", flush=True)
                continue

            if not dst_inspector.has_table(table_name):
                print(f"  ⏭️  {table_name} — 目标表不存在，请先运行 init_db.py", flush=True)
                continue

            # 从源读取所有行
            src_table = src_meta.tables[table_name]
            with src_engine.connect() as conn:
                rows = conn.execute(src_table.select()).fetchall()

            if not rows:
                print(f"  ⏭️  {table_name} — 源表无数据，跳过", flush=True)
                continue

            # 获取列名
            columns = [col.name for col in src_table.columns]

            # 过滤孤儿行（FK 引用不存在的父行）
            fk_filter = _FK_FILTERS.get(table_name, {})
            if fk_filter:
                rows, skipped = filter_orphan_rows(rows, columns, fk_filter)
            else:
                skipped = 0

            if not rows and skipped:
                print(f"  ⚠️  {table_name} — 全部 {skipped} 行为孤儿，跳过", flush=True)
                continue

            insert_columns = columns

            # 检查目标表是否已有数据；目标可能已经有迁移后新写入的数据。
            with dst_engine.connect() as conn:
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
            if count > 0:
                if table_name == "agui_events":
                    with dst_engine.connect() as conn:
                        target_keys = {
                            (r[0], r[1])
                            for r in conn.execute(text("SELECT run_id, sequence FROM agui_events")).fetchall()
                        }
                    missing_rows, source_keys, missing_keys = select_missing_key_rows(
                        rows, columns, ["run_id", "sequence"], target_keys
                    )
                    if not missing_keys:
                        extra_count = count - len(source_keys)
                        print(
                            f"  ⏭️  {table_name} — 目标已包含源端 {len(source_keys)} 个事件"
                            f"（额外 {extra_count} 个运行期新事件），跳过",
                            flush=True,
                        )
                        continue
                    rows = missing_rows
                    insert_columns = [col for col in columns if col != "id"]
                    print(
                        f"  🔁 {table_name} — 补齐 {len(rows)} 个缺失历史事件（使用 PG 新 id）",
                        flush=True,
                    )
                else:
                    pk = _PRIMARY_KEYS.get(table_name)
                    if pk:
                        with dst_engine.connect() as conn:
                            target_keys = {
                                (r[0],)
                                for r in conn.execute(text(f"SELECT {pk} FROM {table_name}")).fetchall()
                            }
                        missing_rows, source_keys, missing_keys = select_missing_key_rows(
                            rows, columns, [pk], target_keys
                        )
                        if not missing_keys:
                            extra_count = count - len(source_keys)
                            print(
                                f"  ⏭️  {table_name} — 目标已包含源端 {len(source_keys)} 行"
                                f"（额外 {extra_count} 行运行期新数据），跳过",
                                flush=True,
                            )
                            continue
                        rows = missing_rows
                        print(
                            f"  🔁 {table_name} — 补齐 {len(rows)} 行缺失历史数据",
                            flush=True,
                        )
                    elif count == len(rows):
                        print(f"  ⏭️  {table_name} — 目标已有 {count} 行，跳过", flush=True)
                        continue
                    else:
                        raise RuntimeError(
                            f"{table_name} 目标已有 {count} 行，但源端可迁移 {len(rows)} 行；"
                            "疑似半迁移状态，请先 TRUNCATE 相关表后重新迁移。"
                        )

            # 批量插入到目标
            insert_sql = text(
                f"INSERT INTO {table_name} ({', '.join(insert_columns)}) "
                f"VALUES ({', '.join(':' + c for c in insert_columns)})"
            )
            column_indices = {col: idx for idx, col in enumerate(columns)}
            batches = []
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                batches.append([
                    build_record(table_name, row, insert_columns, column_indices)
                    for row in batch
                ])

            inserted = insert_batches(dst_engine, insert_sql, batches, table_name, workers)

            if skipped:
                print(f"  ⚠️  {table_name} — 迁移 {inserted} 行，跳过 {skipped} 行（FK 孤儿）", flush=True)
            else:
                print(f"  ✅ {table_name} — 迁移 {inserted} 行", flush=True)
            total_migrated += inserted

        print(f"\n🎉 迁移完成，共迁移 {total_migrated} 行", flush=True)
    finally:
        reset_sequences(dst_engine)


def reset_sequences(dst_engine):
    """重置 PG 自增序列到 max(id)。"""
    print("\n🔧 重置 PG 自增序列...", flush=True)
    with dst_engine.begin() as conn:
        for t in SERIAL_TABLES:
            max_id = conn.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {t}")).scalar()
            if max_id > 0:
                conn.execute(text(f"SELECT setval('{t}_id_seq', {max_id}, true)"))
                print(f"  ✅ {t}_id_seq -> {max_id}", flush=True)
    print("🔧 序列重置完成", flush=True)


def build_record(table_name: str, row, insert_columns: list[str], column_indices: dict[str, int]) -> dict:
    record = {col: row[column_indices[col]] for col in insert_columns}
    if table_name == "memory_embeddings" and "embedding" in record and record["embedding"] is not None:
        record["embedding"] = serialize_pgvector(record["embedding"])
    return record


def filter_orphan_rows(rows: list, columns: list[str], fk_filter: dict[str, set]) -> tuple[list, int]:
    """过滤 FK 指向不存在父行的源端记录。"""
    col_indices = {col: idx for idx, col in enumerate(columns)}
    filtered_rows = []
    skipped = 0
    for row in rows:
        valid = True
        for fk_col, valid_ids in fk_filter.items():
            if fk_col in col_indices:
                val = row[col_indices[fk_col]]
                if val is not None and val not in valid_ids:
                    valid = False
                    break
        if valid:
            filtered_rows.append(row)
        else:
            skipped += 1
    return filtered_rows, skipped


def select_missing_key_rows(
    rows: list,
    columns: list[str],
    key_columns: list[str],
    target_keys: set[tuple],
) -> tuple[list, set[tuple], set[tuple]]:
    """按唯一键选择目标库尚缺失的源端记录。"""
    col_indices = {col: idx for idx, col in enumerate(columns)}

    def _key(row) -> tuple:
        return tuple(row[col_indices[col]] for col in key_columns)

    source_keys = {_key(row) for row in rows}
    missing_keys = source_keys - target_keys
    missing_rows = [row for row in rows if _key(row) in missing_keys]
    return missing_rows, source_keys, missing_keys


def insert_batches(dst_engine, insert_sql, batches: list[list[dict]], table_name: str, workers: int) -> int:
    """并发插入批次；每个 batch 使用独立事务。"""
    if not batches:
        return 0

    def _insert_batch(records: list[dict]) -> int:
        with dst_engine.begin() as conn:
            conn.execute(insert_sql, records)
        return len(records)

    total_batches = len(batches)
    inserted = 0
    if workers <= 1 or total_batches == 1:
        for idx, batch in enumerate(batches, start=1):
            inserted += _insert_batch(batch)
            print(f"    {table_name}: batch {idx}/{total_batches}, inserted={inserted}", flush=True)
        return inserted

    max_workers = min(workers, total_batches)
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_insert_batch, batch) for batch in batches]
        for future in as_completed(futures):
            inserted += future.result()
            completed += 1
            print(
                f"    {table_name}: batch {completed}/{total_batches}, inserted={inserted}",
                flush=True,
            )
    return inserted


if __name__ == "__main__":
    migrate()
