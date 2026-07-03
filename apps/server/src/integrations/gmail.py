"""Gmail worker executor bootstrap and capability contracts."""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.workers.executor import (
    WorkerExecutionFailure,
    WorkerExecutionFailureCategory,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)


GMAIL_EXECUTOR_ROLE = ExecutorRole.CONTENT_SUMMARY


@dataclass(frozen=True)
class GmailCapabilityResult:
    """Shared safe result for Gmail capabilities."""

    status: WorkerExecutionStatus
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GmailReadMessage:
    """Deterministic in-memory Gmail read payload."""

    message_id: str
    subject: str
    sender: str
    body: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class GmailSentMessage:
    """Deterministic in-memory Gmail send payload."""

    sent_message_id: str
    to: tuple[str, ...]
    subject: str
    body: str
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    thread_id: str | None = None


@dataclass(frozen=True)
class GmailReadRequest:
    """Request contract for future Gmail message reads."""

    message_id: str


@dataclass(frozen=True)
class GmailSendRequest:
    """Request contract for future Gmail message sends."""

    to: tuple[str, ...]
    subject: str
    body: str
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    thread_id: str | None = None


@dataclass(frozen=True)
class GmailArchiveRequest:
    """Request contract for future Gmail message archive operations."""

    message_id: str


@dataclass(frozen=True)
class GmailCredentials:
    """Provider-facing Gmail credential material placeholder."""

    access_token: str
    token_type: str = "Bearer"
    expires_at: str | None = None


@dataclass(frozen=True)
class GmailProviderRequest:
    """Provider transport request behind the Gmail integration boundary."""

    operation: str
    path: str
    method: str = "GET"
    body: dict[str, Any] | None = None
    query: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GmailProviderFailure:
    """Provider failure mapping shape for future Gmail adapters."""

    category: WorkerExecutionFailureCategory
    message: str
    retryable: bool = False
    provider_status_code: int | None = None
    provider_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GmailProviderResponse:
    """Provider transport response behind the Gmail integration boundary."""

    status_code: int
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    failure: GmailProviderFailure | None = None


