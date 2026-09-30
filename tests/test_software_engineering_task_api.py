"""Offline coverage for the Software Engineering task HTTP ingress."""

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.action_lifecycle import ActionLifecycleState, ActionStatus
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.core.approval_decisions import approve_pending_action
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import ApplicationContainer, get_container
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_continuation import (
    SoftwareEngineeringTaskContinuation,
)
from apps.server.src.integrations.software_engineering_disposition import (
    SoftwareEngineeringWorkProductDispositionService,
)
from apps.server.src.integrations.software_engineering_promotion import (
    SoftwareEngineeringPromotionResult,
    SoftwareEngineeringPromotionStateError,
)
from apps.server.src.integrations.software_engineering_run_control import (
    SoftwareEngineeringRunControlService,
)
from apps.server.src.integrations.software_engineering_work_product import (
    WorkProductDisposition,
    WorkProductDispositionResult,
    WorkProductIdentityError,
    WorkProductReview,
)
from apps.server.src.main import app
from apps.server.src.workers.executor import (
    NoOpWorkerExecutor,
    WorkerAccountContext,
    WorkerCapability,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)
from fastapi.testclient import TestClient


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> Iterator[ApplicationContainer]:
    """Use deterministic composition with no real Software Engineering provider."""
    monkeypatch.setenv("VELOX_SOFTWARE_ENGINEERING_PROVIDER", "disabled")
    get_settings.cache_clear()
    instance = ApplicationContainer()
    app.dependency_overrides[get_container] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_container, None)
    get_settings.cache_clear()


@pytest.fixture
def client(container: ApplicationContainer) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "objective": "Add one regression test for the task ingress",
        "target": "velox-server",
    }
    body.update(overrides)
    return body


def register_software_engineering_route(container: ApplicationContainer) -> None:
    container.worker_executor_registry.register_capability(
        WorkerCapability(
            identifier=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            role=ExecutorRole.SOFTWARE_ENGINEERING,
            provider="test_software_engineering",
        ),
        NoOpWorkerExecutor(),
    )


