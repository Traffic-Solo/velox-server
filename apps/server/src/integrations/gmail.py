"""Gmail worker executor bootstrap."""

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.workers.executor import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
)


GMAIL_EXECUTOR_ROLE = ExecutorRole.CONTENT_SUMMARY


class GmailWorkerExecutor:
    """Safe Gmail executor bootstrap with no external API behavior."""

    def execute(self, action: Action) -> WorkerExecutionResult:
        """Return a placeholder result without contacting Gmail."""
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            reason="gmail executor bootstrap placeholder",
            metadata={
                "external_execution_performed": False,
                "integration": "gmail",
                "placeholder": True,
            },
        )
