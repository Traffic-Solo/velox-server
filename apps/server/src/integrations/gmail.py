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
    """Shared safe result for Gmail capability placeholders."""

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


class PlaceholderGmailSendCapability:
    """Safe Gmail send placeholder with no external API behavior."""

    def send(self, request: GmailSendRequest) -> GmailCapabilityResult:
        """Return a placeholder send result without contacting Gmail."""
        return _placeholder_capability_result("send")


class PlaceholderGmailArchiveCapability:
    """Safe Gmail archive placeholder with no external API behavior."""

    def archive(self, request: GmailArchiveRequest) -> GmailCapabilityResult:
        """Return a placeholder archive result without contacting Gmail."""
        return _placeholder_capability_result("archive", request.message_id)


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
            send=PlaceholderGmailSendCapability(),
            archive=PlaceholderGmailArchiveCapability(),
        )

    def execute(self, action: Action) -> WorkerExecutionResult:
        """Execute supported Gmail capabilities without contacting Gmail."""
        if action.type == "gmail.read" or action.payload.get("capability") == "read":
            return self._execute_read(action)

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


def _placeholder_capability_result(
    capability: str,
    message_id: str | None = None,
) -> GmailCapabilityResult:
    metadata: dict[str, Any] = {
        "external_execution_performed": False,
        "integration": "gmail",
        "capability": capability,
        "placeholder": True,
    }
    if message_id is not None:
        metadata["message_id"] = message_id

    return GmailCapabilityResult(
        status=WorkerExecutionStatus.SUCCEEDED,
        reason=f"gmail {capability} capability placeholder",
        metadata=metadata,
    )
