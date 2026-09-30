"""In-memory FIFO queue for planned actions."""

from uuid import UUID

from apps.server.src.core.actions import Action


class ActionQueue:
    """Process-local FIFO queue for Action instances."""

    def __init__(self) -> None:
        self._actions: list[Action] = []

    def enqueue(self, action: Action) -> Action:
        """Add one action to the queue."""
        self._actions.append(action)
        return action

    def enqueue_many(self, actions: list[Action]) -> list[Action]:
        """Add multiple actions to the queue in order."""
        for action in actions:
            self.enqueue(action)
        return actions

    def dequeue(self) -> Action | None:
        """Remove and return the next queued action, if any."""
        if not self._actions:
            return None
        return self._actions.pop(0)

    def get(self, action_id: UUID) -> Action | None:
        """Return one queued Action by id without changing queue order."""
        return next((action for action in self._actions if action.id == action_id), None)

    def remove(self, action_id: UUID) -> Action | None:
        """Remove one queued Action by id while preserving every other position."""
        for index, action in enumerate(self._actions):
            if action.id == action_id:
                return self._actions.pop(index)
        return None

    def list(self) -> list[Action]:
        """Return queued actions in FIFO order."""
        return list(self._actions)

    def clear(self) -> None:
        """Remove all queued actions."""
        self._actions.clear()

    def count(self) -> int:
        """Return the number of queued actions."""
        return len(self._actions)
