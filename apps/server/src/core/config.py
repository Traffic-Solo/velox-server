"""Application settings loaded from environment variables and .env.

All VELOX settings use the ``VELOX_`` environment prefix. Secrets (like the
API token) are read from the environment or a local .env file and must never
be committed to the repository or stored in Notion.
"""

from functools import lru_cache

from pydantic import field_validator, model_validator
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

    @field_validator("api_token")
    @classmethod
    def blank_api_token_disables_auth(cls, value: str | None) -> str | None:
        """Treat an empty or whitespace-only token as unset; keep others verbatim."""
        if value is None or not value.strip():
            return None
        return value

    calendar_agenda_live: bool = False
    """Opt in to stored Google credentials and live read-only agenda execution."""

    calendar_agenda_resolver: str = "bounded"
    """Calendar agenda query resolver mode: bounded or ollama."""

    ollama_base_url: str = "http://127.0.0.1:11434"
    """Loopback-only Ollama server base URL."""

    ollama_model: str | None = None
    """Explicit local Ollama model name used by the opt-in resolver."""

    software_engineering_provider: str = "disabled"
    """Software Engineering worker provider: disabled (default) or claude_code."""

    software_engineering_workspace: str | None = None
    """Trusted git repository the Software Engineering worker may branch from."""

    software_engineering_timeout_seconds: int = 1800
    """Process-level bound for one Software Engineering worker invocation."""

    claude_code_executable: str = "claude"
    """Claude Code CLI executable name or path (resolved on PATH when a name)."""

    software_engineering_promotion_enabled: bool = False
    """Explicit opt-in to VELOX-owned commit, push and pull-request promotion."""

    software_engineering_promotion_remote: str = "origin"
    """Trusted git remote used only by the VELOX promotion service."""

    software_engineering_promotion_base_branch: str = "main"
    """Trusted pull-request base branch for VELOX promotion."""

    software_engineering_promotion_author_name: str = "VELOX"
    """Git author/committer name for VELOX-owned promotion commits."""

    software_engineering_promotion_author_email: str = "velox@localhost"
    """Git author/committer email for VELOX-owned promotion commits."""

    github_cli_executable: str = "gh"
    """GitHub CLI executable used only by the pull-request publisher."""

    log_level: str = "INFO"
    """Root logging level: DEBUG, INFO, WARNING, ERROR or CRITICAL."""

    max_transient_retries: int = 3
    """How many times a transiently failed action is re-queued."""

    @model_validator(mode="after")
    def validate_opt_in_settings(self) -> "Settings":
        if self.calendar_agenda_resolver not in {"bounded", "ollama"}:
            raise ValueError("VELOX_CALENDAR_AGENDA_RESOLVER must be bounded or ollama")
        if self.calendar_agenda_resolver == "ollama" and not (self.ollama_model or "").strip():
            raise ValueError("VELOX_OLLAMA_MODEL is required when resolver mode is ollama")
        if self.software_engineering_provider not in {"disabled", "claude_code"}:
            raise ValueError(
                "VELOX_SOFTWARE_ENGINEERING_PROVIDER must be disabled or claude_code",
            )
        if self.software_engineering_provider != "disabled" and not (
            self.software_engineering_workspace or ""
        ).strip():
            raise ValueError(
                "VELOX_SOFTWARE_ENGINEERING_WORKSPACE is required when a provider is enabled",
            )
        if self.software_engineering_timeout_seconds < 1:
            raise ValueError("VELOX_SOFTWARE_ENGINEERING_TIMEOUT_SECONDS must be positive")
        if self.software_engineering_promotion_enabled and (
            self.software_engineering_provider == "disabled"
        ):
            raise ValueError(
                "VELOX_SOFTWARE_ENGINEERING_PROMOTION_ENABLED requires a worker provider",
            )
        trusted_promotion_values = {
            "VELOX_SOFTWARE_ENGINEERING_PROMOTION_REMOTE":
                self.software_engineering_promotion_remote,
            "VELOX_SOFTWARE_ENGINEERING_PROMOTION_BASE_BRANCH":
                self.software_engineering_promotion_base_branch,
            "VELOX_SOFTWARE_ENGINEERING_PROMOTION_AUTHOR_NAME":
                self.software_engineering_promotion_author_name,
            "VELOX_SOFTWARE_ENGINEERING_PROMOTION_AUTHOR_EMAIL":
                self.software_engineering_promotion_author_email,
            "VELOX_GITHUB_CLI_EXECUTABLE": self.github_cli_executable,
        }
        for setting, value in trusted_promotion_values.items():
            if not value.strip():
                raise ValueError(f"{setting} must not be blank")
        if self.software_engineering_promotion_remote.startswith("-"):
            raise ValueError(
                "VELOX_SOFTWARE_ENGINEERING_PROMOTION_REMOTE must not start with '-'"
            )
        if self.software_engineering_promotion_base_branch.startswith("-"):
            raise ValueError(
                "VELOX_SOFTWARE_ENGINEERING_PROMOTION_BASE_BRANCH must not start with '-'"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()
