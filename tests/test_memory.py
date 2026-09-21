import pytest
from pydantic import ValidationError

from ginko.core.events import SessionRef
from ginko.core.memory import MemoryAudience, can_recall_derived


def test_private_memory_does_not_leak_to_another_account_or_group(event):
    audience = MemoryAudience(agent_id="ginko", scope="session", origin=event.session)
    assert audience.allows("ginko", event.session)
    assert not audience.allows("another-agent", event.session)
    group = event.session.model_copy(update={"kind": "group"})
    assert not audience.allows("ginko", group)
    same_user_elsewhere = SessionRef(
        platform="telegram", bot_id="bot:2", kind="private", chat_id="user:1"
    )
    # Even an identical native ID is not a grant to this destination.
    assert not audience.allows("ginko", same_user_elsewhere)


def test_explicit_grant_and_revocation_apply_to_derived_memory(event):
    destination = event.session.model_copy(update={"platform": "telegram"})
    shared = MemoryAudience(
        agent_id="ginko", scope="shared", origin=event.session, grants=(destination,)
    )
    private = MemoryAudience(agent_id="ginko", scope="session", origin=event.session)
    assert shared.allows("ginko", destination)
    assert can_recall_derived((shared,), "ginko", destination)
    assert not can_recall_derived((shared, private), "ginko", destination)
    assert not can_recall_derived((private,), "ginko", destination)
    assert not can_recall_derived((), "ginko", destination)


def test_public_knowledge_still_belongs_to_one_agent(event):
    public = MemoryAudience(agent_id="ginko", scope="public")
    assert public.allows("ginko", event.session)
    assert not public.allows("another-agent", event.session)


def test_accidental_scope_widening_is_rejected(event):
    with pytest.raises(ValidationError):
        MemoryAudience(agent_id="ginko", scope="public", origin=event.session)
    with pytest.raises(ValidationError):
        MemoryAudience(
            agent_id="ginko", scope="session", origin=event.session, grants=(event.session,)
        )
