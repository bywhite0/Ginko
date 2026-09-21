"""Admit explicit OneBot V11 text requests and persist them before waking a worker."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from nonebot import on_message
from nonebot.adapters import Adapter as BaseAdapter
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    Event,
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
from nonebot.drivers import Driver, Request, Response, WebSocket
from nonebot.matcher import Matcher
from pydantic import SecretStr

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

    def __init__(
        self, driver: Driver, *, settings: RuntimeSettings, access_token: SecretStr
    ) -> None:
        self.settings = settings
        # The SDK constructor reads NoneBot's global config. Use its explicit equivalent
        # for this dedicated driver; integration tests cover this SDK compatibility seam.
        BaseAdapter.__init__(self, driver)
        self.onebot_config = OneBotConfig(onebot_access_token=access_token.get_secret_value())
        self.connections = {}
        self.tasks = set()
        self._ws_active = False
        self._setup()

    async def _handle_http(self, request: Request) -> Response:
        # Only authenticated reverse WebSocket is supported; the SDK's HTTP path uses
        # a different, optional signature mechanism and must not become a second ingress.
        return Response(405, content="Use the configured reverse WebSocket endpoint")

    async def _handle_ws(self, websocket: WebSocket) -> None:
        headers = websocket.request.headers
        if (
            headers.get("x-self-id") != self.settings.onebot.bot_id
            or headers.get("x-client-role") != "Universal"
            or self._ws_active
        ):
            await websocket.close(1008, "Unexpected or duplicate OneBot connection")
            return
        # Reserve the slot before accept(), including simultaneous handshakes.
        self._ws_active = True
        try:
            await super()._handle_ws(websocket)
        finally:
            self._ws_active = False

    def json_to_event(self, json_data: object) -> Event | None:
        if (
            isinstance(json_data, dict)
            and "post_type" in json_data
            and str(json_data.get("self_id")) != self.settings.onebot.bot_id
        ):
            return None
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
        self.accepting = True

    def close(self) -> None:
        self.accepting = False

    def receive(self, event: MessageEvent, *, received_at: datetime | None = None) -> UUID | None:
        if not self.accepting:
            return None
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


def register_ingress(
    ingress: OneBotIngress,
    *,
    adapter: OneBotAdapter | None = None,
    on_failure: Callable[[Exception], None] | None = None,
) -> type[Matcher]:
    """Install the thin NoneBot handler once during application startup."""
    matcher = on_message(priority=1, block=False)

    @matcher.handle()
    async def receive(bot: Bot, event: MessageEvent) -> None:
        if bot.self_id == ingress.settings.onebot.bot_id and (
            adapter is None or bot.adapter is adapter
        ):
            try:
                ingress.receive(event)
            except Exception as error:
                # NoneBot catches matcher exceptions. Explicitly notify supervision so a
                # broken durable inbox cannot remain online while silently losing events.
                if on_failure is None:
                    raise
                on_failure(error)

    return matcher
