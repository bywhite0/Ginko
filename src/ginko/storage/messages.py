"""Durable inbox leases and a deliberately conservative delivery lifecycle."""

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Literal
from uuid import UUID, uuid4, uuid5

from ginko.core.events import EventEnvelope, SessionRef
from ginko.storage.database import Database, timestamp


class StaleClaimError(RuntimeError):
    """A timed-out worker cannot commit over its successor."""


class DeliveryStateError(RuntimeError):
    """Delivery transitions must be explicit, including ambiguous results."""


class DeliveryRateLimited(DeliveryStateError):
    """The platform rejected an attempt temporarily and supplied a retry delay."""

    def __init__(self, retry_after_seconds: float) -> None:
        if (
            type(retry_after_seconds) not in (int, float)
            or not isfinite(retry_after_seconds)
            or retry_after_seconds < 0
        ):
            raise ValueError("retry delay must be finite and non-negative")
        self.retry_after_seconds = retry_after_seconds
        super().__init__("rate_limited")


class PermanentDeliveryError(DeliveryStateError):
    """The platform gave a definite rejection; retrying would repeat the failure."""

    def __init__(self, code: str = "permanent_failure") -> None:
        if not code or any(character.isspace() for character in code):
            raise ValueError("delivery error code must be a nonempty token")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EventClaim:
    event: EventEnvelope
    token: str
    attempts: int
    lease_until: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Delivery:
    delivery_id: UUID
    event_id: UUID
    session: SessionRef
    text: str
    expires_at: datetime | None = None
    attempts: int = 0


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: UUID
    event_id: UUID
    session: SessionRef
    status: str
    attempts: int
    expires_at: datetime
    next_attempt_at: datetime
    platform_message_id: str | None
    last_error: str | None


class MessageStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest(self, event: EventEnvelope, *, expires_at: datetime, max_attempts: int = 3) -> UUID:
        expiry = timestamp(expires_at)
        if max_attempts < 1 or expiry <= timestamp(event.received_at):
            raise ValueError("events need a future deadline and at least one attempt")
        with self.database.transaction() as connection:
            # The original event, deadline and attempt counter survive redelivery.
            existing = connection.execute(
                "SELECT event_id FROM inbox WHERE dedupe_key = ?", (event.dedupe_key,)
            ).fetchone()
            if existing:
                return UUID(existing["event_id"])
            connection.execute(
                """INSERT INTO inbox
                (event_id, dedupe_key, agent_id, payload, max_attempts, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(event.event_id),
                    event.dedupe_key,
                    event.agent_id,
                    event.model_dump_json(),
                    max_attempts,
                    expiry,
                ),
            )
        return event.event_id

    def claim(
        self, now: datetime, *, lease_seconds: int = 60, agent_id: str | None = None
    ) -> EventClaim | None:
        moment = timestamp(now)
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE inbox SET status = 'failed', claim_token = NULL
                WHERE status IN ('pending', 'processing')
                AND (? IS NULL OR agent_id = ?)
                AND (expires_at <= ? OR (attempts >= max_attempts AND lease_until <= ?))""",
                (agent_id, agent_id, moment, moment),
            )
            row = connection.execute(
                """SELECT candidate.* FROM inbox AS candidate
                WHERE (candidate.status = 'pending'
                       OR (candidate.status = 'processing' AND candidate.lease_until <= ?))
                  AND candidate.attempts < candidate.max_attempts
                  AND candidate.expires_at > ?
                  AND (? IS NULL OR candidate.agent_id = ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM inbox AS active
                      WHERE active.agent_id = candidate.agent_id
                        AND active.status = 'processing' AND active.lease_until > ?
                  )
                ORDER BY candidate.seq LIMIT 1""",
                (moment, moment, agent_id, agent_id, moment),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid4())
            lease_until = min(moment + lease_seconds, row["expires_at"])
            connection.execute(
                """UPDATE inbox SET status = 'processing', attempts = attempts + 1,
                claim_token = ?, lease_until = ? WHERE event_id = ?""",
                (token, lease_until, row["event_id"]),
            )
            return EventClaim(
                EventEnvelope.model_validate_json(row["payload"]),
                token,
                row["attempts"] + 1,
                datetime.fromtimestamp(lease_until, UTC),
                datetime.fromtimestamp(row["expires_at"], UTC),
            )

    def fail(self, claim: EventClaim, *, now: datetime) -> None:
        """Reject an activity permanently, with the same fencing as a successful decision."""
        moment = timestamp(now)
        with self.database.transaction() as connection:
            changed = connection.execute(
                """UPDATE inbox SET status = 'failed', claim_token = NULL
                WHERE event_id = ? AND status = 'processing' AND claim_token = ?
                  AND lease_until > ? AND expires_at > ?""",
                (str(claim.event.event_id), claim.token, moment, moment),
            ).rowcount
            if changed != 1:
                raise StaleClaimError("claim is expired or has already been replaced")

    def complete(self, claim: EventClaim, *, now: datetime, reply: str | None) -> UUID | None:
        """Commit the decision and outbound intent together; no network call here."""
        moment = timestamp(now)
        if reply is not None and not reply.strip():
            raise ValueError("reply cannot be empty; use None for silence")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM inbox WHERE event_id = ?", (str(claim.event.event_id),)
            ).fetchone()
            if (
                row is None
                or row["status"] != "processing"
                or row["claim_token"] != claim.token
                or row["lease_until"] <= moment
                or row["expires_at"] <= moment
            ):
                raise StaleClaimError("claim is expired or has already been replaced")
            delivery_id = None
            if reply is not None:
                delivery_id = uuid5(claim.event.event_id, "reply:0")
                persisted_event = EventEnvelope.model_validate_json(row["payload"])
                connection.execute(
                    """INSERT INTO outbox
                    (delivery_id, event_id, session, text, expires_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        str(delivery_id),
                        row["event_id"],
                        persisted_event.session.model_dump_json(),
                        reply,
                        row["expires_at"],
                    ),
                )
            connection.execute(
                "UPDATE inbox SET status = 'done', claim_token = NULL WHERE event_id = ?",
                (row["event_id"],),
            )
        return delivery_id

    def claim_delivery(
        self, now: datetime | None = None, *, agent_id: str | None = None
    ) -> Delivery | None:
        moment = timestamp(now or datetime.now(UTC))
        with self.database.transaction() as connection:
            if agent_id is None:
                connection.execute(
                    """UPDATE outbox SET status = 'failed', last_error = 'delivery_deadline'
                    WHERE status = 'pending' AND expires_at <= ?""",
                    (moment,),
                )
            else:
                connection.execute(
                    """UPDATE outbox SET status = 'failed', last_error = 'delivery_deadline'
                    WHERE status = 'pending' AND expires_at <= ?
                      AND EXISTS (
                          SELECT 1 FROM inbox
                          WHERE inbox.event_id = outbox.event_id AND inbox.agent_id = ?
                      )""",
                    (moment, agent_id),
                )
            row = connection.execute(
                """SELECT outbox.* FROM outbox JOIN inbox USING (event_id)
                WHERE outbox.status = 'pending'
                  AND outbox.next_attempt_at <= ?
                  AND outbox.expires_at > ?
                  AND (? IS NULL OR inbox.agent_id = ?)
                ORDER BY outbox.seq LIMIT 1""",
                (moment, moment, agent_id, agent_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE outbox SET status = 'sending', attempts = attempts + 1
                WHERE delivery_id = ?""",
                (row["delivery_id"],),
            )
            return Delivery(
                UUID(row["delivery_id"]),
                UUID(row["event_id"]),
                SessionRef.model_validate_json(row["session"]),
                row["text"],
                datetime.fromtimestamp(row["expires_at"], UTC),
                row["attempts"] + 1,
            )

    def confirm_delivery(self, delivery_id: UUID, platform_message_id: str) -> None:
        """A receipt may resolve an unknown delivery, but never initiate a retry."""
        if not platform_message_id:
            raise ValueError("delivery confirmation requires a platform receipt")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM outbox WHERE delivery_id = ?", (str(delivery_id),)
            ).fetchone()
            if (
                row is not None
                and row["status"] == "sent"
                and row["platform_message_id"] == platform_message_id
            ):
                return
            if row is None or row["status"] not in ("sending", "unknown"):
                raise DeliveryStateError("only an attempted delivery can be confirmed")
            connection.execute(
                """UPDATE outbox SET status = 'sent', platform_message_id = ?, last_error = NULL
                WHERE delivery_id = ?""",
                (platform_message_id, str(delivery_id)),
            )

    def defer_delivery(
        self,
        delivery_id: UUID,
        *,
        retry_at: datetime,
        reason: str = "rate_limited",
    ) -> bool:
        """Return a rate-limited attempt to pending, or fail it at its deadline."""
        retry_moment = timestamp(retry_at)
        if not reason or any(character.isspace() for character in reason):
            raise ValueError("delivery reason must be a nonempty token")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status, expires_at FROM outbox WHERE delivery_id = ?",
                (str(delivery_id),),
            ).fetchone()
            if row is None or row["status"] != "sending":
                raise DeliveryStateError("only an in-flight delivery can be deferred")
            if retry_moment >= row["expires_at"]:
                connection.execute(
                    """UPDATE outbox SET status = 'failed', last_error = 'delivery_deadline'
                    WHERE delivery_id = ?""",
                    (str(delivery_id),),
                )
                return False
            connection.execute(
                """UPDATE outbox SET status = 'pending', next_attempt_at = ?, last_error = ?
                WHERE delivery_id = ?""",
                (retry_moment, reason, str(delivery_id)),
            )
            return True

    def mark_delivery_unknown(self, delivery_id: UUID, *, reason: str = "unknown") -> None:
        if not reason or any(character.isspace() for character in reason):
            raise ValueError("delivery reason must be a nonempty token")
        with self.database.transaction() as connection:
            changed = connection.execute(
                """UPDATE outbox SET status = 'unknown', last_error = ?
                WHERE delivery_id = ? AND status = 'sending'""",
                (reason, str(delivery_id)),
            ).rowcount
            if changed != 1:
                raise DeliveryStateError("only an in-flight delivery can become unknown")

    def fail_delivery(self, delivery_id: UUID, *, reason: str = "permanent_failure") -> None:
        """Stop an attempted delivery after a definite local or platform rejection."""
        if not reason or any(character.isspace() for character in reason):
            raise ValueError("delivery reason must be a nonempty token")
        with self.database.transaction() as connection:
            changed = connection.execute(
                """UPDATE outbox SET status = 'failed', last_error = ?
                WHERE delivery_id = ? AND status = 'sending'""",
                (reason, str(delivery_id)),
            ).rowcount
            if changed != 1:
                raise DeliveryStateError("only an in-flight delivery can be rejected")

    def recover_interrupted_deliveries(self, *, agent_id: str | None = None) -> int:
        """Call once at exclusive sender startup, never alongside an active sender."""
        with self.database.transaction() as connection:
            if agent_id is None:
                return connection.execute(
                    """UPDATE outbox SET status = 'unknown', last_error = 'interrupted'
                    WHERE status = 'sending'"""
                ).rowcount
            return connection.execute(
                """UPDATE outbox SET status = 'unknown', last_error = 'interrupted'
                WHERE status = 'sending' AND EXISTS (
                    SELECT 1 FROM inbox
                    WHERE inbox.event_id = outbox.event_id AND inbox.agent_id = ?
                )""",
                (agent_id,),
            ).rowcount

    def reconcile_delivery(
        self,
        delivery_id: UUID,
        outcome: Literal["sent", "failed"],
        *,
        platform_message_id: str | None = None,
        reason: str = "manual_reconciliation",
    ) -> None:
        """Resolve an unknown delivery without ever putting it back in the send queue."""
        if outcome == "sent":
            if platform_message_id is None:
                raise ValueError("sent reconciliation requires a platform receipt")
            row = self.database.connection.execute(
                "SELECT status FROM outbox WHERE delivery_id = ?", (str(delivery_id),)
            ).fetchone()
            if row is None or row["status"] != "unknown":
                raise DeliveryStateError("only an unknown delivery can be reconciled")
            self.confirm_delivery(delivery_id, platform_message_id)
            return
        if outcome != "failed" or not reason or any(character.isspace() for character in reason):
            raise ValueError("invalid reconciliation outcome")
        with self.database.transaction() as connection:
            changed = connection.execute(
                """UPDATE outbox SET status = 'failed', last_error = ?
                WHERE delivery_id = ? AND status = 'unknown'""",
                (reason, str(delivery_id)),
            ).rowcount
            if changed != 1:
                raise DeliveryStateError("only an unknown delivery can be reconciled")

    @staticmethod
    def _record(row) -> DeliveryRecord:
        return DeliveryRecord(
            UUID(row["delivery_id"]),
            UUID(row["event_id"]),
            SessionRef.model_validate_json(row["session"]),
            row["status"],
            row["attempts"],
            datetime.fromtimestamp(row["expires_at"], UTC),
            datetime.fromtimestamp(row["next_attempt_at"], UTC),
            row["platform_message_id"],
            row["last_error"],
        )

    def delivery_record(self, delivery_id: UUID) -> DeliveryRecord:
        row = self.database.connection.execute(
            "SELECT * FROM outbox WHERE delivery_id = ?", (str(delivery_id),)
        ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return self._record(row)

    def list_delivery_records(self, *, status: str | None = None) -> tuple[DeliveryRecord, ...]:
        if status is not None and status not in {"pending", "sending", "sent", "unknown", "failed"}:
            raise ValueError("invalid delivery status")
        rows = self.database.connection.execute(
            """SELECT * FROM outbox
            WHERE (? IS NULL OR status = ?)
            ORDER BY seq""",
            (status, status),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def event_status(self, event_id: UUID) -> str:
        row = self.database.connection.execute(
            "SELECT status FROM inbox WHERE event_id = ?", (str(event_id),)
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return row["status"]

    def delivery_status(self, delivery_id: UUID) -> str:
        row = self.database.connection.execute(
            "SELECT status FROM outbox WHERE delivery_id = ?", (str(delivery_id),)
        ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return row["status"]
