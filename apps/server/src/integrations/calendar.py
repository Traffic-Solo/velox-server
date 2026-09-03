"""Google Calendar worker executor and deterministic provider-backed event read."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
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
CALENDAR_LIST_EVENTS_CAPABILITY = WorkerCapability(
    identifier="list_calendar_events",
    role=CALENDAR_EXECUTOR_ROLE,
    provider="calendar",
)
CALENDAR_WORKER_CAPABILITIES = (
    CALENDAR_PREPARE_MEETING_CAPABILITY,
    CALENDAR_PREPARE_CONTEXT_CAPABILITY,
    CALENDAR_LIST_EVENTS_CAPABILITY,
)
CALENDAR_ACCOUNT_CONTEXT = WorkerAccountContext(
    principal="velox-local-principal",
    account_identifier="calendar-local-account",
)
_CALENDAR_EVENT_READ_CAPABILITY_IDENTIFIERS = frozenset(
    {
        CALENDAR_PREPARE_MEETING_CAPABILITY.identifier,
        CALENDAR_PREPARE_CONTEXT_CAPABILITY.identifier,
    }
)
_CALENDAR_LIST_CAPABILITY_IDENTIFIERS = frozenset(
    {CALENDAR_LIST_EVENTS_CAPABILITY.identifier}
)
_CALENDAR_CAPABILITY_IDENTIFIERS = frozenset(
    capability.identifier for capability in CALENDAR_WORKER_CAPABILITIES
)
_GOOGLE_CALENDAR_API_BASE_URL = "https://www.googleapis.com"
_GOOGLE_CALENDAR_EVENTS_PATH = "/calendar/v3/calendars/primary/events"
_GOOGLE_CALENDAR_EVENT_PATH_PREFIX = f"{_GOOGLE_CALENDAR_EVENTS_PATH}/"
# VELOX owns this ceiling. Google allows far larger pages, but a small bound keeps
# one page cheap, predictable and reviewable while pagination stays unimplemented.
CALENDAR_LIST_MIN_MAX_RESULTS = 1
CALENDAR_LIST_MAX_MAX_RESULTS = 50
# Canonical RFC3339 date-time with an explicit offset. RFC 3339 also permits the
# lowercase 't'/'z' spellings; VELOX requires the uppercase canonical form so the
# accepted set is exactly what is validated, compared and forwarded to Google.
_RFC3339_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
_CALENDAR_LIST_QUERY_FIELDS = frozenset(
    {"timeMin", "timeMax", "maxResults", "singleEvents", "orderBy"}
)
# A continuation page is the identical bounded query plus the opaque pageToken.
# Google requires every other parameter to stay the same across pages, so the
# window and ordering are never renegotiated mid-pagination.
_CALENDAR_LIST_PAGED_QUERY_FIELDS = _CALENDAR_LIST_QUERY_FIELDS | {"pageToken"}
# Result metadata mirrors the provider request for observability. The opaque page
# token is replaced there rather than copied: metadata is the part most likely to
# be logged or surfaced, and the token belongs only in the request itself.
_REDACTED_QUERY_VALUE = "<redacted>"
# Token format minted by the deterministic fake only. Never a Google token.
_FAKE_CALENDAR_PAGE_TOKEN_PREFIX = "fake-calendar-page-"
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


class CalendarEventListRequestError(ValueError):
    """Safe rejection of a bounded Calendar list request, naming only the field."""

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class CalendarEventListRequest:
    """One validated bounded window for a primary-calendar event listing.

    VELOX owns the bounds rather than deferring to Google: both timestamps are
    required, canonical RFC3339 with an explicit offset, and strictly ordered,
    and max_results must fall inside the conservative VELOX range. Values outside
    the range are rejected instead of clamped, so a caller can never silently
    receive a different window or page size than the one it asked for.

    page_token is opaque provider state. An absent token means the first page; a
    present one is checked only for being a non-blank unpadded string and is
    otherwise never parsed, interpreted, normalized, logged or persisted. One
    request is always exactly one Google page: supplying a token does not start
    a loop, it selects which single page this execution reads.
    """

    time_min: str
    time_max: str
    max_results: int
    page_token: str | None = None

    def __post_init__(self) -> None:
        window_start = _validated_rfc3339_timestamp(self.time_min, "time_min")
        window_end = _validated_rfc3339_timestamp(self.time_max, "time_max")
        if window_start >= window_end:
            raise CalendarEventListRequestError(
                "calendar list request requires time_min earlier than time_max",
                field="time_min",
            )
        # bool is an int subclass; accepting it would turn True into a page size.
        if isinstance(self.max_results, bool) or not isinstance(self.max_results, int):
            raise CalendarEventListRequestError(
                "calendar list request max_results must be an integer",
                field="max_results",
            )
        if not (
            CALENDAR_LIST_MIN_MAX_RESULTS
            <= self.max_results
            <= CALENDAR_LIST_MAX_MAX_RESULTS
        ):
            raise CalendarEventListRequestError(
                "calendar list request max_results is outside the allowed range",
                field="max_results",
            )
        if self.page_token is not None and (
            not isinstance(self.page_token, str)
            or not self.page_token
            or self.page_token != self.page_token.strip()
        ):
            # The token itself is never placed in the message: a rejected token is
            # still opaque provider state and must not leak through a failure path.
            raise CalendarEventListRequestError(
                "calendar list request page_token must be a non-blank unpadded string",
                field="page_token",
            )

    def as_query(self) -> dict[str, Any]:
        """Return the exact bounded Google query parameters for this page.

        singleEvents expands recurring instances and orderBy makes the single page
        chronological, so the one page VELOX reads is a stable prefix of the window.
        A continuation adds pageToken and changes nothing else, because Google
        requires the rest of the query to be identical across pages.
        """
        query = {
            "timeMin": self.time_min,
            "timeMax": self.time_max,
            "maxResults": str(self.max_results),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if self.page_token is not None:
            query["pageToken"] = self.page_token
        return query


def _validated_rfc3339_timestamp(value: object, field_name: str) -> datetime:
    """Return the parsed timestamp, rejecting blank, padded or malformed input.

    fullmatch on the canonical pattern rejects padding and partial timestamps; the
    parse then rejects values that are well-formed but impossible, such as a day
    that does not exist in the given month.
    """
    if not isinstance(value, str) or not _RFC3339_TIMESTAMP_PATTERN.fullmatch(value):
        raise CalendarEventListRequestError(
            f"calendar list request {field_name} must be an RFC3339 timestamp",
            field=field_name,
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CalendarEventListRequestError(
            f"calendar list request {field_name} must be an RFC3339 timestamp",
            field=field_name,
        ) from None
    if parsed.tzinfo is None:
        raise CalendarEventListRequestError(
            f"calendar list request {field_name} must carry an explicit UTC offset",
            field=field_name,
        )
    return parsed


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
        if request.operation in _CALENDAR_LIST_CAPABILITY_IDENTIFIERS:
            return self._default_list_response(request)
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

    def _default_list_response(
        self,
        request: GoogleProviderRequest,
    ) -> GoogleProviderResponse:
        """Return one deterministic page, issuing a token only when one is truncated.

        This fake stands in for Google, so it is the side that mints and reads page
        tokens; VELOX never interprets them. Reporting no further pages while having
        truncated would make the fake claim a completeness it does not have.
        """
        max_results = request.query.get("maxResults")
        limit = (
            int(max_results)
            if isinstance(max_results, str) and max_results.isdigit()
            else len(self._events)
        )
        ordered = tuple(self._events.values())

        offset = 0
        page_token = request.query.get("pageToken")
        if page_token is not None:
            resolved_offset = self._page_token_offset(page_token, len(ordered))
            if resolved_offset is None:
                return self._invalid_page_token_response()
            offset = resolved_offset

        page = ordered[offset : offset + limit]
        next_offset = offset + len(page)
        events = tuple(
            {
                "event_id": event.event_id,
                "title": event.title,
                "start": event.start,
                "end": event.end,
                "attendees": event.attendees,
            }
            for event in page
        )
        return GoogleProviderResponse(
            status_code=200,
            body={
                "external_execution_performed": False,
                "integration": "calendar",
                "adapter": "fake_transport",
                "events": events,
                "next_page_token": (
                    f"{_FAKE_CALENDAR_PAGE_TOKEN_PREFIX}{next_offset}"
                    if next_offset < len(ordered)
                    else None
                ),
            },
        )

    @staticmethod
    def _page_token_offset(page_token: object, event_count: int) -> int | None:
        """Return the offset a fake-issued token refers to, or None when unusable."""
        if not isinstance(page_token, str) or not page_token.startswith(
            _FAKE_CALENDAR_PAGE_TOKEN_PREFIX
        ):
            return None
        offset = page_token.removeprefix(_FAKE_CALENDAR_PAGE_TOKEN_PREFIX)
        if not offset.isdigit() or int(offset) > event_count:
            return None
        return int(offset)

    @staticmethod
    def _invalid_page_token_response() -> GoogleProviderResponse:
        """Reject an unrecognized token the way Google rejects a bad pageToken."""
        return GoogleProviderResponse(
            status_code=400,
            body={
                "external_execution_performed": False,
                "integration": "calendar",
                "adapter": "fake_transport",
                "failed": True,
            },
            failure=GoogleProviderFailure(
                category=WorkerExecutionFailureCategory.PERMANENT,
                message="Google Calendar rejected the event request",
                provider_status_code=400,
                provider_reason="invalidRequest",
            ),
        )


class HttpxCalendarTransportClient:
    """Synchronous Google Calendar read transport with injected HTTP I/O.

    Supports the single-event read and the bounded single-page event listing. The
    listing is deliberately one page: a non-empty nextPageToken is preserved as
    opaque provider metadata and never followed here.
    """

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
        if requested_event_id is None and not self._is_bounded_list_request(request):
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
        if status_code == 404 and requested_event_id is not None:
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

        if requested_event_id is None:
            event_list = self._parse_event_list(response)
            if event_list is None:
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
                    **event_list,
                },
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

    @classmethod
    def _is_bounded_list_request(cls, request: GoogleProviderRequest) -> bool:
        """Report whether this is the bounded single-page primary-events listing.

        The bounded window is re-validated here rather than trusted from the
        caller: this is the last boundary before real HTTP, so an unbounded or
        malformed listing is rejected instead of being forwarded to Google.
        Exactly two query shapes are accepted, the bounded first page and the
        identical bounded query plus one opaque pageToken; anything else, including
        an extra parameter, is rejected rather than forwarded.
        """
        if (
            request.operation not in _CALENDAR_LIST_CAPABILITY_IDENTIFIERS
            or request.method != "GET"
            or request.body is not None
            or request.path != _GOOGLE_CALENDAR_EVENTS_PATH
        ):
            return False
        query = request.query
        if set(query) not in (
            _CALENDAR_LIST_QUERY_FIELDS,
            _CALENDAR_LIST_PAGED_QUERY_FIELDS,
        ):
            return False
        if query["singleEvents"] != "true" or query["orderBy"] != "startTime":
            return False
        max_results = query["maxResults"]
        if not isinstance(max_results, str) or not max_results.isdigit():
            return False
        # A present-but-empty pageToken is rejected rather than treated as absent:
        # silently degrading a continuation into a first page would restart a
        # caller's walk from the beginning without any signal that it happened.
        page_token = query.get("pageToken")
        if "pageToken" in query and page_token is None:
            return False
        try:
            CalendarEventListRequest(
                time_min=query["timeMin"],
                time_max=query["timeMax"],
                max_results=int(max_results),
                page_token=page_token,
            )
        except CalendarEventListRequestError:
            return False
        return True

    @staticmethod
    def _requested_event_id(request: GoogleProviderRequest) -> str | None:
        if (
            request.operation not in _CALENDAR_EVENT_READ_CAPABILITY_IDENTIFIERS
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
        raw_event = cls._parse_json_object(response)
        if raw_event is None:
            return None
        event = cls._map_event(raw_event)
        if event is None or event["event_id"] != requested_event_id:
            return None
        return event

    @classmethod
    def _parse_event_list(
        cls,
        response: httpx.Response,
    ) -> dict[str, object] | None:
        """Map exactly one Google page, preserving nextPageToken without using it."""
        raw_list = cls._parse_json_object(response)
        if raw_list is None:
            return None
        raw_items = raw_list.get("items", [])
        if not isinstance(raw_items, list):
            return None

        events: list[dict[str, object]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                return None
            event = cls._map_event(raw_item)
            if event is None:
                return None
            events.append(event)

        next_page_token = raw_list.get("nextPageToken")
        if next_page_token is not None and (
            not isinstance(next_page_token, str) or not next_page_token.strip()
        ):
            return None
        return {"events": tuple(events), "next_page_token": next_page_token}

    @staticmethod
    def _parse_json_object(response: httpx.Response) -> dict[str, object] | None:
        raw_body: object = None
        parse_failed = False
        try:
            raw_body = response.json()
        except Exception:
            parse_failed = True
        if parse_failed or not isinstance(raw_body, dict):
            return None
        return raw_body

    @classmethod
    def _map_event(cls, raw_event: dict[str, object]) -> dict[str, object] | None:
        """Map one raw Google event onto the five allowlisted VELOX fields."""
        event_id = raw_event.get("id")
        summary = raw_event.get("summary", "")
        start = cls._event_boundary(raw_event.get("start"))
        end = cls._event_boundary(raw_event.get("end"))
        if (
            not isinstance(event_id, str)
            or not event_id.strip()
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

        if resolved_capability in _CALENDAR_LIST_CAPABILITY_IDENTIFIERS:
            return self._execute_list_provider_request(
                action=action,
                capability=resolved_capability,
                account_context=account_context,
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
        result_metadata = self._result_metadata(
            response=response,
            request=request,
            capability=capability,
            account_context=account_context,
            extra={"calendar_event_id": calendar_event_id},
        )
        provider_failure_result = self._provider_failure_result(
            action, response, result_metadata
        )
        if provider_failure_result is not None:
            return provider_failure_result

        return self._capability_worker_result(
            action=action,
            response=response,
            result_metadata=result_metadata,
            capability_result=_calendar_capability_result(
                response,
                capability=capability,
                calendar_event_id=calendar_event_id,
            ),
        )

    def _execute_list_provider_request(
        self,
        action: Action,
        capability: str,
        account_context: WorkerAccountContext | None,
    ) -> WorkerExecutionResult:
        """Execute one bounded single-page primary-calendar event listing."""
        try:
            # Action payloads are untyped by construction; the request contract
            # itself is the validator, so unchecked values are handed to it as-is.
            list_request = CalendarEventListRequest(
                time_min=cast(str, action.payload.get("time_min")),
                time_max=cast(str, action.payload.get("time_max")),
                max_results=cast(int, action.payload.get("max_results")),
                # Absent means first page; the token is forwarded, never inspected.
                page_token=cast("str | None", action.payload.get("page_token")),
            )
        except CalendarEventListRequestError as error:
            reason = str(error)
            return WorkerExecutionResult(
                action=_page_token_redacted_action(action),
                status=WorkerExecutionStatus.FAILED,
                reason=reason,
                metadata={
                    "external_execution_performed": False,
                    "integration": "calendar",
                    "capability": capability,
                },
                failure=WorkerExecutionFailure(
                    category=WorkerExecutionFailureCategory.PERMANENT,
                    message=reason,
                    metadata={"field": error.field},
                ),
            )

        result_action = _page_token_redacted_action(action)

        request = CalendarProviderRequest(
            operation=capability,
            path=_GOOGLE_CALENDAR_EVENTS_PATH,
            query=list_request.as_query(),
            account_context=account_context,
        )
        response = self.provider_composition.execute(request)
        result_metadata = self._result_metadata(
            response=response,
            request=request,
            capability=capability,
            account_context=account_context,
            extra={
                "time_min": list_request.time_min,
                "time_max": list_request.time_max,
                "max_results": list_request.max_results,
                # Whether this execution continued a page, never which token.
                "page_token_supplied": list_request.page_token is not None,
            },
        )
        provider_failure_result = self._provider_failure_result(
            result_action, response, result_metadata
        )
        if provider_failure_result is not None:
            return provider_failure_result

        return self._capability_worker_result(
            action=result_action,
            response=response,
            result_metadata=result_metadata,
            capability_result=_calendar_event_list_result(response),
        )

    @staticmethod
    def _result_metadata(
        *,
        response: CalendarProviderResponse,
        request: CalendarProviderRequest,
        capability: str,
        account_context: WorkerAccountContext | None,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "external_execution_performed": (
                response.body.get("external_execution_performed") is True
            ),
            "integration": "calendar",
            "capability": capability,
            **extra,
            "account_context_used": (
                account_context.as_metadata() if account_context is not None else None
            ),
            "provider_request": _calendar_provider_request_metadata(request),
            "provider_response": _calendar_provider_response_metadata(response),
        }

    @staticmethod
    def _provider_failure_result(
        action: Action,
        response: CalendarProviderResponse,
        result_metadata: dict[str, Any],
    ) -> WorkerExecutionResult | None:
        """Return the preserved provider failure result, or None when it succeeded."""
        failure = response.failure
        if failure is None:
            return None
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

    @staticmethod
    def _capability_worker_result(
        *,
        action: Action,
        response: CalendarProviderResponse,
        result_metadata: dict[str, Any],
        capability_result: CalendarCapabilityResult,
    ) -> WorkerExecutionResult:
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

    event = _allowlisted_calendar_event(response.body.get("event"))
    if event is None or event["event_id"] != calendar_event_id:
        return CalendarCapabilityResult(
            status=WorkerExecutionStatus.FAILED,
            reason="calendar provider returned invalid event data",
        )

    metadata["event"] = event
    return CalendarCapabilityResult(
        status=WorkerExecutionStatus.SUCCEEDED,
        reason="calendar meeting context provider result",
        metadata=metadata,
    )


def _page_token_redacted_action(action: Action) -> Action:
    """Return the action with the opaque page token replaced, identity preserved.

    The result travels further than the request: it is handed back to callers and
    an action payload is what an action repository would persist. Replacing the
    token here keeps it confined to the provider request, while model_copy retains
    the same action id, created_at and every other field, so action identity and
    worker semantics are unchanged. An action without a token is returned as-is,
    so every non-paginated capability keeps the exact object it was given.
    """
    if "page_token" not in action.payload:
        return action
    return action.model_copy(
        update={
            "payload": {
                **action.payload,
                "page_token": _REDACTED_QUERY_VALUE,
            }
        }
    )


def _calendar_event_list_result(
    response: CalendarProviderResponse,
) -> CalendarCapabilityResult:
    """Return one bounded page of allowlisted events plus opaque page metadata.

    A non-empty nextPageToken means Google holds further results for this window.
    It is surfaced as opaque metadata only: this slice never requests another page,
    so a caller must treat a truncated page as truncated rather than complete.
    """
    events_value = response.body.get("events")
    if not isinstance(events_value, (list, tuple)):
        return CalendarCapabilityResult(
            status=WorkerExecutionStatus.FAILED,
            reason="calendar provider returned invalid event list data",
        )

    events: list[dict[str, Any]] = []
    for event_value in events_value:
        event = _allowlisted_calendar_event(event_value)
        if event is None:
            return CalendarCapabilityResult(
                status=WorkerExecutionStatus.FAILED,
                reason="calendar provider returned invalid event list data",
            )
        events.append(event)

    next_page_token = response.body.get("next_page_token")
    if next_page_token is not None and (
        not isinstance(next_page_token, str) or not next_page_token.strip()
    ):
        return CalendarCapabilityResult(
            status=WorkerExecutionStatus.FAILED,
            reason="calendar provider returned invalid event list data",
        )

    metadata: dict[str, Any] = {
        "events": tuple(events),
        "event_count": len(events),
        "next_page_token": next_page_token,
        "has_more_pages": next_page_token is not None,
    }
    adapter = response.body.get("adapter")
    if adapter == "fake_transport":
        metadata["adapter"] = adapter
    return CalendarCapabilityResult(
        status=WorkerExecutionStatus.SUCCEEDED,
        reason="calendar event list provider result",
        metadata=metadata,
    )


def _allowlisted_calendar_event(event: object) -> dict[str, Any] | None:
    """Return only the five allowlisted event fields, or None when malformed."""
    if not isinstance(event, dict):
        return None
    event_id = event.get("event_id")
    title = event.get("title")
    start = event.get("start")
    end = event.get("end")
    attendees = event.get("attendees")
    if (
        not isinstance(event_id, str)
        or not event_id.strip()
        or not isinstance(title, str)
        or not isinstance(start, str)
        or not isinstance(end, str)
        or not isinstance(attendees, (list, tuple))
        or not all(isinstance(attendee, str) for attendee in attendees)
    ):
        return None
    return {
        "event_id": event_id,
        "title": title,
        "start": start,
        "end": end,
        "attendees": tuple(attendees),
    }


def _calendar_provider_request_metadata(
    request: CalendarProviderRequest,
) -> dict[str, object]:
    return {
        "operation": request.operation,
        "path": request.path,
        "method": request.method,
        "body": request.body,
        "query": {
            key: (_REDACTED_QUERY_VALUE if key == "pageToken" else value)
            for key, value in request.query.items()
        },
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
