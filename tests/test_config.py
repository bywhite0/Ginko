import json
import tomllib
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from ginko.cli import main
from ginko.config import ConfigurationError, RuntimeSettings, load_config
from ginko.core.events import SessionRef

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"
SECRETS = {
    "GINKO_ONEBOT_ACCESS_TOKEN": "local-onebot-test-secret",
    "GINKO_MODEL_API_KEY": "model-test-secret-never-print",
}


@pytest.fixture
def raw_config():
    return tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.local.toml"
    path.write_bytes(EXAMPLE.read_bytes())
    return path


def test_load_is_offline_resolves_data_relative_to_config_and_hides_secrets(
    config_path, monkeypatch
):
    monkeypatch.chdir(config_path.parent.parent)
    loaded = load_config(config_path, environ=SECRETS)
    assert loaded.data_dir == config_path.parent / "data"
    assert not loaded.data_dir.exists()
    assert loaded.persona.version == loaded.settings.approved_persona_version == "1.0.0"
    assert loaded.settings.budget.limits.daily_microusd == 0
    assert loaded.model_api_key.get_secret_value() == SECRETS["GINKO_MODEL_API_KEY"]
    assert all(secret not in repr(loaded) for secret in SECRETS.values())


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("budget", "daily_microusd", -1),
        ("budget", "daily_microusd", True),
        ("budget", "monthly_microusd", "100"),
        ("budget", "monthly_microusd", 2**63),
        ("model", "timeout_seconds", float("inf")),
        ("model", "max_input_tokens", 0),
        ("model", "input_microusd_per_million_tokens", 0),
        ("model", "api_key", "model-test-secret-never-print"),
        ("model", "api_key_env", "BAD ENV NAME"),
        ("model", "protocol", "unknown"),
        ("model", "base_url", "https://user:secret@example.invalid/v1"),
        ("model", "base_url", "https://example.invalid/v1?key=secret"),
        ("model", "base_url", "http://example.invalid/v1"),
        ("model", "base_url", "file:///private/config"),
        ("model", "base_url", "https://example.invalid:99999/v1"),
        ("model", "base_url", "https://example.invalid:0/v1"),
        ("onebot", "port", 65536),
        ("onebot", "bot_id", ""),
        ("onebot", "host", "0.0.0.0"),
        ("onebot", "api_timeout_seconds", 0),
        ("onebot", "api_timeout_seconds", 31),
        ("onebot", "api_timeout_seconds", float("inf")),
        ("activity", "max_attempts", 0),
        ("activity", "lease_seconds", 30),
        ("activity", "ttl_seconds", 60),
    ],
)
def test_unsafe_configuration_is_rejected(raw_config, section, key, value):
    raw_config[section][key] = value
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(raw_config)


@pytest.mark.parametrize("sessions", [[], [{"kind": "group", "chat_id": "*"}]])
def test_allowlist_must_be_explicit(raw_config, sessions):
    raw_config["allowed_sessions"] = sessions
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(raw_config)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("config_version", True),
        ("config_version", 1.0),
        ("autonomous", 0),
        ("autonomous", True),
        ("behavior", "respond_to_everything"),
    ],
)
def test_runtime_flags_reject_coercion_and_unsupported_modes(raw_config, key, value):
    raw_config[key] = value
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(raw_config)


def test_duplicate_sessions_and_out_of_scope_relationships_are_rejected(raw_config):
    duplicate = deepcopy(raw_config)
    duplicate["allowed_sessions"] *= 2
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(duplicate)
    raw_config["relationships"] = [
        {"kind": "group", "chat_id": "20002", "user_id": "20001", "role": "kaho"}
    ]
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(raw_config)


def test_relationships_require_exact_platform_bot_session_and_person(raw_config):
    raw_config["relationships"] = [
        {"kind": "private", "chat_id": "20001", "user_id": "20001", "role": "kaho"}
    ]
    settings = RuntimeSettings.model_validate(raw_config)
    session = SessionRef(platform="qq", bot_id="10000", kind="private", chat_id="20001")
    assert settings.allows(session)
    assert settings.relationship_for(session, "20001") == "kaho"
    assert settings.relationship_for(session, "20002") is None
    for override in (
        {"platform": "telegram"},
        {"bot_id": "10001"},
        {"kind": "group"},
        {"chat_id": "20002"},
        {"thread_id": "thread"},
    ):
        other = session.model_copy(update=override)
        assert not settings.allows(other)
        assert settings.relationship_for(other, "20001") is None


def test_private_relationship_cannot_identify_a_different_person(raw_config):
    raw_config["relationships"] = [
        {"kind": "private", "chat_id": "20001", "user_id": "20002", "role": "kaho"}
    ]
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(raw_config)


def test_reservations_round_up_each_token_category(raw_config):
    raw_config["model"].update(
        max_input_tokens=1,
        max_output_tokens=1,
        input_microusd_per_million_tokens=1,
        output_microusd_per_million_tokens=1,
    )
    assert RuntimeSettings.model_validate(raw_config).model.reservation_microusd == 2


def test_persona_version_requires_maintainer_approval(config_path):
    config_path.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            'approved_persona_version = "1.0.0"', 'approved_persona_version = "0.9.0"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="approved version"):
        load_config(config_path, environ=SECRETS)


@pytest.mark.parametrize("credential", ["", " ", "secret\nheader", "secret\tvalue", "secret "])
def test_missing_or_invalid_credentials_fail_closed(config_path, credential):
    env = SECRETS | {"GINKO_MODEL_API_KEY": credential}
    with pytest.raises(ConfigurationError, match="GINKO_MODEL_API_KEY") as error:
        load_config(config_path, environ=env)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("contents", ['[model\napi_key="secret"', 'config_version = "secret"'])
def test_invalid_config_does_not_echo_source_values(config_path, contents):
    config_path.write_text(contents, encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        load_config(config_path, environ=SECRETS)
    assert "secret" not in str(error.value)


def test_check_config_is_an_offline_command_with_redacted_output(config_path, monkeypatch, capsys):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    assert main(["check-config", str(config_path)]) == 0
    result = capsys.readouterr()
    summary = json.loads(result.out)
    assert summary["status"] == "valid"
    assert summary["live_gateway"] is summary["live_model"] is summary["autonomous"] is False
    assert summary["allowed_sessions"] == 1
    assert summary["daily_microusd"] == summary["monthly_microusd"] == 0
    assert not (config_path.parent / "data").exists()
    assert all(secret not in result.out + result.err for secret in SECRETS.values())


def test_check_config_missing_credentials_returns_nonzero(config_path, monkeypatch, capsys):
    monkeypatch.delenv("GINKO_ONEBOT_ACCESS_TOKEN", raising=False)
    assert main(["check-config", str(config_path)]) == 2
    result = capsys.readouterr()
    assert not result.out
    assert "GINKO_ONEBOT_ACCESS_TOKEN" in result.err
