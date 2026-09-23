import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from ginko.storage.database import Database
from ginko.storage.messages import MessageStore


def test_schema_one_migrates_delivery_deadline_and_retry_state(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    event_id = str(uuid4())
    delivery_id = str(uuid4())
    expiry = (datetime.now(UTC) + timedelta(minutes=5)).timestamp()
    session = json.dumps(
        {"platform": "qq", "bot_id": "10000", "kind": "private", "chat_id": "20001"}
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE inbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                dedupe_key TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                lease_until REAL,
                claim_token TEXT
            );
            CREATE TABLE outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL UNIQUE,
                event_id TEXT NOT NULL UNIQUE,
                session TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'sending',
                platform_message_id TEXT
            );
            CREATE INDEX outbox_work ON outbox(status, seq);
            """
        )
        connection.execute(
            "INSERT INTO inbox (event_id, dedupe_key, agent_id, payload, max_attempts, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, "dedupe", "ginko", "{}", 2, expiry),
        )
        connection.execute(
            "INSERT INTO outbox (delivery_id, event_id, session, text) VALUES (?, ?, ?, ?)",
            (delivery_id, event_id, session, "legacy"),
        )

    with Database(path) as database:
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        row = database.connection.execute("SELECT * FROM outbox").fetchone()
        assert row["expires_at"] == expiry
        assert row["next_attempt_at"] == 0
        assert row["attempts"] == 0
        store = MessageStore(database)
        assert store.recover_interrupted_deliveries() == 1
        assert store.delivery_status(UUID(delivery_id)) == "unknown"
