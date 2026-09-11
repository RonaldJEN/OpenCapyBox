"""An acknowledged text delta survives a killed writer before TEXT_MESSAGE_END."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.api.models.database import Base
from src.api.models.round import Round
from src.api.models.session import Session
from src.api.services.agui_event_bus import AguiEventBus
from src.api.services.history_service import HistoryService


def factory_at(path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


async def initialize(factory):
    with factory() as db:
        db.add(Session(id="durable-session", user_id="fixture"))
        db.add(Round(id="durable-round", session_id="durable-session", user_message="test", status="running"))
        db.commit()
    bus = AguiEventBus(factory)
    await bus.publish("durable-round", {"type": "STEP_STARTED", "stepName": "step_1"})
    await bus.publish("durable-round", {"type": "TEXT_MESSAGE_START", "messageId": "durable-message", "role": "assistant"})
    return bus


def test_visible_tail_survives_writer_kill_before_end(tmp_path):
    database = tmp_path / "writer.sqlite"
    process = subprocess.Popen([sys.executable, __file__, str(database)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        line = process.stdout.readline()
        assert line.startswith("VISIBLE:"), line + process.stderr.read() if process.poll() is not None else line
        acknowledged = json.loads(line.removeprefix("VISIBLE:"))
        process.kill()
        process.wait(timeout=10)
        engine, factory = factory_at(database)
        try:
            history = HistoryService(factory).get_session_rounds("durable-session")[0]
            assert history["assistant_messages"][0]["content"] == acknowledged["delta"]
            assert history["assistant_messages"][0]["content_committed"] is True
            assert history["status"] == "running"  # Restoring text is not declaring success.
            assert history["last_event_sequence"] == acknowledged["sequence"]
        finally:
            engine.dispose()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.asyncio
async def test_failed_commit_never_publishes_visible_text(tmp_path, monkeypatch):
    engine, factory = factory_at(tmp_path / "failure.sqlite")
    try:
        bus = await initialize(factory)
        fanout = AsyncMock()
        monkeypatch.setattr(bus, "publish_committed", fanout)
        def fail(*args, **kwargs):
            raise RuntimeError("fixture commit failure")
        monkeypatch.setattr(bus, "_write_events_with_sequences", fail)
        with pytest.raises(RuntimeError, match="commit failure"):
            await bus.publish("durable-round", {"type": "TEXT_MESSAGE_CONTENT", "messageId": "durable-message", "delta": "never published"})
        fanout.assert_not_awaited()
    finally:
        engine.dispose()


async def writer(path):
    _, factory = factory_at(path)
    bus = await initialize(factory)
    stored = await bus.publish("durable-round", {"type": "TEXT_MESSAGE_CONTENT", "messageId": "durable-message", "delta": "已经可见且已提交的尾段"})
    assert stored is not None and stored.event["isAggregate"] is False
    print("VISIBLE:" + json.dumps(stored.event, ensure_ascii=True), flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(writer(sys.argv[1]))
