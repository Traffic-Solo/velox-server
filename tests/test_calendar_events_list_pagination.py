"""Caller-driven single-page pagination for the bounded Calendar events.list read.

One VELOX execution is always exactly one Google request. These tests prove the
token is forwarded verbatim, the bounded query is identical across pages, pages
are never aggregated or followed automatically, and no token reaches metadata or
a failure message.

Every test is deterministic and offline; HTTP is served by an httpx mock.
"""

import httpx
import pytest
from apps.server.src.core.actions import Action
from apps.server.src.integrations.calendar import (
    CALENDAR_EXECUTOR_ROLE,
    CALENDAR_LIST_EVENTS_CAPABILITY,
    CalendarCredentials,
    CalendarEvent,
    CalendarEventListRequest,
    CalendarEventListRequestError,
    CalendarProviderComposition,
    CalendarProviderRequest,
    CalendarWorkerExecutor,
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
MAX_RESULTS = 2
ALLOWLISTED_EVENT_FIELDS = frozenset({"event_id", "title", "start", "end", "attendees"})
ACCOUNT_CONTEXT = WorkerAccountContext(
    principal="principal-1",
    account_identifier="calendar-account-1",
)
PAGE_ACCESS_TOKEN = "calendar-page-access-token-secret"
# A token whose characters would be mangled by any normalization or re-encoding.
OPAQUE_TOKEN = "CiAKGjBpNDd2Zmxxb/+aG9j2dGVzdBIB=-_~ EXAMPLE".replace(" ", "")
SECOND_OPAQUE_TOKEN = "EhAKDjBpNDd2Zmxxb2Rlc3Q=="

BASE_QUERY = {
    "timeMin": TIME_MIN,
    "timeMax": TIME_MAX,
    "maxResults": str(MAX_RESULTS),
    "singleEvents": "true",
    "orderBy": "startTime",
}


def event_payload(index: int) -> dict[str, object]:
    return {
        "id": f"calendar-event-{index}",
        "summary": f"Event {index}",
        "start": {"dateTime": f"2026-09-04T0{index}:00:00Z"},
        "end": {"dateTime": f"2026-09-04T0{index}:30:00Z"},
    }


def mapped_event(index: int) -> dict[str, object]:
    return {
        "event_id": f"calendar-event-{index}",
        "title": f"Event {index}",
        "start": f"2026-09-04T0{index}:00:00Z",
        "end": f"2026-09-04T0{index}:30:00Z",
        "attendees": (),
    }


def page_credentials() -> CalendarCredentials:
    return CalendarCredentials(
        access_token=PAGE_ACCESS_TOKEN,
        principal="principal-1",
        account="calendar-account-1",
    )


def list_action(
    *,
    page_token: object = None,
    max_results: object = MAX_RESULTS,
) -> Action:
    payload: dict[str, object] = {
        "time_min": TIME_MIN,
        "time_max": TIME_MAX,
        "max_results": max_results,
    }
    if page_token is not None:
        payload["page_token"] = page_token
    return Action(
        type=LIST_CAPABILITY,
        target="internal-velox-event-id",
        payload=payload,
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )


def executor_over(handler) -> tuple[CalendarWorkerExecutor, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    executor = CalendarWorkerExecutor(
        provider_composition=CalendarProviderComposition(
            transport_client=HttpxCalendarTransportClient(
                httpx.Client(transport=httpx.MockTransport(recording_handler))
            ),
        )
    )
    return executor, requests


def execute(executor: CalendarWorkerExecutor, action: Action):
    return executor.execute(
        action,
        capability=LIST_CAPABILITY,
        account_context=ACCOUNT_CONTEXT,
    )


# --- request contract --------------------------------------------------------


def test_absent_page_token_means_first_page() -> None:
    request = CalendarEventListRequest(
        time_min=TIME_MIN,
        time_max=TIME_MAX,
        max_results=MAX_RESULTS,
    )

    assert request.page_token is None
    assert "pageToken" not in request.as_query()
    assert request.as_query() == BASE_QUERY


def test_present_page_token_is_forwarded_verbatim_as_google_page_token() -> None:
    request = CalendarEventListRequest(
        time_min=TIME_MIN,
        time_max=TIME_MAX,
        max_results=MAX_RESULTS,
        page_token=OPAQUE_TOKEN,
    )

    assert request.as_query() == {**BASE_QUERY, "pageToken": OPAQUE_TOKEN}
    # Neither parsed nor normalized: the token round-trips byte for byte.
    assert request.as_query()["pageToken"] is OPAQUE_TOKEN


def test_continuation_preserves_every_other_bounded_query_parameter() -> None:
    first_page = CalendarEventListRequest(
        time_min=TIME_MIN,
        time_max=TIME_MAX,
        max_results=MAX_RESULTS,
    ).as_query()
    continuation = CalendarEventListRequest(
        time_min=TIME_MIN,
        time_max=TIME_MAX,
        max_results=MAX_RESULTS,
        page_token=OPAQUE_TOKEN,
    ).as_query()

    assert {
        key: value for key, value in continuation.items() if key != "pageToken"
    } == first_page


@pytest.mark.parametrize(
    "page_token",
    ["", " ", "   ", "\t", "\n", " token", "token ", "\ttoken", "token\n", " token "],
)
def test_blank_or_padded_page_token_is_rejected(page_token: str) -> None:
    with pytest.raises(CalendarEventListRequestError) as error:
        CalendarEventListRequest(
            time_min=TIME_MIN,
            time_max=TIME_MAX,
            max_results=MAX_RESULTS,
            page_token=page_token,
        )

    assert error.value.field == "page_token"


@pytest.mark.parametrize("page_token", [0, 1, True, 1.5, b"token", ["token"], {}])
def test_non_string_page_token_is_rejected(page_token: object) -> None:
    with pytest.raises(CalendarEventListRequestError) as error:
        CalendarEventListRequest(
            time_min=TIME_MIN,
            time_max=TIME_MAX,
            max_results=MAX_RESULTS,
            page_token=page_token,  # type: ignore[arg-type]
        )

    assert error.value.field == "page_token"


def test_rejected_page_token_never_appears_in_the_failure_message() -> None:
    secret_token = "opaque-token-that-must-not-leak "

    with pytest.raises(CalendarEventListRequestError) as error:
        CalendarEventListRequest(
            time_min=TIME_MIN,
            time_max=TIME_MAX,
            max_results=MAX_RESULTS,
            page_token=secret_token,
        )

    assert "must-not-leak" not in str(error.value)
    assert "must-not-leak" not in repr(error.value)


# --- executor: invalid token fails before the provider is reached -------------


@pytest.mark.parametrize("page_token", ["", "   ", " padded", "padded ", 7, b"bytes"])
def test_invalid_page_token_fails_before_provider_execution(
    page_token: object,
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid page token reached the provider")

    executor, requests = executor_over(unreachable)

    result = execute(executor, list_action(page_token=page_token))

    assert result.status == WorkerExecutionStatus.FAILED
    assert result.failure is not None
    assert result.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert result.failure.metadata["field"] == "page_token"
    assert result.metadata["external_execution_performed"] is False
    assert requests == []


# --- exact HTTP semantics ----------------------------------------------------


def test_first_page_request_sends_no_page_token() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(200, json={"items": [event_payload(1)]})
    )

    result = execute(executor, list_action())

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert len(requests) == 1
    assert dict(requests[0].url.params) == BASE_QUERY
    assert "pageToken" not in dict(requests[0].url.params)
    assert result.metadata["page_token_supplied"] is False


def test_continuation_request_sends_the_exact_token_and_identical_query() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(200, json={"items": [event_payload(3)]})
    )

    result = execute(executor, list_action(page_token=OPAQUE_TOKEN))

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == LIST_PATH
    assert dict(requests[0].url.params) == {**BASE_QUERY, "pageToken": OPAQUE_TOKEN}
    assert result.metadata["page_token_supplied"] is True


def test_both_pages_issue_one_get_each_with_the_same_window_and_ordering() -> None:
    pages = [
        httpx.Response(
            200,
            json={"items": [event_payload(1), event_payload(2)],
                  "nextPageToken": OPAQUE_TOKEN},
        ),
        httpx.Response(200, json={"items": [event_payload(3)]}),
    ]
    executor, requests = executor_over(lambda request: pages.pop(0))

    first = execute(executor, list_action())
    second = execute(executor, list_action(page_token=first.metadata["next_page_token"]))

    assert first.status == second.status == WorkerExecutionStatus.SUCCEEDED
    # Exactly one Google request per VELOX execution, never more.
    assert len(requests) == 2
    assert [request.method for request in requests] == ["GET", "GET"]
    assert [request.url.path for request in requests] == [LIST_PATH, LIST_PATH]
    first_params = dict(requests[0].url.params)
    second_params = dict(requests[1].url.params)
    assert first_params == BASE_QUERY
    assert second_params == {**BASE_QUERY, "pageToken": OPAQUE_TOKEN}
    # The bounded window and ordering are never renegotiated between pages.
    assert {k: v for k, v in second_params.items() if k != "pageToken"} == first_params


# --- result shape: current page only -----------------------------------------


def test_first_page_preserves_the_opaque_token_without_following_it() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(
            200,
            json={"items": [event_payload(1), event_payload(2)],
                  "nextPageToken": OPAQUE_TOKEN},
        )
    )

    result = execute(executor, list_action())

    assert result.metadata["next_page_token"] == OPAQUE_TOKEN
    assert result.metadata["has_more_pages"] is True
    assert result.metadata["events"] == (mapped_event(1), mapped_event(2))
    assert result.metadata["event_count"] == 2
    # One page fetched, and no second request was issued on its own.
    assert len(requests) == 1


