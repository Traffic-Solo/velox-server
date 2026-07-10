import socket

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.integrations.gmail import (
    FakeGmailCredentialsProvider,
    FakeGmailTransportClient,
    GmailArchiveCapability,
    GmailArchiveRequest,
    GmailCredentials,
    GmailCredentialsProvider,
    GmailCredentialsProviderError,
    GmailProviderComposition,
    GmailProviderFailure,
    GmailProviderRequest,
    GmailProviderResponse,
    GmailReadCapability,
    GmailReadRequest,
    GmailSendCapability,
    GmailSendRequest,
    GmailTransportClient,
    GmailWorkerExecutor,
)
from apps.server.src.workers.executor import (
    NoOpWorkerExecutor,
    WorkerExecutionFailure,
    WorkerExecutionFailureCategory,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerExecutor,
    WorkerExecutorRegistry,
)


class SuccessfulExecutor:
    def execute(self, action: Action) -> WorkerExecutionResult:
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            metadata={"handled_by": "test-executor"},
        )


class FailedExecutor:
    def execute(self, action: Action) -> WorkerExecutionResult:
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.FAILED,
            reason="execution failed",
            failure=WorkerExecutionFailure(
                category=WorkerExecutionFailureCategory.INTERNAL,
                message="execution failed",
            ),
        )


GMAIL_MESSAGE_ID = "gmail-message-1"


def gmail_content_action(
    action_type: str,
    target: str = GMAIL_MESSAGE_ID,
    payload: dict | None = None,
) -> Action:
    return Action(
        type=action_type,
        target=target,
        payload=payload or {},
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )


def block_external_socket_calls(monkeypatch) -> None:
    def fail_external_call(*args, **kwargs):
        raise AssertionError("external API call attempted")

    monkeypatch.setattr(socket, "create_connection", fail_external_call)
    monkeypatch.setattr(socket, "socket", fail_external_call)


def assert_succeeded_without_external_execution(result) -> None:
    """Assert no failure and no external execution (SKIPPED is valid for no-ops)."""
    assert result.status in {
        WorkerExecutionStatus.SUCCEEDED,
        WorkerExecutionStatus.SKIPPED,
    }
    assert result.metadata["external_execution_performed"] is False


def assert_gmail_in_memory_metadata(result, capability: str) -> None:
    assert result.metadata["integration"] == "gmail"
    assert result.metadata["capability"] == capability
    assert result.metadata["adapter"] == "in_memory"


def assert_gmail_failure_contract(
    result: WorkerExecutionResult,
    action: Action,
    *,
    capability: str,
    reason: str,
    field: str,
) -> None:
    assert result.action == action
    assert result.status == WorkerExecutionStatus.FAILED
    assert result.reason == reason
    assert result.metadata == {
        "external_execution_performed": False,
        "integration": "gmail",
        "capability": capability,
    }
    assert result.failure is not None
    assert result.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert result.failure.message == reason
    assert result.failure.metadata == {"field": field}


def test_worker_executor_contract_shape() -> None:
    executor = SuccessfulExecutor()

    assert isinstance(executor, WorkerExecutor)


def test_gmail_worker_executor_satisfies_worker_executor_contract() -> None:
    executor = GmailWorkerExecutor()

    assert isinstance(executor, WorkerExecutor)


def test_gmail_worker_executor_exposes_capability_contracts() -> None:
    executor = GmailWorkerExecutor()

    assert isinstance(executor.capabilities.read, GmailReadCapability)
    assert isinstance(executor.capabilities.send, GmailSendCapability)
    assert isinstance(executor.capabilities.archive, GmailArchiveCapability)


def test_gmail_provider_boundary_exposes_credentials_contract() -> None:
    provider = FakeGmailCredentialsProvider()

    credentials = provider.get_credentials(
        principal="fake-principal",
        account="fake-account",
    )

    assert isinstance(provider, GmailCredentialsProvider)
    assert credentials == GmailCredentials(
        access_token="fake-gmail-access-token:fake-principal:fake-account",
        principal="fake-principal",
        account="fake-account",
        expires_at="2099-01-01T00:00:00Z",
    )


