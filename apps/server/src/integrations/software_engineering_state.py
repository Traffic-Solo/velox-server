"""Provider-neutral durable state for Software Engineering control-plane evidence."""

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from apps.server.src.core.actions import Action, ExecutorRole
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)
from apps.server.src.integrations.software_engineering_work_product import (
    WorkProductDisposition,
    WorkProductDispositionResult,
)
from apps.server.src.workers.executor import (
    WorkerExecutionFailure,
    WorkerExecutionStatus,
)
from apps.server.src.workers.runtime import (
    WorkerExecutionObservation,
    WorkerExecutionObserver,
)

_SCHEMA_VERSION = 2
_SCHEMA_NAME = "software_engineering_runs"


class SoftwareEngineeringRunStateError(RuntimeError):
    """Durable Software Engineering state is unavailable, invalid or inconsistent."""


class SoftwareEngineeringApprovalStatus(StrEnum):
    """Durable approval state for one recoverable SE Action."""

    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SoftwareEngineeringActionClaim:
    """One durable, non-expiring claim for exact SE execution."""

    action_id: UUID
    claim_id: UUID
    target: str
    objective: str
    claimed_at: datetime


@dataclass(frozen=True, slots=True)
class SoftwareEngineeringRunState:
    """Normalized durable evidence for one Software Engineering Action."""

    action_id: UUID
    executor_role: str
    capability: str
    target: str
    delegation_status: str | None = None
    pending_objective: str | None = None
    approval_status: SoftwareEngineeringApprovalStatus | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    claim_id: UUID | None = None
    claimed_at: datetime | None = None
    execution_started_at: datetime | None = None
    execution_status: str | None = None
    execution_finished_at: datetime | None = None
    external_execution_performed: bool | None = None
    disposition: WorkProductDisposition | None = None
    disposition_succeeded: bool | None = None
    worktree_present: bool | None = None
    branch_present: bool | None = None
    canonical_unchanged: bool | None = None
    promotion_commit_sha: str | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    promotion_base_branch: str | None = None
    promotion_head_branch: str | None = None
    promotion_finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SoftwareEngineeringRunRepository(Protocol):
    """Persistence Role for normalized Software Engineering run evidence."""

    def get(self, action_id: UUID) -> SoftwareEngineeringRunState | None:
        ...

    def list_pending_approval(self) -> list[SoftwareEngineeringRunState]:
        ...

    def register_action(
        self,
        *,
        action_id: UUID,
        target: str,
        executor_role: str,
        capability: str,
        delegation_status: str | None = None,
        objective: str | None = None,
        approval_status: SoftwareEngineeringApprovalStatus | None = None,
    ) -> SoftwareEngineeringRunState:
        ...

    def record_approval(
        self,
        *,
        action_id: UUID,
        objective: str,
        approved_at: datetime,
    ) -> SoftwareEngineeringRunState:
        ...

    def record_rejection(
        self,
        *,
        action_id: UUID,
        rejected_at: datetime,
    ) -> SoftwareEngineeringRunState:
        ...

    def claim_approved(
        self,
        action_id: UUID,
    ) -> SoftwareEngineeringActionClaim:
        ...

    def record_execution_started(
        self,
        *,
        action_id: UUID,
        started_at: datetime,
    ) -> SoftwareEngineeringRunState:
        ...

    def record_execution(
        self,
        *,
        action_id: UUID,
        status: str,
        finished_at: datetime,
        external_execution_performed: bool,
    ) -> SoftwareEngineeringRunState:
        ...

    def record_disposition(
        self,
        result: WorkProductDispositionResult,
    ) -> SoftwareEngineeringRunState:
        ...

    def record_promotion(
        self,
        *,
        action_id: UUID,
        commit_sha: str,
        pull_request_number: int,
        pull_request_url: str,
        base_branch: str,
        head_branch: str,
        finished_at: datetime,
    ) -> SoftwareEngineeringRunState:
        ...


