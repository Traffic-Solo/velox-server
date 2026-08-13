"""Google Calendar worker executor and deterministic provider-backed event read."""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

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
    ProviderManifest,
    WorkerAccountContext,
    WorkerCapability,
    WorkerExecutionFailure,
    WorkerExecutionFailureCategory,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)

CALENDAR_EXECUTOR_ROLE = ExecutorRole.CONTEXT_PREPARATION
CALENDAR_PREPARE_MEETING_CAPABILITY = WorkerCapability(
    identifier="prepare_meeting",
    role=CALENDAR_EXECUTOR_ROLE,
    provider="calendar",
)
CALENDAR_PREPARE_CONTEXT_CAPABILITY = WorkerCapability(
    identifier="prepare_calendar_context",
    role=CALENDAR_EXECUTOR_ROLE,
    provider="calendar",
)
CALENDAR_WORKER_CAPABILITIES = (
    CALENDAR_PREPARE_MEETING_CAPABILITY,
    CALENDAR_PREPARE_CONTEXT_CAPABILITY,
)
CALENDAR_ACCOUNT_CONTEXT = WorkerAccountContext(
    principal="velox-local-principal",
    account_identifier="calendar-local-account",
)
_CALENDAR_CAPABILITY_IDENTIFIERS = frozenset(
    capability.identifier for capability in CALENDAR_WORKER_CAPABILITIES
)
# Provider boundary: shared Google primitives, specialized for Calendar.
CalendarCredentials = GoogleCredentials
CalendarProviderRequest = GoogleProviderRequest
CalendarProviderFailure = GoogleProviderFailure
CalendarProviderResponse = GoogleProviderResponse
CalendarCredentialsProviderError = GoogleCredentialsProviderError
CalendarCredentialsProvider = GoogleCredentialsProvider
CalendarTransportClient = GoogleTransportClient


@dataclass(frozen=True)
class CalendarEvent:
    """Deterministic in-memory Calendar event."""

    event_id: str
    title: str
    start: str
    end: str
    attendees: tuple[str, ...] = ()


@dataclass(frozen=True)
class CalendarMeetingContextRequest:
    """Request for Calendar meeting context."""

    calendar_event_id: str


@dataclass(frozen=True)
class CalendarCapabilityResult:
    """Safe result returned by Calendar capabilities."""

    status: WorkerExecutionStatus
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class FakeCalendarCredentialsProvider(FakeGoogleCredentialsProvider):
    """Deterministic fake Calendar credentials provider with no OAuth or storage."""

    def __init__(
        self,
        failures: dict[str, GoogleProviderFailure] | None = None,
    ) -> None:
        super().__init__(service="calendar", failures=failures)


class FakeCalendarTransportClient(FakeGoogleTransportClient):
    """Deterministic provider-backed Calendar event transport."""

    def __init__(
        self,
        responses: dict[str, GoogleProviderResponse] | None = None,
        failures: dict[str, GoogleProviderFailure] | None = None,
        events: dict[str, CalendarEvent] | None = None,
    ) -> None:
        super().__init__(service="calendar", responses=responses, failures=failures)
        self._events = events if events is not None else {
            "calendar-event-1": CalendarEvent(
                event_id="calendar-event-1",
                title="Sprint 1 planning",
                start="2026-07-27T09:00:00Z",
                end="2026-07-27T09:30:00Z",
                attendees=("owner@example.com", "team@example.com"),
            ),
        }

    def _default_response(
        self,
        request: GoogleProviderRequest,
        credentials: GoogleCredentials,
    ) -> GoogleProviderResponse:
        if request.operation not in _CALENDAR_CAPABILITY_IDENTIFIERS:
            return super()._default_response(request, credentials)

        calendar_event_id = unquote(request.path.rsplit("/", maxsplit=1)[-1])
        event = self._events.get(calendar_event_id)
        body: dict[str, Any] = {
            "external_execution_performed": False,
            "integration": "calendar",
            "adapter": "fake_transport",
            "found": event is not None,
        }
        if event is not None:
            body["event"] = {
                "event_id": event.event_id,
                "title": event.title,
                "start": event.start,
                "end": event.end,
                "attendees": event.attendees,
            }
        return GoogleProviderResponse(status_code=200, body=body)


class CalendarProviderComposition(GoogleProviderComposition):
    """Compose fake Calendar provider dependencies behind the boundary."""

    def __init__(
        self,
        credentials_provider: GoogleCredentialsProvider | None = None,
        transport_client: GoogleTransportClient | None = None,
    ) -> None:
        super().__init__(
            service="calendar",
            credentials_provider=credentials_provider
            or FakeCalendarCredentialsProvider(),
            transport_client=transport_client or FakeCalendarTransportClient(),
        )


