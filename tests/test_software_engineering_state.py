"""Durable Software Engineering state coverage."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_state import (
    DurableSoftwareEngineeringExecutionObserver,
    InMemorySoftwareEngineeringRunRepository,
    SoftwareEngineeringRunStateError,
    SqliteSoftwareEngineeringRunRepository,
    default_software_engineering_state_path,
)
from apps.server.src.integrations.software_engineering_work_product import (
    WorkProductDisposition,
    WorkProductDispositionResult,
)
from apps.server.src.workers.executor import WorkerExecutionStatus
from apps.server.src.workers.runtime import InMemoryWorkerExecutionObserver


def test_sqlite_run_state_survives_repository_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state" / "runs.sqlite3"
    action_id = uuid4()
    repository = SqliteSoftwareEngineeringRunRepository(path)
    repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        delegation_status="awaiting_approval",
    )
    finished_at = datetime.now(UTC)
    repository.record_execution(
        action_id=action_id,
        status="succeeded",
        finished_at=finished_at,
        external_execution_performed=True,
    )
    repository.record_disposition(
        WorkProductDispositionResult(
            action_id=action_id,
            disposition=WorkProductDisposition.KEEP,
            worktree_path=tmp_path / "not-persisted-worktree",
            branch=f"velox/se-{action_id}",
            worktree_present=True,
            branch_present=True,
            canonical_unchanged=True,
        )
    )
    repository.record_promotion(
        action_id=action_id,
        commit_sha="a" * 40,
        pull_request_number=42,
        pull_request_url="https://github.example/owner/repo/pull/42",
        base_branch="main",
        head_branch=f"velox/se-{action_id}",
        finished_at=finished_at,
    )

    reopened = SqliteSoftwareEngineeringRunRepository(path)
    state = reopened.get(action_id)

    assert state is not None
    assert state.action_id == action_id
    assert state.executor_role == "software_engineering"
    assert state.capability == "code.implement"
    assert state.target == "velox-server"
    assert state.delegation_status == "awaiting_approval"
    assert state.execution_status == "succeeded"
    assert state.execution_finished_at == finished_at
    assert state.external_execution_performed is True
    assert state.disposition is WorkProductDisposition.KEEP
    assert state.disposition_succeeded is True
    assert state.worktree_present is True
    assert state.branch_present is True
    assert state.canonical_unchanged is True
    assert state.promotion_commit_sha == "a" * 40
    assert state.pull_request_number == 42
    assert state.pull_request_url == "https://github.example/owner/repo/pull/42"
    assert state.promotion_base_branch == "main"
    assert state.promotion_head_branch == f"velox/se-{action_id}"
    assert state.promotion_finished_at == finished_at


def test_sqlite_run_state_rejects_action_identity_conflict(tmp_path: Path) -> None:
    repository = SqliteSoftwareEngineeringRunRepository(tmp_path / "state.sqlite3")
    action_id = uuid4()
    repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    )

    with pytest.raises(SoftwareEngineeringRunStateError, match="identity conflicts"):
        repository.register_action(
            action_id=action_id,
            target="different-target",
            executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
            capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        )


def test_durable_observer_records_only_canonical_software_engineering_route() -> None:
    repository = InMemorySoftwareEngineeringRunRepository()
    observer = DurableSoftwareEngineeringExecutionObserver(
        InMemoryWorkerExecutionObserver(),
        repository,
    )
    action = Action(
        type=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
        payload={"capability": SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY},
    )

    observation = observer.start(
        action=action,
        requested_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        executor_registered=True,
        requested_capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
    )
    observer.finish(
        observation,
        WorkerExecutionStatus.SUCCEEDED,
        metadata={"external_execution_performed": True, "provider_secret": "not persisted"},
    )

    state = repository.get(action.id)
    assert state is not None
    assert state.execution_status == "succeeded"
    assert state.external_execution_performed is True

    unrelated = Action(
        type="summarize_email",
        target="mail",
        executor_role=ExecutorRole.CONTENT_SUMMARY,
    )
    other = observer.start(
        action=unrelated,
        requested_role=ExecutorRole.CONTENT_SUMMARY.value,
        executor_registered=True,
        requested_capability="summarize_email",
    )
    observer.finish(other, WorkerExecutionStatus.SUCCEEDED, metadata={})

    assert repository.get(unrelated.id) is None


def test_default_state_path_stays_outside_canonical_repository(tmp_path: Path) -> None:
    workspace = tmp_path / "velox-server"
    workspace.mkdir()

    path = default_software_engineering_state_path(workspace)

    assert path == tmp_path / ".velox" / "velox-server-software-engineering.sqlite3"
    assert workspace not in path.parents


def test_sqlite_repository_requires_absolute_path() -> None:
    with pytest.raises(SoftwareEngineeringRunStateError, match="must be absolute"):
        SqliteSoftwareEngineeringRunRepository(Path("relative/state.sqlite3"))
