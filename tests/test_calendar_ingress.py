from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest
from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.events import (
    EventProcessingError,
    IntegrationRouteContext,
    NormalizationError,
    UniversalEvent,
)
from apps.server.src.integrations.calendar_ingress import (
    CalendarEventNormalizer,
    CalendarIngressAdapter,
)
from apps.server.src.workers.executor import (
    WorkerExecutionFailureCategory,
    WorkerExecutionStatus,
)


def calendar_route(
    provider: str = "calendar",
) -> IntegrationRouteContext:
    return IntegrationRouteContext(
        provider=provider,
        principal=ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT.principal,
        account_identifier=(
            ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT.account_identifier
        ),
    )


def raw_calendar_event(event_id: object = "calendar-event-1") -> dict[str, object]:
    return {
        "event_id": event_id,
        "title": "Sprint 1 planning",
        "start": "2026-07-27T09:00:00Z",
        "end": "2026-07-27T09:30:00Z",
        "attendees": ["owner@example.com", "team@example.com"],
    }


def test_calendar_normalizer_copies_mapping_into_calendar_event() -> None:
    normalizer = CalendarEventNormalizer()
    raw = raw_calendar_event()
    original = {
        **raw,
        "attendees": list(raw["attendees"]),  # type: ignore[arg-type]
    }

    event = normalizer.normalize(raw)

    assert isinstance(event, UniversalEvent)
    assert event.source == "calendar"
    assert event.type == "calendar.event"
    assert event.payload == {
        **raw,
        "calendar_event_id": "calendar-event-1",
    }
    assert raw == original
    assert event.payload is not raw


@pytest.mark.parametrize("event_id", [" calendar-event-1 ", "", 42, None])
def test_calendar_normalizer_preserves_external_id_unchanged(
    event_id: object,
) -> None:
    event = CalendarEventNormalizer().normalize({"event_id": event_id})

    assert "calendar_event_id" in event.payload
    assert event.payload["calendar_event_id"] == event_id


def test_calendar_normalizer_does_not_invent_missing_external_id() -> None:
    event = CalendarEventNormalizer().normalize({"title": "No ID"})

    assert "calendar_event_id" not in event.payload


def test_calendar_normalizer_keeps_route_like_fields_out_of_metadata() -> None:
    raw = {
        "provider": "untrusted",
        "capability_provider": "untrusted",
        "account_context": {"account_identifier": "untrusted"},
        "principal": "untrusted",
        "account_identifier": "untrusted",
    }

    event = CalendarEventNormalizer().normalize(raw)

    assert event.payload == raw
    assert set(raw).isdisjoint(event.metadata)


def test_calendar_normalizer_rejects_non_mapping_input() -> None:
    with pytest.raises(NormalizationError):
        CalendarEventNormalizer().normalize("not a mapping")  # type: ignore[arg-type]


def test_adapter_calls_dependencies_once_and_preserves_explicit_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = ApplicationContainer()
    raw = {
        **raw_calendar_event(),
        "provider": "gmail",
        "capability_provider": "gmail",
        "account_context": {"account_identifier": "untrusted"},
    }
    route = calendar_route()
    normalize_spy = Mock(wraps=container.calendar_event_normalizer.normalize)
    accept_spy = Mock(wraps=container.event_workflow_service.accept)
    process_spy = Mock(wraps=container.event_workflow_service.process)
    monkeypatch.setattr(container.calendar_event_normalizer, "normalize", normalize_spy)
    monkeypatch.setattr(container.event_workflow_service, "accept", accept_spy)
    monkeypatch.setattr(container.event_workflow_service, "process", process_spy)

    result = container.calendar_ingress_adapter.ingest(
        raw,
        integration_route=route,
    )

    normalize_spy.assert_called_once_with(raw)
    accept_spy.assert_called_once_with(result.event)
    process_spy.assert_called_once_with(
        result.event.id,
        integration_route=route,
    )
    assert result.processing.processed_event.integration_route is route
    assert container.worker_execution_observer.list() == []
    with pytest.raises(FrozenInstanceError):
        result.event = result.event  # type: ignore[misc]


def test_normalization_failure_does_not_call_workflow() -> None:
    workflow = Mock()
    adapter = CalendarIngressAdapter(
        normalizer=CalendarEventNormalizer(),
        workflow_service=workflow,
    )

    with pytest.raises(NormalizationError):
        adapter.ingest(  # type: ignore[arg-type]
            "not a mapping",
            integration_route=calendar_route(),
        )

    workflow.accept.assert_not_called()
    workflow.process.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "expected_value"),
    [
        ({"title": "Missing ID"}, None),
        ({"event_id": ""}, ""),
        ({"event_id": 42}, 42),
    ],
)
def test_invalid_calendar_id_fails_only_after_explicit_worker_invocation(
    raw: dict[str, object],
    expected_value: object,
) -> None:
    container = ApplicationContainer()
    ingress = container.calendar_ingress_adapter.ingest(
        raw,
        integration_route=calendar_route(),
    )
    action = ingress.processing.permission_evaluations[0].action

    if "event_id" in raw:
        assert action.payload["calendar_event_id"] == expected_value
    else:
        assert "calendar_event_id" not in action.payload
    assert container.worker_execution_observer.list() == []

    invocation = container.worker_runtime_invocation.invoke()

    worker_result = invocation.results[0]
    assert worker_result.execution_status == WorkerExecutionStatus.FAILED
    assert worker_result.action is not None
    failure = worker_result.action.metadata["worker_execution"]["failure"]
    assert failure["category"] == WorkerExecutionFailureCategory.PERMANENT.value
    assert failure["metadata"]["field"] == "calendar_event_id"


