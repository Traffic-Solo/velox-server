"""Deterministic tests for installed Google OAuth bootstrap."""

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from apps.server.src.core.credentials import (
    CredentialAlreadyExistsError,
    CredentialMaterial,
    CredentialReference,
    CredentialStoreBackendError,
    InMemoryCredentialStore,
)
from apps.server.src.integrations import google_oauth
from apps.server.src.integrations.google_oauth import (
    GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
    GOOGLE_OAUTH_SCOPES,
    GoogleIdTokenIdentityVerifier,
    GoogleOAuthAccountMismatchError,
    GoogleOAuthBootstrapError,
    GoogleOAuthBootstrapService,
    GoogleOAuthConnectRequest,
    InstalledAppGoogleOAuthAuthorizer,
    StoredGoogleCredentialsProvider,
)
from apps.server.src.integrations.google_provider import GoogleCredentialsProviderError
from apps.server.src.workers.executor import WorkerExecutionFailureCategory
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials

REFRESH_TOKEN = "refresh-token-secret"
ID_TOKEN = "id-token-secret"
ACCESS_TOKEN = "access-token-secret"
CLIENT_ID = "google-client-id"
CLIENT_SECRET = "client-secret-value"
ACCOUNT_IDENTIFIER = "velox-account-1"
GOOGLE_EMAIL = "person@example.com"


class FakeGoogleOAuthAuthorizer:
    """Return deterministic credentials without browser, socket or HTTP activity."""

    def __init__(self, credentials: Credentials | None = None) -> None:
        self.credentials = credentials if credentials is not None else google_credentials()
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.failure: Exception | None = None

    def authorize(self, client_secrets_file: str, scopes: tuple[str, ...]) -> Credentials:
        self.calls.append((client_secrets_file, scopes))
        if self.failure is not None:
            raise self.failure
        return self.credentials


class FakeGoogleIdentityVerifier:
    """Return deterministic verified claims without contacting Google."""

    def __init__(self, identity: Mapping[str, object] | None = None) -> None:
        self.identity = (
            identity
            if identity is not None
            else {
                "email": GOOGLE_EMAIL,
                "email_verified": True,
            }
        )
        self.calls: list[tuple[str, str]] = []
        self.failure: Exception | None = None

    def verify(self, token: str, *, audience: str) -> Mapping[str, object]:
        self.calls.append((token, audience))
        if self.failure is not None:
            raise self.failure
        return self.identity


def google_credentials(
    *,
    refresh_token: str | None = REFRESH_TOKEN,
    id_token: str | None = ID_TOKEN,
    client_id: str | None = CLIENT_ID,
    client_secret: str | None = CLIENT_SECRET,
) -> Credentials:
    return Credentials(
        token=ACCESS_TOKEN,
        refresh_token=refresh_token,
        id_token=id_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=GOOGLE_OAUTH_SCOPES,
    )


def connect_request(
    *,
    expected_google_email: str = GOOGLE_EMAIL,
    replace: bool = False,
) -> GoogleOAuthConnectRequest:
    return GoogleOAuthConnectRequest(
        client_secrets_file="/safe/client-secrets.json",
        account_identifier=ACCOUNT_IDENTIFIER,
        expected_google_email=expected_google_email,
        replace=replace,
    )


def credential_reference() -> CredentialReference:
    return CredentialReference(
        namespace=GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
        account_identifier=ACCOUNT_IDENTIFIER,
    )


def stored_credential_material(
    *,
    scopes: object = GOOGLE_OAUTH_SCOPES,
    **overrides: object,
) -> CredentialMaterial:
    fields: dict[str, object] = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "scopes": scopes,
    }
    fields.update(overrides)
    return CredentialMaterial(json.dumps(fields))


class RecordingCredentialStore(InMemoryCredentialStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls: list[CredentialReference] = []
        self.store_calls = 0

    def store(
        self,
        reference: CredentialReference,
        material: CredentialMaterial,
        *,
        replace: bool = False,
    ) -> None:
        self.store_calls += 1
        super().store(reference, material, replace=replace)

    def get(self, reference: CredentialReference) -> CredentialMaterial | None:
        self.get_calls.append(reference)
        return super().get(reference)


def refresh_successfully(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str = ACCESS_TOKEN,
) -> None:
    def refresh(credentials: Credentials, request: object) -> None:
        credentials.token = token
        credentials.expiry = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)

    monkeypatch.setattr(Credentials, "refresh", refresh)


