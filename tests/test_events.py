from uuid import uuid4

import pytest
from pydantic import ValidationError

from ginko.core.events import EventEnvelope, SessionRef


def test_redelivery_with_new_trace_has_same_business_key(event):
    redelivery = event.model_copy(update={"event_id": uuid4(), "trace_id": uuid4()})
    assert redelivery.dedupe_key == event.dedupe_key
    assert redelivery.event_id != event.event_id


@pytest.mark.parametrize(
    "change",
    [
        {"bot_id": "another-bot"},
        {"kind": "group"},
        {"thread_id": "topic:1"},
        {"platform": "telegram"},
    ],
)
def test_identical_native_ids_stay_separate_across_namespaces(event, change):
    session = event.session.model_copy(update=change)
    assert session.key != event.session.key
    assert event.model_copy(update={"session": session}).dedupe_key != event.dedupe_key


def test_session_keys_cannot_collide_on_delimiters():
    first = SessionRef(platform="a:b", bot_id="c", kind="private", chat_id="d")
    second = SessionRef(platform="a", bot_id="b:c", kind="private", chat_id="d")
    assert first.key != second.key


def test_edit_and_recall_are_not_duplicates_of_creation(event):
    edit = event.model_copy(update={"kind": "message.edited"})
    recall = event.model_copy(update={"kind": "message.recalled", "content": ()})
    assert len({event.dedupe_key, edit.dedupe_key, recall.dedupe_key}) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"occurred_at": "2026-09-22T09:00:00"},
        {"raw": object()},
        {"content": []},
        {"kind": "message.recalled"},
    ],
)
def test_malformed_platform_data_is_rejected(event, change):
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event.model_dump() | change)
