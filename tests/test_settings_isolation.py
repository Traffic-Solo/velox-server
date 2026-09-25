"""Regression coverage: default tests never inherit live opt-in settings."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from apps.server.src import main
from apps.server.src.core.config import Settings, get_settings
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.main import app
from fastapi.testclient import TestClient

LIVE_DOTENV = (
    "VELOX_CALENDAR_AGENDA_LIVE=true\n"
    "VELOX_CALENDAR_AGENDA_RESOLVER=ollama\n"
    "VELOX_OLLAMA_MODEL=local-model\n"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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


def settings_from_dotenv(monkeypatch: pytest.MonkeyPatch, path: Path) -> Settings:
    monkeypatch.setitem(Settings.model_config, "env_file", path)
    return Settings()


def test_committed_default_example_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from_dotenv(monkeypatch, REPOSITORY_ROOT / ".env.example")

    assert settings.calendar_agenda_live is False
    assert settings.calendar_agenda_resolver == "bounded"


def test_committed_live_example_is_an_explicit_valid_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_dotenv(monkeypatch, REPOSITORY_ROOT / ".env.live.example")

    assert settings.calendar_agenda_live is True
    assert settings.calendar_agenda_resolver == "ollama"
    assert settings.ollama_model


def test_process_environment_overrides_local_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`uv run --env-file .env.live` relies on process values winning over .env."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(LIVE_DOTENV)
    monkeypatch.setenv("VELOX_CALENDAR_AGENDA_LIVE", "false")
    monkeypatch.setenv("VELOX_CALENDAR_AGENDA_RESOLVER", "bounded")

    settings = settings_from_dotenv(monkeypatch, dotenv)

    assert settings.calendar_agenda_live is False
    assert settings.calendar_agenda_resolver == "bounded"


@pytest.mark.parametrize("live", [False, True])
def test_startup_warns_only_when_live_opt_ins_are_enabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, live: bool,
) -> None:
    if live:
        monkeypatch.setenv("VELOX_CALENDAR_AGENDA_LIVE", "true")
        monkeypatch.setenv("VELOX_CALENDAR_AGENDA_RESOLVER", "ollama")
        monkeypatch.setenv("VELOX_OLLAMA_MODEL", "local-model")
        get_settings.cache_clear()
    with (
        patch.object(main, "get_container", return_value=ApplicationContainer()),
        patch.object(main, "live_calendar_agenda_command_service"),
        patch.object(main, "calendar_agenda_semantic_resolver"),
        caplog.at_level(logging.WARNING, logger=main.__name__),
        TestClient(app),
    ):
        pass

    warnings = [record.getMessage() for record in caplog.records]
    if live:
        assert any("VELOX_CALENDAR_AGENDA_LIVE" in message for message in warnings)
        assert any("VELOX_CALENDAR_AGENDA_RESOLVER" in message for message in warnings)
        assert "local-model" not in caplog.text
    else:
        assert warnings == []
