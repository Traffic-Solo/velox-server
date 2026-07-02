"""Worker executor contract."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from apps.server.src.core.actions import Action, ExecutorRole


class WorkerExecutionStatus(StrEnum):
    """Supported worker executor result statuses."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkerExecutionFailureCategory(StrEnum):
    """Vendor-neutral worker execution failure categories."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    INTERNAL = "internal"


@dataclass(frozen=True)
class WorkerExecutionFailure:
    """Vendor-neutral failure details returned by a worker executor."""

    category: WorkerExecutionFailureCategory
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerExecutionResult:
    """Role-level result returned by a worker executor."""

    action: Action
    status: WorkerExecutionStatus
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    failure: WorkerExecutionFailure | None = None


@dataclass(frozen=True)
class WorkerExecutorResolution:
    """Executor resolution details for a requested role."""

    executor: "WorkerExecutor"
    requested_role: str | None
    registered: bool


@runtime_checkable
class WorkerExecutor(Protocol):
    """Contract for role-compatible action executors."""

    def execute(self, action: Action) -> WorkerExecutionResult:
        """Execute an action and return an explicit execution result."""


class NoOpWorkerExecutor:
    """Safe default executor that performs no external work."""

    def execute(self, action: Action) -> WorkerExecutionResult:
        """Return a successful no-op execution result."""
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            metadata={"external_execution_performed": False},
        )


class WorkerExecutorRegistry:
    """Vendor-neutral registry for resolving action executors."""

    def __init__(self, fallback_executor: WorkerExecutor | None = None) -> None:
        self._executors: dict[str, WorkerExecutor] = {}
        self._fallback_executor = fallback_executor or NoOpWorkerExecutor()

    def register(self, role: ExecutorRole | str, executor: WorkerExecutor) -> None:
        """Register an executor for a vendor-neutral role."""
        normalized_key = self._normalize_role(role)
        if not normalized_key:
            raise ValueError("executor registry role must not be empty")

        self._executors[normalized_key] = executor

    def register_role(self, role: ExecutorRole, executor: WorkerExecutor) -> None:
        """Register an executor for an explicit vendor-neutral executor role."""
        self.register(role, executor)

    def registered_roles(self) -> tuple[str, ...]:
        """Return currently registered executor role keys."""
        return tuple(self._executors.keys())

    def resolve(self, action: Action) -> WorkerExecutor:
        """Resolve the best executor for an action, falling back to no-op."""
        return self.resolve_with_registration(action).executor

    def resolve_with_registration(self, action: Action) -> WorkerExecutorResolution:
        """Resolve an executor and expose whether the requested role was registered."""
        role = self._normalize_role(action.executor_role)
        if not role:
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=None,
                registered=False,
            )

        executor = self._executors.get(role)
        return WorkerExecutorResolution(
            executor=executor or self._fallback_executor,
            requested_role=role,
            registered=executor is not None,
        )

    def _normalize_role(self, role: ExecutorRole | str | None) -> str:
        if isinstance(role, ExecutorRole):
            return role.value
        if isinstance(role, str):
            return role.strip()
        return ""
