"""Google Calendar worker executor bootstrap on the shared Google boundary."""

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
    WorkerExecutionResult,
    WorkerExecutionStatus,
)

CALENDAR_EXECUTOR_ROLE = ExecutorRole.CONTEXT_PREPARATION

# Provider boundary: shared Google primitives, specialized for Calendar.
CalendarCredentials = GoogleCredentials
CalendarProviderRequest = GoogleProviderRequest
CalendarProviderFailure = GoogleProviderFailure
CalendarProviderResponse = GoogleProviderResponse
CalendarCredentialsProviderError = GoogleCredentialsProviderError
CalendarCredentialsProvider = GoogleCredentialsProvider
CalendarTransportClient = GoogleTransportClient


class FakeCalendarCredentialsProvider(FakeGoogleCredentialsProvider):
    """Deterministic fake Calendar credentials provider with no OAuth or storage."""

    def __init__(
        self,
        failures: dict[str, GoogleProviderFailure] | None = None,
    ) -> None:
        super().__init__(service="calendar", failures=failures)


class FakeCalendarTransportClient(FakeGoogleTransportClient):
    """Deterministic Calendar transport with no HTTP or provider API behavior."""

    def __init__(
        self,
        responses: dict[str, GoogleProviderResponse] | None = None,
        failures: dict[str, GoogleProviderFailure] | None = None,
    ) -> None:
        super().__init__(service="calendar", responses=responses, failures=failures)


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

    def execute(
        self,
        action: Action,
        *,
        capability: str | None = None,
        account_context: WorkerAccountContext | None = None,
    ) -> WorkerExecutionResult:
        """Return a safe Calendar placeholder without contacting Google Calendar."""
        if account_context is not None and capability in {
            "prepare_meeting",
            "prepare_calendar_context",
        }:
            return self._execute_provider_request(
                action,
                capability,
                account_context,
            )

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

    def _execute_provider_request(
        self,
        action: Action,
        capability: str,
        account_context: WorkerAccountContext,
    ) -> WorkerExecutionResult:
        request = CalendarProviderRequest(
            operation=capability,
            path="/calendar/v3/users/me/calendarList",
            account_context=account_context,
        )
        response = self.provider_composition.execute(request)
        if response.failure is not None:
            failure = response.failure
            return WorkerExecutionResult(
                action=action,
                status=WorkerExecutionStatus.FAILED,
                reason=failure.message,
                metadata={
                    "external_execution_performed": False,
                    "integration": "calendar",
                    "account_context_used": account_context.as_metadata(),
                    "provider_request": _calendar_provider_request_metadata(request),
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

        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            reason="calendar provider request constructed",
            metadata={
                "external_execution_performed": False,
                "integration": "calendar",
                "adapter": "fake_transport",
                "account_context_used": account_context.as_metadata(),
                "provider_request": _calendar_provider_request_metadata(request),
                "provider_response": dict(response.body),
            },
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
