import socket

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.integrations.calendar import (
    CALENDAR_EXECUTOR_ROLE,
    CalendarCredentials,
    CalendarCredentialsProvider,
    CalendarCredentialsProviderError,
    CalendarProviderComposition,
    CalendarProviderFailure,
    CalendarProviderRequest,
    CalendarProviderResponse,
    CalendarTransportClient,
    CalendarWorkerExecutor,
    FakeCalendarCredentialsProvider,
    FakeCalendarTransportClient,
)
from apps.server.src.integrations.gmail import (
    GmailProviderComposition,
    GmailProviderRequest,
)
from apps.server.src.workers.executor import (
    WorkerExecutionFailureCategory,
    WorkerExecutionStatus,
    WorkerExecutor,
)


def block_external_socket_calls(monkeypatch) -> None:
    def fail_external_call(*args, **kwargs):
        raise AssertionError("external API call attempted")

    monkeypatch.setattr(socket, "create_connection", fail_external_call)
    monkeypatch.setattr(socket, "socket", fail_external_call)


def test_calendar_executor_role_is_vendor_neutral() -> None:
    assert CALENDAR_EXECUTOR_ROLE == ExecutorRole.CONTEXT_PREPARATION


def test_calendar_worker_executor_satisfies_worker_executor_contract() -> None:
    executor = CalendarWorkerExecutor()

    assert isinstance(executor, WorkerExecutor)


def test_calendar_worker_executor_returns_safe_placeholder_result() -> None:
    action = Action(
        type="prepare_calendar_context",
        target="calendar-placeholder",
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )
    executor = CalendarWorkerExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.SKIPPED
    assert result.reason == "calendar executor has no capability for this action type"
    assert result.metadata == {
        "external_execution_performed": False,
        "integration": "calendar",
        "placeholder": True,
        "skipped": True,
    }


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


def test_calendar_bootstrap_makes_no_external_api_calls(monkeypatch) -> None:
    block_external_socket_calls(monkeypatch)
    action = Action(
        type="prepare_calendar_context",
        target="calendar-placeholder",
        executor_role=CALENDAR_EXECUTOR_ROLE,
    )
    executor = CalendarWorkerExecutor()
    request = CalendarProviderRequest(
        operation="bootstrap",
        path="/calendar/v3/users/me/calendarList",
    )

    execution_result = executor.execute(action)
    provider_response = executor.provider_composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )

    assert execution_result.status == WorkerExecutionStatus.SKIPPED
    assert execution_result.metadata["external_execution_performed"] is False
    assert provider_response.status_code == 200
    assert provider_response.body["external_execution_performed"] is False
