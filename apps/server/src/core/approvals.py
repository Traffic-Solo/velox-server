"""Registry for actions awaiting explicit human approval.

Actions whose permission decision is REQUIRES_APPROVAL are held here instead
of the execution queue. They enter the ActionQueue only after an explicit
approval, and are discarded (with a rejected lifecycle state) on rejection.
"""

from typing import Protocol
from uuid import UUID

from apps.server.src.core.actions import Action


class PendingApprovalRegistry(Protocol):
    """Storage contract for actions awaiting approval."""

    def add(self, action: Action) -> Action:
        """Store an action awaiting approval and return it."""
        ...

    def get(self, action_id: UUID) -> Action | None:
        """Return a pending action by id, or None when unknown."""
        ...

    def remove(self, action_id: UUID) -> Action | None:
        """Remove and return a pending action by id, or None when unknown."""
        ...

    def list_pending(self) -> list[Action]:
        """Return pending actions in insertion order."""
        ...

    def clear(self) -> None:
        """Remove all pending actions."""
        ...


class InMemoryPendingApprovalRegistry:
    """Process-local in-memory pending approval registry."""

    def __init__(self) -> None:
        self._actions: dict[UUID, Action] = {}

    def add(self, action: Action) -> Action:
        """Store an action awaiting approval and return it."""
        self._actions[action.id] = action
        return action

    def get(self, action_id: UUID) -> Action | None:
        """Return a pending action by id, or None when unknown."""
        return self._actions.get(action_id)

    def remove(self, action_id: UUID) -> Action | None:
        """Remove and return a pending action by id, or None when unknown."""
        return self._actions.pop(action_id, None)

    def list_pending(self) -> list[Action]:
        """Return pending actions in insertion order."""
        return list(self._actions.values())

    def clear(self) -> None:
        """Remove all pending actions."""
        self._actions.clear()
