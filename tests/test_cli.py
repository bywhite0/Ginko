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
