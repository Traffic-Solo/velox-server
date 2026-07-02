from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action
from apps.server.src.workers.executor import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerExecutor,
    WorkerExecutorRegistry,
)
from apps.server.src.workers.runtime import WorkerRuntime, WorkerRuntimeInvocationService


class RecordingExecutor:
    def __init__(self, result_status: WorkerExecutionStatus) -> None:
        self.result_status = result_status
        self.called_actions: list[Action] = []

    def execute(self, action: Action) -> WorkerExecutionResult:
        self.called_actions.append(action)
        return WorkerExecutionResult(
            action=action,
            status=self.result_status,
            reason="execution failed"
            if self.result_status == WorkerExecutionStatus.FAILED
            else None,
            metadata={"handled_by": "recording-executor"},
        )


def create_runtime(
    queue: ActionQueue,
    executor: WorkerExecutor | None = None,
) -> WorkerRuntime:
    return WorkerRuntime(
        action_queue=queue,
        action_lifecycle_manager=ActionLifecycleManager(),
        worker_executor=executor
        or RecordingExecutor(result_status=WorkerExecutionStatus.SUCCEEDED),
    )


def test_worker_runtime_processes_queued_action() -> None:
    queue = ActionQueue()
    action = Action(type="summarize_email", target="event-1")
    queue.enqueue(action)
    runtime = create_runtime(queue)

    result = runtime.process_next()

    assert result.processed is True
    assert result.action is not None
    assert result.action.id == action.id
    assert result.action.status == "completed"
    assert result.execution_status == WorkerExecutionStatus.SUCCEEDED
    assert queue.count() == 0


def test_worker_runtime_calls_executor() -> None:
    queue = ActionQueue()
    action = Action(type="summarize_email", target="event-1")
    queue.enqueue(action)
    executor = RecordingExecutor(result_status=WorkerExecutionStatus.SUCCEEDED)
    runtime = create_runtime(queue, executor)

    result = runtime.process_next()

    assert result.processed is True
    assert executor.called_actions == [action]


def test_worker_runtime_uses_registry_resolved_executor() -> None:
    queue = ActionQueue()
    action = Action(type="prepare_meeting", target="event-1")
    queue.enqueue(action)
    fallback_executor = RecordingExecutor(result_status=WorkerExecutionStatus.FAILED)
    registered_executor = RecordingExecutor(result_status=WorkerExecutionStatus.SUCCEEDED)
    executor_registry = WorkerExecutorRegistry(fallback_executor=fallback_executor)
    executor_registry.register("prepare_meeting", registered_executor)
    runtime = WorkerRuntime(
        action_queue=queue,
        action_lifecycle_manager=ActionLifecycleManager(),
        worker_executor=fallback_executor,
        executor_registry=executor_registry,
    )

    result = runtime.process_next()

    assert result.execution_status == WorkerExecutionStatus.SUCCEEDED
    assert registered_executor.called_actions == [action]
    assert fallback_executor.called_actions == []


def test_worker_runtime_applies_lifecycle_transitions() -> None:
    queue = ActionQueue()
    queue.enqueue(Action(type="summarize_email", target="event-1"))
    runtime = create_runtime(queue)

    result = runtime.process_next()

    assert result.lifecycle_state is not None
    assert result.lifecycle_state.status == ActionStatus.COMPLETED
    assert result.action is not None
    assert result.action.metadata["action_lifecycle"]["status"] == "completed"


def test_worker_runtime_successful_executor_result_updates_processing_result() -> None:
    queue = ActionQueue()
    action = Action(type="summarize_email", target="event-1")
    queue.enqueue(action)
    runtime = create_runtime(
        queue,
        RecordingExecutor(result_status=WorkerExecutionStatus.SUCCEEDED),
    )

    result = runtime.process_next()

    assert result.processed is True
    assert result.execution_status == WorkerExecutionStatus.SUCCEEDED
    assert result.execution_reason is None
    assert result.lifecycle_state is not None
    assert result.lifecycle_state.status == ActionStatus.COMPLETED
    assert result.action is not None
    assert result.action.status == "completed"
    assert result.action.metadata["worker_execution"]["status"] == "succeeded"


