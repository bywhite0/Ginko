"""One bounded, non-streaming Chat Completions attempt per budget reservation."""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from ginko.config import ModelSettings
from ginko.storage.budget import BudgetLedger, BudgetOverrunError

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 1_000_000


class PromptMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(min_length=1)]


class UsageDetails(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    cached_tokens: Annotated[int | None, Field(ge=0, le=2**31 - 1)] = None
    reasoning_tokens: Annotated[int | None, Field(ge=0, le=2**31 - 1)] = None


class TokenUsage(BaseModel):
    # Completion tokens must include reasoning; cache discounts need explicit configured
    # rates and validated cache counts. Missing cache detail uses the full input rate.
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    prompt_tokens: Annotated[int, Field(ge=1, le=2**31 - 1)]
    completion_tokens: Annotated[int, Field(ge=0, le=2**31 - 1)]
    total_tokens: Annotated[int, Field(ge=0, le=2**32 - 2)]
    prompt_tokens_details: UsageDetails | None = None
    completion_tokens_details: UsageDetails | None = None
    prompt_cache_hit_tokens: Annotated[int | None, Field(ge=0, le=2**31 - 1)] = None
    prompt_cache_miss_tokens: Annotated[int | None, Field(ge=0, le=2**31 - 1)] = None

    @model_validator(mode="after")
    def check_total(self) -> Self:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("inconsistent usage totals")
        if self.completion_tokens_details is not None:
            reasoning = self.completion_tokens_details.reasoning_tokens
            if reasoning is not None and reasoning > self.completion_tokens:
                raise ValueError("reasoning tokens exceed total completion tokens")
        cached = (
            self.prompt_tokens_details.cached_tokens
            if self.prompt_tokens_details is not None
            else None
        )
        for amount in (cached, self.prompt_cache_hit_tokens, self.prompt_cache_miss_tokens):
            if amount is not None and amount > self.prompt_tokens:
                raise ValueError("cache tokens exceed total prompt tokens")
        if (
            cached is not None
            and self.prompt_cache_hit_tokens is not None
            and cached != self.prompt_cache_hit_tokens
        ):
            raise ValueError("inconsistent cached-token counts")
        if (
            self.prompt_cache_hit_tokens is not None
            and self.prompt_cache_miss_tokens is not None
            and self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens != self.prompt_tokens
        ):
            raise ValueError("cache counts do not sum to prompt tokens")
        return self

    @property
    def cached_tokens(self) -> int:
        return self.reported_cached_tokens or 0

    @property
    def reported_cached_tokens(self) -> int | None:
        if self.prompt_cache_hit_tokens is not None:
            return self.prompt_cache_hit_tokens
        if self.prompt_tokens_details is not None:
            return self.prompt_tokens_details.cached_tokens
        return None


@dataclass(frozen=True)
class ModelReply:
    text: str
    operation_id: str
    usage: TokenUsage
    cost_microusd: int


class ModelCallError(Exception):
    """Only a fixed error code and attempt ID may cross the provider boundary."""

    def __init__(self, code: str, *, operation_id: str | None = None, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.operation_id = operation_id
        self.retryable = retryable


class ProviderContractError(RuntimeError):
    """The endpoint exceeded a configured token cap; stop the service for review."""


def prompt_token_bound(messages: tuple[PromptMessage, ...]) -> int:
    """Conservative byte-tokenizer admission bound, including chat-template headroom.

    This is a supported-provider contract, not a tokenizer for arbitrary model services.
    Live acceptance must verify the endpoint's tokenizer/template and returned usage.
    """
    if not 1 <= len(messages) <= 16 or not all(isinstance(m, PromptMessage) for m in messages):
        raise ModelCallError("invalid_prompt")
    return 256 + sum(128 + len(message.content.encode("utf-8")) for message in messages)


class ChatClient:
    def __init__(
        self,
        settings: ModelSettings,
        api_key: SecretStr,
        ledger: BudgetLedger,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self._clock = clock
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key.get_secret_value()}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            transport=transport,
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    async def complete(self, messages: tuple[PromptMessage, ...], *, trace_id: UUID) -> ModelReply:
        if self._client.is_closed:
            raise RuntimeError("model client is closed")
        if prompt_token_bound(messages) > self.settings.max_input_tokens:
            raise ModelCallError("input_limit")
        payload = {
            "model": self.settings.model,
            "messages": [message.model_dump() for message in messages],
            "max_tokens": self.settings.max_output_tokens,
            "n": 1,
            "stream": False,
        }
        # The trace prefix survives process restarts in the existing budget ledger.
        # Every explicit retry receives a fresh UUID and an independent reservation.
        operation_id = f"{trace_id}:{uuid4()}"
        started_at = self._clock()
        self.ledger.reserve(
            operation_id,
            "reply",
            self.settings.reservation_microusd,
            now=started_at,
            trace_id=str(trace_id),
        )
        logger.info(
            "model_reserved trace_id=%s operation_id=%s reserved_microusd=%d",
            trace_id,
            operation_id,
            self.settings.reservation_microusd,
        )
        try:
            return await self._complete_reserved(
                payload, operation_id=operation_id, started_at=started_at
            )
        except ModelCallError as error:
            self.ledger.record_model_outcome(operation_id, error.code)
            raise
        except asyncio.CancelledError:
            self.ledger.record_model_outcome(operation_id, "cancelled")
            logger.warning("model_unknown operation_id=%s cause=cancelled", operation_id)
            raise
        except ProviderContractError:
            self.ledger.record_model_outcome(operation_id, "provider_contract")
            raise
        except BudgetOverrunError:
            self.ledger.record_model_outcome(operation_id, "budget_overrun")
            raise

    async def _complete_reserved(
        self, payload: dict[str, object], *, operation_id: str, started_at: datetime
    ) -> ModelReply:
        try:
            # HTTPX timeouts bound individual I/O phases; this also bounds the whole call.
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self._client.stream(
                    "POST", self.settings.base_url + "/chat/completions", json=payload
                ) as response:
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ModelCallError("unsupported_encoding", operation_id=operation_id)
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        raise ModelCallError("unexpected_stream", operation_id=operation_id)
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ModelCallError("response_limit", operation_id=operation_id)
                        body.extend(chunk)
                    status = response.status_code
            finished_at = self._clock()
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ModelCallError("timeout", operation_id=operation_id, retryable=True) from None
        except httpx.TransportError:
            raise ModelCallError(
                "transport_error", operation_id=operation_id, retryable=True
            ) from None

        retryable = status in {200, 408, 429} or status >= 500
        try:
            data = json.loads(body)
        except (UnicodeError, ValueError):
            raise ModelCallError(
                "invalid_json", operation_id=operation_id, retryable=retryable
            ) from None
        if not isinstance(data, dict):
            raise ModelCallError("invalid_response", operation_id=operation_id, retryable=retryable)
        try:
            usage = TokenUsage.model_validate(data.get("usage"))
        except ValidationError:
            # Even a clean HTTP rejection isn't evidence of zero billed usage.
            code = "http_error" if status != 200 else "unknown_usage"
            raise ModelCallError(
                code,
                operation_id=operation_id,
                retryable=retryable,
            ) from None

        choices = data.get("choices")
        if (
            usage.completion_tokens == 0
            and isinstance(choices, list)
            and any(
                isinstance(choice, dict)
                and isinstance(choice.get("message"), dict)
                and (choice["message"].get("content") or choice["message"].get("reasoning_content"))
                for choice in choices
            )
        ):
            raise ModelCallError("unknown_usage", operation_id=operation_id, retryable=retryable)

        rates = self.settings.rates_between(started_at, finished_at)
        cost = rates.cost(usage.prompt_tokens, usage.cached_tokens, usage.completion_tokens)
        # Settle before inspecting generated content. Refusal, empty/truncated replies and
        # downstream JSON/business-validation failures must not erase billed usage.
        self.ledger.settle(
            operation_id,
            cost,
            usage=(usage.prompt_tokens, usage.completion_tokens, usage.total_tokens),
            cached_prompt_tokens=usage.reported_cached_tokens,
            reasoning_tokens=(
                usage.completion_tokens_details.reasoning_tokens
                if usage.completion_tokens_details is not None
                else None
            ),
        )
        logger.info(
            "model_settled operation_id=%s input_tokens=%d output_tokens=%d cost_microusd=%d",
            operation_id,
            usage.prompt_tokens,
            usage.completion_tokens,
            cost,
        )
        if (
            usage.prompt_tokens > self.settings.max_input_tokens
            or usage.completion_tokens > self.settings.max_output_tokens
        ):
            raise ProviderContractError("provider exceeded configured token limits")
        if status != 200:
            raise ModelCallError(
                "http_error",
                operation_id=operation_id,
                retryable=retryable,
            )
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ModelCallError("invalid_choices", operation_id=operation_id, retryable=True)
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ModelCallError("invalid_message", operation_id=operation_id, retryable=True)
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise ModelCallError("refused", operation_id=operation_id)
        if choice.get("finish_reason") != "stop" or message.get("tool_calls"):
            raise ModelCallError("incomplete_reply", operation_id=operation_id)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelCallError("empty_reply", operation_id=operation_id, retryable=True)
        self.ledger.record_model_outcome(operation_id, "accepted")
        return ModelReply(content, operation_id, usage, cost)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ChatClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
