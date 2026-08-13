"""macOS Keychain credential-store adapter through Python keyring."""

from typing import Protocol

import keyring
from apps.server.src.core.credentials import (
    CredentialAlreadyExistsError,
    CredentialMaterial,
    CredentialReference,
    CredentialStoreBackendError,
)
from keyring.backends import macOS


class KeyringBackend(Protocol):
    """Injectable subset of the keyring backend API used by VELOX."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class MacOSKeychainCredentialStore:
    """Persist opaque VELOX credentials in the selected macOS Keychain backend."""

    def __init__(self, backend: KeyringBackend | None = None) -> None:
        if backend is not None:
            self._backend = backend
            return

        selection_failed = False
        try:
            selected_backend = keyring.get_keyring()
            priority = selected_backend.priority
        except Exception:
            selection_failed = True

        if selection_failed:
            raise CredentialStoreBackendError()
        if not isinstance(selected_backend, macOS.Keyring):
            raise CredentialStoreBackendError()
        if not isinstance(priority, int | float) or isinstance(priority, bool) or priority <= 0:
            raise CredentialStoreBackendError()

        self._backend = selected_backend

    def __repr__(self) -> str:
        """Return a representation that never includes backend state or credentials."""
        return "MacOSKeychainCredentialStore(<redacted>)"

    def store(
        self,
        reference: CredentialReference,
        material: CredentialMaterial,
        *,
        replace: bool = False,
    ) -> None:
        """Store material, requiring an explicit replacement request when present.

        The existence check and write cannot be atomic through keyring. Closing that
        check-then-write race remains technical debt for the credential-store contract.
        """
        existing = self._get_password(reference)
        if existing is not None and not replace:
            raise CredentialAlreadyExistsError(reference)

        write_failed = False
        try:
            self._backend.set_password(
                reference.namespace,
                reference.account_identifier,
                material.value,
            )
        except Exception:
            write_failed = True

        if write_failed:
            raise CredentialStoreBackendError()

    def get(self, reference: CredentialReference) -> CredentialMaterial | None:
        """Return stored material, or None when the Keychain entry is absent."""
        value = self._get_password(reference)
        if value is None:
            return None
        return CredentialMaterial(value)

    def delete(self, reference: CredentialReference) -> bool:
        """Delete one credential and report whether it existed."""
        if self._get_password(reference) is None:
            return False

        delete_failed = False
        try:
            self._backend.delete_password(
                reference.namespace,
                reference.account_identifier,
            )
        except Exception:
            delete_failed = True

        if delete_failed:
            raise CredentialStoreBackendError()
        return True

    def _get_password(self, reference: CredentialReference) -> str | None:
        read_failed = False
        value: str | None = None
        try:
            value = self._backend.get_password(
                reference.namespace,
                reference.account_identifier,
            )
        except Exception:
            read_failed = True

        if read_failed:
            raise CredentialStoreBackendError()
        return value
