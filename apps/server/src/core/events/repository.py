"""Repository protocol for event persistence boundaries."""

from typing import Protocol
from uuid import UUID

from apps.server.src.core.events.models import UniversalEvent


class EventRepository(Protocol):
    """Storage contract for UniversalEvent repositories."""

    def append(self, event: UniversalEvent) -> UniversalEvent:
        """Store an event and return the stored event."""
        ...

    def list_events(self) -> list[UniversalEvent]:
        """Return stored events in append order."""
        ...

    def get_event(self, event_id: UUID) -> UniversalEvent | None:
        """Return an event by id, or None when it is not present."""
        ...

    def clear(self) -> None:
        """Remove all stored events."""
        ...
