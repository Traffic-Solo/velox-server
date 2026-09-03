import json
import socket

import pytest
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.integrations import calendar_manual_sync
from apps.server.src.integrations.calendar import (
    CalendarCredentials,
    CalendarCredentialsProviderError,
    CalendarProviderComposition,
    CalendarProviderFailure,
    CalendarProviderRequest,
    CalendarProviderResponse,
    CalendarWorkerExecutor,
)
from apps.server.src.integrations.calendar_manual_sync import (
    ManualCalendarSyncError,
    ManualCalendarSyncFailureCode,
    ManualCalendarSyncRequest,
    ManualCalendarSyncResult,
    ManualCalendarSyncService,
)
from apps.server.src.workers.executor import (
    WorkerExecutionFailureCategory,
    WorkerExecutionStatus,
)

PRINCIPAL = "velox-principal-1"
ACCOUNT_IDENTIFIER = "velox-calendar-account-1"
EVENT_ID = "google-event-1"
ACCESS_TOKEN = "access-token-secret-value"
REFRESH_TOKEN = "refresh-token-secret-value"
_DEFAULT_EVENT = object()


class RecordingCredentialsProvider:
    def __init__(self, failure: CalendarProviderFailure | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str | None, str | None]] = []

    def get_credentials(
        self,
        principal: str | None,
        account: str | None,
    ) -> CalendarCredentials:
        self.calls.append((principal, account))
        if self.failure is not None:
            raise CalendarCredentialsProviderError(self.failure)
        assert principal is not None
        assert account is not None
        return CalendarCredentials(
            access_token=ACCESS_TOKEN,
            principal=principal,
            account=account,
        )


class RecordingTransportClient:
    def __init__(self, response: CalendarProviderResponse) -> None:
        self.response = response
        self.calls: list[tuple[CalendarProviderRequest, CalendarCredentials]] = []

    def execute(
        self,
        request: CalendarProviderRequest,
        credentials: CalendarCredentials,
    ) -> CalendarProviderResponse:
        self.calls.append((request, credentials))
        return self.response


def successful_provider_response(
    *,
    event_id: str = EVENT_ID,
    event: object = _DEFAULT_EVENT,
) -> CalendarProviderResponse:
    event_value = (
        {
            "event_id": event_id,
            "title": "Pilot planning",
            "start": "2026-09-04T09:00:00Z",
            "end": "2026-09-04T09:30:00Z",
            "attendees": ("owner@example.com", "team@example.com"),
            "raw_google_payload": REFRESH_TOKEN,
        }
        if event is _DEFAULT_EVENT
        else event
    )
    return CalendarProviderResponse(
        status_code=200,
        body={
            "external_execution_performed": True,
            "integration": "calendar",
            "adapter": "httpx_transport",
            "found": True,
            "event": event_value,
            "access_token": ACCESS_TOKEN,
        },
    )


def manual_sync_service(
    response: CalendarProviderResponse,
    *,
    credentials_provider: RecordingCredentialsProvider | None = None,
    container: ApplicationContainer | None = None,
) -> tuple[
    ManualCalendarSyncService,
    RecordingCredentialsProvider,
    RecordingTransportClient,
    ApplicationContainer,
]:
    provider = credentials_provider or RecordingCredentialsProvider()
    transport = RecordingTransportClient(response)
    composition = CalendarProviderComposition(
        credentials_provider=provider,
        transport_client=transport,
    )
    executor = CalendarWorkerExecutor(provider_composition=composition)
    application_container = container or ApplicationContainer()
    service = ManualCalendarSyncService(
        provider_executor=executor,
        ingress_adapter=application_container.calendar_ingress_adapter,
    )
    return service, provider, transport, application_container


def sync_request(*, event_id: str = EVENT_ID) -> ManualCalendarSyncRequest:
    return ManualCalendarSyncRequest(
        principal=PRINCIPAL,
        account_identifier=ACCOUNT_IDENTIFIER,
        event_id=event_id,
    )