def bootstrap_service(
    *,
    authorizer: FakeGoogleOAuthAuthorizer | None = None,
    verifier: FakeGoogleIdentityVerifier | None = None,
    store: InMemoryCredentialStore | None = None,
) -> tuple[
    GoogleOAuthBootstrapService,
    FakeGoogleOAuthAuthorizer,
    FakeGoogleIdentityVerifier,
    InMemoryCredentialStore,
]:
    actual_authorizer = authorizer or FakeGoogleOAuthAuthorizer()
    actual_verifier = verifier or FakeGoogleIdentityVerifier()
    actual_store = store or InMemoryCredentialStore()
    return (
        GoogleOAuthBootstrapService(actual_authorizer, actual_verifier, actual_store),
        actual_authorizer,
        actual_verifier,
        actual_store,
    )


def test_installed_app_authorizer_uses_exact_approved_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = google_credentials()
    flow_calls: list[tuple[str, tuple[str, ...]]] = []
    server_calls: list[dict[str, object]] = []

    class FakeInstalledAppFlow:
        def run_local_server(self, **kwargs: object) -> Credentials:
            server_calls.append(kwargs)
            return credentials

    def from_client_secrets_file(
        client_secrets_file: str,
        scopes: tuple[str, ...],
    ) -> FakeInstalledAppFlow:
        flow_calls.append((client_secrets_file, scopes))
        return FakeInstalledAppFlow()

    monkeypatch.setattr(
        google_oauth.InstalledAppFlow,
        "from_client_secrets_file",
        staticmethod(from_client_secrets_file),
    )

    result = InstalledAppGoogleOAuthAuthorizer().authorize(
        "/safe/client-secrets.json",
        GOOGLE_OAUTH_SCOPES,
    )

    assert result is credentials
    assert flow_calls == [("/safe/client-secrets.json", GOOGLE_OAUTH_SCOPES)]
    assert server_calls == [
        {
            "host": "127.0.0.1",
            "port": 0,
            "open_browser": True,
            "access_type": "offline",
            "prompt": "consent",
            "authorization_prompt_message": "",
        }
    ]


def test_identity_verifier_uses_official_verification_with_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google_request = object()
    calls: list[tuple[str, object, str | None]] = []
    claims: Mapping[str, object] = {"email": GOOGLE_EMAIL, "email_verified": True}

    monkeypatch.setattr(google_oauth, "GoogleAuthRequest", lambda: google_request)

    def verify_oauth2_token(
        token: str,
        request: object,
        audience: str | None = None,
    ) -> Mapping[str, object]:
        calls.append((token, request, audience))
        return claims

    monkeypatch.setattr(
        google_oauth.google_id_token,
        "verify_oauth2_token",
        verify_oauth2_token,
    )

    result = GoogleIdTokenIdentityVerifier().verify(ID_TOKEN, audience=CLIENT_ID)

    assert result is claims
    assert calls == [(ID_TOKEN, google_request, CLIENT_ID)]


def test_successful_connect_verifies_identity_and_returns_safe_metadata() -> None:
    service, authorizer, verifier, store = bootstrap_service()

    result = service.connect(connect_request())

    assert authorizer.calls == [("/safe/client-secrets.json", GOOGLE_OAUTH_SCOPES)]
    assert verifier.calls == [(ID_TOKEN, CLIENT_ID)]
    assert result.credential_reference == credential_reference()
    assert result.account_identifier == ACCOUNT_IDENTIFIER
    assert result.verified_google_email == GOOGLE_EMAIL
    assert result.scopes == GOOGLE_OAUTH_SCOPES
    assert set(asdict(result)) == {
        "credential_reference",
        "account_identifier",
        "verified_google_email",
        "scopes",
    }
    exposed_result = repr(result)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, ID_TOKEN, CLIENT_SECRET):
        assert secret not in exposed_result
    assert store.get(credential_reference()) is not None


def test_stored_material_contains_only_refresh_capable_authorized_user_fields() -> None:
    service, _, _, store = bootstrap_service()

    service.connect(connect_request())

    material = store.get(credential_reference())
    assert material is not None
    stored = json.loads(material.value)
    assert stored == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "scopes": list(GOOGLE_OAUTH_SCOPES),
    }
    assert "token" not in stored
    assert "access_token" not in stored
    assert "id_token" not in stored


def test_stored_material_reconstructs_official_refreshable_credentials() -> None:
    service, _, _, store = bootstrap_service()
    service.connect(connect_request())
    material = store.get(credential_reference())
    assert material is not None

    reconstructed = Credentials.from_authorized_user_info(json.loads(material.value))

    assert reconstructed.refresh_token == REFRESH_TOKEN
    assert reconstructed.client_id == CLIENT_ID
    assert reconstructed.client_secret == CLIENT_SECRET
    assert reconstructed.scopes == list(GOOGLE_OAUTH_SCOPES)
    assert reconstructed.token is None
    assert reconstructed.id_token is None


def test_stored_credentials_provider_loads_exact_account_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingCredentialStore()
    store.store(credential_reference(), stored_credential_material())
    initial_store_calls = store.store_calls
    refresh_successfully(monkeypatch)

    credentials = StoredGoogleCredentialsProvider(store).get_credentials(
        principal="principal-1",
        account=ACCOUNT_IDENTIFIER,
    )

    assert store.get_calls == [credential_reference()]
    assert credentials.access_token == ACCESS_TOKEN
    assert credentials.principal == "principal-1"
    assert credentials.account == ACCOUNT_IDENTIFIER
    assert credentials.expires_at == "2026-09-03T20:00:00Z"
    assert store.store_calls == initial_store_calls
    persisted = store.get(credential_reference())
    assert persisted is not None
    assert ACCESS_TOKEN not in persisted.value


def test_stored_credentials_provider_reconstructs_official_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryCredentialStore()
    store.store(credential_reference(), stored_credential_material())
    reconstructed: list[Credentials] = []

    def refresh(credentials: Credentials, request: object) -> None:
        reconstructed.append(credentials)
        credentials.token = ACCESS_TOKEN

    monkeypatch.setattr(Credentials, "refresh", refresh)

    StoredGoogleCredentialsProvider(store).get_credentials(
        principal="principal-1",
        account=ACCOUNT_IDENTIFIER,
    )

    assert len(reconstructed) == 1
    assert reconstructed[0].refresh_token == REFRESH_TOKEN
    assert reconstructed[0].client_id == CLIENT_ID
    assert reconstructed[0].client_secret == CLIENT_SECRET
    assert reconstructed[0].scopes == list(GOOGLE_OAUTH_SCOPES)


@pytest.mark.parametrize(
    ("principal", "account", "field"),
    [
        (None, ACCOUNT_IDENTIFIER, "principal"),
        ("", ACCOUNT_IDENTIFIER, "principal"),
        (" principal-1 ", ACCOUNT_IDENTIFIER, "principal"),
        ("principal-1", None, "account"),
        ("principal-1", "", "account"),
        ("principal-1", " velox-account-1 ", "account"),
    ],
)
def test_stored_credentials_provider_rejects_malformed_routing_identity(
    principal: str | None,
    account: str | None,
    field: str,
) -> None:
    store = RecordingCredentialStore()

    with pytest.raises(GoogleCredentialsProviderError) as raised:
        StoredGoogleCredentialsProvider(store).get_credentials(principal, account)

    assert raised.value.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert raised.value.failure.metadata == {"field": field}
    assert store.get_calls == []


def test_stored_credentials_provider_fails_closed_when_credential_is_missing() -> None:
    with pytest.raises(GoogleCredentialsProviderError) as raised:
        StoredGoogleCredentialsProvider(InMemoryCredentialStore()).get_credentials(
            "principal-1",
            ACCOUNT_IDENTIFIER,
        )

    assert raised.value.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert raised.value.failure.provider_status_code == 401
    assert raised.value.failure.provider_reason == "reconnectRequired"


