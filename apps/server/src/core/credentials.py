"""Vendor-neutral credential storage contract and deterministic test fake."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CredentialReference:
    """Explicit VELOX credential identity, separate from provider routing."""

    namespace: str
    account_identifier: str

    def __post_init__(self) -> None:
        _validate_identifier(self.namespace, "namespace")
        _validate_identifier(self.account_identifier, "account_identifier")


@dataclass(frozen=True, repr=False)
class CredentialMaterial:
    """Opaque credential material whose public representation is always redacted."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("credential material must be a non-empty string")

    def __repr__(self) -> str:
        """Return a representation that never includes credential material."""
        return "CredentialMaterial(<redacted>)"


class CredentialAlreadyExistsError(Exception):
    """Raised when an existing credential would be replaced unintentionally."""

    def __init__(self, reference: CredentialReference) -> None:
        super().__init__(
            "credential already exists for the requested namespace and account"
        )
        self.reference = reference


@runtime_checkable
class CredentialStore(Protocol):
    """Storage boundary for opaque credentials addressed by explicit reference."""

    def store(
        self,
        reference: CredentialReference,
        material: CredentialMaterial,
        *,
        replace: bool = False,
    ) -> None:
        """Store material, requiring explicit permission to replace a value."""
        ...

    def get(self, reference: CredentialReference) -> CredentialMaterial | None:
        """Return stored material, or None when the reference is unknown."""
        ...

    def delete(self, reference: CredentialReference) -> bool:
        """Delete one credential and report whether it existed."""
        ...


class InMemoryCredentialStore:
    """Deterministic process-local credential store for tests."""

    def __init__(self) -> None:
        self._credentials: dict[CredentialReference, CredentialMaterial] = {}

    def store(
        self,
        reference: CredentialReference,
        material: CredentialMaterial,
        *,
        replace: bool = False,
    ) -> None:
        """Store material without silently replacing an existing value."""
        if reference in self._credentials and not replace:
            raise CredentialAlreadyExistsError(reference)
        self._credentials[reference] = material

    def get(self, reference: CredentialReference) -> CredentialMaterial | None:
        """Return stored material, or None when the reference is unknown."""
        return self._credentials.get(reference)

    def delete(self, reference: CredentialReference) -> bool:
        """Delete only the requested credential and report whether it existed."""
        return self._credentials.pop(reference, None) is not None


def _validate_identifier(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"credential reference {field_name} must be a non-blank string")
