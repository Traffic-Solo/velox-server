from typing import Any

import pytest
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.events import (
    EventClassification,
    IntegrationRouteContext,
    ProcessedEvent,
    ResolvedContext,
    UniversalEvent,
)
from apps.server.src.core.planner import BasePlanner, Planner


def create_processed_event(
    category: str = "unknown",
    *,
    payload: dict[str, Any] | None = None,
    integration_route: IntegrationRouteContext | None = None,
) -> ProcessedEvent:
    event = UniversalEvent(
        source="gmail",
        type="message.received",
        payload=payload or {},
    )
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
        integration_route=integration_route,
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
    assert actions[0].target == str(actions[0].metadata["event_id"])


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


@pytest.mark.parametrize(
    "calendar_event_id",
    ["calendar-event-1", " calendar-event-1 ", "", 42, None],
)
def test_base_planner_copies_calendar_event_id_unchanged(
    calendar_event_id: Any,
) -> None:
    planner = BasePlanner()
    processed_event = create_processed_event(
        "calendar",
        payload={"calendar_event_id": calendar_event_id},
    )

    action = planner.plan(processed_event)[0]

    assert "calendar_event_id" in action.payload
    assert action.payload["calendar_event_id"] == calendar_event_id


def test_base_planner_does_not_add_or_fallback_missing_calendar_event_id() -> None:
    planner = BasePlanner()
    processed_event = create_processed_event("calendar")

    action = planner.plan(processed_event)[0]

    assert "calendar_event_id" not in action.payload
    assert action.target == str(processed_event.event.id)


def test_base_planner_propagates_integration_route_unchanged() -> None:
    planner = BasePlanner()
    processed_event = create_processed_event(
        "calendar",
        integration_route=IntegrationRouteContext(
            provider=" Calendar ",
            principal=" Principal ",
            account_identifier=" Account ",
        ),
    )

    action = planner.plan(processed_event)[0]

    assert action.payload == {
        "capability_provider": " Calendar ",
        "account_context": {
            "principal": " Principal ",
            "account_identifier": " Account ",
        },
    }
    assert "capability_provider" not in action.metadata
    assert "account_context" not in action.metadata


def test_base_planner_propagates_integration_route_to_non_calendar_action() -> None:
    planner = BasePlanner()
    processed_event = create_processed_event(
        "gmail",
        integration_route=IntegrationRouteContext(
            provider="gmail",
            principal=None,
            account_identifier="gmail-account",
        ),
    )

    action = planner.plan(processed_event)[0]

    assert action.payload == {
        "capability_provider": "gmail",
        "account_context": {
            "principal": None,
            "account_identifier": "gmail-account",
        },
    }


def test_base_planner_preserves_existing_non_routed_payload_behavior() -> None:
    planner = BasePlanner()

    action = planner.plan(create_processed_event("gmail"))[0]

    assert action.payload == {}


def test_base_planner_does_not_mutate_routed_calendar_event() -> None:
    planner = BasePlanner()
    processed_event = create_processed_event(
        "calendar",
        payload={"calendar_event_id": "calendar-event-1"},
        integration_route=IntegrationRouteContext(
            provider="calendar",
            principal="principal-1",
            account_identifier="calendar-account",
        ),
    )
    event_before = processed_event.event.model_dump()
    processed_event_before = processed_event.model_dump()

    planner.plan(processed_event)

    assert processed_event.event.model_dump() == event_before
    assert processed_event.model_dump() == processed_event_before