class InMemorySoftwareEngineeringRunRepository:
    """Process-local implementation used when no live SE provider is configured."""

    def __init__(self) -> None:
        self._states: dict[UUID, SoftwareEngineeringRunState] = {}

    def get(self, action_id: UUID) -> SoftwareEngineeringRunState | None:
        return self._states.get(action_id)

    def list_pending_approval(self) -> list[SoftwareEngineeringRunState]:
        return [
            state
            for state in self._states.values()
            if (
                state.approval_status
                is SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL
                and state.claim_id is None
                and state.execution_status is None
            )
        ]

    def register_action(
        self,
        *,
        action_id: UUID,
        target: str,
        executor_role: str,
        capability: str,
        delegation_status: str | None = None,
        objective: str | None = None,
        approval_status: SoftwareEngineeringApprovalStatus | None = None,
    ) -> SoftwareEngineeringRunState:
        normalized_objective = _normalize_objective(objective)
        current = self._states.get(action_id)
        if current is not None:
            _require_same_identity(current, target, executor_role, capability)
            _require_compatible_objective(current, normalized_objective)
            current = replace(
                current,
                delegation_status=(
                    delegation_status
                    if delegation_status is not None
                    else current.delegation_status
                ),
                pending_objective=(
                    normalized_objective
                    if (
                        normalized_objective is not None
                        and current.pending_objective is None
                    )
                    else current.pending_objective
                ),
                approval_status=(
                    approval_status
                    if approval_status is not None
                    else current.approval_status
                ),
                updated_at=_utcnow(),
            )
            self._states[action_id] = current
            return current
        now = _utcnow()
        state = SoftwareEngineeringRunState(
            action_id=action_id,
            executor_role=executor_role,
            capability=capability,
            target=target,
            delegation_status=delegation_status,
            pending_objective=normalized_objective,
            approval_status=approval_status,
            created_at=now,
            updated_at=now,
        )
        self._states[action_id] = state
        return state

    def record_approval(
        self,
        *,
        action_id: UUID,
        objective: str,
        approved_at: datetime,
    ) -> SoftwareEngineeringRunState:
        current = self._require(action_id)
        normalized = _normalize_required_objective(objective)
        _require_compatible_objective(current, normalized)
        if current.approval_status is SoftwareEngineeringApprovalStatus.REJECTED:
            raise SoftwareEngineeringRunStateError(
                "rejected software engineering action cannot be approved"
            )
        state = replace(
            current,
            pending_objective=normalized,
            approval_status=SoftwareEngineeringApprovalStatus.APPROVED,
            approved_at=_require_aware(approved_at),
            updated_at=_utcnow(),
        )
        self._states[action_id] = state
        return state

    def record_rejection(
        self,
        *,
        action_id: UUID,
        rejected_at: datetime,
    ) -> SoftwareEngineeringRunState:
        current = self._require(action_id)
        if current.claim_id is not None or current.execution_status is not None:
            raise SoftwareEngineeringRunStateError(
                "claimed or executed software engineering action cannot be rejected"
            )
        state = replace(
            current,
            pending_objective=None,
            approval_status=SoftwareEngineeringApprovalStatus.REJECTED,
            rejected_at=_require_aware(rejected_at),
            updated_at=_utcnow(),
        )
        self._states[action_id] = state
        return state

    def claim_approved(
        self,
        action_id: UUID,
    ) -> SoftwareEngineeringActionClaim:
        current = self._require(action_id)
        if current.approval_status is not SoftwareEngineeringApprovalStatus.APPROVED:
            raise SoftwareEngineeringRunStateError(
                "software engineering action is not durably approved"
            )
        if current.execution_status is not None:
            raise SoftwareEngineeringRunStateError(
                "software engineering action already has an execution result"
            )
        if current.claim_id is not None:
            raise SoftwareEngineeringRunStateError(
                "software engineering action already has a durable execution claim"
            )
        objective = _normalize_required_objective(current.pending_objective)
        claim_id = uuid4()
        claimed_at = _utcnow()
        state = replace(
            current,
            claim_id=claim_id,
            claimed_at=claimed_at,
            updated_at=claimed_at,
        )
        self._states[action_id] = state
        return SoftwareEngineeringActionClaim(
            action_id=action_id,
            claim_id=claim_id,
            target=current.target,
            objective=objective,
            claimed_at=claimed_at,
        )

    def record_execution_started(
        self,
        *,
        action_id: UUID,
        started_at: datetime,
    ) -> SoftwareEngineeringRunState:
        current = self._require(action_id)
        if current.claim_id is None:
            raise SoftwareEngineeringRunStateError(
                "software engineering execution has no durable claim"
            )
        if current.execution_status is not None:
            raise SoftwareEngineeringRunStateError(
                "software engineering action already has an execution result"
            )
        state = replace(
            current,
            execution_started_at=_require_aware(started_at),
            updated_at=_utcnow(),
        )
        self._states[action_id] = state
        return state

    def record_execution(
        self,
        *,
        action_id: UUID,
        status: str,
        finished_at: datetime,
        external_execution_performed: bool,
    ) -> SoftwareEngineeringRunState:
        current = self._require(action_id)
        state = replace(
            current,
            pending_objective=None,
            execution_status=status,
            execution_finished_at=_require_aware(finished_at),
            external_execution_performed=external_execution_performed,
            updated_at=_utcnow(),
        )
        self._states[action_id] = state
        return state

    def record_disposition(
        self,
        result: WorkProductDispositionResult,
    ) -> SoftwareEngineeringRunState:
        current = self._require(result.action_id)
        state = replace(
            current,
            disposition=result.disposition,
            disposition_succeeded=result.succeeded,
            worktree_present=result.worktree_present,
            branch_present=result.branch_present,
            canonical_unchanged=result.canonical_unchanged,
            updated_at=_utcnow(),
        )
        self._states[result.action_id] = state
        return state

    def record_promotion(
        self,
        *,
        action_id: UUID,
        commit_sha: str,
        pull_request_number: int,
        pull_request_url: str,
        base_branch: str,
        head_branch: str,
        finished_at: datetime,
    ) -> SoftwareEngineeringRunState:
        current = self._require(action_id)
        state = replace(
            current,
            promotion_commit_sha=commit_sha,
            pull_request_number=pull_request_number,
            pull_request_url=pull_request_url,
            promotion_base_branch=base_branch,
            promotion_head_branch=head_branch,
            promotion_finished_at=_require_aware(finished_at),
            updated_at=_utcnow(),
        )
        self._states[action_id] = state
        return state

    def _require(self, action_id: UUID) -> SoftwareEngineeringRunState:
        state = self._states.get(action_id)
        if state is None:
            raise SoftwareEngineeringRunStateError(
                "software engineering run state does not exist"
            )
        return state


