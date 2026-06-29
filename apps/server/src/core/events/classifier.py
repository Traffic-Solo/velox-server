"""Rule-based event classification primitives."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from apps.server.src.core.events.models import UniversalEvent


class EventClassification(BaseModel):
    """Classification result for a UniversalEvent."""

    model_config = ConfigDict(frozen=True)

    category: str
    confidence: float
    labels: list[str]
    reason: str | None


class EventClassifier(Protocol):
    """Contract for event classifiers."""

    def classify(self, event: UniversalEvent) -> EventClassification:
        """Classify an event into a category with labels and confidence."""
        ...


class RuleBasedEventClassifier:
    """Rule-based v0 classifier using event source matching."""

    def classify(self, event: UniversalEvent) -> EventClassification:
        """Classify an event using static source-name rules."""
        source = event.source.lower()

        if "gmail" in source:
            category = "communication"
        elif "calendar" in source:
            category = "schedule"
        elif "github" in source:
            category = "development"
        elif "system" in source:
            category = "system"
        else:
            category = "unknown"

        return EventClassification(
            category=category,
            confidence=1.0,
            labels=[event.source, event.type],
            reason="Rule-based v0 classification based on event source.",
        )
