"""Tests for the vendor-neutral credential store contract and fake."""

import builtins
import os
import socket
import subprocess
import urllib.request

import pytest
from apps.server.src.core.credentials import (
    CredentialAlreadyExistsError,
    CredentialMaterial,
    CredentialReference,
    CredentialStore,
    InMemoryCredentialStore,
)


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
