from datetime import UTC
from uuid import UUID

import pytest
from apps.server.src.core.actions import Action, ExecutorRole
from pydantic import ValidationError


def test_action_creation() -> None:
    action = Action(type="email.reply", target="message-123")

    assert action.type == "email.reply"
    assert action.target == "message-123"


def test_action_has_no_status_field() -> None:
    """Lifecycle status lives in ActionLifecycleRepository, not on Action."""
    assert "status" not in Action.model_fields


def test_action_generates_uuid() -> None:
    action = Action(type="email.reply", target="message-123")

    assert isinstance(action.id, UUID)


def test_action_created_at_is_utc_timestamp() -> None:
    action = Action(type="email.reply", target="message-123")

    assert action.created_at.tzinfo is not None
    assert action.created_at.utcoffset() == UTC.utcoffset(action.created_at)


def test_action_is_immutable() -> None:
    action = Action(type="email.reply", target="message-123")

    with pytest.raises(ValidationError):
        action.type = "email.forward"


def test_action_payload_defaults_to_empty_dict() -> None:
    action = Action(type="email.reply", target="message-123")

    assert action.payload == {}


def test_action_metadata_defaults_to_empty_dict() -> None:
    action = Action(type="email.reply", target="message-123")

    assert action.metadata == {}


def test_action_can_carry_executor_role() -> None:
    action = Action(
        type="email.reply",
        target="message-123",
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )

    assert action.executor_role == ExecutorRole.CONTENT_SUMMARY


def test_action_executor_role_defaults_to_none() -> None:
    action = Action(type="email.reply", target="message-123")

    assert action.executor_role is None
