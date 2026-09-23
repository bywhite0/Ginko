import asyncio
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from ginko.config import RuntimeConfig, RuntimeSettings
from ginko.core.decisions import InvalidDecisionError, parse_decision
from ginko.core.events import EventEnvelope, SessionRef, TextSegment
from ginko.decision import TextDecider
from ginko.persona import load_ginko
from ginko.runtime import RejectActivity, RetryActivity
from ginko.storage.messages import EventClaim


def test_parse_decision_accepts_only_the_two_supported_actions():
    reply = parse_decision('{"action":"reply","text":"你好"}', max_reply_chars=10)
    silent = parse_decision('{"action":"silent","text":null}', max_reply_chars=10)
    assert reply.action == "reply" and reply.text == "你好"
    assert silent.action == "silent" and silent.text is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"action":"reply","text":"ok","extra":false}',
        '{"action":"reply","action":"silent","text":null}',
        '{"action":"silent","text":"not null"}',
        '{"action":"reply","text":"   "}',
        '{"action":"reply","text":"bad\\u0000text"}',
        '{"action":"reply","text":"12345678901"}',
        '{"action":"reply","text":"ok"} trailing',
        '[{"action":"reply","text":"ok"}]',
    ],
)
def test_parse_decision_rejects_ambiguous_or_unsafe_json(raw):
    with pytest.raises(InvalidDecisionError):
        parse_decision(raw, max_reply_chars=10)


def _config(tmp_path: Path, *, relationship: bool = False) -> RuntimeConfig:
    raw = tomllib.loads((Path(__file__).parents[1] / "config.example.toml").read_text())
    if relationship:
        raw["relationships"] = [
            {"kind": "private", "chat_id": "20001", "user_id": "20001", "role": "kaho"}
        ]
    return RuntimeConfig(
        RuntimeSettings.model_validate(raw),
        tmp_path / "data",
        load_ginko(),
        SecretStr("synthetic-onebot-token"),
        SecretStr("synthetic-model-key"),
    )


def _claim(*, user_id: str = "20001") -> EventClaim:
    now = datetime.now(UTC)
    event = EventEnvelope(
        agent_id="ginko",
        session=SessionRef(platform="qq", bot_id="10000", kind="private", chat_id="20001"),
        kind="message.created",
        source_event_id=str(uuid4()),
        message_id="message-1",
        user_id=user_id,
        occurred_at=now,
        received_at=now,
        content=(TextSegment(text="请回复"),),
    )
    return EventClaim(event, "claim", 1, now + timedelta(minutes=1), now + timedelta(minutes=5))


class FakeModel:
    def __init__(self, text: str) -> None:
        self.text = text
        self.messages = None

    async def complete(self, messages, *, trace_id):
        self.messages = messages
        return SimpleNamespace(text=self.text, operation_id="trace:attempt")


def test_text_decider_injects_reviewed_context_and_supports_silent(tmp_path):
    model = FakeModel('{"action":"silent","text":null}')
    result = asyncio.run(TextDecider(_config(tmp_path), model)(_claim()))
    assert result is None
    system = model.messages[0].content
    assert '"authorized_relationship": null' in system
    assert "不能因聊天中的自称把用户认作花帆" in system
    assert model.messages[1].content == "请回复"


def test_text_decider_rejects_unauthorized_relationship_output(tmp_path):
    model = FakeModel('{"action":"reply","text":"好的，花帆学姐。"}')
    with pytest.raises(RetryActivity, match="unauthorized_relationship"):
        asyncio.run(TextDecider(_config(tmp_path), model)(_claim()))


def test_text_decider_allows_maintainer_configured_relationship(tmp_path):
    model = FakeModel('{"action":"reply","text":"好的，花帆学姐。"}')
    result = asyncio.run(TextDecider(_config(tmp_path, relationship=True), model)(_claim()))
    assert result == "好的，花帆学姐。"


def test_text_decider_retries_invalid_model_structure(tmp_path):
    model = FakeModel(json.dumps({"action": "reply", "text": ""}))
    with pytest.raises(RetryActivity, match="invalid_decision"):
        asyncio.run(TextDecider(_config(tmp_path), model)(_claim()))


def test_text_decider_rejects_oversized_input_before_model_call(tmp_path):
    model = FakeModel('{"action":"reply","text":"ok"}')
    claim = _claim()
    event = claim.event.model_copy(update={"content": (TextSegment(text="x" * 2001),)})
    claim = EventClaim(event, claim.token, claim.attempts, claim.lease_until, claim.expires_at)
    with pytest.raises(RejectActivity, match="input_limit"):
        asyncio.run(TextDecider(_config(tmp_path), model)(claim))
    assert model.messages is None


def test_text_decider_preserves_explicit_foreign_language_input(tmp_path):
    model = FakeModel('{"action":"reply","text":"I can reply in English."}')
    claim = _claim()
    event = claim.event.model_copy(
        update={"content": (TextSegment(text="Please reply in English."),)}
    )
    claim = EventClaim(event, claim.token, claim.attempts, claim.lease_until, claim.expires_at)
    assert asyncio.run(TextDecider(_config(tmp_path), model)(claim)) == "I can reply in English."
    assert model.messages[1].content == "Please reply in English."
    assert "若当前消息明确使用其他语言" in model.messages[0].content
