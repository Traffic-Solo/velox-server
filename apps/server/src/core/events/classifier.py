"""Rule-based event classification primitives."""

from typing import Protocol

from apps.server.src.core.events.models import UniversalEvent
from pydantic import BaseModel, ConfigDict


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

        if "github" in source or "github.com" in source:
            category = "github"
        elif "gmail" in source or "google-mail" in source:
            category = "gmail"
        elif (
            "calendar" in source
            or "google-calendar" in source
            or "apple-calendar" in source
        ):
            category = "calendar"
        else:
            category = "unknown"

        return EventClassification(
            category=category,
            confidence=1.0,
            labels=[event.source, event.type],
            reason="Rule-based v0 classification based on event source.",
        )
