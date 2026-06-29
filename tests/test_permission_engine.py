from apps.server.src.core.actions import Action
from apps.server.src.core.permission import (
    BasePermissionEngine,
    PermissionDecision,
    PermissionEngine,
    PermissionStatus,
)


def create_action() -> Action:
    return Action(type="summarize_email", target="event-1")


def test_base_permission_engine_returns_permission_decision() -> None:
    engine = BasePermissionEngine()

    decision = engine.evaluate(create_action())

    assert isinstance(decision, PermissionDecision)


def test_base_permission_engine_default_decision_is_allowed() -> None:
    engine = BasePermissionEngine()

    decision = engine.evaluate(create_action())

    assert decision.status == PermissionStatus.ALLOWED


def test_base_permission_engine_evaluate_accepts_action() -> None:
    engine = BasePermissionEngine()
    action = create_action()

    decision = engine.evaluate(action)

    assert isinstance(decision, PermissionDecision)


def test_base_permission_engine_does_not_mutate_action() -> None:
    engine = BasePermissionEngine()
    action = create_action()
    before = action.model_dump()

    engine.evaluate(action)

    assert action.model_dump() == before


def test_permission_engine_protocol_is_compatible_with_base_permission_engine() -> None:
    engine: PermissionEngine = BasePermissionEngine()

    decision = engine.evaluate(create_action())

    assert decision.status == PermissionStatus.ALLOWED
