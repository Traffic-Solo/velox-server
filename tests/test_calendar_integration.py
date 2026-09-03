import socket

import httpx
import pytest
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.integrations.calendar import (
    CALENDAR_EXECUTOR_ROLE,
    CalendarCredentials,
    CalendarCredentialsProvider,
    CalendarCredentialsProviderError,
    CalendarEvent,
    CalendarProviderComposition,
    CalendarProviderFailure,
    CalendarProviderRequest,
    CalendarProviderResponse,
    CalendarTransportClient,
    CalendarWorkerExecutor,
    FakeCalendarCredentialsProvider,
    FakeCalendarTransportClient,
    HttpxCalendarTransportClient,
)
from apps.server.src.integrations.gmail import (
    GmailProviderComposition,
    GmailProviderRequest,
)
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerExecutionFailureCategory,
    WorkerExecutionStatus,
    WorkerExecutor,
)

CALENDAR_ACCOUNT_CONTEXT = WorkerAccountContext(
    principal="principal-1",
    account_identifier="calendar-account-1",
)
CALENDAR_ACCESS_TOKEN = "calendar-access-token-secret"


class UnexpectedCalendarProviderComposition(CalendarProviderComposition):
    def execute(
        self,
        request: CalendarProviderRequest,
        principal: str | None = None,
        account: str | None = None,
    ) -> CalendarProviderResponse:
        raise AssertionError("calendar provider composition invoked")


class StaticCalendarProviderComposition(CalendarProviderComposition):
    def __init__(self, response: CalendarProviderResponse) -> None:
        self.response = response

    def execute(
        self,
        request: CalendarProviderRequest,
        principal: str | None = None,
        account: str | None = None,
    ) -> CalendarProviderResponse:
        return self.response


def block_external_socket_calls(monkeypatch) -> None:
    def fail_external_call(*args, **kwargs):
        raise AssertionError("external API call attempted")

    monkeypatch.setattr(socket, "create_connection", fail_external_call)
    monkeypatch.setattr(socket, "socket", fail_external_call)


def calendar_credentials() -> CalendarCredentials:
    return CalendarCredentials(
        access_token=CALENDAR_ACCESS_TOKEN,
        principal="principal-1",
        account="calendar-account-1",
    )


def calendar_provider_request(
    *,
    event_id: str = "calendar-event-1",
    query: dict[str, object] | None = None,
) -> CalendarProviderRequest:
    return CalendarProviderRequest(
        operation="prepare_meeting",
        path=f"/calendar/v3/calendars/primary/events/{event_id}",
        query=query or {},
    )


