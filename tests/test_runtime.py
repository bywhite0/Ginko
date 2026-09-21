import asyncio
import subprocess
import sys
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from ginko.config import RuntimeConfig, RuntimeSettings
from ginko.core.events import EventEnvelope, SessionRef, TextSegment
from ginko.instance import InstanceLock, InstanceRunningError
from ginko.persona import load_ginko
from ginko.runtime import RejectActivity, RetryActivity, Runtime
from ginko.storage.database import Database
from ginko.storage.messages import MessageStore, StaleClaimError


@pytest.fixture
def config(tmp_path):
    raw = tomllib.loads((Path(__file__).parents[1] / "config.example.toml").read_text())
    return RuntimeConfig(
        RuntimeSettings.model_validate(raw),
        tmp_path / "data",
        load_ginko(),
        SecretStr("synthetic-token"),
        SecretStr("synthetic-key"),
    )


def incoming(**updates):
    now = datetime.now(UTC)
    return EventEnvelope(
        agent_id="ginko",
        session=SessionRef(platform="qq", bot_id="10000", kind="private", chat_id="20001"),
        kind="message.created",
        source_event_id=str(uuid4()),
        message_id="1",
        user_id="20001",
        occurred_at=now,
        received_at=now,
        content=(TextSegment(text="synthetic lifecycle probe"),),
    ).model_copy(update=updates)


def ingest(store, event=None, *, seconds=30, max_attempts=2):
    event = event or incoming()
    store.ingest(
        event,
        expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        max_attempts=max_attempts,
    )
    return event


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.01)


async def reply(claim):
    return "synthetic reply"


async def receipt(delivery):
    return "synthetic-receipt"


