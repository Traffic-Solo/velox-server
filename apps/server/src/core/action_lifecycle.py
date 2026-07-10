"""Action lifecycle state model."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ActionStatus(StrEnum):
    """Supported action lifecycle statuses."""

    PLANNED = "planned"
    QUEUED = "queued"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class ActionLifecycleState(BaseModel):
    """Immutable lifecycle state for an action."""

    model_config = ConfigDict(frozen=True)

    status: ActionStatus = ActionStatus.PLANNED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        """Ensure timestamps are timezone-aware UTC."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("timestamp must be in UTC")
        return value

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_blank_reason(cls, value: str | None) -> str | None:
        """Normalize blank reasons to None."""
        if value is None:
            return None
        if not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def default_updated_at_to_created_at(self) -> "ActionLifecycleState":
        """Default updated_at to created_at when omitted."""
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at)
        return self
