"""Worker runtime foundation for processing queued actions."""

from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import (
    ActionLifecycleRepository,
    InMemoryActionLifecycleRepository,
)
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.workers.executor import (
    WorkerExecutionFailure,
    WorkerExecutionFailureCategory,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerExecutor,
    WorkerExecutorRegistry,
)


@dataclass
class WorkerExecutionObservation:
    """In-memory observation for one worker execution."""

    execution_id: UUID
    action_id: UUID
    requested_role: str | None
    executor_registered: bool
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    reason: str | None = None
    failure_category: str | None = None
    failure_message: str | None = None
    metadata: dict[str, Any] | None = None

    def as_metadata(self) -> dict[str, Any]:
        """Return JSON-compatible structured execution metadata."""
        return {
            "execution_id": str(self.execution_id),
            "action_id": str(self.action_id),
            "requested_role": self.requested_role,
            "executor_registered": self.executor_registered,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat()
            if self.finished_at is not None
            else None,
            "duration_ms": self.duration_ms,
            "reason": self.reason,
            "failure_category": self.failure_category,
            "failure_message": self.failure_message,
            "metadata": dict(self.metadata or {}),
        }


class InMemoryWorkerExecutionObserver:
    """Vendor-neutral in-memory worker execution observer."""

    def __init__(self) -> None:
        self._observations: list[WorkerExecutionObservation] = []

    def start(
        self,
        action: Action,
        requested_role: str | None,
        executor_registered: bool,
    ) -> WorkerExecutionObservation:
        """Record execution start."""
        observation = WorkerExecutionObservation(
            execution_id=uuid4(),
            action_id=action.id,
            requested_role=requested_role,
            executor_registered=executor_registered,
            status="started",
            started_at=datetime.now(UTC),
            metadata={
                "action_type": action.type,
                "action_target": action.target,
            },
        )
        self._observations.append(observation)
        return observation

    def finish(
        self,
        observation: WorkerExecutionObservation,
        status: WorkerExecutionStatus,
        metadata: dict[str, Any],
        reason: str | None = None,
        failure: WorkerExecutionFailure | None = None,
        duration_ms: float | None = None,
    ) -> WorkerExecutionObservation:
        """Record execution finish."""
        observation.status = status.value
        observation.finished_at = datetime.now(UTC)
        observation.duration_ms = duration_ms
        observation.reason = reason
        observation.failure_category = failure.category.value if failure is not None else None
        observation.failure_message = failure.message if failure is not None else None
        observation.metadata = {
            **dict(observation.metadata or {}),
            **dict(metadata),
        }
        return observation

    def list(self) -> list[WorkerExecutionObservation]:
        """Return recorded execution observations."""
        return list(self._observations)


@dataclass(frozen=True)
class WorkerProcessingResult:
    """Result returned after one worker runtime processing attempt."""

    action: Action | None
    lifecycle_state: ActionLifecycleState | None
    execution_status: WorkerExecutionStatus | None
    execution_reason: str | None
    processed: bool
    external_execution_performed: bool


@dataclass(frozen=True)
class WorkerInvocationResult:
    """Explicit result returned by the public worker invocation entrypoint."""

    requested_count: int
    processed_count: int
    queue_empty: bool
    results: list[WorkerProcessingResult]


