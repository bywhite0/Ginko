import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ginko.storage import database as database_module
from ginko.storage.budget import BudgetExceededError, BudgetLedger, BudgetLimits
from ginko.storage.database import Database
from ginko.storage.messages import MessageStore


def legacy_database(path: Path, version: int):
    event_id, delivery_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    expiry = (now + timedelta(minutes=5)).timestamp()
    session = json.dumps(
        {"platform": "qq", "bot_id": "10000", "kind": "private", "chat_id": "20001"}
    )
    schema = Path(__file__).parent / "fixtures" / f"schema_v{version}.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(schema.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO inbox (event_id, dedupe_key, agent_id, payload, max_attempts, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(event_id), "dedupe", "ginko", "{}", 2, expiry),
        )
        if version == 1:
            connection.execute(
                "INSERT INTO outbox (delivery_id, event_id, session, text, status) "
                "VALUES (?, ?, ?, ?, 'sending')",
                (str(delivery_id), str(event_id), session, "legacy"),
            )
        else:
            connection.execute(
                """INSERT INTO outbox
                (delivery_id, event_id, session, text, status, expires_at, attempts,
                 next_attempt_at, last_error) VALUES (?, ?, ?, ?, 'sending', ?, 2, ?, ?)""",
                (
                    str(delivery_id),
                    str(event_id),
                    session,
                    "legacy",
                    expiry,
                    now.timestamp(),
                    "rate_limited",
                ),
            )
        day = now.date().isoformat()
        for operation, status, reserved, actual in (
            ("legacy-unknown", "reserved", 100, None),
            ("legacy-billed", "settled", 50, 40),
            ("legacy-cancelled", "cancelled", 50, None),
        ):
            connection.execute(
                "INSERT INTO budget VALUES (?, 'reply', ?, ?, ?, ?, ?)",
                (operation, day, day[:7], reserved, actual, status),
            )
        budget = connection.execute("SELECT * FROM budget ORDER BY operation_id").fetchall()
    return delivery_id, now, expiry, budget


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_schemas_preserve_messages_and_budget_without_fabricating_usage(tmp_path, version):
    path = tmp_path / "legacy.sqlite3"
    delivery_id, now, expiry, budget = legacy_database(path, version)
    for _ in range(2):
        with Database(path) as database:
            assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 3
            row = database.connection.execute("SELECT * FROM outbox").fetchone()
            assert row["expires_at"] == expiry
            assert row["next_attempt_at"] == (0 if version == 1 else now.timestamp())
            assert row["attempts"] == (0 if version == 1 else 2)
            assert row["last_error"] == (None if version == 1 else "rate_limited")
            assert row["status"] == "sending"
            assert row["text"] == "legacy"
            assert [
                tuple(row)
                for row in database.connection.execute("SELECT * FROM budget ORDER BY operation_id")
            ] == budget
            assert database.connection.execute("PRAGMA foreign_key_check").fetchall() == []
            ledger = BudgetLedger(database, BudgetLimits(200, 200))
            assert ledger.list_model_attempts() == ()
            with pytest.raises(BudgetExceededError):
                ledger.reserve("new", "reply", 61, now=now)
    with Database(path) as database:
        store = MessageStore(database)
        assert store.recover_interrupted_deliveries() == 1
        assert store.delivery_status(delivery_id) == "unknown"


def test_failed_migration_rolls_back_table_and_version_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    _, _, _, budget = legacy_database(path, 2)
    with monkeypatch.context() as patch:
        patch.setattr(
            database_module,
            "MODEL_ATTEMPTS_INDEX",
            "CREATE INDEX model_attempts_trace ON model_attempts(missing_column)",
        )
        with pytest.raises(sqlite3.OperationalError):
            Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'model_attempts'"
            ).fetchall()
            == []
        )
        assert connection.execute("SELECT * FROM budget ORDER BY operation_id").fetchall() == budget
    with Database(path) as database:
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert BudgetLedger(database, BudgetLimits(0, 0)).list_model_attempts() == ()