class CalendarWorkerExecutor:
    """Safe Calendar executor bootstrap with no external API behavior."""

    def __init__(
        self,
        provider_composition: CalendarProviderComposition | None = None,
    ) -> None:
        self.provider_composition = (
            provider_composition or CalendarProviderComposition()
        )
        self.provider_manifest = ProviderManifest(
            capabilities=CALENDAR_WORKER_CAPABILITIES,
            executor=self,
            account_context=CALENDAR_ACCOUNT_CONTEXT,
        )

    @property
    def worker_capabilities(self) -> tuple[WorkerCapability, ...]:
        """Return manifest capabilities for provider interface compatibility."""
        return self.provider_manifest.capabilities

    def execute(
        self,
        action: Action,
        *,
        capability: str | None = None,
        account_context: WorkerAccountContext | None = None,
    ) -> WorkerExecutionResult:
        """Execute meeting-context lookup without contacting Google Calendar."""
        resolved_capability = capability or action.type
        if resolved_capability not in _CALENDAR_CAPABILITY_IDENTIFIERS:
            return WorkerExecutionResult(
                action=action,
                status=WorkerExecutionStatus.SKIPPED,
                reason="calendar executor has no capability for this action type",
                metadata={
                    "external_execution_performed": False,
                    "integration": "calendar",
                    "placeholder": True,
                    "skipped": True,
                },
            )

        calendar_event_id_value = action.payload.get("calendar_event_id")
        if (
            not isinstance(calendar_event_id_value, str)
            or not calendar_event_id_value.strip()
        ):
            reason = "calendar meeting context request invalid calendar_event_id"
            return WorkerExecutionResult(
                action=action,
                status=WorkerExecutionStatus.FAILED,
                reason=reason,
                metadata={
                    "external_execution_performed": False,
                    "integration": "calendar",
                    "capability": resolved_capability,
                },
                failure=WorkerExecutionFailure(
                    category=WorkerExecutionFailureCategory.PERMANENT,
                    message=reason,
                    metadata={"field": "calendar_event_id"},
                ),
            )

        calendar_event_id = calendar_event_id_value.strip()
        return self._execute_provider_request(
            action=action,
            capability=resolved_capability,
            calendar_event_id=calendar_event_id,
            account_context=account_context,
        )

    def _execute_provider_request(
        self,
        action: Action,
        capability: str,
        calendar_event_id: str,
        account_context: WorkerAccountContext | None,
    ) -> WorkerExecutionResult:
        request = CalendarProviderRequest(
            operation=capability,
            path=(
                "/calendar/v3/calendars/primary/events/"
                f"{quote(calendar_event_id, safe='')}"
            ),
            account_context=account_context,
        )
        response = self.provider_composition.execute(request)
        result_metadata: dict[str, Any] = {
            "external_execution_performed": False,
            "integration": "calendar",
            "capability": capability,
            "calendar_event_id": calendar_event_id,
            "account_context_used": (
                account_context.as_metadata() if account_context is not None else None
            ),
            "provider_request": _calendar_provider_request_metadata(request),
            "provider_response": _calendar_provider_response_metadata(response),
        }
        if response.failure is not None:
            failure = response.failure
            return WorkerExecutionResult(
                action=action,
                status=WorkerExecutionStatus.FAILED,
                reason=failure.message,
                metadata=result_metadata,
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

        capability_result = _calendar_capability_result(
            response,
            capability=capability,
            calendar_event_id=calendar_event_id,
        )
        if capability_result.status != WorkerExecutionStatus.SUCCEEDED:
            return WorkerExecutionResult(
                action=action,
                status=capability_result.status,
                reason=capability_result.reason,
                metadata=result_metadata,
                failure=WorkerExecutionFailure(
                    category=WorkerExecutionFailureCategory.INTERNAL,
                    message=capability_result.reason,
                    metadata={"provider_status_code": response.status_code},
                ),
            )

        return WorkerExecutionResult(
            action=action,
            status=capability_result.status,
            reason=capability_result.reason,
            metadata={
                **result_metadata,
                **capability_result.metadata,
            },
        )


def _calendar_capability_result(
    response: CalendarProviderResponse,
    *,
    capability: str,
    calendar_event_id: str,
) -> CalendarCapabilityResult:
    found = response.body.get("found")
    if not isinstance(found, bool):
        return CalendarCapabilityResult(
            status=WorkerExecutionStatus.FAILED,
            reason="calendar provider returned invalid event data",
        )

    metadata: dict[str, Any] = {"found": found}
    adapter = response.body.get("adapter")
    if adapter == "fake_transport":
        metadata["adapter"] = adapter
    if not found:
        return CalendarCapabilityResult(
            status=WorkerExecutionStatus.SUCCEEDED,
            reason="calendar meeting context provider result",
            metadata=metadata,
        )

    event = response.body.get("event")
    if not isinstance(event, dict):
        return CalendarCapabilityResult(
            status=WorkerExecutionStatus.FAILED,
            reason="calendar provider returned invalid event data",
        )
    event_id = event.get("event_id")
    title = event.get("title")
    start = event.get("start")
    end = event.get("end")
    attendees = event.get("attendees")
    if (
        event_id != calendar_event_id
        or not isinstance(title, str)
        or not isinstance(start, str)
        or not isinstance(end, str)
        or not isinstance(attendees, (list, tuple))
        or not all(isinstance(attendee, str) for attendee in attendees)
    ):
        return CalendarCapabilityResult(
            status=WorkerExecutionStatus.FAILED,
            reason="calendar provider returned invalid event data",
        )

    metadata["event"] = {
        "event_id": event_id,
        "title": title,
        "start": start,
        "end": end,
        "attendees": tuple(attendees),
    }
    return CalendarCapabilityResult(
        status=WorkerExecutionStatus.SUCCEEDED,
        reason="calendar meeting context provider result",
        metadata=metadata,
    )


def _calendar_provider_request_metadata(
    request: CalendarProviderRequest,
) -> dict[str, object]:
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


def _calendar_provider_response_metadata(
    response: CalendarProviderResponse,
) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if response.body.get("external_execution_performed") is False:
        metadata["external_execution_performed"] = False
    if response.body.get("integration") == "calendar":
        metadata["integration"] = "calendar"
    adapter = response.body.get("adapter")
    if isinstance(adapter, str) and adapter in {
        "fake_transport",
        "fake_provider_composition",
    }:
        metadata["adapter"] = adapter
    failed = response.body.get("failed")
    if isinstance(failed, bool):
        metadata["failed"] = failed
    return metadata
