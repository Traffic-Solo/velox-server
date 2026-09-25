"""Suite-wide isolation of process settings from the developer environment."""

from collections.abc import Iterator

import pytest
from apps.server.src.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start every test from code defaults, never from a local .env or shell.

    A developer's gitignored .env may opt in to live Calendar composition
    (stored Keychain credentials, Google transport, a local Ollama resolver).
    Default tests must stay offline, so settings come only from environment
    values a test sets explicitly with ``monkeypatch.setenv``.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    prefix = Settings.model_config.get("env_prefix", "")
    for field in Settings.model_fields:
        monkeypatch.delenv(f"{prefix}{field}".upper(), raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
