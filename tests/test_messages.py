from datetime import timedelta
from uuid import uuid4

import pytest

from ginko.storage.database import Database
from ginko.storage.messages import DeliveryStateError, MessageStore, StaleClaimError


def add(store, event, now, **kwargs):
    return store.ingest(event, expires_at=now + timedelta(hours=1), **kwargs)


def test_redelivery_and_reopening_produce_one_decision(tmp_path, event, now):
    path = tmp_path / "persistent.sqlite3"
    with Database(path) as database:
        store = MessageStore(database)
        first = add(store, event, now)
        duplicate = event.model_copy(update={"event_id": uuid4(), "trace_id": uuid4()})
        assert add(store, duplicate, now) == first
    with Database(path) as database:
        store = MessageStore(database)
        claim = store.claim(now)
        assert claim.event.event_id == first
        delivery_id = store.complete(claim, now=now, reply="你好")
        assert store.event_status(first) == "done"
        assert store.claim(now) is None
        delivery = store.claim_delivery(now)
        assert delivery.delivery_id == delivery_id
        store.confirm_delivery(delivery_id, "receipt-1")
        store.confirm_delivery(delivery_id, "receipt-1")
        assert store.claim_delivery() is None
        with pytest.raises(DeliveryStateError):
            store.confirm_delivery(delivery_id, "different-receipt")


def test_expired_worker_is_fenced_from_new_claim(database, event, now):
    store = MessageStore(database)
    add(store, event, now)
    old = store.claim(now, lease_seconds=10)
    later = now + timedelta(seconds=11)
    successor = store.claim(later)
    assert successor.attempts == 2
    with pytest.raises(StaleClaimError):
        store.complete(old, now=later, reply="obsolete reply")
    assert store.claim_delivery() is None
    store.complete(successor, now=later, reply="current reply")
    assert store.claim_delivery(later).text == "current reply"


def test_attempt_counter_survives_reopening_and_redelivery(tmp_path, event, now):
    path = tmp_path / "attempts.sqlite3"
    with Database(path) as database:
        store = MessageStore(database)
        add(store, event, now, max_attempts=2)
        assert store.claim(now, lease_seconds=1).attempts == 1
    with Database(path) as database:
        store = MessageStore(database)
        add(store, event, now, max_attempts=99)
        assert store.claim(now + timedelta(seconds=2), lease_seconds=1).attempts == 2
        assert store.claim(now + timedelta(seconds=4)) is None
        assert store.event_status(event.event_id) == "failed"


def test_expired_activity_is_not_replayed(database, event, now):
    store = MessageStore(database)
    store.ingest(event, expires_at=now + timedelta(seconds=1))
    assert store.claim(now + timedelta(seconds=2)) is None
    assert store.event_status(event.event_id) == "failed"


def test_one_active_claim_per_agent_other_agent_can_progress(database, event, now):
    store = MessageStore(database)
    add(store, event, now)
    second = event.model_copy(update={"event_id": uuid4(), "source_event_id": "native:2"})
    other = event.model_copy(update={"event_id": uuid4(), "agent_id": "other"})
    add(store, second, now)
    add(store, other, now)
    first_claim = store.claim(now)
    assert first_claim.event.event_id == event.event_id
    assert store.claim(now).event.agent_id == "other"
    assert store.claim(now) is None
    store.complete(first_claim, now=now, reply=None)
    assert store.claim(now).event.event_id == second.event_id


def test_interrupted_send_becomes_unknown_and_is_never_auto_retried(tmp_path, event, now):
    path = tmp_path / "outbound.sqlite3"
    with Database(path) as database:
        store = MessageStore(database)
        add(store, event, now)
        delivery_id = store.complete(store.claim(now), now=now, reply="one message")
        store.claim_delivery(now)  # Simulate exit after sending, before receipt.
    with Database(path) as database:
        store = MessageStore(database)
        assert store.recover_interrupted_deliveries() == 1
        assert store.delivery_status(delivery_id) == "unknown"
        assert store.claim_delivery() is None
        store.confirm_delivery(delivery_id, "late-receipt")
        assert store.delivery_status(delivery_id) == "sent"


def test_delivery_timeout_and_invalid_confirmation(database, event, now):
    store = MessageStore(database)
    add(store, event, now)
    delivery_id = store.complete(store.claim(now), now=now, reply="message")
    with pytest.raises(DeliveryStateError):
        store.confirm_delivery(delivery_id, "not-sent-yet")
    store.claim_delivery(now)
    store.mark_delivery_unknown(delivery_id)
    assert store.claim_delivery() is None


def test_silence_completes_without_outbound_message(database, event, now):
    store = MessageStore(database)
    add(store, event, now)
    assert store.complete(store.claim(now), now=now, reply=None) is None
    assert store.event_status(event.event_id) == "done"
    assert store.claim_delivery() is None


def test_rate_limited_delivery_waits_durably_and_retries_with_new_attempt(database, event, now):
    store = MessageStore(database)
    add(store, event, now)
    delivery_id = store.complete(store.claim(now), now=now, reply="message")
    first = store.claim_delivery(now)
    assert first.attempts == 1
    assert store.defer_delivery(delivery_id, retry_at=now + timedelta(seconds=10)) is True
    assert store.delivery_status(delivery_id) == "pending"
    assert store.claim_delivery(now + timedelta(seconds=9)) is None
    second = store.claim_delivery(now + timedelta(seconds=10))
    assert second.attempts == 2
    store.confirm_delivery(delivery_id, "receipt-2")
    assert store.delivery_record(delivery_id).last_error is None


def test_rate_limit_past_deadline_becomes_permanent_failure(database, event, now):
    store = MessageStore(database)
    add(store, event, now)
    delivery_id = store.complete(store.claim(now), now=now, reply="message")
    store.claim_delivery(now)
    assert store.defer_delivery(delivery_id, retry_at=now + timedelta(hours=1)) is False
    assert store.delivery_status(delivery_id) == "failed"
    assert store.delivery_record(delivery_id).last_error == "delivery_deadline"


def test_expired_pending_delivery_is_failed_without_platform_call(database, event, now):
    store = MessageStore(database)
    add(store, event, now, max_attempts=1)
    delivery_id = store.complete(store.claim(now), now=now, reply="message")
    assert store.claim_delivery(now + timedelta(hours=1, seconds=1)) is None
    assert store.delivery_status(delivery_id) == "failed"


def test_unknown_delivery_can_be_queried_and_manually_failed(database, event, now):
    store = MessageStore(database)
    add(store, event, now)
    delivery_id = store.complete(store.claim(now), now=now, reply="message")
    store.claim_delivery(now)
    store.mark_delivery_unknown(delivery_id, reason="transport_error")
    records = store.list_delivery_records(status="unknown")
    assert [record.delivery_id for record in records] == [delivery_id]
    assert records[0].last_error == "transport_error"
    store.reconcile_delivery(delivery_id, "failed")
    assert store.delivery_status(delivery_id) == "failed"
    assert store.claim_delivery(now) is None


def test_unknown_delivery_can_be_manually_confirmed_with_receipt(database, event, now):
    store = MessageStore(database)
    add(store, event, now)
    delivery_id = store.complete(store.claim(now), now=now, reply="message")
    store.claim_delivery(now)
    store.mark_delivery_unknown(delivery_id)
    store.reconcile_delivery(delivery_id, "sent", platform_message_id="manual-receipt")
    assert store.delivery_status(delivery_id) == "sent"
