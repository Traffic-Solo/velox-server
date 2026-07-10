"""Repository boundary for action lifecycle states.

The lifecycle repository is the single source of truth for the lifecycle status
of every action. Layers must read and write lifecycle state here instead of
duplicating status on the action model or fabricating fresh states.
"""

from typing import Protocol
from uuid import UUID

from apps.server.src.core.action_lifecycle import ActionLifecycleState


class ActionLifecycleRepository(Protocol):
    """Storage contract for action lifecycle states."""

    def get(self, action_id: UUID) -> ActionLifecycleState | None:
        """Return the lifecycle state for an action, or None when unknown."""
        ...

    def set(self, action_id: UUID, state: ActionLifecycleState) -> ActionLifecycleState:
        """Store the lifecycle state for an action and return it."""
        ...

    def list_states(self) -> dict[UUID, ActionLifecycleState]:
        """Return all known lifecycle states keyed by action id."""
        ...

    def clear(self) -> None:
        """Remove all stored lifecycle states."""
        ...


class InMemoryActionLifecycleRepository:
    """Process-local in-memory action lifecycle repository."""

    def __init__(self) -> None:
        self._states: dict[UUID, ActionLifecycleState] = {}

    def get(self, action_id: UUID) -> ActionLifecycleState | None:
        """Return the lifecycle state for an action, or None when unknown."""
        return self._states.get(action_id)

    def set(self, action_id: UUID, state: ActionLifecycleState) -> ActionLifecycleState:
        """Store the lifecycle state for an action and return it."""
        self._states[action_id] = state
        return state

    def list_states(self) -> dict[UUID, ActionLifecycleState]:
        """Return all known lifecycle states keyed by action id."""
        return dict(self._states)

    def clear(self) -> None:
        """Remove all stored lifecycle states."""
        self._states.clear()
