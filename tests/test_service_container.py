from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.core.actions import Action
from apps.server.src.core.permission import PermissionDecision, PermissionEngine


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


def test_container_exposes_worker_executor() -> None:
    container = ApplicationContainer()

    assert container.worker_executor is not None


def test_container_exposes_wired_worker_runtime() -> None:
    container = ApplicationContainer()
    action = Action(type="external.vendor.call", target="remote-system")
    container.action_queue.enqueue(action)

    result = container.worker_runtime.process_next()

    assert result.processed is True
    assert result.external_execution_performed is False
    assert result.action is not None
    assert result.action.metadata["external_execution_performed"] is False


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
