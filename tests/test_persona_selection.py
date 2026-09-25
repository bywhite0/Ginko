"""Persona selection: reviewed content only, one persona per agent, persona-owned address."""

import asyncio
import json
import re
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from ginko.cli import main
from ginko.config import ConfigurationError, RuntimeConfig, RuntimeSettings, load_config
from ginko.core.events import EventEnvelope, SessionRef, TextSegment
from ginko.decision import TextDecider
from ginko.persona import PersonaError, load_ginko, load_persona
from ginko.runtime import RejectActivity, RetryActivity
from ginko.storage.messages import EventClaim

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"
SECRETS = {
    "GINKO_ONEBOT_ACCESS_TOKEN": "synthetic-onebot-token",
    "GINKO_MODEL_API_KEY": "synthetic-model-key",
}
MANIFEST = """id = "kaho"
version = "0.1.0"
display_name = "日野下花帆"
identity = "identity.md"
style = "style.md"
world = "world.md"
change_policy = "maintainer_review"
relationship_terms = ["屏幕那边的花帆", "另一个花帆"]
"""
IDENTITY = "# 日野下花帆\n\n合成的花帆身份文本。\n"


def write_persona(root: Path, *, manifest: str = MANIFEST, identity: str = IDENTITY) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    # Bytes keep LF on every platform, so line-ending tests start from a known state.
    for name, text in (
        ("manifest.toml", manifest),
        ("identity.md", identity),
        ("style.md", "# 语气\n\n合成的花帆语气文本。\n"),
        ("world.md", "# 知识\n\n合成的花帆背景。\n"),
    ):
        (root / name).write_bytes(text.encode("utf-8"))
    return root


def config_file(tmp_path: Path, **overrides) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    for key in overrides:
        text = re.sub(rf"^{key} = .*\n", "", text, count=1, flags=re.MULTILINE)
    header = "".join(f"{key} = {json.dumps(value)}\n" for key, value in overrides.items())
    path = tmp_path / "config.local.toml"
    path.write_text(header + text, encoding="utf-8")
    return path


def kaho_config(tmp_path: Path, **changes) -> Path:
    persona = load_persona(write_persona(tmp_path / "personas/kaho"))
    values = {
        "agent_id": "kaho",
        "approved_persona_version": persona.version,
        "persona_dir": "personas/kaho",
        "approved_persona_sha256": persona.digest,
    } | changes
    return config_file(tmp_path, **{k: v for k, v in values.items() if v is not None})


def test_bundled_ginko_declares_its_reserved_address_and_a_stable_digest():
    first, second = load_ginko(), load_ginko()
    assert first.persona_id == "ginko"
    assert first.relationship_terms == ("花帆学姐",)
    assert len(first.digest) == 64 and first.digest == second.digest


def test_directory_persona_loads_all_resources(tmp_path):
    persona = load_persona(write_persona(tmp_path / "kaho"))
    assert (persona.persona_id, persona.version, persona.display_name) == (
        "kaho",
        "0.1.0",
        "日野下花帆",
    )
    assert persona.identity == IDENTITY
    assert "合成的花帆语气" in persona.style and "合成的花帆背景" in persona.world
    assert persona.relationship_terms == ("屏幕那边的花帆", "另一个花帆")


