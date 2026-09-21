import asyncio
import sqlite3
import tomllib
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    PrivateMessageEvent,
)
from pydantic import SecretStr

from ginko.adapters.onebot import OneBotAdapter, OneBotIngress, normalize_message, register_ingress
from ginko.config import RuntimeSettings
from ginko.storage.database import Database
from ginko.storage.messages import MessageStore


@pytest.fixture
def settings():
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    raw = tomllib.loads(example.read_text(encoding="utf-8"))
    raw["allowed_sessions"].append({"kind": "group", "chat_id": "30001"})
    return RuntimeSettings.model_validate(raw)


@pytest.fixture
def payload(now):
    return {
        "time": int(now.timestamp()),
        "self_id": 10000,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 10,
        "user_id": 20001,
        "message": [{"type": "text", "data": {"text": "你好，吟子"}}],
        "raw_message": "this field is not trusted for normalization",
        "font": 0,
        "sender": {"user_id": 20001, "nickname": "Synthetic test user"},
    }


def parse(payload):
    model = GroupMessageEvent if payload["message_type"] == "group" else PrivateMessageEvent
    return model.model_validate(deepcopy(payload))


def test_private_text_preserves_native_id_but_generates_distinct_local_and_trace_ids(
    payload, settings, now
):
    first = normalize_message(parse(payload), settings, received_at=now)
    second = normalize_message(parse(payload), settings, received_at=now + timedelta(seconds=1))
    assert first.source_event_id == first.message_id == "10"
    assert first.dedupe_key == second.dedupe_key
    assert first.event_id != second.event_id
    assert first.trace_id != second.trace_id
    assert first.event_id != first.trace_id
    assert first.content[0].text == "你好，吟子"
    assert first.session.kind == "private"
    assert first.occurred_at == now


def test_group_requires_own_mention_even_when_sdk_has_trimmed_it(payload, settings, now):
    payload.update(message_type="group", sub_type="normal", group_id=30001)
    payload["message"].insert(0, {"type": "at", "data": {"qq": "10000"}})
    event = parse(payload)
    event.message = Message("你好，吟子")
    normalized = normalize_message(event, settings, received_at=now)
    assert normalized.session.kind == "group"
    assert normalized.session.chat_id == "30001"
    assert normalized.user_id == "20001"
    assert normalized.content[0].text == "你好，吟子"
    unmentioned = parse(payload | {"message": "没有明确提及机器人", "to_me": True})
    assert normalize_message(unmentioned, settings, received_at=now) is None


@pytest.mark.parametrize(
    "override",
    [
        {"self_id": 10001},
        {"user_id": 10000},
        {"user_id": 20002},
        {"user_id": -1},
        {"sub_type": "group"},
        {"message": "   "},
        {"message": "x" * 2001},
        {"time": 10**30},
        {"message": [{"type": "at", "data": {"qq": "all"}}]},
        {
            "message": [
                {"type": "text", "data": {"text": "text"}},
                {"type": "image", "data": {"file": "private.png"}},
            ]
        },
        {
            "message": [
                {"type": "reply", "data": {"id": "9"}},
                {"type": "text", "data": {"text": "quoted"}},
            ]
        },
        {
            "message": [
                {"type": "at", "data": {"qq": "20002"}},
                {"type": "text", "data": {"text": "other person"}},
            ]
        },
    ],
)
def test_unadmitted_messages_never_enter_inbox_or_wake_worker(
    payload, settings, database, now, override
):
    wakes = []
    ingress = OneBotIngress(settings, MessageStore(database), lambda: wakes.append(True))
    assert ingress.receive(parse(payload | override), received_at=now) is None
    assert not wakes
    assert database.connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


def test_anonymous_and_unlisted_groups_are_ignored(payload, settings, now):
    payload.update(message_type="group", sub_type="normal", group_id=30002)
    payload["message"].insert(0, {"type": "at", "data": {"qq": "10000"}})
    assert normalize_message(parse(payload), settings, received_at=now) is None
    payload.update(group_id=30001, anonymous={"id": 1, "name": "anonymous", "flag": "opaque"})
    assert normalize_message(parse(payload), settings, received_at=now) is None


def test_cq_escaped_text_is_literal_and_negative_native_message_ids_are_preserved(
    payload, settings, now
):
    payload.update(message_id=-10, message="&#91;CQ:at,qq=10000&#93; is literal text")
    normalized = normalize_message(parse(payload), settings, received_at=now)
    assert normalized.content[0].text == "[CQ:at,qq=10000] is literal text"
    assert normalized.source_event_id == "-10"


def test_native_message_id_does_not_collapse_private_and_group_sessions(
    payload, settings, database, now
):
    ingress = OneBotIngress(settings, MessageStore(database), lambda: None)
    private_id = ingress.receive(parse(payload), received_at=now)
    payload.update(message_type="group", sub_type="normal", group_id=30001)
    payload["message"].insert(0, {"type": "at", "data": {"qq": "10000"}})
    group_id = ingress.receive(parse(payload), received_at=now)
    assert private_id != group_id
    assert database.connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2


