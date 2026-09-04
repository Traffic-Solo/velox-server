"""Bounded read-only Google Calendar events.list contract and transport.

Every test here is deterministic and offline: HTTP is served by an in-process
httpx mock transport, and no credential material or real identifier is used.
"""

import httpx
import pytest
from apps.server.src.core.actions import Action
from apps.server.src.integrations.calendar import (
    CALENDAR_EXECUTOR_ROLE,
    CALENDAR_LIST_EVENTS_CAPABILITY,
    CALENDAR_LIST_MAX_MAX_RESULTS,
    CALENDAR_LIST_MIN_MAX_RESULTS,
    CALENDAR_WORKER_CAPABILITIES,
    CalendarCredentials,
    CalendarEvent,
    CalendarEventListRequest,
    CalendarEventListRequestError,
    CalendarProviderComposition,
    CalendarProviderRequest,
    CalendarProviderResponse,
    CalendarWorkerExecutor,
    FakeCalendarCredentialsProvider,
    FakeCalendarTransportClient,
    HttpxCalendarTransportClient,
)
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerExecutionFailureCategory,
    WorkerExecutionStatus,
)

LIST_CAPABILITY = CALENDAR_LIST_EVENTS_CAPABILITY.identifier
LIST_PATH = "/calendar/v3/calendars/primary/events"
TIME_MIN = "2026-09-04T00:00:00Z"
TIME_MAX = "2026-09-05T00:00:00Z"
MAX_RESULTS = 10
ALLOWLISTED_EVENT_FIELDS = frozenset({"event_id", "title", "start", "end", "attendees"})
ACCOUNT_CONTEXT = WorkerAccountContext(
    principal="principal-1",
    account_identifier="calendar-account-1",
)
LIST_ACCESS_TOKEN = "calendar-list-access-token-secret"

TIMED_EVENT = {
    "id": "calendar-event-1",
    "summary": "Timed planning",
    "start": {"dateTime": "2026-09-04T09:00:00Z"},
    "end": {"dateTime": "2026-09-04T09:30:00Z"},
    "attendees": [{"email": "owner@example.com"}, {"email": "team@example.com"}],
}
ALL_DAY_EVENT = {
    "id": "calendar-event-2",
    "summary": "All day offsite",
    "start": {"date": "2026-09-04"},
    "end": {"date": "2026-09-05"},
}
MAPPED_TIMED_EVENT = {
    "event_id": "calendar-event-1",
    "title": "Timed planning",
    "start": "2026-09-04T09:00:00Z",
    "end": "2026-09-04T09:30:00Z",
    "attendees": ("owner@example.com", "team@example.com"),
}
MAPPED_ALL_DAY_EVENT = {
    "event_id": "calendar-event-2",
    "title": "All day offsite",
    "start": "2026-09-04",
    "end": "2026-09-05",
    "attendees": (),
}


def list_credentials() -> CalendarCredentials:
    return CalendarCredentials(
        access_token=LIST_ACCESS_TOKEN,
        principal="principal-1",
        account="calendar-account-1",
    )


def list_provider_request(
    *,
    time_min: str = TIME_MIN,
    time_max: str = TIME_MAX,
    max_results: int = MAX_RESULTS,
) -> CalendarProviderRequest:
    return CalendarProviderRequest(
        operation=LIST_CAPABILITY,
        path=LIST_PATH,
        query=CalendarEventListRequest(
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
        ).as_query(),
    )


def list_action(
    *,
    time_min: object = TIME_MIN,
    time_max: object = TIME_MAX,
    max_results: object = MAX_RESULTS,
) -> Action:
    return Action(
        type=LIST_CAPABILITY,
        target="internal-velox-event-id",
        payload={
            "time_min": time_min,
            "time_max": time_max,
            "max_results": max_results,
        },
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )


