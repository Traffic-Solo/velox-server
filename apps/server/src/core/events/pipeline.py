"""Event processing pipeline primitives."""

from apps.server.src.core.events.classifier import (
    EventClassification,
    EventClassifier,
)
from apps.server.src.core.events.context import ContextResolver, ResolvedContext
from apps.server.src.core.events.models import UniversalEvent
from pydantic import BaseModel, ConfigDict


class ProcessedEvent(BaseModel):
    """Result of processing a UniversalEvent through the v0 pipeline."""

    model_config = ConfigDict(frozen=True)

    event: UniversalEvent
    classification: EventClassification
    context: ResolvedContext


class EventProcessingPipeline:
    """Synchronous v0 pipeline for classifying and resolving event context."""

    def __init__(
        self,
        classifier: EventClassifier,
        context_resolver: ContextResolver,
    ) -> None:
        self._classifier = classifier
        self._context_resolver = context_resolver

    def process(self, event: UniversalEvent) -> ProcessedEvent:
        """Classify an event, resolve its context, and return the result."""
        classification = self._classifier.classify(event)
        context = self._context_resolver.resolve(event, classification)

        return ProcessedEvent(
            event=event,
            classification=classification,
            context=context,
        )
