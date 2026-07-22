"""Shared Google provider boundary primitives.

Gmail and Calendar (and future Google services) share one deterministic fake
provider boundary: credentials resolution, transport, failure mapping and
composition. Service-specific modules subclass or alias these primitives
instead of duplicating them per integration.

Nothing here performs OAuth, credential storage, HTTP calls or real Google
API behavior.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from apps.server.src.workers.executor import (
    WorkerAccountContext,
    WorkerExecutionFailureCategory,
)


@dataclass(frozen=True)
class GoogleCredentials:
    """Provider-facing Google credential material placeholder."""

    access_token: str
    principal: str
    account: str
    token_type: str = "Bearer"
    expires_at: str | None = None


@dataclass(frozen=True)
class GoogleProviderRequest:
    """Provider transport request behind a Google integration boundary."""

    operation: str
    path: str
    method: str = "GET"
    body: dict[str, Any] | None = None
    query: dict[str, Any] = field(default_factory=dict)
    account_context: WorkerAccountContext | None = None


@dataclass(frozen=True)
class GoogleProviderFailure:
    """Provider failure mapping shape for future Google adapters."""

    category: WorkerExecutionFailureCategory
    message: str
    retryable: bool = False
    provider_status_code: int | None = None
    provider_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GoogleProviderResponse:
    """Provider transport response behind a Google integration boundary."""

    status_code: int
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    failure: GoogleProviderFailure | None = None


class GoogleCredentialsProviderError(Exception):
    """Safe Google credentials provider failure with provider metadata."""

    def __init__(self, failure: GoogleProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


@runtime_checkable
class GoogleCredentialsProvider(Protocol):
    """Contract for resolving Google credentials behind the boundary."""

    def get_credentials(
        self,
        principal: str | None,
        account: str | None,
    ) -> GoogleCredentials:
        """Return Google credentials for a future provider adapter."""


@runtime_checkable
class GoogleTransportClient(Protocol):
    """Contract for Google provider transport behind the integration boundary."""

    def execute(
        self,
        request: GoogleProviderRequest,
        credentials: GoogleCredentials,
    ) -> GoogleProviderResponse:
        """Execute a provider request for a future Google adapter."""


class FakeGoogleCredentialsProvider:
    """Deterministic fake Google credentials provider with no OAuth or storage."""

    def __init__(
        self,
        service: str,
        failures: dict[str, GoogleProviderFailure] | None = None,
    ) -> None:
        self._service = service
        self._failures = failures or {}

    def get_credentials(
        self,
        principal: str | None,
        account: str | None,
    ) -> GoogleCredentials:
        """Return deterministic fake credentials for a normalized principal/account."""
        normalized_principal = self._normalize_identifier(principal, "principal")
        normalized_account = self._normalize_identifier(account, "account")
        failure = (
            self._failures.get(f"{normalized_principal}:{normalized_account}")
            or self._failures.get(normalized_principal)
            or self._failures.get(normalized_account)
        )
        if failure is not None:
            raise GoogleCredentialsProviderError(failure)

        return GoogleCredentials(
            access_token=(
                f"fake-{self._service}-access-token:"
                f"{normalized_principal}:{normalized_account}"
            ),
            principal=normalized_principal,
            account=normalized_account,
            expires_at="2099-01-01T00:00:00Z",
        )

    def _normalize_identifier(self, value: str | None, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GoogleCredentialsProviderError(
                GoogleProviderFailure(
                    category=WorkerExecutionFailureCategory.PERMANENT,
                    message=(
                        f"{self._service} credentials request missing {field_name}"
                    ),
                    metadata={"field": field_name},
                ),
            )
        return value.strip()


class FakeGoogleTransportClient:
    """Deterministic Google transport with no HTTP or provider API behavior."""

    def __init__(
        self,
        service: str,
        responses: dict[str, GoogleProviderResponse] | None = None,
        failures: dict[str, GoogleProviderFailure] | None = None,
    ) -> None:
        self._service = service
        self._responses = responses or {}
        self._failures = failures or {}

    def execute(
        self,
        request: GoogleProviderRequest,
        credentials: GoogleCredentials,
    ) -> GoogleProviderResponse:
        """Return a deterministic fake provider response."""
        key = self._request_key(request)
        failure = self._failures.get(key) or self._failures.get(request.operation)
        if failure is not None:
            return GoogleProviderResponse(
                status_code=failure.provider_status_code or 500,
                body={
                    "external_execution_performed": False,
                    "integration": self._service,
                    "adapter": "fake_transport",
                    "operation": request.operation,
                    "failed": True,
                },
                failure=failure,
            )

        response = self._responses.get(key) or self._responses.get(request.operation)
        if response is not None:
            return response

        return GoogleProviderResponse(
            status_code=200,
            body={
                "external_execution_performed": False,
                "integration": self._service,
                "adapter": "fake_transport",
                "operation": request.operation,
                "path": request.path,
                "method": request.method,
                "token_type": credentials.token_type,
                "principal": credentials.principal,
                "account": credentials.account,
            },
        )

    def _request_key(self, request: GoogleProviderRequest) -> str:
        return f"{request.method}:{request.path}:{request.operation}"


@dataclass(frozen=True)
class GoogleProviderComposition:
    """Compose fake Google provider dependencies behind an integration boundary."""

    service: str
    credentials_provider: GoogleCredentialsProvider
    transport_client: GoogleTransportClient

    def execute(
        self,
        request: GoogleProviderRequest,
        principal: str | None = None,
        account: str | None = None,
    ) -> GoogleProviderResponse:
        """Resolve fake credentials and execute the fake transport safely."""
        if request.account_context is not None:
            request_principal = request.account_context.principal
            request_account = request.account_context.account_identifier
            supplied_principal = principal.strip() if isinstance(principal, str) else None
            supplied_account = account.strip() if isinstance(account, str) else None
            if (
                supplied_principal is not None
                and supplied_principal != request_principal
            ) or (
                supplied_account is not None and supplied_account != request_account
            ):
                failure = GoogleProviderFailure(
                    category=WorkerExecutionFailureCategory.PERMANENT,
                    message=f"{self.service} provider request account context mismatch",
                    metadata={"field": "account_context"},
                )
                return GoogleProviderResponse(
                    status_code=400,
                    body={
                        "external_execution_performed": False,
                        "integration": self.service,
                        "adapter": "fake_provider_composition",
                        "operation": request.operation,
                        "failed": True,
                    },
                    failure=failure,
                )
            principal = request_principal
            account = request_account

        try:
            credentials = self.credentials_provider.get_credentials(
                principal=principal,
                account=account,
            )
        except GoogleCredentialsProviderError as error:
            failure = error.failure
            return GoogleProviderResponse(
                status_code=failure.provider_status_code or 500,
                body={
                    "external_execution_performed": False,
                    "integration": self.service,
                    "adapter": "fake_provider_composition",
                    "operation": request.operation,
                    "failed": True,
                },
                failure=failure,
            )

        return self.transport_client.execute(request, credentials)