class GmailCredentialsProviderError(Exception):
    """Safe Gmail credentials provider failure with provider failure metadata."""

    def __init__(self, failure: GmailProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


@runtime_checkable
class GmailCredentialsProvider(Protocol):
    """Contract for resolving Gmail credentials behind the integration boundary."""

    def get_credentials(
        self,
        principal: str | None = None,
        account: str | None = None,
    ) -> GmailCredentials:
        """Return Gmail credentials for a future provider adapter."""


class FakeGmailCredentialsProvider:
    """Deterministic fake Gmail credentials provider with no OAuth or storage."""

    def __init__(
        self,
        failures: dict[str, GmailProviderFailure] | None = None,
    ) -> None:
        self._failures = failures or {}

    def get_credentials(
        self,
        principal: str | None = "fake-principal",
        account: str | None = "fake-account",
    ) -> GmailCredentials:
        """Return deterministic fake credentials for a normalized principal/account."""
        normalized_principal = self._normalize_identifier(principal, "principal")
        normalized_account = self._normalize_identifier(account, "account")
        failure = (
            self._failures.get(f"{normalized_principal}:{normalized_account}")
            or self._failures.get(normalized_principal)
            or self._failures.get(normalized_account)
        )
        if failure is not None:
            raise GmailCredentialsProviderError(failure)

        return GmailCredentials(
            access_token=(
                "fake-gmail-access-token:"
                f"{normalized_principal}:{normalized_account}"
            ),
            expires_at="2099-01-01T00:00:00Z",
        )

    def _normalize_identifier(self, value: str | None, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GmailCredentialsProviderError(
                GmailProviderFailure(
                    category=WorkerExecutionFailureCategory.PERMANENT,
                    message=f"gmail credentials request missing {field_name}",
                    metadata={"field": field_name},
                ),
            )
        return value.strip()


@runtime_checkable
class GmailTransportClient(Protocol):
    """Contract for Gmail provider transport behind the integration boundary."""

    def execute(
        self,
        request: GmailProviderRequest,
        credentials: GmailCredentials,
    ) -> GmailProviderResponse:
        """Execute a provider request for a future Gmail adapter."""


class FakeGmailTransportClient:
    """Deterministic Gmail transport with no HTTP or provider API behavior."""

    def __init__(
        self,
        responses: dict[str, GmailProviderResponse] | None = None,
        failures: dict[str, GmailProviderFailure] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._failures = failures or {}

    def execute(
        self,
        request: GmailProviderRequest,
        credentials: GmailCredentials,
    ) -> GmailProviderResponse:
        """Return a deterministic fake provider response."""
        key = self._request_key(request)
        failure = self._failures.get(key) or self._failures.get(request.operation)
        if failure is not None:
            return GmailProviderResponse(
                status_code=failure.provider_status_code or 500,
                body={
                    "external_execution_performed": False,
                    "integration": "gmail",
                    "adapter": "fake_transport",
                    "operation": request.operation,
                    "failed": True,
                },
                failure=failure,
            )

        response = self._responses.get(key) or self._responses.get(request.operation)
        if response is not None:
            return response

        return GmailProviderResponse(
            status_code=200,
            body={
                "external_execution_performed": False,
                "integration": "gmail",
                "adapter": "fake_transport",
                "operation": request.operation,
                "path": request.path,
                "method": request.method,
                "token_type": credentials.token_type,
            },
        )

    def _request_key(self, request: GmailProviderRequest) -> str:
        return f"{request.method}:{request.path}:{request.operation}"


@dataclass(frozen=True)
class GmailProviderComposition:
    """Compose fake Gmail provider dependencies behind the integration boundary."""

    credentials_provider: GmailCredentialsProvider = field(
        default_factory=FakeGmailCredentialsProvider,
    )
    transport_client: GmailTransportClient = field(
        default_factory=FakeGmailTransportClient,
    )

    def execute(
        self,
        request: GmailProviderRequest,
        principal: str | None = "fake-principal",
        account: str | None = "fake-account",
    ) -> GmailProviderResponse:
        """Resolve fake credentials and execute the fake Gmail transport safely."""
        try:
            credentials = self.credentials_provider.get_credentials(
                principal=principal,
                account=account,
            )
        except GmailCredentialsProviderError as error:
            failure = error.failure
            return GmailProviderResponse(
                status_code=failure.provider_status_code or 500,
                body={
                    "external_execution_performed": False,
                    "integration": "gmail",
                    "adapter": "fake_provider_composition",
                    "operation": request.operation,
                    "failed": True,
                },
                failure=failure,
            )

        return self.transport_client.execute(request, credentials)


@runtime_checkable
class GmailReadCapability(Protocol):
    """Contract for reading Gmail messages behind the executor boundary."""

    def read(self, request: GmailReadRequest) -> GmailCapabilityResult:
        """Read a Gmail message."""


@runtime_checkable
class GmailSendCapability(Protocol):
    """Contract for sending Gmail messages behind the executor boundary."""

    def send(self, request: GmailSendRequest) -> GmailCapabilityResult:
        """Send a Gmail message."""


@runtime_checkable
class GmailArchiveCapability(Protocol):
    """Contract for archiving Gmail messages behind the executor boundary."""

    def archive(self, request: GmailArchiveRequest) -> GmailCapabilityResult:
        """Archive a Gmail message."""


class InMemoryGmailReadCapability:
    """Safe deterministic Gmail read adapter with no external API behavior."""

    def __init__(
        self,
        messages: dict[str, GmailReadMessage] | None = None,
    ) -> None:
        self._messages = messages or {
            "gmail-message-1": GmailReadMessage(
                message_id="gmail-message-1",
                subject="Sprint 1 status",
                sender="sender@example.com",
                body="Deterministic in-memory Gmail read result.",
                labels=("INBOX",),
            ),
        }

    def read(self, request: GmailReadRequest) -> GmailCapabilityResult:
        """Return an in-memory read result without contacting Gmail."""
        message = self._messages.get(request.message_id)
        metadata: dict[str, Any] = {
            "external_execution_performed": False,
            "integration": "gmail",
            "capability": "read",
            "adapter": "in_memory",
            "message_id": request.message_id,
            "found": message is not None,
        }
        if message is not None:
            metadata["message"] = {
                "message_id": message.message_id,
                "subject": message.subject,
                "sender": message.sender,
                "body": message.body,
                "labels": message.labels,
            }

        return GmailCapabilityResult(
            status=WorkerExecutionStatus.SUCCEEDED,
            reason="gmail read capability in-memory result",
            metadata=metadata,
        )


class InMemoryGmailSendCapability:
    """Safe deterministic Gmail send adapter with no external API behavior."""

    def __init__(self) -> None:
        self._sent_messages: list[GmailSentMessage] = []

    def send(self, request: GmailSendRequest) -> GmailCapabilityResult:
        """Return an in-memory send result without contacting Gmail."""
        sent_message = GmailSentMessage(
            sent_message_id="gmail-fake-sent-message-1",
            to=request.to,
            subject=request.subject,
            body=request.body,
            cc=request.cc,
            bcc=request.bcc,
            thread_id=request.thread_id,
        )
        self._sent_messages.append(sent_message)

        return GmailCapabilityResult(
            status=WorkerExecutionStatus.SUCCEEDED,
            reason="gmail send capability in-memory result",
            metadata={
                "external_execution_performed": False,
                "integration": "gmail",
                "capability": "send",
                "adapter": "in_memory",
                "sent_message": {
                    "sent_message_id": sent_message.sent_message_id,
                    "to": sent_message.to,
                    "subject": sent_message.subject,
                    "body": sent_message.body,
                    "cc": sent_message.cc,
                    "bcc": sent_message.bcc,
                    "thread_id": sent_message.thread_id,
                },
            },
        )


class InMemoryGmailArchiveCapability:
    """Safe deterministic Gmail archive adapter with no external API behavior."""

    def archive(self, request: GmailArchiveRequest) -> GmailCapabilityResult:
        """Return an in-memory archive result without contacting Gmail."""
        message_id = request.message_id.strip()
        archived = message_id == "gmail-message-1"

        return GmailCapabilityResult(
            status=WorkerExecutionStatus.SUCCEEDED,
            reason="gmail archive capability in-memory result",
            metadata={
                "external_execution_performed": False,
                "integration": "gmail",
                "capability": "archive",
                "adapter": "in_memory",
                "message_id": message_id,
                "archived": archived,
                "found": archived,
            },
        )


@dataclass(frozen=True)
class GmailCapabilities:
    """Capability set exposed by the Gmail worker executor."""

    read: GmailReadCapability
    send: GmailSendCapability
    archive: GmailArchiveCapability


class GmailWorkerExecutor:
    """Safe Gmail executor bootstrap with no external API behavior."""

    def __init__(self, capabilities: GmailCapabilities | None = None) -> None:
        self.capabilities = capabilities or GmailCapabilities(
            read=InMemoryGmailReadCapability(),
            send=InMemoryGmailSendCapability(),
            archive=InMemoryGmailArchiveCapability(),
        )

    def execute(self, action: Action) -> WorkerExecutionResult:
        """Execute supported Gmail capabilities without contacting Gmail."""
        if action.type == "gmail.read" or action.payload.get("capability") == "read":
            return self._execute_read(action)
        if action.type == "gmail.send" or action.payload.get("capability") == "send":
            return self._execute_send(action)
        if (
            action.type == "gmail.archive"
            or action.payload.get("capability") == "archive"
        ):
            return self._execute_archive(action)

        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            reason="gmail executor bootstrap placeholder",
            metadata={
                "external_execution_performed": False,
                "integration": "gmail",
                "placeholder": True,
            },
        )

    def _execute_read(self, action: Action) -> WorkerExecutionResult:
        message_id = str(action.payload.get("message_id") or action.target).strip()
        if not message_id:
            return WorkerExecutionResult(
                action=action,
                status=WorkerExecutionStatus.FAILED,
                reason="gmail read request missing message_id",
                metadata={
                    "external_execution_performed": False,
                    "integration": "gmail",
                    "capability": "read",
                },
                failure=WorkerExecutionFailure(
                    category=WorkerExecutionFailureCategory.PERMANENT,
                    message="gmail read request missing message_id",
                    metadata={"field": "message_id"},
                ),
            )

        capability_result = self.capabilities.read.read(
            GmailReadRequest(message_id=message_id),
        )
        return WorkerExecutionResult(
            action=action,
            status=capability_result.status,
            reason=capability_result.reason,
            metadata=capability_result.metadata,
        )

    def _execute_archive(self, action: Action) -> WorkerExecutionResult:
        message_id = str(action.payload.get("message_id") or action.target).strip()
        if not message_id:
            return _gmail_capability_failure_result(
                action=action,
                capability="archive",
                reason="gmail archive request missing message_id",
                field="message_id",
            )

        capability_result = self.capabilities.archive.archive(
            GmailArchiveRequest(message_id=message_id),
        )
        return WorkerExecutionResult(
            action=action,
            status=capability_result.status,
            reason=capability_result.reason,
            metadata=capability_result.metadata,
        )

    def _execute_send(self, action: Action) -> WorkerExecutionResult:
        to = _normalize_recipients(action.payload.get("to"))
        subject = str(action.payload.get("subject") or "").strip()
        body = str(action.payload.get("body") or "").strip()
        cc = _normalize_recipients(action.payload.get("cc"))
        bcc = _normalize_recipients(action.payload.get("bcc"))
        thread_id_value = action.payload.get("thread_id")
        thread_id = str(thread_id_value).strip() if thread_id_value is not None else None

        missing_fields = []
        if not to:
            missing_fields.append("to")
        if not subject:
            missing_fields.append("subject")
        if not body:
            missing_fields.append("body")
        if missing_fields:
            return _gmail_capability_failure_result(
                action=action,
                capability="send",
                reason="gmail send request missing required fields",
                field=",".join(missing_fields),
            )

        capability_result = self.capabilities.send.send(
            GmailSendRequest(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                bcc=bcc,
                thread_id=thread_id,
            ),
        )
        return WorkerExecutionResult(
            action=action,
            status=capability_result.status,
            reason=capability_result.reason,
            metadata=capability_result.metadata,
        )


def _normalize_recipients(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, tuple | list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _gmail_capability_failure_result(
    action: Action,
    capability: str,
    reason: str,
    field: str,
) -> WorkerExecutionResult:
    return WorkerExecutionResult(
        action=action,
        status=WorkerExecutionStatus.FAILED,
        reason=reason,
        metadata={
            "external_execution_performed": False,
            "integration": "gmail",
            "capability": capability,
        },
        failure=WorkerExecutionFailure(
            category=WorkerExecutionFailureCategory.PERMANENT,
            message=reason,
            metadata={"field": field},
        ),
    )