def test_disabled_provider_route_is_rejected_without_side_effects(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    response = client.post("/tasks/software-engineering", json=payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "route_rejected"
    assert body["permission_status"] is None
    assert body["routing_reason"] == "no_handler"
    action_id = UUID(body["action_id"])
    assert container.action_lifecycle_repository.get(action_id) is None
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []
    assert container.worker_execution_observer.list() == []


def test_ingress_maps_to_trusted_software_engineering_route_and_stops_at_approval(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    register_software_engineering_route(container)

    def forbidden_execution(*args: object, **kwargs: object) -> None:
        raise AssertionError("task ingress must not execute a worker")

    monkeypatch.setattr(container.worker_runtime, "process_next", forbidden_execution)

    response = client.post("/tasks/software-engineering", json=payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["permission_status"] == "requires_approval"
    assert body["routing_reason"] == "capability_route"
    assert set(body) == {"action_id", "status", "permission_status", "routing_reason"}

    [pending] = container.pending_approval_registry.list_pending()
    assert pending.id == UUID(body["action_id"])
    assert pending.executor_role is ExecutorRole.SOFTWARE_ENGINEERING
    assert pending.type == SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
    assert pending.target == "velox-server"
    assert pending.payload == {
        "capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        "objective": "Add one regression test for the task ingress",
    }
    assert pending.metadata["task_delegation"] == {
        "requested_role": ExecutorRole.SOFTWARE_ENGINEERING.value,
        "requested_capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    }
    assert "capability_provider" not in pending.payload
    assert "account_context" not in pending.payload
    assert container.action_queue.list() == []
    assert container.worker_execution_observer.list() == []
    assert "test_software_engineering" not in json.dumps(body)



def test_pending_approval_is_listed_and_approvable_after_local_state_loss(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    register_software_engineering_route(container)
    monkeypatch.setattr(
        "apps.server.src.api.events.get_container",
        lambda: container,
    )
    action_id = create_task(client)

    container.pending_approval_registry.clear()
    container.action_lifecycle_repository.clear()

    pending = client.get("/actions/pending-approval")
    assert pending.status_code == 200
    assert [item["action"]["id"] for item in pending.json()] == [str(action_id)]
    assert pending.json()[0]["lifecycle"]["status"] == "queued"

    approved = client.post(f"/actions/{action_id}/approve")

    assert approved.status_code == 200
    assert approved.json()["lifecycle"]["status"] == "approved"
    [queued] = container.action_queue.list()
    assert queued.id == action_id
    durable = container.software_engineering_run_repository.get(action_id)
    assert durable is not None
    assert durable.approval_status == "approved"


@pytest.mark.parametrize("field", ["objective", "target"])
@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_task_fields_fail_before_delegation_side_effects(
    client: TestClient,
    container: ApplicationContainer,
    field: str,
    value: str,
) -> None:
    response = client.post("/tasks/software-engineering", json=payload(**{field: value}))

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid software engineering task"}
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "claude_code"),
        ("executor_role", "software_engineering"),
        ("capability", "code.implement"),
        ("account_context", {"account_identifier": "secret-account"}),
        ("approval", "approved"),
        ("workspace", "/tmp/repo"),
        ("branch", "main"),
        ("retry_policy", "always"),
    ],
)
def test_request_cannot_supply_execution_authority(
    client: TestClient,
    container: ApplicationContainer,
    field: str,
    value: object,
) -> None:
    response = client.post("/tasks/software-engineering", json=payload(**{field: value}))

    assert response.status_code == 422
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []


@pytest.mark.parametrize("body", [{"target": "velox-server"}, {"objective": "Do the work"}])
def test_required_fields_are_enforced_by_http_schema(
    client: TestClient,
    container: ApplicationContainer,
    body: dict[str, str],
) -> None:
    response = client.post("/tasks/software-engineering", json=body)

    assert response.status_code == 422
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []


def test_bearer_auth_protects_task_ingress(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    monkeypatch.setenv("VELOX_API_TOKEN", "test-secret")
    get_settings.cache_clear()

    assert client.post("/tasks/software-engineering", json=payload()).status_code == 401
    response = client.post(
        "/tasks/software-engineering",
        json=payload(),
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "route_rejected"
    assert container.action_queue.list() == []
    assert container.pending_approval_registry.list_pending() == []


def test_openapi_request_schema_exposes_only_caller_owned_fields(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/tasks/software-engineering"]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema_name = request_ref.rsplit("/", 1)[-1]
    request_schema = schema["components"]["schemas"][schema_name]

    assert set(request_schema["properties"]) == {"objective", "target"}
    assert set(request_schema["required"]) == {"objective", "target"}
    assert request_schema["additionalProperties"] is False


class RecordingSoftwareEngineeringExecutor:
    def __init__(self, *, failed: bool = False) -> None:
        self.failed = failed
        self.called_actions: list[Action] = []

    def execute(
        self,
        action: Action,
        *,
        capability: str | None = None,
        account_context: WorkerAccountContext | None = None,
    ) -> WorkerExecutionResult:
        self.called_actions.append(action)
        if self.failed:
            return WorkerExecutionResult(
                action=action,
                status=WorkerExecutionStatus.FAILED,
                reason="provider secret detail sk-test-secret",
                metadata={
                    "external_execution_performed": True,
                    "provider": "secret-provider",
                },
            )
        return WorkerExecutionResult(
            action=action,
            status=WorkerExecutionStatus.SUCCEEDED,
            metadata={
                "external_execution_performed": True,
                "provider": "secret-provider",
            },
        )


class RecordingWorkProductReviewer:
    def __init__(
        self,
        *,
        unavailable: bool = False,
        disposition_unavailable: bool = False,
    ) -> None:
        self.unavailable = unavailable
        self.disposition_unavailable = disposition_unavailable
        self.calls: list[UUID] = []
        self.apply_calls: list[tuple[UUID, WorkProductDisposition]] = []

    def review(self, action_id: UUID) -> WorkProductReview:
        self.calls.append(action_id)
        if self.unavailable:
            raise WorkProductIdentityError("worktree missing")
        return WorkProductReview(
            action_id=action_id,
            worktree_path=Path("/trusted/hidden/worktree"),
            branch=f"velox/se-{action_id}",
            changed_files=("apps/server/src/api/tasks.py",),
            untracked_files=("tests/test_new.py",),
            diff_stat=" 2 files changed",
            diff="diff --git a/file b/file",
            diff_truncated=False,
            dirty=True,
            canonical_clean=True,
            canonical_unchanged=True,
        )


    def apply(
        self,
        action_id: UUID,
        disposition: WorkProductDisposition,
    ) -> WorkProductDispositionResult:
        self.apply_calls.append((action_id, disposition))
        if self.disposition_unavailable:
            raise WorkProductIdentityError(
                "hidden /trusted/secret/worktree velox/secret-branch"
            )
        keep = disposition is WorkProductDisposition.KEEP
        return WorkProductDispositionResult(
            action_id=action_id,
            disposition=disposition,
            worktree_path=Path("/trusted/hidden/worktree"),
            branch=f"velox/se-{action_id}",
            worktree_present=keep,
            branch_present=keep,
            canonical_unchanged=True,
        )


def configure_exact_execution(
    container: ApplicationContainer,
    executor: RecordingSoftwareEngineeringExecutor,
    reviewer: RecordingWorkProductReviewer,
) -> None:
    container.worker_executor_registry.register_capability(
        WorkerCapability(
            identifier=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            role=ExecutorRole.SOFTWARE_ENGINEERING,
            provider="execution-test-provider",
        ),
        executor,
    )
    container.software_engineering_task_continuation = SoftwareEngineeringTaskContinuation(
        action_queue=container.action_queue,
        lifecycle_repository=container.action_lifecycle_repository,
        worker_runtime=container.worker_runtime,
        action_recovery=container.software_engineering_action_recovery,
        work_product_reviewer=reviewer,
    )
    container.software_engineering_work_product_disposition = (
        SoftwareEngineeringWorkProductDispositionService(
            run_repository=container.software_engineering_run_repository,
            work_products=reviewer,
        )
    )


def approve(container: ApplicationContainer, action_id: UUID) -> None:
    approve_pending_action(
        action_id,
        pending_approval_registry=container.pending_approval_registry,
        lifecycle_repository=container.action_lifecycle_repository,
        lifecycle_manager=container.action_lifecycle_manager,
        action_queue=container.action_queue,
        pending_action_recovery=container.software_engineering_action_recovery,
        approval_recorder=container.software_engineering_action_recovery,
    )


def create_task(client: TestClient) -> UUID:
    response = client.post("/tasks/software-engineering", json=payload())
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_approval"
    return UUID(response.json()["action_id"])


def test_exact_execute_processes_only_requested_action_and_returns_review(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)

    before = Action(type="unrelated.before", target="one")
    after = Action(type="unrelated.after", target="three")
    container.action_queue.enqueue(before)
    action_id = create_task(client)
    approve(container, action_id)
    container.action_queue.enqueue(after)

    response = client.post(f"/tasks/software-engineering/{action_id}/execute")

    assert response.status_code == 200
    body = response.json()
    assert body["action_id"] == str(action_id)
    assert body["processed"] is True
    assert body["execution_status"] == "succeeded"
    assert body["lifecycle_status"] == "completed"
    assert body["execution_reason"] is None
    assert body["external_execution_performed"] is True
    assert body["review_status"] == "available"
    assert body["review"] == {
        "changed_files": ["apps/server/src/api/tasks.py"],
        "untracked_files": ["tests/test_new.py"],
        "diff_stat": " 2 files changed",
        "diff": "diff --git a/file b/file",
        "diff_truncated": False,
        "dirty": True,
        "canonical_clean": True,
        "canonical_unchanged": True,
    }
    assert [action.id for action in container.action_queue.list()] == [before.id, after.id]
    assert [action.id for action in executor.called_actions] == [action_id]
    assert reviewer.calls == [action_id]
    rendered = json.dumps(body)
    assert "/trusted/hidden/worktree" not in rendered
    assert "execution-test-provider" not in rendered
    assert "secret-provider" not in rendered



def test_exact_execute_recovers_approved_action_after_local_queue_loss(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    action_id = create_task(client)
    approve(container, action_id)

    container.action_queue.clear()
    container.action_lifecycle_repository.clear()
    container.pending_approval_registry.clear()

    response = client.post(f"/tasks/software-engineering/{action_id}/execute")

    assert response.status_code == 200
    assert response.json()["execution_status"] == "succeeded"
    assert [action.id for action in executor.called_actions] == [action_id]
    durable = container.software_engineering_run_repository.get(action_id)
    assert durable is not None
    assert durable.claim_id is not None
    assert durable.execution_status == "succeeded"
    assert durable.pending_objective is None


def test_exact_execute_refuses_ambiguous_preexisting_durable_claim(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    action_id = create_task(client)
    approve(container, action_id)
    container.software_engineering_run_repository.claim_approved(action_id)

    container.action_queue.clear()
    container.action_lifecycle_repository.clear()

    response = client.post(f"/tasks/software-engineering/{action_id}/execute")

    assert response.status_code == 409
    assert executor.called_actions == []


def test_exact_execute_never_auto_approves(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    action_id = create_task(client)

    response = client.post(f"/tasks/software-engineering/{action_id}/execute")

    assert response.status_code == 409
    assert response.json() == {"detail": "software engineering action is not executable"}
    assert executor.called_actions == []
    assert reviewer.calls == []
    assert [action.id for action in container.pending_approval_registry.list_pending()] == [
        action_id
    ]


def test_exact_execute_rejects_non_software_engineering_action(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    wrong = Action(
        type="summarize_email",
        target="message-1",
        payload={"capability": "summarize_email"},
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )
    container.action_lifecycle_repository.set(
        wrong.id,
        ActionLifecycleState(status=ActionStatus.APPROVED),
    )
    container.action_queue.enqueue(wrong)

    response = client.post(f"/tasks/software-engineering/{wrong.id}/execute")

    assert response.status_code == 409
    assert executor.called_actions == []
    assert container.action_queue.list() == [wrong]


def test_exact_execute_unknown_action_is_404(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)

    response = client.post(f"/tasks/software-engineering/{uuid4()}/execute")

    assert response.status_code == 404
    assert executor.called_actions == []


def test_exact_execute_preserves_execution_truth_when_review_is_unavailable(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer(unavailable=True)
    configure_exact_execution(container, executor, reviewer)
    action_id = create_task(client)
    approve(container, action_id)

    response = client.post(f"/tasks/software-engineering/{action_id}/execute")

    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "succeeded"
    assert body["lifecycle_status"] == "completed"
    assert body["review_status"] == "unavailable"
    assert body["review"] is None
    assert reviewer.calls == [action_id]


def test_exact_execute_redacts_worker_failure_reason(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor(failed=True)
    reviewer = RecordingWorkProductReviewer(unavailable=True)
    configure_exact_execution(container, executor, reviewer)
    action_id = create_task(client)
    approve(container, action_id)

    response = client.post(f"/tasks/software-engineering/{action_id}/execute")

    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "failed"
    assert body["lifecycle_status"] == "failed"
    assert body["execution_reason"] == "worker execution failed"
    assert "sk-test-secret" not in response.text
    assert "secret-provider" not in response.text



def execute_task(client: TestClient, container: ApplicationContainer) -> UUID:
    action_id = create_task(client)
    approve(container, action_id)
    response = client.post(f"/tasks/software-engineering/{action_id}/execute")
    assert response.status_code == 200
    return action_id


def test_work_product_keep_requires_executed_exact_software_engineering_action(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    action_id = execute_task(client, container)

    response = client.post(
        f"/tasks/software-engineering/{action_id}/work-product/disposition",
        json={"disposition": "keep"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "action_id": str(action_id),
        "disposition": "keep",
        "succeeded": True,
        "worktree_present": True,
        "branch_present": True,
        "canonical_unchanged": True,
        "remaining": [],
    }
    assert reviewer.apply_calls == [(action_id, WorkProductDisposition.KEEP)]
    assert "/trusted/hidden/worktree" not in response.text
    assert f"velox/se-{action_id}" not in response.text
    assert "execution-test-provider" not in response.text


def test_work_product_discard_returns_only_safe_cleanup_state(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    action_id = execute_task(client, container)

    response = client.post(
        f"/tasks/software-engineering/{action_id}/work-product/disposition",
        json={"disposition": "discard"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "action_id": str(action_id),
        "disposition": "discard",
        "succeeded": True,
        "worktree_present": False,
        "branch_present": False,
        "canonical_unchanged": True,
        "remaining": [],
    }
    assert reviewer.apply_calls == [(action_id, WorkProductDisposition.DISCARD)]


def test_work_product_disposition_never_runs_before_execution_finishes(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    action_id = create_task(client)
    approve(container, action_id)

    response = client.post(
        f"/tasks/software-engineering/{action_id}/work-product/disposition",
        json={"disposition": "discard"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "software engineering work product is not disposable"
    }
    assert reviewer.apply_calls == []


def test_work_product_disposition_hides_non_software_engineering_action(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    wrong = Action(
        type="summarize_email",
        target="message-1",
        payload={"capability": "summarize_email"},
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )
    container.action_lifecycle_repository.set(
        wrong.id,
        ActionLifecycleState(status=ActionStatus.COMPLETED),
    )
    observation = container.worker_execution_observer.start(
        action=wrong,
        requested_role=ExecutorRole.CONTENT_SUMMARY.value,
        executor_registered=True,
        requested_capability="summarize_email",
    )
    container.worker_execution_observer.finish(
        observation,
        status=WorkerExecutionStatus.SUCCEEDED,
        metadata={},
    )

    response = client.post(
        f"/tasks/software-engineering/{wrong.id}/work-product/disposition",
        json={"disposition": "discard"},
    )

    assert response.status_code == 404
    assert reviewer.apply_calls == []


def test_work_product_disposition_unknown_action_is_404(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)

    response = client.post(
        f"/tasks/software-engineering/{uuid4()}/work-product/disposition",
        json={"disposition": "keep"},
    )

    assert response.status_code == 404
    assert reviewer.apply_calls == []


def test_work_product_identity_failure_is_redacted_as_generic_conflict(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer(disposition_unavailable=True)
    configure_exact_execution(container, executor, reviewer)
    action_id = execute_task(client, container)

    response = client.post(
        f"/tasks/software-engineering/{action_id}/work-product/disposition",
        json={"disposition": "discard"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "software engineering work product is not disposable"
    }
    assert "/trusted/secret/worktree" not in response.text
    assert "velox/secret-branch" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"disposition": "delete"},
        {"disposition": "discard", "path": "/tmp/other"},
        {"disposition": "keep", "branch": "main"},
        {"disposition": "keep", "provider": "claude_code"},
    ],
)
def test_work_product_disposition_schema_accepts_only_keep_or_discard(
    client: TestClient,
    container: ApplicationContainer,
    body: dict[str, str],
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)

    response = client.post(
        f"/tasks/software-engineering/{uuid4()}/work-product/disposition",
        json=body,
    )

    assert response.status_code == 422
    assert reviewer.apply_calls == []


def test_openapi_work_product_disposition_exposes_only_disposition_choice(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][
        "/tasks/software-engineering/{action_id}/work-product/disposition"
    ]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema_name = request_ref.rsplit("/", 1)[-1]
    request_schema = schema["components"]["schemas"][schema_name]

    assert set(request_schema["properties"]) == {"disposition"}
    assert set(request_schema["required"]) == {"disposition"}
    assert request_schema["additionalProperties"] is False



class FakePromotionService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[UUID, str, str]] = []

    def promote(
        self,
        action_id: UUID,
        *,
        title: str,
        body: str,
    ) -> SoftwareEngineeringPromotionResult:
        self.calls.append((action_id, title, body))
        if self.fail:
            raise SoftwareEngineeringPromotionStateError(
                "hidden /trusted/worktree provider-secret"
            )
        return SoftwareEngineeringPromotionResult(
            action_id=action_id,
            commit_sha="a" * 40,
            pull_request_number=42,
            pull_request_url="https://github.example/owner/repo/pull/42",
            base_branch="main",
            head_branch=f"velox/se-{action_id}",
            pull_request_created=True,
        )


def test_promotion_endpoint_returns_only_safe_provider_neutral_result(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    fake = FakePromotionService()
    monkeypatch.setattr(container, "software_engineering_promotion", fake)
    action_id = uuid4()

    response = client.post(
        f"/tasks/software-engineering/{action_id}/promote",
        json={"title": "Slice 10", "body": "Guarded promotion"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "action_id": str(action_id),
        "commit_sha": "a" * 40,
        "pull_request_number": 42,
        "pull_request_url": "https://github.example/owner/repo/pull/42",
        "base_branch": "main",
        "head_branch": f"velox/se-{action_id}",
        "pull_request_created": True,
    }
    assert fake.calls == [(action_id, "Slice 10", "Guarded promotion")]
    rendered = response.text
    assert "/trusted/" not in rendered
    assert "provider-secret" not in rendered


def test_promotion_endpoint_redacts_state_failure(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    fake = FakePromotionService(fail=True)
    monkeypatch.setattr(container, "software_engineering_promotion", fake)

    response = client.post(
        f"/tasks/software-engineering/{uuid4()}/promote",
        json={"title": "Slice 10"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "software engineering work product is not promotable"
    }
    assert "/trusted/worktree" not in response.text
    assert "provider-secret" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": "Slice 10", "remote": "evil"},
        {"title": "Slice 10", "base_branch": "release"},
        {"title": "Slice 10", "head_branch": "attacker"},
        {"title": "Slice 10", "path": "/tmp/other"},
        {"title": "Slice 10", "provider": "claude_code"},
        {"title": "Slice 10", "force": True},
    ],
)
def test_promotion_schema_exposes_no_git_or_provider_authority(
    client: TestClient,
    body: dict[str, object],
) -> None:
    response = client.post(
        f"/tasks/software-engineering/{uuid4()}/promote",
        json=body,
    )
    assert response.status_code == 422


def test_openapi_promotion_request_exposes_only_pr_copy(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/tasks/software-engineering/{action_id}/promote"]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema_name = request_ref.rsplit("/", 1)[-1]
    request_schema = schema["components"]["schemas"][schema_name]

    assert set(request_schema["properties"]) == {"title", "body"}
    assert set(request_schema["required"]) == {"title"}
    assert request_schema["additionalProperties"] is False



def configure_run_control(
    container: ApplicationContainer,
    reviewer: RecordingWorkProductReviewer | None = None,
) -> None:
    container.software_engineering_run_control = SoftwareEngineeringRunControlService(
        repository=container.software_engineering_run_repository,
        work_product_inspector=reviewer,
    )


def test_run_status_hides_objective_claim_identity_and_provider_details(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    register_software_engineering_route(container)
    action_id = create_task(client)
    approve(container, action_id)
    container.software_engineering_run_repository.claim_approved(action_id)

    response = client.get(f"/tasks/software-engineering/{action_id}/status")

    assert response.status_code == 200
    body = response.json()
    assert body["phase"] == "claimed"
    assert body["claimed"] is True
    assert body["worker_started"] is False
    assert body["reconciliation_options"] == ["release"]
    assert body["retriable"] is False
    assert len(body["state_token"]) == 64
    rendered = json.dumps(body)
    assert "Add one regression test for the task ingress" not in rendered
    assert "claim_id" not in rendered
    assert "test_software_engineering" not in rendered
    assert "/trusted/" not in rendered


def test_run_status_unknown_action_is_404(
    client: TestClient,
) -> None:
    response = client.get(f"/tasks/software-engineering/{uuid4()}/status")
    assert response.status_code == 404


def test_claim_release_never_executes_worker_and_restores_retriable_state(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    configure_run_control(container, reviewer)
    action_id = create_task(client)
    approve(container, action_id)
    container.software_engineering_run_repository.claim_approved(action_id)
    before = client.get(f"/tasks/software-engineering/{action_id}/status").json()

    response = client.post(
        f"/tasks/software-engineering/{action_id}/claim/reconcile",
        json={
            "resolution": "release",
            "state_token": before["state_token"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["resolution"] == "release"
    assert body["run"]["phase"] == "approved"
    assert body["run"]["claimed"] is False
    assert body["run"]["retriable"] is True
    assert executor.called_actions == []


def test_claim_reconciliation_rejects_stale_state_token(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    register_software_engineering_route(container)
    action_id = create_task(client)
    approve(container, action_id)
    container.software_engineering_run_repository.claim_approved(action_id)
    before = client.get(f"/tasks/software-engineering/{action_id}/status").json()
    container.software_engineering_run_repository.record_execution_started(
        action_id=action_id,
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )

    response = client.post(
        f"/tasks/software-engineering/{action_id}/claim/reconcile",
        json={
            "resolution": "release",
            "state_token": before["state_token"],
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "software engineering claim is not reconcilable"
    }


def test_started_ambiguous_status_exposes_only_bounded_work_product_summary(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    configure_run_control(container, reviewer)
    action_id = create_task(client)
    approve(container, action_id)
    container.software_engineering_run_repository.claim_approved(action_id)
    container.software_engineering_run_repository.record_execution_started(
        action_id=action_id,
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )

    response = client.get(f"/tasks/software-engineering/{action_id}/status")

    assert response.status_code == 200
    body = response.json()
    assert body["phase"] == "running_or_ambiguous"
    assert body["reconciliation_options"] == ["abandon"]
    assert body["work_product"] == {
        "status": "available",
        "dirty": True,
        "changed_files_count": 1,
        "untracked_files_count": 1,
        "canonical_clean": True,
        "canonical_unchanged": True,
    }
    rendered = json.dumps(body)
    assert "/trusted/hidden/worktree" not in rendered
    assert f"velox/se-{action_id}" not in rendered
    assert "diff --git" not in rendered


def test_started_claim_abandon_requires_ack_and_never_makes_action_retriable(
    client: TestClient,
    container: ApplicationContainer,
) -> None:
    executor = RecordingSoftwareEngineeringExecutor()
    reviewer = RecordingWorkProductReviewer()
    configure_exact_execution(container, executor, reviewer)
    configure_run_control(container, reviewer)
    action_id = create_task(client)
    approve(container, action_id)
    container.software_engineering_run_repository.claim_approved(action_id)
    container.software_engineering_run_repository.record_execution_started(
        action_id=action_id,
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    before = client.get(f"/tasks/software-engineering/{action_id}/status").json()

    denied = client.post(
        f"/tasks/software-engineering/{action_id}/claim/reconcile",
        json={
            "resolution": "abandon",
            "state_token": before["state_token"],
        },
    )
    assert denied.status_code == 409

    response = client.post(
        f"/tasks/software-engineering/{action_id}/claim/reconcile",
        json={
            "resolution": "abandon",
            "state_token": before["state_token"],
            "acknowledge_possible_external_side_effects": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["resolution"] == "abandon"
    assert body["run"]["phase"] == "abandoned"
    assert body["run"]["claimed"] is True
    assert body["run"]["retriable"] is False
    assert body["run"]["reconciliation_options"] == []
    assert executor.called_actions == []

    execution = client.post(f"/tasks/software-engineering/{action_id}/execute")
    assert execution.status_code == 409
    assert executor.called_actions == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"resolution": "release"},
        {"resolution": "retry", "state_token": "a" * 64},
        {"resolution": "release", "state_token": "short"},
        {
            "resolution": "release",
            "state_token": "a" * 64,
            "provider": "claude_code",
        },
        {
            "resolution": "release",
            "state_token": "a" * 64,
            "path": "/tmp/worktree",
        },
    ],
)
def test_claim_reconciliation_schema_exposes_no_execution_authority(
    client: TestClient,
    body: dict[str, object],
) -> None:
    response = client.post(
        f"/tasks/software-engineering/{uuid4()}/claim/reconcile",
        json=body,
    )
    assert response.status_code == 422


def test_openapi_claim_reconciliation_exposes_only_safe_operator_fields(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][
        "/tasks/software-engineering/{action_id}/claim/reconcile"
    ]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema_name = request_ref.rsplit("/", 1)[-1]
    request_schema = schema["components"]["schemas"][schema_name]

    assert set(request_schema["properties"]) == {
        "resolution",
        "state_token",
        "acknowledge_possible_external_side_effects",
    }
    assert set(request_schema["required"]) == {"resolution", "state_token"}
    assert request_schema["additionalProperties"] is False
