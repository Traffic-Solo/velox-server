"""Event lifecycle transition manager."""

from datetime import UTC, datetime
from typing import ClassVar

from apps.server.src.core.events.lifecycle import EventLifecycleState, EventStatus


class EventLifecycleManager:
    """Manages valid EventLifecycleState transitions."""

    _valid_transitions: ClassVar[set[tuple[EventStatus, EventStatus]]] = {
        ("accepted", "pending"),
        ("pending", "processing"),
        ("processing", "processed"),
        ("processing", "failed"),
        # Replay: a failed event may be re-processed (Event Inbox replay).
        ("failed", "processing"),
    }

    def transition(
        self,
        state: EventLifecycleState,
        status: EventStatus,
        reason: str | None = None,
    ) -> EventLifecycleState:
        """Return a new lifecycle state for a valid transition."""
        if (state.status, status) not in self._valid_transitions:
            raise ValueError(f"invalid lifecycle transition: {state.status} -> {status}")

        if reason is not None and status != "failed":
            raise ValueError("reason may only be supplied when moving to failed")

        return EventLifecycleState(
            event_id=state.event_id,
            status=status,
            reason=reason,
            metadata=dict(state.metadata),
            updated_at=datetime.now(UTC),
        )
