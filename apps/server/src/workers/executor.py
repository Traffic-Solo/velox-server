"""Worker executor contract."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from apps.server.src.core.actions import Action, ExecutorRole


class WorkerExecutionStatus(StrEnum):
    """Supported worker executor result statuses."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkerExecutionResult:
    """Role-level result returned by a worker executor."""

    action: Action
    status: WorkerExecutionStatus
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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

    def resolve(self, action: Action) -> WorkerExecutor:
        """Resolve the best executor for an action, falling back to no-op."""
        role = self._normalize_role(action.executor_role)
        if not role:
            return self._fallback_executor

        return self._executors.get(role, self._fallback_executor)

    def _normalize_role(self, role: ExecutorRole | str | None) -> str:
        if isinstance(role, ExecutorRole):
            return role.value
        if isinstance(role, str):
            return role.strip()
        return ""
