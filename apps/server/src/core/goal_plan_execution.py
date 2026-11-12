"""Durable worker execution evidence for Goal/Plan step Actions.

``DurableGoalPlanExecutionObserver`` wraps the existing ``WorkerExecutionObserver``
chain. Ownership comes only from the durable step delegation keyed by the
trusted Action id, never from Action metadata or worker output. For an owned
Action it records the attempt start before the worker is invoked and the
executor-reported completion afterwards. A failed start write raises before
the worker runs; a failed finish write raises after it, leaving only the start
on record so the attempt reads as uncertain. Neither path re-queues the Action.
No SQLite transaction is held while the worker executes.

``GoalPlanRetryPolicy`` withholds the runtime's automatic transient retry for
owned Actions (and, failing closed, whenever ownership cannot be read).

``GoalPlanExecutionReconciliation`` is a read-only view. Execution evidence never
changes PlanStep or Goal status: executor-reported success is not proof that the
step objective was met, and explicit transitions remain the only way to
finalize a step.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.goal_lifecycle import TERMINAL_PLAN_STEP_STATUSES
from apps.server.src.core.goal_plan_repository import (
    GoalPlanNotFoundError,
    GoalPlanRepository,
    GoalPlanRepositoryError,
    StepDelegation,
    StepDelegationStatus,
    StepExecution,
    StepExecutionStatus,
)
from apps.server.src.core.goals import GoalPlanContractError, PlanStep, PlanStepStatus
from apps.server.src.workers.executor import WorkerExecutionFailure, WorkerExecutionStatus
from apps.server.src.workers.runtime import WorkerExecutionObservation, WorkerExecutionObserver

logger = logging.getLogger(__name__)

_EXECUTABLE = frozenset({StepDelegationStatus.QUEUED, StepDelegationStatus.APPROVED})


class GoalPlanExecutionEvidenceError(RuntimeError):
    """Execution evidence for a Goal/Plan Action could not be established or recorded."""


def _evidence[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (GoalPlanRepositoryError, GoalPlanContractError, ValueError) as error:
        raise GoalPlanExecutionEvidenceError(
            "goal/plan execution evidence could not be recorded",
        ) from error


class DurableGoalPlanExecutionObserver:
    """Persist attempt evidence for delegated plan Actions; pass every Action through."""

    def __init__(self, observer: WorkerExecutionObserver, repository: GoalPlanRepository) -> None:
        self._observer = observer
        self._repository = repository

    def start(
        self,
        action: Action,
        requested_role: str | None,
        executor_registered: bool,
        requested_capability: str | None = None,
        requested_provider: str | None = None,
        matched_provider: str | None = None,
        requested_account_context: dict[str, str | None] | None = None,
        matched_account_context: dict[str, str | None] | None = None,
        account_context_used: dict[str, str | None] | None = None,
        routing_reason: str | None = None,
    ) -> WorkerExecutionObservation:
        owned = self._owned(action.id)
        observation = self._observer.start(
            action=action,
            requested_role=requested_role,
            executor_registered=executor_registered,
            requested_capability=requested_capability,
            requested_provider=requested_provider,
            matched_provider=matched_provider,
            requested_account_context=requested_account_context,
            matched_account_context=matched_account_context,
            account_context_used=account_context_used,
            routing_reason=routing_reason,
        )
        if owned:
            _evidence(
                lambda: self._repository.record_execution_start(
                    execution_id=observation.execution_id,
                    action_id=action.id,
                    executor_role=ExecutorRole(requested_role or ""),
                    capability=requested_capability or "",
                    started_at=observation.started_at,
                ),
            )
        return observation

    def finish(
        self,
        observation: WorkerExecutionObservation,
        status: WorkerExecutionStatus,
        metadata: dict[str, Any],
        reason: str | None = None,
        failure: WorkerExecutionFailure | None = None,
        duration_ms: float | None = None,
    ) -> WorkerExecutionObservation:
        finished = self._observer.finish(
            observation=observation,
            status=status,
            metadata=metadata,
            reason=reason,
            failure=failure,
            duration_ms=duration_ms,
        )
        if self._owned(finished.action_id):
            finished_at = finished.finished_at
            if finished_at is None:
                raise GoalPlanExecutionEvidenceError(
                    "finished execution observation has no completion time",
                )
            _evidence(
                lambda: self._repository.record_execution_finish(
                    execution_id=finished.execution_id,
                    action_id=finished.action_id,
                    status=StepExecutionStatus(status.value),
                    finished_at=finished_at,
                    external_execution_reported=metadata.get("external_execution_performed")
                    is True,
                    failure_category=(
                        failure.category.value
                        if failure is not None and status is WorkerExecutionStatus.FAILED
                        else None
                    ),
                    reason=reason,
                ),
            )
        return finished

    def list(self) -> list[WorkerExecutionObservation]:
        return self._observer.list()

    def _owned(self, action_id: UUID) -> bool:
        delegation = _evidence(lambda: self._repository.get_step_delegation_for_action(action_id))
        return delegation is not None


class GoalPlanRetryPolicy:
    """``WorkerRetryPolicy``: no implicit retry for Goal/Plan-owned Actions.

    A retry needs explicit authorization and an attempt policy that do not exist
    yet. When ownership cannot be read the retry is withheld (fail closed).
    """

    def __init__(self, repository: GoalPlanRepository) -> None:
        self._repository = repository

    def allows_automatic_retry(self, action: Action) -> bool:
        try:
            return self._repository.get_step_delegation_for_action(action.id) is None
        except GoalPlanRepositoryError:
            logger.exception("retry ownership of action %s is unreadable; withheld", action.id)
            return False


class StepExecutionCategory(StrEnum):
    """What durable evidence says about a step's execution; nothing here is applied.

    ``explicitly_finalized_consistent`` only means an explicit terminal step
    transition agrees with the single recorded attempt; it is not independent
    verification that the step objective was met.
    """

    NO_EVIDENCE = "no_evidence"
    STARTED_UNCERTAIN = "started_uncertain"
    WORKER_REPORTED_SUCCESS = "worker_reported_success"
    WORKER_REPORTED_FAILURE = "worker_reported_failure"
    SKIPPED = "skipped"
    RETRY_INTERMEDIATE = "retry_intermediate"
    EXPLICITLY_FINALIZED_CONSISTENT = "explicitly_finalized_consistent"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, slots=True)
class StepExecutionReconciliation:
    """Read-only reconciliation of one step and the records supporting it."""

    goal_id: UUID
    version: int
    step_id: UUID
    step_status: PlanStepStatus
    delegation: StepDelegation | None
    attempts: tuple[StepExecution, ...]
    category: StepExecutionCategory
    requires_operator: bool
    reason: str


class GoalPlanExecutionReconciliation:
    """Classify execution evidence against Goal/Plan state without changing either."""

    def __init__(self, repository: GoalPlanRepository) -> None:
        self._repository = repository

    def reconcile_plan(
        self,
        goal_id: UUID,
        version: int,
    ) -> tuple[StepExecutionReconciliation, ...]:
        stored = self._repository.get_plan(goal_id, version)
        if stored is None:
            raise GoalPlanNotFoundError(f"plan version {version} of goal {goal_id} does not exist")
        return tuple(self._reconcile(goal_id, version, step) for step in stored.plan.steps)

    def reconcile_step(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
    ) -> StepExecutionReconciliation:
        stored = self._repository.get_plan(goal_id, version)
        if stored is None:
            raise GoalPlanNotFoundError(f"plan version {version} of goal {goal_id} does not exist")
        try:
            step = stored.plan.step(step_id)
        except KeyError:
            raise GoalPlanNotFoundError(f"plan step {step_id} does not exist") from None
        return self._reconcile(goal_id, version, step)

    def _reconcile(
        self,
        goal_id: UUID,
        version: int,
        step: PlanStep,
    ) -> StepExecutionReconciliation:
        delegation = self._repository.get_step_delegation(goal_id, version, step.id)
        attempts = self._repository.list_step_executions(goal_id, version, step.id)
        category, requires_operator, reason = _classify(step, delegation, attempts)
        return StepExecutionReconciliation(
            goal_id=goal_id,
            version=version,
            step_id=step.id,
            step_status=step.status,
            delegation=delegation,
            attempts=attempts,
            category=category,
            requires_operator=requires_operator,
            reason=reason,
        )


# A single completed attempt agrees with an explicit terminal finalization only
# when the finalization claims no more than the worker reported.
_CONSISTENT_FINALIZATIONS = {
    StepExecutionStatus.SUCCEEDED: frozenset({PlanStepStatus.SUCCEEDED}),
    StepExecutionStatus.FAILED: frozenset({PlanStepStatus.FAILED, PlanStepStatus.CANCELLED}),
    StepExecutionStatus.SKIPPED: frozenset({PlanStepStatus.FAILED, PlanStepStatus.CANCELLED}),
}

_UNVERIFIED = "no independent objective verification or verifier identity is recorded"


def _classify(
    step: PlanStep,
    delegation: StepDelegation | None,
    attempts: tuple[StepExecution, ...],
) -> tuple[StepExecutionCategory, bool, str]:
    """Classify evidence; a terminal step status never hides missing or contrary evidence."""
    released = delegation is not None and delegation.status in _EXECUTABLE
    if attempts and (delegation is None or not released):
        return (
            StepExecutionCategory.INCONSISTENT,
            True,
            "execution evidence exists without an authorized handoff",
        )
    if delegation is not None and any(
        attempt.action_id != delegation.action_id
        or (attempt.executor_role, attempt.capability) != (step.executor_role, step.capability)
        for attempt in attempts
    ):
        return (
            StepExecutionCategory.INCONSISTENT,
            True,
            "execution evidence does not match the delegated step",
        )
    if not attempts:
        return _without_attempts(step, released)
    unfinished = [a for a in attempts if a.status is StepExecutionStatus.STARTED]
    if len(attempts) > 1:
        return (
            StepExecutionCategory.RETRY_INTERMEDIATE,
            True,
            f"{len(attempts)} attempts recorded ({len(unfinished)} unfinished) while the "
            f"step is {step.status}; review every attempt",
        )
    if unfinished:
        return (
            StepExecutionCategory.STARTED_UNCERTAIN,
            True,
            f"an attempt started without a recorded completion while the step is "
            f"{step.status}; an external effect may or may not have happened",
        )
    [attempt] = attempts
    if step.status not in TERMINAL_PLAN_STEP_STATUSES:
        category = {
            StepExecutionStatus.SUCCEEDED: StepExecutionCategory.WORKER_REPORTED_SUCCESS,
            StepExecutionStatus.FAILED: StepExecutionCategory.WORKER_REPORTED_FAILURE,
            StepExecutionStatus.SKIPPED: StepExecutionCategory.SKIPPED,
        }[attempt.status]
        return (
            category,
            True,
            f"the worker reported {attempt.status}; the step objective is not verified "
            "and the step awaits explicit finalization",
        )
    if step.status in _CONSISTENT_FINALIZATIONS[attempt.status]:
        return (
            StepExecutionCategory.EXPLICITLY_FINALIZED_CONSISTENT,
            False,
            f"the step was explicitly finalized as {step.status} and the only attempt "
            f"reported {attempt.status}; {_UNVERIFIED}",
        )
    return (
        StepExecutionCategory.INCONSISTENT,
        True,
        f"the step was finalized as {step.status} but the only attempt reported "
        f"{attempt.status}"
        + (
            "; the handed-off Action may have run after cancellation"
            if step.status is PlanStepStatus.CANCELLED
            else ""
        ),
    )


def _without_attempts(
    step: PlanStep,
    released: bool,
) -> tuple[StepExecutionCategory, bool, str]:
    if step.status is PlanStepStatus.SUCCEEDED:
        return (
            StepExecutionCategory.NO_EVIDENCE,
            True,
            "the step was finalized as succeeded without any execution evidence",
        )
    if not released:
        return StepExecutionCategory.NO_EVIDENCE, False, "not released for execution"
    if step.status is PlanStepStatus.PENDING:
        return (
            StepExecutionCategory.INCONSISTENT,
            True,
            "released for execution but the step never started",
        )
    return (
        StepExecutionCategory.NO_EVIDENCE,
        True,
        f"handed off but no execution attempt was recorded while the step is "
        f"{step.status}; it may not have run yet, may still run, or its evidence was lost",
    )
