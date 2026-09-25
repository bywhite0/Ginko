"""Backup and restore keep committed state, never overwrite, and never run beside a service."""

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ginko.cli import main
from ginko.instance import InstanceLock
from ginko.storage.backup import BackupError, backup_database, restore_database
from ginko.storage.budget import BudgetLedger, BudgetLimits
from ginko.storage.database import Database
from ginko.storage.messages import MessageStore

FIXTURES = Path(__file__).parent / "fixtures"
REPLY = "私密的回复正文"


def populate(path: Path, event, now) -> dict[str, str]:
    """One sent, one unknown and one pending delivery plus settled and reserved spend."""
    ids = {}
    with Database(path) as database:
        store = MessageStore(database)
        ledger = BudgetLedger(database, BudgetLimits(10_000, 10_000))
        for index, outcome in enumerate(("sent", "unknown", "pending")):
            saved = event.model_copy(
                update={"source_event_id": f"native:{index}", "event_id": uuid4()}
            )
            store.ingest(saved, expires_at=now + timedelta(hours=1))
            claim = store.claim(now)
            delivery_id = store.complete(claim, now=now, reply=f"{REPLY}{index}")
            ids[outcome] = str(delivery_id)
            if outcome == "pending":
                continue
            store.claim_delivery(now)
            if outcome == "sent":
                store.confirm_delivery(delivery_id, "receipt:1")
            else:
                store.mark_delivery_unknown(delivery_id)
        ledger.reserve("paid:settled", "reply", 300, now=now, trace_id="trace:1")
        ledger.settle("paid:settled", 120, usage=(100, 20, 120))
        ledger.reserve("paid:unknown", "reply", 400, now=now, trace_id="trace:2")
    return ids


