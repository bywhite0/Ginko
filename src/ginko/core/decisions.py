"""Strict, bounded text decisions without platform or model dependencies."""

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class InvalidDecisionError(ValueError):
    """A safe error that never includes generated content."""


class TextDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    action: Literal["reply", "silent"]
    text: Annotated[str | None, Field(max_length=4000)]

    @model_validator(mode="after")
    def check_action(self) -> Self:
        if self.action == "silent" and self.text is not None:
            raise ValueError("silent decisions require null text")
        if self.action == "reply":
            if self.text is None or not self.text.strip():
                raise ValueError("reply decisions require nonempty text")
            if any(ord(char) < 32 and char not in "\n\t" for char in self.text):
                raise ValueError("unsupported control character")
            self.text.encode("utf-8")
        return self


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def parse_decision(raw: str, *, max_reply_chars: int) -> TextDecision:
    # JSON escaping may expand a character to two six-byte surrogate escapes.
    if len(raw) > max_reply_chars * 12 + 1024:
        raise InvalidDecisionError("decision exceeds its bound")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        decision = TextDecision.model_validate(data)
        if decision.text is not None and len(decision.text) > max_reply_chars:
            raise ValueError("reply exceeds configured limit")
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise InvalidDecisionError("invalid text decision") from None
    return decision
