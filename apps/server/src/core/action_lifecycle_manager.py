"""Action lifecycle transition manager."""

from datetime import UTC, datetime
from typing import Any, ClassVar

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus


class ActionLifecycleManager:
    """Manages valid immutable ActionLifecycleState transitions."""

    _valid_transitions: ClassVar[set[tuple[ActionStatus, ActionStatus]]] = {
        (ActionStatus.PLANNED, ActionStatus.QUEUED),
        (ActionStatus.QUEUED, ActionStatus.APPROVED),
        (ActionStatus.QUEUED, ActionStatus.REJECTED),
        (ActionStatus.APPROVED, ActionStatus.EXECUTING),
        (ActionStatus.EXECUTING, ActionStatus.COMPLETED),
        (ActionStatus.EXECUTING, ActionStatus.SKIPPED),
        (ActionStatus.EXECUTING, ActionStatus.FAILED),
        # Retry: a transiently failed action may be re-queued.
        (ActionStatus.FAILED, ActionStatus.QUEUED),
    }

    def transition(
        self,
        state: ActionLifecycleState,
        status: ActionStatus,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ActionLifecycleState:
        """Return a new lifecycle state for a valid transition."""
        if (state.status, status) not in self._valid_transitions:
            raise ValueError(f"invalid action lifecycle transition: {state.status} -> {status}")

        if reason is not None and status not in {
            ActionStatus.FAILED,
            ActionStatus.REJECTED,
            ActionStatus.SKIPPED,
        }:
            raise ValueError(
                "reason may only be supplied for failed, rejected or skipped actions"
            )

        return ActionLifecycleState(
            status=status,
            created_at=state.created_at,
            updated_at=datetime.now(UTC),
            reason=reason,
            metadata={**dict(state.metadata), **(metadata or {})},
        )
