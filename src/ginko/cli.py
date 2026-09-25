"""Offline checks and an explicit, configured dedicated-session service entry point."""

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from ginko import __version__
from ginko.config import ConfigurationError, load_config
from ginko.core.events import EventEnvelope, SessionRef, TextSegment
from ginko.persona import load_ginko
from ginko.storage.budget import BudgetLedger, BudgetLimits
from ginko.storage.database import Database
from ginko.storage.messages import DeliveryStateError, MessageStore


def smoke() -> dict[str, object]:
    """Use a disposable DB and synthetic receipt; never imply a real bot reply."""
    now = datetime.now(UTC)
    event = EventEnvelope(
        agent_id="ginko",
        session=SessionRef(platform="offline", bot_id="local", kind="private", chat_id="demo"),
        kind="message.created",
        source_event_id="demo:1",
        message_id="1",
        user_id="demo",
        occurred_at=now,
        received_at=now,
        content=(TextSegment(text="离线检查"),),
    )
    with TemporaryDirectory(prefix="ginko-smoke-") as directory:
        path = Path(directory) / "ginko.sqlite3"
        with Database(path) as database:
            store = MessageStore(database)
            saved_id = store.ingest(event, expires_at=now + timedelta(minutes=5))
            duplicate_id = store.ingest(event, expires_at=now + timedelta(minutes=5))
        # A new connection demonstrates that pending work survives reopening.
        with Database(path) as database:
            store = MessageStore(database)
            claim = store.claim(now)
            if claim is None:
                raise RuntimeError("persisted work was lost")
            ledger = BudgetLedger(database, BudgetLimits(1000, 1000))
            ledger.reserve("offline:1", "smoke", 100, now=now)
            ledger.settle("offline:1", 0)  # Synthetic accounting, no paid call.
            delivery_id = store.complete(claim, now=now, reply="[offline] storage check passed")
            delivery = store.claim_delivery(now)
            if delivery is None or delivery_id is None:
                raise RuntimeError("outbound intent was lost")
            store.confirm_delivery(delivery_id, "synthetic-receipt:1")
            return {
                "mode": "offline",
                "version": __version__,
                "persona": load_ginko().display_name,
                "deduplicated": saved_id == duplicate_id,
                "inbox": store.event_status(saved_id),
                "outbox": store.delivery_status(delivery.delivery_id),
                "paid_calls": 0,
                "platform_messages": 0,
            }


