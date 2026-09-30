"""Explicit opt-in composition of the Software Engineering worker provider."""

from dataclasses import dataclass
from pathlib import Path

from apps.server.src.core.config import get_settings
from apps.server.src.integrations.claude_code import ClaudeCodeSoftwareEngineeringExecutor
from apps.server.src.integrations.github_pull_request import GitHubCliPullRequestPublisher
from apps.server.src.integrations.pull_request import PullRequestPublisher
from apps.server.src.integrations.software_engineering import (
    ProcessRunner,
    SubprocessRunner,
    TrustedGitWorkspace,
)
from apps.server.src.integrations.software_engineering_state import (
    SoftwareEngineeringRunRepository,
    SqliteSoftwareEngineeringRunRepository,
    default_software_engineering_state_path,
)
from apps.server.src.integrations.software_engineering_work_product import (
    SoftwareEngineeringWorkProductService,
)


@dataclass(frozen=True, slots=True)
class SoftwareEngineeringComposition:
    """Configured worker plus VELOX-owned workspace and promotion dependencies."""

    executor: ClaudeCodeSoftwareEngineeringExecutor
    workspace: TrustedGitWorkspace
    work_products: SoftwareEngineeringWorkProductService
    pull_request_publisher: PullRequestPublisher | None
    run_repository: SoftwareEngineeringRunRepository


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
    workspace_root = Path(settings.software_engineering_workspace or "").expanduser()
    workspace = TrustedGitWorkspace(workspace_root, process_runner)
    state_path = (
        Path(settings.software_engineering_state_database_path).expanduser()
        if settings.software_engineering_state_database_path is not None
        else default_software_engineering_state_path(workspace_root)
    )
    run_repository = SqliteSoftwareEngineeringRunRepository(state_path)
    publisher: PullRequestPublisher | None = None
    if settings.software_engineering_promotion_enabled:
        publisher = GitHubCliPullRequestPublisher(
            process_runner,
            executable=settings.github_cli_executable,
        )
    return SoftwareEngineeringComposition(
        executor=ClaudeCodeSoftwareEngineeringExecutor(
            workspace=workspace,
            runner=process_runner,
            executable=settings.claude_code_executable,
            timeout_seconds=settings.software_engineering_timeout_seconds,
        ),
        workspace=workspace,
        work_products=SoftwareEngineeringWorkProductService(workspace),
        pull_request_publisher=publisher,
        run_repository=run_repository,
    )