class WorkerRuntime:
    """Processes queued actions through lifecycle transitions without integrations."""

    def __init__(
        self,
        action_queue: ActionQueue,
        action_lifecycle_manager: ActionLifecycleManager,
        worker_executor: WorkerExecutor,
        executor_registry: WorkerExecutorRegistry | None = None,
        execution_observer: InMemoryWorkerExecutionObserver | None = None,
        lifecycle_repository: ActionLifecycleRepository | None = None,
        max_transient_retries: int = 3,
    ) -> None:
        self._action_queue = action_queue
        self._action_lifecycle_manager = action_lifecycle_manager
        self._worker_executor = worker_executor
        self._executor_registry = executor_registry
        self._execution_observer = execution_observer or InMemoryWorkerExecutionObserver()
        self._lifecycle_repository = (
            lifecycle_repository
            if lifecycle_repository is not None
            else InMemoryActionLifecycleRepository()
        )
        self._max_transient_retries = max_transient_retries

    def pending_action_count(self) -> int:
        """Return the number of actions currently waiting in the queue."""
        return self._action_queue.count()

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

        lifecycle_state = self._lifecycle_repository.get(action.id)
        if lifecycle_state is None:
            lifecycle_state = ActionLifecycleState(status=ActionStatus.QUEUED)

        if lifecycle_state.status == ActionStatus.QUEUED:
            if lifecycle_state.metadata.get("approval_required"):
                # Defense in depth: an unapproved action must never execute,
                # even if it somehow reached the execution queue.
                self._action_queue.enqueue(action)
                return WorkerProcessingResult(
                    action=action,
                    lifecycle_state=lifecycle_state,
                    execution_status=None,
                    execution_reason="action awaits explicit approval",
                    processed=False,
                    external_execution_performed=False,
                )
            lifecycle_state = self._action_lifecycle_manager.transition(
                lifecycle_state,
                ActionStatus.APPROVED,
            )

        if lifecycle_state.status != ActionStatus.APPROVED:
            # A rejected or otherwise non-approved action is dropped safely.
            return WorkerProcessingResult(
                action=action,
                lifecycle_state=lifecycle_state,
                execution_status=None,
                execution_reason=(
                    "action is not approved for execution "
                    f"(status: {lifecycle_state.status.value})"
                ),
                processed=False,
                external_execution_performed=False,
            )

        lifecycle_state = self._action_lifecycle_manager.transition(
            lifecycle_state,
            ActionStatus.EXECUTING,
        )
        self._lifecycle_repository.set(action.id, lifecycle_state)

        if self._executor_registry is not None:
            resolution = self._executor_registry.resolve_with_registration(action)
            executor = resolution.executor
            requested_role = resolution.requested_role
            executor_registered = resolution.registered
        else:
            executor = self._worker_executor
            requested_role = (
                action.executor_role.value
                if isinstance(action.executor_role, ExecutorRole)
                else action.executor_role
            )
            executor_registered = False

        observation = self._execution_observer.start(
            action=action,
            requested_role=requested_role,
            executor_registered=executor_registered,
        )
        started_monotonic = perf_counter()
        try:
            execution_result = executor.execute(action)
        except Exception as exc:
            execution_result = WorkerExecutionResult(
                action=action,
                status=WorkerExecutionStatus.FAILED,
                reason=f"executor raised exception: {exc}",
                metadata={
                    "external_execution_performed": False,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                failure=WorkerExecutionFailure(
                    category=WorkerExecutionFailureCategory.INTERNAL,
                    message=f"executor raised exception: {exc}",
                    metadata={
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    },
                ),
            )
        duration_ms = round((perf_counter() - started_monotonic) * 1000, 3)
        failure = self._failure_for_result(execution_result)
        observation = self._execution_observer.finish(
            observation=observation,
            status=execution_result.status,
            metadata=execution_result.metadata,
            reason=execution_result.reason,
            failure=failure,
            duration_ms=duration_ms,
        )
        if execution_result.status == WorkerExecutionStatus.SUCCEEDED:
            lifecycle_state = self._action_lifecycle_manager.transition(
                lifecycle_state,
                ActionStatus.COMPLETED,
            )
        elif execution_result.status == WorkerExecutionStatus.SKIPPED:
            lifecycle_state = self._action_lifecycle_manager.transition(
                lifecycle_state,
                ActionStatus.SKIPPED,
                reason=execution_result.reason,
            )
        else:
            lifecycle_state = self._action_lifecycle_manager.transition(
                lifecycle_state,
                ActionStatus.FAILED,
                reason=execution_result.reason,
            )
        self._lifecycle_repository.set(action.id, lifecycle_state)

        if (
            lifecycle_state.status == ActionStatus.FAILED
            and failure is not None
            and failure.category == WorkerExecutionFailureCategory.TRANSIENT
        ):
            retry_count = int(lifecycle_state.metadata.get("transient_retry_count", 0))
            if retry_count < self._max_transient_retries:
                retry_state = self._action_lifecycle_manager.transition(
                    lifecycle_state,
                    ActionStatus.QUEUED,
                    metadata={"transient_retry_count": retry_count + 1},
                )
                # The action was already approved before this execution
                # attempt; a transient retry re-uses that approval.
                retry_state = self._action_lifecycle_manager.transition(
                    retry_state,
                    ActionStatus.APPROVED,
                )
                self._lifecycle_repository.set(action.id, retry_state)
                self._action_queue.enqueue(action)
                lifecycle_state = retry_state

        external_execution_performed = bool(
            execution_result.metadata.get("external_execution_performed", False)
        )

        metadata = {
            **execution_result.action.metadata,
            "action_lifecycle": lifecycle_state.model_dump(mode="json"),
            "worker_execution": {
                "status": execution_result.status.value,
                "reason": execution_result.reason,
                "failure": self._failure_metadata(failure),
                "requested_role": requested_role,
                "executor_registered": executor_registered,
                "started_at": observation.started_at.isoformat(),
                "finished_at": observation.finished_at.isoformat()
                if observation.finished_at is not None
                else None,
                "duration_ms": observation.duration_ms,
                "metadata": dict(execution_result.metadata),
                "observation": observation.as_metadata(),
            },
            "external_execution_performed": external_execution_performed,
        }

        processed_action = execution_result.action.model_copy(
            update={"metadata": metadata}
        )

        return WorkerProcessingResult(
            action=processed_action,
            lifecycle_state=lifecycle_state,
            execution_status=execution_result.status,
            execution_reason=execution_result.reason,
            processed=True,
            external_execution_performed=external_execution_performed,
        )

    def _failure_for_result(
        self,
        execution_result: WorkerExecutionResult,
    ) -> WorkerExecutionFailure | None:
        if execution_result.status != WorkerExecutionStatus.FAILED:
            return None

        if execution_result.failure is not None:
            return execution_result.failure

        return WorkerExecutionFailure(
            category=WorkerExecutionFailureCategory.INTERNAL,
            message=execution_result.reason,
        )

    def _failure_metadata(
        self,
        failure: WorkerExecutionFailure | None,
    ) -> dict[str, Any] | None:
        if failure is None:
            return None

        return {
            "category": failure.category.value,
            "message": failure.message,
            "metadata": dict(failure.metadata),
        }


class WorkerRuntimeInvocationService:
    """Vendor-neutral entrypoint for invoking worker runtime processing."""

    def __init__(self, worker_runtime: WorkerRuntime) -> None:
        self._worker_runtime = worker_runtime

    def invoke(self, max_actions: int = 1) -> WorkerInvocationResult:
        """Process one queued action or an explicit small batch."""
        if max_actions < 1:
            raise ValueError("max_actions must be at least 1")

        results: list[WorkerProcessingResult] = []
        for _ in range(max_actions):
            result = self._worker_runtime.process_next()
            results.append(result)
            if not result.processed:
                break

        processed_count = sum(1 for result in results if result.processed)
        return WorkerInvocationResult(
            requested_count=max_actions,
            processed_count=processed_count,
            queue_empty=self._worker_runtime.pending_action_count() == 0,
            results=results,
        )