def list_http_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def google_page(
    items: list[object],
    *,
    next_page_token: str | None = None,
) -> dict[str, object]:
    """Build a Google events.list page body, including fields VELOX must drop."""
    page: dict[str, object] = {
        "kind": "calendar#events",
        "etag": "raw-etag-must-not-leak",
        "updated": "2026-09-03T00:00:00.000Z",
        "items": items,
    }
    if next_page_token is not None:
        page["nextPageToken"] = next_page_token
    return page


def executor_over(handler) -> tuple[CalendarWorkerExecutor, list[httpx.Request]]:
    """Compose the executor over the real transport with in-process HTTP."""
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    executor = CalendarWorkerExecutor(
        provider_composition=CalendarProviderComposition(
            transport_client=HttpxCalendarTransportClient(
                list_http_client(recording_handler)
            ),
        )
    )
    return executor, requests


def execute_list(
    executor: CalendarWorkerExecutor,
    *,
    account_context: WorkerAccountContext | None = ACCOUNT_CONTEXT,
    action: Action | None = None,
):
    return executor.execute(
        action if action is not None else list_action(),
        capability=LIST_CAPABILITY,
        account_context=account_context,
    )


# --- capability registration -------------------------------------------------


def test_list_capability_is_registered_on_the_existing_calendar_role() -> None:
    assert CALENDAR_LIST_EVENTS_CAPABILITY in CALENDAR_WORKER_CAPABILITIES
    assert CALENDAR_LIST_EVENTS_CAPABILITY.role == CALENDAR_EXECUTOR_ROLE
    assert CALENDAR_LIST_EVENTS_CAPABILITY.provider == "calendar"


# --- bounded request contract ------------------------------------------------


def test_valid_bounded_window_produces_the_exact_google_query() -> None:
    request = CalendarEventListRequest(
        time_min=TIME_MIN,
        time_max=TIME_MAX,
        max_results=MAX_RESULTS,
    )

    assert request.as_query() == {
        "timeMin": TIME_MIN,
        "timeMax": TIME_MAX,
        "maxResults": "10",
        "singleEvents": "true",
        "orderBy": "startTime",
    }


@pytest.mark.parametrize(
    "timestamp",
    [
        "",
        "   ",
        " 2026-09-04T00:00:00Z",
        "2026-09-04T00:00:00Z ",
        "\t2026-09-04T00:00:00Z\n",
        "2026-09-04",
        "2026-09-04T00:00Z",
        "2026-09-04T00:00:00",
        "2026-09-04 00:00:00Z",
        "2026-09-04t00:00:00z",
        "2026-13-04T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "not-a-timestamp",
    ],
)
def test_blank_padded_or_malformed_timestamps_are_rejected(timestamp: str) -> None:
    with pytest.raises(CalendarEventListRequestError) as time_min_error:
        CalendarEventListRequest(
            time_min=timestamp,
            time_max=TIME_MAX,
            max_results=MAX_RESULTS,
        )
    assert time_min_error.value.field == "time_min"

    with pytest.raises(CalendarEventListRequestError) as time_max_error:
        CalendarEventListRequest(
            time_min=TIME_MIN,
            time_max=timestamp,
            max_results=MAX_RESULTS,
        )
    assert time_max_error.value.field == "time_max"


@pytest.mark.parametrize("timestamp", [None, 20260904, ["2026-09-04T00:00:00Z"]])
def test_non_string_timestamps_are_rejected(timestamp: object) -> None:
    with pytest.raises(CalendarEventListRequestError):
        CalendarEventListRequest(
            time_min=timestamp,  # type: ignore[arg-type]
            time_max=TIME_MAX,
            max_results=MAX_RESULTS,
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-04T00:00:00Z",
        "2026-09-04T00:00:00.123Z",
        "2026-09-04T00:00:00+02:00",
        "2026-09-04T00:00:00-05:30",
    ],
)
def test_canonical_rfc3339_offsets_are_accepted(timestamp: str) -> None:
    request = CalendarEventListRequest(
        time_min=timestamp,
        time_max="2027-01-01T00:00:00Z",
        max_results=MAX_RESULTS,
    )

    assert request.time_min == timestamp


