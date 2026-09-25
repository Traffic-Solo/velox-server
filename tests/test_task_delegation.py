"""Offline coverage for the vendor-neutral task delegation boundary."""

import ast
import socket
from dataclasses import fields
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from apps.server.src.core.action_lifecycle import ActionStatus
from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.delegation import (
    ActionTaskDelegator,
    TaskDelegationRequest,
    TaskDelegationRequestError,
    TaskDelegationResult,
    TaskDelegationStatus,
)
from apps.server.src.core.events import UniversalEvent
from apps.server.src.core.permission import (
    PermissionDecision,
    PermissionEngineRuntime,
    PermissionStatus,
)
from apps.server.src.integrations.gmail import GMAIL_ACCOUNT_CONTEXT
from apps.server.src.workers.executor import (
    NoOpWorkerExecutor,
    WorkerAccountContext,
    WorkerCapability,
    WorkerExecutionResult,
    WorkerExecutorRegistry,
)
from apps.server.src.workers.runtime import WorkerRuntime, WorkerRuntimeInvocationService

SRC = Path(__file__).resolve().parents[1] / "apps" / "server" / "src"


def request(**overrides: Any) -> TaskDelegationRequest:
    values: dict[str, Any] = {
        "objective": "Summarize the latest message for the owner",
        "target": "gmail-message-1",
        "executor_role": ExecutorRole.CONTENT_SUMMARY,
        "capability": "summarize_email",
        "account_context": GMAIL_ACCOUNT_CONTEXT,
    }
    values.update(overrides)
    return TaskDelegationRequest(**values)


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> ApplicationContainer:
    """Container whose workers fail loudly if anything tries to execute."""

    def no_execution(*args: object, **kwargs: object) -> WorkerExecutionResult:
        raise AssertionError("delegation must not execute a worker")

    instance = ApplicationContainer()
    monkeypatch.setattr(instance.gmail_worker_executor, "execute", no_execution)
    monkeypatch.setattr(instance.calendar_worker_executor, "execute", no_execution)
    monkeypatch.setattr(WorkerRuntime, "process_next", no_execution)
    monkeypatch.setattr(WorkerRuntimeInvocationService, "invoke", no_execution)
    return instance


def pending_ids(container: ApplicationContainer) -> list[UUID]:
    return [action.id for action in container.pending_approval_registry.list_pending()]


def test_request_is_typed_and_carries_no_provider_or_execution_fields() -> None:
    names = {field.name for field in fields(TaskDelegationRequest)}
    assert names == {"objective", "target", "executor_role", "capability", "account_context"}
    with pytest.raises(TypeError):
        TaskDelegationRequest(  # type: ignore[call-arg]
            objective="o", target="t", executor_role=ExecutorRole.CONTENT_SUMMARY,
            capability="summarize_email", capability_provider="gmail",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"objective": ""}, {"objective": "   "}, {"target": ""}, {"target": "\t"},
        {"capability": ""}, {"capability": " Summarize_Email "},
        {"executor_role": "content_summary"}, {"objective": None},
        {"account_context": WorkerAccountContext(principal="p", account_identifier=" ")},
        {"account_context": WorkerAccountContext(principal="", account_identifier="acct")},
        {"account_context": {"account_identifier": "acct"}},
    ],
)
def test_invalid_requests_fail_before_any_side_effect(
    container: ApplicationContainer, overrides: dict[str, Any],
) -> None:
    with pytest.raises(TaskDelegationRequestError):
        container.task_delegator.delegate(request(**overrides))
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []


def test_allowed_task_creates_expected_action_and_is_queued_not_executed(
    container: ApplicationContainer,
) -> None:
    result = container.task_delegator.delegate(request())

    assert result == TaskDelegationResult(
        action_id=result.action_id,
        status=TaskDelegationStatus.QUEUED,
        permission_status=PermissionStatus.ALLOWED,
        routing_reason="capability_route",
    )
    assert result.queued is True
    [queued] = container.action_queue.list()
    assert queued.id == result.action_id
    assert queued.type == "summarize_email"
    assert queued.target == "gmail-message-1"
    assert queued.executor_role == ExecutorRole.CONTENT_SUMMARY
    assert queued.payload == {
        "capability": "summarize_email",
        "objective": "Summarize the latest message for the owner",
        "account_context": GMAIL_ACCOUNT_CONTEXT.as_metadata(),
    }
    assert queued.metadata["task_delegation"] == {
        "requested_role": "content_summary",
        "requested_capability": "summarize_email",
    }
    assert "capability_provider" not in queued.payload
    assert "capability_provider" not in queued.metadata
    lifecycle = container.action_lifecycle_repository.get(result.action_id)
    assert lifecycle is not None and lifecycle.status == ActionStatus.APPROVED
    assert container.worker_execution_observer.list() == []