def test_manual_sync_uses_exact_route_path_and_existing_workflow() -> None:
    encoded_event_id = "event/id with space?and=query"
    service, credentials, transport, container = manual_sync_service(
        successful_provider_response(event_id=encoded_event_id)
    )

    result = service.sync(sync_request(event_id=encoded_event_id))

    assert credentials.calls == [(PRINCIPAL, ACCOUNT_IDENTIFIER)]
    assert len(transport.calls) == 1
    provider_request, resolved_credentials = transport.calls[0]
    assert provider_request.operation == "prepare_meeting"
    assert provider_request.method == "GET"
    assert provider_request.path == (
        "/calendar/v3/calendars/primary/events/"
        "event%2Fid%20with%20space%3Fand%3Dquery"
    )
    assert provider_request.account_context is not None
    assert provider_request.account_context.principal == PRINCIPAL
    assert provider_request.account_context.account_identifier == ACCOUNT_IDENTIFIER
    assert resolved_credentials.access_token == ACCESS_TOKEN

    stored_events = container.event_repository.list_events()
    assert len(stored_events) == 1
    event = stored_events[0]
    assert event.payload == {
        "event_id": encoded_event_id,
        "title": "Pilot planning",
        "start": "2026-09-04T09:00:00Z",
        "end": "2026-09-04T09:30:00Z",
        "attendees": ("owner@example.com", "team@example.com"),
        "calendar_event_id": encoded_event_id,
    }
    assert container.event_lifecycle_states[event.id].status == "processed"
    assert container.event_inbox.list_pending() == []
    assert container.worker_execution_observer.list() == []

    queued_actions = container.action_queue.list()
    assert len(queued_actions) == 1
    action = queued_actions[0]
    assert action.target == str(event.id)
    assert action.payload == {
        "calendar_event_id": encoded_event_id,
        "capability_provider": "calendar",
        "account_context": {
            "principal": PRINCIPAL,
            "account_identifier": ACCOUNT_IDENTIFIER,
        },
    }
    assert result == ManualCalendarSyncResult(
        google_calendar_event_id=encoded_event_id,
        universal_event_id=str(event.id),
        acceptance_outcome="accepted",
        processing_outcome="processed",
        provider="calendar",
        principal=PRINCIPAL,
        account_identifier=ACCOUNT_IDENTIFIER,
        external_execution_performed=True,
    )


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        (("", ACCOUNT_IDENTIFIER, EVENT_ID), "principal"),
        (("   ", ACCOUNT_IDENTIFIER, EVENT_ID), "principal"),
        ((PRINCIPAL, "", EVENT_ID), "account_identifier"),
        ((PRINCIPAL, "   ", EVENT_ID), "account_identifier"),
        ((PRINCIPAL, ACCOUNT_IDENTIFIER, ""), "event_id"),
        ((PRINCIPAL, ACCOUNT_IDENTIFIER, "   "), "event_id"),
    ],
)
def test_invalid_explicit_input_fails_before_production_execution(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, str, str],
    field: str,
) -> None:
    production_sync_calls: list[ManualCalendarSyncRequest] = []
    monkeypatch.setattr(
        calendar_manual_sync,
        "_run_production_sync",
        production_sync_calls.append,
    )

    exit_code = calendar_manual_sync.main(arguments)

    assert exit_code == 2
    assert production_sync_calls == []
    error = None
    try:
        ManualCalendarSyncRequest(*arguments)
    except ManualCalendarSyncError as caught:
        error = caught
    assert error is not None
    assert error.code == ManualCalendarSyncFailureCode.INVALID_INPUT
    assert error.field == field


def test_event_not_found_is_a_safe_sync_failure() -> None:
    service, _, _, container = manual_sync_service(
        CalendarProviderResponse(
            status_code=404,
            body={
                "external_execution_performed": True,
                "integration": "calendar",
                "adapter": "httpx_transport",
                "found": False,
            },
        )
    )

    with pytest.raises(ManualCalendarSyncError) as caught:
        service.sync(sync_request())

    assert caught.value.code == ManualCalendarSyncFailureCode.EVENT_NOT_FOUND
    assert caught.value.external_execution_performed is True
    assert container.event_repository.list_events() == []


@pytest.mark.parametrize(
    ("provider_reason", "category", "expected_code"),
    [
        (
            "credentialRefreshUnavailable",
            WorkerExecutionFailureCategory.TRANSIENT,
            ManualCalendarSyncFailureCode.CREDENTIAL_REFRESH_FAILURE,
        ),
        (
            "transportUnavailable",
            WorkerExecutionFailureCategory.TRANSIENT,
            ManualCalendarSyncFailureCode.TRANSPORT_FAILURE,
        ),
        (
            "rateLimitExceeded",
            WorkerExecutionFailureCategory.TRANSIENT,
            ManualCalendarSyncFailureCode.PROVIDER_TRANSIENT_FAILURE,
        ),
        (
            "forbidden",
            WorkerExecutionFailureCategory.PERMANENT,
            ManualCalendarSyncFailureCode.PROVIDER_PERMANENT_FAILURE,
        ),
    ],
)
def test_provider_failures_are_safely_distinguished(
    provider_reason: str,
    category: WorkerExecutionFailureCategory,
    expected_code: ManualCalendarSyncFailureCode,
) -> None:
    failure = CalendarProviderFailure(
        category=category,
        message=f"unsafe provider detail {ACCESS_TOKEN}",
        retryable=category == WorkerExecutionFailureCategory.TRANSIENT,
        provider_status_code=503,
        provider_reason=provider_reason,
        metadata={"raw_error": REFRESH_TOKEN},
    )
    service, _, _, _ = manual_sync_service(
        CalendarProviderResponse(
            status_code=503,
            body={
                "external_execution_performed": True,
                "integration": "calendar",
                "adapter": "httpx_transport",
                "failed": True,
            },
            failure=failure,
        )
    )

    with pytest.raises(ManualCalendarSyncError) as caught:
        service.sync(sync_request())

    assert caught.value.code == expected_code
    assert caught.value.category == category
    safe_failure = json.dumps(caught.value.as_dict())
    assert ACCESS_TOKEN not in safe_failure
    assert REFRESH_TOKEN not in safe_failure
    assert ACCESS_TOKEN not in repr(caught.value)
    assert REFRESH_TOKEN not in repr(caught.value)


