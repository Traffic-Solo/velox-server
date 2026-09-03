"""Installed Google OAuth bootstrap for one explicit VELOX account."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast, runtime_checkable

from apps.server.src.core.credentials import (
    CredentialAlreadyExistsError,
    CredentialMaterial,
    CredentialReference,
    CredentialStore,
    CredentialStoreBackendError,
)
from apps.server.src.integrations.google_provider import (
    GoogleCredentials,
    GoogleCredentialsProviderError,
    GoogleProviderFailure,
)
from apps.server.src.workers.executor import WorkerExecutionFailureCategory
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

GOOGLE_OAUTH_CREDENTIAL_NAMESPACE = "velox.google.oauth"
GOOGLE_OAUTH_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)
_STORED_GOOGLE_CREDENTIAL_FIELDS = frozenset(
    {"client_id", "client_secret", "refresh_token", "scopes"}
)


class GoogleOAuthBootstrapError(Exception):
    """Safe failure raised when Google OAuth bootstrap cannot complete."""

    def __init__(self) -> None:
        super().__init__("Google OAuth bootstrap failed")


class GoogleOAuthAccountMismatchError(GoogleOAuthBootstrapError):
    """Safe failure raised when the authenticated account is not the expected one."""

    def __init__(self) -> None:
        Exception.__init__(self, "authenticated Google account does not match expected account")


@dataclass(frozen=True)
class GoogleOAuthConnectRequest:
    """Explicit inputs for connecting one VELOX account to one Google account."""

    client_secrets_file: str
    account_identifier: str
    expected_google_email: str
    replace: bool = False


@dataclass(frozen=True)
class GoogleOAuthConnectResult:
    """Safe metadata returned after a verified Google OAuth connection."""

    credential_reference: CredentialReference
    account_identifier: str
    verified_google_email: str
    scopes: tuple[str, ...]


@runtime_checkable
class GoogleOAuthAuthorizer(Protocol):
    """Obtain Google OAuth credentials through an interactive installed-app flow."""

    def authorize(
        self,
        client_secrets_file: str,
        scopes: tuple[str, ...],
    ) -> Credentials: ...


class InstalledAppGoogleOAuthAuthorizer:
    """Run Google's installed-app OAuth flow through a browser and loopback server."""

    def authorize(
        self,
        client_secrets_file: str,
        scopes: tuple[str, ...],
    ) -> Credentials:
        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes=scopes)
        return cast(
            Credentials,
            flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=True,
                access_type="offline",
                prompt="consent",
                authorization_prompt_message="",
            ),
        )


@runtime_checkable
class GoogleIdentityVerifier(Protocol):
    """Verify a Google-issued ID token and return its authenticated identity claims."""

    def verify(self, token: str, *, audience: str) -> Mapping[str, object]: ...


class GoogleIdTokenIdentityVerifier:
    """Verify Google ID tokens with the official google-auth implementation."""

    def verify(self, token: str, *, audience: str) -> Mapping[str, object]:
        verify_oauth2_token = cast(
            Callable[[str, GoogleAuthRequest, str], Mapping[str, object]],
            google_id_token.verify_oauth2_token,
        )
        return verify_oauth2_token(
            token,
            GoogleAuthRequest(),
            audience,
        )


