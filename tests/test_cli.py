import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ginko.cli import main, smoke
from ginko.core.events import EventEnvelope, SessionRef, TextSegment
from ginko.persona import load_ginko
from ginko.storage.budget import BudgetLedger, BudgetLimits
from ginko.storage.database import Database
from ginko.storage.messages import MessageStore


def test_packaged_persona_resources_are_loadable():
    persona = load_ginko()
    assert persona.persona_id == "ginko"
    assert persona.display_name == "百生吟子"
    assert persona.identity and persona.style and persona.world


def test_offline_smoke_exercises_persistence():
    result = smoke()
    assert result["deduplicated"] is True
    assert result["inbox"] == "done"
    assert result["outbox"] == "sent"
    assert result["paid_calls"] == result["platform_messages"] == 0


def test_doctor_does_not_claim_live_capability(capsys):
    assert main(["doctor"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "foundation"
    assert result["live_gateway"] is False
    assert result["live_model"] is False


@pytest.mark.parametrize("command", ["doctor", "smoke", "persona"])
def test_cli_emits_utf8_with_legacy_stdout_encoding(command):
    result = subprocess.run(
        [sys.executable, "-m", "ginko", command],
        env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert load_ginko().display_name in result.stdout.decode("utf-8")


def test_delivery_cli_lists_unknown_without_message_text_and_reconciles(tmp_path, capsys):
    now = datetime.now(UTC)
    event = EventEnvelope(
        agent_id="ginko",
        session=SessionRef(platform="qq", bot_id="10000", kind="private", chat_id="20001"),
        kind="message.created",
        source_event_id=str(uuid4()),
        message_id="message-1",
        user_id="20001",
        occurred_at=now,
        received_at=now,
        content=(TextSegment(text="private message"),),
    )
    path = tmp_path / "ginko.sqlite3"
    with Database(path) as database:
        store = MessageStore(database)
        store.ingest(event, expires_at=now + timedelta(minutes=5))
        delivery_id = store.complete(store.claim(now), now=now, reply="private reply")
        store.claim_delivery(now)
        store.mark_delivery_unknown(delivery_id, reason="transport_error")

    assert main(["deliveries", str(path), "--status", "unknown"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["delivery_id"] == str(delivery_id)
    assert listed[0]["last_error"] == "transport_error"
    assert "private reply" not in json.dumps(listed)

    assert main(["reconcile-delivery", str(path), str(delivery_id), "failed"]) == 0
    reconciled = json.loads(capsys.readouterr().out)
    assert reconciled["status"] == "failed"


def test_model_attempt_cli_lists_audit_metadata_without_prompt_text(tmp_path, capsys):
    path = tmp_path / "attempts.sqlite3"
    with Database(path) as database:
        ledger = BudgetLedger(database, BudgetLimits(500, 1000))
        ledger.reserve("trace:attempt", "chat", 100, now=datetime.now(UTC), trace_id="trace")
        ledger.settle("trace:attempt", 42, usage=(30, 10, 40))
        ledger.record_model_outcome("trace:attempt", "accepted")
        ledger.reserve("other:attempt", "chat", 100, now=datetime.now(UTC), trace_id="other")
        now = datetime.now(UTC)
        event = EventEnvelope(
            agent_id="ginko",
            session=SessionRef(platform="qq", bot_id="10000", kind="private", chat_id="20001"),
            kind="message.created",
            source_event_id="private",
            message_id="private",
            user_id="20001",
            occurred_at=now,
            received_at=now,
            content=(TextSegment(text="private prompt must stay private"),),
        )
        store = MessageStore(database)
        store.ingest(event, expires_at=now + timedelta(minutes=5))
        store.complete(store.claim(now), now=now, reply="private reply must stay private")
    assert main(["model-attempts", str(path), "--trace-id", "trace"]) == 0
    output = capsys.readouterr().out
    assert "private" not in output
    result = json.loads(output)
    assert len(result) == 1
    assert result[0]["trace_id"] == "trace"
    assert result[0]["operation_id"] == "trace:attempt"
    assert result[0]["status"] == "settled"
    assert result[0]["actual_microusd"] == 42
    assert "text" not in result[0]
    assert main(["model-attempts", str(path), "trace:attempt"]) == 0
    assert json.loads(capsys.readouterr().out) == result
    assert main(["model-attempts", str(path)]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert main(["model-attempts", str(path), "--trace-id", "missing"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    with Database(path) as database:
        assert (
            BudgetLedger(database, BudgetLimits(0, 0)).model_attempt("other:attempt").status
            == "reserved"
        )


@pytest.mark.parametrize("kind", ["missing", "corrupt", "newer_schema", "unknown_id", "filters"])
def test_model_attempt_cli_query_failures_are_safe(tmp_path, capsys, kind):
    path = tmp_path / "private-database.sqlite3"
    args = ["model-attempts", str(path)]
    if kind == "corrupt":
        path.write_bytes(b"private secret database data")
    elif kind != "missing":
        with Database(path) as database:
            if kind == "newer_schema":
                database.connection.execute("PRAGMA user_version = 999")
        if kind == "unknown_id":
            args.append("unknown")
        if kind == "filters":
            args.extend(["unknown", "--trace-id", "trace"])
    assert main(args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        "Model attempt command failed: "
        + ("conflicting_filters" if kind == "filters" else "invalid_query")
        + "\n"
    )
    if kind == "missing":
        assert not path.exists()
