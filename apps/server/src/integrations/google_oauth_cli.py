"""Explicit local command around the existing installed Google OAuth bootstrap."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from enum import StrEnum
from typing import cast

from apps.server.src.core.credentials import (
    CredentialAlreadyExistsError,
    CredentialReference,
    CredentialStore,
    CredentialStoreBackendError,
)
from apps.server.src.integrations.google_oauth import (
    GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
    GOOGLE_OAUTH_SCOPES,
    GoogleIdTokenIdentityVerifier,
    GoogleOAuthAccountMismatchError,
    GoogleOAuthBootstrapError,
    GoogleOAuthBootstrapService,
    GoogleOAuthConnectRequest,
    InstalledAppGoogleOAuthAuthorizer,
)
from apps.server.src.integrations.keyring_credentials import (
    MacOSKeychainCredentialStore,
)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

_PROGRAM_NAME = "python -m apps.server.src.integrations.google_oauth_cli"
# Printed to stderr in --no-browser mode so the operator can open the URL
# deliberately. The authorization URL is not secret material: it carries the
# public client id, redirect URI, requested scopes and a CSRF state nonce.
_AUTHORIZATION_URL_PREFIX = "VELOX authorization URL: "
# Mirrors the material serialized by GoogleOAuthBootstrapService; the CLI only ever
# reports presence of these keys and never their values.
_EXPECTED_CREDENTIAL_FIELDS = frozenset(
    {"client_id", "client_secret", "refresh_token", "scopes"}
)
_FORBIDDEN_PERSISTED_FIELDS = ("access_token", "id_token", "token")


class GoogleOAuthCliFailureCode(StrEnum):
    """Safe failure codes for the explicit local OAuth command boundary."""

    INVALID_INPUT = "invalid_input"
    CREDENTIAL_STORE_UNAVAILABLE = "credential_store_unavailable"
    CREDENTIAL_ALREADY_EXISTS = "credential_already_exists"
    ACCOUNT_MISMATCH = "account_mismatch"
    OAUTH_BOOTSTRAP_FAILED = "oauth_bootstrap_failed"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_MALFORMED = "credential_malformed"


_FAILURE_MESSAGES = {
    GoogleOAuthCliFailureCode.INVALID_INPUT: "OAuth command input is invalid",
    GoogleOAuthCliFailureCode.CREDENTIAL_STORE_UNAVAILABLE: (
        "the macOS Keychain credential store is unavailable"
    ),
    GoogleOAuthCliFailureCode.CREDENTIAL_ALREADY_EXISTS: (
        "a credential already exists for this account; rerun with --replace to replace it"
    ),
    GoogleOAuthCliFailureCode.ACCOUNT_MISMATCH: (
        "the authenticated Google account does not match the expected email"
    ),
    GoogleOAuthCliFailureCode.OAUTH_BOOTSTRAP_FAILED: (
        "Google OAuth bootstrap did not complete"
    ),
    GoogleOAuthCliFailureCode.CREDENTIAL_MISSING: (
        "no stored Google credential exists for this account"
    ),
    GoogleOAuthCliFailureCode.CREDENTIAL_MALFORMED: (
        "the stored Google credential is not valid refresh-capable material"
    ),
}


class GoogleOAuthCliError(Exception):
    """Safe command failure that retains no third-party exception or secret."""

    def __init__(self, code: GoogleOAuthCliFailureCode) -> None:
        self.code = code
        super().__init__(_FAILURE_MESSAGES[code])

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible failure containing only safe fields."""
        return {
            "status": "failed",
            "failure_code": self.code.value,
            "message": str(self),
        }


