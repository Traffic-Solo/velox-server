"""In-memory event storage for VELOX events.

This store is intentionally process-local and non-durable. It exists as the
first minimal persistence boundary for API behavior and tests, without adding a
database, queue, worker, or external integration.
"""

from uuid import UUID

from apps.server.src.core.events.models import UniversalEvent
from apps.server.src.core.events.repository import EventRepository


class EventStore(EventRepository):
    """Append-only in-memory store for UniversalEvent instances."""

    def __init__(self) -> None:
        self._events: list[UniversalEvent] = []

    def append(self, event: UniversalEvent) -> UniversalEvent:
        """Append an event and return the stored event."""
        self._events.append(event)
        return event

    def list_events(self) -> list[UniversalEvent]:
        """Return stored events in append order."""
        return list(self._events)

    def get_event(self, event_id: UUID) -> UniversalEvent | None:
        """Return an event by id, or None when it is not present."""
        for event in self._events:
            if event.id == event_id:
                return event
        return None

    def clear(self) -> None:
        """Remove all stored events."""
        self._events.clear()
