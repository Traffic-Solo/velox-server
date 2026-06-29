"""In-memory inbox for newly accepted UniversalEvent instances."""

from uuid import UUID

from apps.server.src.core.events.models import UniversalEvent


class EventInbox:
    """Process-local pending inbox for UniversalEvent instances.

    The inbox tracks events that have been accepted but not marked as processed.
    It does not process, dispatch, queue, classify, or persist events outside
    this process.
    """

    def __init__(self) -> None:
        self._pending: list[UniversalEvent] = []

    def enqueue(self, event: UniversalEvent) -> UniversalEvent:
        """Add an event to the pending inbox."""
        self._pending.append(event)
        return event

    def list_pending(self) -> list[UniversalEvent]:
        """Return pending events in enqueue order."""
        return list(self._pending)

    def mark_processed(self, event_id: UUID) -> UniversalEvent | None:
        """Remove and return a pending event by id, or None when missing."""
        for index, event in enumerate(self._pending):
            if event.id == event_id:
                return self._pending.pop(index)
        return None

    def clear(self) -> None:
        """Remove all pending events."""
        self._pending.clear()
