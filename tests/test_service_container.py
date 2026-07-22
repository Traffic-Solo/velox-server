from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.core.permission import PermissionDecision, PermissionEngine
from apps.server.src.integrations.calendar import (
    CALENDAR_EXECUTOR_ROLE,
    CALENDAR_WORKER_CAPABILITIES,
    CalendarWorkerExecutor,
)
from apps.server.src.integrations.gmail import (
    GMAIL_EXECUTOR_ROLE,
    GMAIL_WORKER_CAPABILITIES,
    GmailArchiveCapability,
    GmailReadCapability,
    GmailSendCapability,
    GmailWorkerExecutor,
)
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerCapabilityRoute,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)


class ContainerRecordingExecutor:
    def __init__(self) -> None:
        self.called_actions: list[Action] = []

    def execute(
        self,
        action: Action,
        *,
        capability: str | None = None,
        account_context: WorkerAccountContext | None = None,
    ) -> WorkerExecutionResult:
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


def test_container_exposes_worker_execution_observer() -> None:
    container = ApplicationContainer()

    assert container.worker_execution_observer is not None


def test_container_registers_gmail_worker_executor() -> None:
    container = ApplicationContainer()
    action = Action(
        type="summarize_email",
        target="gmail-message-1",
        payload={
            "account_context": (
                ApplicationContainer.GMAIL_ACCOUNT_CONTEXT.as_metadata()
            ),
        },
        executor_role=GMAIL_EXECUTOR_ROLE,
    )

    resolution = container.worker_executor_registry.resolve_with_registration(action)

    assert resolution.registered is True
    assert resolution.requested_role == ExecutorRole.CONTENT_SUMMARY.value
    assert resolution.executor is container.gmail_worker_executor
    assert (
        resolution.matched_account_context
        == ApplicationContainer.GMAIL_ACCOUNT_CONTEXT
    )
    assert isinstance(resolution.executor, GmailWorkerExecutor)


def test_container_registers_calendar_worker_executor() -> None:
    container = ApplicationContainer()
    action = Action(
        type="prepare_calendar_context",
        target="calendar-placeholder",
        payload={
            "account_context": (
                ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT.as_metadata()
            ),
        },
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    resolution = container.worker_executor_registry.resolve_with_registration(action)

    assert resolution.registered is True
    assert resolution.requested_role == ExecutorRole.CONTEXT_PREPARATION.value
    assert resolution.executor is container.calendar_worker_executor
    assert (
        resolution.matched_account_context
        == ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT
    )
    assert isinstance(resolution.executor, CalendarWorkerExecutor)


def test_container_registered_gmail_executor_exposes_capability_contracts() -> None:
    container = ApplicationContainer()

    assert isinstance(
        container.gmail_worker_executor.capabilities.read,
        GmailReadCapability,
    )
    assert isinstance(
        container.gmail_worker_executor.capabilities.send,
        GmailSendCapability,
    )
    assert isinstance(
        container.gmail_worker_executor.capabilities.archive,
        GmailArchiveCapability,
    )


def test_container_registers_provider_capability_declarations() -> None:
    container = ApplicationContainer()

    assert container.gmail_worker_executor.worker_capabilities == (
        GMAIL_WORKER_CAPABILITIES
    )
    assert container.calendar_worker_executor.worker_capabilities == (
        CALENDAR_WORKER_CAPABILITIES
    )
    assert container.worker_executor_registry.registered_capabilities() == (
        GMAIL_WORKER_CAPABILITIES + CALENDAR_WORKER_CAPABILITIES
    )


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
        type="prepare_meeting_test",
        target="event-1",
        executor_role=ExecutorRole.CONTEXT_PREPARATION,
    )
    executor = ContainerRecordingExecutor()
    container.worker_executor_registry.register_capability_provider(
        WorkerCapabilityRoute(
            role=ExecutorRole.CONTEXT_PREPARATION,
            capability="prepare_meeting_test",
            provider="calendar",
        ),
        executor=executor,
    )
    container.action_queue.enqueue(action)

    result = container.worker_runtime.process_next()

    assert result.execution_status == WorkerExecutionStatus.SUCCEEDED
    assert executor.called_actions == [action]


def test_container_gmail_route_requires_explicit_account_context() -> None:
    container = ApplicationContainer()
    action = Action(
        type="summarize_email",
        target="gmail-message-1",
        payload={"capability_provider": "gmail"},
        executor_role=GMAIL_EXECUTOR_ROLE,
    )

    resolution = container.worker_executor_registry.resolve_with_registration(action)

    assert resolution.executor is container.worker_executor
    assert resolution.registered is False
    assert resolution.routing_reason == "missing_account_context"


def test_container_gmail_route_rejects_wrong_account_context() -> None:
    container = ApplicationContainer()
    action = Action(
        type="summarize_email",
        target="gmail-message-1",
        payload={
            "capability_provider": "gmail",
            "account_context": {
                "principal": "velox-local-principal",
                "account_identifier": "calendar-local-account",
            },
        },
        executor_role=GMAIL_EXECUTOR_ROLE,
    )

    resolution = container.worker_executor_registry.resolve_with_registration(action)

    assert resolution.executor is container.worker_executor
    assert resolution.registered is False
    assert resolution.routing_reason == "no_handler"


