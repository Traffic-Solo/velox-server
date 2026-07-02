from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.workers.executor import (
    NoOpWorkerExecutor,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerExecutor,
    WorkerExecutorRegistry,
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


def test_worker_executor_registry_registers_executor() -> None:
    registry = WorkerExecutorRegistry()
    executor = SuccessfulExecutor()
    action = Action(
        type="summarize_email",
        target="event-1",
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )

    registry.register(ExecutorRole.CONTENT_SUMMARY, executor)

    assert registry.resolve(action) is executor


def test_worker_executor_registry_registers_executor_by_explicit_role() -> None:
    registry = WorkerExecutorRegistry()
    executor = SuccessfulExecutor()
    action = Action(
        type="summarize_email",
        target="event-1",
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )

    registry.register_role(ExecutorRole.CONTENT_SUMMARY, executor)

    assert registry.resolve(action) is executor
    assert registry.registered_roles() == (ExecutorRole.CONTENT_SUMMARY.value,)


def test_worker_executor_registry_exposes_successful_role_resolution() -> None:
    registry = WorkerExecutorRegistry()
    executor = SuccessfulExecutor()
    action = Action(
        type="summarize_email",
        target="event-1",
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )

    registry.register_role(ExecutorRole.CONTENT_SUMMARY, executor)

    resolution = registry.resolve_with_registration(action)
    assert resolution.executor is executor
    assert resolution.requested_role == ExecutorRole.CONTENT_SUMMARY.value
    assert resolution.registered is True


def test_worker_executor_registry_exposes_fallback_role_resolution() -> None:
    fallback_executor = NoOpWorkerExecutor()
    registry = WorkerExecutorRegistry(fallback_executor=fallback_executor)
    action = Action(
        type="generic_action",
        target="event-1",
        executor_role=ExecutorRole.CONTENT_REVIEW,
    )

    resolution = registry.resolve_with_registration(action)
    assert resolution.executor is fallback_executor
    assert resolution.requested_role == ExecutorRole.CONTENT_REVIEW.value
    assert resolution.registered is False


def test_worker_executor_registry_resolves_string_executor_role() -> None:
    registry = WorkerExecutorRegistry()
    executor = SuccessfulExecutor()
    action = Action(
        type="generic_action",
        target="event-1",
        executor_role="summarizer",
    )

    registry.register("summarizer", executor)

    assert registry.resolve(action) is executor


def test_worker_executor_registry_falls_back_to_no_op_executor() -> None:
    fallback_executor = NoOpWorkerExecutor()
    registry = WorkerExecutorRegistry(fallback_executor=fallback_executor)
    action = Action(type="missing_executor", target="event-1")

    assert registry.resolve(action) is fallback_executor


def test_worker_executor_registry_falls_back_for_unknown_executor_role() -> None:
    fallback_executor = NoOpWorkerExecutor()
    registry = WorkerExecutorRegistry(fallback_executor=fallback_executor)
    action = Action(
        type="generic_action",
        target="event-1",
        executor_role="unknown_role",
    )

    assert registry.resolve(action) is fallback_executor


def test_worker_executor_registry_does_not_introduce_vendor_specific_behavior() -> None:
    registry = WorkerExecutorRegistry()
    action = Action(type="external.vendor.call", target="remote-system")

    executor = registry.resolve(action)
    result = executor.execute(action)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["external_execution_performed"] is False
