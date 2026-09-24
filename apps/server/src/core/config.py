"""Application settings loaded from environment variables and .env.

All VELOX settings use the ``VELOX_`` environment prefix. Secrets (like the
API token) are read from the environment or a local .env file and must never
be committed to the repository or stored in Notion.
"""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide VELOX Server settings."""

    model_config = SettingsConfigDict(
        env_prefix="VELOX_",
        env_file=".env",
        extra="ignore",
    )

    api_token: str | None = None
    """Bearer token required for mutating API endpoints. None disables auth
    (local development only — set a token before exposing the server)."""

    calendar_agenda_live: bool = False
    """Opt in to stored Google credentials and live read-only agenda execution."""

    calendar_agenda_resolver: str = "bounded"
    """Calendar agenda query resolver mode: bounded or ollama."""

    ollama_base_url: str = "http://127.0.0.1:11434"
    """Loopback-only Ollama server base URL."""

    ollama_model: str | None = None
    """Explicit local Ollama model name used by the opt-in resolver."""

    log_level: str = "INFO"
    """Root logging level: DEBUG, INFO, WARNING, ERROR or CRITICAL."""

    max_transient_retries: int = 3
    """How many times a transiently failed action is re-queued."""

    @model_validator(mode="after")
    def validate_calendar_agenda_resolver(self) -> "Settings":
        if self.calendar_agenda_resolver not in {"bounded", "ollama"}:
            raise ValueError("VELOX_CALENDAR_AGENDA_RESOLVER must be bounded or ollama")
        if self.calendar_agenda_resolver == "ollama" and not (self.ollama_model or "").strip():
            raise ValueError("VELOX_OLLAMA_MODEL is required when resolver mode is ollama")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()