def test_registry_selects_the_unique_provider_without_caller_input(
    container: ApplicationContainer,
) -> None:
    container.task_delegator.delegate(request())
    [queued] = container.action_queue.list()
    resolution = container.worker_executor_registry.resolve_with_registration(queued)
    assert resolution.registered is True
    assert resolution.requested_provider is None
    assert resolution.matched_provider == "gmail"


def test_approval_required_task_is_held_and_not_queued(
    container: ApplicationContainer,
) -> None:
    result = container.task_delegator.delegate(request(capability="gmail.send"))
    assert result.status is TaskDelegationStatus.AWAITING_APPROVAL
    assert result.permission_status is PermissionStatus.REQUIRES_APPROVAL
    assert result.queued is False
    assert container.action_queue.list() == []
    assert pending_ids(container) == [result.action_id]


def test_denied_task_is_not_queued_or_held(container: ApplicationContainer) -> None:
    class DenyAll:
        def evaluate(self, action: Action) -> PermissionDecision:
            return PermissionDecision(status=PermissionStatus.DENIED, reason="blocked")

    delegator = ActionTaskDelegator(
        executor_registry=container.worker_executor_registry,
        permission_runtime=PermissionEngineRuntime(
            permission_engine=DenyAll(),
            action_lifecycle_manager=ActionLifecycleManager(),
            lifecycle_repository=container.action_lifecycle_repository,
            pending_approval_registry=container.pending_approval_registry,
        ),
        action_queue=container.action_queue,
    )
    result = delegator.delegate(request())
    assert result.status is TaskDelegationStatus.DENIED
    assert result.permission_status is PermissionStatus.DENIED
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []
    lifecycle = container.action_lifecycle_repository.get(result.action_id)
    assert lifecycle is not None and lifecycle.status == ActionStatus.REJECTED


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"capability": "code.implement"}, "no_handler"),
        ({"executor_role": ExecutorRole.CONTENT_REVIEW}, "no_handler"),
        ({"account_context": None}, "missing_account_context"),
        (
            {"account_context": WorkerAccountContext("someone", "unknown-account")},
            "no_handler",
        ),
    ],
)
def test_missing_route_fails_closed_before_permission(
    container: ApplicationContainer, overrides: dict[str, Any], reason: str,
) -> None:
    result = container.task_delegator.delegate(request(**overrides))
    assert result.status is TaskDelegationStatus.ROUTE_REJECTED
    assert result.permission_status is None
    assert result.routing_reason == reason
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []
    assert container.action_lifecycle_repository.get(result.action_id) is None


def test_ambiguous_provider_route_fails_closed_without_ranking() -> None:
    registry = WorkerExecutorRegistry()
    for provider in ("worker-a", "worker-b"):
        registry.register_capability(
            WorkerCapability("summarize_email", ExecutorRole.CONTENT_SUMMARY, provider),
            NoOpWorkerExecutor(),
        )
    queue = ActionQueue()
    permission_runtime = PermissionEngineRuntime(
        permission_engine=ApplicationContainer().permission_engine,
        action_lifecycle_manager=ActionLifecycleManager(),
    )
    delegator = ActionTaskDelegator(
        executor_registry=registry, permission_runtime=permission_runtime, action_queue=queue,
    )
    result = delegator.delegate(request(account_context=None))
    assert result.status is TaskDelegationStatus.ROUTE_REJECTED
    assert result.routing_reason == "ambiguous_capability_route"
    assert result.permission_status is None
    assert queue.list() == []


def test_default_delegation_makes_no_external_calls(
    monkeypatch: pytest.MonkeyPatch, container: ApplicationContainer,
) -> None:
    def fail_external_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("external call attempted")

    monkeypatch.setattr(socket, "create_connection", fail_external_call)
    monkeypatch.setattr(socket, "socket", fail_external_call)
    assert container.task_delegator.delegate(request()).queued is True


def imported_modules(relative: str) -> set[str]:
    tree = ast.parse((SRC / relative).read_text())
    return {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_delegation_is_separate_from_event_planning_and_semantic_ingress() -> None:
    assert not any(".integrations" in module for module in imported_modules("core/delegation.py"))
    for relative in (
        "core/planner.py", "core/events/workflow.py", "core/semantic.py",
        "core/semantic_query.py", "api/semantic.py",
    ):
        assert "apps.server.src.core.delegation" not in imported_modules(relative)


def test_event_workflow_actions_are_unchanged_by_delegation(
    container: ApplicationContainer,
) -> None:
    event = UniversalEvent(source="gmail", type="message.received", payload={})
    container.event_workflow_service.accept(event)
    container.event_workflow_service.process(event.id)
    actions = container.action_queue.list() + container.pending_approval_registry.list_pending()
    assert [action.type for action in actions] == ["summarize_email"]
    for action in actions:
        assert "task_delegation" not in action.metadata
