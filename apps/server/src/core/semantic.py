"""Explicit semantic dispatch to application handlers by Role and Capability."""

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from apps.server.src.core.actions import ExecutorRole

logger = logging.getLogger(__name__)


class SemanticRoutingError(ValueError):
    """No approved route exists for the supplied semantic intent."""


@dataclass(frozen=True)
class SemanticRoute:
    """Trusted application declaration, never a model-selected provider route."""

    intent: str
    role: ExecutorRole
    capability: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, ExecutorRole):
            raise ValueError("semantic route requires a known executor role")
        for value in (self.intent, self.capability):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError("semantic route identifiers must be non-blank and unpadded")


class SemanticRouter[RequestT, ResultT]:
    """Dispatch only declared intents; provider routing stays with existing handlers.

    Construction snapshots and validates the complete table. Requests pass through
    unchanged: this boundary cannot infer an account, provider or approval decision.
    Handlers retain responsibility for validating their application request.
    """

    def __init__(
        self,
        routes: Iterable[SemanticRoute],
        handlers: Mapping[tuple[ExecutorRole, str], Callable[[RequestT], ResultT]],
    ) -> None:
        self._handlers = dict(handlers)
        self._routes: dict[str, SemanticRoute] = {}
        for route in routes:
            if route.intent in self._routes:
                raise ValueError("duplicate semantic intent route")
            handler = self._handlers.get((route.role, route.capability))
            if not callable(handler):
                raise ValueError("semantic route requires a registered handler")
            self._routes[route.intent] = route

    def resolve(self, intent: str) -> SemanticRoute:
        """Resolve exact semantic identifiers without executing a handler."""
        route = self._routes.get(intent)
        if route is None:
            logger.info("semantic routing rejected unsupported intent")
            raise SemanticRoutingError("unsupported semantic intent")
        return route

    def execute(self, intent: str, request: RequestT) -> ResultT:
        """Invoke exactly the handler registered for the resolved Role/Capability."""
        route = self.resolve(intent)
        logger.info("semantic route dispatched", extra={
            "semantic_intent": route.intent,
            "executor_role": route.role.value,
            "capability": route.capability,
        })
        return self._handlers[(route.role, route.capability)](request)
