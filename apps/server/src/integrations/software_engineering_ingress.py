"""Trusted application adapter for Software Engineering task delegation.

The caller supplies only the engineering objective and target. VELOX owns the
Role and capability mapping, while the existing TaskDelegator continues to own
route validation and permission gating. This boundary never executes a worker.
"""

from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.delegation import (
    TaskDelegationRequest,
    TaskDelegationResult,
    TaskDelegationStatus,
    TaskDelegator,
)
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_state import (
    SoftwareEngineeringApprovalStatus,
    SoftwareEngineeringRunRepository,
)


class SoftwareEngineeringTaskIngress:
    """Map caller-owned task text onto the canonical Software Engineering route."""

    def __init__(
        self,
        task_delegator: TaskDelegator,
        run_repository: SoftwareEngineeringRunRepository,
    ) -> None:
        self._task_delegator = task_delegator
        self._run_repository = run_repository

    def delegate(self, *, objective: str, target: str) -> TaskDelegationResult:
        """Create one governed delegation and persist its trusted Action identity."""
        result = self._task_delegator.delegate(
            TaskDelegationRequest(
                objective=objective,
                target=target,
                executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
                capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            )
        )
        approval_status = None
        objective_for_recovery = None
        if result.status is TaskDelegationStatus.AWAITING_APPROVAL:
            approval_status = SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL
            objective_for_recovery = objective
        elif result.status is TaskDelegationStatus.QUEUED:
            approval_status = SoftwareEngineeringApprovalStatus.APPROVED
            objective_for_recovery = objective

        self._run_repository.register_action(
            action_id=result.action_id,
            target=target,
            executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
            capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            delegation_status=result.status.value,
            objective=objective_for_recovery,
            approval_status=approval_status,
        )
        return result
