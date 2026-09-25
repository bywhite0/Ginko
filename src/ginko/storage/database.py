"""SQLite connection and schema for the single-process foundation."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 3
MODEL_ATTEMPTS_TABLE = """CREATE TABLE model_attempts (
    operation_id TEXT PRIMARY KEY REFERENCES budget(operation_id),
    trace_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK(status IN ('reserved', 'settled', 'unknown', 'cancelled')),
    prompt_tokens INTEGER CHECK(prompt_tokens IS NULL OR prompt_tokens >= 1),
    completion_tokens INTEGER CHECK(completion_tokens IS NULL OR completion_tokens >= 0),
    total_tokens INTEGER CHECK(total_tokens IS NULL OR total_tokens >= 1),
    cached_prompt_tokens INTEGER CHECK(
        cached_prompt_tokens IS NULL OR cached_prompt_tokens BETWEEN 0 AND prompt_tokens
    ),
    reasoning_tokens INTEGER CHECK(
        reasoning_tokens IS NULL OR reasoning_tokens BETWEEN 0 AND completion_tokens
    ),
    outcome_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)"""
MODEL_ATTEMPTS_INDEX = "CREATE INDEX model_attempts_trace ON model_attempts(trace_id, created_at)"
SCHEMA = f"""
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
    platform_message_id TEXT,
    expires_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_work ON outbox(status, next_attempt_at, seq);
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
{MODEL_ATTEMPTS_TABLE};
{MODEL_ATTEMPTS_INDEX};
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
        if version not in (0, 1, 2, SCHEMA_VERSION):
            self.close()
            raise ValueError(f"unsupported database schema version: {version}")
        if version == 0:
            self.connection.executescript(
                f"BEGIN IMMEDIATE;\n{SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
        elif version == 1:
            self._migrate_v1_to_v2()
            self._migrate_v2_to_v3()
        elif version == 2:
            self._migrate_v2_to_v3()

    def _migrate_v1_to_v2(self) -> None:
        """Add durable delivery scheduling fields without rewriting existing messages."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "ALTER TABLE outbox ADD COLUMN expires_at REAL NOT NULL DEFAULT 0"
            )
            self.connection.execute(
                "ALTER TABLE outbox ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0"
            )
            self.connection.execute(
                "ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.execute("ALTER TABLE outbox ADD COLUMN last_error TEXT")
            self.connection.execute(
                """UPDATE outbox SET expires_at = (
                    SELECT inbox.expires_at FROM inbox WHERE inbox.event_id = outbox.event_id
                ) WHERE expires_at = 0"""
            )
            self.connection.execute("DROP INDEX IF EXISTS outbox_work")
            self.connection.execute(
                "CREATE INDEX outbox_work ON outbox(status, next_attempt_at, seq)"
            )
            self.connection.execute("PRAGMA user_version = 2")
        except BaseException:
            self.connection.rollback()
            self.close()
            raise
        else:
            self.connection.commit()

    def _migrate_v2_to_v3(self) -> None:
        """Add durable model-attempt metadata without changing budget totals."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(MODEL_ATTEMPTS_TABLE)
            self.connection.execute(MODEL_ATTEMPTS_INDEX)
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            self.connection.rollback()
            self.close()
            raise
        else:
            self.connection.commit()

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
