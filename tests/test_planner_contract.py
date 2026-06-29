from apps.server.src.core.actions import Action
from apps.server.src.core.events import (
    EventClassification,
    ProcessedEvent,
    ResolvedContext,
    UniversalEvent,
)
from apps.server.src.core.planner import BasePlanner, Planner


def create_processed_event() -> ProcessedEvent:
    event = UniversalEvent(source="gmail", type="message.received")
    classification = EventClassification(
        category="communication",
        confidence=1.0,
        labels=["gmail", "message.received"],
        reason="test classification",
    )
    context = ResolvedContext(
        event=event,
        classification=classification,
        context={},
        sources=[],
        confidence=classification.confidence,
        reason="test context",
    )

    return ProcessedEvent(
        event=event,
        classification=classification,
        context=context,
    )


def test_base_planner_returns_list() -> None:
    planner = BasePlanner()

    actions = planner.plan(create_processed_event())

    assert isinstance(actions, list)


def test_base_planner_returns_empty_list_by_default() -> None:
    planner = BasePlanner()

    assert planner.plan(create_processed_event()) == []


def test_base_planner_does_not_mutate_processed_event() -> None:
    planner = BasePlanner()
    processed_event = create_processed_event()
    before = processed_event.model_dump()

    planner.plan(processed_event)

    assert processed_event.model_dump() == before


def test_planner_protocol_is_compatible_with_base_planner() -> None:
    planner: Planner = BasePlanner()

    assert planner.plan(create_processed_event()) == []


def test_plan_accepts_processed_event() -> None:
    planner = BasePlanner()
    processed_event = create_processed_event()

    actions: list[Action] = planner.plan(processed_event)

    assert actions == []
