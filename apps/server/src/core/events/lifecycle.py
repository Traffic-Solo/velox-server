"""Event lifecycle state model."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

EventStatus = Literal["accepted", "pending", "processing", "processed", "failed"]


class EventLifecycleState(BaseModel):
    """Immutable lifecycle state for a UniversalEvent."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID
    status: EventStatus
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_blank_reason(cls, value: str | None) -> str | None:
        """Normalize blank reasons to None."""
        if value is None:
            return None
        if not value.strip():
            return None
        return value

    @field_validator("updated_at")
    @classmethod
    def require_utc_updated_at(cls, value: datetime) -> datetime:
        """Ensure updated_at is timezone-aware UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")

        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("updated_at must be in UTC")

        return value
