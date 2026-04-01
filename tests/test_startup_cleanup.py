"""测试服务器启动时清理残留 running 轮次的逻辑"""
import uuid
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.models.database import Base
from src.api.models.round import Round


def _cleanup_stale_rounds(db):
    """模拟启动清理逻辑 — 将所有 running 轮次标记为 failed"""
    from src.api.utils.timezone import now_naive

    stale_count = (
        db.query(Round)
        .filter(Round.status == "running")
        .update({
            "status": "failed",
            "completed_at": now_naive(),
            "final_response": "[系统重启，执行被中断]",
        })
    )
    db.commit()
    return stale_count


@pytest.fixture
def in_memory_db():
    """创建内存 SQLite 数据库用于测试"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


class TestStartupCleanup:
    """测试启动时清理残留 running 轮次"""

    def test_stale_running_rounds_marked_failed(self, in_memory_db):
        """重启后，所有 running 轮次应被标记为 failed"""
        engine, Session = in_memory_db
        with Session() as db:
            # 插入 2 个 running 和 1 个 completed 轮次
            r1 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="hi", status="running")
            r2 = Round(id=str(uuid.uuid4()), session_id="s2", user_message="hello", status="running")
            r3 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="done", status="completed")
            db.add_all([r1, r2, r3])
            db.commit()

            # 模拟启动清理逻辑
            stale_count = _cleanup_stale_rounds(db)

            assert stale_count == 2

            # 验证状态
            all_rounds = db.query(Round).all()
            running = [r for r in all_rounds if r.status == "running"]
            failed = [r for r in all_rounds if r.status == "failed"]
            completed = [r for r in all_rounds if r.status == "completed"]

            assert len(running) == 0
            assert len(failed) == 2
            assert len(completed) == 1

            # 验证 failed 轮次有正确的 final_response 和 completed_at
            for r in failed:
                assert r.final_response == "[系统重启，执行被中断]"
                assert r.completed_at is not None

    def test_no_stale_rounds_noop(self, in_memory_db):
        """没有 running 轮次时，清理不影响已有数据"""
        engine, Session = in_memory_db
        with Session() as db:
            r1 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="ok", status="completed")
            db.add(r1)
            db.commit()

            stale_count = _cleanup_stale_rounds(db)

            assert stale_count == 0

            # completed 轮次不受影响
            r = db.query(Round).first()
            assert r.status == "completed"
            assert r.final_response is None

    def test_completed_rounds_untouched(self, in_memory_db):
        """清理只影响 running 状态，不影响 completed/failed"""
        engine, Session = in_memory_db
        with Session() as db:
            r1 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="a", status="completed",
                       final_response="done")
            r2 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="b", status="failed",
                       final_response="error")
            r3 = Round(id=str(uuid.uuid4()), session_id="s1", user_message="c", status="running")
            db.add_all([r1, r2, r3])
            db.commit()

            stale_count = _cleanup_stale_rounds(db)

            assert stale_count == 1

            # 验证原有 completed/failed 未被修改
            r1_db = db.query(Round).filter(Round.id == r1.id).first()
            assert r1_db.final_response == "done"
            assert r1_db.status == "completed"

            r2_db = db.query(Round).filter(Round.id == r2.id).first()
            assert r2_db.final_response == "error"
            assert r2_db.status == "failed"
