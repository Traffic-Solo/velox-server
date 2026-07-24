from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.events import (
    EventClassification,
    IntegrationRouteContext,
    ProcessedEvent,
    ResolvedContext,
    UniversalEvent,
)
from apps.server.src.core.planner import BasePlanner, Planner


def create_processed_event(category: str = "unknown") -> ProcessedEvent:
    event = UniversalEvent(source="gmail", type="message.received")
    classification = EventClassification(
        category=category,
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


def test_base_planner_generates_review_pull_request_for_github() -> None:
    planner = BasePlanner()

    actions = planner.plan(create_processed_event("github"))

    assert len(actions) == 1
    assert actions[0].type == "review_pull_request"
    assert actions[0].executor_role == ExecutorRole.CONTENT_REVIEW


def test_base_planner_generates_summarize_email_for_gmail() -> None:
    planner = BasePlanner()

    actions = planner.plan(create_processed_event("gmail"))

    assert len(actions) == 1
    assert actions[0].type == "summarize_email"
    assert actions[0].executor_role == ExecutorRole.CONTENT_SUMMARY


def test_base_planner_generates_prepare_meeting_for_calendar() -> None:
    planner = BasePlanner()

    actions = planner.plan(create_processed_event("calendar"))

    assert len(actions) == 1
    assert actions[0].type == "prepare_meeting"
    assert actions[0].executor_role == ExecutorRole.CONTEXT_PREPARATION


def test_base_planner_returns_empty_list_for_unknown_category() -> None:
    planner = BasePlanner()

    actions = planner.plan(create_processed_event("unknown"))

    assert actions == []


def test_base_planner_remains_deterministic() -> None:
    planner = BasePlanner()
    processed_event = create_processed_event("gmail")

    first_actions = planner.plan(processed_event)
    second_actions = planner.plan(processed_event)

    assert [action.model_dump(exclude={"id", "created_at"}) for action in first_actions] == [
        action.model_dump(exclude={"id", "created_at"}) for action in second_actions
    ]


def test_base_planner_does_not_propagate_integration_route() -> None:
    planner = BasePlanner()
    processed_event = create_processed_event("calendar").model_copy(
        update={
            "integration_route": IntegrationRouteContext(
                provider="calendar",
                principal="principal-1",
                account_identifier="calendar-account",
            )
        }
    )

    action = planner.plan(processed_event)[0]

    assert action.payload == {}
    assert "capability_provider" not in action.metadata
    assert "account_context" not in action.metadata
