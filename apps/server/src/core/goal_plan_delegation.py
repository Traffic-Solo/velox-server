"""Permission-gated handoff of one ready PlanStep to the existing TaskDelegator.

A Plan is not authorization to execute. This service only composes existing
Roles: the ``GoalPlanRepository`` decides whether a step may start, the
``TaskDelegator`` (with its ``WorkerExecutorRegistry`` routing and
``PermissionEngineRuntime`` gate) decides whether and how the work is queued or
held for approval, and ``WorkerRuntime`` alone executes queued Actions later.
Nothing here executes a worker, grants permission or reports work as done.

The handoff runs inside the repository's lock / ``BEGIN IMMEDIATE`` transaction
together with the durable delegation record, so a step is handed off at most
once. Approval-required work stays ``pending``; ``GoalPlanApprovalRecorder``
plugs into the existing approval entry points and re-validates the Goal, Plan
version and step before an approved Action may be queued.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import ActionLifecycleRepository
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.approvals import PendingApprovalRegistry
from apps.server.src.core.delegation import (
    TaskDelegationRequest,
    TaskDelegationResult,
    TaskDelegator,
)
from apps.server.src.core.goal_lifecycle import GoalPlanTransitionError
from apps.server.src.core.goal_plan_repository import (
    GoalPlanConflictError,
    GoalPlanDelegationExistsError,
    GoalPlanNotFoundError,
    GoalPlanRepository,
    StepDelegation,
    StepDelegationStatus,
    StepHandoff,
)
from apps.server.src.core.goals import GoalPlanContractError, GoalStatus, PlanStep
from apps.server.src.core.permission import PermissionStatus
from apps.server.src.workers.executor import WorkerAccountContext

logger = logging.getLogger(__name__)


class GoalPlanDelegationRequestError(ValueError):
    """The step delegation request is structurally invalid or lacks authority."""


class GoalPlanApprovalError(ValueError):
    """An approval decision conflicts with the step's durable state; nothing is queued."""


class GoalPlanDelegationOutcome(StrEnum):
    """Handoff outcome. ``QUEUED`` means accepted for execution, never executed.

    The first four values mirror ``TaskDelegationStatus``; ``FAILED`` adds the case
    where no durable delegation could be established.
    """

    QUEUED = "queued"
    AWAITING_APPROVAL = "awaiting_approval"
    DENIED = "denied"
    ROUTE_REJECTED = "route_rejected"
    FAILED = "failed"


_OUTCOME_FOR_STATUS = {
    StepDelegationStatus.QUEUED: GoalPlanDelegationOutcome.QUEUED,
    StepDelegationStatus.APPROVED: GoalPlanDelegationOutcome.QUEUED,
    StepDelegationStatus.AWAITING_APPROVAL: GoalPlanDelegationOutcome.AWAITING_APPROVAL,
    StepDelegationStatus.DENIED: GoalPlanDelegationOutcome.DENIED,
    StepDelegationStatus.APPROVAL_REJECTED: GoalPlanDelegationOutcome.DENIED,
    StepDelegationStatus.ROUTE_REJECTED: GoalPlanDelegationOutcome.ROUTE_REJECTED,
}


def _require_canonical_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise GoalPlanDelegationRequestError(f"step delegation {name} must be canonical text")


@dataclass(frozen=True, slots=True)
class GoalPlanStepDelegationRequest:
    """Trusted caller input: which step, at which revision, by whom, for which account.

    There is deliberately no permission, approval, provider or executor field; the
    executor Role and capability come only from the stored PlanStep.
    """

    goal_id: UUID
    version: int
    step_id: UUID
    expected_revision: int
    requested_by: str
    account_context: WorkerAccountContext | None = None

    def __post_init__(self) -> None:
        identifiers: tuple[tuple[object, str], ...] = (
            (self.goal_id, "goal id"),
            (self.step_id, "step id"),
        )
        for identifier, name in identifiers:
            if not isinstance(identifier, UUID):
                raise GoalPlanDelegationRequestError(f"step delegation {name} must be a UUID")
        counters: tuple[tuple[object, str], ...] = (
            (self.version, "plan version"),
            (self.expected_revision, "expected revision"),
        )
        for counter, name in counters:
            if isinstance(counter, bool) or not isinstance(counter, int) or counter < 1:
                raise GoalPlanDelegationRequestError(
                    f"step delegation {name} must be a positive integer",
                )
        _require_canonical_text(self.requested_by, "requester")
        context = self.account_context
        if context is None:
            return
        if not isinstance(context, WorkerAccountContext):
            raise GoalPlanDelegationRequestError("step delegation account context is invalid")
        _require_canonical_text(context.account_identifier, "account identifier")
        if context.principal is not None:
            _require_canonical_text(context.principal, "account principal")