def test_missing_credentials_requires_reconnect_without_transport() -> None:
    failure = CalendarProviderFailure(
        category=WorkerExecutionFailureCategory.PERMANENT,
        message=f"unsafe missing credential detail {REFRESH_TOKEN}",
        provider_status_code=401,
        provider_reason="reconnectRequired",
    )
    credentials = RecordingCredentialsProvider(failure=failure)
    service, _, transport, container = manual_sync_service(
        successful_provider_response(),
        credentials_provider=credentials,
    )

    with pytest.raises(ManualCalendarSyncError) as caught:
        service.sync(sync_request())

    assert caught.value.code == ManualCalendarSyncFailureCode.RECONNECT_REQUIRED
    assert caught.value.external_execution_performed is False
    assert credentials.calls == [(PRINCIPAL, ACCOUNT_IDENTIFIER)]
    assert transport.calls == []
    assert container.event_repository.list_events() == []
    assert REFRESH_TOKEN not in json.dumps(caught.value.as_dict())


def test_transient_credential_refresh_failure_does_not_reach_transport() -> None:
    failure = CalendarProviderFailure(
        category=WorkerExecutionFailureCategory.TRANSIENT,
        message=f"unsafe refresh detail {REFRESH_TOKEN}",
        retryable=True,
        provider_status_code=503,
        provider_reason="credentialRefreshUnavailable",
    )
    credentials = RecordingCredentialsProvider(failure=failure)
    service, _, transport, _ = manual_sync_service(
        successful_provider_response(),
        credentials_provider=credentials,
    )

    with pytest.raises(ManualCalendarSyncError) as caught:
        service.sync(sync_request())

    assert (
        caught.value.code
        == ManualCalendarSyncFailureCode.CREDENTIAL_REFRESH_FAILURE
    )
    assert caught.value.category == WorkerExecutionFailureCategory.TRANSIENT
    assert transport.calls == []
    assert REFRESH_TOKEN not in repr(caught.value)


@pytest.mark.parametrize(
    "event",
    [
        None,
        {"event_id": EVENT_ID},
        {
            "event_id": "wrong-event",
            "title": "Pilot planning",
            "start": "2026-09-04T09:00:00Z",
            "end": "2026-09-04T09:30:00Z",
            "attendees": (),
        },
        {
            "event_id": EVENT_ID,
            "title": "Pilot planning",
            "start": "",
            "end": "2026-09-04T09:30:00Z",
            "attendees": (),
        },
    ],
)
def test_invalid_provider_event_fails_before_ingress(event: object) -> None:
    response = successful_provider_response(event=event)
    service, _, _, container = manual_sync_service(response)

    with pytest.raises(ManualCalendarSyncError) as caught:
        service.sync(sync_request())

    assert caught.value.code == ManualCalendarSyncFailureCode.MALFORMED_PROVIDER_EVENT
    assert container.event_repository.list_events() == []


