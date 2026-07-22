"""Gmail worker executor bootstrap and capability contracts."""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.integrations.google_provider import (
    FakeGoogleCredentialsProvider,
    FakeGoogleTransportClient,
    GoogleCredentials,
    GoogleCredentialsProvider,
    GoogleCredentialsProviderError,
    GoogleProviderComposition,
    GoogleProviderFailure,
    GoogleProviderRequest,
    GoogleProviderResponse,
    GoogleTransportClient,
)
from apps.server.src.workers.executor import (
    WorkerAccountContext,
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


# Provider boundary: shared Google primitives, specialized for Gmail.
GmailCredentials = GoogleCredentials
GmailProviderRequest = GoogleProviderRequest
GmailProviderFailure = GoogleProviderFailure
GmailProviderResponse = GoogleProviderResponse
GmailCredentialsProviderError = GoogleCredentialsProviderError
GmailCredentialsProvider = GoogleCredentialsProvider
GmailTransportClient = GoogleTransportClient


class FakeGmailCredentialsProvider(FakeGoogleCredentialsProvider):
    """Deterministic fake Gmail credentials provider with no OAuth or storage."""

    def __init__(
        self,
        failures: dict[str, GoogleProviderFailure] | None = None,
    ) -> None:
        super().__init__(service="gmail", failures=failures)


class FakeGmailTransportClient(FakeGoogleTransportClient):
    """Deterministic Gmail transport with no HTTP or provider API behavior."""

    def __init__(
        self,
        responses: dict[str, GoogleProviderResponse] | None = None,
        failures: dict[str, GoogleProviderFailure] | None = None,
    ) -> None:
        super().__init__(service="gmail", responses=responses, failures=failures)


class GmailProviderComposition(GoogleProviderComposition):
    """Compose fake Gmail provider dependencies behind the integration boundary."""

    def __init__(
        self,
        credentials_provider: GoogleCredentialsProvider | None = None,
        transport_client: GoogleTransportClient | None = None,
    ) -> None:
        super().__init__(
            service="gmail",
            credentials_provider=credentials_provider or FakeGmailCredentialsProvider(),
            transport_client=transport_client or FakeGmailTransportClient(),
        )


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

    def __init__(
        self,
        capabilities: GmailCapabilities | None = None,
        provider_composition: GmailProviderComposition | None = None,
    ) -> None:
        self.capabilities = capabilities or GmailCapabilities(
            read=InMemoryGmailReadCapability(),
            send=InMemoryGmailSendCapability(),
            archive=InMemoryGmailArchiveCapability(),
        )
        self.provider_composition = provider_composition or GmailProviderComposition()

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
            status=WorkerExecutionStatus.SKIPPED,
            reason="gmail executor has no capability for this action type",
            metadata={
                "external_execution_performed": False,
                "integration": "gmail",
                "placeholder": True,
                "skipped": True,
            },
        )

    def execute_with_account_context(
        self,
        action: Action,
        account_context: WorkerAccountContext,
    ) -> WorkerExecutionResult:
        """Construct and execute a fake provider request with routed context."""
        capability_result = self.execute(action)
        if capability_result.status != WorkerExecutionStatus.SUCCEEDED:
            return capability_result

        request = self._provider_request(action, account_context)
        response = self.provider_composition.execute(request)
        if response.failure is not None:
            return _gmail_provider_failure_result(action, request, response)

        return WorkerExecutionResult(
            action=action,
            status=capability_result.status,
            reason=capability_result.reason,
            metadata={
                **capability_result.metadata,
                "account_context_used": account_context.as_metadata(),
                "provider_request": _gmail_provider_request_metadata(request),
                "provider_response": dict(response.body),
            },
        )

    def _provider_request(
        self,
        action: Action,
        account_context: WorkerAccountContext,
    ) -> GmailProviderRequest:
        if action.type == "gmail.read" or action.payload.get("capability") == "read":
            message_id = str(action.payload.get("message_id") or "").strip()
            return GmailProviderRequest(
                operation="read",
                path=f"/gmail/v1/users/me/messages/{message_id}",
                account_context=account_context,
            )
        if action.type == "gmail.send" or action.payload.get("capability") == "send":
            return GmailProviderRequest(
                operation="send",
                path="/gmail/v1/users/me/messages/send",
                method="POST",
                body={
                    "to": _normalize_recipients(action.payload.get("to")),
                    "subject": str(action.payload.get("subject") or "").strip(),
                    "body": str(action.payload.get("body") or "").strip(),
                    "cc": _normalize_recipients(action.payload.get("cc")),
                    "bcc": _normalize_recipients(action.payload.get("bcc")),
                    "thread_id": action.payload.get("thread_id"),
                },
                account_context=account_context,
            )

        message_id = str(action.payload.get("message_id") or "").strip()
        return GmailProviderRequest(
            operation="archive",
            path=f"/gmail/v1/users/me/messages/{message_id}/modify",
            method="POST",
            body={"remove_label_ids": ("INBOX",)},
            account_context=account_context,
        )

    def _execute_read(self, action: Action) -> WorkerExecutionResult:
        # message_id must be explicit: action.target holds an event id, not a
        # Gmail message id, so falling back to it would query the wrong thing.
        message_id = str(action.payload.get("message_id") or "").strip()
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
        message_id = str(action.payload.get("message_id") or "").strip()
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


def _gmail_provider_request_metadata(
    request: GmailProviderRequest,
) -> dict[str, Any]:
    return {
        "operation": request.operation,
        "path": request.path,
        "method": request.method,
        "body": request.body,
        "query": dict(request.query),
        "account_context": (
            request.account_context.as_metadata()
            if request.account_context is not None
            else None
        ),
    }


def _gmail_provider_failure_result(
    action: Action,
    request: GmailProviderRequest,
    response: GmailProviderResponse,
) -> WorkerExecutionResult:
    failure = response.failure
    if failure is None:
        raise ValueError("gmail provider failure response must include failure details")

    return WorkerExecutionResult(
        action=action,
        status=WorkerExecutionStatus.FAILED,
        reason=failure.message,
        metadata={
            "external_execution_performed": False,
            "integration": "gmail",
            "capability": request.operation,
            "account_context_used": (
                request.account_context.as_metadata()
                if request.account_context is not None
                else None
            ),
            "provider_request": _gmail_provider_request_metadata(request),
            "provider_response": dict(response.body),
        },
        failure=WorkerExecutionFailure(
            category=failure.category,
            message=failure.message,
            metadata={
                **failure.metadata,
                "provider_status_code": response.status_code,
                "provider_reason": failure.provider_reason,
                "retryable": failure.retryable,
            },
        ),
    )
