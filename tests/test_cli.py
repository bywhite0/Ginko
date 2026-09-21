import json
import os
import subprocess
import sys

import pytest

from ginko.cli import main, smoke
from ginko.persona import load_ginko


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
