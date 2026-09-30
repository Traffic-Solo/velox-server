"""Provider-neutral SE run status and ambiguous-claim reconciliation."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from apps.server.src.core.actions import ExecutorRole
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_state import (
    SoftwareEngineeringApprovalStatus,
    SoftwareEngineeringClaimResolution,
    SoftwareEngineeringRunRepository,
    SoftwareEngineeringRunState,
    SoftwareEngineeringRunStateError,
)
from apps.server.src.integrations.software_engineering_work_product import (
    WorkProductDisposition,
    WorkProductIdentityError,
    WorkProductReview,
)


class SoftwareEngineeringRunControlNotFoundError(LookupError):
    """No durable Software Engineering run exists for the requested Action."""


class SoftwareEngineeringRunControlStateError(RuntimeError):
    """The requested status/reconciliation operation is not valid."""


class SoftwareEngineeringRunPhase(StrEnum):
    """Safe derived lifecycle phase for one durable Software Engineering run."""

    REGISTERED = "registered"
    ROUTE_REJECTED = "route_rejected"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    CLAIMED = "claimed"
    RUNNING_OR_AMBIGUOUS = "running_or_ambiguous"
    ABANDONED = "abandoned"
    REJECTED = "rejected"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    KEPT = "kept"
    DISCARDED = "discarded"
    PROMOTED = "promoted"


class SoftwareEngineeringClaimReconciliation(StrEnum):
    """Operator-controlled reconciliation choices."""

    RELEASE = "release"
    ABANDON = "abandon"


class WorkProductInspectionStatus(StrEnum):
    """Whether bounded Action-derived work-product inspection was available."""

    NOT_APPLICABLE = "not_applicable"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SoftwareEngineeringWorkProductSummary:
    """Safe bounded work-product summary with no path, branch or diff contents."""

    status: WorkProductInspectionStatus
    dirty: bool | None = None
    changed_files_count: int | None = None
    untracked_files_count: int | None = None
    canonical_clean: bool | None = None
    canonical_unchanged: bool | None = None


@dataclass(frozen=True, slots=True)
class SoftwareEngineeringRunStatus:
    """Provider-neutral public status for one durable SE Action."""

    action_id: UUID
    target: str
    phase: SoftwareEngineeringRunPhase
    approval_status: SoftwareEngineeringApprovalStatus | None
    claimed: bool
    worker_started: bool
    execution_status: str | None
    external_execution_performed: bool | None
    disposition: WorkProductDisposition | None
    claim_resolution: SoftwareEngineeringClaimResolution | None
    promoted: bool
    pull_request_number: int | None
    pull_request_url: str | None
    promotion_base_branch: str | None
    promotion_head_branch: str | None
    reconciliation_options: tuple[SoftwareEngineeringClaimReconciliation, ...]
    state_token: str
    work_product: SoftwareEngineeringWorkProductSummary


class SoftwareEngineeringWorkProductInspector(Protocol):
    """Provider-neutral bounded review boundary used for ambiguous claims."""

    def review(self, action_id: UUID) -> WorkProductReview:
        ...


class SoftwareEngineeringRunControlService:
    """Read durable SE status and reconcile claims without invoking a worker."""

    def __init__(
        self,
        *,
        repository: SoftwareEngineeringRunRepository,
        work_product_inspector: SoftwareEngineeringWorkProductInspector | None,
    ) -> None:
        self._repository = repository
        self._work_product_inspector = work_product_inspector

    def status(self, action_id: UUID) -> SoftwareEngineeringRunStatus:
        """Return safe durable status, inspecting work product only when relevant."""
        state = self._get(action_id)
        return self._status_from_state(state)

    def reconcile(
        self,
        action_id: UUID,
        *,
        resolution: SoftwareEngineeringClaimReconciliation,
        state_token: str,
        acknowledge_possible_external_side_effects: bool,
    ) -> SoftwareEngineeringRunStatus:
        """Apply one evidence-gated reconciliation and never invoke WorkerRuntime."""
        state = self._get(action_id)
        if self._state_token(state) != state_token:
            raise SoftwareEngineeringRunControlStateError(
                "software engineering run state changed since status inspection"
            )
        if state.updated_at is None:
            raise SoftwareEngineeringRunControlStateError(
                "software engineering run has no durable state version"
            )

        now = datetime.now(UTC)
        try:
            if resolution is SoftwareEngineeringClaimReconciliation.RELEASE:
                self._require_releasable(state)
                updated = self._repository.release_unstarted_claim(
                    action_id=action_id,
                    expected_updated_at=state.updated_at,
                    reconciled_at=now,
                )
            else:
                self._require_abandonable(
                    state,
                    acknowledge_possible_external_side_effects=(
                        acknowledge_possible_external_side_effects
                    ),
                )
                self._inspect_for_abandon(action_id)
                updated = self._repository.abandon_started_claim(
                    action_id=action_id,
                    expected_updated_at=state.updated_at,
                    reconciled_at=now,
                )
        except SoftwareEngineeringRunStateError as error:
            raise SoftwareEngineeringRunControlStateError(
                "software engineering claim reconciliation failed closed"
            ) from error

        return self._status_from_state(updated)

    def _get(self, action_id: UUID) -> SoftwareEngineeringRunState:
        try:
            state = self._repository.get(action_id)
        except SoftwareEngineeringRunStateError as error:
            raise SoftwareEngineeringRunControlStateError(
                "software engineering durable state is unavailable"
            ) from error
        if state is None:
            raise SoftwareEngineeringRunControlNotFoundError(
                "software engineering action was not found"
            )
        if (
            state.executor_role != ExecutorRole.SOFTWARE_ENGINEERING.value
            or state.capability != SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
        ):
            raise SoftwareEngineeringRunControlNotFoundError(
                "software engineering action was not found"
            )
        return state

    def _status_from_state(
        self,
        state: SoftwareEngineeringRunState,
    ) -> SoftwareEngineeringRunStatus:
        options = self._reconciliation_options(state)
        inspect = (
            SoftwareEngineeringClaimReconciliation.ABANDON in options
            or state.claim_resolution
            is SoftwareEngineeringClaimResolution.ABANDONED_AFTER_START
        )
        work_product = (
            self._inspect_work_product(state.action_id)
            if inspect
            else SoftwareEngineeringWorkProductSummary(
                status=WorkProductInspectionStatus.NOT_APPLICABLE
            )
        )
        return SoftwareEngineeringRunStatus(
            action_id=state.action_id,
            target=state.target,
            phase=self._phase(state),
            approval_status=state.approval_status,
            claimed=state.claim_id is not None,
            worker_started=state.execution_started_at is not None,
            execution_status=state.execution_status,
            external_execution_performed=state.external_execution_performed,
            disposition=state.disposition,
            claim_resolution=state.claim_resolution,
            promoted=state.promotion_finished_at is not None,
            pull_request_number=state.pull_request_number,
            pull_request_url=state.pull_request_url,
            promotion_base_branch=state.promotion_base_branch,
            promotion_head_branch=state.promotion_head_branch,
            reconciliation_options=options,
            state_token=self._state_token(state),
            work_product=work_product,
        )

    @staticmethod
    def _phase(state: SoftwareEngineeringRunState) -> SoftwareEngineeringRunPhase:
        if state.promotion_finished_at is not None:
            return SoftwareEngineeringRunPhase.PROMOTED
        if state.disposition_succeeded and state.disposition is WorkProductDisposition.KEEP:
            return SoftwareEngineeringRunPhase.KEPT
        if (
            state.disposition_succeeded
            and state.disposition is WorkProductDisposition.DISCARD
        ):
            return SoftwareEngineeringRunPhase.DISCARDED
        if state.execution_status == "succeeded":
            return SoftwareEngineeringRunPhase.SUCCEEDED
        if state.execution_status == "failed":
            return SoftwareEngineeringRunPhase.FAILED
        if state.execution_status == "skipped":
            return SoftwareEngineeringRunPhase.SKIPPED
        if (
            state.claim_resolution
            is SoftwareEngineeringClaimResolution.ABANDONED_AFTER_START
        ):
            return SoftwareEngineeringRunPhase.ABANDONED
        if state.claim_id is not None and state.execution_started_at is not None:
            return SoftwareEngineeringRunPhase.RUNNING_OR_AMBIGUOUS
        if state.claim_id is not None:
            return SoftwareEngineeringRunPhase.CLAIMED
        if state.approval_status is SoftwareEngineeringApprovalStatus.REJECTED:
            return SoftwareEngineeringRunPhase.REJECTED
        if state.approval_status is SoftwareEngineeringApprovalStatus.APPROVED:
            return SoftwareEngineeringRunPhase.APPROVED
        if (
            state.approval_status
            is SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL
        ):
            return SoftwareEngineeringRunPhase.AWAITING_APPROVAL
        if state.delegation_status == "route_rejected":
            return SoftwareEngineeringRunPhase.ROUTE_REJECTED
        return SoftwareEngineeringRunPhase.REGISTERED

    @staticmethod
    def _reconciliation_options(
        state: SoftwareEngineeringRunState,
    ) -> tuple[SoftwareEngineeringClaimReconciliation, ...]:
        if state.execution_status is not None or state.claim_id is None:
            return ()
        if (
            state.claim_resolution
            is SoftwareEngineeringClaimResolution.ABANDONED_AFTER_START
        ):
            return ()
        if state.execution_started_at is None:
            return (SoftwareEngineeringClaimReconciliation.RELEASE,)
        return (SoftwareEngineeringClaimReconciliation.ABANDON,)

    @staticmethod
    def _require_releasable(state: SoftwareEngineeringRunState) -> None:
        if (
            state.claim_id is None
            or state.execution_started_at is not None
            or state.execution_status is not None
            or state.approval_status is not SoftwareEngineeringApprovalStatus.APPROVED
        ):
            raise SoftwareEngineeringRunControlStateError(
                "software engineering claim cannot be safely released"
            )

    @staticmethod
    def _require_abandonable(
        state: SoftwareEngineeringRunState,
        *,
        acknowledge_possible_external_side_effects: bool,
    ) -> None:
        if not acknowledge_possible_external_side_effects:
            raise SoftwareEngineeringRunControlStateError(
                "possible external execution side effects were not acknowledged"
            )
        if (
            state.claim_id is None
            or state.execution_started_at is None
            or state.execution_status is not None
            or state.claim_resolution
            is SoftwareEngineeringClaimResolution.ABANDONED_AFTER_START
        ):
            raise SoftwareEngineeringRunControlStateError(
                "software engineering claim cannot be abandoned"
            )

    def _inspect_for_abandon(self, action_id: UUID) -> None:
        if self._work_product_inspector is None:
            raise SoftwareEngineeringRunControlStateError(
                "software engineering work-product inspection is unavailable"
            )
        try:
            self._work_product_inspector.review(action_id)
        except WorkProductIdentityError:
            # Missing/unverifiable Action-derived work product is itself a bounded
            # inspection result. Abandon remains safe because it never retries.
            return

    def _inspect_work_product(
        self,
        action_id: UUID,
    ) -> SoftwareEngineeringWorkProductSummary:
        if self._work_product_inspector is None:
            return SoftwareEngineeringWorkProductSummary(
                status=WorkProductInspectionStatus.UNAVAILABLE
            )
        try:
            review = self._work_product_inspector.review(action_id)
        except WorkProductIdentityError:
            return SoftwareEngineeringWorkProductSummary(
                status=WorkProductInspectionStatus.UNAVAILABLE
            )
        return SoftwareEngineeringWorkProductSummary(
            status=WorkProductInspectionStatus.AVAILABLE,
            dirty=review.dirty,
            changed_files_count=len(review.changed_files),
            untracked_files_count=len(review.untracked_files),
            canonical_clean=review.canonical_clean,
            canonical_unchanged=review.canonical_unchanged,
        )

    @staticmethod
    def _state_token(state: SoftwareEngineeringRunState) -> str:
        if state.updated_at is None:
            raise SoftwareEngineeringRunControlStateError(
                "software engineering run has no durable state version"
            )
        material = "|".join(
            (
                str(state.action_id),
                state.updated_at.isoformat(),
                str(state.claim_id or ""),
                state.execution_started_at.isoformat()
                if state.execution_started_at is not None
                else "",
                state.execution_status or "",
                state.claim_resolution.value
                if state.claim_resolution is not None
                else "",
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
