"""Application settings loaded from environment variables and .env.

All VELOX settings use the ``VELOX_`` environment prefix. Secrets (like the
API token) are read from the environment or a local .env file and must never
be committed to the repository or stored in Notion.
"""

from functools import lru_cache

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

    log_level: str = "INFO"
    """Root logging level: DEBUG, INFO, WARNING, ERROR or CRITICAL."""

    max_transient_retries: int = 3
    """How many times a transiently failed action is re-queued."""


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()