def test_gmail_fake_credentials_provider_returns_deterministic_credentials() -> None:
    provider = FakeGmailCredentialsProvider()

    credentials = provider.get_credentials(
        principal="principal-1",
        account="account-1",
    )

    assert credentials == GmailCredentials(
        access_token="fake-gmail-access-token:principal-1:account-1",
        principal="principal-1",
        account="account-1",
        expires_at="2099-01-01T00:00:00Z",
    )


def test_gmail_fake_credentials_provider_normalizes_principal_and_account() -> None:
    provider = FakeGmailCredentialsProvider()

    credentials = provider.get_credentials(
        principal=" principal-1 ",
        account=" account-1 ",
    )

    assert credentials.access_token == (
        "fake-gmail-access-token:principal-1:account-1"
    )
    assert credentials.principal == "principal-1"
    assert credentials.account == "account-1"


def test_gmail_fake_credentials_provider_distinguishes_account_contexts() -> None:
    provider = FakeGmailCredentialsProvider()

    account_1_credentials = provider.get_credentials(
        principal="principal-1",
        account="account-1",
    )
    account_2_credentials = provider.get_credentials(
        principal="principal-1",
        account="account-2",
    )

    assert account_1_credentials.access_token != account_2_credentials.access_token
    assert account_1_credentials.account == "account-1"
    assert account_2_credentials.account == "account-2"


def test_gmail_fake_credentials_provider_handles_missing_principal_safely() -> None:
    provider = FakeGmailCredentialsProvider()

    try:
        provider.get_credentials(principal="", account="account-1")
    except GmailCredentialsProviderError as error:
        failure = error.failure
    else:
        raise AssertionError("expected GmailCredentialsProviderError")

    assert failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert failure.message == "gmail credentials request missing principal"
    assert failure.metadata == {"field": "principal"}


def test_gmail_fake_credentials_provider_handles_missing_account_safely() -> None:
    provider = FakeGmailCredentialsProvider()

    try:
        provider.get_credentials(principal="principal-1", account=None)
    except GmailCredentialsProviderError as error:
        failure = error.failure
    else:
        raise AssertionError("expected GmailCredentialsProviderError")

    assert failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert failure.message == "gmail credentials request missing account"
    assert failure.metadata == {"field": "account"}


def test_gmail_fake_credentials_provider_can_simulate_provider_failure() -> None:
    failure = GmailProviderFailure(
        category=WorkerExecutionFailureCategory.TRANSIENT,
        message="gmail credentials provider unavailable",
        retryable=True,
        provider_reason="temporarilyUnavailable",
        metadata={"principal": "principal-1", "account": "account-1"},
    )
    provider = FakeGmailCredentialsProvider(
        failures={"principal-1:account-1": failure},
    )

    try:
        provider.get_credentials(principal="principal-1", account="account-1")
    except GmailCredentialsProviderError as error:
        raised_failure = error.failure
    else:
        raise AssertionError("expected GmailCredentialsProviderError")

    assert raised_failure == failure


def test_gmail_provider_boundary_exposes_transport_contract() -> None:
    credentials = GmailCredentials(
        access_token="fake-token",
        principal="principal-1",
        account="account-1",
    )
    request = GmailProviderRequest(operation="read", path="/gmail/v1/users/me/messages/1")
    client = FakeGmailTransportClient(
        responses={
            "read": GmailProviderResponse(
                status_code=200,
                body={"operation": "read", "token_type": "Bearer"},
            ),
        },
    )

    response = client.execute(request, credentials)

    assert isinstance(client, GmailTransportClient)
    assert response == GmailProviderResponse(
        status_code=200,
        body={"operation": "read", "token_type": "Bearer"},
    )


