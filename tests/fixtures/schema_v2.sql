-- Frozen Ginko schema 2 from commit 543d9c3.
PRAGMA user_version = 2;

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
