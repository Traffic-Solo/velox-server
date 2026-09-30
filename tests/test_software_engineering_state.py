"""Durable Software Engineering state coverage."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_disposition import (
    SoftwareEngineeringWorkProductDispositionService,
)
from apps.server.src.integrations.software_engineering_state import (
    DurableSoftwareEngineeringExecutionObserver,
    InMemorySoftwareEngineeringRunRepository,
    SoftwareEngineeringApprovalStatus,
    SoftwareEngineeringClaimResolution,
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


def test_sqlite_v1_state_migrates_to_v3_without_losing_existing_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    action_id = uuid4()
    now = datetime.now(UTC).isoformat()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE velox_schema (
            name TEXT PRIMARY KEY,
            version INTEGER NOT NULL
        );
        CREATE TABLE software_engineering_runs (
            action_id TEXT PRIMARY KEY,
            executor_role TEXT NOT NULL,
            capability TEXT NOT NULL,
            target TEXT NOT NULL,
            delegation_status TEXT,
            execution_status TEXT,
            execution_finished_at TEXT,
            external_execution_performed INTEGER,
            disposition TEXT,
            disposition_succeeded INTEGER,
            worktree_present INTEGER,
            branch_present INTEGER,
            canonical_unchanged INTEGER,
            promotion_commit_sha TEXT,
            pull_request_number INTEGER,
            pull_request_url TEXT,
            promotion_base_branch TEXT,
            promotion_head_branch TEXT,
            promotion_finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO velox_schema(name, version) VALUES (?, 1)",
        ("software_engineering_runs",),
    )
    connection.execute(
        """
        INSERT INTO software_engineering_runs (
            action_id, executor_role, capability, target,
            delegation_status, execution_status, execution_finished_at,
            external_execution_performed, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(action_id),
            ExecutorRole.SOFTWARE_ENGINEERING.value,
            SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            "velox-server",
            "awaiting_approval",
            "succeeded",
            now,
            1,
            now,
            now,
        ),
    )
    connection.commit()
    connection.close()

    repository = SqliteSoftwareEngineeringRunRepository(path)
    state = repository.get(action_id)

    assert state is not None
    assert state.execution_status == "succeeded"
    assert state.pending_objective is None
    assert state.approval_status is None
    with sqlite3.connect(path) as migrated:
        version = migrated.execute(
            "SELECT version FROM velox_schema WHERE name = ?",
            ("software_engineering_runs",),
        ).fetchone()
    assert version == (3,)


