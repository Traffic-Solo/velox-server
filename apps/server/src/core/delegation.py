"""Vendor-neutral task delegation: typed request -> routed, permission-gated Action.

Delegation names *what* should be done (objective, target, executor Role and
capability, optional caller-owned account context). It never names a provider:
``WorkerExecutorRegistry`` owns provider selection, ``PermissionEngineRuntime``
owns approval and ``WorkerRuntime`` owns execution. Delegation only creates an
Action, validates that a unique trusted route exists, evaluates permission and
queues allowed work. It never executes a worker.

This is separate from the event ``Planner`` (``ProcessedEvent -> list[Action]``).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.permission import PermissionEngineRuntime, PermissionStatus
from apps.server.src.workers.executor import WorkerAccountContext, WorkerExecutorRegistry


class TaskDelegationRequestError(ValueError):
    """The typed delegation request is structurally invalid."""


class TaskDelegationStatus(StrEnum):
    """Delegation outcome. ``QUEUED`` means accepted for execution, not executed."""

    QUEUED = "queued"
    AWAITING_APPROVAL = "awaiting_approval"
    DENIED = "denied"
    ROUTE_REJECTED = "route_rejected"


def _require_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TaskDelegationRequestError(f"task delegation {field} is required")


@dataclass(frozen=True, slots=True)
class TaskDelegationRequest:
    """What to do and for whom; never which provider, credential or approval."""

    objective: str
    target: str
    executor_role: ExecutorRole
    capability: str
    account_context: WorkerAccountContext | None = None

    def __post_init__(self) -> None:
        _require_text(self.objective, "objective")
        _require_text(self.target, "target")
        if not isinstance(self.executor_role, ExecutorRole):
            raise TaskDelegationRequestError("task delegation requires a known executor role")
        _require_text(self.capability, "capability")
        if self.capability != self.capability.strip().casefold():
            raise TaskDelegationRequestError(
                "task delegation capability must be a canonical identifier",
            )
        context = self.account_context
        if context is None:
            return
        if not isinstance(context, WorkerAccountContext):
            raise TaskDelegationRequestError("task delegation account context is invalid")
        _require_text(context.account_identifier, "account identifier")
        if context.principal is not None:
            _require_text(context.principal, "account principal")


@dataclass(frozen=True, slots=True)
class TaskDelegationResult:
    """Delegation outcome for the application; no provider or worker internals."""

    action_id: UUID
    status: TaskDelegationStatus
    permission_status: PermissionStatus | None
    routing_reason: str | None

    @property
    def queued(self) -> bool:
        """Accepted into the execution queue; says nothing about execution."""
        return self.status is TaskDelegationStatus.QUEUED


class TaskDelegator(Protocol):
    """Role: turn a typed task request into governed, queued-or-held work."""

    def delegate(self, request: TaskDelegationRequest) -> TaskDelegationResult:
        ...


class ActionTaskDelegator:
    """Delegate through the existing registry, permission runtime and queue."""

    def __init__(
        self,
        *,
        executor_registry: WorkerExecutorRegistry,
        permission_runtime: PermissionEngineRuntime,
        action_queue: ActionQueue,
    ) -> None:
        self._executor_registry = executor_registry
        self._permission_runtime = permission_runtime
        self._action_queue = action_queue

    def delegate(self, request: TaskDelegationRequest) -> TaskDelegationResult:
        action = self._action_for(request)
        resolution = self._executor_registry.resolve_with_registration(action)
        if not resolution.registered:
            return TaskDelegationResult(
                action_id=action.id,
                status=TaskDelegationStatus.ROUTE_REJECTED,
                permission_status=None,
                routing_reason=resolution.routing_reason,
            )
        evaluation = self._permission_runtime.evaluate([action])[0]
        permission_status = evaluation.decision.status
        if permission_status is PermissionStatus.ALLOWED:
            self._action_queue.enqueue(evaluation.action)
            status = TaskDelegationStatus.QUEUED
        elif permission_status is PermissionStatus.REQUIRES_APPROVAL:
            status = TaskDelegationStatus.AWAITING_APPROVAL
        else:
            status = TaskDelegationStatus.DENIED
        return TaskDelegationResult(
            action_id=action.id,
            status=status,
            permission_status=permission_status,
            routing_reason=resolution.routing_reason,
        )

    @staticmethod
    def _action_for(request: TaskDelegationRequest) -> Action:
        payload: dict[str, object] = {
            "capability": request.capability,
            "objective": request.objective,
        }
        if request.account_context is not None:
            payload["account_context"] = request.account_context.as_metadata()
        return Action(
            type=request.capability,
            target=request.target,
            executor_role=request.executor_role,
            payload=payload,
            metadata={
                "task_delegation": {
                    "requested_role": request.executor_role.value,
                    "requested_capability": request.capability,
                },
            },
        )