def main(argv: list[str] | None = None) -> int:
    # Windows pipes may default to a code page that cannot encode the persona.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="ginko", description="Ginko foundation tools")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="inspect the installed persona and stage")
    subparsers.add_parser("smoke", help="run a disposable, offline persistence check")
    subparsers.add_parser("persona", help="show the curated persona identity and style")
    config_parser = subparsers.add_parser(
        "check-config", help="validate local runtime configuration"
    )
    config_parser.add_argument("path", type=Path)
    run_parser = subparsers.add_parser("run", help="run the explicitly configured OneBot service")
    run_parser.add_argument("path", type=Path)
    deliveries_parser = subparsers.add_parser(
        "deliveries", help="inspect durable delivery states without starting the service"
    )
    deliveries_parser.add_argument("database", type=Path)
    deliveries_parser.add_argument("delivery_id", nargs="?")
    deliveries_parser.add_argument(
        "--status", choices=["pending", "sending", "sent", "unknown", "failed"]
    )
    reconcile_parser = subparsers.add_parser(
        "reconcile-delivery", help="resolve one unknown delivery after manual platform checking"
    )
    reconcile_parser.add_argument("database", type=Path)
    reconcile_parser.add_argument("delivery_id")
    reconcile_parser.add_argument("outcome", choices=["sent", "failed"])
    reconcile_parser.add_argument("--receipt", help="platform message receipt when outcome is sent")
    reconcile_parser.add_argument("--reason", default="manual_reconciliation")
    attempts_parser = subparsers.add_parser(
        "model-attempts", help="inspect durable model attempt metadata without prompts"
    )
    attempts_parser.add_argument("database", type=Path)
    attempts_parser.add_argument("operation_id", nargs="?")
    attempts_parser.add_argument("--trace-id")
    args = parser.parse_args(argv)
    if args.command in {"check-config", "run"}:
        try:
            configured = load_config(args.path)
        except ConfigurationError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 2
        if args.command == "run":
            from ginko.application import Application

            try:
                asyncio.run(Application(configured).serve())
            except KeyboardInterrupt:
                return 0
            except Exception as error:
                print(f"Service stopped: {type(error).__name__}", file=sys.stderr)
                return 1
            return 0
        settings = configured.settings
        print(
            json.dumps(
                {
                    "status": "valid",
                    "config_version": settings.config_version,
                    "persona_version": configured.persona.version,
                    "allowed_sessions": len(settings.allowed_sessions),
                    "relationships": len(settings.relationships),
                    "autonomous": settings.autonomous,
                    "model_protocol": settings.model.protocol,
                    "daily_microusd": settings.budget.daily_microusd,
                    "monthly_microusd": settings.budget.monthly_microusd,
                    "attempt_reservation_microusd": settings.model.reservation_microusd,
                    "live_gateway": False,
                    "live_model": False,
                },
                indent=2,
            )
        )
    elif args.command == "deliveries":
        if args.delivery_id is not None and args.status is not None:
            print("Delivery command failed: conflicting_filters", file=sys.stderr)
            return 2
        try:
            if not args.database.is_file():
                raise FileNotFoundError(args.database)
            with Database(args.database) as database:
                store = MessageStore(database)
                records = (
                    (store.delivery_record(UUID(args.delivery_id)),)
                    if args.delivery_id is not None
                    else store.list_delivery_records(status=args.status)
                )
                print(json.dumps([_delivery_record_json(record) for record in records], indent=2))
        except (FileNotFoundError, KeyError, ValueError, DeliveryStateError, OSError):
            print("Delivery command failed: invalid_query", file=sys.stderr)
            return 2
    elif args.command == "reconcile-delivery":
        try:
            if not args.database.is_file():
                raise FileNotFoundError(args.database)
            with Database(args.database) as database:
                store = MessageStore(database)
                store.reconcile_delivery(
                    UUID(args.delivery_id),
                    args.outcome,
                    platform_message_id=args.receipt,
                    reason=args.reason,
                )
                print(
                    json.dumps(_delivery_record_json(store.delivery_record(UUID(args.delivery_id))))
                )
        except (FileNotFoundError, KeyError, ValueError, DeliveryStateError, OSError):
            print("Delivery command failed: invalid_reconciliation", file=sys.stderr)
            return 2
    elif args.command == "model-attempts":
        if args.operation_id is not None and args.trace_id is not None:
            print("Model attempt command failed: conflicting_filters", file=sys.stderr)
            return 2
        try:
            if not args.database.is_file():
                raise FileNotFoundError(args.database)
            with Database(args.database) as database:
                ledger = BudgetLedger(database, BudgetLimits(0, 0))
                records = (
                    (ledger.model_attempt(args.operation_id),)
                    if args.operation_id is not None
                    else ledger.list_model_attempts(trace_id=args.trace_id)
                )
                print(json.dumps([_model_attempt_json(record) for record in records], indent=2))
        except (KeyError, ValueError, OSError, sqlite3.Error):
            print("Model attempt command failed: invalid_query", file=sys.stderr)
            return 2
    elif args.command == "smoke":
        print(json.dumps(smoke(), ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        persona = load_ginko()
        print(
            json.dumps(
                {
                    "version": __version__,
                    "stage": "foundation",
                    "persona": persona.display_name,
                    "persona_version": persona.version,
                    "live_gateway": False,
                    "live_model": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        persona = load_ginko()
        print(persona.identity + "\n" + persona.style)
    return 0


def _delivery_record_json(record) -> dict[str, object]:
    return {
        "delivery_id": str(record.delivery_id),
        "event_id": str(record.event_id),
        "session": record.session.model_dump(mode="json"),
        "status": record.status,
        "attempts": record.attempts,
        "expires_at": record.expires_at.isoformat(),
        "next_attempt_at": record.next_attempt_at.isoformat(),
        "platform_message_id": record.platform_message_id,
        "last_error": record.last_error,
    }


def _model_attempt_json(record) -> dict[str, object]:
    return {
        "operation_id": record.operation_id,
        "trace_id": record.trace_id,
        "budget_status": record.budget_status,
        "status": record.status,
        "reserved_microusd": record.reserved_microusd,
        "actual_microusd": record.actual_microusd,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "total_tokens": record.total_tokens,
        "cached_prompt_tokens": record.cached_prompt_tokens,
        "reasoning_tokens": record.reasoning_tokens,
        "outcome_code": record.outcome_code,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }
