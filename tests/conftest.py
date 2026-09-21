from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ginko.core.events import EventEnvelope, SessionRef, TextSegment
from ginko.storage.database import Database


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 22, 9, tzinfo=UTC)


@pytest.fixture
def event(now: datetime) -> EventEnvelope:
    return EventEnvelope(
        agent_id="ginko",
        session=SessionRef(platform="qq", bot_id="bot:1", kind="private", chat_id="user:1"),
        kind="message.created",
        source_event_id="native:1",
        message_id="message:1",
        user_id="user:1",
        occurred_at=now,
        received_at=now,
        content=(TextSegment(text="你好"),),
    )


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    with Database(tmp_path / "test.sqlite3") as database:
        yield database