def test_gmail_fake_transport_returns_deterministic_response() -> None:
    credentials = GmailCredentials(
        access_token="fake-token",
        principal="principal-1",
        account="account-1",
    )
    request = GmailProviderRequest(
        operation="read",
        path="/gmail/v1/users/me/messages/gmail-message-1",
    )
    client = FakeGmailTransportClient()

    response = client.execute(request, credentials)

    assert isinstance(client, GmailTransportClient)
    assert response == GmailProviderResponse(
        status_code=200,
        body={
            "external_execution_performed": False,
            "integration": "gmail",
            "adapter": "fake_transport",
            "operation": "read",
            "path": "/gmail/v1/users/me/messages/gmail-message-1",
            "method": "GET",
            "token_type": "Bearer",
            "principal": "principal-1",
            "account": "account-1",
        },
    )


def test_gmail_fake_transport_can_simulate_provider_failure() -> None:
    credentials = GmailCredentials(
        access_token="fake-token",
        principal="principal-1",
        account="account-1",
    )
    request = GmailProviderRequest(operation="read", path="/gmail/v1/users/me/messages/1")
    failure = GmailProviderFailure(
        category=WorkerExecutionFailureCategory.TRANSIENT,
        message="gmail provider unavailable",
        retryable=True,
        provider_status_code=503,
        provider_reason="backendError",
        metadata={"operation": "read"},
    )
    client = FakeGmailTransportClient(failures={"read": failure})

    response = client.execute(request, credentials)

    assert response.status_code == 503
    assert response.body == {
        "external_execution_performed": False,
        "integration": "gmail",
        "adapter": "fake_transport",
        "operation": "read",
        "failed": True,
    }
    assert response.failure == failure


def test_gmail_fake_transport_makes_no_external_api_calls(monkeypatch) -> None:
    block_external_socket_calls(monkeypatch)
    credentials = GmailCredentials(
        access_token="fake-token",
        principal="principal-1",
        account="account-1",
    )
    request = GmailProviderRequest(operation="send", path="/gmail/v1/users/me/messages/send")
    client = FakeGmailTransportClient()

    response = client.execute(request, credentials)

    assert response.status_code == 200
    assert response.body["external_execution_performed"] is False


def test_gmail_provider_failure_mapping_shape() -> None:
    failure = GmailProviderFailure(
        category=WorkerExecutionFailureCategory.TRANSIENT,
        message="gmail provider unavailable",
        retryable=True,
        provider_status_code=503,
        provider_reason="backendError",
        metadata={"operation": "read"},
    )

    assert failure.category == WorkerExecutionFailureCategory.TRANSIENT
    assert failure.message == "gmail provider unavailable"
    assert failure.retryable is True
    assert failure.provider_status_code == 503
    assert failure.provider_reason == "backendError"
    assert failure.metadata == {"operation": "read"}


def test_gmail_provider_composition_executes_fake_credentials_and_transport() -> None:
    request = GmailProviderRequest(
        operation="read",
        path="/gmail/v1/users/me/messages/gmail-message-1",
    )
    composition = GmailProviderComposition()

    response = composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )

    assert response == GmailProviderResponse(
        status_code=200,
        body={
            "external_execution_performed": False,
            "integration": "gmail",
            "adapter": "fake_transport",
            "operation": "read",
            "path": "/gmail/v1/users/me/messages/gmail-message-1",
            "method": "GET",
            "token_type": "Bearer",
            "principal": "principal-1",
            "account": "account-1",
        },
    )


def test_gmail_provider_composition_requires_account_context_safely() -> None:
    request = GmailProviderRequest(
        operation="read",
        path="/gmail/v1/users/me/messages/gmail-message-1",
    )
    composition = GmailProviderComposition()

    response = composition.execute(
        request,
        principal="principal-1",
        account=None,
    )

    assert response.status_code == 500
    assert response.body == {
        "external_execution_performed": False,
        "integration": "gmail",
        "adapter": "fake_provider_composition",
        "operation": "read",
        "failed": True,
    }
    assert response.failure is not None
    assert response.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert response.failure.message == "gmail credentials request missing account"
    assert response.failure.metadata == {"field": "account"}


