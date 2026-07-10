"""Core action model."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExecutorRole(StrEnum):
    """Vendor-neutral worker executor roles."""

    CONTENT_REVIEW = "content_review"
    CONTENT_SUMMARY = "content_summary"
    CONTEXT_PREPARATION = "context_preparation"


class Action(BaseModel):
    """Immutable representation of an action proposed or tracked by VELOX.

    Lifecycle status is intentionally not part of this model. The single
    source of truth for action status is ActionLifecycleState stored in the
    ActionLifecycleRepository, keyed by action id.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    type: str
    target: str
    payload: dict[str, Any] = Field(default_factory=dict)
    executor_role: ExecutorRole | str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        """Ensure created_at is timezone-aware UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")

        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("created_at must be in UTC")

        return value
