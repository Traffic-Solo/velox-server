"""Worker runtime foundation for processing queued actions."""

from dataclasses import dataclass

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action
from apps.server.src.workers.executor import (
    WorkerExecutionStatus,
    WorkerExecutor,
    WorkerExecutorRegistry,
)


@dataclass(frozen=True)
class WorkerProcessingResult:
    """Result returned after one worker runtime processing attempt."""

    action: Action | None
    lifecycle_state: ActionLifecycleState | None
    execution_status: WorkerExecutionStatus | None
    execution_reason: str | None
    processed: bool
    external_execution_performed: bool


class WorkerRuntime:
    """Processes queued actions through lifecycle transitions without integrations."""

    def __init__(
        self,
        action_queue: ActionQueue,
        action_lifecycle_manager: ActionLifecycleManager,
        worker_executor: WorkerExecutor,
        executor_registry: WorkerExecutorRegistry | None = None,
    ) -> None:
        self._action_queue = action_queue
        self._action_lifecycle_manager = action_lifecycle_manager
        self._worker_executor = worker_executor
        self._executor_registry = executor_registry

    def process_next(self) -> WorkerProcessingResult:
        """Process one queued action if available."""
        action = self._action_queue.dequeue()
        if action is None:
            return WorkerProcessingResult(
                action=None,
                lifecycle_state=None,
                execution_status=None,
                execution_reason=None,
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

        executor = (
            self._executor_registry.resolve(action)
            if self._executor_registry is not None
            else self._worker_executor
        )
        execution_result = executor.execute(action)
        if execution_result.status == WorkerExecutionStatus.SUCCEEDED:
            lifecycle_state = self._action_lifecycle_manager.transition(
                lifecycle_state,
                ActionStatus.COMPLETED,
            )
            action_status = "completed"
        else:
            lifecycle_state = self._action_lifecycle_manager.transition(
                lifecycle_state,
                ActionStatus.FAILED,
                reason=execution_result.reason,
            )
            action_status = action.status

        external_execution_performed = bool(
            execution_result.metadata.get("external_execution_performed", False)
        )

        metadata = {
            **execution_result.action.metadata,
            "action_lifecycle": lifecycle_state.model_dump(mode="json"),
            "worker_execution": {
                "status": execution_result.status.value,
                "reason": execution_result.reason,
                "metadata": dict(execution_result.metadata),
            },
            "external_execution_performed": external_execution_performed,
        }

        processed_action = execution_result.action.model_copy(
            update={
                "status": action_status,
                "metadata": metadata,
            }
        )

        return WorkerProcessingResult(
            action=processed_action,
            lifecycle_state=lifecycle_state,
            execution_status=execution_result.status,
            execution_reason=execution_result.reason,
            processed=True,
            external_execution_performed=external_execution_performed,
        )