def calendar_http_client(
    handler,
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_calendar_executor_role_is_vendor_neutral() -> None:
    assert CALENDAR_EXECUTOR_ROLE == ExecutorRole.CONTEXT_PREPARATION


def test_calendar_worker_executor_satisfies_worker_executor_contract() -> None:
    executor = CalendarWorkerExecutor()

    assert isinstance(executor, WorkerExecutor)


def test_calendar_worker_executor_requires_explicit_calendar_event_id() -> None:
    action = Action(
        type="prepare_calendar_context",
        target="internal-velox-event-id",
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )
    executor = CalendarWorkerExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.FAILED
    assert result.failure is not None
    assert result.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert result.failure.metadata == {"field": "calendar_event_id"}


def test_calendar_known_event_returns_structured_meeting_context() -> None:
    action = Action(
        type="prepare_calendar_context",
        target="internal-velox-event-id",
        payload={"calendar_event_id": "calendar-event-1"},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = CalendarWorkerExecutor().execute(
        action,
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["calendar_event_id"] == "calendar-event-1"
    assert result.metadata["found"] is True
    assert result.metadata["event"] == {
        "event_id": "calendar-event-1",
        "title": "Sprint 1 planning",
        "start": "2026-07-27T09:00:00Z",
        "end": "2026-07-27T09:30:00Z",
        "attendees": ("owner@example.com", "team@example.com"),
    }
    assert result.metadata["adapter"] == "fake_transport"


def test_calendar_event_data_originates_from_provider_response() -> None:
    provider_event = CalendarEvent(
        event_id="calendar-event-1",
        title="Provider-backed planning",
        start="2026-08-14T10:00:00Z",
        end="2026-08-14T10:45:00Z",
        attendees=("provider@example.com",),
    )
    executor = CalendarWorkerExecutor(
        provider_composition=CalendarProviderComposition(
            transport_client=FakeCalendarTransportClient(
                events={provider_event.event_id: provider_event},
            ),
        ),
    )
    action = Action(
        type="prepare_meeting",
        target="internal-velox-event-id",
        payload={"calendar_event_id": provider_event.event_id},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = executor.execute(
        action,
        capability="prepare_meeting",
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["event"] == {
        "event_id": provider_event.event_id,
        "title": provider_event.title,
        "start": provider_event.start,
        "end": provider_event.end,
        "attendees": provider_event.attendees,
    }


def test_calendar_unknown_event_is_successful_not_found() -> None:
    action = Action(
        type="prepare_meeting",
        target="internal-velox-event-id",
        payload={"calendar_event_id": "unknown-event"},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = CalendarWorkerExecutor().execute(
        action,
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["calendar_event_id"] == "unknown-event"
    assert result.metadata["found"] is False
    assert "event" not in result.metadata


def test_calendar_explicit_empty_event_store_remains_empty() -> None:
    executor = CalendarWorkerExecutor(
        provider_composition=CalendarProviderComposition(
            transport_client=FakeCalendarTransportClient(events={}),
        ),
    )
    action = Action(
        type="prepare_meeting",
        target="internal-velox-event-id",
        payload={"calendar_event_id": "calendar-event-1"},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = executor.execute(action, account_context=CALENDAR_ACCOUNT_CONTEXT)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["calendar_event_id"] == "calendar-event-1"
    assert result.metadata["found"] is False
    assert "event" not in result.metadata


@pytest.mark.parametrize("calendar_event_id", ["", "   ", None, 123])
def test_calendar_invalid_event_id_is_permanent_failure(
    calendar_event_id: object,
) -> None:
    action = Action(
        type="prepare_meeting",
        target="must-not-be-used",
        payload={"calendar_event_id": calendar_event_id},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = CalendarWorkerExecutor().execute(action)

    assert result.status == WorkerExecutionStatus.FAILED
    assert result.failure is not None
    assert result.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert result.failure.metadata == {"field": "calendar_event_id"}


def test_calendar_invalid_event_id_does_not_invoke_adapters() -> None:
    executor = CalendarWorkerExecutor(
        provider_composition=UnexpectedCalendarProviderComposition(),
    )
    action = Action(
        type="prepare_meeting",
        target="must-not-be-used",
        payload={"calendar_event_id": " "},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = executor.execute(
        action,
        capability="prepare_meeting",
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.FAILED


@pytest.mark.parametrize(
    "capability",
    ["prepare_meeting", "prepare_calendar_context"],
)
def test_calendar_capability_identifiers_use_meeting_context(capability: str) -> None:
    action = Action(
        type=capability,
        target="internal-velox-event-id",
        payload={"calendar_event_id": "calendar-event-1"},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = CalendarWorkerExecutor().execute(
        action,
        capability=capability,
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["capability"] == capability
    assert result.metadata["found"] is True


def test_calendar_worker_executor_constructs_account_aware_provider_request() -> None:
    action = Action(
        type="prepare_calendar_context",
        target="calendar-placeholder",
        payload={
            "calendar_event_id": "calendar-event-1",
            "provider": "gmail",
            "account": "untrusted-account",
        },
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )
    executor = CalendarWorkerExecutor()

    result = executor.execute(
        action,
        capability="prepare_calendar_context",
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["account_context_used"] == (
        CALENDAR_ACCOUNT_CONTEXT.as_metadata()
    )
    assert result.metadata["provider_request"] == {
        "operation": "prepare_calendar_context",
        "path": "/calendar/v3/calendars/primary/events/calendar-event-1",
        "method": "GET",
        "body": None,
        "query": {},
        "account_context": CALENDAR_ACCOUNT_CONTEXT.as_metadata(),
    }
    assert result.metadata["provider_response"]["integration"] == "calendar"
    assert result.metadata["account_context_used"] == (
        CALENDAR_ACCOUNT_CONTEXT.as_metadata()
    )
    assert result.metadata["provider_request"]["account_context"] == (
        CALENDAR_ACCOUNT_CONTEXT.as_metadata()
    )
    assert {
        "account",
        "principal",
        "operation",
        "path",
        "method",
        "token_type",
    }.isdisjoint(result.metadata["provider_response"])
    assert result.metadata["adapter"] == "fake_transport"
    assert result.metadata["found"] is True


def test_calendar_provider_request_encodes_event_id_as_one_path_segment() -> None:
    action = Action(
        type="prepare_calendar_context",
        target="internal-velox-event-id",
        payload={"calendar_event_id": "calendar/event 2"},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = CalendarWorkerExecutor().execute(
        action,
        capability="prepare_calendar_context",
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["calendar_event_id"] == "calendar/event 2"
    assert result.metadata["provider_request"]["path"] == (
        "/calendar/v3/calendars/primary/events/calendar%2Fevent%202"
    )


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (None, WorkerExecutionStatus.SUCCEEDED),
        (
            CalendarProviderFailure(
                category=WorkerExecutionFailureCategory.TRANSIENT,
                message="calendar provider unavailable",
                retryable=True,
                provider_status_code=503,
                provider_reason="backendError",
            ),
            WorkerExecutionStatus.FAILED,
        ),
    ],
)
def test_calendar_provider_response_metadata_is_allowlisted(
    failure: CalendarProviderFailure | None,
    expected_status: WorkerExecutionStatus,
) -> None:
    response = CalendarProviderResponse(
        status_code=503 if failure is not None else 200,
        body={
            "external_execution_performed": False,
            "integration": "calendar",
            "adapter": "fake_transport",
            "found": True,
            "event": {
                "event_id": "calendar-event-1",
                "title": "Provider result",
                "start": "2026-08-14T10:00:00Z",
                "end": "2026-08-14T10:30:00Z",
                "attendees": ["safe@example.com"],
                "authorization": "must-not-leak",
            },
            "operation": {"credentials": "must-not-leak"},
            "path": "must-not-leak",
            "method": {"authorization": "must-not-leak"},
            "token_type": "must-not-leak",
            "principal": ["must-not-leak"],
            "account": {"access_token": "must-not-leak"},
            "failed": failure is not None,
            "access_token": "must-not-leak",
            "refresh_token": "must-not-leak",
            "credentials": {"secret": "must-not-leak"},
            "authorization": "Bearer must-not-leak",
        },
        failure=failure,
    )
    executor = CalendarWorkerExecutor(
        provider_composition=StaticCalendarProviderComposition(response),
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
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == expected_status
    provider_metadata = result.metadata["provider_response"]
    assert provider_metadata["external_execution_performed"] is False
    assert provider_metadata["integration"] == "calendar"
    assert provider_metadata["adapter"] == "fake_transport"
    assert provider_metadata["failed"] is (failure is not None)
    assert result.metadata["account_context_used"] == (
        CALENDAR_ACCOUNT_CONTEXT.as_metadata()
    )
    assert result.metadata["provider_request"] == {
        "operation": "prepare_meeting",
        "path": "/calendar/v3/calendars/primary/events/calendar-event-1",
        "method": "GET",
        "body": None,
        "query": {},
        "account_context": CALENDAR_ACCOUNT_CONTEXT.as_metadata(),
    }
    assert "must-not-leak" not in repr(result.metadata)
    if failure is None:
        assert result.metadata["found"] is True
        assert result.metadata["event"] == {
            "event_id": "calendar-event-1",
            "title": "Provider result",
            "start": "2026-08-14T10:00:00Z",
            "end": "2026-08-14T10:30:00Z",
            "attendees": ("safe@example.com",),
        }
    else:
        assert "found" not in result.metadata
        assert "event" not in result.metadata
    for sensitive_field in (
        "account",
        "principal",
        "operation",
        "path",
        "method",
        "token_type",
        "access_token",
        "refresh_token",
        "credentials",
        "authorization",
    ):
        assert sensitive_field not in provider_metadata
    if failure is not None:
        assert result.failure is not None
        assert result.failure.category == WorkerExecutionFailureCategory.TRANSIENT


def test_calendar_fake_credentials_provider_returns_deterministic_credentials() -> None:
    provider = FakeCalendarCredentialsProvider()

    credentials = provider.get_credentials(
        principal="principal-1",
        account="account-1",
    )

    assert isinstance(provider, CalendarCredentialsProvider)
    assert credentials == CalendarCredentials(
        access_token="fake-calendar-access-token:principal-1:account-1",
        principal="principal-1",
        account="account-1",
        expires_at="2099-01-01T00:00:00Z",
    )


def test_calendar_fake_credentials_provider_handles_missing_account_safely() -> None:
    provider = FakeCalendarCredentialsProvider()

    try:
        provider.get_credentials(principal="principal-1", account=None)
    except CalendarCredentialsProviderError as error:
        failure = error.failure
    else:
        raise AssertionError("expected CalendarCredentialsProviderError")

    assert failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert failure.message == "calendar credentials request missing account"
    assert failure.metadata == {"field": "account"}


def test_calendar_fake_credentials_provider_distinguishes_account_contexts() -> None:
    provider = FakeCalendarCredentialsProvider()

    account_1_credentials = provider.get_credentials(
        principal="principal-1",
        account="calendar-account-1",
    )
    account_2_credentials = provider.get_credentials(
        principal="principal-1",
        account="calendar-account-2",
    )

    assert account_1_credentials.access_token != account_2_credentials.access_token
    assert account_1_credentials.account == "calendar-account-1"
    assert account_2_credentials.account == "calendar-account-2"


def test_calendar_fake_credentials_provider_handles_missing_principal_safely() -> None:
    provider = FakeCalendarCredentialsProvider()

    try:
        provider.get_credentials(principal="", account="account-1")
    except CalendarCredentialsProviderError as error:
        failure = error.failure
    else:
        raise AssertionError("expected CalendarCredentialsProviderError")

    assert failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert failure.message == "calendar credentials request missing principal"
    assert failure.metadata == {"field": "principal"}


def test_calendar_fake_transport_returns_deterministic_response() -> None:
    credentials = CalendarCredentials(
        access_token="fake-token",
        principal="principal-1",
        account="calendar-account-1",
    )
    request = CalendarProviderRequest(
        operation="bootstrap",
        path="/calendar/v3/users/me/calendarList",
    )
    client = FakeCalendarTransportClient()

    response = client.execute(request, credentials)

    assert isinstance(client, CalendarTransportClient)
    assert response == CalendarProviderResponse(
        status_code=200,
        body={
            "external_execution_performed": False,
            "integration": "calendar",
            "adapter": "fake_transport",
            "operation": "bootstrap",
            "path": "/calendar/v3/users/me/calendarList",
            "method": "GET",
            "token_type": "Bearer",
            "principal": "principal-1",
            "account": "calendar-account-1",
        },
    )


def test_calendar_httpx_transport_uses_exact_get_url_query_and_authorization() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "calendar-event-1",
                "summary": "Planning",
                "start": {"dateTime": "2026-09-04T09:00:00Z"},
                "end": {"dateTime": "2026-09-04T09:30:00Z"},
            },
        )

    response = HttpxCalendarTransportClient(calendar_http_client(handler)).execute(
        calendar_provider_request(query={"timeZone": "UTC"}),
        calendar_credentials(),
    )

    assert response.failure is None
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == (
        "https://www.googleapis.com/calendar/v3/calendars/primary/events/"
        "calendar-event-1?timeZone=UTC"
    )
    assert requests[0].headers["Authorization"] == (
        f"Bearer {CALENDAR_ACCESS_TOKEN}"
    )
    assert CALENDAR_ACCESS_TOKEN not in str(requests[0].url)
    assert requests[0].content == b""


@pytest.mark.parametrize(
    ("provider_event", "expected_event"),
    [
        (
            {
                "id": "calendar-event-1",
                "summary": "Timed planning",
                "start": {"dateTime": "2026-09-04T09:00:00Z"},
                "end": {"dateTime": "2026-09-04T09:30:00Z"},
                "attendees": [
                    {"email": "owner@example.com"},
                    {"email": "team@example.com"},
                ],
            },
            {
                "event_id": "calendar-event-1",
                "title": "Timed planning",
                "start": "2026-09-04T09:00:00Z",
                "end": "2026-09-04T09:30:00Z",
                "attendees": ("owner@example.com", "team@example.com"),
            },
        ),
        (
            {
                "id": "calendar-event-1",
                "summary": "All day",
                "start": {"date": "2026-09-04"},
                "end": {"date": "2026-09-05"},
            },
            {
                "event_id": "calendar-event-1",
                "title": "All day",
                "start": "2026-09-04",
                "end": "2026-09-05",
                "attendees": (),
            },
        ),
        (
            {
                "id": "calendar-event-1",
                "start": {"dateTime": "2026-09-04T09:00:00Z"},
                "end": {"dateTime": "2026-09-04T09:30:00Z"},
            },
            {
                "event_id": "calendar-event-1",
                "title": "",
                "start": "2026-09-04T09:00:00Z",
                "end": "2026-09-04T09:30:00Z",
                "attendees": (),
            },
        ),
    ],
)
def test_calendar_httpx_transport_maps_google_event_shape(
    provider_event: dict[str, object],
    expected_event: dict[str, object],
) -> None:
    client = calendar_http_client(
        lambda request: httpx.Response(200, json=provider_event)
    )

    response = HttpxCalendarTransportClient(client).execute(
        calendar_provider_request(),
        calendar_credentials(),
    )

    assert response.status_code == 200
    assert response.failure is None
    assert response.body["found"] is True
    assert response.body["event"] == expected_event


def test_calendar_httpx_transport_maps_not_found_to_existing_semantics() -> None:
    client = calendar_http_client(
        lambda request: httpx.Response(404, json={"error": "must-not-leak"})
    )

    response = HttpxCalendarTransportClient(client).execute(
        calendar_provider_request(),
        calendar_credentials(),
    )

    assert response.status_code == 404
    assert response.failure is None
    assert response.body["found"] is False
    assert "must-not-leak" not in repr(response)


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
            {
                "error": {
                    "errors": [
                        {"reason": "rateLimitExceeded", "message": "raw-secret"}
                    ]
                }
            },
            WorkerExecutionFailureCategory.TRANSIENT,
            True,
            "rateLimitExceeded",
        ),
        (
            403,
            {
                "error": {
                    "errors": [{"reason": "forbidden", "message": "raw-secret"}]
                }
            },
            WorkerExecutionFailureCategory.PERMANENT,
            False,
            "forbidden",
        ),
        (
            429,
            {"error": "raw-secret"},
            WorkerExecutionFailureCategory.TRANSIENT,
            True,
            "rateLimitExceeded",
        ),
        (
            500,
            {"error": "raw-secret"},
            WorkerExecutionFailureCategory.TRANSIENT,
            True,
            "providerUnavailable",
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
def test_calendar_httpx_transport_maps_provider_failures(
    status_code: int,
    body: dict[str, object],
    category: WorkerExecutionFailureCategory,
    retryable: bool,
    reason: str,
) -> None:
    client = calendar_http_client(
        lambda request: httpx.Response(status_code, json=body)
    )

    response = HttpxCalendarTransportClient(client).execute(
        calendar_provider_request(),
        calendar_credentials(),
    )

    assert response.status_code == status_code
    assert response.failure is not None
    assert response.failure.category == category
    assert response.failure.retryable is retryable
    assert response.failure.provider_reason == reason
    assert "raw-secret" not in repr(response)
    assert CALENDAR_ACCESS_TOKEN not in repr(response)


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ConnectError("network-secret"),
        httpx.ReadTimeout("timeout-secret"),
    ],
)
def test_calendar_httpx_transport_maps_network_failures_as_transient(
    transport_error: Exception,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise transport_error

    response = HttpxCalendarTransportClient(calendar_http_client(handler)).execute(
        calendar_provider_request(),
        calendar_credentials(),
    )

    assert response.failure is not None
    assert response.failure.category == WorkerExecutionFailureCategory.TRANSIENT
    assert response.failure.retryable is True
    assert "secret" not in repr(response)
    assert CALENDAR_ACCESS_TOKEN not in repr(response)


@pytest.mark.parametrize(
    "provider_response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"id": "calendar-event-1"}),
        httpx.Response(
            200,
            json={
                "id": "different-event",
                "start": {"date": "2026-09-04"},
                "end": {"date": "2026-09-05"},
            },
        ),
    ],
)
def test_calendar_httpx_transport_maps_invalid_success_as_internal(
    provider_response: httpx.Response,
) -> None:
    client = calendar_http_client(lambda request: provider_response)

    response = HttpxCalendarTransportClient(client).execute(
        calendar_provider_request(),
        calendar_credentials(),
    )

    assert response.failure is not None
    assert response.failure.category == WorkerExecutionFailureCategory.INTERNAL
    assert response.failure.provider_reason == "invalidProviderResponse"


def test_calendar_httpx_transport_never_exposes_authorization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = calendar_http_client(
        lambda request: httpx.Response(
            401,
            json={"error": f"Bearer {CALENDAR_ACCESS_TOKEN}"},
        )
    )

    response = HttpxCalendarTransportClient(client).execute(
        calendar_provider_request(),
        calendar_credentials(),
    )

    assert CALENDAR_ACCESS_TOKEN not in repr(response)
    assert CALENDAR_ACCESS_TOKEN not in caplog.text


def test_calendar_provider_composition_executes_fake_credentials_and_transport() -> None:
    request = CalendarProviderRequest(
        operation="bootstrap",
        path="/calendar/v3/users/me/calendarList",
    )
    composition = CalendarProviderComposition()

    response = composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )

    assert response.status_code == 200
    assert response.body["external_execution_performed"] is False
    assert response.body["integration"] == "calendar"
    assert response.body["adapter"] == "fake_transport"
    assert response.body["operation"] == "bootstrap"
    assert response.body["token_type"] == "Bearer"
    assert response.body["principal"] == "principal-1"
    assert response.body["account"] == "account-1"


def test_calendar_provider_composition_requires_account_context_safely() -> None:
    request = CalendarProviderRequest(
        operation="bootstrap",
        path="/calendar/v3/users/me/calendarList",
    )
    composition = CalendarProviderComposition()

    response = composition.execute(
        request,
        principal="principal-1",
        account=None,
    )

    assert response.status_code == 500
    assert response.body == {
        "external_execution_performed": False,
        "integration": "calendar",
        "adapter": "fake_provider_composition",
        "operation": "bootstrap",
        "failed": True,
    }
    assert response.failure is not None
    assert response.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert response.failure.message == "calendar credentials request missing account"
    assert response.failure.metadata == {"field": "account"}


def test_calendar_provider_composition_distinguishes_account_contexts() -> None:
    request = CalendarProviderRequest(
        operation="bootstrap",
        path="/calendar/v3/users/me/calendarList",
    )
    composition = CalendarProviderComposition()

    account_1_response = composition.execute(
        request,
        principal="principal-1",
        account="calendar-account-1",
    )
    account_2_response = composition.execute(
        request,
        principal="principal-1",
        account="calendar-account-2",
    )

    assert account_1_response.body["account"] == "calendar-account-1"
    assert account_2_response.body["account"] == "calendar-account-2"
    assert account_1_response.body != account_2_response.body


def test_gmail_and_calendar_can_use_separate_account_contexts() -> None:
    principal = "principal-1"
    gmail_request = GmailProviderRequest(
        operation="read",
        path="/gmail/v1/users/me/messages/gmail-message-1",
    )
    calendar_request = CalendarProviderRequest(
        operation="bootstrap",
        path="/calendar/v3/users/me/calendarList",
    )

    gmail_response = GmailProviderComposition().execute(
        gmail_request,
        principal=principal,
        account="gmail-account-1",
    )
    calendar_response = CalendarProviderComposition().execute(
        calendar_request,
        principal=principal,
        account="calendar-account-1",
    )

    assert gmail_response.body["principal"] == principal
    assert calendar_response.body["principal"] == principal
    assert gmail_response.body["account"] == "gmail-account-1"
    assert calendar_response.body["account"] == "calendar-account-1"


def test_calendar_provider_composition_returns_credentials_failure_safely() -> None:
    failure = CalendarProviderFailure(
        category=WorkerExecutionFailureCategory.PERMANENT,
        message="calendar credentials unavailable",
        provider_status_code=401,
        provider_reason="invalidCredentials",
        metadata={"principal": "principal-1"},
    )
    composition = CalendarProviderComposition(
        credentials_provider=FakeCalendarCredentialsProvider(
            failures={"principal-1": failure},
        ),
    )
    request = CalendarProviderRequest(
        operation="bootstrap",
        path="/calendar/v3/users/me/calendarList",
    )

    response = composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )

    assert response.status_code == 401
    assert response.body == {
        "external_execution_performed": False,
        "integration": "calendar",
        "adapter": "fake_provider_composition",
        "operation": "bootstrap",
        "failed": True,
    }
    assert response.failure == failure


def test_calendar_provider_composition_returns_transport_failure_safely() -> None:
    failure = CalendarProviderFailure(
        category=WorkerExecutionFailureCategory.TRANSIENT,
        message="calendar provider unavailable",
        retryable=True,
        provider_status_code=503,
        provider_reason="backendError",
        metadata={"operation": "bootstrap"},
    )
    composition = CalendarProviderComposition(
        transport_client=FakeCalendarTransportClient(failures={"bootstrap": failure}),
    )
    request = CalendarProviderRequest(
        operation="bootstrap",
        path="/calendar/v3/users/me/calendarList",
    )

    response = composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )

    assert response.status_code == 503
    assert response.body == {
        "external_execution_performed": False,
        "integration": "calendar",
        "adapter": "fake_transport",
        "operation": "bootstrap",
        "failed": True,
    }
    assert response.failure == failure


@pytest.mark.parametrize(
    ("failure_category", "provider_component"),
    [
        (WorkerExecutionFailureCategory.PERMANENT, "credentials"),
        (WorkerExecutionFailureCategory.TRANSIENT, "transport"),
    ],
)
def test_calendar_executor_preserves_provider_failure_classification(
    failure_category: WorkerExecutionFailureCategory,
    provider_component: str,
) -> None:
    failure = CalendarProviderFailure(
        category=failure_category,
        message=f"calendar {provider_component} failure",
        retryable=failure_category == WorkerExecutionFailureCategory.TRANSIENT,
        provider_status_code=503,
        provider_reason="testFailure",
    )
    if provider_component == "credentials":
        composition = CalendarProviderComposition(
            credentials_provider=FakeCalendarCredentialsProvider(
                failures={"principal-1": failure},
            ),
        )
    else:
        composition = CalendarProviderComposition(
            transport_client=FakeCalendarTransportClient(
                failures={"prepare_meeting": failure},
            ),
        )
    executor = CalendarWorkerExecutor(provider_composition=composition)
    action = Action(
        type="prepare_meeting",
        target="internal-velox-event-id",
        payload={"calendar_event_id": "calendar-event-1"},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )

    result = executor.execute(
        action,
        capability="prepare_meeting",
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.FAILED
    assert result.failure is not None
    assert result.failure.category == failure_category
    assert "found" not in result.metadata


def test_calendar_bootstrap_makes_no_external_api_calls(monkeypatch) -> None:
    block_external_socket_calls(monkeypatch)
    action = Action(
        type="prepare_calendar_context",
        target="calendar-placeholder",
        payload={"calendar_event_id": "calendar-event-1"},
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )
    executor = CalendarWorkerExecutor()
    request = CalendarProviderRequest(
        operation="bootstrap",
        path="/calendar/v3/users/me/calendarList",
    )

    execution_result = executor.execute(
        action,
        account_context=CALENDAR_ACCOUNT_CONTEXT,
    )
    provider_response = executor.provider_composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )

    assert execution_result.status == WorkerExecutionStatus.SUCCEEDED
    assert execution_result.metadata["external_execution_performed"] is False
    assert provider_response.status_code == 200
    assert provider_response.body["external_execution_performed"] is False
