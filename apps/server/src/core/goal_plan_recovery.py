"""Restart recovery and read-only reconciliation for Goal/Plan step handoffs.

Unknown execution state is not permission to execute again. The Action queue and
pending-approval registry are process-local, while step delegations are durable;
after a restart only ``awaiting_approval`` handoffs are recoverable, and only as
approval holds. ``GoalPlanActionRecovery`` rebuilds those exact Actions (original
UUID, stored step Role/capability/objective/target and stored account context)
for the existing approve/reject entry points, which still record the decision
durably and re-validate Goal, Plan version and step before anything is queued.
Nothing here queues, executes, retries or finalizes work.

``GoalPlanHandoffReconciliation`` is a read-only report. ``queued`` and
``approved`` handoffs whose step is still ``in_progress`` have no durable
execution evidence, so they are reported as execution-uncertain and left for
explicit operator reconciliation; they are never republished.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action
from apps.server.src.core.approval_decisions import PendingActionRecoveryError
from apps.server.src.core.approvals import PendingApprovalRegistry
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
from apps.server.src.core.goals import GoalStatus, Plan, PlanStep, PlanStepStatus
from apps.server.src.workers.executor import WorkerAccountContext

_HANDED_OFF = frozenset({StepDelegationStatus.QUEUED, StepDelegationStatus.APPROVED})
_NOT_EXECUTABLE = frozenset(
    {
        StepDelegationStatus.DENIED,
        StepDelegationStatus.ROUTE_REJECTED,
        StepDelegationStatus.APPROVAL_REJECTED,
    },
)


class GoalPlanRecoveryError(PendingActionRecoveryError):
    """Durable Goal/Plan handoff state cannot be recovered or classified safely."""


def _read[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except GoalPlanRepositoryError as error:
        raise GoalPlanRecoveryError(
            "goal/plan handoff state is unavailable or invalid",
        ) from error


def _stored_step(
    repository: GoalPlanRepository,
    delegation: StepDelegation,
) -> tuple[Plan, PlanStep]:
    stored = _read(lambda: repository.get_plan(delegation.goal_id, delegation.version))
    if stored is None:
        raise GoalPlanRecoveryError(
            f"delegation {delegation.action_id} references a missing plan version",
        )
    try:
        return stored.plan, stored.plan.step(delegation.step_id)
    except KeyError:
        raise GoalPlanRecoveryError(
            f"delegation {delegation.action_id} references a missing plan step",
        ) from None


class GoalPlanActionRecovery:
    """``PendingActionRecovery`` owner for Actions created by plan step delegation."""

    def __init__(self, repository: GoalPlanRepository) -> None:
        self._repository = repository

    def recover_pending(self, action_id: UUID) -> Action | None:
        """Rebuild one awaiting-approval plan Action; ``None`` when not ours or not held."""
        delegation = _read(lambda: self._repository.get_step_delegation_for_action(action_id))
        if delegation is None or delegation.status is not StepDelegationStatus.AWAITING_APPROVAL:
            return None
        return self._action_for(delegation)

    def list_pending(self) -> list[Action]:
        """Every durable awaiting-approval plan Action, in delegation order."""
        return [
            self._action_for(delegation)
            for delegation in _read(self._repository.list_step_delegations)
            if delegation.status is StepDelegationStatus.AWAITING_APPROVAL
        ]

    def _action_for(self, delegation: StepDelegation) -> Action:
        _, step = _stored_step(self._repository, delegation)
        account = (
            WorkerAccountContext(
                principal=delegation.account_principal,
                account_identifier=delegation.account_identifier,
            )
            if delegation.account_identifier is not None
            else None
        )
        try:
            request = TaskDelegationRequest(
                objective=step.objective,
                target=step.target,
                executor_role=step.executor_role,
                capability=step.capability,
                account_context=account,
            )
        except TaskDelegationRequestError as error:
            raise GoalPlanRecoveryError(
                f"delegation {delegation.action_id} cannot be rebuilt exactly",
            ) from error
        action = task_delegation_action(request, action_id=delegation.action_id)
        return action.model_copy(
            update={"metadata": {**action.metadata, "durable_recovery": {"pending": True}}},
        )


class GoalPlanHandoffCategory(StrEnum):
    """Reconciliation class of one durable handoff; none of these replays work."""

    RECOVERABLE_APPROVAL = "recoverable_approval"
    STALE_APPROVAL = "stale_approval"
    EXECUTION_UNCERTAIN = "execution_uncertain"
    RESOLVED = "resolved"
    NOT_EXECUTABLE = "not_executable"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, slots=True)
class GoalPlanHandoffReport:
    """What durable state says about one handoff, and whether a human must act."""

    goal_id: UUID
    version: int
    step_id: UUID
    action_id: UUID
    delegation_status: StepDelegationStatus
    step_status: PlanStepStatus
    category: GoalPlanHandoffCategory
    requires_operator: bool
    held_in_process: bool
    reason: str


class GoalPlanHandoffReconciliation:
    """Read-only classification of durable step handoffs for operator review."""

    def __init__(
        self,
        *,
        repository: GoalPlanRepository,
        action_queue: ActionQueue,
        pending_approval_registry: PendingApprovalRegistry,
    ) -> None:
        self._repository = repository
        self._action_queue = action_queue
        self._pending_approval_registry = pending_approval_registry

    def classify(self) -> tuple[GoalPlanHandoffReport, ...]:
        """Classify every delegation; unreadable or dangling records fail closed."""
        return tuple(
            self._report(delegation)
            for delegation in _read(self._repository.list_step_delegations)
        )

    def _report(self, delegation: StepDelegation) -> GoalPlanHandoffReport:
        plan, step = _stored_step(self._repository, delegation)
        category, requires_operator, reason = self._classify(delegation, plan, step)
        return GoalPlanHandoffReport(
            goal_id=delegation.goal_id,
            version=delegation.version,
            step_id=delegation.step_id,
            action_id=delegation.action_id,
            delegation_status=delegation.status,
            step_status=step.status,
            category=category,
            requires_operator=requires_operator,
            held_in_process=(
                self._action_queue.get(delegation.action_id) is not None
                or self._pending_approval_registry.get(delegation.action_id) is not None
            ),
            reason=reason,
        )

    def _classify(
        self,
        delegation: StepDelegation,
        plan: Plan,
        step: PlanStep,
    ) -> tuple[GoalPlanHandoffCategory, bool, str]:
        status = delegation.status
        if status in _NOT_EXECUTABLE:
            return (
                GoalPlanHandoffCategory.NOT_EXECUTABLE,
                False,
                f"handoff was {status}; no executable Action was released",
            )
        if status in _HANDED_OFF:
            if step.status is PlanStepStatus.PENDING:
                return (
                    GoalPlanHandoffCategory.INCONSISTENT,
                    True,
                    f"handoff is {status} but the step never started",
                )
            if step.status is PlanStepStatus.IN_PROGRESS:
                return (
                    GoalPlanHandoffCategory.EXECUTION_UNCERTAIN,
                    True,
                    "handed off without durable execution evidence; never replayed "
                    "automatically - finalize the step explicitly",
                )
            return (
                GoalPlanHandoffCategory.RESOLVED,
                False,
                f"step was finalized as {step.status}",
            )
        refusal = self._approval_refusal(delegation, plan, step)
        if refusal is None:
            return (
                GoalPlanHandoffCategory.RECOVERABLE_APPROVAL,
                True,
                "awaiting an explicit approve or reject decision",
            )
        return (
            GoalPlanHandoffCategory.STALE_APPROVAL,
            True,
            f"approval would be refused: {refusal}; reject it explicitly",
        )

    def _approval_refusal(
        self,
        delegation: StepDelegation,
        plan: Plan,
        step: PlanStep,
    ) -> str | None:
        goal = _read(lambda: self._repository.get_goal(delegation.goal_id))
        latest = _read(lambda: self._repository.get_latest_plan(delegation.goal_id))
        if goal is None or latest is None:
            raise GoalPlanRecoveryError(
                f"delegation {delegation.action_id} references a missing goal or plan",
            )
        if goal.goal.status is not GoalStatus.ACTIVE:
            return f"goal is {goal.goal.status}"
        if latest.plan.version != delegation.version:
            return f"plan version {delegation.version} is superseded"
        if step.status is not PlanStepStatus.PENDING:
            return f"step is {step.status}"
        if any(
            plan.step(dependency).status is not PlanStepStatus.SUCCEEDED
            for dependency in step.depends_on
        ):
            return "dependencies have not all succeeded"
        return None
