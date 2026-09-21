"""Audience policy is evaluated before retrieval, including derived memories."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from ginko.core.events import Contract, Identifier, SessionRef


class MemoryAudience(Contract):
    agent_id: Identifier
    scope: Literal["session", "shared", "public"]
    origin: SessionRef | None = None
    grants: tuple[SessionRef, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope == "public":
            if self.origin is not None or self.grants:
                raise ValueError("public knowledge has no private origin or session grants")
        elif self.origin is None:
            raise ValueError("non-public memory must retain its original audience")
        if self.scope != "shared" and self.grants:
            raise ValueError("extra audiences require an explicit shared grant")
        if self.scope == "shared" and not self.grants:
            raise ValueError("shared memory requires at least one explicit grant")
        return self

    def allows(self, agent_id: str, session: SessionRef) -> bool:
        return self.agent_id == agent_id and (
            self.scope == "public" or self.origin == session or session in self.grants
        )


class MemoryFact(Contract):
    fact_id: UUID
    text: Identifier
    audience: MemoryAudience
    evidence_kind: Literal["first_party", "third_party", "fiction", "inference"]
    source_event_ids: tuple[UUID, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    importance: int = Field(ge=1, le=10)


def can_recall_derived(
    audiences: tuple[MemoryAudience, ...], agent_id: str, session: SessionRef
) -> bool:
    """A summary can be shown only if every contributing source permits it.

    Callers resolve current grants for all dependencies on every retrieval; an
    old copy of a grant must not survive revocation in a materialized summary.
    """
    return bool(audiences) and all(audience.allows(agent_id, session) for audience in audiences)
