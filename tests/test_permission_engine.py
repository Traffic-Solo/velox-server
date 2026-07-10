from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import (
    InMemoryActionLifecycleRepository,
)
from apps.server.src.core.actions import Action
from apps.server.src.core.permission import (
    BasePermissionEngine,
    PermissionDecision,
    PermissionEngine,
    PermissionEngineRuntime,
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
    lifecycle_repository = InMemoryActionLifecycleRepository()
    runtime = PermissionEngineRuntime(
        permission_engine=BasePermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
        lifecycle_repository=lifecycle_repository,
    )

    evaluations = runtime.evaluate([create_action()])

    assert evaluations[0].decision.status == PermissionStatus.ALLOWED
    assert evaluations[0].action.metadata["permission_decision"]["status"] == "allowed"
    lifecycle_state = lifecycle_repository.get(evaluations[0].action.id)
    assert lifecycle_state is not None
    assert lifecycle_state.status == ActionStatus.QUEUED


def test_permission_runtime_returns_denied_action_evaluation() -> None:
    class DenyingPermissionEngine:
        def evaluate(self, action: Action) -> PermissionDecision:
            return PermissionDecision(
                status=PermissionStatus.DENIED,
                reason="blocked",
            )

    lifecycle_repository = InMemoryActionLifecycleRepository()
    runtime = PermissionEngineRuntime(
        permission_engine=DenyingPermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
        lifecycle_repository=lifecycle_repository,
    )

    evaluations = runtime.evaluate([create_action()])

    assert evaluations[0].decision.status == PermissionStatus.DENIED
    assert evaluations[0].action.metadata["permission_decision"]["reason"] == "blocked"
    lifecycle_state = lifecycle_repository.get(evaluations[0].action.id)
    assert lifecycle_state is not None
    assert lifecycle_state.status == ActionStatus.REJECTED


def test_permission_runtime_defaults_to_deny_when_decision_cannot_be_resolved() -> None:
    class MissingDecisionPermissionEngine:
        def evaluate(self, action: Action) -> None:
            return None

    lifecycle_repository = InMemoryActionLifecycleRepository()
    runtime = PermissionEngineRuntime(
        permission_engine=MissingDecisionPermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
        lifecycle_repository=lifecycle_repository,
    )

    evaluations = runtime.evaluate([create_action()])

    assert evaluations[0].decision.status == PermissionStatus.DENIED
    rejected_state = lifecycle_repository.get(evaluations[0].action.id)
    assert rejected_state is not None
    assert rejected_state.status == ActionStatus.REJECTED
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
    lifecycle_repository = InMemoryActionLifecycleRepository()
    runtime = PermissionEngineRuntime(
        permission_engine=MixedPermissionEngine(),
        action_lifecycle_manager=ActionLifecycleManager(),
        lifecycle_repository=lifecycle_repository,
    )

    evaluations = runtime.evaluate([allowed_action, denied_action])

    assert runtime.queueable_actions(evaluations) == [evaluations[0].action]
    denied_state = lifecycle_repository.get(evaluations[1].action.id)
    assert denied_state is not None
    assert denied_state.status == ActionStatus.REJECTED
