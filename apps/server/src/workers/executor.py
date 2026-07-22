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
    requested_account_context: "WorkerAccountContext | None" = None
    matched_account_context: "WorkerAccountContext | None" = None
    routing_reason: str | None = None

    def execute(self, action: Action) -> WorkerExecutionResult:
        """Execute through the resolved capability and account route."""
        return self.executor.execute(
            action,
            capability=self.requested_capability if self.registered else None,
            account_context=self.matched_account_context,
        )


@runtime_checkable
class WorkerExecutor(Protocol):
    """Contract for role-compatible action executors."""

    def execute(
        self,
        action: Action,
        *,
        capability: str | None = None,
        account_context: "WorkerAccountContext | None" = None,
    ) -> WorkerExecutionResult:
        """Execute an action and return an explicit execution result."""


class NoOpWorkerExecutor:
    """Safe default executor that performs no external work.

    Returns SKIPPED, never SUCCEEDED: no work was done, and reporting success
    for a no-op would hide unhandled actions (Nothing Dies Silently).
    """

    def execute(
        self,
        action: Action,
        *,
        capability: str | None = None,
        account_context: "WorkerAccountContext | None" = None,
    ) -> WorkerExecutionResult:
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
class WorkerAccountContext:
    """Explicit vendor-neutral account context used for provider routing."""

    principal: str | None
    account_identifier: str

    def as_metadata(self) -> dict[str, str | None]:
        """Return JSON-compatible account context metadata."""
        return {
            "principal": self.principal,
            "account_identifier": self.account_identifier,
        }


@dataclass(frozen=True)
class WorkerCapabilityRoute:
    """Explicit capability-provider route for a vendor-neutral executor role."""

    role: ExecutorRole | str
    capability: str
    provider: str
    account_context: WorkerAccountContext | None = None


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


@dataclass(frozen=True)
class _AccountContextRequest:
    account_context: WorkerAccountContext | None
    present: bool
    valid: bool