def test_ingress_commits_before_wake_and_redelivery_preserves_attempts_and_deadline(
    payload, settings, tmp_path, now
):
    path = tmp_path / "ingress.sqlite3"
    observed = []

    def wake():
        # A separate connection can already see committed work when notified.
        with Database(path) as reopened:
            observed.append(reopened.connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0])

    with Database(path) as database:
        store = MessageStore(database)
        ingress = OneBotIngress(settings, store, wake)
        first = ingress.receive(parse(payload), received_at=now)
        claim = store.claim(now, lease_seconds=60)
        original_expiry = database.connection.execute("SELECT expires_at FROM inbox").fetchone()[0]
        repeated = ingress.receive(parse(payload), received_at=now + timedelta(seconds=30))
        assert first == repeated == claim.event.event_id
        row = database.connection.execute("SELECT * FROM inbox").fetchone()
        assert row["attempts"] == 1
        assert row["max_attempts"] == settings.activity.max_attempts
        assert row["expires_at"] == original_expiry
        assert observed == [1, 1]
    with Database(path) as reopened:
        recovered = MessageStore(reopened).claim(now + timedelta(seconds=61))
        assert recovered.event.event_id == first
        assert recovered.attempts == 2


def test_failed_database_commit_does_not_wake_worker(payload, settings, database, now):
    wakes = []
    database.connection.execute(
        "CREATE TRIGGER reject_inbox BEFORE INSERT ON inbox "
        "BEGIN SELECT RAISE(ABORT, 'disk fault'); END"
    )
    ingress = OneBotIngress(settings, MessageStore(database), lambda: wakes.append(True))
    with pytest.raises(sqlite3.IntegrityError, match="disk fault"):
        ingress.receive(parse(payload), received_at=now)
    assert not wakes
    assert database.connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


def test_wakeup_failure_does_not_lose_persisted_work(payload, settings, database, now):
    def failed_wake():
        raise RuntimeError("wake lost")

    store = MessageStore(database)
    ingress = OneBotIngress(settings, store, failed_wake)
    with pytest.raises(RuntimeError, match="wake lost"):
        ingress.receive(parse(payload), received_at=now)
    assert store.claim(now).event.source_event_id == "10"


@pytest.mark.parametrize("delivery_state", ["sent", "unknown"])
def test_redelivery_after_decision_does_not_recreate_outbox(
    payload, settings, database, now, delivery_state
):
    store = MessageStore(database)
    ingress = OneBotIngress(settings, store, lambda: None)
    first_id = ingress.receive(parse(payload), received_at=now)
    claim = store.claim(now)
    delivery_id = store.complete(claim, now=now, reply="synthetic test response")
    assert store.claim_delivery().delivery_id == delivery_id
    if delivery_state == "sent":
        store.confirm_delivery(delivery_id, "test-receipt-1")
    else:
        store.mark_delivery_unknown(delivery_id)
    later = now + timedelta(minutes=10)
    assert ingress.receive(parse(payload), received_at=later) == first_id
    assert store.claim(later) is None
    assert store.claim_delivery() is None
    assert store.delivery_status(delivery_id) == delivery_state
    assert database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


@pytest.mark.parametrize("connected_bot", ["10000", "10001"])
def test_nonebot_dispatch_checks_connection_identity(payload, settings, database, connected_bot):
    nonebot.init(driver="~fastapi", log_level="ERROR")
    adapter = OneBotAdapter(
        nonebot.get_driver(), settings=settings, access_token=SecretStr("synthetic-token")
    )
    bot = Bot(adapter, connected_bot)
    store = MessageStore(database)
    wakes = []
    matcher = register_ingress(OneBotIngress(settings, store, lambda: wakes.append(True)))
    try:
        event = adapter.json_to_event(deepcopy(payload))
        asyncio.run(bot.handle_event(event))
    finally:
        matcher.destroy()
    expected = int(connected_bot == "10000")
    assert len(wakes) == expected
    assert database.connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == expected


@pytest.mark.parametrize("user_id", [20001, 20002])
def test_rejected_quotes_never_reach_sdk_network_preprocessing(payload, settings, user_id):
    nonebot.init(driver="~fastapi", log_level="ERROR")
    adapter = OneBotAdapter(
        nonebot.get_driver(), settings=settings, access_token=SecretStr("synthetic-token")
    )
    calls = []

    class TrackingBot(Bot):
        async def call_api(self, api, **data):
            calls.append(api)
            raise AssertionError("rejected messages must not query platform content")

    bot = TrackingBot(adapter, "10000")
    payload.update(user_id=user_id)
    payload["message"].insert(0, {"type": "reply", "data": {"id": "9"}})

    async def receive():
        if event := adapter.json_to_event(deepcopy(payload)):
            await bot.handle_event(event)

    asyncio.run(receive())
    assert not calls
    assert adapter.json_to_event(deepcopy(payload)) is None


def test_admission_adapter_preserves_api_receipts(settings):
    nonebot.init(driver="~fastapi", log_level="ERROR")
    adapter = OneBotAdapter(
        nonebot.get_driver(), settings=settings, access_token=SecretStr("synthetic-token")
    )

    async def receive_receipt():
        sequence = adapter._result_store.get_seq()
        receipt = {"echo": str(sequence), "status": "ok", "retcode": 0, "data": {"message_id": 42}}
        pending = asyncio.create_task(adapter._result_store.fetch(sequence, 1))
        await asyncio.sleep(0)
        assert adapter.json_to_event(receipt) is None
        assert await pending == receipt

    asyncio.run(receive_receipt())