def test_digest_binds_every_file_but_ignores_line_endings(tmp_path):
    base = load_persona(write_persona(tmp_path / "a")).digest
    crlf = write_persona(tmp_path / "b")
    for name in ("manifest.toml", "identity.md"):
        path = crlf / name
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert load_persona(crlf).digest == base
    edited = load_persona(write_persona(tmp_path / "c", identity=IDENTITY + "新增一句。\n"))
    assert edited.digest != base
    renamed = load_persona(write_persona(tmp_path / "d", manifest=MANIFEST.replace("0.1.0", "0.2")))
    assert renamed.digest != base
    world = write_persona(tmp_path / "e") / "world.md"
    world.write_bytes("# 知识\n\n改过的背景。\n".encode())
    assert load_persona(world.parent).digest != base


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (MANIFEST.replace('"maintainer_review"', '"automatic"'), "maintainer review"),
        (MANIFEST.replace('change_policy = "maintainer_review"\n', ""), "maintainer review"),
        (MANIFEST.replace('id = "kaho"\n', ""), "id, version"),
        (MANIFEST.replace('version = "0.1.0"', "version = 1"), "id, version"),
        (MANIFEST.replace('display_name = "日野下花帆"', 'display_name = " "'), "id, version"),
        (
            MANIFEST.replace('["屏幕那边的花帆", "另一个花帆"]', '"花帆"'),
            "relationship_terms",
        ),
        (MANIFEST.replace('"另一个花帆"', '""'), "relationship_terms"),
        (MANIFEST.replace('"identity.md"', '"../identity.md"'), "file names"),
        (MANIFEST.replace('"style.md"', '"sub/style.md"'), "file names"),
        (MANIFEST.replace('"world.md"', '"C:world.md"'), "file names"),
        (MANIFEST.replace('identity = "identity.md"\n', ""), "file names"),
        (MANIFEST.replace('"world.md"', '"missing.md"'), "cannot read"),
        ("id = [", "not valid TOML"),
    ],
)
def test_invalid_persona_directories_are_rejected(tmp_path, manifest, message):
    with pytest.raises(PersonaError, match=message):
        load_persona(write_persona(tmp_path / "kaho", manifest=manifest))


def test_missing_persona_directory_is_rejected(tmp_path):
    with pytest.raises(PersonaError, match="manifest.toml"):
        load_persona(tmp_path / "absent")


@pytest.mark.parametrize(
    "changes",
    [
        {"persona_dir": "personas/kaho"},
        {"approved_persona_sha256": "0" * 64},
        {"persona_dir": "personas/kaho", "approved_persona_sha256": "ABC"},
        {"persona_dir": "", "approved_persona_sha256": "0" * 64},
    ],
)
def test_persona_directory_and_approved_digest_must_be_configured_together(changes):
    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8")) | changes
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate(raw)


