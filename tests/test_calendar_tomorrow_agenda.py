"""Deterministic coverage for the local tomorrow Calendar agenda workflow."""

from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from apps.server.src.core.actions import Action
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.integrations.calendar import (
    CALENDAR_LIST_EVENTS_CAPABILITY,
    CALENDAR_LIST_MAX_MAX_PAGES,
    CALENDAR_LIST_MAX_MAX_RESULTS,
)
from apps.server.src.integrations.calendar_agenda import (
    CALENDAR_TOMORROW_INTENT,
    CalendarAgendaWorkflowError,
    CalendarTomorrowAgendaRequest,
    CalendarTomorrowAgendaResult,
    CalendarTomorrowAgendaWorkflow,
)
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerExecutionFailure,
    WorkerExecutionFailureCategory,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)

ACCOUNT_CONTEXT = WorkerAccountContext(
    principal="principal-1",
    account_identifier="calendar-account-1",
)
EVENT = {
    "event_id": "calendar-event-1",
    "title": "Planning",
    "start": "2026-01-02T09:00:00+02:00",
    "end": "2026-01-02T09:30:00+02:00",
    "attendees": ("owner@example.com",),
}
ALLOWLISTED_EVENT_FIELDS = frozenset(
    {"event_id", "title", "start", "end", "attendees"}
)
DEFAULT_NOW = datetime(2026, 1, 1, 12, tzinfo=ZoneInfo("UTC"))


class RecordingOrchestrator:
    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        status: WorkerExecutionStatus = WorkerExecutionStatus.SUCCEEDED,
        failure: WorkerExecutionFailure | None = None,
    ) -> None:
        self.metadata = metadata or complete_metadata()
        self.status = status
        self.failure = failure
        self.calls: list[tuple[Action, WorkerAccountContext | None]] = []

    def execute(
        self,
        action: Action,
        *,
        account_context: WorkerAccountContext | None,
    ) -> WorkerExecutionResult:
        self.calls.append((action, account_context))
        return WorkerExecutionResult(
            action=action,
            status=self.status,
            reason="recorded orchestration result",
            metadata=self.metadata,
            failure=self.failure,
        )


def complete_metadata(
    *,
    events: tuple[dict[str, Any], ...] = (EVENT,),
    aggregate_complete: bool = True,
    skipped_event_count: int = 0,
    termination_reason: str = "exhausted",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "events": events,
        "event_count": len(events),
        "pages_fetched": 1,
        "skipped_event_count": skipped_event_count,
        "aggregate_complete": aggregate_complete,
        "termination_reason": termination_reason,
        **extra,
    }


def agenda_request(
    *,
    intent: Any = CALENDAR_TOMORROW_INTENT,
    account_context: Any = ACCOUNT_CONTEXT,
    timezone: Any = "Europe/Kyiv",
    now: Any = DEFAULT_NOW,
) -> CalendarTomorrowAgendaRequest:
    return CalendarTomorrowAgendaRequest(
        intent=cast(str, intent),
        account_context=cast(WorkerAccountContext, account_context),
        timezone=cast(str, timezone),
        now=cast(datetime, now),
    )


def workflow_over(
    orchestrator: RecordingOrchestrator,
) -> CalendarTomorrowAgendaWorkflow:
    return CalendarTomorrowAgendaWorkflow(cast(Any, orchestrator))


def test_ordinary_tomorrow_window_in_non_dst_timezone() -> None:
    orchestrator = RecordingOrchestrator()

    result = workflow_over(orchestrator).execute(
        agenda_request(timezone="Asia/Kolkata")
    )

    assert result.local_date == "2026-01-02"
    assert result.time_min == "2026-01-02T00:00:00+05:30"
    assert result.time_max == "2026-01-03T00:00:00+05:30"


