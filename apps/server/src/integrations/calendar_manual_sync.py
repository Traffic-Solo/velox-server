"""Explicit local synchronization of one Google Calendar event."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn

import httpx
from apps.server.src.core.actions import Action
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.core.events import DuplicateEventError, IntegrationRouteContext
from apps.server.src.integrations.calendar import (
    CALENDAR_EXECUTOR_ROLE,
    CalendarProviderComposition,
    CalendarWorkerExecutor,
    HttpxCalendarTransportClient,
)
from apps.server.src.integrations.calendar_ingress import CalendarIngressAdapter
from apps.server.src.integrations.google_oauth import StoredGoogleCredentialsProvider
from apps.server.src.integrations.keyring_credentials import (
    MacOSKeychainCredentialStore,
)
from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerExecutionFailureCategory,
    WorkerExecutionStatus,
)

_CALENDAR_PROVIDER = "calendar"
_CALENDAR_READ_CAPABILITY = "prepare_meeting"
_DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
_USAGE = (
    "usage: python -m apps.server.src.integrations.calendar_manual_sync "
    "PRINCIPAL ACCOUNT_IDENTIFIER EVENT_ID"
)


class ManualCalendarSyncFailureCode(StrEnum):
    """Safe failure codes for the explicit manual sync boundary."""

    INVALID_INPUT = "invalid_input"
    RECONNECT_REQUIRED = "reconnect_required"
    CREDENTIAL_REFRESH_FAILURE = "credential_refresh_failure"
    TRANSPORT_FAILURE = "transport_failure"
    EVENT_NOT_FOUND = "event_not_found"
    PROVIDER_PERMANENT_FAILURE = "provider_permanent_failure"
    PROVIDER_TRANSIENT_FAILURE = "provider_transient_failure"
    PROVIDER_INTERNAL_FAILURE = "provider_internal_failure"
    MALFORMED_PROVIDER_EVENT = "malformed_provider_event"
    DUPLICATE_EVENT = "duplicate_event"
    INGRESS_WORKFLOW_FAILURE = "ingress_workflow_failure"


_FAILURE_MESSAGES = {
    ManualCalendarSyncFailureCode.INVALID_INPUT: "manual sync input is invalid",
    ManualCalendarSyncFailureCode.RECONNECT_REQUIRED: (
        "Google credentials are unavailable or invalid; reconnect is required"
    ),
    ManualCalendarSyncFailureCode.CREDENTIAL_REFRESH_FAILURE: (
        "Google credential refresh is temporarily unavailable"
    ),
    ManualCalendarSyncFailureCode.TRANSPORT_FAILURE: (
        "Google Calendar transport is unavailable"
    ),
    ManualCalendarSyncFailureCode.EVENT_NOT_FOUND: (
        "Google Calendar event was not found"
    ),
    ManualCalendarSyncFailureCode.PROVIDER_PERMANENT_FAILURE: (
        "Google Calendar rejected the manual sync request"
    ),
    ManualCalendarSyncFailureCode.PROVIDER_TRANSIENT_FAILURE: (
        "Google Calendar is temporarily unavailable"
    ),
    ManualCalendarSyncFailureCode.PROVIDER_INTERNAL_FAILURE: (
        "Google Calendar provider execution failed safely"
    ),
    ManualCalendarSyncFailureCode.MALFORMED_PROVIDER_EVENT: (
        "Google Calendar returned invalid event data"
    ),
    ManualCalendarSyncFailureCode.DUPLICATE_EVENT: (
        "the Calendar event conflicts with an existing VELOX event"
    ),
    ManualCalendarSyncFailureCode.INGRESS_WORKFLOW_FAILURE: (
        "Calendar ingress or workflow processing failed"
    ),
}


class ManualCalendarSyncError(Exception):
    """Safe manual-sync failure with no retained third-party exception."""

    def __init__(
        self,
        code: ManualCalendarSyncFailureCode,
        *,
        category: WorkerExecutionFailureCategory | None = None,
        field: str | None = None,
        external_execution_performed: bool = False,
    ) -> None:
        self.code = code
        self.category = category
        self.field = field
        self.external_execution_performed = external_execution_performed
        super().__init__(_FAILURE_MESSAGES[code])

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible failure containing only safe fields."""
        failure: dict[str, object] = {
            "status": "failed",
            "failure_code": self.code.value,
            "message": str(self),
            "external_execution_performed": self.external_execution_performed,
        }
        if self.category is not None:
            failure["failure_category"] = self.category.value
        if self.field is not None:
            failure["field"] = self.field
        return failure


