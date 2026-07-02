from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action
from apps.server.src.workers.executor import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerExecutor,
)
from apps.server.src.workers.runtime import WorkerRuntime


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
