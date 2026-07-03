"""Google Calendar worker executor bootstrap and provider contracts."""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.workers.executor import (
    WorkerExecutionFailureCategory,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)


CALENDAR_EXECUTOR_ROLE = ExecutorRole.CONTEXT_PREPARATION


@dataclass(frozen=True)
class CalendarCredentials:
    """Provider-facing Calendar credential material placeholder."""

    access_token: str
    token_type: str = "Bearer"
    expires_at: str | None = None


@dataclass(frozen=True)
class CalendarProviderRequest:
    """Provider transport request behind the Calendar integration boundary."""

    operation: str
    path: str
    method: str = "GET"
    body: dict[str, Any] | None = None
    query: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CalendarProviderFailure:
    """Provider failure mapping shape for future Calendar adapters."""

    category: WorkerExecutionFailureCategory
    message: str
    retryable: bool = False
    provider_status_code: int | None = None
    provider_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CalendarProviderResponse:
    """Provider transport response behind the Calendar integration boundary."""

    status_code: int
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    failure: CalendarProviderFailure | None = None


class CalendarCredentialsProviderError(Exception):
    """Safe Calendar credentials provider failure with provider metadata."""

    def __init__(self, failure: CalendarProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


@runtime_checkable
class CalendarCredentialsProvider(Protocol):
    """Contract for resolving Calendar credentials behind the boundary."""

    def get_credentials(
        self,
        principal: str | None = None,
        account: str | None = None,
    ) -> CalendarCredentials:
        """Return Calendar credentials for a future provider adapter."""


class FakeCalendarCredentialsProvider:
    """Deterministic fake Calendar credentials provider with no OAuth or storage."""

    def __init__(
        self,
        failures: dict[str, CalendarProviderFailure] | None = None,
    ) -> None:
        self._failures = failures or {}

    def get_credentials(
        self,
        principal: str | None = "fake-principal",
        account: str | None = "fake-account",
    ) -> CalendarCredentials:
        """Return deterministic fake credentials for a normalized principal/account."""
        normalized_principal = self._normalize_identifier(principal, "principal")
        normalized_account = self._normalize_identifier(account, "account")
        failure = (
            self._failures.get(f"{normalized_principal}:{normalized_account}")
            or self._failures.get(normalized_principal)
            or self._failures.get(normalized_account)
        )
        if failure is not None:
            raise CalendarCredentialsProviderError(failure)

        return CalendarCredentials(
            access_token=(
                "fake-calendar-access-token:"
                f"{normalized_principal}:{normalized_account}"
            ),
            expires_at="2099-01-01T00:00:00Z",
        )

    def _normalize_identifier(self, value: str | None, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CalendarCredentialsProviderError(
                CalendarProviderFailure(
                    category=WorkerExecutionFailureCategory.PERMANENT,
                    message=f"calendar credentials request missing {field_name}",
                    metadata={"field": field_name},
                ),
            )
        return value.strip()


@runtime_checkable
class CalendarTransportClient(Protocol):
    """Contract for Calendar provider transport behind the integration boundary."""

    def execute(
        self,
        request: CalendarProviderRequest,
        credentials: CalendarCredentials,
    ) -> CalendarProviderResponse:
        """Execute a provider request for a future Calendar adapter."""


class FakeCalendarTransportClient:
    """Deterministic Calendar transport with no HTTP or provider API behavior."""

    def __init__(
        self,
        responses: dict[str, CalendarProviderResponse] | None = None,
        failures: dict[str, CalendarProviderFailure] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._failures = failures or {}

    def execute(
        self,
        request: CalendarProviderRequest,
        credentials: CalendarCredentials,
    ) -> CalendarProviderResponse:
        """Return a deterministic fake provider response."""
        key = self._request_key(request)
        failure = self._failures.get(key) or self._failures.get(request.operation)
        if failure is not None:
            return CalendarProviderResponse(
                status_code=failure.provider_status_code or 500,
                body={
                    "external_execution_performed": False,
                    "integration": "calendar",
                    "adapter": "fake_transport",
                    "operation": request.operation,
                    "failed": True,
                },
                failure=failure,
            )

        response = self._responses.get(key) or self._responses.get(request.operation)
        if response is not None:
            return response

        return CalendarProviderResponse(
            status_code=200,
            body={
                "external_execution_performed": False,
                "integration": "calendar",
                "adapter": "fake_transport",
                "operation": request.operation,
                "path": request.path,
                "method": request.method,
                "token_type": credentials.token_type,
            },
        )

    def _request_key(self, request: CalendarProviderRequest) -> str:
        return f"{request.method}:{request.path}:{request.operation}"


@dataclass(frozen=True)
class CalendarProviderComposition:
    """Compose fake Calendar provider dependencies behind the boundary."""

    credentials_provider: CalendarCredentialsProvider = field(
        default_factory=FakeCalendarCredentialsProvider,
    )
    transport_client: CalendarTransportClient = field(
        default_factory=FakeCalendarTransportClient,
    )

    def execute(
        self,
        request: CalendarProviderRequest,
        principal: str | None = "fake-principal",
        account: str | None = "fake-account",
    ) -> CalendarProviderResponse:
        """Resolve fake credentials and execute the fake Calendar transport safely."""
        try:
            credentials = self.credentials_provider.get_credentials(
                principal=principal,
                account=account,
            )
        except CalendarCredentialsProviderError as error:
            failure = error.failure
            return CalendarProviderResponse(
                status_code=failure.provider_status_code or 500,
                body={
                    "external_execution_performed": False,
                    "integration": "calendar",
                    "adapter": "fake_provider_composition",
                    "operation": request.operation,
                    "failed": True,
                },
                failure=failure,
            )

        return self.transport_client.execute(request, credentials)


class CalendarWorkerExecutor:
    """Safe Calendar executor bootstrap with no external API behavior."""

    def __init__(
        self,
        provider_composition: CalendarProviderComposition | None = None,
    ) -> None:
        self.provider_composition = (
            provider_composition or CalendarProviderComposition()
        )

    def execute(self, action: Action) -> WorkerExecutionResult:
        """Return a safe Calendar placeholder without contacting Google Calendar."""
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            reason="calendar executor bootstrap placeholder",
            metadata={
                "external_execution_performed": False,
                "integration": "calendar",
                "placeholder": True,
            },
        )
