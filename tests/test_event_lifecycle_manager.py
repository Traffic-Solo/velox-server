from uuid import uuid4

import pytest

from apps.server.src.core.events import EventLifecycleManager, EventLifecycleState


def create_state(status: str) -> EventLifecycleState:
    return EventLifecycleState(event_id=uuid4(), status=status)


def test_lifecycle_manager_transitions_accepted_to_pending() -> None:
    manager = EventLifecycleManager()
    state = create_state("accepted")

    next_state = manager.transition(state, "pending")

    assert next_state.status == "pending"


def test_lifecycle_manager_transitions_pending_to_processing() -> None:
    manager = EventLifecycleManager()
    state = create_state("pending")

    next_state = manager.transition(state, "processing")

    assert next_state.status == "processing"


def test_lifecycle_manager_transitions_processing_to_processed() -> None:
    manager = EventLifecycleManager()
    state = create_state("processing")

    next_state = manager.transition(state, "processed")

    assert next_state.status == "processed"


def test_lifecycle_manager_transitions_processing_to_failed() -> None:
    manager = EventLifecycleManager()
    state = create_state("processing")

    next_state = manager.transition(state, "failed")

    assert next_state.status == "failed"


def test_lifecycle_manager_rejects_invalid_transition() -> None:
    manager = EventLifecycleManager()
    state = create_state("accepted")

    with pytest.raises(ValueError):
        manager.transition(state, "processed")


def test_lifecycle_manager_does_not_mutate_original_state() -> None:
    manager = EventLifecycleManager()
    state = create_state("accepted")

    next_state = manager.transition(state, "pending")

    assert state.status == "accepted"
    assert next_state.status == "pending"
    assert next_state is not state


def test_lifecycle_manager_refreshes_updated_at() -> None:
    manager = EventLifecycleManager()
    state = create_state("accepted")

    next_state = manager.transition(state, "pending")

    assert next_state.updated_at > state.updated_at


def test_lifecycle_manager_failed_transition_stores_reason() -> None:
    manager = EventLifecycleManager()
    state = create_state("processing")

    next_state = manager.transition(state, "failed", reason="classification failed")

    assert next_state.reason == "classification failed"