def test_gmail_provider_composition_distinguishes_account_contexts() -> None:
    request = GmailProviderRequest(
        operation="read",
        path="/gmail/v1/users/me/messages/gmail-message-1",
    )
    composition = GmailProviderComposition()

    account_1_response = composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )
    account_2_response = composition.execute(
        request,
        principal="principal-1",
        account="account-2",
    )

    assert account_1_response.body["account"] == "account-1"
    assert account_2_response.body["account"] == "account-2"
    assert account_1_response.body != account_2_response.body


def test_gmail_provider_composition_returns_credentials_failure_safely() -> None:
    failure = GmailProviderFailure(
        category=WorkerExecutionFailureCategory.PERMANENT,
        message="gmail credentials unavailable",
        provider_status_code=401,
        provider_reason="invalidCredentials",
        metadata={"principal": "principal-1"},
    )
    composition = GmailProviderComposition(
        credentials_provider=FakeGmailCredentialsProvider(
            failures={"principal-1": failure},
        ),
    )
    request = GmailProviderRequest(
        operation="read",
        path="/gmail/v1/users/me/messages/gmail-message-1",
    )

    response = composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )

    assert response.status_code == 401
    assert response.body == {
        "external_execution_performed": False,
        "integration": "gmail",
        "adapter": "fake_provider_composition",
        "operation": "read",
        "failed": True,
    }
    assert response.failure == failure


def test_gmail_provider_composition_returns_transport_failure_safely() -> None:
    failure = GmailProviderFailure(
        category=WorkerExecutionFailureCategory.TRANSIENT,
        message="gmail provider unavailable",
        retryable=True,
        provider_status_code=503,
        provider_reason="backendError",
        metadata={"operation": "read"},
    )
    composition = GmailProviderComposition(
        transport_client=FakeGmailTransportClient(failures={"read": failure}),
    )
    request = GmailProviderRequest(
        operation="read",
        path="/gmail/v1/users/me/messages/gmail-message-1",
    )

    response = composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )

    assert response.status_code == 503
    assert response.body == {
        "external_execution_performed": False,
        "integration": "gmail",
        "adapter": "fake_transport",
        "operation": "read",
        "failed": True,
    }
    assert response.failure == failure


def test_gmail_provider_composition_makes_no_external_api_calls(monkeypatch) -> None:
    block_external_socket_calls(monkeypatch)
    composition = GmailProviderComposition()
    request = GmailProviderRequest(
        operation="send",
        path="/gmail/v1/users/me/messages/send",
    )

    response = composition.execute(
        request,
        principal="principal-1",
        account="account-1",
    )

    assert response.status_code == 200
    assert response.body["external_execution_performed"] is False


def test_worker_executor_successful_execution_result() -> None:
    action = Action(type="summarize_email", target="event-1")
    executor: WorkerExecutor = SuccessfulExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.reason is None
    assert result.metadata == {"handled_by": "test-executor"}


def test_worker_executor_failed_execution_result() -> None:
    action = Action(type="summarize_email", target="event-1")
    executor: WorkerExecutor = FailedExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.FAILED
    assert result.reason == "execution failed"
    assert result.failure is not None
    assert result.failure.category == WorkerExecutionFailureCategory.INTERNAL


def test_worker_execution_failure_supports_transient_category() -> None:
    failure = WorkerExecutionFailure(
        category=WorkerExecutionFailureCategory.TRANSIENT,
        message="temporary outage",
        metadata={"retry_after_seconds": 30},
    )

    assert failure.category == WorkerExecutionFailureCategory.TRANSIENT
    assert failure.message == "temporary outage"
    assert failure.metadata == {"retry_after_seconds": 30}


def test_worker_execution_failure_supports_permanent_category() -> None:
    failure = WorkerExecutionFailure(
        category=WorkerExecutionFailureCategory.PERMANENT,
        message="invalid action",
        metadata={"field": "target"},
    )

    assert failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert failure.message == "invalid action"
    assert failure.metadata == {"field": "target"}


def test_worker_execution_failure_supports_internal_category() -> None:
    failure = WorkerExecutionFailure(
        category=WorkerExecutionFailureCategory.INTERNAL,
        message="executor error",
        metadata={"component": "worker"},
    )

    assert failure.category == WorkerExecutionFailureCategory.INTERNAL
    assert failure.message == "executor error"
    assert failure.metadata == {"component": "worker"}


