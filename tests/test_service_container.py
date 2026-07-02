from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.permission import PermissionDecision, PermissionEngine
from apps.server.src.workers.executor import WorkerExecutionResult, WorkerExecutionStatus


class ContainerRecordingExecutor:
    def __init__(self) -> None:
        self.called_actions: list[Action] = []

    def execute(self, action: Action) -> WorkerExecutionResult:
        self.called_actions.append(action)
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            metadata={"handled_by": "container-recording-executor"},
        )


def test_container_exposes_event_repository() -> None:
    container = ApplicationContainer()

    assert container.event_repository is not None


def test_container_exposes_event_inbox() -> None:
    container = ApplicationContainer()

    assert container.event_inbox is not None


def test_container_exposes_event_classifier() -> None:
    container = ApplicationContainer()

    assert container.event_classifier is not None


def test_container_exposes_context_resolver() -> None:
    container = ApplicationContainer()

    assert container.context_resolver is not None


def test_container_exposes_event_processing_pipeline() -> None:
    container = ApplicationContainer()

    assert container.event_processing_pipeline is not None


def test_container_exposes_planner() -> None:
    container = ApplicationContainer()

    assert container.planner is not None


def test_container_exposes_permission_engine() -> None:
    container = ApplicationContainer()

    assert container.permission_engine is not None


def test_container_exposes_permission_runtime() -> None:
    container = ApplicationContainer()

    assert container.permission_runtime is not None


def test_container_exposes_worker_runtime() -> None:
    container = ApplicationContainer()

    assert container.worker_runtime is not None


def test_container_exposes_worker_runtime_invocation() -> None:
    container = ApplicationContainer()

    assert container.worker_runtime_invocation is not None


def test_container_exposes_worker_executor() -> None:
    container = ApplicationContainer()

    assert container.worker_executor is not None


def test_container_exposes_worker_executor_registry() -> None:
    container = ApplicationContainer()

    assert container.worker_executor_registry is not None


def test_container_exposes_wired_worker_runtime() -> None:
    container = ApplicationContainer()
    action = Action(type="external.vendor.call", target="remote-system")
    container.action_queue.enqueue(action)

    result = container.worker_runtime.process_next()

    assert result.processed is True
    assert result.external_execution_performed is False
    assert result.action is not None
    assert result.action.metadata["external_execution_performed"] is False


def test_container_wired_worker_runtime_uses_executor_registry() -> None:
    container = ApplicationContainer()
    action = Action(
        type="prepare_meeting",
        target="event-1",
        executor_role=ExecutorRole.CONTEXT_PREPARATION,
    )
    executor = ContainerRecordingExecutor()
    container.worker_executor_registry.register(
        ExecutorRole.CONTEXT_PREPARATION,
        executor,
    )
    container.action_queue.enqueue(action)

    result = container.worker_runtime.process_next()

    assert result.execution_status == WorkerExecutionStatus.SUCCEEDED
    assert executor.called_actions == [action]


def test_container_worker_runtime_invocation_processes_queue() -> None:
    container = ApplicationContainer()
    action = Action(type="prepare_meeting", target="event-1")
    container.action_queue.enqueue(action)

    result = container.worker_runtime_invocation.invoke()

    assert result.processed_count == 1
    assert result.results[0].processed is True
    assert result.results[0].action is not None
    assert result.results[0].action.id == action.id


def test_container_permission_engine_satisfies_contract() -> None:
    container = ApplicationContainer()
    engine: PermissionEngine = container.permission_engine

    decision = engine.evaluate(Action(type="summarize_email", target="event-1"))

    assert isinstance(decision, PermissionDecision)


def test_container_exposes_action_lifecycle_manager() -> None:
    container = ApplicationContainer()

    assert container.action_lifecycle_manager is not None


def test_get_container_returns_application_container() -> None:
    container = get_container()

    assert isinstance(container, ApplicationContainer)


def test_get_container_returns_same_instance() -> None:
    first_container = get_container()
    second_container = get_container()

    assert first_container is second_container


def test_get_container_exposes_registered_permission_engine() -> None:
    container = get_container()

    assert container.permission_engine is not None


def test_get_container_exposes_registered_action_lifecycle_manager() -> None:
    container = get_container()

    assert container.action_lifecycle_manager is not None


def test_get_container_exposes_registered_worker_runtime() -> None:
    container = get_container()

    assert container.worker_runtime is not None


def test_get_container_exposes_registered_worker_runtime_invocation() -> None:
    container = get_container()

    assert container.worker_runtime_invocation is not None
