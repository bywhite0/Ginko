"""Maintainer-owned runtime configuration; loading has no network or database effects."""

import os
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from ginko.core.events import SessionRef
from ginko.persona import Persona, load_ginko
from ginko.storage.budget import BudgetLimits

Name = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
PlatformId = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
EnvName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
PositiveInt = Annotated[int, Field(gt=0, le=1_000_000)]
Amount = Annotated[int, Field(ge=0, le=2**63 - 1)]
Rate = Annotated[int, Field(gt=0, le=10**15)]
CachedRate = Annotated[int | None, Field(ge=0, le=10**15)]
UtcRange = Annotated[
    str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]-(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$")
]


def _ceil_million(numerator: int) -> int:
    # Round each token category up; fractional micro-USD must not disappear.
    return (numerator + 999_999) // 1_000_000


@dataclass(frozen=True)
class TokenRates:
    """Micro-USD per million tokens; cached input without its own rate pays full input."""

    input: int
    cached_input: int
    output: int

    @classmethod
    def highest(cls, rates: "list[TokenRates]") -> "TokenRates":
        return cls(
            max(rate.input for rate in rates),
            max(rate.cached_input for rate in rates),
            max(rate.output for rate in rates),
        )

    def cost(self, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> int:
        input_numerator = (prompt_tokens - cached_tokens) * self.input
        return _ceil_million(input_numerator + cached_tokens * self.cached_input) + _ceil_million(
            completion_tokens * self.output
        )

    def reservation(self, max_input_tokens: int, max_output_tokens: int) -> int:
        return _ceil_million(max_input_tokens * max(self.input, self.cached_input)) + _ceil_million(
            max_output_tokens * self.output
        )


class ConfigurationError(ValueError):
    """Safe to display: excludes source values and credentials."""


class Settings(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class AllowedSession(Settings):
    kind: Literal["private", "group"]
    chat_id: PlatformId


class Relationship(AllowedSession):
    user_id: PlatformId
    role: Literal["kaho"]


class OneBotSettings(Settings):
    bot_id: PlatformId
    host: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    port: Annotated[int, Field(gt=0, le=65535)] = 8080
    access_token_env: EnvName = "GINKO_ONEBOT_ACCESS_TOKEN"
    api_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10.0


class RateWindow(Settings):
    """Provider price for listed UTC weekdays and time ranges, e.g. a peak period."""

    name: Name
    weekdays: Annotated[
        tuple[Annotated[int, Field(ge=1, le=7)], ...], Field(min_length=1, max_length=7)
    ]
    utc_ranges: Annotated[tuple[UtcRange, ...], Field(min_length=1, max_length=24)]
    input_microusd_per_million_tokens: Rate
    cached_input_microusd_per_million_tokens: CachedRate = None
    output_microusd_per_million_tokens: Rate
    excluded_dates: Annotated[tuple[date, ...], Field(max_length=366)] = ()

    @field_validator("weekdays", "utc_ranges", "excluded_dates", mode="before")
    @classmethod
    def toml_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays must be unique")
        if len(set(self.excluded_dates)) != len(self.excluded_dates):
            raise ValueError("excluded dates must be unique")
        if any(start >= end for start, end in self.spans):
            raise ValueError("each UTC range must start before it ends")
        return self

    @property
    def spans(self) -> tuple[tuple[int, int], ...]:
        """Minutes after UTC midnight; each range is half-open."""

        def minutes(clock: str) -> int:
            hours, mins = clock.split(":")
            return int(hours) * 60 + int(mins)

        return tuple(
            (minutes(start), minutes(end))
            for start, end in (value.split("-") for value in self.utc_ranges)
        )

    @property
    def rates(self) -> TokenRates:
        cached = self.cached_input_microusd_per_million_tokens
        return TokenRates(
            self.input_microusd_per_million_tokens,
            self.input_microusd_per_million_tokens if cached is None else cached,
            self.output_microusd_per_million_tokens,
        )

    def occurrences(self, start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
        """Window instances overlapping the closed interval [start, end]."""
        day = start.date()
        while day <= end.date():
            if day.isoweekday() in self.weekdays and day not in self.excluded_dates:
                midnight = datetime.combine(day, time(), UTC)
                for first, last in self.spans:
                    opens = midnight + timedelta(minutes=first)
                    closes = midnight + timedelta(minutes=last)
                    if opens <= end and start < closes:
                        yield opens, closes
            day += timedelta(days=1)


class ModelSettings(Settings):
    protocol: Literal["chat_completions"]
    base_url: str
    model: Name
    api_key_env: EnvName = "GINKO_MODEL_API_KEY"
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30.0
    max_input_tokens: PositiveInt
    max_output_tokens: PositiveInt
    input_microusd_per_million_tokens: Rate
    cached_input_microusd_per_million_tokens: CachedRate = None
    output_microusd_per_million_tokens: Rate
    price_checked_on: date
    # Base rates apply whenever no window covers the request.
    rate_windows: Annotated[tuple[RateWindow, ...], Field(max_length=16)] = ()

    @field_validator("rate_windows", mode="before")
    @classmethod
    def toml_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_windows(self) -> Self:
        names = [window.name for window in self.rate_windows]
        if len(set(names)) != len(names):
            raise ValueError("rate window names must be unique")
        return self

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            value != value.strip()
            or any(character.isspace() for character in value)
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.scheme not in {"http", "https"}
        ):
            raise ValueError("use an HTTP(S) URL without credentials, query or fragment")
        if url.scheme == "http" and url.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("remote model endpoints require HTTPS")
        if url.port is not None and url.port == 0:
            raise ValueError("port must be positive")
        return value.rstrip("/")

    @property
    def base_rates(self) -> TokenRates:
        cached = self.cached_input_microusd_per_million_tokens
        return TokenRates(
            self.input_microusd_per_million_tokens,
            self.input_microusd_per_million_tokens if cached is None else cached,
            self.output_microusd_per_million_tokens,
        )

    @property
    def reservation_microusd(self) -> int:
        # Reserve at the highest configured rate, independent of when the call runs.
        rates = TokenRates.highest(
            [self.base_rates, *(window.rates for window in self.rate_windows)]
        )
        return rates.reservation(self.max_input_tokens, self.max_output_tokens)

    def rates_between(self, start: datetime, end: datetime) -> TokenRates:
        """Highest rates that may apply to a call running from start to end.

        The provider may bill at request start or completion, so every period the call
        touches counts; base rates count unless windows cover the whole interval.
        """
        if start.tzinfo is None or end.tzinfo is None or end < start:
            raise ValueError("billing interval must be timezone-aware and ordered")
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        hits = sorted(
            (
                (opens, closes, window.rates)
                for window in self.rate_windows
                for opens, closes in window.occurrences(start, end)
            ),
            key=lambda hit: hit[:2],
        )
        covered = start
        for opens, closes, _ in hits:
            if opens > covered:
                break
            covered = max(covered, closes)
        rates = [rate for *_, rate in hits]
        if covered <= end:
            rates.append(self.base_rates)
        return TokenRates.highest(rates)


class BudgetSettings(Settings):
    daily_microusd: Amount
    monthly_microusd: Amount

    @property
    def limits(self) -> BudgetLimits:
        return BudgetLimits(self.daily_microusd, self.monthly_microusd)


class ActivitySettings(Settings):
    lease_seconds: Annotated[int, Field(gt=0, le=600)] = 60
    ttl_seconds: Annotated[int, Field(gt=0, le=3600)] = 300
    max_attempts: Annotated[int, Field(ge=1, le=3)] = 2
    max_input_chars: Annotated[int, Field(gt=0, le=16000)] = 2000
    max_reply_chars: Annotated[int, Field(gt=0, le=4000)] = 1000


class RuntimeSettings(Settings):
    config_version: Annotated[int, Field(ge=1, le=1)]
    agent_id: Name = "ginko"
    data_dir: Annotated[str, Field(min_length=1)] = "data"
    approved_persona_version: Name
    behavior: Literal["mixed"] = "mixed"
    autonomous: bool = False
    onebot: OneBotSettings
    allowed_sessions: Annotated[tuple[AllowedSession, ...], Field(min_length=1, max_length=32)]
    relationships: tuple[Relationship, ...] = ()
    model: ModelSettings
    budget: BudgetSettings
    activity: ActivitySettings = ActivitySettings()

    @field_validator("allowed_sessions", "relationships", mode="before")
    @classmethod
    def toml_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if self.autonomous:
            raise ValueError("autonomous activity is not supported in this version")
        sessions = {(session.kind, session.chat_id) for session in self.allowed_sessions}
        if len(sessions) != len(self.allowed_sessions):
            raise ValueError("allowed sessions must be unique")
        bindings = set()
        for relationship in self.relationships:
            key = (relationship.kind, relationship.chat_id, relationship.user_id)
            if key in bindings or key[:2] not in sessions:
                raise ValueError("relationships must be unique and belong to allowed sessions")
            if relationship.kind == "private" and relationship.user_id != relationship.chat_id:
                raise ValueError("private relationships must identify the conversation peer")
            bindings.add(key)
        if self.activity.lease_seconds < self.model.timeout_seconds + 5:
            raise ValueError(
                "activity lease must exceed the model timeout by at least five seconds"
            )
        if self.activity.ttl_seconds <= self.activity.lease_seconds:
            raise ValueError("activity TTL must exceed the lease")
        if not self.data_dir.strip() or "\x00" in self.data_dir:
            raise ValueError("data directory must be a nonempty path")
        return self

    def allows(self, session: SessionRef) -> bool:
        return (
            session.platform == "qq"
            and session.bot_id == self.onebot.bot_id
            and session.thread_id is None
            and any(
                allowed.kind == session.kind and allowed.chat_id == session.chat_id
                for allowed in self.allowed_sessions
            )
        )

    def relationship_for(self, session: SessionRef, user_id: str) -> str | None:
        if not self.allows(session):
            return None
        return next(
            (
                relationship.role
                for relationship in self.relationships
                if relationship.kind == session.kind
                and relationship.chat_id == session.chat_id
                and relationship.user_id == user_id
            ),
            None,
        )


@dataclass(frozen=True)
class RuntimeConfig:
    settings: RuntimeSettings
    data_dir: Path
    persona: Persona = field(repr=False)
    onebot_access_token: SecretStr = field(repr=False)
    model_api_key: SecretStr = field(repr=False)


def load_config(path: Path, *, environ: Mapping[str, str] | None = None) -> RuntimeConfig:
    """Load explicit TOML and required environment secrets without creating runtime data."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigurationError("cannot read a valid UTF-8 TOML configuration") from None
    try:
        settings = RuntimeSettings.model_validate(raw)
    except ValidationError as error:
        locations = sorted(
            {
                ".".join(str(part) for part in item["loc"]) or item["msg"]
                for item in error.errors(include_input=False, include_context=False)
            }
        )
        raise ConfigurationError("invalid configuration: " + ", ".join(locations)) from None
    persona = load_ginko()
    if persona.version != settings.approved_persona_version:
        raise ConfigurationError("bundled persona differs from the approved version")
    environment = os.environ if environ is None else environ
    secrets = []
    for name in (settings.onebot.access_token_env, settings.model.api_key_env):
        value = environment.get(name, "")
        if (
            not value
            or value != value.strip()
            or any(ord(char) < 33 or ord(char) > 126 for char in value)
        ):
            raise ConfigurationError(f"missing or invalid credential environment variable: {name}")
        secrets.append(SecretStr(value))
    data_dir = (path.resolve().parent / settings.data_dir).resolve()
    return RuntimeConfig(settings, data_dir, persona, *secrets)
