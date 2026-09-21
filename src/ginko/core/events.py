"""Identifiers stay distinct across accounts, conversations and event kinds."""

import json
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionRef(Contract):
    platform: Identifier
    bot_id: Identifier
    kind: Literal["private", "group", "channel"]
    chat_id: Identifier
    thread_id: Identifier | None = None

    @property
    def key(self) -> str:
        # Encoding a tuple avoids delimiter collisions in native platform IDs.
        return json.dumps(
            [self.platform, self.bot_id, self.kind, self.chat_id, self.thread_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )


class TextSegment(Contract):
    type: Literal["text"] = "text"
    text: Annotated[str, Field(min_length=1)]


class EventEnvelope(Contract):
    schema_version: Literal[1] = 1
    event_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    agent_id: Identifier
    session: SessionRef
    kind: Literal["message.created", "message.edited", "message.recalled"]
    source_event_id: Identifier
    message_id: Identifier
    user_id: Identifier
    occurred_at: AwareDatetime
    received_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    content: tuple[TextSegment, ...] = ()

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if self.kind == "message.recalled" and self.content:
            raise ValueError("recalled messages cannot contain replacement content")
        if self.kind != "message.recalled" and not self.content:
            raise ValueError("created or edited messages need content")
        return self

    @property
    def dedupe_key(self) -> str:
        # The gateway must provide a stable ID on redelivery, including revision
        # identity for edits. A fresh trace_id does not change business identity.
        return json.dumps(
            [self.agent_id, self.session.key, self.kind, self.source_event_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )
