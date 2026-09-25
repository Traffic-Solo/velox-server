"""Regression coverage: default tests never inherit live opt-in settings."""

from pathlib import Path
from unittest.mock import patch

import pytest
from apps.server.src import main
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.main import app
from fastapi.testclient import TestClient

LIVE_DOTENV = (
    "VELOX_CALENDAR_AGENDA_LIVE=true\n"
    "VELOX_CALENDAR_AGENDA_RESOLVER=ollama\n"
    "VELOX_OLLAMA_MODEL=local-model\n"
)


@pytest.fixture
def live_dotenv_in_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(LIVE_DOTENV)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


@pytest.mark.usefixtures("live_dotenv_in_cwd")
def test_local_dotenv_does_not_leak_into_test_settings() -> None:
    settings = get_settings()

    assert settings.calendar_agenda_live is False
    assert settings.calendar_agenda_resolver == "bounded"
    assert settings.ollama_model is None


@pytest.mark.usefixtures("live_dotenv_in_cwd")
def test_default_lifespan_never_composes_live_calendar() -> None:
    container = ApplicationContainer()
    previous = container.calendar_agenda_command_service
    with (
        patch.object(main, "get_container", return_value=container),
        patch.object(
            main, "live_calendar_agenda_command_service",
            side_effect=AssertionError("live Calendar composed in a default test"),
        ) as factory,
        TestClient(app),
    ):
        assert container.calendar_agenda_command_service is previous

    assert factory.call_count == 0


def test_explicit_environment_opt_in_is_still_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VELOX_CALENDAR_AGENDA_LIVE", "true")
    get_settings.cache_clear()

    assert get_settings().calendar_agenda_live is True