def test_ingress_failure_does_not_expose_exception_or_provider_secrets() -> None:
    class FailingIngress:
        def ingest(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(f"unsafe {ACCESS_TOKEN} {REFRESH_TOKEN}")

    provider = RecordingCredentialsProvider()
    transport = RecordingTransportClient(successful_provider_response())
    service = ManualCalendarSyncService(
        provider_executor=CalendarWorkerExecutor(
            provider_composition=CalendarProviderComposition(
                credentials_provider=provider,
                transport_client=transport,
            )
        ),
        ingress_adapter=FailingIngress(),  # type: ignore[arg-type]
    )

    with pytest.raises(ManualCalendarSyncError) as caught:
        service.sync(sync_request())

    assert caught.value.code == ManualCalendarSyncFailureCode.INGRESS_WORKFLOW_FAILURE
    rendered = f"{caught.value!r} {caught.value.as_dict()}"
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
    assert caught.value.__cause__ is None


def test_duplicate_ingress_failure_is_surfaced_without_bypassing_workflow() -> None:
    class DuplicateIngress:
        def ingest(self, *args: object, **kwargs: object) -> None:
            from apps.server.src.core.events import DuplicateEventError

            raise DuplicateEventError("duplicate internal event")

    provider = RecordingCredentialsProvider()
    transport = RecordingTransportClient(successful_provider_response())
    service = ManualCalendarSyncService(
        provider_executor=CalendarWorkerExecutor(
            provider_composition=CalendarProviderComposition(
                credentials_provider=provider,
                transport_client=transport,
            )
        ),
        ingress_adapter=DuplicateIngress(),  # type: ignore[arg-type]
    )

    with pytest.raises(ManualCalendarSyncError) as caught:
        service.sync(sync_request())

    assert caught.value.code == ManualCalendarSyncFailureCode.DUPLICATE_EVENT
    assert caught.value.__cause__ is None


def test_safe_success_result_omits_provider_payload_and_secrets() -> None:
    service, _, _, _ = manual_sync_service(successful_provider_response())

    result = service.sync(sync_request())

    rendered = f"{result!r} {json.dumps(result.as_dict())}"
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
    assert "raw_google_payload" not in rendered
    assert "Authorization" not in rendered


def test_default_calendar_executor_behavior_remains_fake() -> None:
    executor = CalendarWorkerExecutor()
    action = calendar_manual_sync.Action(
        type="prepare_meeting",
        target="event-1",
        payload={"calendar_event_id": "calendar-event-1"},
        executor_role=calendar_manual_sync.CALENDAR_EXECUTOR_ROLE,
    )

    result = executor.execute(
        action,
        capability="prepare_meeting",
        account_context=ApplicationContainer.CALENDAR_ACCOUNT_CONTEXT,
    )

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["external_execution_performed"] is False
    assert result.metadata["found"] is True


def test_application_container_startup_uses_no_network_or_production_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_external_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("external operation attempted")

    monkeypatch.setattr(socket, "create_connection", fail_external_call)
    monkeypatch.setattr(socket, "socket", fail_external_call)
    monkeypatch.setattr(
        calendar_manual_sync,
        "MacOSKeychainCredentialStore",
        fail_external_call,
    )
    monkeypatch.setattr(calendar_manual_sync.httpx, "Client", fail_external_call)

    container = ApplicationContainer()

    assert container.calendar_worker_executor.provider_composition is not None


def test_cli_success_prints_only_safe_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_result = ManualCalendarSyncResult(
        google_calendar_event_id=EVENT_ID,
        universal_event_id="e20041d1-6361-4027-9fab-b024df4df599",
        acceptance_outcome="accepted",
        processing_outcome="processed",
        provider="calendar",
        principal=PRINCIPAL,
        account_identifier=ACCOUNT_IDENTIFIER,
        external_execution_performed=True,
    )
    received_requests: list[ManualCalendarSyncRequest] = []

    def fake_sync(request: ManualCalendarSyncRequest) -> ManualCalendarSyncResult:
        received_requests.append(request)
        return expected_result

    monkeypatch.setattr(calendar_manual_sync, "_run_production_sync", fake_sync)

    exit_code = calendar_manual_sync.main(
        (PRINCIPAL, ACCOUNT_IDENTIFIER, EVENT_ID)
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert received_requests == [sync_request()]
    assert json.loads(output.out) == expected_result.as_dict()
    assert output.err == ""
    assert ACCESS_TOKEN not in output.out
    assert REFRESH_TOKEN not in output.out


def test_cli_failure_has_nonzero_exit_and_no_secret_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_sync(request: ManualCalendarSyncRequest) -> ManualCalendarSyncResult:
        raise ManualCalendarSyncError(
            ManualCalendarSyncFailureCode.TRANSPORT_FAILURE,
            category=WorkerExecutionFailureCategory.TRANSIENT,
            external_execution_performed=True,
        )

    monkeypatch.setattr(calendar_manual_sync, "_run_production_sync", fail_sync)

    exit_code = calendar_manual_sync.main(
        (PRINCIPAL, ACCOUNT_IDENTIFIER, EVENT_ID)
    )

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert json.loads(output.err) == {
        "status": "failed",
        "failure_code": "transport_failure",
        "failure_category": "transient",
        "message": "Google Calendar transport is unavailable",
        "external_execution_performed": True,
    }
    assert ACCESS_TOKEN not in output.err
    assert REFRESH_TOKEN not in output.err


def test_cli_argument_count_validation_precedes_production_construction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    production_sync_calls: list[ManualCalendarSyncRequest] = []
    monkeypatch.setattr(
        calendar_manual_sync,
        "_run_production_sync",
        production_sync_calls.append,
    )

    exit_code = calendar_manual_sync.main((PRINCIPAL, ACCOUNT_IDENTIFIER))

    output = capsys.readouterr()
    assert exit_code == 2
    assert production_sync_calls == []
    assert "invalid_input" in output.err
    assert "usage:" in output.err
