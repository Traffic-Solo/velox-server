from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from apps.server.src.core.permission import PermissionDecision, PermissionStatus


def test_permission_decision_defaults_to_allowed() -> None:
    decision = PermissionDecision()

    assert decision.status == PermissionStatus.ALLOWED


def test_permission_decision_can_be_denied() -> None:
    decision = PermissionDecision(status=PermissionStatus.DENIED)

    assert decision.status == PermissionStatus.DENIED


def test_permission_decision_requires_utc_created_at() -> None:
    with pytest.raises(ValidationError):
        PermissionDecision(created_at=datetime(2026, 6, 29, 12, 0, 0))


def test_permission_decision_metadata_defaults_to_empty_dict() -> None:
    decision = PermissionDecision()

    assert decision.metadata == {}


def test_permission_decision_blank_reason_normalizes_to_none() -> None:
    decision = PermissionDecision(reason="   ")

    assert decision.reason is None


def test_permission_decision_is_immutable() -> None:
    decision = PermissionDecision()

    with pytest.raises(ValidationError):
        decision.status = PermissionStatus.DENIED


def test_permission_decision_accepts_utc_created_at() -> None:
    created_at = datetime.now(UTC)

    decision = PermissionDecision(created_at=created_at)

    assert decision.created_at == created_at
