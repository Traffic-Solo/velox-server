"""Permission decision model."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.server.src.core.actions import Action


class PermissionStatus(StrEnum):
    """Supported permission decision statuses."""

    ALLOWED = "allowed"
    DENIED = "denied"


class PermissionDecision(BaseModel):
    """Immutable permission decision."""

    model_config = ConfigDict(frozen=True)

    status: PermissionStatus = PermissionStatus.ALLOWED
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_blank_reason(cls, value: str | None) -> str | None:
        """Normalize blank reasons to None."""
        if value is None:
            return None
        if not value.strip():
            return None
        return value

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        """Ensure created_at is timezone-aware UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("created_at must be in UTC")
        return value


class PermissionEngine(Protocol):
    """Contract for evaluating whether an action is permitted."""

    def evaluate(self, action: Action) -> PermissionDecision:
        """Return a permission decision for an action."""
        ...


class BasePermissionEngine:
    """Default permission engine that allows actions without side effects."""

    def evaluate(self, action: Action) -> PermissionDecision:
        """Return an allowed decision without mutating or executing the action."""
        return PermissionDecision(status=PermissionStatus.ALLOWED)