def rows(path: Path, query: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(query).fetchall()


def test_backup_is_a_standalone_verified_copy_of_committed_state(tmp_path, event, now):
    source = tmp_path / "data/ginko.sqlite3"
    populate(source, event, now)
    backup = tmp_path / "backups/ginko-20260925.sqlite3"
    result = backup_database(source, backup)
    assert result["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert result["bytes"] == backup.stat().st_size
    summary = result["summary"]
    assert summary["schema_version"] == 3
    assert summary["inbox"] == {"done": 3}
    assert summary["outbox"] == {"pending": 1, "sent": 1, "unknown": 1}
    assert summary["budget"] == {"reserved": 1, "settled": 1}
    assert (summary["reserved_microusd"], summary["settled_microusd"]) == (400, 120)
    assert summary["model_attempts"] == {"reserved": 1, "settled": 1}
    assert REPLY not in json.dumps(result, ensure_ascii=False)
    assert rows(backup, "PRAGMA journal_mode") == [("delete",)]
    assert not Path(f"{backup}-wal").exists()
    assert not Path(f"{backup}.partial").exists()


def test_backup_includes_committed_rows_still_in_the_wal(tmp_path, event, now):
    source = tmp_path / "ginko.sqlite3"
    with Database(source) as database:
        database.connection.execute("PRAGMA wal_autocheckpoint = 0")
        MessageStore(database).ingest(event, expires_at=now + timedelta(hours=1))
        assert Path(f"{source}-wal").stat().st_size > 0
        # The writer is still open, as after a crash before any checkpoint.
        result = backup_database(source, tmp_path / "copy.sqlite3")
    assert result["summary"]["inbox"] == {"pending": 1}
    assert rows(tmp_path / "copy.sqlite3", "SELECT COUNT(*) FROM inbox") == [(1,)]


def test_backup_keeps_the_source_schema_without_migrating(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.executescript((FIXTURES / "schema_v2.sql").read_text(encoding="utf-8"))
    result = backup_database(source, tmp_path / "copy.sqlite3")
    assert result["summary"]["schema_version"] == 2
    assert "model_attempts" not in result["summary"]
    assert rows(source, "PRAGMA user_version") == [(2,)]
    assert rows(tmp_path / "copy.sqlite3", "PRAGMA user_version") == [(2,)]


@pytest.mark.parametrize("existing", ["", "-wal", "-shm", "-journal", ".partial"])
def test_backup_never_replaces_an_existing_file(tmp_path, event, now, existing):
    source = tmp_path / "ginko.sqlite3"
    populate(source, event, now)
    backup = tmp_path / "copy.sqlite3"
    blocker = Path(f"{backup}{existing}")
    blocker.write_bytes(b"keep")
    with pytest.raises(BackupError, match="target_exists"):
        backup_database(source, backup)
    assert blocker.read_bytes() == b"keep"
    if existing:
        assert not backup.exists()


@pytest.mark.parametrize(
    ("contents", "code"),
    [(None, "missing_database"), (b"not a database" * 100, "invalid_database")],
)
def test_backup_rejects_missing_or_foreign_sources_without_creating_them(tmp_path, contents, code):
    source = tmp_path / "ginko.sqlite3"
    if contents is not None:
        source.write_bytes(contents)
    with pytest.raises(BackupError, match=code):
        backup_database(source, tmp_path / "copy.sqlite3")
    assert source.exists() is (contents is not None)
    assert not (tmp_path / "copy.sqlite3").exists()


def test_backup_rejects_unknown_schemas_and_unrelated_databases(tmp_path):
    future = tmp_path / "future.sqlite3"
    with sqlite3.connect(future) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(BackupError, match="unsupported_schema"):
        backup_database(future, tmp_path / "a.sqlite3")
    unrelated = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
        connection.execute("PRAGMA user_version = 3")
    with pytest.raises(BackupError, match="invalid_database"):
        backup_database(unrelated, tmp_path / "b.sqlite3")
    assert not list(tmp_path.glob("[ab].sqlite3*"))


def test_restore_preserves_delivery_and_budget_semantics(tmp_path, event, now):
    source = tmp_path / "old/ginko.sqlite3"
    ids = populate(source, event, now)
    backup = tmp_path / "backup.sqlite3"
    taken = backup_database(source, backup)
    target = tmp_path / "new/ginko.sqlite3"
    restored = restore_database(backup, target)
    assert restored["summary"] == taken["summary"]
    with Database(target) as database:
        store = MessageStore(database)
        ledger = BudgetLedger(database, BudgetLimits(10_000, 10_000))
        assert store.recover_interrupted_deliveries() == 0
        # The restored runtime marks both open attempts interrupted but releases nothing.
        assert ledger.recover_interrupted_model_attempts() == 2
        # Sent stays sent and unknown is never claimed again; only the pending intent is.
        claimed = store.claim_delivery(now)
        assert str(claimed.delivery_id) == ids["pending"]
        assert store.claim_delivery(now) is None
        assert store.delivery_status(claimed.delivery_id) == "sending"
        assert store.delivery_record(claimed.delivery_id).attempts == 1
        attempt = ledger.model_attempt("paid:unknown")
        assert (attempt.status, attempt.budget_status) == ("unknown", "reserved")
        settled = ledger.model_attempt("paid:settled")
        assert (settled.budget_status, settled.actual_microusd) == ("settled", 120)
    assert rows(target, "SELECT status, reserved, actual FROM budget ORDER BY operation_id") == [
        ("settled", 300, 120),
        ("reserved", 400, None),
    ]
    assert rows(target, "SELECT status FROM outbox ORDER BY seq") == [
        ("sent",),
        ("unknown",),
        ("sending",),
    ]


def test_restored_legacy_backup_migrates_only_when_the_runtime_opens_it(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.executescript((FIXTURES / "schema_v1.sql").read_text(encoding="utf-8"))
    backup_database(source, tmp_path / "backup.sqlite3")
    target = tmp_path / "data/ginko.sqlite3"
    assert restore_database(tmp_path / "backup.sqlite3", target)["summary"]["schema_version"] == 1
    with Database(target) as database:
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("existing", ["", "-wal", "-shm", "-journal"])
def test_restore_never_overwrites_a_database_or_its_side_files(tmp_path, event, now, existing):
    populate(tmp_path / "source.sqlite3", event, now)
    backup_database(tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3")
    target = tmp_path / "data/ginko.sqlite3"
    target.parent.mkdir()
    blocker = Path(f"{target}{existing}")
    blocker.write_bytes(b"keep")
    with pytest.raises(BackupError, match="target_exists"):
        restore_database(tmp_path / "backup.sqlite3", target)
    assert blocker.read_bytes() == b"keep"
    if existing:
        assert not target.exists()


def test_restore_rejects_corrupted_backups_without_writing(tmp_path, event, now):
    populate(tmp_path / "source.sqlite3", event, now)
    backup = tmp_path / "backup.sqlite3"
    backup_database(tmp_path / "source.sqlite3", backup)
    data = bytearray(backup.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    for offset in range(page_size, len(data), page_size):
        data[offset + 8 : offset + 64] = b"\xff" * 56
    backup.write_bytes(bytes(data))
    target = tmp_path / "data/ginko.sqlite3"
    with pytest.raises(BackupError, match="integrity_failed|invalid_database"):
        restore_database(backup, target)
    assert not target.exists()
    assert not Path(f"{target}.partial").exists()


def test_cli_backup_and_restore_round_trip(tmp_path, event, now, capsys):
    data_dir = tmp_path / "data"
    populate(data_dir / "ginko.sqlite3", event, now)
    backup = tmp_path / "backup.sqlite3"
    assert main(["backup", str(data_dir), str(backup)]) == 0
    taken = json.loads(capsys.readouterr().out)
    assert taken["status"] == "backed_up"
    assert REPLY not in json.dumps(taken, ensure_ascii=False)
    restored_dir = tmp_path / "restored"
    assert main(["restore", str(backup), str(restored_dir)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["status"] == "restored"
    assert restored["summary"] == taken["summary"]
    assert (
        restored["sha256"]
        == hashlib.sha256((restored_dir / "ginko.sqlite3").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_cli_refuses_while_a_service_holds_the_data_directory(
    tmp_path, event, now, capsys, command
):
    data_dir = tmp_path / "data"
    populate(tmp_path / "source.sqlite3", event, now)
    backup_database(tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3")
    if command == "backup":
        populate(data_dir / "ginko.sqlite3", event, now)
        argv = ["backup", str(data_dir), str(tmp_path / "copy.sqlite3")]
    else:
        argv = ["restore", str(tmp_path / "backup.sqlite3"), str(data_dir)]
    with InstanceLock(data_dir):
        assert main(argv) == 2
    assert "instance_running" in capsys.readouterr().err
    assert not (tmp_path / "copy.sqlite3").exists()
    if command == "restore":
        assert not (data_dir / "ginko.sqlite3").exists()


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["backup", "{tmp}/missing", "{tmp}/copy.sqlite3"], "missing_database"),
        (["restore", "{tmp}/missing.sqlite3", "{tmp}/data"], "missing_database"),
    ],
)
def test_cli_reports_fixed_error_codes(tmp_path, capsys, argv, code):
    assert main([part.format(tmp=tmp_path) for part in argv]) == 2
    assert f"failed: {code}" in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()
