import pytest
from apps.server.src.core.events import (
    EventClassification,
    EventProcessingPipeline,
    IntegrationRouteContext,
    ProcessedEvent,
    ResolvedContext,
    UniversalEvent,
)
from pydantic import ValidationError


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


def test_integration_route_context_accepts_and_preserves_valid_values() -> None:
    route = IntegrationRouteContext(
        provider=" Calendar ",
        principal=" Principal ",
        account_identifier=" Account ",
    )

    assert route.provider == " Calendar "
    assert route.principal == " Principal "
    assert route.account_identifier == " Account "


def test_integration_route_context_defaults_omitted_principal_to_none() -> None:
    route = IntegrationRouteContext(
        provider="calendar",
        account_identifier="calendar-account",
    )

    assert route.principal is None


def test_integration_route_context_is_immutable() -> None:
    route = IntegrationRouteContext(
        provider="calendar",
        account_identifier="calendar-account",
    )

    with pytest.raises(ValidationError):
        route.provider = "gmail"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", ""),
        ("provider", "   "),
        ("provider", 123),
        ("account_identifier", ""),
        ("account_identifier", "   "),
        ("account_identifier", 123),
        ("principal", ""),
        ("principal", "   "),
        ("principal", 123),
    ],
)
def test_integration_route_context_rejects_invalid_values(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "provider": "calendar",
        "principal": None,
        "account_identifier": "calendar-account",
    }
    values[field] = value

    with pytest.raises(ValidationError):
        IntegrationRouteContext.model_validate(values)


def test_pipeline_preserves_explicit_integration_route() -> None:
    event, _, _, _, pipeline = create_pipeline_parts()
    route = IntegrationRouteContext(
        provider="calendar",
        principal="principal-1",
        account_identifier="calendar-account",
    )

    processed = pipeline.process(event, integration_route=route)

    assert processed.integration_route is route


def test_pipeline_defaults_integration_route_to_none() -> None:
    event, _, _, _, pipeline = create_pipeline_parts()

    processed = pipeline.process(event)

    assert processed.integration_route is None


def test_pipeline_route_input_does_not_mutate_original_event() -> None:
    event, _, _, _, pipeline = create_pipeline_parts()
    before = event.model_dump()
    route = IntegrationRouteContext(
        provider="calendar",
        principal=None,
        account_identifier="calendar-account",
    )

    pipeline.process(event, integration_route=route)

    assert event.model_dump() == before


def test_pipeline_does_not_extract_integration_route_from_event_data() -> None:
    untrusted_fields = {
        "account_context": {
            "principal": "untrusted",
            "account_identifier": "untrusted-account",
        },
        "capability_provider": "untrusted-provider",
        "provider": "untrusted-provider",
    }
    event = UniversalEvent(
        source="calendar",
        type="event.updated",
        payload=untrusted_fields,
        metadata=untrusted_fields,
    )
    _, classification, classifier, resolver, _ = create_pipeline_parts()
    classifier.classification = classification
    pipeline = EventProcessingPipeline(
        classifier=classifier,
        context_resolver=resolver,
    )

    processed = pipeline.process(event)

    assert processed.integration_route is None