class SqliteSoftwareEngineeringRunRepository:
    """SQLite implementation with short atomic transactions per operation."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise SoftwareEngineeringRunStateError(
                "software engineering state database path must be absolute"
            )
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS velox_schema (
                        name TEXT PRIMARY KEY,
                        version INTEGER NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT version FROM velox_schema WHERE name = ?",
                    (_SCHEMA_NAME,),
                ).fetchone()
                if row is None:
                    self._create_v2_table(connection)
                    connection.execute(
                        "INSERT INTO velox_schema(name, version) VALUES (?, ?)",
                        (_SCHEMA_NAME, _SCHEMA_VERSION),
                    )
                else:
                    version = int(row["version"])
                    if version == 1:
                        self._migrate_v1_to_v2(connection)
                        connection.execute(
                            "UPDATE velox_schema SET version = ? WHERE name = ?",
                            (_SCHEMA_VERSION, _SCHEMA_NAME),
                        )
                    elif version == _SCHEMA_VERSION:
                        self._create_v2_table(connection)
                    else:
                        raise SoftwareEngineeringRunStateError(
                            "unsupported software engineering state schema version"
                        )
        except sqlite3.Error:
            raise SoftwareEngineeringRunStateError(
                "software engineering state database is unavailable"
            ) from None

    @staticmethod
    def _create_v2_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS software_engineering_runs (
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
            )
            """
        )

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(software_engineering_runs)"
            ).fetchall()
        }
        additions = {
            "pending_objective": "TEXT",
            "approval_status": "TEXT",
            "approved_at": "TEXT",
            "rejected_at": "TEXT",
            "claim_id": "TEXT",
            "claimed_at": "TEXT",
            "execution_started_at": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE software_engineering_runs ADD COLUMN {name} {sql_type}"
                )

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self._path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            return connection
        except sqlite3.Error:
            raise SoftwareEngineeringRunStateError(
                "software engineering state database is unavailable"
            ) from None

    def get(self, action_id: UUID) -> SoftwareEngineeringRunState | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM software_engineering_runs WHERE action_id = ?",
                    (str(action_id),),
                ).fetchone()
        except sqlite3.Error:
            raise SoftwareEngineeringRunStateError(
                "software engineering state could not be read"
            ) from None
        return _state_from_row(row) if row is not None else None

    def list_pending_approval(self) -> list[SoftwareEngineeringRunState]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM software_engineering_runs
                    WHERE approval_status = ?
                      AND claim_id IS NULL
                      AND execution_status IS NULL
                    ORDER BY created_at, action_id
                    """,
                    (SoftwareEngineeringApprovalStatus.AWAITING_APPROVAL.value,),
                ).fetchall()
        except sqlite3.Error:
            raise SoftwareEngineeringRunStateError(
                "software engineering pending approvals could not be read"
            ) from None
        return [_state_from_row(row) for row in rows]

    def register_action(
        self,
        *,
        action_id: UUID,
        target: str,
        executor_role: str,
        capability: str,
        delegation_status: str | None = None,
        objective: str | None = None,
        approval_status: SoftwareEngineeringApprovalStatus | None = None,
    ) -> SoftwareEngineeringRunState:
        normalized_objective = _normalize_objective(objective)
        current = self.get(action_id)
        if current is not None:
            _require_same_identity(current, target, executor_role, capability)
            _require_compatible_objective(current, normalized_objective)
            assignments = ["updated_at = ?"]
            values: list[object] = [_utcnow().isoformat()]
            if delegation_status is not None:
                assignments.append("delegation_status = ?")
                values.append(delegation_status)
            if normalized_objective is not None and current.pending_objective is None:
                assignments.append("pending_objective = ?")
                values.append(normalized_objective)
            if approval_status is not None:
                assignments.append("approval_status = ?")
                values.append(approval_status.value)
            self._update(
                action_id,
                ", ".join(assignments),
                tuple(values),
            )
            return self._require(action_id)

        now = _utcnow().isoformat()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO software_engineering_runs (
                        action_id, executor_role, capability, target,
                        delegation_status, pending_objective, approval_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(action_id),
                        executor_role,
                        capability,
                        target,
                        delegation_status,
                        normalized_objective,
                        approval_status.value if approval_status is not None else None,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            current = self.get(action_id)
            if current is None:
                raise SoftwareEngineeringRunStateError(
                    "software engineering action identity could not be registered"
                ) from None
            _require_same_identity(current, target, executor_role, capability)
            _require_compatible_objective(current, normalized_objective)
        except sqlite3.Error:
            raise SoftwareEngineeringRunStateError(
                "software engineering action identity could not be registered"
            ) from None

        return self._require(action_id)

    def record_approval(
        self,
        *,
        action_id: UUID,
        objective: str,
        approved_at: datetime,
    ) -> SoftwareEngineeringRunState:
        current = self._require(action_id)
        normalized = _normalize_required_objective(objective)
        _require_compatible_objective(current, normalized)
        if current.approval_status is SoftwareEngineeringApprovalStatus.REJECTED:
            raise SoftwareEngineeringRunStateError(
                "rejected software engineering action cannot be approved"
            )
        self._update(
            action_id,
            """
            pending_objective = ?,
            approval_status = ?,
            approved_at = ?,
            updated_at = ?
            """,
            (
                normalized,
                SoftwareEngineeringApprovalStatus.APPROVED.value,
                _require_aware(approved_at).isoformat(),
                _utcnow().isoformat(),
            ),
        )
        return self._require(action_id)

    def record_rejection(
        self,
        *,
        action_id: UUID,
        rejected_at: datetime,
    ) -> SoftwareEngineeringRunState:
        current = self._require(action_id)
        if current.claim_id is not None or current.execution_status is not None:
            raise SoftwareEngineeringRunStateError(
                "claimed or executed software engineering action cannot be rejected"
            )
        self._update(
            action_id,
            """
            pending_objective = NULL,
            approval_status = ?,
            rejected_at = ?,
            updated_at = ?
            """,
            (
                SoftwareEngineeringApprovalStatus.REJECTED.value,
                _require_aware(rejected_at).isoformat(),
                _utcnow().isoformat(),
            ),
        )
        return self._require(action_id)

    def claim_approved(
        self,
        action_id: UUID,
    ) -> SoftwareEngineeringActionClaim:
        claim_id = uuid4()
        claimed_at = _utcnow()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM software_engineering_runs WHERE action_id = ?",
                    (str(action_id),),
                ).fetchone()
                if row is None:
                    raise SoftwareEngineeringRunStateError(
                        "software engineering run state does not exist"
                    )
                current = _state_from_row(row)
                if (
                    current.approval_status
                    is not SoftwareEngineeringApprovalStatus.APPROVED
                ):
                    raise SoftwareEngineeringRunStateError(
                        "software engineering action is not durably approved"
                    )
                if current.execution_status is not None:
                    raise SoftwareEngineeringRunStateError(
                        "software engineering action already has an execution result"
                    )
                if current.claim_id is not None:
                    raise SoftwareEngineeringRunStateError(
                        "software engineering action already has a durable execution claim"
                    )
                objective = _normalize_required_objective(current.pending_objective)
                cursor = connection.execute(
                    """
                    UPDATE software_engineering_runs
                    SET claim_id = ?, claimed_at = ?, updated_at = ?
                    WHERE action_id = ?
                      AND approval_status = ?
                      AND claim_id IS NULL
                      AND execution_status IS NULL
                    """,
                    (
                        str(claim_id),
                        claimed_at.isoformat(),
                        claimed_at.isoformat(),
                        str(action_id),
                        SoftwareEngineeringApprovalStatus.APPROVED.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SoftwareEngineeringRunStateError(
                        "software engineering action could not be claimed atomically"
                    )
        except sqlite3.Error:
            raise SoftwareEngineeringRunStateError(
                "software engineering execution claim could not be persisted"
            ) from None

        return SoftwareEngineeringActionClaim(
            action_id=action_id,
            claim_id=claim_id,
            target=current.target,
            objective=objective,
            claimed_at=claimed_at,
        )

    def record_execution_started(
        self,
        *,
        action_id: UUID,
        started_at: datetime,
    ) -> SoftwareEngineeringRunState:
        current = self._require(action_id)
        if current.claim_id is None:
            raise SoftwareEngineeringRunStateError(
                "software engineering execution has no durable claim"
            )
        if current.execution_status is not None:
            raise SoftwareEngineeringRunStateError(
                "software engineering action already has an execution result"
            )
        self._update(
            action_id,
            "execution_started_at = ?, updated_at = ?",
            (
                _require_aware(started_at).isoformat(),
                _utcnow().isoformat(),
            ),
        )
        return self._require(action_id)

    def record_execution(
        self,
        *,
        action_id: UUID,
        status: str,
        finished_at: datetime,
        external_execution_performed: bool,
    ) -> SoftwareEngineeringRunState:
        self._require(action_id)
        self._update(
            action_id,
            """
            pending_objective = NULL,
            execution_status = ?,
            execution_finished_at = ?,
            external_execution_performed = ?,
            updated_at = ?
            """,
            (
                status,
                _require_aware(finished_at).isoformat(),
                int(external_execution_performed),
                _utcnow().isoformat(),
            ),
        )
        return self._require(action_id)

    def record_disposition(
        self,
        result: WorkProductDispositionResult,
    ) -> SoftwareEngineeringRunState:
        self._require(result.action_id)
        self._update(
            result.action_id,
            """
            disposition = ?,
            disposition_succeeded = ?,
            worktree_present = ?,
            branch_present = ?,
            canonical_unchanged = ?,
            updated_at = ?
            """,
            (
                result.disposition.value,
                int(result.succeeded),
                int(result.worktree_present),
                int(result.branch_present),
                int(result.canonical_unchanged),
                _utcnow().isoformat(),
            ),
        )
        return self._require(result.action_id)

    def record_promotion(
        self,
        *,
        action_id: UUID,
        commit_sha: str,
        pull_request_number: int,
        pull_request_url: str,
        base_branch: str,
        head_branch: str,
        finished_at: datetime,
    ) -> SoftwareEngineeringRunState:
        self._require(action_id)
        self._update(
            action_id,
            """
            promotion_commit_sha = ?,
            pull_request_number = ?,
            pull_request_url = ?,
            promotion_base_branch = ?,
            promotion_head_branch = ?,
            promotion_finished_at = ?,
            updated_at = ?
            """,
            (
                commit_sha,
                pull_request_number,
                pull_request_url,
                base_branch,
                head_branch,
                _require_aware(finished_at).isoformat(),
                _utcnow().isoformat(),
            ),
        )
        return self._require(action_id)

    def _require(self, action_id: UUID) -> SoftwareEngineeringRunState:
        state = self.get(action_id)
        if state is None:
            raise SoftwareEngineeringRunStateError(
                "software engineering run state does not exist"
            )
        return state

    def _update(
        self,
        action_id: UUID,
        assignments: str,
        values: tuple[object, ...],
    ) -> None:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    f"""
                    UPDATE software_engineering_runs
                    SET {assignments}
                    WHERE action_id = ?
                    """,
                    (*values, str(action_id)),
                )
                if cursor.rowcount != 1:
                    raise SoftwareEngineeringRunStateError(
                        "software engineering run state does not exist"
                    )
        except sqlite3.Error:
            raise SoftwareEngineeringRunStateError(
                "software engineering state could not be updated"
            ) from None


class DurableSoftwareEngineeringExecutionObserver:
    """Persist canonical SE execution evidence while retaining observer behavior."""

    def __init__(
        self,
        observer: WorkerExecutionObserver,
        repository: SoftwareEngineeringRunRepository,
    ) -> None:
        self._observer = observer
        self._repository = repository

    def start(
        self,
        action: Action,
        requested_role: str | None,
        executor_registered: bool,
        requested_capability: str | None = None,
        requested_provider: str | None = None,
        matched_provider: str | None = None,
        requested_account_context: dict[str, str | None] | None = None,
        matched_account_context: dict[str, str | None] | None = None,
        account_context_used: dict[str, str | None] | None = None,
        routing_reason: str | None = None,
    ) -> WorkerExecutionObservation:
        observation = self._observer.start(
            action=action,
            requested_role=requested_role,
            executor_registered=executor_registered,
            requested_capability=requested_capability,
            requested_provider=requested_provider,
            matched_provider=matched_provider,
            requested_account_context=requested_account_context,
            matched_account_context=matched_account_context,
            account_context_used=account_context_used,
            routing_reason=routing_reason,
        )
        if _is_canonical_route(requested_role, requested_capability):
            state = self._repository.register_action(
                action_id=action.id,
                target=action.target,
                executor_role=ExecutorRole.SOFTWARE_ENGINEERING.value,
                capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
            )
            if state.claim_id is not None and state.execution_started_at is None:
                self._repository.record_execution_started(
                    action_id=action.id,
                    started_at=observation.started_at,
                )
        return observation

    def finish(
        self,
        observation: WorkerExecutionObservation,
        status: WorkerExecutionStatus,
        metadata: dict[str, Any],
        reason: str | None = None,
        failure: WorkerExecutionFailure | None = None,
        duration_ms: float | None = None,
    ) -> WorkerExecutionObservation:
        finished = self._observer.finish(
            observation=observation,
            status=status,
            metadata=metadata,
            reason=reason,
            failure=failure,
            duration_ms=duration_ms,
        )
        if _is_canonical_route(
            finished.requested_role,
            finished.requested_capability,
        ):
            if finished.finished_at is None:
                raise SoftwareEngineeringRunStateError(
                    "finished execution observation has no completion time"
                )
            self._repository.record_execution(
                action_id=finished.action_id,
                status=status.value,
                finished_at=finished.finished_at,
                external_execution_performed=bool(
                    metadata.get("external_execution_performed", False)
                ),
            )
        return finished

    def list(self) -> list[WorkerExecutionObservation]:
        return self._observer.list()


def default_software_engineering_state_path(workspace_root: Path) -> Path:
    """Derive a durable state path outside the canonical repository."""
    root = workspace_root.expanduser()
    if not root.is_absolute():
        raise SoftwareEngineeringRunStateError(
            "trusted software engineering workspace must be absolute"
        )
    return root.parent / ".velox" / f"{root.name}-software-engineering.sqlite3"


def _state_from_row(row: sqlite3.Row) -> SoftwareEngineeringRunState:
    try:
        disposition_raw = row["disposition"]
        return SoftwareEngineeringRunState(
            action_id=UUID(str(row["action_id"])),
            executor_role=str(row["executor_role"]),
            capability=str(row["capability"]),
            target=str(row["target"]),
            delegation_status=_optional_text(row["delegation_status"]),
            pending_objective=_optional_text(row["pending_objective"]),
            approval_status=(
                SoftwareEngineeringApprovalStatus(str(row["approval_status"]))
                if row["approval_status"] is not None
                else None
            ),
            approved_at=_optional_datetime(row["approved_at"]),
            rejected_at=_optional_datetime(row["rejected_at"]),
            claim_id=(
                UUID(str(row["claim_id"])) if row["claim_id"] is not None else None
            ),
            claimed_at=_optional_datetime(row["claimed_at"]),
            execution_started_at=_optional_datetime(row["execution_started_at"]),
            execution_status=_optional_text(row["execution_status"]),
            execution_finished_at=_optional_datetime(row["execution_finished_at"]),
            external_execution_performed=_optional_bool(
                row["external_execution_performed"]
            ),
            disposition=(
                WorkProductDisposition(str(disposition_raw))
                if disposition_raw is not None
                else None
            ),
            disposition_succeeded=_optional_bool(row["disposition_succeeded"]),
            worktree_present=_optional_bool(row["worktree_present"]),
            branch_present=_optional_bool(row["branch_present"]),
            canonical_unchanged=_optional_bool(row["canonical_unchanged"]),
            promotion_commit_sha=_optional_text(row["promotion_commit_sha"]),
            pull_request_number=(
                int(row["pull_request_number"])
                if row["pull_request_number"] is not None
                else None
            ),
            pull_request_url=_optional_text(row["pull_request_url"]),
            promotion_base_branch=_optional_text(row["promotion_base_branch"]),
            promotion_head_branch=_optional_text(row["promotion_head_branch"]),
            promotion_finished_at=_optional_datetime(row["promotion_finished_at"]),
            created_at=_optional_datetime(row["created_at"]),
            updated_at=_optional_datetime(row["updated_at"]),
        )
    except (KeyError, TypeError, ValueError):
        raise SoftwareEngineeringRunStateError(
            "software engineering durable state is invalid"
        ) from None


def _normalize_objective(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise SoftwareEngineeringRunStateError(
            "software engineering pending objective must not be blank"
        )
    return normalized


def _normalize_required_objective(value: str | None) -> str:
    normalized = _normalize_objective(value)
    if normalized is None:
        raise SoftwareEngineeringRunStateError(
            "software engineering pending objective is unavailable"
        )
    return normalized


def _require_compatible_objective(
    state: SoftwareEngineeringRunState,
    objective: str | None,
) -> None:
    if (
        objective is not None
        and state.pending_objective is not None
        and state.pending_objective != objective
    ):
        raise SoftwareEngineeringRunStateError(
            "software engineering pending objective conflicts with durable state"
        )


def _require_same_identity(
    state: SoftwareEngineeringRunState,
    target: str,
    executor_role: str,
    capability: str,
) -> None:
    if (
        state.target != target
        or state.executor_role != executor_role
        or state.capability != capability
    ):
        raise SoftwareEngineeringRunStateError(
            "software engineering Action identity conflicts with durable state"
        )


def _is_canonical_route(role: str | None, capability: str | None) -> bool:
    return (
        role == ExecutorRole.SOFTWARE_ENGINEERING.value
        and capability == SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY
    )


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, (bool, int)):
        return bool(value)
    raise SoftwareEngineeringRunStateError(
        "software engineering durable boolean state is invalid"
    )


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _require_aware(datetime.fromisoformat(str(value)))


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SoftwareEngineeringRunStateError(
            "software engineering state timestamp must be timezone-aware"
        )
    return value


def _utcnow() -> datetime:
    return datetime.now(UTC)
