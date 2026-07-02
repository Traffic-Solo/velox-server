from apps.server.src.core.actions import Action
from apps.server.src.workers.executor import (
    NoOpWorkerExecutor,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerExecutor,
)


class SuccessfulExecutor:
    def execute(self, action: Action) -> WorkerExecutionResult:
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            metadata={"handled_by": "test-executor"},
        )


class FailedExecutor:
    def execute(self, action: Action) -> WorkerExecutionResult:
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.FAILED,
            reason="execution failed",
        )


def test_worker_executor_contract_shape() -> None:
    executor = SuccessfulExecutor()

    assert isinstance(executor, WorkerExecutor)


def test_worker_executor_successful_execution_result() -> None:
    action = Action(type="summarize_email", target="event-1")
    executor: WorkerExecutor = SuccessfulExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.reason is None
    assert result.metadata == {"handled_by": "test-executor"}


def test_worker_executor_failed_execution_result() -> None:
    action = Action(type="summarize_email", target="event-1")
    executor: WorkerExecutor = FailedExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.FAILED
    assert result.reason == "execution failed"


def test_no_op_worker_executor_is_safe_default() -> None:
    action = Action(type="external.vendor.call", target="remote-system")
    executor: WorkerExecutor = NoOpWorkerExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["external_execution_performed"] is False
