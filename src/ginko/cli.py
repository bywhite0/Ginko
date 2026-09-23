"""Offline checks and an explicit, configured dedicated-session service entry point."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from ginko import __version__
from ginko.config import ConfigurationError, load_config
from ginko.core.events import EventEnvelope, SessionRef, TextSegment
from ginko.persona import load_ginko
from ginko.storage.budget import BudgetLedger, BudgetLimits
from ginko.storage.database import Database
from ginko.storage.messages import MessageStore


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
            delivery = store.claim_delivery()
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