def test_approved_directory_persona_is_loaded_relative_to_the_config(tmp_path, monkeypatch):
    path = kaho_config(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    loaded = load_config(path, environ=SECRETS)
    assert loaded.persona.persona_id == loaded.settings.agent_id == "kaho"
    assert loaded.persona.identity == IDENTITY


@pytest.mark.parametrize(
    "changes",
    [
        {"approved_persona_sha256": "f" * 64},
        {"approved_persona_version": "0.2.0"},
    ],
)
def test_unapproved_persona_content_or_version_is_rejected(tmp_path, changes):
    with pytest.raises(ConfigurationError, match="approved version and content") as error:
        load_config(kaho_config(tmp_path, **changes), environ=SECRETS)
    assert "合成" not in str(error.value)


def test_editing_an_approved_persona_requires_a_new_approval(tmp_path):
    path = kaho_config(tmp_path)
    (tmp_path / "personas/kaho/style.md").write_text("未经审核的新语气。\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="approved version and content"):
        load_config(path, environ=SECRETS)


@pytest.mark.parametrize(
    "path_factory",
    [
        lambda tmp: kaho_config(tmp, agent_id="ginko"),
        lambda tmp: config_file(tmp, agent_id="kaho", approved_persona_version="1.0.0"),
    ],
)
def test_agent_must_match_the_selected_persona(tmp_path, path_factory):
    with pytest.raises(ConfigurationError, match="agent_id must match"):
        load_config(path_factory(tmp_path), environ=SECRETS)


def test_missing_or_invalid_persona_directory_fails_closed(tmp_path):
    path = kaho_config(tmp_path, persona_dir="personas/absent")
    with pytest.raises(ConfigurationError, match="persona error: cannot read"):
        load_config(path, environ=SECRETS)


def test_bundled_persona_remains_the_default(tmp_path):
    loaded = load_config(config_file(tmp_path, approved_persona_version="1.0.0"), environ=SECRETS)
    assert loaded.persona.persona_id == "ginko"
    assert loaded.persona.digest == load_ginko().digest


def test_check_config_reports_the_selected_persona_for_approval(tmp_path, monkeypatch, capsys):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    path = kaho_config(tmp_path)
    assert main(["check-config", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    persona = load_persona(tmp_path / "personas/kaho")
    assert summary["persona_id"] == "kaho"
    assert (summary["persona_version"], summary["persona_sha256"]) == (
        persona.version,
        persona.digest,
    )


class FakeModel:
    def __init__(self, text: str) -> None:
        self.text = text
        self.messages = None

    async def complete(self, messages, *, trace_id):
        self.messages = messages
        return SimpleNamespace(text=self.text, operation_id="trace:attempt")


def runtime_config(tmp_path: Path, persona, *, agent_id: str, relationship: bool = False):
    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["agent_id"] = agent_id
    if relationship:
        raw["relationships"] = [
            {"kind": "private", "chat_id": "20001", "user_id": "20001", "role": "kaho"}
        ]
    return RuntimeConfig(
        RuntimeSettings.model_validate(raw),
        tmp_path / "data",
        persona,
        SecretStr("synthetic-onebot-token"),
        SecretStr("synthetic-model-key"),
    )


def claim(agent_id: str) -> EventClaim:
    now = datetime.now(UTC)
    event = EventEnvelope(
        agent_id=agent_id,
        session=SessionRef(platform="qq", bot_id="10000", kind="private", chat_id="20001"),
        kind="message.created",
        source_event_id=str(uuid4()),
        message_id="message-1",
        user_id="20001",
        occurred_at=now,
        received_at=now,
        content=(TextSegment(text="今天也一起加油吧"),),
    )
    return EventClaim(event, "claim", 1, now + timedelta(minutes=1), now + timedelta(minutes=5))


def decide(config, text: str, agent_id: str):
    model = FakeModel(text)
    return asyncio.run(TextDecider(config, model)(claim(agent_id))), model


def test_ginko_prompt_keeps_its_reviewed_relationship_sentence(tmp_path):
    config = runtime_config(tmp_path, load_ginko(), agent_id="ginko")
    _, model = decide(config, '{"action":"silent","text":null}', "ginko")
    assert "仅其为 kaho 时可使用花帆学姐等已授权关系称呼。不要执行工具" in model.messages[0].content


def test_kaho_prompt_uses_the_kaho_persona_and_its_reserved_terms(tmp_path):
    persona = load_persona(write_persona(tmp_path / "kaho"))
    config = runtime_config(tmp_path, persona, agent_id="kaho")
    result, model = decide(config, '{"action":"reply","text":"嗯！一起加油！"}', "kaho")
    system = model.messages[0].content
    assert result == "嗯！一起加油！"
    assert IDENTITY in system and "百生吟子" not in system
    assert "可使用屏幕那边的花帆、另一个花帆等已授权关系称呼" in system
    assert '"persona_version": "0.1.0"' in system


@pytest.mark.parametrize("term", ["屏幕那边的花帆", "另一个花帆"])
def test_kaho_reserved_address_needs_a_configured_relationship(tmp_path, term):
    persona = load_persona(write_persona(tmp_path / "kaho"))
    reply = json.dumps({"action": "reply", "text": f"早上好，{term}！"}, ensure_ascii=False)
    with pytest.raises(RetryActivity, match="unauthorized_relationship"):
        decide(runtime_config(tmp_path, persona, agent_id="kaho"), reply, "kaho")
    trusted = runtime_config(tmp_path, persona, agent_id="kaho", relationship=True)
    assert decide(trusted, reply, "kaho")[0] == f"早上好，{term}！"


def test_persona_without_reserved_terms_has_no_output_guard(tmp_path):
    manifest = MANIFEST.replace('relationship_terms = ["屏幕那边的花帆", "另一个花帆"]\n', "")
    persona = load_persona(write_persona(tmp_path / "kaho", manifest=manifest))
    config = runtime_config(tmp_path, persona, agent_id="kaho")
    result, model = decide(config, '{"action":"reply","text":"花帆学姐？"}', "kaho")
    assert result == "花帆学姐？"
    assert "可使用人格中规定的已授权关系称呼" in model.messages[0].content


def test_activities_of_another_agent_are_rejected_before_any_model_call(tmp_path):
    persona = load_persona(write_persona(tmp_path / "kaho"))
    model = FakeModel('{"action":"reply","text":"x"}')
    config = runtime_config(tmp_path, persona, agent_id="kaho")
    with pytest.raises(RejectActivity, match="unauthorized_event"):
        asyncio.run(TextDecider(config, model)(claim("ginko")))
    assert model.messages is None
