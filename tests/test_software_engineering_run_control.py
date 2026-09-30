"""Provider-neutral Software Engineering run status and reconciliation coverage."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from apps.server.src.core.actions import ExecutorRole
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_run_control import (
    SoftwareEngineeringClaimReconciliation,
    SoftwareEngineeringRunControlService,
    SoftwareEngineeringRunControlStateError,
    SoftwareEngineeringRunPhase,
    WorkProductInspectionStatus,
)
from apps.server.src.integrations.software_engineering_state import (
    InMemorySoftwareEngineeringRunRepository,
    SoftwareEngineeringApprovalStatus,
    SoftwareEngineeringClaimResolution,
)
from apps.server.src.integrations.software_engineering_work_product import (
    WorkProductReview,
)


class RecordingInspector:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def review(self, action_id: object) -> WorkProductReview:
        self.calls.append(action_id)
        return WorkProductReview(
            action_id=action_id,  # type: ignore[arg-type]
            worktree_path=Path("/hidden/worktree"),
            branch="velox/se-hidden",
            changed_files=("apps/a.py", "tests/test_a.py"),
            untracked_files=("notes.txt",),
            diff_stat="hidden",
            diff="secret diff",
            diff_truncated=False,
            dirty=True,
            canonical_clean=True,
            canonical_unchanged=True,
        )


def approved_repository() -> tuple[InMemorySoftwareEngineeringRunRepository, object]:
    repository = InMemorySoftwareEngineeringRunRepository()
    action_id = uuid4()
    repository.register_action(
        action_id=action_id,
        target="velox-server",
        executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
        capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        objective="Sensitive objective",
        approval_status=SoftwareEngineeringApprovalStatus.APPROVED,
    )
    return repository, action_id


def test_status_for_unstarted_claim_allows_release_only() -> None:
    repository, action_id = approved_repository()
    repository.claim_approved(action_id)  # type: ignore[arg-type]
    service = SoftwareEngineeringRunControlService(
        repository=repository,
        work_product_inspector=None,
    )

    status = service.status(action_id)  # type: ignore[arg-type]

    assert status.phase is SoftwareEngineeringRunPhase.CLAIMED
    assert status.claimed is True
    assert status.worker_started is False
    assert status.retriable is False
    assert status.reconciliation_options == (
        SoftwareEngineeringClaimReconciliation.RELEASE,
    )
    assert status.work_product.status is WorkProductInspectionStatus.NOT_APPLICABLE
    assert len(status.state_token) == 64


def test_release_uses_state_token_and_restores_retriable_approved_state() -> None:
    repository, action_id = approved_repository()
    repository.claim_approved(action_id)  # type: ignore[arg-type]
    service = SoftwareEngineeringRunControlService(
        repository=repository,
        work_product_inspector=None,
    )
    before = service.status(action_id)  # type: ignore[arg-type]

    after = service.reconcile(
        action_id,  # type: ignore[arg-type]
        resolution=SoftwareEngineeringClaimReconciliation.RELEASE,
        state_token=before.state_token,
        acknowledge_possible_external_side_effects=False,
    )

    assert after.phase is SoftwareEngineeringRunPhase.APPROVED
    assert after.claimed is False
    assert after.retriable is True
    assert after.reconciliation_options == ()
    persisted = repository.get(action_id)  # type: ignore[arg-type]
    assert persisted is not None
    assert (
        persisted.claim_resolution
        is SoftwareEngineeringClaimResolution.RELEASED_BEFORE_START
    )


def test_release_refuses_stale_status_token() -> None:
    repository, action_id = approved_repository()
    repository.claim_approved(action_id)  # type: ignore[arg-type]
    service = SoftwareEngineeringRunControlService(
        repository=repository,
        work_product_inspector=None,
    )
    before = service.status(action_id)  # type: ignore[arg-type]
    repository.record_execution_started(  # type: ignore[arg-type]
        action_id=action_id,
        started_at=datetime.now(UTC),
    )

    with pytest.raises(SoftwareEngineeringRunControlStateError, match="changed"):
        service.reconcile(
            action_id,  # type: ignore[arg-type]
            resolution=SoftwareEngineeringClaimReconciliation.RELEASE,
            state_token=before.state_token,
            acknowledge_possible_external_side_effects=False,
        )


def test_started_ambiguous_status_inspects_work_product_summary_only() -> None:
    repository, action_id = approved_repository()
    repository.claim_approved(action_id)  # type: ignore[arg-type]
    repository.record_execution_started(  # type: ignore[arg-type]
        action_id=action_id,
        started_at=datetime.now(UTC),
    )
    inspector = RecordingInspector()
    service = SoftwareEngineeringRunControlService(
        repository=repository,
        work_product_inspector=inspector,
    )

    status = service.status(action_id)  # type: ignore[arg-type]

    assert status.phase is SoftwareEngineeringRunPhase.RUNNING_OR_AMBIGUOUS
    assert status.reconciliation_options == (
        SoftwareEngineeringClaimReconciliation.ABANDON,
    )
    assert status.work_product.status is WorkProductInspectionStatus.AVAILABLE
    assert status.work_product.dirty is True
    assert status.work_product.changed_files_count == 2
    assert status.work_product.untracked_files_count == 1
    assert "/hidden/worktree" not in repr(status)
    assert "secret diff" not in repr(status)


def test_started_claim_requires_acknowledgement_before_abandon() -> None:
    repository, action_id = approved_repository()
    repository.claim_approved(action_id)  # type: ignore[arg-type]
    repository.record_execution_started(  # type: ignore[arg-type]
        action_id=action_id,
        started_at=datetime.now(UTC),
    )
    inspector = RecordingInspector()
    service = SoftwareEngineeringRunControlService(
        repository=repository,
        work_product_inspector=inspector,
    )
    before = service.status(action_id)  # type: ignore[arg-type]

    with pytest.raises(
        SoftwareEngineeringRunControlStateError,
        match="not acknowledged",
    ):
        service.reconcile(
            action_id,  # type: ignore[arg-type]
            resolution=SoftwareEngineeringClaimReconciliation.ABANDON,
            state_token=before.state_token,
            acknowledge_possible_external_side_effects=False,
        )


def test_abandon_started_claim_keeps_it_non_retriable_and_scrubs_objective() -> None:
    repository, action_id = approved_repository()
    repository.claim_approved(action_id)  # type: ignore[arg-type]
    repository.record_execution_started(  # type: ignore[arg-type]
        action_id=action_id,
        started_at=datetime.now(UTC),
    )
    inspector = RecordingInspector()
    service = SoftwareEngineeringRunControlService(
        repository=repository,
        work_product_inspector=inspector,
    )
    before = service.status(action_id)  # type: ignore[arg-type]

    after = service.reconcile(
        action_id,  # type: ignore[arg-type]
        resolution=SoftwareEngineeringClaimReconciliation.ABANDON,
        state_token=before.state_token,
        acknowledge_possible_external_side_effects=True,
    )

    assert after.phase is SoftwareEngineeringRunPhase.ABANDONED
    assert after.claimed is True
    assert after.retriable is False
    assert after.reconciliation_options == ()
    persisted = repository.get(action_id)  # type: ignore[arg-type]
    assert persisted is not None
    assert persisted.pending_objective is None
    assert (
        persisted.claim_resolution
        is SoftwareEngineeringClaimResolution.ABANDONED_AFTER_START
    )
    assert len(inspector.calls) >= 2


def test_terminal_execution_has_no_reconciliation_options() -> None:
    repository, action_id = approved_repository()
    repository.claim_approved(action_id)  # type: ignore[arg-type]
    repository.record_execution_started(  # type: ignore[arg-type]
        action_id=action_id,
        started_at=datetime.now(UTC),
    )
    repository.record_execution(  # type: ignore[arg-type]
        action_id=action_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        external_execution_performed=True,
    )
    service = SoftwareEngineeringRunControlService(
        repository=repository,
        work_product_inspector=None,
    )

    status = service.status(action_id)  # type: ignore[arg-type]

    assert status.phase is SoftwareEngineeringRunPhase.SUCCEEDED
    assert status.reconciliation_options == ()
    assert status.retriable is False
