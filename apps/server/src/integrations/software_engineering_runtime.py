"""Explicit opt-in composition of the Software Engineering worker provider."""

from dataclasses import dataclass
from pathlib import Path

from apps.server.src.core.config import get_settings
from apps.server.src.integrations.claude_code import ClaudeCodeSoftwareEngineeringExecutor
from apps.server.src.integrations.software_engineering import (
    ProcessRunner,
    SubprocessRunner,
    TrustedGitWorkspace,
)
from apps.server.src.integrations.software_engineering_work_product import (
    SoftwareEngineeringWorkProductService,
)


@dataclass(frozen=True, slots=True)
class SoftwareEngineeringComposition:
    """The configured provider and the provider-neutral work-product service."""

    executor: ClaudeCodeSoftwareEngineeringExecutor
    work_products: SoftwareEngineeringWorkProductService


def configured_software_engineering(
    runner: ProcessRunner | None = None,
) -> SoftwareEngineeringComposition | None:
    """Return the configured composition, or None when disabled (the default).

    Only one provider is ever composed, so the Software Engineering route stays
    unambiguous. The workspace comes from trusted settings, never a task request,
    and is shared by the provider and the work-product service.
    """
    settings = get_settings()
    if settings.software_engineering_provider != "claude_code":
        return None
    process_runner = runner or SubprocessRunner()
    workspace = TrustedGitWorkspace(
        Path(settings.software_engineering_workspace or ""), process_runner,
    )
    return SoftwareEngineeringComposition(
        executor=ClaudeCodeSoftwareEngineeringExecutor(
            workspace=workspace,
            runner=process_runner,
            executable=settings.claude_code_executable,
            timeout_seconds=settings.software_engineering_timeout_seconds,
        ),
        work_products=SoftwareEngineeringWorkProductService(workspace),
    )