def test_second_page_may_itself_return_another_next_page_token() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(
            200,
            json={"items": [event_payload(3)], "nextPageToken": SECOND_OPAQUE_TOKEN},
        )
    )

    result = execute(executor, list_action(page_token=OPAQUE_TOKEN))

    assert result.metadata["next_page_token"] == SECOND_OPAQUE_TOKEN
    assert result.metadata["has_more_pages"] is True
    assert result.metadata["events"] == (mapped_event(3),)
    assert len(requests) == 1


def test_final_page_reports_no_further_pages() -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(200, json={"items": [event_payload(3)]})
    )

    result = execute(executor, list_action(page_token=OPAQUE_TOKEN))

    assert result.metadata["next_page_token"] is None
    assert result.metadata["has_more_pages"] is False


def test_pages_are_never_aggregated() -> None:
    pages = [
        httpx.Response(
            200,
            json={"items": [event_payload(1), event_payload(2)],
                  "nextPageToken": OPAQUE_TOKEN},
        ),
        httpx.Response(200, json={"items": [event_payload(3)]}),
    ]
    executor, _ = executor_over(lambda request: pages.pop(0))

    first = execute(executor, list_action())
    second = execute(executor, list_action(page_token=OPAQUE_TOKEN))

    # Each result carries only its own page; nothing accumulates across calls.
    assert first.metadata["events"] == (mapped_event(1), mapped_event(2))
    assert second.metadata["events"] == (mapped_event(3),)
    assert first.metadata["event_count"] == 2
    assert second.metadata["event_count"] == 1
    assert len(first.metadata["events"]) <= MAX_RESULTS
    assert len(second.metadata["events"]) <= MAX_RESULTS
    for event in first.metadata["events"] + second.metadata["events"]:
        assert set(event) == ALLOWLISTED_EVENT_FIELDS