@pytest.mark.parametrize(
    "material",
    [
        CredentialMaterial("not-json"),
        CredentialMaterial("[]"),
        stored_credential_material(client_id=""),
        stored_credential_material(client_secret=None),
        stored_credential_material(refresh_token=1),
        stored_credential_material(scopes=[]),
        stored_credential_material(scopes=[*GOOGLE_OAUTH_SCOPES, "unexpected"]),
        stored_credential_material(token=ACCESS_TOKEN),
    ],
)
def test_stored_credentials_provider_rejects_malformed_material(
    material: CredentialMaterial,
) -> None:
    store = InMemoryCredentialStore()
    store.store(credential_reference(), material)

    with pytest.raises(GoogleCredentialsProviderError) as raised:
        StoredGoogleCredentialsProvider(store).get_credentials(
            "principal-1",
            ACCOUNT_IDENTIFIER,
        )

    assert raised.value.failure.category == WorkerExecutionFailureCategory.PERMANENT
    assert raised.value.failure.provider_status_code == 401
    assert raised.value.failure.provider_reason == "reconnectRequired"


@pytest.mark.parametrize(
    ("refresh_error", "category", "retryable", "status_code"),
    [
        (
            RefreshError("invalid grant secret", retryable=False),
            WorkerExecutionFailureCategory.PERMANENT,
            False,
            401,
        ),
        (
            RefreshError("temporary secret", retryable=True),
            WorkerExecutionFailureCategory.TRANSIENT,
            True,
            503,
        ),
        (
            TransportError("network secret"),
            WorkerExecutionFailureCategory.TRANSIENT,
            True,
            503,
        ),
    ],
)
def test_stored_credentials_provider_maps_refresh_failures_safely(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    refresh_error: Exception,
    category: WorkerExecutionFailureCategory,
    retryable: bool,
    status_code: int,
) -> None:
    store = InMemoryCredentialStore()
    store.store(credential_reference(), stored_credential_material())

    def refresh(credentials: Credentials, request: object) -> None:
        raise refresh_error

    monkeypatch.setattr(Credentials, "refresh", refresh)

    with pytest.raises(GoogleCredentialsProviderError) as raised:
        StoredGoogleCredentialsProvider(store).get_credentials(
            "principal-1",
            ACCOUNT_IDENTIFIER,
        )

    assert raised.value.failure.category == category
    assert raised.value.failure.retryable is retryable
    assert raised.value.failure.provider_status_code == status_code
    exposed = (
        str(raised.value),
        repr(raised.value),
        repr(raised.value.__dict__),
        caplog.text,
    )
    assert all("secret" not in value for value in exposed)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


def test_stored_credentials_provider_maps_backend_failure_safely() -> None:
    class FailingCredentialStore(InMemoryCredentialStore):
        def get(self, reference: CredentialReference) -> CredentialMaterial | None:
            raise CredentialStoreBackendError()

    with pytest.raises(GoogleCredentialsProviderError) as raised:
        StoredGoogleCredentialsProvider(FailingCredentialStore()).get_credentials(
            "principal-1",
            ACCOUNT_IDENTIFIER,
        )

    assert raised.value.failure.category == WorkerExecutionFailureCategory.TRANSIENT
    assert raised.value.failure.retryable is True
    assert raised.value.failure.provider_status_code == 503
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "expected_google_email",
    [" person@example.com ", "PERSON@EXAMPLE.COM", " Person@Example.Com "],
)
def test_email_matching_trims_and_tolerates_case_differences(
    expected_google_email: str,
) -> None:
    verifier = FakeGoogleIdentityVerifier(
        {"email": " Person@Example.com ", "email_verified": True}
    )
    service, _, _, _ = bootstrap_service(verifier=verifier)

    result = service.connect(connect_request(expected_google_email=expected_google_email))

    assert result.verified_google_email == "Person@Example.com"


def test_account_mismatch_raises_dedicated_safe_error_and_stores_nothing() -> None:
    service, _, _, store = bootstrap_service()

    with pytest.raises(GoogleOAuthAccountMismatchError) as raised:
        service.connect(connect_request(expected_google_email="different@example.com"))

    assert store.get(credential_reference()) is None
    assert GOOGLE_EMAIL not in str(raised.value)
    assert "different@example.com" not in repr(raised.value)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("email_verified", [False, None, "true", 1])
