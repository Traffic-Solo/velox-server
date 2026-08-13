"""Tests for the vendor-neutral credential store contract and fake."""

import builtins
import os
import socket
import subprocess
import urllib.request
from collections.abc import Callable

import keyring
import pytest
from apps.server.src.core.credentials import (
    CredentialAlreadyExistsError,
    CredentialMaterial,
    CredentialReference,
    CredentialStore,
    CredentialStoreBackendError,
    InMemoryCredentialStore,
)
from apps.server.src.integrations.keyring_credentials import (
    MacOSKeychainCredentialStore,
)


class FakeKeyringBackend:
    """Deterministic injected keyring boundary with no system Keychain access."""

    def __init__(self) -> None:
        self.credentials: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.failures: dict[str, Exception] = {}

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get_password", service, username))
        self._raise_failure("get_password")
        return self.credentials.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append(("set_password", service, username, password))
        self._raise_failure("set_password")
        self.credentials[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete_password", service, username))
        self._raise_failure("delete_password")
        del self.credentials[(service, username)]

    def _raise_failure(self, operation: str) -> None:
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure


def credential_reference(account_identifier: str) -> CredentialReference:
    return CredentialReference(
        namespace="velox.oauth",
        account_identifier=account_identifier,
    )


def test_store_and_retrieve_by_explicit_reference_and_account() -> None:
    store = InMemoryCredentialStore()
    reference = credential_reference("account-1")
    material = CredentialMaterial("opaque-credential-material")

    store.store(reference, material)

    assert isinstance(store, CredentialStore)
    assert store.get(reference) == material


def test_distinct_accounts_remain_isolated() -> None:
    store = InMemoryCredentialStore()
    account_1 = credential_reference("account-1")
    account_2 = credential_reference("account-2")
    store.store(account_1, CredentialMaterial("credential-1"))
    store.store(account_2, CredentialMaterial("credential-2"))

    assert store.get(account_1) == CredentialMaterial("credential-1")
    assert store.get(account_2) == CredentialMaterial("credential-2")


def test_missing_credential_returns_none() -> None:
    store = InMemoryCredentialStore()

    assert store.get(credential_reference("missing-account")) is None
    assert store.delete(credential_reference("missing-account")) is False


def test_existing_credential_requires_intentional_replacement() -> None:
    store = InMemoryCredentialStore()
    reference = credential_reference("account-1")
    original = CredentialMaterial("original-material")
    replacement = CredentialMaterial("replacement-material")
    store.store(reference, original)

    with pytest.raises(CredentialAlreadyExistsError):
        store.store(reference, replacement)

    assert store.get(reference) == original

    store.store(reference, replacement, replace=True)

    assert store.get(reference) == replacement


def test_deletion_removes_only_the_requested_credential() -> None:
    store = InMemoryCredentialStore()
    account_1 = credential_reference("account-1")
    account_2 = credential_reference("account-2")
    material_1 = CredentialMaterial("credential-1")
    material_2 = CredentialMaterial("credential-2")
    store.store(account_1, material_1)
    store.store(account_2, material_2)

    assert store.delete(account_1) is True
    assert store.get(account_1) is None
    assert store.get(account_2) == material_2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("namespace", ""),
        ("namespace", "   "),
        ("namespace", None),
        ("account_identifier", ""),
        ("account_identifier", "   "),
        ("account_identifier", None),
    ],
)
def test_invalid_reference_identifiers_fail_deterministically(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "namespace": "velox.oauth",
        "account_identifier": "account-1",
    }
    values[field] = value

    with pytest.raises(
        ValueError,
        match=f"credential reference {field} must be a non-blank string",
    ):
        CredentialReference(**values)  # type: ignore[arg-type]


def test_secret_material_is_redacted_from_representations_errors_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "must-never-be-exposed"
    store = InMemoryCredentialStore()
    reference = credential_reference("account-1")
    material = CredentialMaterial(secret)
    store.store(reference, material)

    with pytest.raises(CredentialAlreadyExistsError) as raised:
        store.store(reference, material)

    exposed_values = (
        repr(material),
        repr(store),
        str(raised.value),
        repr(raised.value),
        repr(raised.value.__dict__),
        caplog.text,
    )
    assert all(secret not in exposed for exposed in exposed_values)
    assert not hasattr(raised.value, "metadata")