class WorkerExecutorRegistry:
    """Vendor-neutral registry for resolving action executors."""

    def __init__(self, fallback_executor: WorkerExecutor | None = None) -> None:
        self._executors: dict[str, WorkerExecutor] = {}
        self._capability_executors: dict[
            tuple[str, str, str, str | None],
            WorkerExecutor,
        ] = {}
        self._capability_account_contexts: dict[
            tuple[str, str, str, str | None],
            WorkerAccountContext | None,
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
        account_context = self._normalize_account_context(route.account_context)
        account_key = (
            account_context.account_identifier
            if account_context is not None
            else None
        )
        if not role:
            raise ValueError("capability route role must not be empty")
        if not capability:
            raise ValueError("capability route capability must not be empty")
        if not provider:
            raise ValueError("capability route provider must not be empty")
        if route.account_context is not None and account_context is None:
            raise ValueError("capability route account context must not be empty")

        route_key = (role, capability, provider, account_key)
        if route_key in self._capability_executors:
            raise ValueError("capability route is already registered")

        self._capability_executors[route_key] = executor
        self._capability_account_contexts[route_key] = account_context

    def registered_roles(self) -> tuple[str, ...]:
        """Return currently registered executor role keys."""
        return tuple(self._executors.keys())

    def registered_capability_routes(self) -> tuple[tuple[str, str, str, str | None], ...]:
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
        account_context_request = self._account_context_for_action(action)
        if capability_request.present and not capability_request.valid:
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability_request.capability,
                requested_provider=provider_request.provider,
                requested_account_context=account_context_request.account_context,
                routing_reason="invalid_capability",
            )

        if provider_request.present and not provider_request.valid:
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability_request.capability,
                requested_provider=provider_request.provider,
                requested_account_context=account_context_request.account_context,
                routing_reason="invalid_capability_provider",
            )

        if account_context_request.present and not account_context_request.valid:
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability_request.capability,
                requested_provider=provider_request.provider,
                requested_account_context=account_context_request.account_context,
                routing_reason="invalid_account_context",
            )

        capability_resolution = self._resolve_capability_route(
            role=role,
            capability=capability_request.capability,
            provider=provider_request.provider,
            account_context=account_context_request.account_context,
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
                requested_account_context=account_context_request.account_context,
                routing_reason="no_handler",
            )

        executor = self._executors.get(role)
        return WorkerExecutorResolution(
            executor=executor or self._fallback_executor,
            requested_role=role,
            registered=executor is not None,
            requested_capability=capability_request.capability,
            requested_provider=provider_request.provider,
            requested_account_context=account_context_request.account_context,
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

    def _normalize_account_context(
        self,
        account_context: WorkerAccountContext | None,
    ) -> WorkerAccountContext | None:
        if account_context is None:
            return None

        account_identifier = self._normalize_route_value(
            account_context.account_identifier
        )
        principal = (
            self._normalize_route_value(account_context.principal)
            if account_context.principal is not None
            else None
        )
        if not account_identifier:
            return None
        if account_context.principal is not None and not principal:
            return None

        return WorkerAccountContext(
            principal=principal,
            account_identifier=account_identifier,
        )

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

    def _account_context_for_action(self, action: Action) -> _AccountContextRequest:
        if "account_context" in action.payload:
            return self._parse_account_context(action.payload.get("account_context"))

        if "account_context" in action.metadata:
            return self._parse_account_context(action.metadata.get("account_context"))

        return _AccountContextRequest(
            account_context=None,
            present=False,
            valid=True,
        )

    def _parse_account_context(self, value: Any) -> _AccountContextRequest:
        if not isinstance(value, dict):
            return _AccountContextRequest(
                account_context=None,
                present=True,
                valid=False,
            )

        account_identifier = self._normalize_route_value(
            value.get("account_identifier")
        )
        principal = value.get("principal")
        normalized_principal = (
            self._normalize_route_value(principal)
            if principal is not None
            else None
        )
        if principal is not None and not normalized_principal:
            return _AccountContextRequest(
                account_context=None,
                present=True,
                valid=False,
            )
        if not account_identifier:
            return _AccountContextRequest(
                account_context=None,
                present=True,
                valid=False,
            )

        return _AccountContextRequest(
            account_context=WorkerAccountContext(
                principal=normalized_principal,
                account_identifier=account_identifier,
            ),
            present=True,
            valid=True,
        )

    def _resolve_capability_route(
        self,
        role: str,
        capability: str | None,
        provider: str | None,
        account_context: WorkerAccountContext | None,
    ) -> WorkerExecutorResolution | None:
        if capability is None:
            return None

        if provider is not None:
            matching_routes = self._matching_capability_routes(
                role=role,
                capability=capability,
                provider=provider,
                account_context=account_context,
            )
            if len(matching_routes) == 1:
                matched_provider, matched_account_context, executor = matching_routes[0]
                return WorkerExecutorResolution(
                    executor=executor,
                    requested_role=role,
                    registered=True,
                    requested_capability=capability,
                    requested_provider=provider,
                    matched_provider=matched_provider,
                    requested_account_context=account_context,
                    matched_account_context=matched_account_context,
                    routing_reason="capability_route",
                )
            if len(matching_routes) > 1:
                return WorkerExecutorResolution(
                    executor=self._fallback_executor,
                    requested_role=role,
                    registered=False,
                    requested_capability=capability,
                    requested_provider=provider,
                    requested_account_context=account_context,
                    routing_reason="ambiguous_capability_route",
                )
            if self._has_account_specific_routes(role, capability, provider):
                routing_reason = (
                    "missing_account_context"
                    if account_context is None
                    else "no_handler"
                )
            else:
                routing_reason = "no_handler"
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability,
                requested_provider=provider,
                requested_account_context=account_context,
                routing_reason=routing_reason,
            )

        matching_routes = self._matching_capability_routes(
            role=role,
            capability=capability,
            provider=None,
            account_context=account_context,
        )
        if len(matching_routes) == 1:
            matched_provider, matched_account_context, executor = matching_routes[0]
            return WorkerExecutorResolution(
                executor=executor,
                requested_role=role,
                registered=True,
                requested_capability=capability,
                requested_provider=None,
                matched_provider=matched_provider,
                requested_account_context=account_context,
                matched_account_context=matched_account_context,
                routing_reason="capability_route",
            )
        if len(matching_routes) > 1:
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability,
                requested_provider=None,
                requested_account_context=account_context,
                routing_reason="ambiguous_capability_route",
            )
        if account_context is None and self._has_account_specific_routes(
            role,
            capability,
            provider=None,
        ):
            return WorkerExecutorResolution(
                executor=self._fallback_executor,
                requested_role=role,
                registered=False,
                requested_capability=capability,
                requested_provider=None,
                requested_account_context=None,
                routing_reason="missing_account_context",
            )

        return None

    def _matching_capability_routes(
        self,
        role: str,
        capability: str,
        provider: str | None,
        account_context: WorkerAccountContext | None,
    ) -> list[tuple[str, WorkerAccountContext | None, WorkerExecutor]]:
        account_key = (
            account_context.account_identifier
            if account_context is not None
            else None
        )
        return [
            (
                route_provider,
                self._capability_account_contexts[
                    (
                        route_role,
                        route_capability,
                        route_provider,
                        route_account_key,
                    )
                ],
                executor,
            )
            for (
                route_role,
                route_capability,
                route_provider,
                route_account_key,
            ), executor in self._capability_executors.items()
            if route_role == role
            and route_capability == capability
            and (provider is None or route_provider == provider)
            and route_account_key == account_key
            and self._capability_account_contexts[
                (
                    route_role,
                    route_capability,
                    route_provider,
                    route_account_key,
                )
            ]
            == account_context
        ]

    def _has_account_specific_routes(
        self,
        role: str,
        capability: str,
        provider: str | None,
    ) -> bool:
        return any(
            route_role == role
            and route_capability == capability
            and (provider is None or route_provider == provider)
            and route_account_key is not None
            for (
                route_role,
                route_capability,
                route_provider,
                route_account_key,
            ) in self._capability_executors
        )
