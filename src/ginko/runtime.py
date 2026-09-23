"""One cooperative activity worker and one sender, backed by durable SQLite state."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from ginko.adapters.onebot import OneBotIngress
from ginko.config import RuntimeConfig
from ginko.instance import InstanceLock
from ginko.storage.database import Database
from ginko.storage.messages import (
    Delivery,
    DeliveryRateLimited,
    EventClaim,
    MessageStore,
    PermanentDeliveryError,
    StaleClaimError,
)

logger = logging.getLogger(__name__)
Decision = Callable[[EventClaim], Awaitable[str | None]]
Send = Callable[[Delivery], Awaitable[str]]


class RetryActivity(Exception):
    """Retry only after the persisted lease, within the original attempts and deadline."""


class RejectActivity(Exception):
    """A definite decision rejection that must not be retried."""


class Runtime:
    def __init__(self, config: RuntimeConfig, decide: Decision, send: Send) -> None:
        self.config = config
        self.decide = decide
        self.send = send
        self.lock = InstanceLock(config.data_dir)
        self.database: Database | None = None
        self.store: MessageStore
        self.ingress: OneBotIngress
        self._wake_worker = asyncio.Event()
        self._wake_sender = asyncio.Event()
        self._connected = False
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._stopping = False
        self._stop_task: asyncio.Task | None = None
        self._failed = asyncio.Event()
        self._failure: BaseException | None = None

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("a runtime can only be started once")
        self.lock.acquire()
        try:
            self.database = Database(self.config.data_dir / "ginko.sqlite3")
            self.store = MessageStore(self.database)
            recovered = self.store.recover_interrupted_deliveries(
                agent_id=self.config.settings.agent_id
            )
            logger.info("recovered_unknown_deliveries count=%d", recovered)
            self.ingress = OneBotIngress(self.config.settings, self.store, self._wake_worker.set)
            self._tasks = [
                asyncio.create_task(self._worker(), name="ginko-worker"),
                asyncio.create_task(self._sender(), name="ginko-sender"),
            ]
            for task in self._tasks:
                task.add_done_callback(self._task_done)
            self._started = True
        except BaseException:
            if self.database is not None:
                self.database.close()
                self.database = None
            self.lock.release()
            raise

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._wake_sender.set()

    def _task_done(self, task: asyncio.Task) -> None:
        if self._stopping or self._failed.is_set():
            return
        self.fail(
            (RuntimeError("runtime task was cancelled") if task.cancelled() else task.exception())
            or RuntimeError("runtime task exited unexpectedly")
        )

    def fail(self, error: BaseException) -> None:
        if self._stopping or self._failed.is_set():
            return
        self._failure = error
        self.ingress.close()
        self._failed.set()

    async def wait_failed(self) -> None:
        await self._failed.wait()
        # Handler exceptions can contain payloads, URLs or credentials; do not display them.
        raise RuntimeError(f"runtime task failed: {type(self._failure).__name__}") from None

    @staticmethod
    async def _wait(wake: asyncio.Event) -> None:
        # Notifications reduce latency; periodic polling recovers a lost wakeup.
        with suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=0.5)

    async def _worker(self) -> None:
        settings = self.config.settings
        while not self._stopping and not self._failed.is_set():
            self._wake_worker.clear()
            claim = self.store.claim(
                datetime.now(UTC),
                lease_seconds=settings.activity.lease_seconds,
                agent_id=settings.agent_id,
            )
            if claim is None:
                await self._wait(self._wake_worker)
                continue
            try:
                if not settings.allows(claim.event.session):
                    self.store.fail(claim, now=datetime.now(UTC))
                    continue
                # One timeout bounds the entire decision, including model retries.
                seconds = (claim.lease_until - datetime.now(UTC)).total_seconds() - 0.5
                async with asyncio.timeout(max(0, seconds)) as deadline:
                    reply = await self.decide(claim)
                # Cancellation suppression must never permit a late or shutdown commit.
                if self._stopping or self._failed.is_set() or deadline.expired():
                    continue
                self.store.complete(claim, now=datetime.now(UTC), reply=reply)
                self._wake_sender.set()
            except RejectActivity:
                with suppress(StaleClaimError):
                    self.store.fail(claim, now=datetime.now(UTC))
            except (TimeoutError, RetryActivity, StaleClaimError) as error:
                logger.warning(
                    "activity_incomplete trace_id=%s attempts=%d cause=%s",
                    claim.event.trace_id,
                    claim.attempts,
                    type(error).__name__,
                )
            await asyncio.sleep(0)

    async def _sender(self) -> None:
        settings = self.config.settings
        while not self._stopping and not self._failed.is_set():
            self._wake_sender.clear()
            delivery = (
                self.store.claim_delivery(datetime.now(UTC), agent_id=settings.agent_id)
                if self._connected
                else None
            )
            if delivery is None:
                await self._wait(self._wake_sender)
                continue
            if not settings.allows(delivery.session):
                self.store.fail_delivery(delivery.delivery_id, reason="unauthorized_target")
                continue
            remaining = (delivery.expires_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                self.store.fail_delivery(delivery.delivery_id, reason="delivery_deadline")
                continue
            try:
                async with asyncio.timeout(
                    min(settings.onebot.api_timeout_seconds, remaining)
                ) as deadline:
                    receipt = await self.send(delivery)
                if deadline.expired():
                    self.store.mark_delivery_unknown(delivery.delivery_id, reason="timeout")
                else:
                    self.store.confirm_delivery(delivery.delivery_id, receipt)
            except DeliveryRateLimited as error:
                delay = max(0.5, min(error.retry_after_seconds, remaining))
                retry_at = datetime.now(UTC) + timedelta(seconds=delay)
                self.store.defer_delivery(
                    delivery.delivery_id, retry_at=retry_at, reason="rate_limited"
                )
                self._wake_sender.set()
            except PermanentDeliveryError as error:
                self.store.fail_delivery(delivery.delivery_id, reason=error.code)
            except TimeoutError:
                self.store.mark_delivery_unknown(delivery.delivery_id, reason="timeout")
            except BaseException:
                # Cancellation and unexpected transport failures are equally ambiguous.
                self.store.mark_delivery_unknown(delivery.delivery_id, reason="unknown_error")
                raise
            await asyncio.sleep(0)

    async def stop(self) -> None:
        if not self._started:
            return
        if self._stop_task is None:
            self._stopping = True
            self.ingress.close()
            self._stop_task = asyncio.create_task(self._finish_stop(), name="ginko-stopping")
        # Concurrent or cancelled callers must not close storage ahead of running tasks.
        await asyncio.shield(self._stop_task)

    async def _finish_stop(self) -> None:
        # No further claims or commits. In-flight sends record unknown before the DB closes.
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def close(self) -> None:
        await self.stop()
        try:
            if self.database is not None:
                self.database.close()
                self.database = None
        finally:
            self.lock.release()

    async def __aenter__(self) -> "Runtime":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
