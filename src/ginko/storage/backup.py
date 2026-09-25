"""Consistent SQLite copies for a stopped runtime; restore only writes an empty target.

Callers hold the data-directory instance lock. Neither operation migrates the source,
so a backup keeps the schema it was taken from and upgrades remain an explicit step.
"""

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ginko.storage.database import SCHEMA_VERSION

SUPPORTED_SCHEMAS = (1, 2, SCHEMA_VERSION)
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


class BackupError(RuntimeError):
    """Carries a fixed, display-safe code; never paths' contents or message text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def database_summary(connection: sqlite3.Connection) -> dict[str, object]:
    """Counts needed to reconcile a restore; excludes message bodies and identities."""
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not {"inbox", "outbox", "budget"} <= tables:
        raise BackupError("invalid_database")

    def by_status(table: str) -> dict[str, int]:
        rows = connection.execute(f"SELECT status, COUNT(*) FROM {table} GROUP BY status")
        return {status: count for status, count in sorted(rows)}

    def scalar(query: str) -> int:
        return connection.execute(query).fetchone()[0] or 0

    summary: dict[str, object] = {
        "schema_version": scalar("PRAGMA user_version"),
        "inbox": by_status("inbox"),
        "inbox_last_seq": scalar("SELECT MAX(seq) FROM inbox"),
        "outbox": by_status("outbox"),
        "outbox_last_seq": scalar("SELECT MAX(seq) FROM outbox"),
        "budget": by_status("budget"),
        "reserved_microusd": scalar("SELECT SUM(reserved) FROM budget WHERE status = 'reserved'"),
        "settled_microusd": scalar("SELECT SUM(actual) FROM budget WHERE status = 'settled'"),
    }
    if "model_attempts" in tables:
        summary["model_attempts"] = by_status("model_attempts")
    return summary


def backup_database(source: Path, destination: Path) -> dict[str, object]:
    """Copy committed state, including an unmerged WAL, into a new standalone file."""
    _require_absent(destination)
    with _open(source, "rw") as connection:
        expected = database_summary(connection)
        return _copy(connection, destination, expected)


def restore_database(backup: Path, target: Path) -> dict[str, object]:
    """Place a verified backup at an unused database path; never merge or overwrite."""
    _require_absent(target)
    with _open(backup, "ro") as connection:
        _check_integrity(connection)
        expected = database_summary(connection)
        return _copy(connection, target, expected)


def _require_absent(path: Path) -> None:
    # A stale WAL beside a new file could be replayed onto it, so side files block too.
    candidates = (path, *(Path(f"{path}{suffix}") for suffix in SIDE_SUFFIXES))
    if any(candidate.exists() for candidate in candidates):
        raise BackupError("target_exists")


@contextmanager
def _open(path: Path, mode: str) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise BackupError("missing_database")
    connection = None
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode={mode}", uri=True, isolation_level=None
        )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError:
        if connection is not None:
            connection.close()
        raise BackupError("invalid_database") from None
    try:
        if version not in SUPPORTED_SCHEMAS:
            raise BackupError("unsupported_schema")
        yield connection
    finally:
        connection.close()


def _check_integrity(connection: sqlite3.Connection) -> None:
    try:
        result = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError:
        raise BackupError("invalid_database") from None
    if result != [("ok",)]:
        raise BackupError("integrity_failed")


def _copy(
    source: sqlite3.Connection, destination: Path, expected: dict[str, object]
) -> dict[str, object]:
    partial = Path(f"{destination}.partial")
    _require_absent(partial)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        copy = sqlite3.connect(partial, isolation_level=None)
        try:
            source.backup(copy)
            # One self-contained file: rollback journal mode leaves no WAL to carry along.
            copy.execute("PRAGMA journal_mode = DELETE")
            _check_integrity(copy)
            if database_summary(copy) != expected:
                raise BackupError("summary_mismatch")
        finally:
            copy.close()
        _publish(partial, destination)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "summary": expected,
    }


def _publish(partial: Path, destination: Path) -> None:
    """Move without replacing a file that appeared after the initial check."""
    try:
        os.link(partial, destination)
    except FileExistsError:
        raise BackupError("target_exists") from None
    except OSError:
        # Filesystems without hard links: Windows rename still refuses an existing target.
        if destination.exists():
            raise BackupError("target_exists") from None
        partial.rename(destination)
