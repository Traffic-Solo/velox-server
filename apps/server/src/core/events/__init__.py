"""Core event model exports.

The events package defines VELOX's canonical event contracts. These models are
pure data structures and intentionally contain no persistence, routing, or
business workflow logic.
"""

from apps.server.src.core.events.models import UniversalEvent
from apps.server.src.core.events.classifier import (
    EventClassification,
    EventClassifier,
    RuleBasedEventClassifier,
)
from apps.server.src.core.events.context import (
    BaseContextResolver,
    ContextResolver,
    ResolvedContext,
)
from apps.server.src.core.events.inbox import EventInbox
from apps.server.src.core.events.normalizer import (
    BaseEventNormalizer,
    EventNormalizer,
    NormalizationError,
)
from apps.server.src.core.events.repository import EventRepository
from apps.server.src.core.events.store import EventStore

__all__ = [
    "BaseContextResolver",
    "BaseEventNormalizer",
    "ContextResolver",
    "EventClassification",
    "EventClassifier",
    "EventInbox",
    "EventNormalizer",
    "EventRepository",
    "EventStore",
    "NormalizationError",
    "ResolvedContext",
    "RuleBasedEventClassifier",
    "UniversalEvent",
]