def test_current_page_is_mapped_exactly_once() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={"items": [event_payload(1)], "nextPageToken": OPAQUE_TOKEN},
        )

    executor, _ = executor_over(handler)

    result = execute(executor, list_action(page_token=OPAQUE_TOKEN))

    assert len(calls) == 1
    assert result.metadata["events"] == (mapped_event(1),)
    assert result.metadata["event_count"] == 1


# --- token confinement -------------------------------------------------------


def test_page_token_is_not_mirrored_into_result_metadata() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(200, json={"items": [event_payload(3)]})
    )

    result = execute(executor, list_action(page_token=OPAQUE_TOKEN))

    # The token belongs in the request only; metadata records presence, not value.
    assert result.metadata["provider_request"]["query"]["pageToken"] == "<redacted>"
    assert result.metadata["page_token_supplied"] is True
    assert OPAQUE_TOKEN not in repr(result.metadata)
    assert OPAQUE_TOKEN not in str(result.reason)
    # result.action is the caller's own input object, which VELOX echoes back
    # unchanged for every capability; it is upstream of this contract, so the
    # surfaces asserted here are the ones VELOX itself produces.
    # It still genuinely reached Google on the wire.
    assert dict(requests[0].url.params)["pageToken"] == OPAQUE_TOKEN


