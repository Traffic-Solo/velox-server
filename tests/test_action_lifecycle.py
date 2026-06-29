from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus


def test_action_lifecycle_default_planned_state() -> None:
    state = ActionLifecycleState()

    assert state.status == ActionStatus.PLANNED


def test_action_lifecycle_requires_utc_timestamps() -> None:
    with pytest.raises(ValidationError):
        ActionLifecycleState(created_at=datetime(2026, 6, 29, 12, 0, 0))


def test_action_lifecycle_metadata_defaults_to_empty_dict() -> None:
    state = ActionLifecycleState()

    assert state.metadata == {}


def test_action_lifecycle_blank_reason_normalizes_to_none() -> None:
    state = ActionLifecycleState(reason="   ")

    assert state.reason is None


def test_action_lifecycle_updated_at_defaults_to_created_at() -> None:
    state = ActionLifecycleState()

    assert state.updated_at == state.created_at


def test_action_lifecycle_is_immutable() -> None:
    state = ActionLifecycleState()

    with pytest.raises(ValidationError):
        state.status = ActionStatus.QUEUED


def test_action_lifecycle_accepts_utc_timestamps() -> None:
    timestamp = datetime.now(UTC)

    state = ActionLifecycleState(created_at=timestamp, updated_at=timestamp)

    assert state.created_at.utcoffset() == UTC.utcoffset(state.created_at)
    assert state.updated_at is not None
    assert state.updated_at.utcoffset() == UTC.utcoffset(state.updated_at)
