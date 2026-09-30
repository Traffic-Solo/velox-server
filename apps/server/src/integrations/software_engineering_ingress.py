"""Trusted application adapter for Software Engineering task delegation.

The caller supplies only the engineering objective and target. VELOX owns the
Role and capability mapping, while the existing TaskDelegator continues to own
route validation and permission gating. This boundary never executes a worker.
"""

from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.delegation import (
    TaskDelegationRequest,
    TaskDelegationResult,
    TaskDelegator,
)
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)


class SoftwareEngineeringTaskIngress:
    """Map caller-owned task text onto the canonical Software Engineering route."""

    def __init__(self, task_delegator: TaskDelegator) -> None:
        self._task_delegator = task_delegator

    def delegate(self, *, objective: str, target: str) -> TaskDelegationResult:
        """Create one governed Software Engineering delegation and nothing more."""
        return self._task_delegator.delegate(
            TaskDelegationRequest(
                objective=objective,
                target=target,
                executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
                capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            )
        )
