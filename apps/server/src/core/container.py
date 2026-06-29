"""Application service container."""

from uuid import UUID

from apps.server.src.core.events import (
    BaseContextResolver,
    EventInbox,
    EventLifecycleManager,
    EventLifecycleState,
    EventProcessingPipeline,
    EventRepository,
    EventStore,
    RuleBasedEventClassifier,
)
from apps.server.src.core.events.classifier import EventClassifier
from apps.server.src.core.events.context import ContextResolver


class ApplicationContainer:
    """Wires current in-process application services."""

    def __init__(self) -> None:
        self.event_repository: EventRepository = EventStore()
        self.event_inbox = EventInbox()
        self.event_lifecycle_manager = EventLifecycleManager()
        self.event_lifecycle_states: dict[UUID, EventLifecycleState] = {}
        self.event_classifier: EventClassifier = RuleBasedEventClassifier()
        self.context_resolver: ContextResolver = BaseContextResolver()
        self.event_processing_pipeline = EventProcessingPipeline(
            classifier=self.event_classifier,
            context_resolver=self.context_resolver,
        )


_container: ApplicationContainer | None = None


def get_container() -> ApplicationContainer:
    """Return the process-wide application container singleton."""
    global _container

    if _container is None:
        _container = ApplicationContainer()

    return _container