class ManualOpenGoogleOAuthAuthorizer:
    """Emit the authorization URL instead of auto-opening a browser.

    `run_local_server` prints its authorization prompt to stdout. Stdout is
    reserved for the single safe JSON result, so the prompt is redirected to
    stderr for the duration of the flow. Loopback host and random port
    selection are unchanged, so each run binds a fresh port.
    """

    def authorize(
        self,
        client_secrets_file: str,
        scopes: tuple[str, ...],
    ) -> Credentials:
        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes=scopes)
        with contextlib.redirect_stdout(sys.stderr):
            return cast(
                Credentials,
                flow.run_local_server(
                    host="127.0.0.1",
                    port=0,
                    open_browser=False,
                    access_type="offline",
                    prompt="consent",
                    authorization_prompt_message=_AUTHORIZATION_URL_PREFIX + "{url}",
                ),
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROGRAM_NAME,
        description=(
            "Connect or verify one explicit VELOX account against one Google account. "
            "Never prints tokens or client secrets."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser(
        "connect",
        help="run the installed-app OAuth flow and store refresh-capable material",
    )
    connect.add_argument("--account-identifier", required=True)
    connect.add_argument("--expected-google-email", required=True)
    connect.add_argument("--client-secrets", required=True)
    connect.add_argument(
        "--replace",
        action="store_true",
        help="explicitly allow replacing an existing stored credential",
    )
    connect.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorization URL to stderr instead of opening a browser",
    )

    verify = subparsers.add_parser(
        "verify",
        help="report safe presence and shape of stored material without revealing it",
    )
    verify.add_argument("--account-identifier", required=True)

    return parser


def _connect(
    namespace: argparse.Namespace,
    credential_store: CredentialStore,
) -> dict[str, object]:
    request = GoogleOAuthConnectRequest(
        client_secrets_file=namespace.client_secrets,
        account_identifier=namespace.account_identifier,
        expected_google_email=namespace.expected_google_email,
        replace=bool(namespace.replace),
    )
    authorizer = (
        ManualOpenGoogleOAuthAuthorizer()
        if bool(getattr(namespace, "no_browser", False))
        else InstalledAppGoogleOAuthAuthorizer()
    )
    service = GoogleOAuthBootstrapService(
        authorizer=authorizer,
        identity_verifier=GoogleIdTokenIdentityVerifier(),
        credential_store=credential_store,
    )
    try:
        result = service.connect(request)
    except CredentialAlreadyExistsError:
        raise GoogleOAuthCliError(
            GoogleOAuthCliFailureCode.CREDENTIAL_ALREADY_EXISTS
        ) from None
    except CredentialStoreBackendError:
        raise GoogleOAuthCliError(
            GoogleOAuthCliFailureCode.CREDENTIAL_STORE_UNAVAILABLE
        ) from None
    except GoogleOAuthAccountMismatchError:
        raise GoogleOAuthCliError(
            GoogleOAuthCliFailureCode.ACCOUNT_MISMATCH
        ) from None
    except GoogleOAuthBootstrapError:
        raise GoogleOAuthCliError(
            GoogleOAuthCliFailureCode.OAUTH_BOOTSTRAP_FAILED
        ) from None

    return {
        "status": "succeeded",
        "command": "connect",
        "credential_namespace": result.credential_reference.namespace,
        "account_identifier": result.account_identifier,
        "verified_google_email": result.verified_google_email,
        "scopes": list(result.scopes),
    }


def _verify(
    namespace: argparse.Namespace,
    credential_store: CredentialStore,
) -> dict[str, object]:
    account_identifier = namespace.account_identifier
    if (
        not isinstance(account_identifier, str)
        or not account_identifier.strip()
        or account_identifier != account_identifier.strip()
    ):
        raise GoogleOAuthCliError(GoogleOAuthCliFailureCode.INVALID_INPUT)

    reference = CredentialReference(
        namespace=GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
        account_identifier=account_identifier,
    )
    try:
        material = credential_store.get(reference)
    except CredentialStoreBackendError:
        raise GoogleOAuthCliError(
            GoogleOAuthCliFailureCode.CREDENTIAL_STORE_UNAVAILABLE
        ) from None
    if material is None:
        raise GoogleOAuthCliError(GoogleOAuthCliFailureCode.CREDENTIAL_MISSING)

    parse_failed = False
    parsed: object = None
    try:
        parsed = json.loads(material.value)
    except Exception:
        parse_failed = True
    if parse_failed or not isinstance(parsed, dict):
        raise GoogleOAuthCliError(GoogleOAuthCliFailureCode.CREDENTIAL_MALFORMED)

    if set(parsed) != _EXPECTED_CREDENTIAL_FIELDS:
        raise GoogleOAuthCliError(GoogleOAuthCliFailureCode.CREDENTIAL_MALFORMED)
    for field_name in ("client_id", "client_secret", "refresh_token"):
        value = parsed.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise GoogleOAuthCliError(GoogleOAuthCliFailureCode.CREDENTIAL_MALFORMED)

    scopes = parsed.get("scopes")
    if (
        not isinstance(scopes, list)
        or not all(isinstance(scope, str) for scope in scopes)
        or tuple(scopes) != GOOGLE_OAUTH_SCOPES
    ):
        raise GoogleOAuthCliError(GoogleOAuthCliFailureCode.CREDENTIAL_MALFORMED)

    return {
        "status": "succeeded",
        "command": "verify",
        "credential_namespace": GOOGLE_OAUTH_CREDENTIAL_NAMESPACE,
        "account_identifier": account_identifier,
        "credential_present": True,
        "material_parses": True,
        "refresh_token_present": True,
        "client_id_present": True,
        "client_secret_present": True,
        "scopes": list(GOOGLE_OAUTH_SCOPES),
        "persisted_forbidden_fields": [
            field_name
            for field_name in _FORBIDDEN_PERSISTED_FIELDS
            if field_name in parsed
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicit local OAuth command and print one safe JSON result."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        namespace = parser.parse_args(arguments)
    except SystemExit:
        return 2

    try:
        credential_store: CredentialStore = MacOSKeychainCredentialStore()
    except CredentialStoreBackendError:
        error = GoogleOAuthCliError(
            GoogleOAuthCliFailureCode.CREDENTIAL_STORE_UNAVAILABLE
        )
        print(json.dumps(error.as_dict(), sort_keys=True), file=sys.stderr)
        return 1

    try:
        if namespace.command == "connect":
            result = _connect(namespace, credential_store)
        else:
            result = _verify(namespace, credential_store)
    except GoogleOAuthCliError as error:
        print(json.dumps(error.as_dict(), sort_keys=True), file=sys.stderr)
        return 2 if error.code == GoogleOAuthCliFailureCode.INVALID_INPUT else 1
    except Exception:
        safe_error = GoogleOAuthCliError(
            GoogleOAuthCliFailureCode.OAUTH_BOOTSTRAP_FAILED
        )
        print(json.dumps(safe_error.as_dict(), sort_keys=True), file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
