"""Core action model."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Action(BaseModel):
    """Immutable representation of an action proposed or tracked by VELOX."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    type: str
    target: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "rejected", "completed"] = "pending"
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
