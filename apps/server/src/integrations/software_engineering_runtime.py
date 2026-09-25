"""Explicit opt-in composition of the Software Engineering worker provider."""

from pathlib import Path

from apps.server.src.core.config import get_settings
from apps.server.src.integrations.claude_code import ClaudeCodeSoftwareEngineeringExecutor
from apps.server.src.integrations.software_engineering import (
    ProcessRunner,
    SubprocessRunner,
    TrustedGitWorkspace,
)


def configured_software_engineering_executor(
    runner: ProcessRunner | None = None,
) -> ClaudeCodeSoftwareEngineeringExecutor | None:
    """Return the configured provider, or None when disabled (the default).

    Only one provider is ever composed, so the Software Engineering route stays
    unambiguous. The workspace comes from trusted settings, never a task request.
    """
    settings = get_settings()
    if settings.software_engineering_provider != "claude_code":
        return None
    process_runner = runner or SubprocessRunner()
    return ClaudeCodeSoftwareEngineeringExecutor(
        workspace=TrustedGitWorkspace(
            Path(settings.software_engineering_workspace or ""), process_runner,
        ),
        runner=process_runner,
        executable=settings.claude_code_executable,
        timeout_seconds=settings.software_engineering_timeout_seconds,
    )
