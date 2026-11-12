"""Pre-execution admission of Goal/Plan step Actions against durable lifecycle state.

``GoalPlanAdmissionGate`` is the ``WorkerAdmissionGate`` the runtime consults after
dequeueing an approved Action and before ``executing``, observation or the worker.
Ownership comes from the durable step delegation keyed by the trusted Action id;
Action metadata is never trusted. Actions without a delegation are admitted
unchanged (SE and every other route). An owned Action runs only while its
delegation is released, its Goal is active, its Plan version is current, its
step is ``in_progress``, the Action envelope equals the one rebuilt from the
stored step and delegation, and no attempt was recorded before. Unreadable or
inconsistent state is a refusal, never permission.

This is a preflight read. The durable execution-start write repeats the
lifecycle check inside its own transaction, so a cancellation committed before
that write still stops the worker. A cancellation committed after the start
write cannot interrupt a worker that is already running; that attempt then
reconciles as uncertain or inconsistent.
"""

from enum import StrEnum

from apps.server.src.core.actions import Action
from apps.server.src.core.delegation import (
    TaskDelegationRequest,
    TaskDelegationRequestError,
    task_delegation_action,
)
from apps.server.src.core.goal_plan_repository import (
    GoalPlanRepository,
    GoalPlanRepositoryError,
    StepDelegation,
    StepDelegationStatus,
)
from apps.server.src.core.goals import GoalPlanContractError, GoalStatus, PlanStepStatus
from apps.server.src.workers.executor import WorkerAccountContext
from apps.server.src.workers.runtime import WorkerAdmission

_RELEASED = frozenset({StepDelegationStatus.QUEUED, StepDelegationStatus.APPROVED})


class GoalPlanAdmissionRejection(StrEnum):
    """Why a Goal/Plan Action was refused before execution."""

    OWNERSHIP_UNREADABLE = "ownership_unreadable"
    STATE_UNREADABLE = "state_unreadable"
    DELEGATION_NOT_RELEASED = "delegation_not_released"
    GOAL_NOT_ACTIVE = "goal_not_active"
    PLAN_NOT_CURRENT = "plan_not_current"
    STEP_NOT_IN_PROGRESS = "step_not_in_progress"
    ACTION_MISMATCH = "action_mismatch"
    ALREADY_ATTEMPTED = "already_attempted"


def _identity(action: Action) -> tuple[object, ...]:
    role = action.executor_role
    return (
        action.id,
        str(role) if role is not None else None,
        action.type,
        action.target,
        action.payload,
    )


class GoalPlanAdmissionGate:
    """``WorkerAdmissionGate`` that refuses stale or foreign Goal/Plan Actions."""

    def __init__(self, repository: GoalPlanRepository) -> None:
        self._repository = repository

    def admit(self, action: Action) -> WorkerAdmission:
        try:
            delegation = self._repository.get_step_delegation_for_action(action.id)
        except GoalPlanRepositoryError:
            return _refuse(GoalPlanAdmissionRejection.OWNERSHIP_UNREADABLE)
        if delegation is None:
            return WorkerAdmission(admitted=True)
        try:
            rejection = self._rejection(action, delegation)
        except (
            GoalPlanRepositoryError,
            GoalPlanContractError,
            TaskDelegationRequestError,
            KeyError,
            ValueError,
        ):
            rejection = GoalPlanAdmissionRejection.STATE_UNREADABLE
        return WorkerAdmission(admitted=True) if rejection is None else _refuse(rejection)

    def _rejection(
        self,
        action: Action,
        delegation: StepDelegation,
    ) -> GoalPlanAdmissionRejection | None:
        if delegation.status not in _RELEASED:
            return GoalPlanAdmissionRejection.DELEGATION_NOT_RELEASED
        goal = self._repository.get_goal(delegation.goal_id)
        latest = self._repository.get_latest_plan(delegation.goal_id)
        if goal is None or latest is None:
            return GoalPlanAdmissionRejection.STATE_UNREADABLE
        if goal.goal.status is not GoalStatus.ACTIVE:
            return GoalPlanAdmissionRejection.GOAL_NOT_ACTIVE
        if latest.plan.version != delegation.version:
            return GoalPlanAdmissionRejection.PLAN_NOT_CURRENT
        step = latest.plan.step(delegation.step_id)
        if step.status is not PlanStepStatus.IN_PROGRESS:
            return GoalPlanAdmissionRejection.STEP_NOT_IN_PROGRESS
        expected = task_delegation_action(
            TaskDelegationRequest(
                objective=step.objective,
                target=step.target,
                executor_role=step.executor_role,
                capability=step.capability,
                account_context=(
                    WorkerAccountContext(
                        principal=delegation.account_principal,
                        account_identifier=delegation.account_identifier,
                    )
                    if delegation.account_identifier is not None
                    else None
                ),
            ),
            action_id=delegation.action_id,
        )
        if _identity(action) != _identity(expected):
            return GoalPlanAdmissionRejection.ACTION_MISMATCH
        attempts = self._repository.list_step_executions(
            delegation.goal_id,
            delegation.version,
            delegation.step_id,
        )
        if any(attempt.action_id == action.id for attempt in attempts):
            return GoalPlanAdmissionRejection.ALREADY_ATTEMPTED
        return None


def _refuse(rejection: GoalPlanAdmissionRejection) -> WorkerAdmission:
    return WorkerAdmission(admitted=False, reason=rejection.value)