def test_container_worker_runtime_routes_matching_action_to_gmail_executor() -> None:
    container = ApplicationContainer()
    action = Action(
        type="summarize_email",
        target="gmail-message-1",
        payload={
            "account_context": (
                ApplicationContainer.GMAIL_ACCOUNT_CONTEXT.as_metadata()
            ),
        },
        executor_role=GMAIL_EXECUTOR_ROLE,
    )
    container.action_queue.enqueue(action)

    result = container.worker_runtime.process_next()

    assert result.processed is True
    assert result.execution_status == WorkerExecutionStatus.SKIPPED
    assert result.external_execution_performed is False
    assert result.action is not None
    execution_metadata = result.action.metadata["worker_execution"]
    assert execution_metadata["requested_role"] == ExecutorRole.CONTENT_SUMMARY.value
    assert execution_metadata["executor_registered"] is True
    assert execution_metadata["requested_capability"] == "summarize_email"
    assert execution_metadata["matched_provider"] == "gmail"
    assert execution_metadata["matched_account_context"] == (
        ApplicationContainer.GMAIL_ACCOUNT_CONTEXT.as_metadata()
    )
    assert execution_metadata["metadata"] == {
        "external_execution_performed": False,
        "integration": "gmail",
        "placeholder": True,
        "skipped": True,
    }


def test_container_worker_runtime_constructs_gmail_request_with_routed_account() -> None:
    container = ApplicationContainer()
    action = Action(
        type="gmail.read",
        target="event-1",
        payload={
            "message_id": "gmail-message-1",
            "account_context": (
                ApplicationContainer.GMAIL_ACCOUNT_CONTEXT.as_metadata()
            ),
            "provider": "calendar",
            "account": "untrusted-account",
        },
        executor_role=GMAIL_EXECUTOR_ROLE,
    )
    container.action_queue.enqueue(action)

    result = container.worker_runtime.process_next()

    assert result.execution_status == WorkerExecutionStatus.SUCCEEDED
    assert result.external_execution_performed is False
    assert result.action is not None
    execution_metadata = result.action.metadata["worker_execution"]
    assert execution_metadata["matched_provider"] == "gmail"
    assert execution_metadata["account_context_used"] == (
        ApplicationContainer.GMAIL_ACCOUNT_CONTEXT.as_metadata()
    )
    assert execution_metadata["metadata"]["provider_request"][
        "account_context"
    ] == ApplicationContainer.GMAIL_ACCOUNT_CONTEXT.as_metadata()
    assert execution_metadata["metadata"]["provider_response"]["account"] == (
        ApplicationContainer.GMAIL_ACCOUNT_CONTEXT.account_identifier
    )


def test_container_worker_runtime_routes_matching_action_to_calendar_executor() -> None:
    container = ApplicationContainer()
    action = Action(
        type="prepare_calendar_context",
        target="calendar-placeholder",
        payload={
            "account_context": (
                ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT.as_metadata()
            ),
        },
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )
    container.action_queue.enqueue(action)

    result = container.worker_runtime.process_next()

    assert result.processed is True
    assert result.execution_status == WorkerExecutionStatus.SUCCEEDED
    assert result.external_execution_performed is False
    assert result.action is not None
    execution_metadata = result.action.metadata["worker_execution"]
    assert execution_metadata["requested_role"] == ExecutorRole.CONTEXT_PREPARATION.value
    assert execution_metadata["executor_registered"] is True
    assert execution_metadata["requested_capability"] == "prepare_calendar_context"
    assert execution_metadata["matched_provider"] == "calendar"
    assert execution_metadata["matched_account_context"] == (
        ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT.as_metadata()
    )
    assert execution_metadata["account_context_used"] == (
        ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT.as_metadata()
    )
    assert execution_metadata["metadata"]["provider_request"][
        "account_context"
    ] == ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT.as_metadata()
    assert execution_metadata["metadata"]["provider_response"]["account"] == (
        ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT.account_identifier
    )


def test_container_wired_worker_runtime_records_execution_observation() -> None:
    container = ApplicationContainer()
    action = Action(
        type="prepare_meeting_test",
        target="event-1",
        executor_role=ExecutorRole.CONTEXT_PREPARATION,
    )
    executor = ContainerRecordingExecutor()
    container.worker_executor_registry.register_capability_provider(
        WorkerCapabilityRoute(
            role=ExecutorRole.CONTEXT_PREPARATION,
            capability="prepare_meeting_test",
            provider="calendar",
        ),
        executor=executor,
    )
    container.action_queue.enqueue(action)

    result = container.worker_runtime.process_next()

    observations = container.worker_execution_observer.list()
    assert result.processed is True
    assert len(observations) == 1
    assert observations[0].action_id == action.id
    assert observations[0].requested_role == ExecutorRole.CONTEXT_PREPARATION.value
    assert observations[0].executor_registered is True
    assert observations[0].status == "succeeded"


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
