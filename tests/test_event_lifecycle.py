from datetime import UTC
from uuid import uuid4

import pytest
from apps.server.src.core.events import EventLifecycleState
from pydantic import ValidationError


def create_lifecycle_state(status: str = "accepted") -> EventLifecycleState:
    return EventLifecycleState(event_id=uuid4(), status=status)


def test_event_lifecycle_state_creation() -> None:
    event_id = uuid4()

    state = EventLifecycleState(event_id=event_id, status="accepted")

    assert state.event_id == event_id
    assert state.status == "accepted"


def test_event_lifecycle_state_default_metadata() -> None:
    state = create_lifecycle_state()

    assert state.metadata == {}


def test_event_lifecycle_state_updated_at_is_utc() -> None:
    state = create_lifecycle_state()

    assert state.updated_at.tzinfo is not None
    assert state.updated_at.utcoffset() == UTC.utcoffset(state.updated_at)


def test_event_lifecycle_state_is_immutable() -> None:
    state = create_lifecycle_state()

    with pytest.raises(ValidationError):
        state.status = "processed"


def test_event_lifecycle_state_accepts_accepted_status() -> None:
    state = create_lifecycle_state("accepted")

    assert state.status == "accepted"


def test_event_lifecycle_state_accepts_pending_status() -> None:
    state = create_lifecycle_state("pending")

    assert state.status == "pending"


def test_event_lifecycle_state_accepts_processing_status() -> None:
    state = create_lifecycle_state("processing")

    assert state.status == "processing"


def test_event_lifecycle_state_accepts_processed_status() -> None:
    state = create_lifecycle_state("processed")

    assert state.status == "processed"


def test_event_lifecycle_state_accepts_failed_status() -> None:
    state = create_lifecycle_state("failed")

    assert state.status == "failed"


def test_event_lifecycle_state_blank_reason_becomes_none() -> None:
    state = EventLifecycleState(event_id=uuid4(), status="accepted", reason="   ")

    assert state.reason is None