def test_page_token_does_not_leak_into_provider_failure_output() -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(400, json={"error": "raw-secret"})
    )

    result = execute(executor, list_action(page_token=OPAQUE_TOKEN))

    assert result.status == WorkerExecutionStatus.FAILED
    # Everything VELOX produces from the failure: message, metadata, failure detail.
    blob = repr(result.metadata) + str(result.reason) + repr(result.failure)
    assert OPAQUE_TOKEN not in blob
    assert "raw-secret" not in blob


def test_page_token_never_appears_in_the_request_url_path_or_credentials() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    HttpxCalendarTransportClient(
        httpx.Client(transport=httpx.MockTransport(handler))
    ).execute(
        CalendarProviderRequest(
            operation=LIST_CAPABILITY,
            path=LIST_PATH,
            query={**BASE_QUERY, "pageToken": OPAQUE_TOKEN},
        ),
        page_credentials(),
    )

    assert requests[0].url.path == LIST_PATH
    assert OPAQUE_TOKEN not in requests[0].url.path
    assert PAGE_ACCESS_TOKEN not in str(requests[0].url)


# --- transport validation ----------------------------------------------------


def test_transport_accepts_the_bounded_continuation_shape() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    response = HttpxCalendarTransportClient(
        httpx.Client(transport=httpx.MockTransport(handler))
    ).execute(
        CalendarProviderRequest(
            operation=LIST_CAPABILITY,
            path=LIST_PATH,
            query={**BASE_QUERY, "pageToken": OPAQUE_TOKEN},
        ),
        page_credentials(),
    )

    assert response.failure is None
    assert response.body["events"] == ()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "query",
    [
        # Malformed or non-string tokens never reach Google.
        {**BASE_QUERY, "pageToken": ""},
        {**BASE_QUERY, "pageToken": "   "},
        {**BASE_QUERY, "pageToken": " padded"},
        {**BASE_QUERY, "pageToken": "padded "},
        {**BASE_QUERY, "pageToken": 7},
        {**BASE_QUERY, "pageToken": None},
        # A token cannot substitute for the bounds.
        {"pageToken": OPAQUE_TOKEN},
        {"timeMin": TIME_MIN, "timeMax": TIME_MAX, "pageToken": OPAQUE_TOKEN},
        # Unbounded or off-contract alongside a valid token.
        {**BASE_QUERY, "maxResults": "2500", "pageToken": OPAQUE_TOKEN},
        {**BASE_QUERY, "singleEvents": "false", "pageToken": OPAQUE_TOKEN},
        {**BASE_QUERY, "orderBy": "updated", "pageToken": OPAQUE_TOKEN},
        {**BASE_QUERY, "pageToken": OPAQUE_TOKEN, "syncToken": "opaque-sync"},
        {**BASE_QUERY, "pageToken": OPAQUE_TOKEN, "q": "search-term"},
    ],
)
def test_transport_rejects_unexpected_pagination_query_shapes(
    query: dict[str, object],
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("off-contract pagination query reached Google")

    response = HttpxCalendarTransportClient(
        httpx.Client(transport=httpx.MockTransport(unreachable))
    ).execute(
        CalendarProviderRequest(
            operation=LIST_CAPABILITY,
            path=LIST_PATH,
            query=query,
        ),
        page_credentials(),
    )

    assert response.failure is not None
    assert response.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert response.failure.provider_reason == "invalidRequest"
    assert response.body["external_execution_performed"] is False


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_transport_rejects_any_write_method_on_the_paged_list_path(
    method: str,
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a write method reached Google")

    response = HttpxCalendarTransportClient(
        httpx.Client(transport=httpx.MockTransport(unreachable))
    ).execute(
        CalendarProviderRequest(
            operation=LIST_CAPABILITY,
            path=LIST_PATH,
            method=method,
            query={**BASE_QUERY, "pageToken": OPAQUE_TOKEN},
        ),
        page_credentials(),
    )

    assert response.failure is not None
    assert response.failure.provider_reason == "invalidRequest"


def test_transport_rejects_a_paged_list_request_carrying_a_body() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a list request with a body reached Google")

    response = HttpxCalendarTransportClient(
        httpx.Client(transport=httpx.MockTransport(unreachable))
    ).execute(
        CalendarProviderRequest(
            operation=LIST_CAPABILITY,
            path=LIST_PATH,
            body={"summary": "new event"},
            query={**BASE_QUERY, "pageToken": OPAQUE_TOKEN},
        ),
        page_credentials(),
    )

    assert response.failure is not None
    assert response.failure.provider_reason == "invalidRequest"


# --- deterministic fake ------------------------------------------------------


def paginating_executor(event_count: int) -> CalendarWorkerExecutor:
    events = {
        f"calendar-event-{index}": CalendarEvent(
            event_id=f"calendar-event-{index}",
            title=f"Event {index}",
            start="2026-09-04T09:00:00Z",
            end="2026-09-04T09:30:00Z",
        )
        for index in range(1, event_count + 1)
    }
    return CalendarWorkerExecutor(
        provider_composition=CalendarProviderComposition(
            transport_client=FakeCalendarTransportClient(events=events),
        )
    )


def test_fake_transport_reports_truncation_instead_of_claiming_completeness() -> None:
    result = execute(paginating_executor(5), list_action())

    assert result.metadata["event_count"] == MAX_RESULTS
    assert result.metadata["has_more_pages"] is True
    assert isinstance(result.metadata["next_page_token"], str)


def test_fake_transport_walks_pages_only_when_the_caller_asks() -> None:
    executor = paginating_executor(5)

    first = execute(executor, list_action())
    second = execute(executor, list_action(page_token=first.metadata["next_page_token"]))
    third = execute(executor, list_action(page_token=second.metadata["next_page_token"]))

    seen = [
        tuple(event["event_id"] for event in result.metadata["events"])
        for result in (first, second, third)
    ]
    assert seen == [
        ("calendar-event-1", "calendar-event-2"),
        ("calendar-event-3", "calendar-event-4"),
        ("calendar-event-5",),
    ]
    assert third.metadata["has_more_pages"] is False
    assert third.metadata["next_page_token"] is None
    # No page ever contains another page's events.
    assert len(set(seen[0]) | set(seen[1]) | set(seen[2])) == 5


def test_fake_transport_rejects_a_token_it_did_not_issue() -> None:
    result = execute(paginating_executor(5), list_action(page_token=OPAQUE_TOKEN))

    assert result.status == WorkerExecutionStatus.FAILED
    assert result.failure is not None
    assert result.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert OPAQUE_TOKEN not in repr(result.metadata)


def test_page_token_is_confined_to_the_request_and_a_presence_flag() -> None:
    """The token reaches Google and nothing else VELOX emits.

    It does remain in the caller's own Action payload, which VELOX echoes back
    unchanged for every capability exactly as it does for calendar_event_id.
    Scrubbing caller input is a separate concern from this contract.
    """
    executor, requests = executor_over(
        lambda request: httpx.Response(
            200, json={"items": [event_payload(3)], "nextPageToken": SECOND_OPAQUE_TOKEN}
        )
    )

    result = execute(executor, list_action(page_token=OPAQUE_TOKEN))

    emitted = repr(result.metadata) + str(result.reason) + repr(result.failure)
    assert OPAQUE_TOKEN not in emitted
    assert result.metadata["page_token_supplied"] is True
    assert result.metadata["provider_request"]["query"]["pageToken"] == "<redacted>"
    # The freshly returned token is surfaced, since a caller needs it to continue.
    assert result.metadata["next_page_token"] == SECOND_OPAQUE_TOKEN
    assert dict(requests[0].url.params)["pageToken"] == OPAQUE_TOKEN


# --- token confinement across every result surface ---------------------------

SENTINEL_TOKEN = "SENTINEL-PAGE-TOKEN-MUST-NEVER-ESCAPE-7f3a91"


def result_surfaces(result) -> str:
    """Every surface a caller can read off a WorkerExecutionResult."""
    return "|".join(
        (
            repr(result),
            repr(result.action),
            repr(result.action.payload),
            repr(result.metadata),
            repr(result.failure),
            repr(result.failure.metadata if result.failure is not None else None),
            str(result.reason),
        )
    )


def test_sentinel_token_reaches_the_transport_but_no_result_surface() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(
            200,
            json={"items": [event_payload(3)], "nextPageToken": SECOND_OPAQUE_TOKEN},
        )
    )

    result = execute(executor, list_action(page_token=SENTINEL_TOKEN))

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    # It reached Google exactly, unmodified.
    assert dict(requests[0].url.params)["pageToken"] == SENTINEL_TOKEN
    # And it appears on no surface the caller can read back.
    assert SENTINEL_TOKEN not in result_surfaces(result)
    assert result.action.payload["page_token"] == "<redacted>"
    assert result.metadata["page_token_supplied"] is True
    assert result.metadata["provider_request"]["query"]["pageToken"] == "<redacted>"


def test_sentinel_token_is_absent_from_a_provider_failure_result() -> None:
    executor, requests = executor_over(
        lambda request: httpx.Response(403, json={"error": "raw-secret"})
    )

    result = execute(executor, list_action(page_token=SENTINEL_TOKEN))

    assert result.status == WorkerExecutionStatus.FAILED
    assert dict(requests[0].url.params)["pageToken"] == SENTINEL_TOKEN
    assert SENTINEL_TOKEN not in result_surfaces(result)
    assert "raw-secret" not in result_surfaces(result)


@pytest.mark.parametrize(
    "invalid_token",
    [
        f" {SENTINEL_TOKEN}",
        f"{SENTINEL_TOKEN} ",
        f"\t{SENTINEL_TOKEN}\n",
    ],
)
def test_rejected_sentinel_token_is_absent_from_the_validation_failure(
    invalid_token: str,
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid page token reached the provider")

    executor, requests = executor_over(unreachable)

    result = execute(executor, list_action(page_token=invalid_token))

    assert result.status == WorkerExecutionStatus.FAILED
    assert result.failure is not None
    assert result.failure.metadata["field"] == "page_token"
    # Rejected before any provider call, and the bad token is not echoed back.
    assert requests == []
    assert SENTINEL_TOKEN not in result_surfaces(result)
    assert result.action.payload["page_token"] == "<redacted>"


def test_redacting_the_token_preserves_action_identity() -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(200, json={"items": [event_payload(3)]})
    )
    action = list_action(page_token=SENTINEL_TOKEN)

    result = execute(executor, action)

    # Same action, with only the token value replaced.
    assert result.action.id == action.id
    assert result.action.created_at == action.created_at
    assert result.action.type == action.type
    assert result.action.target == action.target
    assert result.action.executor_role == action.executor_role
    assert result.action.metadata == action.metadata
    assert {
        key: value
        for key, value in result.action.payload.items()
        if key != "page_token"
    } == {key: value for key, value in action.payload.items() if key != "page_token"}
    # The caller's own object is never mutated in place.
    assert action.payload["page_token"] == SENTINEL_TOKEN


def test_first_page_result_returns_the_caller_action_untouched() -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(200, json={"items": [event_payload(1)]})
    )
    action = list_action()

    result = execute(executor, action)

    # No token present, so no copy is made and nothing about the action changes.
    assert result.action is action
    assert "page_token" not in result.action.payload


def test_events_get_result_action_is_unchanged_by_pagination_redaction() -> None:
    executor, _ = executor_over(
        lambda request: httpx.Response(
            200,
            json={
                "id": "calendar-event-1",
                "summary": "Planning",
                "start": {"dateTime": "2026-09-04T09:00:00Z"},
                "end": {"dateTime": "2026-09-04T09:30:00Z"},
            },
        )
    )
    action = Action(
        type="prepare_meeting",
        target="internal-velox-event-id",
        payload={"calendar_event_id": "calendar-event-1"},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = executor.execute(
        action,
        capability="prepare_meeting",
        account_context=ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.action is action
    assert result.action.payload == {"calendar_event_id": "calendar-event-1"}