@dataclass(frozen=True, slots=True)
class GoalPlanDelegationResult:
    """Typed handoff result; no outcome means the work started, ran or succeeded."""

    goal_id: UUID
    version: int
    step_id: UUID
    outcome: GoalPlanDelegationOutcome
    action_id: UUID | None
    delegation_status: StepDelegationStatus | None
    permission_status: PermissionStatus | None
    routing_reason: str | None
    plan_revision: int | None
    duplicate: bool = False
    reason: str | None = None


class _ReservedRouteError(Exception):
    """The step's route must use its own dedicated, durable ingress."""


class _HandoffFailedError(Exception):
    """The TaskDelegator raised; nothing was recorded."""


class GoalPlanDelegationService:
    """Hand one ready step to the existing TaskDelegator, exactly once per plan version."""

    def __init__(
        self,
        *,
        repository: GoalPlanRepository,
        task_delegator: TaskDelegator,
        action_queue: ActionQueue,
        pending_approval_registry: PendingApprovalRegistry,
        lifecycle_repository: ActionLifecycleRepository,
        lifecycle_manager: ActionLifecycleManager,
        reserved_routes: Iterable[tuple[ExecutorRole, str]] = (),
    ) -> None:
        self._repository = repository
        self._task_delegator = task_delegator
        self._action_queue = action_queue
        self._pending_approval_registry = pending_approval_registry
        self._lifecycle_repository = lifecycle_repository
        self._lifecycle_manager = lifecycle_manager
        self._reserved_routes = frozenset(reserved_routes)

    def ready_steps(self, goal_id: UUID) -> tuple[PlanStep, ...]:
        """Advisory: undelegated ready steps of the latest Plan of an active Goal.

        ``delegate`` re-validates everything atomically; this read is not a claim.
        """
        goal = self._repository.get_goal(goal_id)
        if goal is None:
            raise GoalPlanNotFoundError(f"goal {goal_id} does not exist")
        if goal.goal.status is not GoalStatus.ACTIVE:
            return ()
        latest = self._repository.get_latest_plan(goal_id)
        if latest is None:
            return ()
        return tuple(
            step
            for step in latest.plan.ready_steps()
            if self._repository.get_step_delegation(goal_id, latest.plan.version, step.id)
            is None
        )

    def delegate(self, request: GoalPlanStepDelegationRequest) -> GoalPlanDelegationResult:
        """Validate and hand off one step; precondition failures raise typed errors.

        Unknown Goal/Plan/step raise ``GoalPlanNotFoundError``; stale revisions and
        superseded versions raise ``GoalPlanConflictError``; inactive Goals and
        non-pending or blocked steps raise ``GoalPlanTransitionError``. A repeated
        request for an already delegated step returns that delegation with
        ``duplicate=True`` and never delegates again.
        """
        if not isinstance(request, GoalPlanStepDelegationRequest):
            raise GoalPlanDelegationRequestError("step delegation request is invalid")
        context = request.account_context
        handed_off: list[TaskDelegationResult] = []

        def handoff(step: PlanStep) -> StepHandoff:
            if (step.executor_role, step.capability) in self._reserved_routes:
                raise _ReservedRouteError
            delegation_request = TaskDelegationRequest(
                objective=step.objective,
                target=step.target,
                executor_role=step.executor_role,
                capability=step.capability,
                account_context=context,
            )
            try:
                result = self._task_delegator.delegate(delegation_request)
            except Exception as error:
                raise _HandoffFailedError from error
            handed_off.append(result)
            return StepHandoff(
                action_id=result.action_id,
                status=StepDelegationStatus(result.status.value),
                permission_status=(
                    result.permission_status.value
                    if result.permission_status is not None
                    else None
                ),
                routing_reason=result.routing_reason,
            )

        try:
            stored, delegation = self._repository.delegate_step(
                request.goal_id,
                request.version,
                request.step_id,
                expected_revision=request.expected_revision,
                requested_by=request.requested_by,
                account_principal=context.principal if context is not None else None,
                account_identifier=context.account_identifier if context is not None else None,
                handoff=handoff,
            )
        except GoalPlanDelegationExistsError as error:
            return self._duplicate(request, error.delegation)
        except _ReservedRouteError:
            return self._unrecorded(
                request,
                GoalPlanDelegationOutcome.ROUTE_REJECTED,
                routing_reason="reserved_route",
                reason="this route requires its dedicated durable ingress",
            )
        except _HandoffFailedError:
            logger.exception("task delegation raised for plan step %s", request.step_id)
            return self._unrecorded(
                request,
                GoalPlanDelegationOutcome.FAILED,
                reason="task delegation failed; nothing was recorded",
            )
        except Exception:
            if not handed_off:
                raise
            action_id = handed_off[0].action_id
            revoked = self._revoke(action_id)
            logger.exception(
                "plan step %s delegation could not be recorded; handoff %s",
                request.step_id,
                "revoked" if revoked else "revocation incomplete",
            )
            return self._unrecorded(
                request,
                GoalPlanDelegationOutcome.FAILED,
                action_id=action_id,
                reason=(
                    "delegation could not be recorded; the handoff was revoked"
                    if revoked
                    else "delegation could not be recorded and the handoff could not "
                    "be revoked; the Action may still be executed"
                ),
            )
        return self._result(request, delegation, stored.revision, duplicate=False)

    def _duplicate(
        self,
        request: GoalPlanStepDelegationRequest,
        delegation: StepDelegation,
    ) -> GoalPlanDelegationResult:
        context = request.account_context
        same_account = (
            delegation.account_identifier
            == (context.account_identifier if context is not None else None)
            and delegation.account_principal
            == (context.principal if context is not None else None)
        )
        if delegation.requested_by != request.requested_by or not same_account:
            raise GoalPlanConflictError(
                f"plan step {delegation.step_id} was already delegated under a different "
                "requester or account context",
            )
        stored = self._repository.get_plan(delegation.goal_id, delegation.version)
        return self._result(
            request,
            delegation,
            stored.revision if stored is not None else None,
            duplicate=True,
        )

    @staticmethod
    def _result(
        request: GoalPlanStepDelegationRequest,
        delegation: StepDelegation,
        plan_revision: int | None,
        *,
        duplicate: bool,
    ) -> GoalPlanDelegationResult:
        return GoalPlanDelegationResult(
            goal_id=request.goal_id,
            version=request.version,
            step_id=request.step_id,
            outcome=_OUTCOME_FOR_STATUS[delegation.status],
            action_id=delegation.action_id,
            delegation_status=delegation.status,
            permission_status=(
                PermissionStatus(delegation.permission_status)
                if delegation.permission_status is not None
                else None
            ),
            routing_reason=delegation.routing_reason,
            plan_revision=plan_revision,
            duplicate=duplicate,
        )

    @staticmethod
    def _unrecorded(
        request: GoalPlanStepDelegationRequest,
        outcome: GoalPlanDelegationOutcome,
        *,
        action_id: UUID | None = None,
        routing_reason: str | None = None,
        reason: str,
    ) -> GoalPlanDelegationResult:
        return GoalPlanDelegationResult(
            goal_id=request.goal_id,
            version=request.version,
            step_id=request.step_id,
            outcome=outcome,
            action_id=action_id,
            delegation_status=None,
            permission_status=None,
            routing_reason=routing_reason,
            plan_revision=None,
            reason=reason,
        )

    def _revoke(self, action_id: UUID) -> bool:
        """Withdraw an unrecorded handoff from the process-local queue/approval hold."""
        queued = self._action_queue.remove(action_id)
        pending = self._pending_approval_registry.remove(action_id)
        if pending is not None:
            state = self._lifecycle_repository.get(action_id)
            if state is not None and state.status is ActionStatus.QUEUED:
                self._lifecycle_repository.set(
                    action_id,
                    self._lifecycle_manager.transition(
                        state,
                        ActionStatus.REJECTED,
                        reason="goal plan delegation could not be recorded",
                    ),
                )
        return queued is not None or pending is not None


class GoalPlanApprovalRecorder:
    """``ApprovalDecisionRecorder`` for Actions that a plan step delegation created.

    Invoked by the existing approve/reject entry points before any process-local
    approval mutation. Actions without a step delegation are ignored. Approval
    re-validates the Goal, Plan version and step and starts the step; any
    conflict raises ``GoalPlanApprovalError`` so the Action is never queued.
    """

    def __init__(self, repository: GoalPlanRepository) -> None:
        self._repository = repository

    def record_approved(self, action: Action) -> None:
        self._resolve(action, approved=True)

    def record_rejected(self, action: Action) -> None:
        self._resolve(action, approved=False)

    def _resolve(self, action: Action, *, approved: bool) -> None:
        if self._repository.get_step_delegation_for_action(action.id) is None:
            return
        try:
            self._repository.resolve_step_approval(action.id, approved=approved)
        except (
            GoalPlanNotFoundError,
            GoalPlanConflictError,
            GoalPlanTransitionError,
            GoalPlanContractError,
        ) as error:
            raise GoalPlanApprovalError(str(error)) from error