@pytest.mark.parametrize(
    ("time_min", "time_max"),
    [
        (TIME_MIN, TIME_MIN),
        (TIME_MAX, TIME_MIN),
        # Same instant expressed in two offsets is still not a positive window.
        ("2026-09-04T02:00:00+02:00", "2026-09-04T00:00:00Z"),
    ],
)
def test_window_must_be_strictly_ordered(time_min: str, time_max: str) -> None:
    with pytest.raises(CalendarEventListRequestError) as error:
        CalendarEventListRequest(
            time_min=time_min,
            time_max=time_max,
            max_results=MAX_RESULTS,
        )

    assert error.value.field == "time_min"


@pytest.mark.parametrize(
    "max_results",
    [CALENDAR_LIST_MIN_MAX_RESULTS, CALENDAR_LIST_MAX_MAX_RESULTS],
)
def test_minimum_and_maximum_max_results_are_accepted(max_results: int) -> None:
    request = CalendarEventListRequest(
        time_min=TIME_MIN,
        time_max=TIME_MAX,
        max_results=max_results,
    )

    assert request.as_query()["maxResults"] == str(max_results)


@pytest.mark.parametrize(
    "max_results",
    [
        CALENDAR_LIST_MIN_MAX_RESULTS - 1,
        CALENDAR_LIST_MAX_MAX_RESULTS + 1,
        -1,
        2500,
    ],
)
def test_out_of_range_max_results_is_rejected_rather_than_clamped(
    max_results: int,
) -> None:
    with pytest.raises(CalendarEventListRequestError) as error:
        CalendarEventListRequest(
            time_min=TIME_MIN,
            time_max=TIME_MAX,
            max_results=max_results,
        )

    assert error.value.field == "max_results"


@pytest.mark.parametrize("max_results", [None, True, False, "10", 10.0])
def test_non_integer_max_results_is_rejected(max_results: object) -> None:
    with pytest.raises(CalendarEventListRequestError) as error:
        CalendarEventListRequest(
            time_min=TIME_MIN,
            time_max=TIME_MAX,
            max_results=max_results,  # type: ignore[arg-type]
        )

    assert error.value.field == "max_results"