def test_worker_execution_success_remains_backward_compatible() -> None:
    action = Action(type="summarize_email", target="event-1")

    result = WorkerExecutionResult(
        action=action,
        status=WorkerExecutionStatus.SUCCEEDED,
    )

    assert result.action == action
    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.reason is None
    assert result.metadata == {}
    assert result.failure is None


def test_no_op_worker_executor_is_safe_default() -> None:
    action = Action(type="external.vendor.call", target="remote-system")
    executor: WorkerExecutor = NoOpWorkerExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.SKIPPED
    assert result.reason == "no registered executor handled this action"
    assert result.metadata["external_execution_performed"] is False


def test_gmail_worker_executor_returns_safe_placeholder_result() -> None:
    action = gmail_content_action("summarize_email")
    executor: WorkerExecutor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.SKIPPED
    assert result.reason == "gmail executor has no capability for this action type"
    assert result.metadata == {
        "external_execution_performed": False,
        "integration": "gmail",
        "placeholder": True,
        "skipped": True,
    }


def test_gmail_worker_executor_makes_no_external_api_calls(monkeypatch) -> None:
    block_external_socket_calls(monkeypatch)
    action = gmail_content_action("summarize_email")
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert_succeeded_without_external_execution(result)


def test_gmail_capabilities_make_no_external_api_calls(monkeypatch) -> None:
    block_external_socket_calls(monkeypatch)
    executor = GmailWorkerExecutor()

    read_result = executor.capabilities.read.read(
        GmailReadRequest(message_id=GMAIL_MESSAGE_ID),
    )
    send_result = executor.capabilities.send.send(
        GmailSendRequest(
            to=("recipient@example.com",),
            subject="Subject",
            body="Body",
        ),
    )
    archive_result = executor.capabilities.archive.archive(
        GmailArchiveRequest(message_id=GMAIL_MESSAGE_ID),
    )

    assert_succeeded_without_external_execution(read_result)
    assert_succeeded_without_external_execution(send_result)
    assert_succeeded_without_external_execution(archive_result)
    assert read_result.metadata["capability"] == "read"
    assert send_result.metadata["capability"] == "send"
    assert archive_result.metadata["capability"] == "archive"


def test_gmail_read_capability_returns_deterministic_in_memory_message() -> None:
    executor = GmailWorkerExecutor()

    result = executor.capabilities.read.read(
        GmailReadRequest(message_id=GMAIL_MESSAGE_ID),
    )

    assert_succeeded_without_external_execution(result)
    assert result.reason == "gmail read capability in-memory result"
    assert_gmail_in_memory_metadata(result, "read")
    assert result.metadata["message_id"] == GMAIL_MESSAGE_ID
    assert result.metadata["found"] is True
    assert result.metadata["message"] == {
        "message_id": GMAIL_MESSAGE_ID,
        "subject": "Sprint 1 status",
        "sender": "sender@example.com",
        "body": "Deterministic in-memory Gmail read result.",
        "labels": ("INBOX",),
    }


def test_gmail_read_capability_handles_missing_message_safely() -> None:
    executor = GmailWorkerExecutor()

    result = executor.capabilities.read.read(
        GmailReadRequest(message_id="missing-message"),
    )

    assert_succeeded_without_external_execution(result)
    assert result.metadata["found"] is False
    assert "message" not in result.metadata


def test_gmail_send_capability_returns_deterministic_in_memory_result() -> None:
    executor = GmailWorkerExecutor()

    result = executor.capabilities.send.send(
        GmailSendRequest(
            to=("recipient@example.com",),
            subject="Subject",
            body="Body",
            cc=("cc@example.com",),
            bcc=("bcc@example.com",),
            thread_id="thread-1",
        ),
    )

    assert_succeeded_without_external_execution(result)
    assert result.reason == "gmail send capability in-memory result"
    assert_gmail_in_memory_metadata(result, "send")
    assert result.metadata["sent_message"] == {
        "sent_message_id": "gmail-fake-sent-message-1",
        "to": ("recipient@example.com",),
        "subject": "Subject",
        "body": "Body",
        "cc": ("cc@example.com",),
        "bcc": ("bcc@example.com",),
        "thread_id": "thread-1",
    }