@dataclass(frozen=True)
class ManualCalendarSyncRequest:
    """Validated explicit routing and event identity for one manual sync."""

    principal: str
    account_identifier: str
    event_id: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("principal", self.principal),
            ("account_identifier", self.account_identifier),
            ("event_id", self.event_id),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ManualCalendarSyncError(
                    ManualCalendarSyncFailureCode.INVALID_INPUT,
                    field=field_name,
                )


@dataclass(frozen=True)
class ManualCalendarSyncResult:
    """Safe VELOX result for one successfully synchronized Calendar event."""

    google_calendar_event_id: str
    universal_event_id: str
    acceptance_outcome: str
    processing_outcome: str
    provider: str
    principal: str
    account_identifier: str
    external_execution_performed: bool

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible result containing only safe fields."""
        return {
            "status": "succeeded",
            "google_calendar_event_id": self.google_calendar_event_id,
            "universal_event_id": self.universal_event_id,
            "acceptance_outcome": self.acceptance_outcome,
            "processing_outcome": self.processing_outcome,
            "provider": self.provider,
            "principal": self.principal,
            "account_identifier": self.account_identifier,
            "external_execution_performed": self.external_execution_performed,
        }


class ManualCalendarSyncService:
    """Synchronize one explicit provider event through Calendar ingress."""

    def __init__(
        self,
        *,
        provider_executor: CalendarWorkerExecutor,
        ingress_adapter: CalendarIngressAdapter,
    ) -> None:
        self._provider_executor = provider_executor
        self._ingress_adapter = ingress_adapter

    def sync(
        self,
        request: ManualCalendarSyncRequest,
    ) -> ManualCalendarSyncResult:
        """Read, allowlist, normalize, accept, and process one Calendar event."""
        account_context = WorkerAccountContext(
            principal=request.principal,
            account_identifier=request.account_identifier,
        )
        provider_action = Action(
            type=_CALENDAR_READ_CAPABILITY,
            target="manual-calendar-sync",
            payload={"calendar_event_id": request.event_id},
            executor_role=CALENDAR_EXECUTOR_ROLE,
        )
        provider_result = self._provider_executor.execute(
            provider_action,
            capability=_CALENDAR_READ_CAPABILITY,
            account_context=account_context,
        )
        external_execution_performed = (
            provider_result.metadata.get("external_execution_performed") is True
        )
        if provider_result.status != WorkerExecutionStatus.SUCCEEDED:
            raise self._provider_error(
                provider_result.reason,
                provider_result.failure.category
                if provider_result.failure is not None
                else None,
                provider_result.failure.metadata
                if provider_result.failure is not None
                else {},
                external_execution_performed=external_execution_performed,
            )
        if provider_result.metadata.get("found") is not True:
            raise ManualCalendarSyncError(
                ManualCalendarSyncFailureCode.EVENT_NOT_FOUND,
                external_execution_performed=external_execution_performed,
            )

        raw_event = self._allowlisted_raw_event(
            provider_result.metadata.get("event"),
            expected_event_id=request.event_id,
            external_execution_performed=external_execution_performed,
        )
        integration_route = IntegrationRouteContext(
            provider=_CALENDAR_PROVIDER,
            principal=request.principal,
            account_identifier=request.account_identifier,
        )
        try:
            ingress = self._ingress_adapter.ingest(
                raw_event,
                integration_route=integration_route,
            )
        except DuplicateEventError:
            raise ManualCalendarSyncError(
                ManualCalendarSyncFailureCode.DUPLICATE_EVENT,
                external_execution_performed=external_execution_performed,
            ) from None
        except Exception:
            raise ManualCalendarSyncError(
                ManualCalendarSyncFailureCode.INGRESS_WORKFLOW_FAILURE,
                external_execution_performed=external_execution_performed,
            ) from None

        return ManualCalendarSyncResult(
            google_calendar_event_id=request.event_id,
            universal_event_id=str(ingress.event.id),
            acceptance_outcome="accepted",
            processing_outcome="processed",
            provider=_CALENDAR_PROVIDER,
            principal=request.principal,
            account_identifier=request.account_identifier,
            external_execution_performed=external_execution_performed,
        )

    @staticmethod
    def _allowlisted_raw_event(
        event: object,
        *,
        expected_event_id: str,
        external_execution_performed: bool,
    ) -> dict[str, object]:
        if not isinstance(event, Mapping):
            ManualCalendarSyncService._raise_malformed_provider_event(
                external_execution_performed
            )

        event_id = event.get("event_id")
        title = event.get("title")
        start = event.get("start")
        end = event.get("end")
        attendees = event.get("attendees")
        if (
            event_id != expected_event_id
            or not isinstance(title, str)
            or not isinstance(start, str)
            or not start.strip()
            or not isinstance(end, str)
            or not end.strip()
            or not isinstance(attendees, (list, tuple))
            or not all(
                isinstance(attendee, str) and bool(attendee.strip())
                for attendee in attendees
            )
        ):
            ManualCalendarSyncService._raise_malformed_provider_event(
                external_execution_performed
            )

        return {
            "event_id": event_id,
            "title": title,
            "start": start,
            "end": end,
            "attendees": tuple(attendees),
        }

    @staticmethod
    def _raise_malformed_provider_event(
        external_execution_performed: bool,
    ) -> NoReturn:
        raise ManualCalendarSyncError(
            ManualCalendarSyncFailureCode.MALFORMED_PROVIDER_EVENT,
            category=WorkerExecutionFailureCategory.INTERNAL,
            external_execution_performed=external_execution_performed,
        )

    @staticmethod
    def _provider_error(
        reason: str | None,
        category: WorkerExecutionFailureCategory | None,
        metadata: Mapping[str, Any],
        *,
        external_execution_performed: bool,
    ) -> ManualCalendarSyncError:
        provider_reason = metadata.get("provider_reason")
        if provider_reason == "reconnectRequired":
            code = ManualCalendarSyncFailureCode.RECONNECT_REQUIRED
        elif provider_reason == "credentialRefreshUnavailable":
            code = ManualCalendarSyncFailureCode.CREDENTIAL_REFRESH_FAILURE
        elif provider_reason in {"transportUnavailable", "internalTransportError"}:
            code = ManualCalendarSyncFailureCode.TRANSPORT_FAILURE
        elif (
            provider_reason == "invalidProviderResponse"
            or reason == "calendar provider returned invalid event data"
        ):
            code = ManualCalendarSyncFailureCode.MALFORMED_PROVIDER_EVENT
        elif category == WorkerExecutionFailureCategory.TRANSIENT:
            code = ManualCalendarSyncFailureCode.PROVIDER_TRANSIENT_FAILURE
        elif category == WorkerExecutionFailureCategory.PERMANENT:
            code = ManualCalendarSyncFailureCode.PROVIDER_PERMANENT_FAILURE
        else:
            code = ManualCalendarSyncFailureCode.PROVIDER_INTERNAL_FAILURE
        return ManualCalendarSyncError(
            code,
            category=category,
            external_execution_performed=external_execution_performed,
        )


def build_production_manual_calendar_sync_service(
    *,
    application_container: ApplicationContainer,
    http_client: httpx.Client,
    timeout_seconds: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> ManualCalendarSyncService:
    """Build the opt-in production provider path over application-owned ingress."""
    credential_store = MacOSKeychainCredentialStore()
    credentials_provider = StoredGoogleCredentialsProvider(credential_store)
    transport_client = HttpxCalendarTransportClient(
        http_client,
        timeout_seconds=timeout_seconds,
    )
    provider_composition = CalendarProviderComposition(
        credentials_provider=credentials_provider,
        transport_client=transport_client,
    )
    provider_executor = CalendarWorkerExecutor(
        provider_composition=provider_composition,
    )
    return ManualCalendarSyncService(
        provider_executor=provider_executor,
        ingress_adapter=application_container.calendar_ingress_adapter,
    )


def _parse_request(argv: Sequence[str]) -> ManualCalendarSyncRequest:
    if len(argv) != 3:
        raise ManualCalendarSyncError(
            ManualCalendarSyncFailureCode.INVALID_INPUT,
            field="arguments",
        )
    return ManualCalendarSyncRequest(
        principal=argv[0],
        account_identifier=argv[1],
        event_id=argv[2],
    )


def _run_production_sync(
    request: ManualCalendarSyncRequest,
) -> ManualCalendarSyncResult:
    with httpx.Client() as http_client:
        service = build_production_manual_calendar_sync_service(
            application_container=get_container(),
            http_client=http_client,
        )
        return service.sync(request)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicit local command and print one safe JSON result."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        request = _parse_request(arguments)
        result = _run_production_sync(request)
    except ManualCalendarSyncError as error:
        print(json.dumps(error.as_dict(), sort_keys=True), file=sys.stderr)
        if error.code == ManualCalendarSyncFailureCode.INVALID_INPUT:
            print(_USAGE, file=sys.stderr)
            return 2
        return 1
    except Exception:
        safe_error = ManualCalendarSyncError(
            ManualCalendarSyncFailureCode.INGRESS_WORKFLOW_FAILURE
        )
        print(json.dumps(safe_error.as_dict(), sort_keys=True), file=sys.stderr)
        return 1

    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