class GoogleOAuthBootstrapService:
    """Authorize, verify and persist refresh-capable Google credential material."""

    def __init__(
        self,
        authorizer: GoogleOAuthAuthorizer,
        identity_verifier: GoogleIdentityVerifier,
        credential_store: CredentialStore,
    ) -> None:
        self._authorizer = authorizer
        self._identity_verifier = identity_verifier
        self._credential_store = credential_store

    def connect(self, request: GoogleOAuthConnectRequest) -> GoogleOAuthConnectResult:
        """Connect only the explicitly requested and verified Google account."""
        self._validate_request(request)

        authorization_failed = False
        credentials: Credentials | None = None
        try:
            credentials = self._authorizer.authorize(
                request.client_secrets_file,
                GOOGLE_OAUTH_SCOPES,
            )
        except Exception:
            authorization_failed = True

        if authorization_failed or credentials is None:
            raise GoogleOAuthBootstrapError()

        credential_fields_failed = False
        raw_refresh_token: object = None
        raw_identity_token: object = None
        raw_client_id: object = None
        raw_client_secret: object = None
        try:
            raw_refresh_token = credentials.refresh_token
            raw_identity_token = credentials.id_token
            raw_client_id = credentials.client_id
            raw_client_secret = credentials.client_secret
        except Exception:
            credential_fields_failed = True

        if credential_fields_failed:
            raise GoogleOAuthBootstrapError()

        refresh_token = self._require_non_blank_string(raw_refresh_token)
        identity_token = self._require_non_blank_string(raw_identity_token)
        client_id = self._require_non_blank_string(raw_client_id)
        client_secret = self._require_non_blank_string(raw_client_secret)

        verification_failed = False
        identity: Mapping[str, object] | None = None
        try:
            identity = self._identity_verifier.verify(identity_token, audience=client_id)
        except Exception:
            verification_failed = True

        if verification_failed or identity is None:
            raise GoogleOAuthBootstrapError()

        verified_email = self._verified_email(identity)
        expected_email = request.expected_google_email.strip()
        if verified_email.casefold() != expected_email.casefold():
            raise GoogleOAuthAccountMismatchError()

        serialized_material = json.dumps(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "scopes": list(GOOGLE_OAUTH_SCOPES),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        reference = CredentialReference(
            namespace=GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
            account_identifier=request.account_identifier,
        )

        storage_failed = False
        try:
            self._credential_store.store(
                reference,
                CredentialMaterial(serialized_material),
                replace=request.replace,
            )
        except (CredentialAlreadyExistsError, CredentialStoreBackendError):
            raise
        except Exception:
            storage_failed = True

        if storage_failed:
            raise GoogleOAuthBootstrapError()

        return GoogleOAuthConnectResult(
            credential_reference=reference,
            account_identifier=request.account_identifier,
            verified_google_email=verified_email,
            scopes=GOOGLE_OAUTH_SCOPES,
        )

    @staticmethod
    def _validate_request(request: GoogleOAuthConnectRequest) -> None:
        if (
            not isinstance(request.client_secrets_file, str)
            or not request.client_secrets_file.strip()
        ):
            raise GoogleOAuthBootstrapError()
        if (
            not isinstance(request.account_identifier, str)
            or not request.account_identifier.strip()
        ):
            raise GoogleOAuthBootstrapError()
        if (
            not isinstance(request.expected_google_email, str)
            or not request.expected_google_email.strip()
        ):
            raise GoogleOAuthBootstrapError()
        if not isinstance(request.replace, bool):
            raise GoogleOAuthBootstrapError()

    @staticmethod
    def _require_non_blank_string(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GoogleOAuthBootstrapError()
        return value

    @staticmethod
    def _verified_email(identity: Mapping[str, object]) -> str:
        if identity.get("email_verified") is not True:
            raise GoogleOAuthBootstrapError()
        email = identity.get("email")
        if not isinstance(email, str) or not email.strip():
            raise GoogleOAuthBootstrapError()
        return email.strip()


class StoredGoogleCredentialsProvider:
    """Resolve and refresh stored Google OAuth credentials for one VELOX account."""

    def __init__(self, credential_store: CredentialStore) -> None:
        self._credential_store = credential_store

    def get_credentials(
        self,
        principal: str | None,
        account: str | None,
    ) -> GoogleCredentials:
        """Return an ephemeral refreshed token for explicit VELOX routing identity."""
        self._validate_routing_identifier(principal, "principal")
        self._validate_routing_identifier(account, "account")
        assert isinstance(principal, str)
        assert isinstance(account, str)

        reference = CredentialReference(
            namespace=GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
            account_identifier=account,
        )
        storage_failure: GoogleProviderFailure | None = None
        material: CredentialMaterial | None = None
        try:
            material = self._credential_store.get(reference)
        except CredentialStoreBackendError:
            storage_failure = self._failure(
                WorkerExecutionFailureCategory.TRANSIENT,
                "Google credential store is unavailable",
                retryable=True,
                status_code=503,
                reason="credentialStoreUnavailable",
            )
        except Exception:
            storage_failure = self._failure(
                WorkerExecutionFailureCategory.INTERNAL,
                "Google credentials could not be resolved",
                status_code=500,
                reason="internalCredentialsError",
            )
        if storage_failure is not None:
            raise GoogleCredentialsProviderError(storage_failure)
        if material is None:
            raise GoogleCredentialsProviderError(
                self._failure(
                    WorkerExecutionFailureCategory.PERMANENT,
                    "Google credentials are unavailable; reconnect is required",
                    status_code=401,
                    reason="reconnectRequired",
                )
            )

        credential_info = self._parse_credential_material(material.value)
        reconstruction_failure = False
        credentials: Credentials | None = None
        try:
            from_authorized_user_info = cast(
                Callable[[Mapping[str, object]], Credentials],
                Credentials.from_authorized_user_info,
            )
            credentials = from_authorized_user_info(credential_info)
        except Exception:
            reconstruction_failure = True
        if reconstruction_failure or credentials is None:
            raise GoogleCredentialsProviderError(
                self._failure(
                    WorkerExecutionFailureCategory.PERMANENT,
                    "Stored Google credentials are invalid; reconnect is required",
                    status_code=401,
                    reason="reconnectRequired",
                )
            )

        refresh_failure: GoogleProviderFailure | None = None
        try:
            refresh = cast(Callable[[GoogleAuthRequest], None], credentials.refresh)
            refresh(GoogleAuthRequest())
        except TransportError:
            refresh_failure = self._failure(
                WorkerExecutionFailureCategory.TRANSIENT,
                "Google credential refresh is temporarily unavailable",
                retryable=True,
                status_code=503,
                reason="credentialRefreshUnavailable",
            )
        except RefreshError as error:
            retryable = bool(error.retryable)
            refresh_failure = self._failure(
                (
                    WorkerExecutionFailureCategory.TRANSIENT
                    if retryable
                    else WorkerExecutionFailureCategory.PERMANENT
                ),
                (
                    "Google credential refresh is temporarily unavailable"
                    if retryable
                    else "Google credentials are invalid; reconnect is required"
                ),
                retryable=retryable,
                status_code=503 if retryable else 401,
                reason=(
                    "credentialRefreshUnavailable"
                    if retryable
                    else "reconnectRequired"
                ),
            )
        except Exception:
            refresh_failure = self._failure(
                WorkerExecutionFailureCategory.INTERNAL,
                "Google credentials could not be refreshed",
                status_code=500,
                reason="internalCredentialsError",
            )
        if refresh_failure is not None:
            raise GoogleCredentialsProviderError(refresh_failure)

        access_token = credentials.token
        if not isinstance(access_token, str) or not access_token.strip():
            raise GoogleCredentialsProviderError(
                self._failure(
                    WorkerExecutionFailureCategory.PERMANENT,
                    "Google credentials are invalid; reconnect is required",
                    status_code=401,
                    reason="reconnectRequired",
                )
            )

        return GoogleCredentials(
            access_token=access_token,
            principal=principal,
            account=account,
            expires_at=self._safe_expiry(credentials.expiry),
        )

    @classmethod
    def _parse_credential_material(cls, serialized: str) -> dict[str, object]:
        parse_failed = False
        parsed: object = None
        try:
            parsed = json.loads(serialized)
        except Exception:
            parse_failed = True
        if (
            parse_failed
            or not isinstance(parsed, dict)
            or set(parsed) != _STORED_GOOGLE_CREDENTIAL_FIELDS
        ):
            cls._raise_malformed_credentials()

        assert isinstance(parsed, dict)
        for field_name in ("client_id", "client_secret", "refresh_token"):
            value = parsed.get(field_name)
            if not isinstance(value, str) or not value.strip():
                cls._raise_malformed_credentials()

        scopes = parsed.get("scopes")
        if (
            not isinstance(scopes, list)
            or not all(isinstance(scope, str) for scope in scopes)
            or tuple(scopes) != GOOGLE_OAUTH_SCOPES
        ):
            cls._raise_malformed_credentials()

        return {
            "client_id": parsed["client_id"],
            "client_secret": parsed["client_secret"],
            "refresh_token": parsed["refresh_token"],
            "scopes": list(GOOGLE_OAUTH_SCOPES),
        }

    @staticmethod
    def _validate_routing_identifier(value: object, field_name: str) -> None:
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise GoogleCredentialsProviderError(
                StoredGoogleCredentialsProvider._failure(
                    WorkerExecutionFailureCategory.PERMANENT,
                    f"Google credentials request has invalid {field_name}",
                    status_code=400,
                    reason="invalidAccountContext",
                    metadata={"field": field_name},
                )
            )

    @staticmethod
    def _safe_expiry(expiry: object) -> str | None:
        if not isinstance(expiry, datetime):
            return None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return expiry.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _raise_malformed_credentials() -> None:
        raise GoogleCredentialsProviderError(
            StoredGoogleCredentialsProvider._failure(
                WorkerExecutionFailureCategory.PERMANENT,
                "Stored Google credentials are invalid; reconnect is required",
                status_code=401,
                reason="reconnectRequired",
            )
        )

    @staticmethod
    def _failure(
        category: WorkerExecutionFailureCategory,
        message: str,
        *,
        retryable: bool = False,
        status_code: int,
        reason: str,
        metadata: dict[str, object] | None = None,
    ) -> GoogleProviderFailure:
        return GoogleProviderFailure(
            category=category,
            message=message,
            retryable=retryable,
            provider_status_code=status_code,
            provider_reason=reason,
            metadata=metadata or {},
        )
