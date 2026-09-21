"""Durable inbox leases and a deliberately conservative delivery lifecycle."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from ginko.core.events import EventEnvelope, SessionRef
from ginko.storage.database import Database, timestamp


class StaleClaimError(RuntimeError):
    """A timed-out worker cannot commit over its successor."""


class DeliveryStateError(RuntimeError):
    """Delivery transitions must be explicit, including ambiguous results."""


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
                    """INSERT INTO outbox (delivery_id, event_id, session, text)
                    VALUES (?, ?, ?, ?)""",
                    (
                        str(delivery_id),
                        row["event_id"],
                        persisted_event.session.model_dump_json(),
                        reply,
                    ),
                )
            connection.execute(
                "UPDATE inbox SET status = 'done', claim_token = NULL WHERE event_id = ?",
                (row["event_id"],),
            )
        return delivery_id

    def claim_delivery(self, *, agent_id: str | None = None) -> Delivery | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT outbox.* FROM outbox JOIN inbox USING (event_id)
                WHERE outbox.status = 'pending' AND (? IS NULL OR inbox.agent_id = ?)
                ORDER BY outbox.seq LIMIT 1""",
                (agent_id, agent_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE outbox SET status = 'sending' WHERE delivery_id = ?",
                (row["delivery_id"],),
            )
            return Delivery(
                UUID(row["delivery_id"]),
                UUID(row["event_id"]),
                SessionRef.model_validate_json(row["session"]),
                row["text"],
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
                """UPDATE outbox SET status = 'sent', platform_message_id = ?
                WHERE delivery_id = ?""",
                (platform_message_id, str(delivery_id)),
            )

    def mark_delivery_unknown(self, delivery_id: UUID) -> None:
        with self.database.transaction() as connection:
            changed = connection.execute(
                """UPDATE outbox SET status = 'unknown'
                WHERE delivery_id = ? AND status = 'sending'""",
                (str(delivery_id),),
            ).rowcount
            if changed != 1:
                raise DeliveryStateError("only an in-flight delivery can become unknown")

    def fail_delivery(self, delivery_id: UUID) -> None:
        """Stop an attempted delivery after a definite local or platform rejection."""
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE outbox SET status = 'failed' WHERE delivery_id = ? AND status = 'sending'",
                (str(delivery_id),),
            ).rowcount
            if changed != 1:
                raise DeliveryStateError("only an in-flight delivery can be rejected")

    def recover_interrupted_deliveries(self) -> int:
        """Call once at exclusive sender startup, never alongside an active sender."""
        with self.database.transaction() as connection:
            return connection.execute(
                "UPDATE outbox SET status = 'unknown' WHERE status = 'sending'"
            ).rowcount

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
