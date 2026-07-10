"""Context resolution contracts for classified events."""

from typing import Any, Protocol

from apps.server.src.core.events.classifier import EventClassification
from apps.server.src.core.events.models import UniversalEvent
from pydantic import BaseModel, ConfigDict, Field


class ResolvedContext(BaseModel):
    """Context attached to a classified UniversalEvent."""

    model_config = ConfigDict(frozen=True)

    event: UniversalEvent
    classification: EventClassification
    context: dict[str, Any] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)
    confidence: float
    reason: str | None


class ContextResolver(Protocol):
    """Contract for resolving context around a classified event."""

    def resolve(
        self,
        event: UniversalEvent,
        classification: EventClassification,
    ) -> ResolvedContext:
        """Resolve context for an event and its classification."""
        ...


class BaseContextResolver:
    """Base v0 resolver that returns an empty context envelope."""

    def resolve(
        self,
        event: UniversalEvent,
        classification: EventClassification,
    ) -> ResolvedContext:
        """Return a ResolvedContext with no additional external context."""
        return ResolvedContext(
            event=event,
            classification=classification,
            context={},
            sources=[],
            confidence=classification.confidence,
            reason="Base context resolver v0 returned no additional context.",
        )
