"""Worker executor contract."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from apps.server.src.core.actions import Action, ExecutorRole


class WorkerExecutionStatus(StrEnum):
    """Supported worker executor result statuses."""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
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
    requested_capability: str | None = None
    requested_provider: str | None = None
    matched_provider: str | None = None
    routing_reason: str | None = None


@runtime_checkable
class WorkerExecutor(Protocol):
    """Contract for role-compatible action executors."""

    def execute(self, action: Action) -> WorkerExecutionResult:
        """Execute an action and return an explicit execution result."""


class NoOpWorkerExecutor:
    """Safe default executor that performs no external work.

    Returns SKIPPED, never SUCCEEDED: no work was done, and reporting success
    for a no-op would hide unhandled actions (Nothing Dies Silently).
    """

    def execute(self, action: Action) -> WorkerExecutionResult:
        """Return an explicit skipped no-op execution result."""
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SKIPPED,
            reason="no registered executor handled this action",
            metadata={
                "external_execution_performed": False,
                "skipped": True,
            },
        )


@dataclass(frozen=True)
class WorkerCapabilityRoute:
    """Explicit capability-provider route for a vendor-neutral executor role."""

    role: ExecutorRole | str
    capability: str
    provider: str


@dataclass(frozen=True)
class _CapabilityRouteRequest:
    capability: str | None
    present: bool
    valid: bool


@dataclass(frozen=True)
class _CapabilityProviderRequest:
    provider: str | None
    present: bool
    valid: bool


class WorkerExecutorRegistry:
    """Vendor-neutral registry for resolving action executors."""

    def __init__(self, fallback_executor: WorkerExecutor | None = None) -> None:
        self._executors: dict[str, WorkerExecutor] = {}
        self._capability_executors: dict[
            tuple[str, str, str],
            WorkerExecutor,
        ] = {}
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

    def register_capability_provider(
        self,
        route: WorkerCapabilityRoute,
        executor: WorkerExecutor,
    ) -> None:
        """Register an executor for an explicit role/capability/provider route."""
        role = self._normalize_role(route.role)
        capability = self._normalize_route_value(route.capability)
        provider = self._normalize_route_value(route.provider)
        if not role:
            raise ValueError("capability route role must not be empty")
        if not capability:
            raise ValueError("capability route capability must not be empty")
        if not provider:
            raise ValueError("capability route provider must not be empty")

        route_key = (role, capability, provider)
        if route_key in self._capability_executors:
            raise ValueError("capability route is already registered")

        self._capability_executors[route_key] = executor

    def registered_roles(self) -> tuple[str, ...]:
        """Return currently registered executor role keys."""
        return tuple(self._executors.keys())

    def registered_capability_routes(self) -> tuple[tuple[str, str, str], ...]:
        """Return currently registered capability-provider route keys."""
        return tuple(self._capability_executors.keys())

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
                routing_reason="missing_executor_role",
            )

        capability_request = self._capability_for_action(action)
        provider_request = self._provider_for_action(action)
        if capability_request.present and not capability_request.valid:
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability_request.capability,
                requested_provider=provider_request.provider,
                routing_reason="invalid_capability",
            )

        if provider_request.present and not provider_request.valid:
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability_request.capability,
                requested_provider=provider_request.provider,
                routing_reason="invalid_capability_provider",
            )

        capability_resolution = self._resolve_capability_route(
            role=role,
            capability=capability_request.capability,
            provider=provider_request.provider,
        )
        if capability_resolution is not None:
            return capability_resolution

        if capability_request.present:
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability_request.capability,
                requested_provider=provider_request.provider,
                routing_reason="no_handler",
            )

        executor = self._executors.get(role)
        return WorkerExecutorResolution(
            executor=executor or self._fallback_executor,
            requested_role=role,
            registered=executor is not None,
            requested_capability=capability_request.capability,
            requested_provider=provider_request.provider,
            routing_reason="legacy_role_route" if executor is not None else "no_handler",
        )

    def _normalize_role(self, role: ExecutorRole | str | None) -> str:
        if isinstance(role, ExecutorRole):
            return role.value
        if isinstance(role, str):
            return role.strip()
        return ""

    def _normalize_route_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

    def _capability_for_action(self, action: Action) -> _CapabilityRouteRequest:
        if "capability" in action.payload:
            capability = self._normalize_route_value(action.payload.get("capability"))
            return _CapabilityRouteRequest(
                capability=capability or None,
                present=True,
                valid=bool(capability),
            )

        if "capability" in action.metadata:
            capability = self._normalize_route_value(action.metadata.get("capability"))
            return _CapabilityRouteRequest(
                capability=capability or None,
                present=True,
                valid=bool(capability),
            )

        action_type = self._normalize_route_value(action.type)
        return _CapabilityRouteRequest(
            capability=action_type or None,
            present=False,
            valid=bool(action_type),
        )

    def _provider_for_action(self, action: Action) -> _CapabilityProviderRequest:
        if "capability_provider" in action.payload:
            provider = self._normalize_route_value(
                action.payload.get("capability_provider")
            )
            return _CapabilityProviderRequest(
                provider=provider or None,
                present=True,
                valid=bool(provider),
            )

        if "capability_provider" in action.metadata:
            provider = self._normalize_route_value(
                action.metadata.get("capability_provider")
            )
            return _CapabilityProviderRequest(
                provider=provider or None,
                present=True,
                valid=bool(provider),
            )

        return _CapabilityProviderRequest(provider=None, present=False, valid=True)

    def _resolve_capability_route(
        self,
        role: str,
        capability: str | None,
        provider: str | None,
    ) -> WorkerExecutorResolution | None:
        if capability is None:
            return None

        if provider is not None:
            executor = self._capability_executors.get((role, capability, provider))
            return WorkerExecutorResolution(
                executor=executor or self._fallback_executor,
                requested_role=role,
                registered=executor is not None,
                requested_capability=capability,
                requested_provider=provider,
                matched_provider=provider if executor is not None else None,
                routing_reason="capability_route" if executor is not None else "no_handler",
            )

        matching_routes = [
            (
                route_provider,
                self._capability_executors[
                    (route_role, route_capability, route_provider)
                ],
            )
            for route_role, route_capability, route_provider in self._capability_executors
            if route_role == role and route_capability == capability
        ]
        if len(matching_routes) == 1:
            matched_provider, executor = matching_routes[0]
            return WorkerExecutorResolution(
                executor=executor,
                requested_role=role,
                registered=True,
                requested_capability=capability,
                requested_provider=None,
                matched_provider=matched_provider,
                routing_reason="capability_route",
            )
        if len(matching_routes) > 1:
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability,
                requested_provider=None,
                routing_reason="ambiguous_capability_route",
            )

        return None
