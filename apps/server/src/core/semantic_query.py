"""Domain-neutral semantic query ingress: resolve, then dispatch a trusted route.

The request carries only caller-owned context. The resolver classifies text into
a canonical intent; the application route table alone decides which handler runs.
Handlers adapt this envelope to their own domain request inside trusted
composition and return a domain-neutral result envelope.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from apps.server.src.core.semantic import (
    SemanticInputError,
    SemanticResolution,
    SemanticResolutionStatus,
    SemanticResolver,
    SemanticResolverError,
    SemanticRouter,
    SemanticRoutingError,
)
from apps.server.src.workers.executor import WorkerAccountContext


class SemanticRequestError(ValueError):
    """A handler rejected caller-owned request context as invalid."""


@dataclass(frozen=True, slots=True)
class SemanticQueryRequest:
    """Caller-owned query context; never produced or amended by a resolver."""

    text: str
    account_context: WorkerAccountContext
    timezone: str


@dataclass(frozen=True, slots=True)
class ResolvedSemanticQuery:
    """Router input: the canonical intent plus the unchanged caller request."""

    intent: str
    request: SemanticQueryRequest


@dataclass(frozen=True, slots=True)
class SemanticQueryResult:
    """Domain-neutral handler output: canonical intent and a JSON-safe payload."""

    intent: str
    result: Mapping[str, object]


type SemanticQueryRouter = SemanticRouter[ResolvedSemanticQuery, SemanticQueryResult]


def execute_semantic_query(
    request: SemanticQueryRequest,
    *,
    resolver: SemanticResolver,
    router: SemanticQueryRouter,
    allowed_intents: Collection[str] | None = None,
) -> SemanticQueryResult:
    """Run the one canonical resolve-then-dispatch path.

    Raises ``SemanticInputError`` for invalid text, ``SemanticResolverError`` for
    classifier failure or malformed resolver output, ``SemanticRoutingError`` for
    unresolved, disallowed or unregistered intents (before any handler runs), and
    ``SemanticRequestError`` when a handler rejects the caller context.
    """
    try:
        # Typed as object: adapter output is checked at runtime, never trusted.
        resolution: object = resolver.resolve(request.text)
    except SemanticInputError:
        raise
    except Exception:
        raise SemanticResolverError("semantic resolution failed") from None
    if not isinstance(resolution, SemanticResolution):
        raise SemanticResolverError("semantic resolver returned an invalid result")
    intent = resolution.intent
    if resolution.status is not SemanticResolutionStatus.RESOLVED or intent is None:
        raise SemanticRoutingError("semantic query is unresolved")
    if allowed_intents is not None and intent not in allowed_intents:
        raise SemanticRoutingError("semantic intent is not allowed on this ingress")
    result: object = router.execute(
        intent, ResolvedSemanticQuery(intent=intent, request=request),
    )
    if not isinstance(result, SemanticQueryResult) or result.intent != intent:
        raise RuntimeError("semantic handler returned an invalid result")
    return result
