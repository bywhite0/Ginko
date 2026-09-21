"""SQLite connection and schema for the single-process foundation."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    dedupe_key TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'processing', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    expires_at REAL NOT NULL,
    lease_until REAL,
    claim_token TEXT
);
CREATE INDEX IF NOT EXISTS inbox_work ON inbox(status, seq);
CREATE INDEX IF NOT EXISTS inbox_agent ON inbox(agent_id, status, lease_until);
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL UNIQUE REFERENCES inbox(event_id),
    session TEXT NOT NULL,
    text TEXT NOT NULL CHECK(length(text) > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'sending', 'sent', 'unknown', 'failed')),
    platform_message_id TEXT
);
CREATE INDEX IF NOT EXISTS outbox_work ON outbox(status, seq);
CREATE TABLE IF NOT EXISTS budget (
    operation_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    day TEXT NOT NULL,
    month TEXT NOT NULL,
    reserved INTEGER NOT NULL CHECK(reserved > 0),
    actual INTEGER CHECK(actual >= 0),
    status TEXT NOT NULL CHECK(status IN ('reserved', 'settled', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS budget_day ON budget(day, status);
CREATE INDEX IF NOT EXISTS budget_month ON budget(month, status);
"""


def timestamp(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.timestamp()


class Database:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        # Full synchronization favors durability for small local workloads.
        self.connection.execute("PRAGMA synchronous = FULL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            self.close()
            raise ValueError(f"unsupported database schema version: {version}")
        if version == 0:
            self.connection.executescript(
                f"BEGIN IMMEDIATE;\n{SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
