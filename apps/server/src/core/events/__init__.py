"""Core event model exports.

The events package defines VELOX's canonical event contracts. These models are
pure data structures and intentionally contain no persistence, routing, or
business workflow logic.
"""

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
from apps.server.src.core.events.lifecycle import EventLifecycleState, EventStatus
from apps.server.src.core.events.lifecycle_manager import EventLifecycleManager
from apps.server.src.core.events.models import UniversalEvent
from apps.server.src.core.events.normalizer import (
    BaseEventNormalizer,
    EventNormalizer,
    NormalizationError,
)
from apps.server.src.core.events.pipeline import (
    EventProcessingPipeline,
    IntegrationRouteContext,
    ProcessedEvent,
)
from apps.server.src.core.events.repository import EventRepository
from apps.server.src.core.events.store import EventStore
from apps.server.src.core.events.workflow import (
    DuplicateEventError,
    EventAcceptanceResult,
    EventLifecycleConflictError,
    EventNotFoundError,
    EventProcessingError,
    EventProcessingResult,
    EventWorkflowService,
)

__all__ = [
    "BaseContextResolver",
    "BaseEventNormalizer",
    "ContextResolver",
    "DuplicateEventError",
    "EventAcceptanceResult",
    "EventClassification",
    "EventClassifier",
    "EventInbox",
    "EventLifecycleConflictError",
    "EventLifecycleManager",
    "EventLifecycleState",
    "EventNormalizer",
    "EventNotFoundError",
    "EventProcessingError",
    "EventProcessingPipeline",
    "EventProcessingResult",
    "EventRepository",
    "EventStatus",
    "EventStore",
    "EventWorkflowService",
    "IntegrationRouteContext",
    "NormalizationError",
    "ProcessedEvent",
    "ResolvedContext",
    "RuleBasedEventClassifier",
    "UniversalEvent",
]