# --- executor input validation ----------------------------------------------


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"time_min": " 2026-09-04T00:00:00Z"}, "time_min"),
        ({"time_max": "2026-09-05"}, "time_max"),
        ({"time_min": TIME_MAX, "time_max": TIME_MIN}, "time_min"),
        ({"max_results": 0}, "max_results"),
        ({"max_results": CALENDAR_LIST_MAX_MAX_RESULTS + 1}, "max_results"),
        ({"max_results": None}, "max_results"),
    ],
)
def test_executor_rejects_invalid_list_input_without_contacting_the_provider(
    payload: dict[str, object],
    field: str,
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider transport invoked for an invalid list request")

    executor, requests = executor_over(unreachable)

    result = execute_list(executor, action=list_action(**payload))  # type: ignore[arg-type]

    assert result.status == WorkerExecutionStatus.FAILED
    assert result.failure is not None
    assert result.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert result.failure.metadata["field"] == field
    assert result.metadata["external_execution_performed"] is False
    assert requests == []


# --- exact request semantics -------------------------------------------------


def test_executor_issues_the_exact_bounded_primary_calendar_request() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(200, json=google_page([TIMED_EVENT]))
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == LIST_PATH
    assert dict(requests[0].url.params) == {
        "timeMin": TIME_MIN,
        "timeMax": TIME_MAX,
        "maxResults": "10",
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    assert requests[0].content == b""


def test_list_request_url_is_exact() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=google_page([]))

    HttpxCalendarTransportClient(list_http_client(handler)).execute(
        list_provider_request(),
        list_credentials(),
    )

    assert str(requests[0].url) == (
        "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        "?timeMin=2026-09-04T00%3A00%3A00Z&timeMax=2026-09-05T00%3A00%3A00Z"
        "&maxResults=10&singleEvents=true&orderBy=startTime"
    )
    assert LIST_ACCESS_TOKEN not in str(requests[0].url)
    assert requests[0].headers["Authorization"] == f"Bearer {LIST_ACCESS_TOKEN}"


def test_transport_rejects_an_unbounded_list_query_before_any_http_call() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unbounded list query reached Google")

    unbounded = CalendarProviderRequest(
        operation=LIST_CAPABILITY,
        path=LIST_PATH,
        query={
            "timeMin": TIME_MIN,
            "timeMax": TIME_MAX,
            "maxResults": "2500",
            "singleEvents": "true",
            "orderBy": "startTime",
        },
    )

    response = HttpxCalendarTransportClient(list_http_client(unreachable)).execute(
        unbounded,
        list_credentials(),
    )

    assert response.failure is not None
    assert response.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert response.failure.provider_reason == "invalidRequest"
    assert response.body["external_execution_performed"] is False


@pytest.mark.parametrize(
    "query_override",
    [
        {"singleEvents": "false"},
        {"orderBy": "updated"},
        # pageToken is accepted from Slice 2 on; these remain outside the contract.
        {"q": "search-term"},
        {"syncToken": "opaque-sync-token"},
        {"showDeleted": "true"},
    ],
)
def test_transport_rejects_a_list_query_outside_the_velox_contract(
    query_override: dict[str, str],
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("off-contract list query reached Google")

    query = CalendarEventListRequest(
        time_min=TIME_MIN,
        time_max=TIME_MAX,
        max_results=MAX_RESULTS,
    ).as_query()
    query.update(query_override)

    response = HttpxCalendarTransportClient(list_http_client(unreachable)).execute(
        CalendarProviderRequest(
            operation=LIST_CAPABILITY,
            path=LIST_PATH,
            query=query,
        ),
        list_credentials(),
    )

    assert response.failure is not None
    assert response.failure.provider_reason == "invalidRequest"


# --- explicit principal / account routing ------------------------------------


def test_list_request_routes_the_explicit_principal_and_account() -> None:
    resolved: list[CalendarCredentials] = []
    seen_requests: list[CalendarProviderRequest] = []

    class RecordingTransport:
        def execute(
            self,
            request: CalendarProviderRequest,
            credentials: CalendarCredentials,
        ) -> CalendarProviderResponse:
            seen_requests.append(request)
            resolved.append(credentials)
            return CalendarProviderResponse(
                status_code=200,
                body={
                    "external_execution_performed": False,
                    "integration": "calendar",
                    "adapter": "fake_transport",
                    "events": (),
                    "skipped_event_count": 0,
                    "next_page_token": None,
                },
            )

    executor = CalendarWorkerExecutor(
        provider_composition=CalendarProviderComposition(
            credentials_provider=FakeCalendarCredentialsProvider(),
            transport_client=RecordingTransport(),
        )
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert resolved[0].principal == "principal-1"
    assert resolved[0].account == "calendar-account-1"
    assert seen_requests[0].account_context == ACCOUNT_CONTEXT
    assert result.metadata["account_context_used"] == ACCOUNT_CONTEXT.as_metadata()


def test_list_request_without_account_context_fails_closed() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unrouted list request reached Google")

    executor, requests = executor_over(unreachable)

    result = execute_list(executor, account_context=None)

    assert result.status == WorkerExecutionStatus.FAILED
    assert result.metadata["external_execution_performed"] is False
    assert requests == []


# --- event mapping -----------------------------------------------------------


@pytest.mark.parametrize(
    ("items", "expected_events"),
    [
        ([], ()),
        ([TIMED_EVENT], (MAPPED_TIMED_EVENT,)),
        (
            [TIMED_EVENT, ALL_DAY_EVENT],
            (MAPPED_TIMED_EVENT, MAPPED_ALL_DAY_EVENT),
        ),
    ],
)
def test_list_maps_pages_through_the_events_get_mapping(
    items: list[dict[str, object]],
    expected_events: tuple[dict[str, object], ...],
) -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(200, json=google_page(items))
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["events"] == expected_events
    assert result.metadata["event_count"] == len(expected_events)
    assert result.metadata["skipped_event_count"] == 0
    assert result.metadata["page_complete"] is True


def test_list_preserves_valid_siblings_when_one_item_is_malformed() -> None:
    malformed_event = {**TIMED_EVENT, "attendees": "not-a-list"}
    executor, _ = executor_over(
        lambda request: httpx.Response(
            200,
            json=google_page([TIMED_EVENT, malformed_event, ALL_DAY_EVENT]),
        )
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["events"] == (MAPPED_TIMED_EVENT, MAPPED_ALL_DAY_EVENT)
    assert result.metadata["event_count"] == 2
    assert result.metadata["skipped_event_count"] == 1
    assert result.metadata["page_complete"] is False


def test_list_skips_multiple_malformed_siblings() -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(
            200,
            json=google_page(
                [TIMED_EVENT, "not-an-object", {"summary": "missing-id"}]
            ),
        )
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["events"] == (MAPPED_TIMED_EVENT,)
    assert result.metadata["event_count"] == 1
    assert result.metadata["skipped_event_count"] == 2
    assert result.metadata["page_complete"] is False


def test_list_all_malformed_items_is_a_successful_partial_page() -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(
            200,
            json=google_page(["not-an-object", {"summary": "missing-id"}]),
        )
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["events"] == ()
    assert result.metadata["event_count"] == 0
    assert result.metadata["skipped_event_count"] == 2
    assert result.metadata["page_complete"] is False


def test_partial_page_preserves_next_page_token_without_following_it() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(
            200,
            json=google_page(
                [TIMED_EVENT, "not-an-object"],
                next_page_token="opaque-next-page-token",
            ),
        )
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["events"] == (MAPPED_TIMED_EVENT,)
    assert result.metadata["next_page_token"] == "opaque-next-page-token"
    assert result.metadata["has_more_pages"] is True
    assert result.metadata["page_complete"] is False
    assert result.metadata["skipped_event_count"] == 1
    assert len(requests) == 1


def test_malformed_item_values_do_not_cross_the_calendar_boundary() -> None:
    sentinel = "malformed-calendar-sentinel-must-not-leak"
    malformed_event = {
        "id": sentinel,
        "summary": sentinel,
        "start": {"dateTime": "2026-09-04T10:00:00Z"},
        "end": {"dateTime": "2026-09-04T10:30:00Z"},
        "attendees": sentinel,
    }
    executor, _ = executor_over(
        lambda request: httpx.Response(
            200,
            json=google_page([TIMED_EVENT, malformed_event]),
        )
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["skipped_event_count"] == 1
    exposed = (
        repr(result),
        repr(result.action),
        repr(result.metadata),
        repr(result.failure),
        str(result.reason),
    )
    assert all(sentinel not in value for value in exposed)


def test_list_preserves_all_day_and_timed_event_boundaries() -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(
            200, json=google_page([TIMED_EVENT, ALL_DAY_EVENT])
        )
    )

    events = execute_list(executor).metadata["events"]

    assert events[0]["start"] == "2026-09-04T09:00:00Z"
    assert events[0]["end"] == "2026-09-04T09:30:00Z"
    assert events[1]["start"] == "2026-09-04"
    assert events[1]["end"] == "2026-09-05"


def test_list_defaults_missing_summary_and_attendees() -> None:
    minimal_event = {
        "id": "calendar-event-3",
        "start": {"dateTime": "2026-09-04T11:00:00Z"},
        "end": {"dateTime": "2026-09-04T11:15:00Z"},
    }
    executor, _ = executor_over(
        lambda request: httpx.Response(200, json=google_page([minimal_event]))
    )

    events = execute_list(executor).metadata["events"]

    assert events == (
        {
            "event_id": "calendar-event-3",
            "title": "",
            "start": "2026-09-04T11:00:00Z",
            "end": "2026-09-04T11:15:00Z",
            "attendees": (),
        },
    )


def test_only_the_five_allowlisted_fields_cross_the_provider_boundary() -> None:
    noisy_event = {
        **TIMED_EVENT,
        "hangoutLink": "raw-link-must-not-leak",
        "creator": {"email": "creator-must-not-leak@example.com"},
        "organizer": {"email": "organizer-must-not-leak@example.com"},
        "iCalUID": "raw-uid-must-not-leak",
        "status": "confirmed",
    }
    executor, _ = executor_over(
        lambda request: httpx.Response(200, json=google_page([noisy_event]))
    )

    result = execute_list(executor)

    events = result.metadata["events"]
    assert set(events[0]) == ALLOWLISTED_EVENT_FIELDS
    blob = repr(result) + repr(result.metadata)
    assert "must-not-leak" not in blob
    assert "raw-etag" not in blob


# --- pagination boundary -----------------------------------------------------


def test_next_page_token_is_preserved_as_opaque_metadata_and_never_followed() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(
            200,
            json=google_page([TIMED_EVENT], next_page_token="opaque-next-page-token"),
        )
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["next_page_token"] == "opaque-next-page-token"
    assert result.metadata["has_more_pages"] is True
    assert result.metadata["events"] == (MAPPED_TIMED_EVENT,)
    # One page only: a second page is never requested, and no pageToken is sent.
    assert len(requests) == 1
    assert "pageToken" not in dict(requests[0].url.params)


def test_absent_next_page_token_reports_no_further_pages() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(200, json=google_page([TIMED_EVENT]))
    )

    result = execute_list(executor)

    assert result.metadata["next_page_token"] is None
    assert result.metadata["has_more_pages"] is False
    assert len(requests) == 1


@pytest.mark.parametrize("next_page_token", ["", "   ", 42, []])
def test_malformed_next_page_token_fails_closed(next_page_token: object) -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(
            200,
            json={"items": [TIMED_EVENT], "nextPageToken": next_page_token},
        )
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.FAILED


# --- malformed provider responses -------------------------------------------


@pytest.mark.parametrize(
    "provider_response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"items": "not-a-list"}),
    ],
)
def test_structurally_invalid_google_list_response_is_an_internal_failure(
    provider_response: httpx.Response,
) -> None:
    response = HttpxCalendarTransportClient(
        list_http_client(lambda request: provider_response)
    ).execute(list_provider_request(), list_credentials())

    assert response.failure is not None
    assert response.failure.category == WorkerExecutionFailureCategory.INTERNAL
    assert response.failure.provider_reason == "invalidProviderResponse"
    assert "events" not in response.body


# --- failure mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "body", "category", "retryable", "reason"),
    [
        (
            400,
            {"error": "raw-secret"},
            WorkerExecutionFailureCategory.PERMANENT,
            False,
            "invalidRequest",
        ),
        (
            401,
            {"error": "raw-secret"},
            WorkerExecutionFailureCategory.PERMANENT,
            False,
            "reconnectRequired",
        ),
        (
            403,
            {"error": {"errors": [{"reason": "forbidden"}]}},
            WorkerExecutionFailureCategory.PERMANENT,
            False,
            "forbidden",
        ),
        (
            403,
            {"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
            WorkerExecutionFailureCategory.TRANSIENT,
            True,
            "rateLimitExceeded",
        ),
        (
            404,
            {"error": "raw-secret"},
            WorkerExecutionFailureCategory.PERMANENT,
            False,
            "providerRequestFailed",
        ),
        (
            429,
            {"error": "raw-secret"},
            WorkerExecutionFailureCategory.TRANSIENT,
            True,
            "rateLimitExceeded",
        ),
        (
            503,
            {"error": "raw-secret"},
            WorkerExecutionFailureCategory.TRANSIENT,
            True,
            "providerUnavailable",
        ),
    ],
)
def test_list_reuses_the_existing_google_failure_classification(
    status_code: int,
    body: dict[str, object],
    category: WorkerExecutionFailureCategory,
    retryable: bool,
    reason: str,
) -> None:
    response = HttpxCalendarTransportClient(
        list_http_client(lambda request: httpx.Response(status_code, json=body))
    ).execute(list_provider_request(), list_credentials())

    assert response.failure is not None
    assert response.failure.category == category
    assert response.failure.retryable is retryable
    assert response.failure.provider_reason == reason
    assert "raw-secret" not in repr(response)
    assert "events" not in response.body


@pytest.mark.parametrize(
    "transport_error",
    [httpx.ConnectError("network-secret"), httpx.ReadTimeout("timeout-secret")],
)
def test_list_maps_network_failures_as_transient(
    transport_error: Exception,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise transport_error

    response = HttpxCalendarTransportClient(list_http_client(handler)).execute(
        list_provider_request(),
        list_credentials(),
    )

    assert response.failure is not None
    assert response.failure.category == WorkerExecutionFailureCategory.TRANSIENT
    assert response.failure.retryable is True
    assert "network-secret" not in repr(response)
    assert "timeout-secret" not in repr(response)


def test_executor_preserves_the_provider_failure_classification() -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(429, json={"error": "raw-secret"})
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.FAILED
    assert result.failure is not None
    assert result.failure.category == WorkerExecutionFailureCategory.TRANSIENT
    assert result.failure.metadata["retryable"] is True
    assert result.failure.metadata["provider_status_code"] == 429
    assert "raw-secret" not in repr(result)
    assert "events" not in result.metadata


# --- credential and token safety ---------------------------------------------


def test_list_result_never_exposes_credential_material() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(
            200,
            json=google_page([TIMED_EVENT], next_page_token="opaque-next-page-token"),
        )
    )

    result = execute_list(executor)

    blob = repr(result) + repr(result.metadata)
    assert "fake-calendar-access-token" not in blob
    assert "Authorization" not in blob
    assert "Bearer" not in blob
    assert "access_token" not in blob
    assert requests[0].headers["Authorization"].startswith("Bearer ")


def test_list_transport_rejects_blank_credentials_before_any_http_call() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("blank credentials reached Google")

    response = HttpxCalendarTransportClient(list_http_client(unreachable)).execute(
        list_provider_request(),
        CalendarCredentials(access_token="  ", principal="p", account="a"),
    )

    assert response.failure is not None
    assert response.failure.provider_reason == "reconnectRequired"
    assert response.body["external_execution_performed"] is False


# --- fake transport ----------------------------------------------------------


def test_fake_transport_serves_a_deterministic_bounded_page() -> None:
    executor = CalendarWorkerExecutor()

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["adapter"] == "fake_transport"
    assert result.metadata["external_execution_performed"] is False
    assert result.metadata["next_page_token"] is None
    assert result.metadata["has_more_pages"] is False
    assert result.metadata["page_complete"] is True
    assert result.metadata["skipped_event_count"] == 0
    assert set(result.metadata["events"][0]) == ALLOWLISTED_EVENT_FIELDS


def test_fake_transport_honours_the_requested_page_bound() -> None:
    events = {
        f"calendar-event-{index}": CalendarEvent(
            event_id=f"calendar-event-{index}",
            title=f"Event {index}",
            start="2026-09-04T09:00:00Z",
            end="2026-09-04T09:30:00Z",
        )
        for index in range(1, 5)
    }
    executor = CalendarWorkerExecutor(
        provider_composition=CalendarProviderComposition(
            transport_client=FakeCalendarTransportClient(events=events),
        )
    )

    result = execute_list(
        executor,
        action=list_action(max_results=2),
    )

    assert result.metadata["event_count"] == 2


def test_fake_transport_empty_event_store_returns_an_empty_page() -> None:
    executor = CalendarWorkerExecutor(
        provider_composition=CalendarProviderComposition(
            transport_client=FakeCalendarTransportClient(events={}),
        )
    )

    result = execute_list(executor)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["events"] == ()
    assert result.metadata["event_count"] == 0
    assert result.metadata["page_complete"] is True
    assert result.metadata["skipped_event_count"] == 0