def test_sqlite_v2_state_migrates_to_v3_with_claim_evidence_intact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state-v2.sqlite3"
    action_id = uuid4()
    now = datetime.now(UTC).isoformat()
    claim_id = uuid4()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE velox_schema (
            name TEXT PRIMARY KEY,
            version INTEGER NOT NULL
        );
        CREATE TABLE software_engineering_runs (
            action_id TEXT PRIMARY KEY,
            executor_role TEXT NOT NULL,
            capability TEXT NOT NULL,
            target TEXT NOT NULL,
            delegation_status TEXT,
            pending_objective TEXT,
            approval_status TEXT,
            approved_at TEXT,
            rejected_at TEXT,
            claim_id TEXT,
            claimed_at TEXT,
            execution_started_at TEXT,
            execution_status TEXT,
            execution_finished_at TEXT,
            external_execution_performed INTEGER,
            disposition TEXT,
            disposition_succeeded INTEGER,
            worktree_present INTEGER,
            branch_present INTEGER,
            canonical_unchanged INTEGER,
            promotion_commit_sha TEXT,
            pull_request_number INTEGER,
            pull_request_url TEXT,
            promotion_base_branch TEXT,
            promotion_head_branch TEXT,
            promotion_finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO velox_schema(name, version) VALUES (?, 2)",
        ("software_engineering_runs",),
    )
    connection.execute(
        """
        INSERT INTO software_engineering_runs (
            action_id, executor_role, capability, target,
            delegation_status, pending_objective, approval_status,
            approved_at, claim_id, claimed_at, execution_started_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(action_id),
            ExecutorRole.SOFTWARE_ENGINEERING.value,
            SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            "velox-server",
            "awaiting_approval",
            "Recover claim",
            SoftwareEngineeringApprovalStatus.APPROVED.value,
            now,
            str(claim_id),
            now,
            now,
            now,
            now,
        ),
    )
    connection.commit()
    connection.close()

    repository = SqliteSoftwareEngineeringRunRepository(path)
    state = repository.get(action_id)

    assert state is not None
    assert state.claim_id == claim_id
    assert state.execution_started_at == datetime.fromisoformat(now)
    assert state.claim_resolution is None
    assert state.claim_reconciled_at is None
    with sqlite3.connect(path) as migrated:
        version = migrated.execute(
            "SELECT version FROM velox_schema WHERE name = ?",
            ("software_engineering_runs",),
        ).fetchone()
    assert version == (3,)


def test_pending_objective_and_approval_survive_reopen_then_scrub_on_completion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    action_id = uuid4()
    repository = SqliteSoftwareEngineeringRunRepository(path)
    repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        delegation_status="awaiting_approval",
        objective="Implement restart recovery",
        approval_status=SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL,
    )
    repository.record_approval(
        action_id=action_id,
        objective="Implement restart recovery",
        approved_at=datetime.now(UTC),
    )

    restarted = SqliteSoftwareEngineeringRunRepository(path)
    state = restarted.get(action_id)
    assert state is not None
    assert state.pending_objective == "Implement restart recovery"
    assert state.approval_status is SoftwareEngineeringApprovalStatus.APPROVED

    claim = restarted.claim_approved(action_id)
    restarted.record_execution_started(
        action_id=action_id,
        started_at=datetime.now(UTC),
    )
    restarted.record_execution(
        action_id=action_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        external_execution_performed=True,
    )

    terminal = SqliteSoftwareEngineeringRunRepository(path).get(action_id)
    assert terminal is not None
    assert terminal.pending_objective is None
    assert terminal.claim_id == claim.claim_id
    assert terminal.execution_started_at is not None
    assert terminal.execution_status == "succeeded"


def test_sqlite_claim_is_atomic_across_repository_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    action_id = uuid4()
    first = SqliteSoftwareEngineeringRunRepository(path)
    first.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        objective="Implement one thing",
        approval_status=SoftwareEngineeringApprovalStatus.APPROVED,
    )
    second = SqliteSoftwareEngineeringRunRepository(path)

    claim = first.claim_approved(action_id)

    assert claim.action_id == action_id
    with pytest.raises(
        SoftwareEngineeringRunStateError,
        match="durable execution claim",
    ):
        second.claim_approved(action_id)


def test_unstarted_claim_release_is_cas_and_retriable(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    action_id = uuid4()
    repository = SqliteSoftwareEngineeringRunRepository(path)
    repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        objective="Recover release",
        approval_status=SoftwareEngineeringApprovalStatus.APPROVED,
    )
    repository.claim_approved(action_id)
    claimed = repository.get(action_id)
    assert claimed is not None
    assert claimed.updated_at is not None

    reconciled_at = datetime.now(UTC)
    released = repository.release_unstarted_claim(
        action_id=action_id,
        expected_updated_at=claimed.updated_at,
        reconciled_at=reconciled_at,
    )

    assert released.claim_id is None
    assert released.claimed_at is None
    assert (
        released.claim_resolution
        is SoftwareEngineeringClaimResolution.RELEASED_BEFORE_START
    )
    assert released.claim_reconciled_at == reconciled_at
    assert released.pending_objective == "Recover release"
    retry_claim = repository.claim_approved(action_id)
    assert retry_claim.action_id == action_id
    retried = repository.get(action_id)
    assert retried is not None
    assert retried.claim_resolution is None
    assert retried.claim_reconciled_at is None


def test_unstarted_claim_release_rejects_stale_state_version(tmp_path: Path) -> None:
    repository = SqliteSoftwareEngineeringRunRepository(tmp_path / "state.sqlite3")
    action_id = uuid4()
    repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        objective="Stale release",
        approval_status=SoftwareEngineeringApprovalStatus.APPROVED,
    )
    repository.claim_approved(action_id)
    claimed = repository.get(action_id)
    assert claimed is not None
    assert claimed.updated_at is not None
    stale = claimed.updated_at
    repository.record_execution_started(
        action_id=action_id,
        started_at=datetime.now(UTC),
    )

    with pytest.raises(SoftwareEngineeringRunStateError):
        repository.release_unstarted_claim(
            action_id=action_id,
            expected_updated_at=stale,
            reconciled_at=datetime.now(UTC),
        )


def test_started_claim_abandon_scrubs_objective_and_remains_non_retriable(
    tmp_path: Path,
) -> None:
    repository = SqliteSoftwareEngineeringRunRepository(tmp_path / "state.sqlite3")
    action_id = uuid4()
    repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        objective="Potential side effect",
        approval_status=SoftwareEngineeringApprovalStatus.APPROVED,
    )
    repository.claim_approved(action_id)
    repository.record_execution_started(
        action_id=action_id,
        started_at=datetime.now(UTC),
    )
    started = repository.get(action_id)
    assert started is not None
    assert started.updated_at is not None

    abandoned = repository.abandon_started_claim(
        action_id=action_id,
        expected_updated_at=started.updated_at,
        reconciled_at=datetime.now(UTC),
    )

    assert abandoned.pending_objective is None
    assert abandoned.claim_id is not None
    assert (
        abandoned.claim_resolution
        is SoftwareEngineeringClaimResolution.ABANDONED_AFTER_START
    )
    with pytest.raises(SoftwareEngineeringRunStateError):
        repository.claim_approved(action_id)


def test_rejection_scrubs_pending_objective(tmp_path: Path) -> None:
    repository = SqliteSoftwareEngineeringRunRepository(tmp_path / "state.sqlite3")
    action_id = uuid4()
    repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        objective="Sensitive pending task",
        approval_status=SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL,
    )

    repository.record_rejection(
        action_id=action_id,
        rejected_at=datetime.now(UTC),
    )

    state = repository.get(action_id)
    assert state is not None
    assert state.pending_objective is None
    assert state.approval_status is SoftwareEngineeringApprovalStatus.REJECTED


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



class RecordingDisposer:
    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.calls: list[tuple[UUID, WorkProductDisposition]] = []

    def apply(
        self,
        action_id: UUID,
        disposition: WorkProductDisposition,
    ) -> WorkProductDispositionResult:
        self.calls.append((action_id, disposition))
        return WorkProductDispositionResult(
            action_id=action_id,
            disposition=disposition,
            worktree_path=self.worktree,
            branch=f"velox/se-{action_id}",
            worktree_present=True,
            branch_present=True,
            canonical_unchanged=True,
        )


def test_disposition_uses_sqlite_execution_evidence_after_repository_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    action_id = uuid4()
    first_process = SqliteSoftwareEngineeringRunRepository(path)
    first_process.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        delegation_status="awaiting_approval",
    )
    first_process.record_execution(
        action_id=action_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        external_execution_performed=True,
    )

    restarted_repository = SqliteSoftwareEngineeringRunRepository(path)
    disposer = RecordingDisposer(tmp_path / "derived-worktree")
    service = SoftwareEngineeringWorkProductDispositionService(
        run_repository=restarted_repository,
        work_products=disposer,
    )

    result = service.apply(action_id, WorkProductDisposition.KEEP)

    assert result.succeeded is True
    reopened_again = SqliteSoftwareEngineeringRunRepository(path)
    persisted = reopened_again.get(action_id)
    assert persisted is not None
    assert persisted.execution_status == "succeeded"
    assert persisted.disposition is WorkProductDisposition.KEEP
    assert persisted.disposition_succeeded is True
