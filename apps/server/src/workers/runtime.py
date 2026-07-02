"""Worker runtime foundation for processing queued actions."""

from dataclasses import dataclass

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action


@dataclass(frozen=True)
class WorkerProcessingResult:
    """Result returned after one worker runtime processing attempt."""

    action: Action | None
    lifecycle_state: ActionLifecycleState | None
    processed: bool
    external_execution_performed: bool


class WorkerRuntime:
    """Processes queued actions through lifecycle transitions without integrations."""

    def __init__(
        self,
        action_queue: ActionQueue,
        action_lifecycle_manager: ActionLifecycleManager,
    ) -> None:
        self._action_queue = action_queue
        self._action_lifecycle_manager = action_lifecycle_manager

    def process_next(self) -> WorkerProcessingResult:
        """Process one queued action if available."""
        action = self._action_queue.dequeue()
        if action is None:
            return WorkerProcessingResult(
                action=None,
                lifecycle_state=None,
                processed=False,
                external_execution_performed=False,
            )

        lifecycle_state = ActionLifecycleState(status=ActionStatus.QUEUED)
        lifecycle_state = self._action_lifecycle_manager.transition(
            lifecycle_state,
            ActionStatus.APPROVED,
        )
        lifecycle_state = self._action_lifecycle_manager.transition(
            lifecycle_state,
            ActionStatus.EXECUTING,
        )
        lifecycle_state = self._action_lifecycle_manager.transition(
            lifecycle_state,
            ActionStatus.COMPLETED,
        )

        processed_action = action.model_copy(
            update={
                "status": "completed",
                "metadata": {
                    **action.metadata,
                    "action_lifecycle": lifecycle_state.model_dump(mode="json"),
                    "external_execution_performed": False,
                },
            }
        )

        return WorkerProcessingResult(
            action=processed_action,
            lifecycle_state=lifecycle_state,
            processed=True,
            external_execution_performed=False,
        )
