"""Deterministic tests for the explicit local Google OAuth command."""

import json
from typing import ClassVar

import pytest
from apps.server.src.core.credentials import (
    CredentialMaterial,
    CredentialReference,
    CredentialStore,
    CredentialStoreBackendError,
    InMemoryCredentialStore,
)
from apps.server.src.integrations import google_oauth_cli
from apps.server.src.integrations.google_oauth import (
    GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
    GOOGLE_OAUTH_SCOPES,
    GoogleOAuthBootstrapError,
)
from google.oauth2.credentials import Credentials

REFRESH_TOKEN = "refresh-token-secret"
ID_TOKEN = "id-token-secret"
ACCESS_TOKEN = "access-token-secret"
CLIENT_ID = "google-client-id"
CLIENT_SECRET = "client-secret-value"
ACCOUNT_IDENTIFIER = "velox-account-1"
GOOGLE_EMAIL = "person@example.com"
CLIENT_SECRETS_FILE = "/safe/client-secrets.json"

SECRET_VALUES = (REFRESH_TOKEN, ID_TOKEN, ACCESS_TOKEN, CLIENT_SECRET)


class FakeAuthorizer:
    """Return deterministic credentials without browser, socket or HTTP activity."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def authorize(self, client_secrets_file: str, scopes: tuple[str, ...]) -> Credentials:
        self.calls.append((client_secrets_file, scopes))
        if self.failure is not None:
            raise self.failure
        return Credentials(
            token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            id_token=ID_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scopes=GOOGLE_OAUTH_SCOPES,
        )


class FakeIdentityVerifier:
    """Return deterministic verified claims without contacting Google."""

    def __init__(self, email: str = GOOGLE_EMAIL) -> None:
        self.email = email

    def verify(self, token: str, *, audience: str) -> dict[str, object]:
        return {"email": self.email, "email_verified": True}


class UnavailableCredentialStore:
    """Credential store whose backend always fails safely."""

    def store(
        self,
        reference: CredentialReference,
        material: CredentialMaterial,
        *,
        replace: bool = False,
    ) -> None:
        raise CredentialStoreBackendError()

    def get(self, reference: CredentialReference) -> CredentialMaterial | None:
        raise CredentialStoreBackendError()

    def delete(self, reference: CredentialReference) -> bool:
        raise CredentialStoreBackendError()


def credential_reference(
    account_identifier: str = ACCOUNT_IDENTIFIER,
) -> CredentialReference:
    return CredentialReference(
        namespace=GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
        account_identifier=account_identifier,
    )


def stored_material(**overrides: object) -> CredentialMaterial:
    fields: dict[str, object] = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "scopes": list(GOOGLE_OAUTH_SCOPES),
    }
    fields.update(overrides)
    return CredentialMaterial(json.dumps(fields))


@pytest.fixture
def credential_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryCredentialStore:
    """Replace the real macOS Keychain store so no default test touches it."""
    store = InMemoryCredentialStore()
    monkeypatch.setattr(
        google_oauth_cli,
        "MacOSKeychainCredentialStore",
        lambda: store,
    )
    return store


@pytest.fixture
def authorizer(monkeypatch: pytest.MonkeyPatch) -> FakeAuthorizer:
    """Replace the installed-app authorizer so no default test opens a browser."""
    fake = FakeAuthorizer()
    monkeypatch.setattr(
        google_oauth_cli,
        "InstalledAppGoogleOAuthAuthorizer",
        lambda: fake,
    )
    monkeypatch.setattr(
        google_oauth_cli,
        "GoogleIdTokenIdentityVerifier",
        FakeIdentityVerifier,
    )
    return fake


def connect_arguments(*extra: str) -> tuple[str, ...]:
    return (
        "connect",
        "--account-identifier",
        ACCOUNT_IDENTIFIER,
        "--expected-google-email",
        GOOGLE_EMAIL,
        "--client-secrets",
        CLIENT_SECRETS_FILE,
        *extra,
    )


def assert_no_secret_material(captured: str) -> None:
    for secret in SECRET_VALUES:
        assert secret not in captured
    assert "Authorization" not in captured
    assert "Bearer" not in captured


def test_connect_stores_credential_and_prints_only_safe_metadata(
    credential_store: InMemoryCredentialStore,
    authorizer: FakeAuthorizer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = google_oauth_cli.main(connect_arguments())

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == {
        "account_identifier": ACCOUNT_IDENTIFIER,
        "command": "connect",
        "credential_namespace": GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
        "scopes": list(GOOGLE_OAUTH_SCOPES),
        "status": "succeeded",
        "verified_google_email": GOOGLE_EMAIL,
    }
    assert_no_secret_material(captured.out + captured.err)
    assert credential_store.get(credential_reference()) is not None
    assert authorizer.calls == [(CLIENT_SECRETS_FILE, GOOGLE_OAUTH_SCOPES)]


def test_connect_persists_no_access_or_id_token(
    credential_store: InMemoryCredentialStore,
    authorizer: FakeAuthorizer,
) -> None:
    google_oauth_cli.main(connect_arguments())

    material = credential_store.get(credential_reference())
    assert material is not None
    persisted = json.loads(material.value)
    assert set(persisted) == {"client_id", "client_secret", "refresh_token", "scopes"}
    assert "access_token" not in persisted
    assert "id_token" not in persisted
    assert "token" not in persisted


def test_connect_rejects_existing_credential_without_replace(
    credential_store: InMemoryCredentialStore,
    authorizer: FakeAuthorizer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_store.store(credential_reference(), stored_material())

    exit_code = google_oauth_cli.main(connect_arguments())

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert json.loads(captured.err)["failure_code"] == "credential_already_exists"
    assert_no_secret_material(captured.out + captured.err)


def test_connect_replaces_existing_credential_when_explicitly_requested(
    credential_store: InMemoryCredentialStore,
    authorizer: FakeAuthorizer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_store.store(
        credential_reference(),
        stored_material(refresh_token="stale-refresh-token"),
    )

    exit_code = google_oauth_cli.main(connect_arguments("--replace"))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["status"] == "succeeded"
    material = credential_store.get(credential_reference())
    assert material is not None
    assert json.loads(material.value)["refresh_token"] == REFRESH_TOKEN


def test_connect_reports_account_mismatch_without_storing(
    credential_store: InMemoryCredentialStore,
    authorizer: FakeAuthorizer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        google_oauth_cli,
        "GoogleIdTokenIdentityVerifier",
        lambda: FakeIdentityVerifier(email="someone-else@example.com"),
    )

    exit_code = google_oauth_cli.main(connect_arguments())

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.err)["failure_code"] == "account_mismatch"
    assert credential_store.get(credential_reference()) is None
    assert_no_secret_material(captured.out + captured.err)


def test_connect_maps_bootstrap_failure_safely(
    credential_store: InMemoryCredentialStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        google_oauth_cli,
        "InstalledAppGoogleOAuthAuthorizer",
        lambda: FakeAuthorizer(failure=GoogleOAuthBootstrapError()),
    )
    monkeypatch.setattr(
        google_oauth_cli,
        "GoogleIdTokenIdentityVerifier",
        FakeIdentityVerifier,
    )

    exit_code = google_oauth_cli.main(connect_arguments())

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.err)["failure_code"] == "oauth_bootstrap_failed"
    assert credential_store.get(credential_reference()) is None


def test_unavailable_keychain_backend_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable() -> CredentialStore:
        raise CredentialStoreBackendError()

    monkeypatch.setattr(
        google_oauth_cli,
        "MacOSKeychainCredentialStore",
        unavailable,
    )

    exit_code = google_oauth_cli.main(connect_arguments())

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.err)["failure_code"] == "credential_store_unavailable"


def test_missing_required_arguments_return_usage_exit_code(
    credential_store: InMemoryCredentialStore,
) -> None:
    assert google_oauth_cli.main(("connect",)) == 2
    assert google_oauth_cli.main(()) == 2


def test_verify_reports_safe_shape_without_revealing_material(
    credential_store: InMemoryCredentialStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_store.store(credential_reference(), stored_material())

    exit_code = google_oauth_cli.main(
        ("verify", "--account-identifier", ACCOUNT_IDENTIFIER)
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "account_identifier": ACCOUNT_IDENTIFIER,
        "client_id_present": True,
        "client_secret_present": True,
        "command": "verify",
        "credential_namespace": GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
        "credential_present": True,
        "material_parses": True,
        "persisted_forbidden_fields": [],
        "refresh_token_present": True,
        "scopes": list(GOOGLE_OAUTH_SCOPES),
        "status": "succeeded",
    }
    assert_no_secret_material(captured.out + captured.err)


def test_verify_reports_missing_credential(
    credential_store: InMemoryCredentialStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = google_oauth_cli.main(
        ("verify", "--account-identifier", ACCOUNT_IDENTIFIER)
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.err)["failure_code"] == "credential_missing"


@pytest.mark.parametrize(
    "material",
    [
        CredentialMaterial("not-json"),
        CredentialMaterial(json.dumps(["client_id"])),
        CredentialMaterial(json.dumps({"client_id": CLIENT_ID})),
    ],
)
def test_verify_rejects_malformed_material(
    credential_store: InMemoryCredentialStore,
    capsys: pytest.CaptureFixture[str],
    material: CredentialMaterial,
) -> None:
    credential_store.store(credential_reference(), material)

    exit_code = google_oauth_cli.main(
        ("verify", "--account-identifier", ACCOUNT_IDENTIFIER)
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.err)["failure_code"] == "credential_malformed"
    assert_no_secret_material(captured.out + captured.err)


def test_verify_rejects_unexpected_scopes(
    credential_store: InMemoryCredentialStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_store.store(
        credential_reference(),
        stored_material(scopes=["https://www.googleapis.com/auth/calendar"]),
    )

    exit_code = google_oauth_cli.main(
        ("verify", "--account-identifier", ACCOUNT_IDENTIFIER)
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.err)["failure_code"] == "credential_malformed"


@pytest.mark.parametrize("account_identifier", ["", "   ", " padded "])
def test_verify_rejects_invalid_account_identifier(
    credential_store: InMemoryCredentialStore,
    capsys: pytest.CaptureFixture[str],
    account_identifier: str,
) -> None:
    exit_code = google_oauth_cli.main(
        ("verify", "--account-identifier", account_identifier)
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.err)["failure_code"] == "invalid_input"


def test_verify_resolves_only_the_exact_account_identifier(
    credential_store: InMemoryCredentialStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_store.store(credential_reference("other-account"), stored_material())

    exit_code = google_oauth_cli.main(
        ("verify", "--account-identifier", ACCOUNT_IDENTIFIER)
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.err)["failure_code"] == "credential_missing"


def test_unavailable_store_maps_verify_failure_safely(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        google_oauth_cli,
        "MacOSKeychainCredentialStore",
        UnavailableCredentialStore,
    )

    exit_code = google_oauth_cli.main(
        ("verify", "--account-identifier", ACCOUNT_IDENTIFIER)
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.err)["failure_code"] == "credential_store_unavailable"


class RecordingFlow:
    """Capture run_local_server keyword arguments without any real OAuth work."""

    last_kwargs: ClassVar[dict[str, object]] = {}
    created_with: ClassVar[tuple[str, tuple[str, ...]] | None] = None

    @classmethod
    def from_client_secrets_file(
        cls,
        client_secrets_file: str,
        scopes: tuple[str, ...],
    ) -> "RecordingFlow":
        cls.created_with = (client_secrets_file, scopes)
        return cls()

    def run_local_server(self, **kwargs: object) -> Credentials:
        RecordingFlow.last_kwargs = kwargs
        message = kwargs.get("authorization_prompt_message")
        assert isinstance(message, str)
        print(message.format(url="https://accounts.google.com/o/oauth2/auth?state=abc"))
        return Credentials(
            token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            id_token=ID_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scopes=GOOGLE_OAUTH_SCOPES,
        )


def test_no_browser_mode_emits_url_to_stderr_and_never_opens_a_browser(
    credential_store: InMemoryCredentialStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(google_oauth_cli, "InstalledAppFlow", RecordingFlow)
    monkeypatch.setattr(
        google_oauth_cli,
        "GoogleIdTokenIdentityVerifier",
        FakeIdentityVerifier,
    )

    exit_code = google_oauth_cli.main(connect_arguments("--no-browser"))

    captured = capsys.readouterr()
    assert exit_code == 0
    # The authorization prompt must not pollute stdout, which carries the JSON result.
    assert json.loads(captured.out)["status"] == "succeeded"
    assert google_oauth_cli._AUTHORIZATION_URL_PREFIX in captured.err
    assert "https://accounts.google.com/o/oauth2/auth" in captured.err
    assert RecordingFlow.last_kwargs["open_browser"] is False
    assert RecordingFlow.last_kwargs["host"] == "127.0.0.1"
    # Port 0 makes the OS pick a fresh port, so no run reuses a previous one.
    assert RecordingFlow.last_kwargs["port"] == 0
    assert RecordingFlow.last_kwargs["access_type"] == "offline"
    assert RecordingFlow.last_kwargs["prompt"] == "consent"
    assert RecordingFlow.created_with == (CLIENT_SECRETS_FILE, GOOGLE_OAUTH_SCOPES)
    assert_no_secret_material(captured.out + captured.err)


def test_default_connect_mode_uses_the_browser_opening_authorizer(
    credential_store: InMemoryCredentialStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[str] = []

    class Marker:
        def __init__(self) -> None:
            selected.append(type(self).__name__)

        def authorize(
            self,
            client_secrets_file: str,
            scopes: tuple[str, ...],
        ) -> Credentials:
            raise GoogleOAuthBootstrapError()

    monkeypatch.setattr(google_oauth_cli, "InstalledAppGoogleOAuthAuthorizer", Marker)
    monkeypatch.setattr(
        google_oauth_cli,
        "GoogleIdTokenIdentityVerifier",
        FakeIdentityVerifier,
    )

    google_oauth_cli.main(connect_arguments())

    assert selected == ["Marker"]
