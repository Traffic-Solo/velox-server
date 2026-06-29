"""Canonical event models for VELOX Server.

The Universal Event Model is the stable envelope used to describe facts that
enter or move through VELOX. It is deliberately storage-agnostic and contains no
business behavior, making it safe for API adapters, integrations, planners, and
future workers to share.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.server.src.core.events.types import EventMetadata, EventPayload


class UniversalEvent(BaseModel):
    """Immutable canonical representation of an event in VELOX.

    A UniversalEvent captures the common envelope every event needs, regardless
    of which integration, API route, worker, or internal process created it.
    The event is frozen after creation so consumers can safely pass it between
    layers without accidental mutation.

    Attributes:
        id: Globally unique event identifier.
        source: System, integration, or component that produced the event.
        type: Event type name, such as ``note.created`` or ``task.updated``.
        timestamp: UTC timestamp representing when the event occurred.
        payload: Source-specific event body.
        metadata: Non-domain context such as trace data, source details, or
            routing hints.
        correlation_id: Optional identifier linking related events in the same
            higher-level workflow.
        causation_id: Optional identifier of the event that directly caused
            this event.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    source: str
    type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: EventPayload = Field(default_factory=dict)
    metadata: EventMetadata = Field(default_factory=dict)
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    @field_validator("source", "type")
    @classmethod
    def require_non_empty_text(cls, value: str) -> str:
        """Reject blank identifiers while preserving the submitted value."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Ensure event timestamps are timezone-aware UTC datetimes."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")

        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("timestamp must be in UTC")

        return value
