from apps.server.src.core.actions import Action
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.permission import (
    BasePermissionEngine,
    PermissionDecision,
    PermissionEngineRuntime,
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


def test_permission_runtime_returns_allowed_action_evaluation() -> None:
    runtime = PermissionEngineRuntime(
        permission_engine=BasePermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
    )

    evaluations = runtime.evaluate([create_action()])

    assert evaluations[0].decision.status == PermissionStatus.ALLOWED
    assert evaluations[0].action.metadata["permission_decision"]["status"] == "allowed"
    assert evaluations[0].action.metadata["action_lifecycle"]["status"] == "queued"


def test_permission_runtime_returns_denied_action_evaluation() -> None:
    class DenyingPermissionEngine:
        def evaluate(self, action: Action) -> PermissionDecision:
            return PermissionDecision(
                status=PermissionStatus.DENIED,
                reason="blocked",
            )

    runtime = PermissionEngineRuntime(
        permission_engine=DenyingPermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
    )

    evaluations = runtime.evaluate([create_action()])

    assert evaluations[0].decision.status == PermissionStatus.DENIED
    assert evaluations[0].action.status == "rejected"
    assert evaluations[0].action.metadata["permission_decision"]["reason"] == "blocked"
    assert evaluations[0].action.metadata["action_lifecycle"]["status"] == "rejected"


def test_permission_runtime_defaults_to_deny_when_decision_cannot_be_resolved() -> None:
    class MissingDecisionPermissionEngine:
        def evaluate(self, action: Action) -> None:
            return None

    runtime = PermissionEngineRuntime(
        permission_engine=MissingDecisionPermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
    )

    evaluations = runtime.evaluate([create_action()])

    assert evaluations[0].decision.status == PermissionStatus.DENIED
    assert evaluations[0].action.status == "rejected"
    assert (
        evaluations[0].decision.reason
        == "permission decision could not be resolved"
    )


def test_permission_runtime_filters_queueable_actions() -> None:
    class MixedPermissionEngine:
        def evaluate(self, action: Action) -> PermissionDecision:
            if action.type == "summarize_email":
                return PermissionDecision(status=PermissionStatus.ALLOWED)
            return PermissionDecision(status=PermissionStatus.DENIED)

    allowed_action = Action(type="summarize_email", target="event-1")
    denied_action = Action(type="prepare_meeting", target="event-2")
    runtime = PermissionEngineRuntime(
        permission_engine=MixedPermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
    )

    evaluations = runtime.evaluate([allowed_action, denied_action])

    assert runtime.queueable_actions(evaluations) == [evaluations[0].action]
    assert evaluations[1].action.status == "rejected"