def test_now_is_converted_to_requested_timezone_before_date_resolution() -> None:
    orchestrator = RecordingOrchestrator()
    now = datetime(2026, 1, 1, 23, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

    result = workflow_over(orchestrator).execute(
        agenda_request(timezone="Europe/Kyiv", now=now)
    )

    assert result.local_date == "2026-01-03"
    assert result.time_min == "2026-01-03T00:00:00+02:00"


def test_spring_forward_constructs_boundaries_with_independent_offsets() -> None:
    result = workflow_over(RecordingOrchestrator()).execute(
        agenda_request(
            timezone="Europe/Berlin",
            now=datetime(2026, 3, 28, 12, tzinfo=ZoneInfo("UTC")),
        )
    )

    assert result.local_date == "2026-03-29"
    assert result.time_min == "2026-03-29T00:00:00+01:00"
    assert result.time_max == "2026-03-30T00:00:00+02:00"
    assert datetime.fromisoformat(result.time_max) - datetime.fromisoformat(
        result.time_min
    ) != pytest.approx(24 * 60 * 60)


def test_fall_back_constructs_boundaries_with_independent_offsets() -> None:
    result = workflow_over(RecordingOrchestrator()).execute(
        agenda_request(
            timezone="Europe/Berlin",
            now=datetime(2026, 10, 24, 12, tzinfo=ZoneInfo("UTC")),
        )
    )

    assert result.local_date == "2026-10-25"
    assert result.time_min == "2026-10-25T00:00:00+02:00"
    assert result.time_max == "2026-10-26T00:00:00+01:00"
    assert datetime.fromisoformat(result.time_max) - datetime.fromisoformat(
        result.time_min
    ) != pytest.approx(24 * 60 * 60)


@pytest.mark.parametrize("timezone", ["Not/A_Zone", "Europe/Kyiv "])
def test_invalid_timezone_fails_before_calendar_execution(timezone: str) -> None:
    orchestrator = RecordingOrchestrator()

    with pytest.raises(CalendarAgendaWorkflowError) as error:
        workflow_over(orchestrator).execute(agenda_request(timezone=timezone))

    assert error.value.field == "timezone"
    assert orchestrator.calls == []


@pytest.mark.parametrize("timezone", ["", " ", "\t"])
def test_blank_timezone_fails_before_calendar_execution(timezone: str) -> None:
    orchestrator = RecordingOrchestrator()

    with pytest.raises(CalendarAgendaWorkflowError) as error:
        workflow_over(orchestrator).execute(agenda_request(timezone=timezone))

    assert error.value.field == "timezone"
    assert orchestrator.calls == []


@pytest.mark.parametrize("timezone", [None, 42, True])
def test_non_string_timezone_fails_before_calendar_execution(timezone: Any) -> None:
    orchestrator = RecordingOrchestrator()

    with pytest.raises(CalendarAgendaWorkflowError) as error:
        workflow_over(orchestrator).execute(agenda_request(timezone=timezone))

    assert error.value.field == "timezone"
    assert orchestrator.calls == []


@pytest.mark.parametrize(
    "now",
    [datetime(2026, 1, 1, 12), "2026-01-01T12:00:00Z", None],
)
def test_naive_or_invalid_now_fails_before_calendar_execution(now: Any) -> None:
    orchestrator = RecordingOrchestrator()

    with pytest.raises(CalendarAgendaWorkflowError) as error:
        workflow_over(orchestrator).execute(agenda_request(now=now))

    assert error.value.field == "now"
    assert orchestrator.calls == []


@pytest.mark.parametrize("intent", ["today", "Tomorrow", " tomorrow", "", None])
def test_unsupported_intent_fails_before_calendar_execution(intent: Any) -> None:
    orchestrator = RecordingOrchestrator()

    with pytest.raises(CalendarAgendaWorkflowError) as error:
        workflow_over(orchestrator).execute(agenda_request(intent=intent))

    assert error.value.field == "intent"
    assert orchestrator.calls == []


@pytest.mark.parametrize(
    ("account_context", "field"),
    [
        (None, "account_context"),
        (WorkerAccountContext(principal=None, account_identifier="account"), "principal"),
        (WorkerAccountContext(principal=" ", account_identifier="account"), "principal"),
        (
            WorkerAccountContext(principal="principal", account_identifier=" account"),
            "account_identifier",
        ),
    ],
)
def test_invalid_account_context_fails_before_calendar_execution(
    account_context: Any,
    field: str,
) -> None:
    orchestrator = RecordingOrchestrator()

    with pytest.raises(CalendarAgendaWorkflowError) as error:
        workflow_over(orchestrator).execute(
            agenda_request(account_context=account_context)
        )

    assert error.value.field == field
    assert orchestrator.calls == []


def test_exact_orchestrator_call_uses_resolved_bounds_and_workflow_limits() -> None:
    orchestrator = RecordingOrchestrator()

    workflow_over(orchestrator).execute(agenda_request())

    assert len(orchestrator.calls) == 1
    action, account_context = orchestrator.calls[0]
    assert action.type == CALENDAR_LIST_EVENTS_CAPABILITY.identifier
    assert action.payload == {
        "time_min": "2026-01-02T00:00:00+02:00",
        "time_max": "2026-01-03T00:00:00+02:00",
        "max_results": CALENDAR_LIST_MAX_MAX_RESULTS,
        "max_pages": CALENDAR_LIST_MAX_MAX_PAGES,
    }
    assert account_context is ACCOUNT_CONTEXT


def test_complete_populated_agenda_exposes_exact_workflow_result() -> None:
    events = (EVENT,)
    result = workflow_over(
        RecordingOrchestrator(metadata=complete_metadata(events=events))
    ).execute(agenda_request())

    assert result == CalendarTomorrowAgendaResult(
        intent="tomorrow",
        timezone="Europe/Kyiv",
        local_date="2026-01-02",
        time_min="2026-01-02T00:00:00+02:00",
        time_max="2026-01-03T00:00:00+02:00",
        events=events,
        event_count=1,
        aggregate_complete=True,
        skipped_event_count=0,
        termination_reason="exhausted",
    )
    assert result.events is events


def test_complete_empty_agenda_is_a_valid_success() -> None:
    result = workflow_over(
        RecordingOrchestrator(metadata=complete_metadata(events=()))
    ).execute(agenda_request())

    assert result.events == ()
    assert result.event_count == 0
    assert result.aggregate_complete is True
    assert result.termination_reason == "exhausted"


def test_partial_aggregate_propagates_completeness_and_skipped_count() -> None:
    result = workflow_over(
        RecordingOrchestrator(
            metadata=complete_metadata(
                aggregate_complete=False,
                skipped_event_count=2,
            )
        )
    ).execute(agenda_request())

    assert result.aggregate_complete is False
    assert result.skipped_event_count == 2


@pytest.mark.parametrize("termination_reason", ["page_limit", "repeated_page_token"])
def test_incomplete_termination_reason_is_propagated(
    termination_reason: str,
) -> None:
    result = workflow_over(
        RecordingOrchestrator(
            metadata=complete_metadata(
                aggregate_complete=False,
                termination_reason=termination_reason,
            )
        )
    ).execute(agenda_request())

    assert result.aggregate_complete is False
    assert result.termination_reason == termination_reason


def test_underlying_orchestration_failure_fails_the_workflow() -> None:
    orchestrator = RecordingOrchestrator(
        status=WorkerExecutionStatus.FAILED,
        failure=WorkerExecutionFailure(
            category=WorkerExecutionFailureCategory.TRANSIENT,
            message="provider failed safely",
        ),
    )

    with pytest.raises(CalendarAgendaWorkflowError) as error:
        workflow_over(orchestrator).execute(agenda_request())

    assert error.value.category == WorkerExecutionFailureCategory.TRANSIENT


def test_five_field_calendar_event_allowlist_is_unchanged() -> None:
    events = (EVENT,)

    result = workflow_over(
        RecordingOrchestrator(metadata=complete_metadata(events=events))
    ).execute(agenda_request())

    assert result.events is events
    assert set(result.events[0]) == ALLOWLISTED_EVENT_FIELDS


def test_page_token_metadata_never_reaches_workflow_result_surfaces() -> None:
    sentinel = "SENTINEL-PAGE-TOKEN-MUST-NOT-ESCAPE"
    result = workflow_over(
        RecordingOrchestrator(
            metadata=complete_metadata(
                next_page_token=sentinel,
                page_token=sentinel,
            )
        )
    ).execute(agenda_request())

    assert sentinel not in repr(result)
    assert not hasattr(result, "next_page_token")
    assert not hasattr(result, "page_token")


def test_container_composes_workflow_over_its_calendar_orchestrator() -> None:
    container = ApplicationContainer()

    assert (
        container.calendar_tomorrow_agenda_workflow._orchestrator
        is container.calendar_event_list_orchestrator
    )
    assert (
        container.calendar_event_list_orchestrator.executor
        is container.calendar_worker_executor
    )