def test_processing_failure_remains_pending_and_replayable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = ApplicationContainer()
    pipeline = container.event_processing_pipeline
    original_process = pipeline.process
    monkeypatch.setattr(
        pipeline,
        "process",
        Mock(side_effect=RuntimeError("temporary failure")),
    )

    with pytest.raises(EventProcessingError):
        container.calendar_ingress_adapter.ingest(
            raw_calendar_event(),
            integration_route=calendar_route(),
        )

    event = container.event_repository.list_events()[0]
    assert container.event_lifecycle_states[event.id].status == "failed"
    assert container.event_inbox.list_pending() == [event]
    monkeypatch.setattr(pipeline, "process", original_process)

    container.event_workflow_service.process(
        event.id,
        integration_route=calendar_route(),
    )

    assert container.event_lifecycle_states[event.id].status == "processed"
    assert container.event_inbox.list_pending() == []


def test_mismatched_provider_route_fails_closed_without_calendar_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = ApplicationContainer()
    calendar_executor_spy = Mock(wraps=container.calendar_worker_executor.execute)
    calendar_capability_spy = Mock(
        wraps=container.calendar_worker_executor.capabilities.meeting_context.prepare
    )
    calendar_provider_spy = Mock(
        wraps=container.calendar_worker_executor.provider_composition.execute
    )
    monkeypatch.setattr(
        container.calendar_worker_executor,
        "execute",
        calendar_executor_spy,
    )
    monkeypatch.setattr(
        container.calendar_worker_executor.capabilities.meeting_context,
        "prepare",
        calendar_capability_spy,
    )
    monkeypatch.setattr(
        container.calendar_worker_executor.provider_composition,
        "execute",
        calendar_provider_spy,
    )
    container.calendar_ingress_adapter.ingest(
        raw_calendar_event(),
        integration_route=calendar_route(provider="gmail"),
    )

    invocation = container.worker_runtime_invocation.invoke()

    assert invocation.results[0].execution_status == WorkerExecutionStatus.SKIPPED
    assert invocation.results[0].action is not None
    execution = invocation.results[0].action.metadata["worker_execution"]
    assert execution["executor_registered"] is False
    assert execution["routing_reason"] == "no_handler"
    calendar_executor_spy.assert_not_called()
    calendar_capability_spy.assert_not_called()
    calendar_provider_spy.assert_not_called()


def test_valid_calendar_ingress_completes_deterministic_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = ApplicationContainer()
    raw = raw_calendar_event()
    route = calendar_route()
    calendar_capability_spy = Mock(
        wraps=container.calendar_worker_executor.capabilities.meeting_context.prepare
    )
    calendar_provider_spy = Mock(
        wraps=container.calendar_worker_executor.provider_composition.execute
    )
    monkeypatch.setattr(
        container.calendar_worker_executor.capabilities.meeting_context,
        "prepare",
        calendar_capability_spy,
    )
    monkeypatch.setattr(
        container.calendar_worker_executor.provider_composition,
        "execute",
        calendar_provider_spy,
    )

    ingress = container.calendar_ingress_adapter.ingest(
        raw,
        integration_route=route,
    )

    event = ingress.event
    assert container.event_repository.get_event(event.id) is event
    assert event.source == "calendar"
    assert event.type == "calendar.event"
    assert str(event.id) != "calendar-event-1"
    assert event.payload["calendar_event_id"] == "calendar-event-1"
    assert container.event_lifecycle_states[event.id].status == "processed"
    assert container.event_inbox.list_pending() == []
    assert ingress.processing.processed_event.integration_route is route
    assert ingress.processing.processed_event.classification.category == "calendar"
    evaluations = ingress.processing.permission_evaluations
    assert len(evaluations) == 1
    action = evaluations[0].action
    action_lifecycle = container.action_lifecycle_repository.get(action.id)
    assert action.type == "prepare_meeting"
    assert action.target == str(event.id)
    assert action.payload["calendar_event_id"] == "calendar-event-1"
    assert action.payload["capability_provider"] == "calendar"
    assert action.payload["account_context"] == (
        ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT.as_metadata()
    )
    assert evaluations[0].decision.status.value == "allowed"
    assert action_lifecycle is not None
    assert action_lifecycle.status == ActionStatus.APPROVED
    assert container.action_queue.list() == [action]
    assert container.worker_execution_observer.list() == []
    calendar_capability_spy.assert_not_called()
    calendar_provider_spy.assert_not_called()

    invocation = container.worker_runtime_invocation.invoke()

    calendar_capability_spy.assert_called_once()
    calendar_provider_spy.assert_called_once()
    worker_result = invocation.results[0]
    assert worker_result.processed is True
    assert worker_result.execution_status == WorkerExecutionStatus.SUCCEEDED
    assert worker_result.external_execution_performed is False
    assert worker_result.action is not None
    execution = worker_result.action.metadata["worker_execution"]
    assert execution["executor_registered"] is True
    assert execution["matched_provider"] == "calendar"
    assert execution["metadata"]["found"] is True
    assert execution["metadata"]["event"]["event_id"] == "calendar-event-1"
    assert execution["metadata"]["external_execution_performed"] is False
