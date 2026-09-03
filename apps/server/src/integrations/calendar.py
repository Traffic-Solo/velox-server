"""Google Calendar worker executor and deterministic provider-backed event read."""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

import httpx
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
_GOOGLE_CALENDAR_API_BASE_URL = "https://www.googleapis.com"
_GOOGLE_CALENDAR_EVENT_PATH_PREFIX = "/calendar/v3/calendars/primary/events/"
_GOOGLE_RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
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


class HttpxCalendarTransportClient:
    """Synchronous Google Calendar events.get transport with injected HTTP I/O."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        timeout_seconds: float = 10.0,
        api_base_url: str = _GOOGLE_CALENDAR_API_BASE_URL,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or not (
            0 < timeout_seconds <= 60
        ):
            raise ValueError("calendar HTTP timeout must be between 0 and 60 seconds")
        if not isinstance(api_base_url, str) or not api_base_url.strip():
            raise ValueError("calendar API base URL must be a non-blank string")
        self._client = client
        self._timeout_seconds = float(timeout_seconds)
        self._api_base_url = api_base_url.rstrip("/")

    def execute(
        self,
        request: GoogleProviderRequest,
        credentials: GoogleCredentials,
    ) -> GoogleProviderResponse:
        """Execute the supported Calendar event read and return a safe VELOX shape."""
        requested_event_id = self._requested_event_id(request)
        if requested_event_id is None:
            return self._failure_response(
                400,
                WorkerExecutionFailureCategory.PERMANENT,
                "Calendar provider request is invalid",
                reason="invalidRequest",
            )
        if (
            not isinstance(credentials.access_token, str)
            or not credentials.access_token.strip()
        ):
            return self._failure_response(
                401,
                WorkerExecutionFailureCategory.PERMANENT,
                "Google Calendar credentials are invalid; reconnect is required",
                reason="reconnectRequired",
            )

        response: httpx.Response | None = None
        transport_failure: GoogleProviderFailure | None = None
        try:
            response = self._client.request(
                "GET",
                f"{self._api_base_url}{request.path}",
                params=request.query,
                headers={"Authorization": f"Bearer {credentials.access_token}"},
                timeout=self._timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            transport_failure = GoogleProviderFailure(
                category=WorkerExecutionFailureCategory.TRANSIENT,
                message="Google Calendar transport is temporarily unavailable",
                retryable=True,
                provider_status_code=503,
                provider_reason="transportUnavailable",
            )
        except Exception:
            transport_failure = GoogleProviderFailure(
                category=WorkerExecutionFailureCategory.INTERNAL,
                message="Google Calendar transport failed safely",
                provider_status_code=500,
                provider_reason="internalTransportError",
            )
        if transport_failure is not None:
            return GoogleProviderResponse(
                status_code=transport_failure.provider_status_code or 500,
                body=self._safe_body(failed=True, external_execution_performed=True),
                failure=transport_failure,
            )
        assert response is not None

        status_code = response.status_code
        if status_code == 404:
            return GoogleProviderResponse(
                status_code=404,
                body={
                    **self._safe_body(external_execution_performed=True),
                    "found": False,
                },
            )
        if status_code == 400:
            return self._failure_response(
                status_code,
                WorkerExecutionFailureCategory.PERMANENT,
                "Google Calendar rejected the event request",
                reason="invalidRequest",
                external_execution_performed=True,
            )
        if status_code == 401:
            return self._failure_response(
                status_code,
                WorkerExecutionFailureCategory.PERMANENT,
                "Google Calendar authorization failed; reconnect is required",
                reason="reconnectRequired",
                external_execution_performed=True,
            )
        if status_code == 403:
            provider_reason = self._safe_error_reason(response)
            rate_limited = provider_reason in _GOOGLE_RATE_LIMIT_REASONS
            return self._failure_response(
                status_code,
                (
                    WorkerExecutionFailureCategory.TRANSIENT
                    if rate_limited
                    else WorkerExecutionFailureCategory.PERMANENT
                ),
                (
                    "Google Calendar rate limit was reached"
                    if rate_limited
                    else "Google Calendar access is forbidden"
                ),
                retryable=rate_limited,
                reason=provider_reason or "forbidden",
                external_execution_performed=True,
            )
        if status_code == 429:
            return self._failure_response(
                status_code,
                WorkerExecutionFailureCategory.TRANSIENT,
                "Google Calendar rate limit was reached",
                retryable=True,
                reason="rateLimitExceeded",
                external_execution_performed=True,
            )
        if 500 <= status_code <= 599:
            return self._failure_response(
                status_code,
                WorkerExecutionFailureCategory.TRANSIENT,
                "Google Calendar is temporarily unavailable",
                retryable=True,
                reason="providerUnavailable",
                external_execution_performed=True,
            )
        if not 200 <= status_code <= 299:
            return self._failure_response(
                status_code,
                WorkerExecutionFailureCategory.PERMANENT,
                "Google Calendar request failed",
                reason="providerRequestFailed",
                external_execution_performed=True,
            )

        event = self._parse_event(response, requested_event_id)
        if event is None:
            return self._failure_response(
                status_code,
                WorkerExecutionFailureCategory.INTERNAL,
                "Google Calendar returned invalid event data",
                reason="invalidProviderResponse",
                external_execution_performed=True,
            )
        return GoogleProviderResponse(
            status_code=status_code,
            body={
                **self._safe_body(external_execution_performed=True),
                "found": True,
                "event": event,
            },
        )

    @staticmethod
    def _requested_event_id(request: GoogleProviderRequest) -> str | None:
        if (
            request.operation not in _CALENDAR_CAPABILITY_IDENTIFIERS
            or request.method != "GET"
            or request.body is not None
            or not request.path.startswith(_GOOGLE_CALENDAR_EVENT_PATH_PREFIX)
        ):
            return None
        encoded_event_id = request.path.removeprefix(
            _GOOGLE_CALENDAR_EVENT_PATH_PREFIX
        )
        if not encoded_event_id or "/" in encoded_event_id:
            return None
        event_id = unquote(encoded_event_id)
        return event_id if event_id else None

    @classmethod
    def _parse_event(
        cls,
        response: httpx.Response,
        requested_event_id: str,
    ) -> dict[str, object] | None:
        raw_event: object = None
        parse_failed = False
        try:
            raw_event = response.json()
        except Exception:
            parse_failed = True
        if parse_failed or not isinstance(raw_event, dict):
            return None

        event_id = raw_event.get("id")
        summary = raw_event.get("summary", "")
        start = cls._event_boundary(raw_event.get("start"))
        end = cls._event_boundary(raw_event.get("end"))
        if (
            event_id != requested_event_id
            or not isinstance(summary, str)
            or start is None
            or end is None
        ):
            return None

        attendees_value = raw_event.get("attendees", [])
        if not isinstance(attendees_value, list):
            return None
        attendees: list[str] = []
        for attendee in attendees_value:
            if not isinstance(attendee, dict):
                return None
            email = attendee.get("email")
            if not isinstance(email, str) or not email.strip():
                return None
            attendees.append(email)

        return {
            "event_id": event_id,
            "title": summary,
            "start": start,
            "end": end,
            "attendees": tuple(attendees),
        }

    @staticmethod
    def _event_boundary(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        date_time = value.get("dateTime")
        if isinstance(date_time, str) and date_time.strip():
            return date_time
        date = value.get("date")
        if isinstance(date, str) and date.strip():
            return date
        return None

    @staticmethod
    def _safe_error_reason(response: httpx.Response) -> str | None:
        error_body: object = None
        try:
            error_body = response.json()
        except Exception:
            return None
        if not isinstance(error_body, dict):
            return None
        error = error_body.get("error")
        if not isinstance(error, dict):
            return None
        errors = error.get("errors")
        if not isinstance(errors, list):
            return None
        for item in errors:
            if (
                isinstance(item, dict)
                and item.get("reason") in _GOOGLE_RATE_LIMIT_REASONS
            ):
                return str(item["reason"])
        return None

    @staticmethod
    def _safe_body(
        *,
        external_execution_performed: bool,
        failed: bool = False,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "external_execution_performed": external_execution_performed,
            "integration": "calendar",
            "adapter": "httpx_transport",
        }
        if failed:
            body["failed"] = True
        return body

    @classmethod
    def _failure_response(
        cls,
        status_code: int,
        category: WorkerExecutionFailureCategory,
        message: str,
        *,
        retryable: bool = False,
        reason: str,
        external_execution_performed: bool = False,
    ) -> GoogleProviderResponse:
        return GoogleProviderResponse(
            status_code=status_code,
            body=cls._safe_body(
                failed=True,
                external_execution_performed=external_execution_performed,
            ),
            failure=GoogleProviderFailure(
                category=category,
                message=message,
                retryable=retryable,
                provider_status_code=status_code,
                provider_reason=reason,
            ),
        )


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
            "external_execution_performed": (
                response.body.get("external_execution_performed") is True
            ),
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
    external_execution_performed = response.body.get("external_execution_performed")
    if isinstance(external_execution_performed, bool):
        metadata["external_execution_performed"] = external_execution_performed
    if response.body.get("integration") == "calendar":
        metadata["integration"] = "calendar"
    adapter = response.body.get("adapter")
    if isinstance(adapter, str) and adapter in {
        "fake_transport",
        "fake_provider_composition",
        "httpx_transport",
    }:
        metadata["adapter"] = adapter
    failed = response.body.get("failed")
    if isinstance(failed, bool):
        metadata["failed"] = failed
    return metadata