def test_worker_runtime_failed_executor_result_updates_processing_result() -> None:
    queue = ActionQueue()
    action = Action(type="summarize_email", target="event-1")
    queue.enqueue(action)
    runtime = create_runtime(
        queue,
        RecordingExecutor(result_status=WorkerExecutionStatus.FAILED),
    )

    result = runtime.process_next()

    assert result.processed is True
    assert result.execution_status == WorkerExecutionStatus.FAILED
    assert result.execution_reason == "execution failed"
    assert result.lifecycle_state is not None
    assert result.lifecycle_state.status == ActionStatus.FAILED
    assert result.lifecycle_state.reason == "execution failed"
    assert result.action is not None
    assert result.action.status == action.status
    assert result.action.metadata["worker_execution"]["status"] == "failed"


def test_worker_runtime_handles_empty_queue_safely() -> None:
    runtime = create_runtime(ActionQueue())

    result = runtime.process_next()

    assert result.processed is False
    assert result.action is None
    assert result.lifecycle_state is None
    assert result.execution_status is None
    assert result.execution_reason is None
    assert result.external_execution_performed is False


def test_worker_runtime_processes_one_action_at_a_time() -> None:
    queue = ActionQueue()
    first_action = Action(type="summarize_email", target="event-1")
    second_action = Action(type="prepare_meeting", target="event-2")
    queue.enqueue_many([first_action, second_action])
    runtime = create_runtime(queue)

    result = runtime.process_next()

    assert result.action is not None
    assert result.action.id == first_action.id
    assert queue.list() == [second_action]


def test_worker_runtime_does_not_introduce_vendor_specific_behavior() -> None:
    queue = ActionQueue()
    queue.enqueue(
        Action(
            type="external.vendor.call",
            target="remote-system",
            payload={"should_not_execute": True},
        )
    )
    runtime = create_runtime(queue)

    result = runtime.process_next()

    assert result.external_execution_performed is False
    assert result.action is not None
    assert result.action.metadata["external_execution_performed"] is False
    assert "vendor" not in result.action.metadata


def test_worker_runtime_invocation_handles_empty_queue() -> None:
    invocation = WorkerRuntimeInvocationService(create_runtime(ActionQueue()))

    result = invocation.invoke()

    assert result.requested_count == 1
    assert result.processed_count == 0
    assert result.queue_empty is True
    assert len(result.results) == 1
    assert result.results[0].processed is False


def test_worker_runtime_invocation_processes_one_queued_action() -> None:
    queue = ActionQueue()
    action = Action(type="summarize_email", target="event-1")
    queue.enqueue(action)
    invocation = WorkerRuntimeInvocationService(create_runtime(queue))

    result = invocation.invoke()

    assert result.processed_count == 1
    assert result.queue_empty is False
    assert result.results[0].action is not None
    assert result.results[0].action.id == action.id
    assert queue.count() == 0


def test_worker_runtime_invocation_result_shape() -> None:
    queue = ActionQueue()
    queue.enqueue(Action(type="summarize_email", target="event-1"))
    invocation = WorkerRuntimeInvocationService(create_runtime(queue))

    result = invocation.invoke(max_actions=2)

    assert result.requested_count == 2
    assert result.processed_count == 1
    assert result.queue_empty is True
    assert len(result.results) == 2
    assert result.results[0].processed is True
    assert result.results[1].processed is False


def test_worker_runtime_invocation_does_not_introduce_vendor_specific_behavior() -> None:
    queue = ActionQueue()
    queue.enqueue(
        Action(
            type="external.vendor.call",
            target="remote-system",
            payload={"should_not_execute": True},
        )
    )
    invocation = WorkerRuntimeInvocationService(create_runtime(queue))

    result = invocation.invoke()

    processed_result = result.results[0]
    assert processed_result.external_execution_performed is False
    assert processed_result.action is not None
    assert processed_result.action.metadata["external_execution_performed"] is False
    assert "gmail" not in processed_result.action.metadata
    assert "calendar" not in processed_result.action.metadata
    assert "notion" not in processed_result.action.metadata
    assert "slack" not in processed_result.action.metadata
