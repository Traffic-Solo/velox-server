from apps.server.src.core.events import (
    EventClassification,
    EventProcessingPipeline,
    ProcessedEvent,
    ResolvedContext,
    UniversalEvent,
)


class RecordingClassifier:
    def __init__(self, classification: EventClassification) -> None:
        self.classification = classification
        self.calls = 0

    def classify(self, event: UniversalEvent) -> EventClassification:
        self.calls += 1
        return self.classification


class RecordingContextResolver:
    def __init__(self) -> None:
        self.calls = 0
        self.context: ResolvedContext | None = None

    def resolve(
        self,
        event: UniversalEvent,
        classification: EventClassification,
    ) -> ResolvedContext:
        self.calls += 1
        self.context = ResolvedContext(
            event=event,
            classification=classification,
            context={},
            sources=[],
            confidence=classification.confidence,
            reason="test context",
        )
        return self.context


def create_pipeline_parts():
    event = UniversalEvent(source="gmail", type="message.received")
    classification = EventClassification(
        category="communication",
        confidence=1.0,
        labels=["gmail", "message.received"],
        reason="test classification",
    )
    classifier = RecordingClassifier(classification)
    resolver = RecordingContextResolver()
    pipeline = EventProcessingPipeline(
        classifier=classifier,
        context_resolver=resolver,
    )

    return event, classification, classifier, resolver, pipeline


def test_pipeline_classifies_event() -> None:
    event, classification, _, _, pipeline = create_pipeline_parts()

    processed = pipeline.process(event)

    assert processed.classification == classification


def test_pipeline_resolves_context() -> None:
    event, _, _, resolver, pipeline = create_pipeline_parts()

    processed = pipeline.process(event)

    assert processed.context == resolver.context


def test_processed_event_contains_original_event() -> None:
    event, _, _, _, pipeline = create_pipeline_parts()

    processed = pipeline.process(event)

    assert isinstance(processed, ProcessedEvent)
    assert processed.event == event


def test_processed_event_contains_classification() -> None:
    event, classification, _, _, pipeline = create_pipeline_parts()

    processed = pipeline.process(event)

    assert processed.classification == classification


def test_processed_event_contains_resolved_context() -> None:
    event, _, _, resolver, pipeline = create_pipeline_parts()

    processed = pipeline.process(event)

    assert processed.context == resolver.context


def test_classifier_and_resolver_are_called_exactly_once() -> None:
    event, _, classifier, resolver, pipeline = create_pipeline_parts()

    pipeline.process(event)

    assert classifier.calls == 1
    assert resolver.calls == 1
