"""Event processing pipeline primitives."""

from typing import Any

from apps.server.src.core.events.classifier import (
    EventClassification,
    EventClassifier,
)
from apps.server.src.core.events.context import ContextResolver, ResolvedContext
from apps.server.src.core.events.models import UniversalEvent
from pydantic import BaseModel, ConfigDict, field_validator


class IntegrationRouteContext(BaseModel):
    """Explicit vendor-neutral provider and account routing input."""

    model_config = ConfigDict(frozen=True)

    provider: str
    principal: str | None = None
    account_identifier: str

    @field_validator("provider", "account_identifier", "principal", mode="before")
    @classmethod
    def require_non_blank_strings(cls, value: Any) -> Any:
        """Reject non-string and blank route values without normalizing them."""
        if value is None:
            return value
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-blank string")
        return value


class ProcessedEvent(BaseModel):
    """Result of processing a UniversalEvent through the v0 pipeline."""

    model_config = ConfigDict(frozen=True)

    event: UniversalEvent
    classification: EventClassification
    context: ResolvedContext
    integration_route: IntegrationRouteContext | None = None


class EventProcessingPipeline:
    """Synchronous v0 pipeline for classifying and resolving event context."""

    def __init__(
        self,
        classifier: EventClassifier,
        context_resolver: ContextResolver,
    ) -> None:
        self._classifier = classifier
        self._context_resolver = context_resolver

    def process(
        self,
        event: UniversalEvent,
        *,
        integration_route: IntegrationRouteContext | None = None,
    ) -> ProcessedEvent:
        """Classify an event, resolve its context, and return the result."""
        classification = self._classifier.classify(event)
        context = self._context_resolver.resolve(event, classification)

        return ProcessedEvent(
            event=event,
            classification=classification,
            context=context,
            integration_route=integration_route,
        )