def test_email_verified_must_be_boolean_true(email_verified: object) -> None:
    verifier = FakeGoogleIdentityVerifier(
        {"email": GOOGLE_EMAIL, "email_verified": email_verified}
    )
    service, _, _, store = bootstrap_service(verifier=verifier)

    with pytest.raises(GoogleOAuthBootstrapError):
        service.connect(connect_request())

    assert store.get(credential_reference()) is None


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"email_verified": True},
        {"email": None, "email_verified": True},
        {"email": "", "email_verified": True},
        {"email": "   ", "email_verified": True},
    ],
)
def test_invalid_verified_identity_fails_closed(identity: Mapping[str, object]) -> None:
    service, _, _, store = bootstrap_service(
        verifier=FakeGoogleIdentityVerifier(identity)
    )

    with pytest.raises(GoogleOAuthBootstrapError):
        service.connect(connect_request())

    assert store.get(credential_reference()) is None


@pytest.mark.parametrize(
    ("field", "credentials"),
    [
        ("refresh token", google_credentials(refresh_token=None)),
        ("ID token", google_credentials(id_token=None)),
        ("client ID", google_credentials(client_id=None)),
        ("client secret", google_credentials(client_secret=None)),
    ],
)
def test_missing_required_credential_field_fails_closed(
    field: str,
    credentials: Credentials,
) -> None:
    service, _, verifier, store = bootstrap_service(
        authorizer=FakeGoogleOAuthAuthorizer(credentials)
    )

    with pytest.raises(GoogleOAuthBootstrapError):
        service.connect(connect_request())

    assert store.get(credential_reference()) is None, field
    assert verifier.calls == []


@pytest.mark.parametrize("failure_boundary", ["authorizer", "verifier"])
def test_external_failures_are_redacted_without_exception_chaining_or_logs(
    failure_boundary: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "third-party-exception-secret"
    authorizer = FakeGoogleOAuthAuthorizer()
    verifier = FakeGoogleIdentityVerifier()
    if failure_boundary == "authorizer":
        authorizer.failure = RuntimeError(f"authorization failed: {secret}")
    else:
        verifier.failure = RuntimeError(f"verification failed: {secret}")
    service, _, _, store = bootstrap_service(authorizer=authorizer, verifier=verifier)

    with pytest.raises(GoogleOAuthBootstrapError) as raised:
        service.connect(connect_request())

    exposed_values = (
        str(raised.value),
        repr(raised.value),
        repr(raised.value.__dict__),
        caplog.text,
    )
    assert all(secret not in exposed for exposed in exposed_values)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert store.get(credential_reference()) is None


def test_existing_credential_is_not_silently_overwritten() -> None:
    store = InMemoryCredentialStore()
    original = CredentialMaterial("original-material")
    store.store(credential_reference(), original)
    service, _, _, _ = bootstrap_service(store=store)

    with pytest.raises(CredentialAlreadyExistsError):
        service.connect(connect_request())

    assert store.get(credential_reference()) == original


def test_explicit_replace_updates_existing_credential() -> None:
    store = InMemoryCredentialStore()
    store.store(credential_reference(), CredentialMaterial("original-material"))
    service, _, _, _ = bootstrap_service(store=store)

    service.connect(connect_request(replace=True))

    material = store.get(credential_reference())
    assert material is not None
    assert json.loads(material.value)["refresh_token"] == REFRESH_TOKEN


@pytest.mark.parametrize(
    "oauth_connect_request",
    [
        GoogleOAuthConnectRequest("", ACCOUNT_IDENTIFIER, GOOGLE_EMAIL),
        GoogleOAuthConnectRequest("client.json", "", GOOGLE_EMAIL),
        GoogleOAuthConnectRequest("client.json", ACCOUNT_IDENTIFIER, ""),
        GoogleOAuthConnectRequest(
            "client.json",
            ACCOUNT_IDENTIFIER,
            GOOGLE_EMAIL,
            replace=1,  # type: ignore[arg-type]
        ),
    ],
)
def test_invalid_explicit_input_fails_before_authorization(
    oauth_connect_request: GoogleOAuthConnectRequest,
) -> None:
    service, authorizer, verifier, store = bootstrap_service()

    with pytest.raises(GoogleOAuthBootstrapError):
        service.connect(oauth_connect_request)

    assert authorizer.calls == []
    assert verifier.calls == []
    assert store.get(credential_reference()) is None