def test_os_lock_blocks_second_process_and_recovers_after_crash(tmp_path):
    code = (
        "import sys; from pathlib import Path; from ginko.instance import InstanceLock; "
        "lock = InstanceLock(Path(sys.argv[1])); lock.acquire(); "
        "print('locked', flush=True); sys.stdin.read(1)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(InstanceRunningError), InstanceLock(tmp_path / "."):
            pytest.fail("second process acquired the lock")
        child.kill()
        child.communicate(timeout=10)
        with InstanceLock(tmp_path):
            assert (tmp_path / "ginko.lock").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_second_runtime_cannot_recover_live_sender_and_lock_is_released(config):
    async def run():
        async with Runtime(config, reply, receipt) as first:
            event = ingest(first.store)
            claim = first.store.claim(datetime.now(UTC))
            delivery_id = first.store.complete(claim, now=datetime.now(UTC), reply="hello")
            first.store.claim_delivery()
            with pytest.raises(InstanceRunningError):
                await Runtime(config, reply, receipt).start()
            assert first.store.delivery_status(delivery_id) == "sending"
            assert first.store.event_status(event.event_id) == "done"
        async with Runtime(config, reply, receipt) as successor:
            assert successor.store.delivery_status(delivery_id) == "unknown"
            successor.set_connected(True)
            await asyncio.sleep(0.02)
            assert successor.store.claim_delivery() is None

    asyncio.run(run())


def test_startup_failure_releases_lock(config):
    path = config.data_dir / "ginko.sqlite3"
    with Database(path) as database:
        database.connection.execute("PRAGMA user_version = 999")

    async def run():
        with pytest.raises(ValueError, match="unsupported database schema"):
            await Runtime(config, reply, receipt).start()
        with InstanceLock(config.data_dir):
            pass

    asyncio.run(run())


def test_periodic_polling_recovers_lost_wake_and_only_sends_when_connected(config):
    async def run():
        async with Runtime(config, reply, receipt) as runtime:
            await asyncio.sleep(0)  # Worker has already checked the empty DB.
            event = ingest(runtime.store)  # Deliberately no notification.
            await until(lambda: runtime.store.event_status(event.event_id) == "done")
            row = runtime.database.connection.execute("SELECT * FROM outbox").fetchone()
            assert row["status"] == "pending"
            runtime.set_connected(True)
            await until(
                lambda: (
                    runtime.database.connection.execute("SELECT status FROM outbox").fetchone()[0]
                    == "sent"
                )
            )
            with pytest.raises(RuntimeError, match="only be started once"):
                await runtime.start()

    asyncio.run(run())


def test_single_worker_and_sender_do_not_block_ingress_or_each_other(config):
    async def run():
        deciding, sending, release_send = asyncio.Event(), asyncio.Event(), asyncio.Event()
        decisions, deliveries = [], []

        async def decide(claim):
            decisions.append(claim.event.event_id)
            deciding.set()
            await asyncio.sleep(0.02)
            return "reply"

        async def send(delivery):
            deliveries.append(delivery.event_id)
            sending.set()
            await release_send.wait()
            return str(delivery.delivery_id)

        async with Runtime(config, decide, send) as runtime:
            runtime.set_connected(True)
            first = ingest(runtime.store)
            await deciding.wait()
            second = ingest(runtime.store)
            await sending.wait()
            third = ingest(runtime.store)
            await until(lambda: runtime.store.event_status(third.event_id) == "done")
            assert decisions == [first.event_id, second.event_id, third.event_id]
            assert deliveries == [first.event_id]
            release_send.set()
            await until(lambda: len(deliveries) == 3)

    asyncio.run(run())


@pytest.mark.parametrize("suppress_cancel", [False, True])
def test_shutdown_never_commits_cancelled_decision(config, suppress_cancel):
    async def run():
        started = asyncio.Event()

        async def decide(claim):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not suppress_cancel:
                    raise
                return "cancelled but returning"

        runtime = Runtime(config, decide, receipt)
        await runtime.start()
        ingest(runtime.store, seconds=180)
        await started.wait()
        await runtime.close()
        assert not runtime.ingress.accepting
        await runtime.close()
        with Database(config.data_dir / "ginko.sqlite3") as database:
            row = database.connection.execute("SELECT * FROM inbox").fetchone()
            assert row["status"] == "processing"
            assert row["attempts"] == 1
            assert database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
            store = MessageStore(database)
            later = datetime.fromtimestamp(row["lease_until"] + 0.01, UTC)
            # Original deadline and attempt counter survive cancellation and a new connection.
            assert store.claim(later).attempts == 2

    asyncio.run(run())


@pytest.mark.parametrize("suppress_cancel", [False, True])
def test_whole_decision_deadline_rejects_late_results(config, suppress_cancel):
    async def decide(claim):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            if not suppress_cancel:
                raise
            return "too late"

    async def run():
        async with Runtime(config, decide, receipt) as runtime:
            event = ingest(runtime.store, seconds=0.7)
            await until(lambda: runtime.store.event_status(event.event_id) == "failed")
            assert (
                runtime.database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
                == 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("outcome", [RejectActivity, RetryActivity])
def test_definite_rejection_and_retry_keep_bounded_attempts(config, outcome):
    calls = []

    async def decide(claim):
        calls.append(claim.attempts)
        raise outcome()

    async def run():
        async with Runtime(config, decide, receipt) as runtime:
            event = ingest(runtime.store, seconds=0.7)
            await until(lambda: runtime.store.event_status(event.event_id) == "failed")
            assert calls == [1]
            assert (
                runtime.database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
                == 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "cancel", "unexpected"])
def test_inflight_send_is_unknown_after_interruption_and_is_not_retried(config, failure):
    async def run():
        started = asyncio.Event()
        sends = []

        async def send(delivery):
            sends.append(delivery.delivery_id)
            started.set()
            if failure == "timeout":
                raise TimeoutError()
            if failure == "unexpected":
                raise ValueError("must not appear in logs")
            await asyncio.Event().wait()

        async with Runtime(config, reply, send) as runtime:
            runtime.set_connected(True)
            ingest(runtime.store)
            await started.wait()
            if failure == "unexpected":
                with pytest.raises(RuntimeError, match="runtime task failed: ValueError"):
                    await runtime.wait_failed()
                assert not runtime.ingress.accepting
        async with Runtime(config, reply, receipt) as successor:
            successor.set_connected(True)
            assert successor.store.delivery_status(sends[0]) == "unknown"
            await asyncio.sleep(0.02)
            assert successor.store.claim_delivery() is None
            assert len(sends) == 1

    asyncio.run(run())


def test_runtime_applies_send_timeout_even_if_transport_suppresses_cancellation(config):
    async def send(delivery):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return "late-receipt"

    async def run():
        settings = config.settings.model_copy(
            update={
                "onebot": config.settings.onebot.model_copy(update={"api_timeout_seconds": 0.02})
            }
        )
        async with Runtime(replace(config, settings=settings), reply, send) as runtime:
            runtime.set_connected(True)
            ingest(runtime.store)
            await until(
                lambda: (
                    runtime.database.connection.execute("SELECT status FROM outbox").fetchone()
                    is not None
                    and runtime.database.connection.execute("SELECT status FROM outbox").fetchone()[
                        0
                    ]
                    == "unknown"
                )
            )

    asyncio.run(run())


def test_unexpected_worker_error_stops_admission_and_is_sanitized(config):
    async def decide(claim):
        raise ValueError("secret credential payload")

    async def run():
        async with Runtime(config, decide, receipt) as runtime:
            ingest(runtime.store)
            with pytest.raises(RuntimeError, match="^runtime task failed: ValueError$"):
                await runtime.wait_failed()
            assert not runtime.ingress.accepting

    asyncio.run(run())


def test_concurrent_and_cancelled_close_wait_for_inflight_cleanup_before_unlocking(config):
    async def run():
        sending, cleaning, allow_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def send(delivery):
            sending.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await allow_cleanup.wait()

        runtime = Runtime(config, reply, send)
        await runtime.start()
        runtime.set_connected(True)
        ingest(runtime.store)
        await sending.wait()
        first = asyncio.create_task(runtime.close())
        await cleaning.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.02)
        assert not second.done()
        with pytest.raises(InstanceRunningError), InstanceLock(config.data_dir):
            pytest.fail("lock released before in-flight cleanup")
        allow_cleanup.set()
        await second
        with InstanceLock(config.data_dir):
            pass
        with Database(config.data_dir / "ginko.sqlite3") as database:
            assert (
                database.connection.execute("SELECT status FROM outbox").fetchone()[0] == "unknown"
            )

    asyncio.run(run())


