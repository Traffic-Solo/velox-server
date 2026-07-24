from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import (
    InMemoryActionLifecycleRepository,
)
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.approvals import InMemoryPendingApprovalRegistry
from apps.server.src.core.events import (
    BaseContextResolver,
    DuplicateEventError,
    EventInbox,
    EventLifecycleConflictError,
    EventLifecycleManager,
    EventLifecycleState,
    EventNotFoundError,
    EventProcessingError,
    EventProcessingPipeline,
    EventStore,
    EventWorkflowService,
    IntegrationRouteContext,
    RuleBasedEventClassifier,
    UniversalEvent,
)
from apps.server.src.core.permission import (
    BasePermissionEngine,
    PermissionEngineRuntime,
)
from apps.server.src.core.planner import Planner


class MixedPermissionPlanner:
    def plan(self, processed_event) -> list[Action]:
        return [
            Action(
                type="review_pull_request",
                target=str(processed_event.event.id),
                executor_role=ExecutorRole.CONTENT_REVIEW,
            ),
            Action(
                type="unsafe.action",
                target=str(processed_event.event.id),
                executor_role=ExecutorRole.CONTENT_REVIEW,
            ),
        ]


def create_event(**overrides: object) -> UniversalEvent:
    values: dict[str, object] = {
        "source": "github",
        "type": "pull_request.opened",
        "timestamp": datetime.now(UTC),
        "payload": {"number": 1},
        "metadata": {},
    }
    values.update(overrides)
    return UniversalEvent.model_validate(values)


def create_workflow(
    *,
    planner: Planner | None = None,
) -> tuple[
    EventWorkflowService,
    EventStore,
    EventInbox,
    dict,
    EventProcessingPipeline,
    PermissionEngineRuntime,
    ActionQueue,
]:
    repository = EventStore()
    inbox = EventInbox()
    lifecycle_states: dict = {}
    pipeline = EventProcessingPipeline(
        classifier=RuleBasedEventClassifier(),
        context_resolver=BaseContextResolver(),
    )
    lifecycle_repository = InMemoryActionLifecycleRepository()
    permission_runtime = PermissionEngineRuntime(
        permission_engine=BasePermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
        lifecycle_repository=lifecycle_repository,
        pending_approval_registry=InMemoryPendingApprovalRegistry(),
    )
    action_queue = ActionQueue()
    service = EventWorkflowService(
        event_repository=repository,
        event_inbox=inbox,
        event_lifecycle_manager=EventLifecycleManager(),
        event_lifecycle_states=lifecycle_states,
        event_processing_pipeline=pipeline,
        planner=planner or MixedPermissionPlanner(),
        permission_runtime=permission_runtime,
        action_queue=action_queue,
    )
    return (
        service,
        repository,
        inbox,
        lifecycle_states,
        pipeline,
        permission_runtime,
        action_queue,
    )


def test_accept_stores_enqueues_and_transitions_event_to_pending() -> None:
    service, repository, inbox, states, _, _, _ = create_workflow()
    event = create_event()

    result = service.accept(event)

    assert result.event is event
    assert repository.get_event(event.id) is event
    assert inbox.list_pending() == [event]
    assert states[event.id].status == "pending"


def test_duplicate_acceptance_is_rejected_without_partial_mutation() -> None:
    service, repository, inbox, states, _, _, _ = create_workflow()
    event = create_event()
    service.accept(event)
    repository_before = repository.list_events()
    inbox_before = inbox.list_pending()
    states_before = dict(states)

    with pytest.raises(DuplicateEventError, match=f"event {event.id} already exists"):
        service.accept(event)

    assert repository.list_events() == repository_before
    assert inbox.list_pending() == inbox_before
    assert states == states_before


def test_processing_missing_event_raises_not_found() -> None:
    service, _, _, _, _, _, _ = create_workflow()

    with pytest.raises(EventNotFoundError):
        service.process(uuid4())


def test_successful_processing_transitions_and_invokes_collaborators_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, inbox, states, _, permission_runtime, action_queue = create_workflow()
    event = create_event()
    service.accept(event)
    planner_spy = Mock(wraps=service.planner.plan)
    permission_spy = Mock(wraps=permission_runtime.evaluate)
    monkeypatch.setattr(service.planner, "plan", planner_spy)
    monkeypatch.setattr(permission_runtime, "evaluate", permission_spy)

    result = service.process(event.id)

    assert states[event.id].status == "processed"
    assert inbox.list_pending() == []
    planner_spy.assert_called_once_with(result.processed_event)
    permission_spy.assert_called_once()
    assert [action.type for action in action_queue.list()] == ["review_pull_request"]
    assert [item.action.type for item in result.permission_evaluations] == [
        "review_pull_request",
        "unsafe.action",
    ]


def test_explicit_route_reaches_pipeline_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, _, pipeline, _, _ = create_workflow()
    event = create_event(source="calendar", type="event.updated")
    route = IntegrationRouteContext(
        provider="calendar",
        principal="person@example.test",
        account_identifier="calendar-account",
    )
    service.accept(event)
    pipeline_spy = Mock(wraps=pipeline.process)
    monkeypatch.setattr(pipeline, "process", pipeline_spy)

    result = service.process(event.id, integration_route=route)

    pipeline_spy.assert_called_once_with(event, integration_route=route)
    assert result.processed_event.integration_route is route


def test_missing_route_is_not_invented_from_event_payload_or_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, _, pipeline, _, _ = create_workflow()
    route_fields = {
        "provider": "untrusted",
        "capability_provider": "untrusted",
        "account_context": {"account_identifier": "untrusted"},
    }
    event = create_event(
        source="calendar",
        type="event.updated",
        payload=route_fields,
        metadata=route_fields,
    )
    original = event.model_copy(deep=True)
    service.accept(event)
    pipeline_spy = Mock(wraps=pipeline.process)
    monkeypatch.setattr(pipeline, "process", pipeline_spy)

    result = service.process(event.id)

    pipeline_spy.assert_called_once_with(event)
    assert result.processed_event.integration_route is None
    assert event == original


def test_pipeline_failure_marks_failed_preserves_inbox_and_chains_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, inbox, states, pipeline, _, _ = create_workflow()
    event = create_event()
    service.accept(event)
    failure = RuntimeError("internal pipeline detail")
    monkeypatch.setattr(pipeline, "process", Mock(side_effect=failure))

    with pytest.raises(EventProcessingError) as raised:
        service.process(event.id)

    assert states[event.id].status == "failed"
    assert states[event.id].reason == "internal pipeline detail"
    assert inbox.list_pending() == [event]
    assert raised.value.__cause__ is failure


def test_failed_event_remains_replayable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, inbox, states, pipeline, _, _ = create_workflow()
    event = create_event()
    service.accept(event)
    original_process = pipeline.process
    monkeypatch.setattr(
        pipeline,
        "process",
        Mock(side_effect=RuntimeError("temporary failure")),
    )
    with pytest.raises(EventProcessingError):
        service.process(event.id)
    monkeypatch.setattr(pipeline, "process", original_process)

    service.process(event.id)

    assert states[event.id].status == "processed"
    assert inbox.list_pending() == []


def test_lifecycle_conflict_uses_core_exception_contract() -> None:
    service, _, _, states, _, _, _ = create_workflow()
    event = create_event()
    service.accept(event)
    states[event.id] = EventLifecycleState(event_id=event.id, status="processed")

    with pytest.raises(
        EventLifecycleConflictError,
        match="invalid lifecycle transition: processed -> processing",
    ):
        service.process(event.id)