def test_gmail_worker_executor_executes_read_capability() -> None:
    action = gmail_content_action("gmail.read")
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert_succeeded_without_external_execution(result)
    assert result.metadata["adapter"] == "in_memory"
    assert result.metadata["message"]["message_id"] == GMAIL_MESSAGE_ID


def test_gmail_worker_executor_read_accepts_payload_message_id() -> None:
    action = gmail_content_action(
        "summarize_email",
        target="",
        payload={"capability": "read", "message_id": GMAIL_MESSAGE_ID},
    )
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["message_id"] == GMAIL_MESSAGE_ID
    assert result.metadata["found"] is True


def test_gmail_worker_executor_read_failure_follows_failure_contract() -> None:
    action = gmail_content_action("gmail.read", target="")
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert_gmail_failure_contract(
        result,
        action,
        capability="read",
        reason="gmail read request missing message_id",
        field="message_id",
    )


def test_gmail_worker_executor_executes_send_capability() -> None:
    action = gmail_content_action(
        "gmail.send",
        target="draft-1",
        payload={
            "to": ("recipient@example.com",),
            "subject": "Subject",
            "body": "Body",
        },
    )
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.reason == "gmail send capability in-memory result"
    assert result.metadata["external_execution_performed"] is False
    assert result.metadata["adapter"] == "in_memory"
    assert result.metadata["sent_message"]["sent_message_id"] == (
        "gmail-fake-sent-message-1"
    )
    assert result.metadata["sent_message"]["to"] == ("recipient@example.com",)


def test_gmail_worker_executor_send_accepts_payload_capability() -> None:
    action = gmail_content_action(
        "summarize_email",
        target="draft-1",
        payload={
            "capability": "send",
            "to": "recipient@example.com",
            "subject": "Subject",
            "body": "Body",
        },
    )
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["sent_message"]["to"] == ("recipient@example.com",)
    assert result.metadata["sent_message"]["subject"] == "Subject"
    assert result.metadata["sent_message"]["body"] == "Body"


def test_gmail_worker_executor_send_failure_follows_failure_contract() -> None:
    action = gmail_content_action(
        "gmail.send",
        target="draft-1",
        payload={"to": (), "subject": "", "body": ""},
    )
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert_gmail_failure_contract(
        result,
        action,
        capability="send",
        reason="gmail send request missing required fields",
        field="to,subject,body",
    )


def test_gmail_archive_capability_returns_deterministic_in_memory_result() -> None:
    executor = GmailWorkerExecutor()

    result = executor.capabilities.archive.archive(
        GmailArchiveRequest(message_id=GMAIL_MESSAGE_ID),
    )

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.reason == "gmail archive capability in-memory result"
    assert result.metadata == {
        "external_execution_performed": False,
        "integration": "gmail",
        "capability": "archive",
        "adapter": "in_memory",
        "message_id": GMAIL_MESSAGE_ID,
        "archived": True,
        "found": True,
    }


def test_gmail_archive_capability_handles_missing_message_safely() -> None:
    executor = GmailWorkerExecutor()

    result = executor.capabilities.archive.archive(
        GmailArchiveRequest(message_id="missing-message"),
    )

    assert_succeeded_without_external_execution(result)
    assert result.reason == "gmail archive capability in-memory result"
    assert result.metadata["message_id"] == "missing-message"
    assert result.metadata["archived"] is False
    assert result.metadata["found"] is False


def test_gmail_worker_executor_executes_archive_capability() -> None:
    action = gmail_content_action("gmail.archive")
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert result.action == action
    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.reason == "gmail archive capability in-memory result"
    assert result.metadata["external_execution_performed"] is False
    assert result.metadata["adapter"] == "in_memory"
    assert result.metadata["message_id"] == GMAIL_MESSAGE_ID
    assert result.metadata["archived"] is True