def test_fake_store_performs_no_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_external_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("external I/O is not allowed")

    monkeypatch.setattr(builtins, "open", fail_external_io)
    monkeypatch.setattr(os, "open", fail_external_io)
    monkeypatch.setattr(socket, "socket", fail_external_io)
    monkeypatch.setattr(subprocess, "run", fail_external_io)
    monkeypatch.setattr(urllib.request, "urlopen", fail_external_io)

    store = InMemoryCredentialStore()
    reference = credential_reference("account-1")
    material = CredentialMaterial("opaque-material")

    store.store(reference, material)
    assert store.get(reference) == material
    store.store(reference, CredentialMaterial("replacement"), replace=True)
    assert store.delete(reference) is True


def test_keyring_store_satisfies_contract_and_maps_reference_fields() -> None:
    backend = FakeKeyringBackend()
    store = MacOSKeychainCredentialStore(backend)
    reference = CredentialReference(
        namespace="velox.calendar.oauth",
        account_identifier="calendar-account-1",
    )
    material = CredentialMaterial("opaque-material")

    store.store(reference, material)

    assert isinstance(store, CredentialStore)
    assert backend.calls == [
        ("get_password", "velox.calendar.oauth", "calendar-account-1"),
        (
            "set_password",
            "velox.calendar.oauth",
            "calendar-account-1",
            "opaque-material",
        ),
    ]
    assert store.get(reference) == material


def test_keyring_store_keeps_distinct_references_isolated() -> None:
    backend = FakeKeyringBackend()
    store = MacOSKeychainCredentialStore(backend)
    reference_1 = CredentialReference("velox.oauth", "account-1")
    reference_2 = CredentialReference("velox.oauth", "account-2")

    store.store(reference_1, CredentialMaterial("credential-1"))
    store.store(reference_2, CredentialMaterial("credential-2"))

    assert store.get(reference_1) == CredentialMaterial("credential-1")
    assert store.get(reference_2) == CredentialMaterial("credential-2")


def test_keyring_store_requires_explicit_replacement() -> None:
    backend = FakeKeyringBackend()
    store = MacOSKeychainCredentialStore(backend)
    reference = credential_reference("account-1")
    original = CredentialMaterial("original-material")
    replacement = CredentialMaterial("replacement-material")
    store.store(reference, original)

    with pytest.raises(CredentialAlreadyExistsError):
        store.store(reference, replacement)

    assert store.get(reference) == original

    store.store(reference, replacement, replace=True)

    assert store.get(reference) == replacement


def test_keyring_store_missing_get_and_delete_do_not_call_delete() -> None:
    backend = FakeKeyringBackend()
    store = MacOSKeychainCredentialStore(backend)
    reference = credential_reference("missing-account")

    assert store.get(reference) is None
    assert store.delete(reference) is False
    assert not any(call[0] == "delete_password" for call in backend.calls)


def test_keyring_store_successful_delete_returns_true() -> None:
    backend = FakeKeyringBackend()
    store = MacOSKeychainCredentialStore(backend)
    reference = credential_reference("account-1")
    store.store(reference, CredentialMaterial("opaque-material"))

    assert store.delete(reference) is True
    assert store.get(reference) is None


@pytest.mark.parametrize(
    ("operation", "exercise"),
    [
        ("get_password", lambda store, reference: store.get(reference)),
        (
            "set_password",
            lambda store, reference: store.store(
                reference,
                CredentialMaterial("must-never-be-exposed"),
            ),
        ),
        (
            "delete_password",
            lambda store, reference: store.delete(reference),
        ),
    ],
)
def test_keyring_failures_become_safe_velox_failures(
    operation: str,
    exercise: Callable[[MacOSKeychainCredentialStore, CredentialReference], object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "must-never-be-exposed"
    backend = FakeKeyringBackend()
    reference = credential_reference("account-1")
    if operation == "delete_password":
        backend.credentials[(reference.namespace, reference.account_identifier)] = secret
    backend.failures[operation] = keyring.errors.KeyringError(f"backend failed: {secret}")
    store = MacOSKeychainCredentialStore(backend)

    with pytest.raises(CredentialStoreBackendError) as raised:
        exercise(store, reference)

    exposed_values = (
        str(raised.value),
        repr(raised.value),
        repr(raised.value.__dict__),
        repr(store),
        caplog.text,
    )
    assert all(secret not in exposed for exposed in exposed_values)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert not hasattr(raised.value, "metadata")


def test_default_keyring_selection_fails_closed_for_non_macos_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_backend() -> keyring.backend.KeyringBackend:
        return keyring.backends.null.Keyring()

    monkeypatch.setattr(keyring, "get_keyring", unexpected_backend)

    with pytest.raises(CredentialStoreBackendError):
        MacOSKeychainCredentialStore()