def test_runtime_never_processes_other_agents_or_revoked_sessions(config):
    async def run():
        async with Runtime(config, reply, receipt) as runtime:
            other = ingest(runtime.store, incoming(agent_id="other"))
            revoked = ingest(
                runtime.store,
                incoming(
                    session=SessionRef(
                        platform="qq", bot_id="10000", kind="private", chat_id="20002"
                    )
                ),
            )
            allowed = ingest(runtime.store)
            await until(lambda: runtime.store.event_status(allowed.event_id) == "done")
            assert runtime.store.event_status(other.event_id) == "pending"
            assert runtime.store.event_status(revoked.event_id) == "failed"

    asyncio.run(run())


def test_runtime_never_sends_other_agents_or_revoked_sessions(config):
    async def run():
        async with Runtime(config, reply, receipt) as runtime:
            ids = []
            for event in (
                incoming(agent_id="other"),
                incoming(
                    session=SessionRef(
                        platform="qq", bot_id="10000", kind="private", chat_id="20002"
                    )
                ),
                incoming(),
            ):
                ingest(runtime.store, event)
                claim = runtime.store.claim(datetime.now(UTC))
                ids.append(runtime.store.complete(claim, now=datetime.now(UTC), reply="reply"))
            runtime.set_connected(True)
            await until(lambda: runtime.store.delivery_status(ids[2]) == "sent")
            assert runtime.store.delivery_status(ids[0]) == "pending"
            assert runtime.store.delivery_status(ids[1]) == "failed"

    asyncio.run(run())


def test_failed_activity_uses_lease_token_and_deadline_fencing(database, event, now):
    store = MessageStore(database)
    store.ingest(event, expires_at=now + timedelta(seconds=60))
    first = store.claim(now, lease_seconds=1)
    assert first.lease_until == now + timedelta(seconds=1)
    assert first.expires_at == now + timedelta(seconds=60)
    later = now + timedelta(seconds=2)
    successor = store.claim(later)
    with pytest.raises(StaleClaimError):
        store.fail(first, now=later)
    assert store.event_status(event.event_id) == "processing"
    store.fail(successor, now=later)
    assert store.event_status(event.event_id) == "failed"
