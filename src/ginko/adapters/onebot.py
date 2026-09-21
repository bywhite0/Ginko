"""Admit explicit OneBot V11 text requests and persist them before waking a worker."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    Event,
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.drivers import Driver
from nonebot.matcher import Matcher

from ginko.config import RuntimeSettings
from ginko.core.events import EventEnvelope, SessionRef, TextSegment
from ginko.storage.messages import MessageStore


def normalize_message(
    event: MessageEvent, settings: RuntimeSettings, *, received_at: datetime
) -> EventEnvelope | None:
    """Only friend DMs and explicitly mentioned group text in the allowlist are admitted."""
    if str(event.self_id) != settings.onebot.bot_id or event.user_id == event.self_id:
        return None
    if event.user_id <= 0:
        return None
    if isinstance(event, PrivateMessageEvent) and event.sub_type == "friend":
        session = SessionRef(
            platform="qq", bot_id=str(event.self_id), kind="private", chat_id=str(event.user_id)
        )
    elif (
        isinstance(event, GroupMessageEvent)
        and event.sub_type == "normal"
        and event.anonymous is None
        and event.group_id > 0
    ):
        session = SessionRef(
            platform="qq", bot_id=str(event.self_id), kind="group", chat_id=str(event.group_id)
        )
    else:
        return None
    if not settings.allows(session):
        return None

    text = []
    mentioned = False
    # NoneBot may already have removed the leading at-segment from event.message.
    for segment in event.original_message:
        if segment.type == "text" and isinstance(segment.data.get("text"), str):
            text.append(segment.data["text"])
        elif segment.type == "at" and str(segment.data.get("qq")) == settings.onebot.bot_id:
            mentioned = True
        else:
            # Do not silently turn media, quoted replies, or mentions of others into text requests.
            return None
    if session.kind == "group" and not mentioned:
        return None
    content = "".join(text).strip()
    if not content or len(content) > settings.activity.max_input_chars:
        return None
    try:
        occurred_at = datetime.fromtimestamp(event.time, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return EventEnvelope(
        agent_id=settings.agent_id,
        session=session,
        kind="message.created",
        source_event_id=str(event.message_id),
        message_id=str(event.message_id),
        user_id=str(event.user_id),
        occurred_at=occurred_at,
        received_at=received_at,
        content=(TextSegment(text=content),),
    )


class OneBotAdapter(Adapter):
    """Filter messages before SDK Bot preprocessing can query quoted-message content."""

    def __init__(self, driver: Driver, *, settings: RuntimeSettings) -> None:
        self.settings = settings
        super().__init__(driver)

    def json_to_event(self, json_data: object) -> Event | None:
        event = super().json_to_event(json_data)
        if (
            isinstance(event, MessageEvent)
            and normalize_message(event, self.settings, received_at=datetime.now(UTC)) is None
        ):
            return None
        return event


class OneBotIngress:
    def __init__(
        self, settings: RuntimeSettings, store: MessageStore, wake: Callable[[], None]
    ) -> None:
        self.settings = settings
        self.store = store
        self.wake = wake

    def receive(self, event: MessageEvent, *, received_at: datetime | None = None) -> UUID | None:
        normalized = normalize_message(
            event, self.settings, received_at=received_at or datetime.now(UTC)
        )
        if normalized is None:
            return None
        event_id = self.store.ingest(
            normalized,
            expires_at=normalized.received_at
            + timedelta(seconds=self.settings.activity.ttl_seconds),
            max_attempts=self.settings.activity.max_attempts,
        )
        self.wake()
        return event_id


def register_ingress(ingress: OneBotIngress) -> type[Matcher]:
    """Install the thin NoneBot handler once during application startup."""
    matcher = on_message(priority=1, block=False)

    @matcher.handle()
    async def receive(bot: Bot, event: MessageEvent) -> None:
        if bot.self_id == ingress.settings.onebot.bot_id:
            ingress.receive(event)

    return matcher
