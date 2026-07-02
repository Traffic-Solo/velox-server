"""Worker executor contract."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from apps.server.src.core.actions import Action


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

    def register(self, key: str, executor: WorkerExecutor) -> None:
        """Register an executor for a vendor-neutral action key."""
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("executor registry key must not be empty")

        self._executors[normalized_key] = executor

    def resolve(self, action: Action) -> WorkerExecutor:
        """Resolve the best executor for an action, falling back to no-op."""
        for key in self._candidate_keys(action):
            executor = self._executors.get(key)
            if executor is not None:
                return executor

        return self._fallback_executor

    def _candidate_keys(self, action: Action) -> list[str]:
        metadata = action.metadata
        candidates = [
            metadata.get("executor_key"),
            metadata.get("worker_role"),
            metadata.get("role"),
            action.type,
        ]

        keys: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue

            key = candidate.strip()
            if key and key not in keys:
                keys.append(key)

        return keys