def test_gmail_worker_executor_archive_accepts_payload_message_id() -> None:
    action = gmail_content_action(
        "summarize_email",
        target="",
        payload={"capability": "archive", "message_id": GMAIL_MESSAGE_ID},
    )
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert result.status == WorkerExecutionStatus.SUCCEEDED
    assert result.metadata["message_id"] == GMAIL_MESSAGE_ID
    assert result.metadata["archived"] is True


def test_gmail_worker_executor_archive_failure_follows_failure_contract() -> None:
    action = gmail_content_action("gmail.archive", target="")
    executor = GmailWorkerExecutor()

    result = executor.execute(action)

    assert_gmail_failure_contract(
        result,
        action,
        capability="archive",
        reason="gmail archive request missing message_id",
        field="message_id",
    )


def test_worker_executor_registry_registers_executor() -> None:
    registry = WorkerExecutorRegistry()
    executor = SuccessfulExecutor()
    action = Action(
        type="summarize_email",
        target="event-1",
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )

    registry.register(ExecutorRole.CONTENT_SUMMARY, executor)

    assert registry.resolve(action) is executor


def test_worker_executor_registry_registers_executor_by_explicit_role() -> None:
    registry = WorkerExecutorRegistry()
    executor = SuccessfulExecutor()
    action = Action(
        type="summarize_email",
        target="event-1",
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )

    registry.register_role(ExecutorRole.CONTENT_SUMMARY, executor)

    assert registry.resolve(action) is executor
    assert registry.registered_roles() == (ExecutorRole.CONTENT_SUMMARY.value,)


def test_worker_executor_registry_exposes_successful_role_resolution() -> None:
    registry = WorkerExecutorRegistry()
    executor = SuccessfulExecutor()
    action = Action(
        type="summarize_email",
        target="event-1",
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )

    registry.register_role(ExecutorRole.CONTENT_SUMMARY, executor)

    resolution = registry.resolve_with_registration(action)
    assert resolution.executor is executor
    assert resolution.requested_role == ExecutorRole.CONTENT_SUMMARY.value
    assert resolution.registered is True


def test_worker_executor_registry_exposes_fallback_role_resolution() -> None:
    fallback_executor = NoOpWorkerExecutor()
    registry = WorkerExecutorRegistry(fallback_executor=fallback_executor)
    action = Action(
        type="generic_action",
        target="event-1",
        executor_role=ExecutorRole.CONTENT_REVIEW,
    )

    resolution = registry.resolve_with_registration(action)
    assert resolution.executor is fallback_executor
    assert resolution.requested_role == ExecutorRole.CONTENT_REVIEW.value
    assert resolution.registered is False


def test_worker_executor_registry_resolves_string_executor_role() -> None:
    registry = WorkerExecutorRegistry()
    executor = SuccessfulExecutor()
    action = Action(
        type="generic_action",
        target="event-1",
        executor_role="summarizer",
    )

    registry.register("summarizer", executor)

    assert registry.resolve(action) is executor


def test_worker_executor_registry_falls_back_to_no_op_executor() -> None:
    fallback_executor = NoOpWorkerExecutor()
    registry = WorkerExecutorRegistry(fallback_executor=fallback_executor)
    action = Action(type="missing_executor", target="event-1")

    assert registry.resolve(action) is fallback_executor


def test_worker_executor_registry_falls_back_for_unknown_executor_role() -> None:
    fallback_executor = NoOpWorkerExecutor()
    registry = WorkerExecutorRegistry(fallback_executor=fallback_executor)
    action = Action(
        type="generic_action",
        target="event-1",
        executor_role="unknown_role",
    )

    assert registry.resolve(action) is fallback_executor


def test_worker_executor_registry_does_not_introduce_vendor_specific_behavior() -> None:
    registry = WorkerExecutorRegistry()
    action = Action(type="external.vendor.call", target="remote-system")

    executor = registry.resolve(action)
    result = executor.execute(action)

    assert result.status == WorkerExecutionStatus.SKIPPED
    assert result.metadata["external_execution_performed"] is False
