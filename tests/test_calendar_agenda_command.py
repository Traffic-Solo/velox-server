"""Deterministic semantic command boundary coverage."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.integrations import calendar_agenda_command as composition
from apps.server.src.integrations.calendar_agenda import (
    CalendarAgendaWorkflowError,
    CalendarTomorrowAgendaRequest,
    CalendarTomorrowAgendaResult,
    CalendarTomorrowAgendaWorkflow,
)
from apps.server.src.integrations.calendar_agenda_command import (
    CalendarAgendaCommandRequest,
    CalendarAgendaCommandService,
)
from apps.server.src.integrations.google_provider import GoogleCredentials
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerExecutionFailureCategory,
)
from keyring.backends import macOS


class OfflineKeyring(macOS.Keyring):
    """Avoid evaluating the platform-dependent priority descriptor in mock specs."""

    priority = 1


ACCOUNT = WorkerAccountContext(principal="principal", account_identifier="account")
NOW = datetime(2026, 3, 28, 23, tzinfo=ZoneInfo("Europe/Tirane"))
RESULT = CalendarTomorrowAgendaResult(
    intent="tomorrow", timezone="Europe/Tirane", local_date="2026-03-29",
    time_min="2026-03-29T00:00:00+01:00",
    time_max="2026-03-30T00:00:00+02:00", events=(), event_count=0,
    aggregate_complete=True, skipped_event_count=0, termination_reason="exhausted",
)


class RecordingWorkflow:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[CalendarTomorrowAgendaRequest] = []
        self.error = error

    def execute(
        self, request: CalendarTomorrowAgendaRequest,
    ) -> CalendarTomorrowAgendaResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return RESULT


def command(intent: str = "tomorrow") -> CalendarAgendaCommandRequest:
    return CalendarAgendaCommandRequest(
        intent=intent, account_context=ACCOUNT, timezone="Europe/Tirane",
    )


def service(
    workflow: RecordingWorkflow, clock: Callable[[], datetime] = lambda: NOW,
) -> CalendarAgendaCommandService:
    return CalendarAgendaCommandService(
        cast(CalendarTomorrowAgendaWorkflow, workflow), clock,
    )


def test_command_delegates_once_preserving_inputs_and_result_identity() -> None:
    workflow = RecordingWorkflow()
    clock_calls: list[bool] = []

    def clock() -> datetime:
        clock_calls.append(True)
        return NOW

    request = command()
    result = service(workflow, clock).execute(request)

    assert result is RESULT
    assert clock_calls == [True]
    assert len(workflow.calls) == 1
    delegated = workflow.calls[0]
    assert delegated.intent == request.intent
    assert delegated.account_context is request.account_context
    assert delegated.timezone == request.timezone
    assert delegated.now is NOW


@pytest.mark.parametrize("intent", ["today", "this week", "Tomorrow", " tomorrow", ""])
def test_unsupported_intent_fails_before_clock_and_workflow(intent: str) -> None:
    workflow = RecordingWorkflow()

    def clock() -> datetime:
        pytest.fail("unsupported intent must not read the clock")

    with pytest.raises(CalendarAgendaWorkflowError) as caught:
        service(workflow, clock).execute(command(intent))
    assert caught.value.field == "intent"
    assert workflow.calls == []


@pytest.mark.parametrize("value", [datetime(2026, 1, 1), None, "secret-value", 42])
def test_invalid_clock_fails_before_workflow(value: object) -> None:
    workflow = RecordingWorkflow()
    with pytest.raises(CalendarAgendaWorkflowError) as caught:
        service(workflow, lambda: cast(datetime, value)).execute(command())
    assert caught.value.field == "now"
    assert str(caught.value) == (
        "calendar agenda clock must return a timezone-aware datetime"
    )
    assert workflow.calls == []


@pytest.mark.parametrize("error", [
    CalendarAgendaWorkflowError("invalid account", field="account_context"),
    CalendarAgendaWorkflowError("invalid timezone", field="timezone"),
    CalendarAgendaWorkflowError(
        "orchestration failed", category=WorkerExecutionFailureCategory.TRANSIENT,
    ),
    RuntimeError("workflow failure"),
])
def test_workflow_failures_propagate_unchanged(error: Exception) -> None:
    workflow = RecordingWorkflow(error)
    with pytest.raises(type(error)) as caught:
        service(workflow).execute(command())
    assert caught.value is error
    assert len(workflow.calls) == 1


def test_container_composes_existing_workflow_with_aware_utc_wall_clock() -> None:
    container = ApplicationContainer()
    composed = container.calendar_agenda_command_service
    assert isinstance(composed, CalendarAgendaCommandService)
    assert composed._workflow is container.calendar_tomorrow_agenda_workflow
    before = datetime.now(UTC)
    now = composed._clock()
    after = datetime.now(UTC)
    assert now.tzinfo is UTC
    assert before <= now <= after


def test_command_over_existing_workflow_preserves_tomorrow_dst_bounds() -> None:
    container = ApplicationContainer()
    composed = CalendarAgendaCommandService(
        container.calendar_tomorrow_agenda_workflow, lambda: NOW,
    )
    request = CalendarAgendaCommandRequest(
        intent="tomorrow", account_context=container.CALENDAR_ACCOUNT_CONTEXT,
        timezone="Europe/Tirane",
    )
    expected = container.calendar_tomorrow_agenda_workflow.execute(
        CalendarTomorrowAgendaRequest(
            intent=request.intent, account_context=request.account_context,
            timezone=request.timezone, now=NOW,
        )
    )
    result = composed.execute(request)
    assert result == expected
    assert result.time_min == RESULT.time_min
    assert result.time_max == RESULT.time_max


@pytest.mark.parametrize("status", [200, 401, 403, 429, 500])
def test_live_composition_is_offline_and_closes_client(status: int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"items": [], "secret": "provider-secret"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    backend = Mock(spec=OfflineKeyring, priority=1)
    with (
        patch.object(composition.httpx, "Client", return_value=client),
        patch("keyring.get_keyring", return_value=backend),
        patch.object(composition.StoredGoogleCredentialsProvider, "get_credentials",
                     return_value=GoogleCredentials("test-token", "principal", "account"))
        as credentials,
        patch.object(composition, "CalendarProviderComposition",
                     wraps=composition.CalendarProviderComposition) as provider,
    ):
        with composition.live_calendar_agenda_command_service(clock=lambda: NOW) as live:
            wired = provider.call_args.kwargs
            assert isinstance(wired["credentials_provider"],
                              composition.StoredGoogleCredentialsProvider)
            assert isinstance(wired["credentials_provider"]._credential_store,
                              composition.MacOSKeychainCredentialStore)
            assert isinstance(wired["transport_client"], composition.HttpxCalendarTransportClient)
            assert not client.is_closed
            if status == 200:
                assert live.execute(command()) == RESULT
            else:
                with pytest.raises(CalendarAgendaWorkflowError) as caught:
                    live.execute(command())
                assert "provider-secret" not in str(caught.value)
                assert caught.value.category is not None
            credentials.assert_called_once_with(principal="principal", account="account")
            assert len(requests) == 1
            assert requests[0].method == "GET"
            assert requests[0].url.path.endswith("/calendars/primary/events")
        assert client.is_closed


@pytest.mark.parametrize("construction_failure", [True, False])
def test_live_composition_closes_on_construction_or_caller_failure(
    construction_failure: bool,
) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    with (
        patch.object(composition.httpx, "Client", return_value=client),
        patch.object(composition, "MacOSKeychainCredentialStore",
                     side_effect=RuntimeError("failure") if construction_failure else None),
        pytest.raises(RuntimeError, match="failure"),
        composition.live_calendar_agenda_command_service(),
    ):
        raise RuntimeError("failure")
    assert client.is_closed


def test_live_missing_credentials_fails_before_transport() -> None:
    transport = Mock(side_effect=AssertionError("HTTP must not run"))
    client = httpx.Client(transport=httpx.MockTransport(transport))
    backend = Mock(spec=OfflineKeyring, priority=1)
    backend.get_password.return_value = None
    with (
        patch.object(composition.httpx, "Client", return_value=client),
        patch("keyring.get_keyring", return_value=backend),
        composition.live_calendar_agenda_command_service(clock=lambda: NOW) as live,
        pytest.raises(CalendarAgendaWorkflowError) as caught,
    ):
        live.execute(command())
    assert caught.value.category == WorkerExecutionFailureCategory.PERMANENT
    assert client.is_closed
    transport.assert_not_called()
    assert backend.get_password.call_args.args[1] == "account"
