import pytest
from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager


def test_action_lifecycle_manager_transitions_planned_to_queued() -> None:
    manager = ActionLifecycleManager()

    next_state = manager.transition(ActionLifecycleState(), ActionStatus.QUEUED)

    assert next_state.status == ActionStatus.QUEUED


def test_action_lifecycle_manager_transitions_queued_to_approved() -> None:
    manager = ActionLifecycleManager()
    state = ActionLifecycleState(status=ActionStatus.QUEUED)

    next_state = manager.transition(state, ActionStatus.APPROVED)

    assert next_state.status == ActionStatus.APPROVED


def test_action_lifecycle_manager_transitions_queued_to_rejected() -> None:
    manager = ActionLifecycleManager()
    state = ActionLifecycleState(status=ActionStatus.QUEUED)

    next_state = manager.transition(state, ActionStatus.REJECTED)

    assert next_state.status == ActionStatus.REJECTED


def test_action_lifecycle_manager_transitions_approved_to_executing() -> None:
    manager = ActionLifecycleManager()
    state = ActionLifecycleState(status=ActionStatus.APPROVED)

    next_state = manager.transition(state, ActionStatus.EXECUTING)

    assert next_state.status == ActionStatus.EXECUTING


def test_action_lifecycle_manager_transitions_executing_to_completed() -> None:
    manager = ActionLifecycleManager()
    state = ActionLifecycleState(status=ActionStatus.EXECUTING)

    next_state = manager.transition(state, ActionStatus.COMPLETED)

    assert next_state.status == ActionStatus.COMPLETED


def test_action_lifecycle_manager_transitions_executing_to_failed() -> None:
    manager = ActionLifecycleManager()
    state = ActionLifecycleState(status=ActionStatus.EXECUTING)

    next_state = manager.transition(state, ActionStatus.FAILED)

    assert next_state.status == ActionStatus.FAILED


def test_action_lifecycle_manager_invalid_transition_raises_value_error() -> None:
    manager = ActionLifecycleManager()

    with pytest.raises(ValueError):
        manager.transition(ActionLifecycleState(), ActionStatus.COMPLETED)


def test_action_lifecycle_manager_does_not_mutate_original_state() -> None:
    manager = ActionLifecycleManager()
    state = ActionLifecycleState()

    next_state = manager.transition(state, ActionStatus.QUEUED)

    assert state.status == ActionStatus.PLANNED
    assert next_state.status == ActionStatus.QUEUED
    assert next_state is not state


def test_action_lifecycle_manager_refreshes_updated_at() -> None:
    manager = ActionLifecycleManager()
    state = ActionLifecycleState()

    next_state = manager.transition(state, ActionStatus.QUEUED)

    assert next_state.updated_at is not None
    assert state.updated_at is not None
    assert next_state.updated_at > state.updated_at


def test_action_lifecycle_manager_failed_transition_stores_reason() -> None:
    manager = ActionLifecycleManager()
    state = ActionLifecycleState(status=ActionStatus.EXECUTING)

    next_state = manager.transition(
        state,
        ActionStatus.FAILED,
        reason="execution failed",
    )

    assert next_state.reason == "execution failed"


def test_action_lifecycle_manager_rejected_transition_stores_reason() -> None:
    manager = ActionLifecycleManager()
    state = ActionLifecycleState(status=ActionStatus.QUEUED)

    next_state = manager.transition(
        state,
        ActionStatus.REJECTED,
        reason="not allowed",
    )

    assert next_state.reason == "not allowed"
