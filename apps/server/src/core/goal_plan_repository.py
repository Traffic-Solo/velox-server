"""Durable, provider-neutral persistence for Goal and Plan snapshots.

Representation
--------------
``goals``
    One row per Goal. Objective, success criteria and constraints never change
    after creation; only ``status`` changes, through ``transition_goal``, and every
    effective change increments the row's storage ``revision``.
``goal_plans``
    One row per ``(goal_id, version)``. ``version`` is the logical Plan definition
    version (``Plan.version``); ``revision`` is the storage revision of that
    version's execution progress. A new logical version is a new row, earlier
    versions are never rewritten, and only the latest version accepts changes.
    A version with an ``in_progress`` step cannot be superseded, so no started
    step is ever frozen before it reaches a terminal status.
``goal_plan_steps``
    The immutable step definitions of one plan version in plan order
    (``position``) plus each step's mutable ``status``. Only ``status`` changes,
    through ``transition_step``, and every effective change increments the owning
    plan version's ``revision``; it never creates a new logical version.
``goal_plan_step_delegations``
    At most one durable handoff record per step of a plan version: the Action id
    the existing ``TaskDelegator`` created, who requested it, the explicit account
    context it was routed with and the delegation/approval outcome. It is
    evidence that work was handed off (or held, denied or rejected), never that
    work started, ran or succeeded; Action lifecycle stays in the Action
    lifecycle repository.

``goal_plan_step_executions``
    Append-only evidence of individual worker execution attempts for delegated
    Actions, keyed by the runtime's ``execution_id``: the observed start, and
    once observed the executor-reported status, completion time, whether the
    executor reported an external execution, failure category and reason. The
    association with Goal, Plan version and step is taken from the delegation,
    never from the caller. A completed attempt is immutable. Evidence never
    changes step or Goal status and is not proof that the step objective was met.

Goal status gates every mutation boundary: terminal Goals receive no new Plan
versions, and a step may start (directly, through delegation or through
approval) only while its Goal is ``active``. Already started steps can always be
finalized, whatever the Goal status, so no started work is stranded.

SQLite triggers reject edits to immutable columns, deletes and changes to
superseded versions, as defense in depth behind the repository checks. Schema
versioning uses the shared ``velox_schema`` table under its own ``goal_plans``
row, independent of every other namespace, so other repositories (such as the
Software Engineering run state) can share one database file in any
initialization order.

Holding or persisting a Goal or Plan authorizes nothing and executes nothing.
"""

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeGuard
from uuid import UUID

from apps.server.src.core import goal_lifecycle
from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.goal_lifecycle import GoalPlanTransitionError
from apps.server.src.core.goals import (
    Goal,
    GoalPlanContractError,
    GoalStatus,
    Plan,
    PlanStep,
    PlanStepStatus,
)

_SCHEMA_NAME = "goal_plans"
_SCHEMA_VERSION = 3

_SCHEMA_V1_STATEMENTS = (
    """
    CREATE TABLE goals (
        goal_id TEXT PRIMARY KEY,
        objective TEXT NOT NULL,
        success_criteria TEXT NOT NULL,
        constraints TEXT NOT NULL,
        status TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1)
    )
    """,
    """
    CREATE TABLE goal_plans (
        goal_id TEXT NOT NULL REFERENCES goals(goal_id),
        version INTEGER NOT NULL CHECK (version >= 1),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        PRIMARY KEY (goal_id, version)
    )
    """,
    """
    CREATE TABLE goal_plan_steps (
        goal_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        position INTEGER NOT NULL CHECK (position >= 0),
        step_id TEXT NOT NULL,
        objective TEXT NOT NULL,
        target TEXT NOT NULL,
        executor_role TEXT NOT NULL,
        capability TEXT NOT NULL,
        depends_on TEXT NOT NULL,
        status TEXT NOT NULL,
        PRIMARY KEY (goal_id, version, step_id),
        UNIQUE (goal_id, version, position),
        FOREIGN KEY (goal_id, version) REFERENCES goal_plans(goal_id, version)
    )
    """,
    """
    CREATE TRIGGER goals_definition_immutable
    BEFORE UPDATE OF goal_id, objective, success_criteria, constraints ON goals
    BEGIN SELECT RAISE(ABORT, 'goal definitions are immutable'); END
    """,
    """
    CREATE TRIGGER goals_retained
    BEFORE DELETE ON goals
    BEGIN SELECT RAISE(ABORT, 'goals are retained'); END
    """,
    """
    CREATE TRIGGER goal_plans_identity_immutable
    BEFORE UPDATE OF goal_id, version ON goal_plans
    BEGIN SELECT RAISE(ABORT, 'plan versions are immutable'); END
    """,
    """
    CREATE TRIGGER goal_plans_retained
    BEFORE DELETE ON goal_plans
    BEGIN SELECT RAISE(ABORT, 'plan versions are retained'); END
    """,
    """
    CREATE TRIGGER goal_plans_superseded_read_only
    BEFORE UPDATE OF revision ON goal_plans
    WHEN EXISTS (
        SELECT 1 FROM goal_plans AS newer
        WHERE newer.goal_id = OLD.goal_id AND newer.version > OLD.version
    )
    BEGIN SELECT RAISE(ABORT, 'superseded plan versions are read-only'); END
    """,
    """
    CREATE TRIGGER goal_plan_steps_definition_immutable
    BEFORE UPDATE OF
        goal_id, version, position, step_id, objective, target,
        executor_role, capability, depends_on
    ON goal_plan_steps
    BEGIN SELECT RAISE(ABORT, 'plan step definitions are immutable'); END
    """,
    """
    CREATE TRIGGER goal_plan_steps_retained
    BEFORE DELETE ON goal_plan_steps
    BEGIN SELECT RAISE(ABORT, 'plan steps are retained'); END
    """,
    """
    CREATE TRIGGER goal_plan_steps_superseded_read_only
    BEFORE UPDATE OF status ON goal_plan_steps
    WHEN EXISTS (
        SELECT 1 FROM goal_plans AS newer
        WHERE newer.goal_id = OLD.goal_id AND newer.version > OLD.version
    )
    BEGIN SELECT RAISE(ABORT, 'superseded plan versions are read-only'); END
    """,
)

_SCHEMA_V1_OBJECTS = frozenset(
    {
        ("table", "goals"),
        ("table", "goal_plans"),
        ("table", "goal_plan_steps"),
        ("trigger", "goals_definition_immutable"),
        ("trigger", "goals_retained"),
        ("trigger", "goal_plans_identity_immutable"),
        ("trigger", "goal_plans_retained"),
        ("trigger", "goal_plans_superseded_read_only"),
        ("trigger", "goal_plan_steps_definition_immutable"),
        ("trigger", "goal_plan_steps_retained"),
        ("trigger", "goal_plan_steps_superseded_read_only"),
    },
)


_SCHEMA_V2_STATEMENTS = (
    """
    CREATE TABLE goal_plan_step_delegations (
        goal_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        step_id TEXT NOT NULL,
        action_id TEXT NOT NULL UNIQUE,
        requested_by TEXT NOT NULL,
        account_principal TEXT,
        account_identifier TEXT,
        status TEXT NOT NULL,
        permission_status TEXT,
        routing_reason TEXT,
        PRIMARY KEY (goal_id, version, step_id),
        FOREIGN KEY (goal_id, version, step_id)
            REFERENCES goal_plan_steps(goal_id, version, step_id)
    )
    """,
    """
    CREATE TRIGGER goal_plan_step_delegations_definition_immutable
    BEFORE UPDATE OF
        goal_id, version, step_id, action_id, requested_by, account_principal,
        account_identifier, permission_status, routing_reason
    ON goal_plan_step_delegations
    BEGIN SELECT RAISE(ABORT, 'step delegations are immutable'); END
    """,
    """
    CREATE TRIGGER goal_plan_step_delegations_retained
    BEFORE DELETE ON goal_plan_step_delegations
    BEGIN SELECT RAISE(ABORT, 'step delegations are retained'); END
    """,
)

_SCHEMA_V2_OBJECTS = _SCHEMA_V1_OBJECTS | frozenset(
    {
        ("table", "goal_plan_step_delegations"),
        ("trigger", "goal_plan_step_delegations_definition_immutable"),
        ("trigger", "goal_plan_step_delegations_retained"),
    },
)

_SCHEMA_V3_STATEMENTS = (
    """
    CREATE TABLE goal_plan_step_executions (
        execution_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES goal_plan_step_delegations(action_id),
        goal_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        step_id TEXT NOT NULL,
        executor_role TEXT NOT NULL,
        capability TEXT NOT NULL,
        started_at TEXT NOT NULL,
        status TEXT NOT NULL,
        finished_at TEXT,
        external_execution_reported INTEGER,
        failure_category TEXT,
        reason TEXT,
        FOREIGN KEY (goal_id, version, step_id)
            REFERENCES goal_plan_step_delegations(goal_id, version, step_id)
    )
    """,
    """
    CREATE TRIGGER goal_plan_step_executions_identity_immutable
    BEFORE UPDATE OF
        execution_id, action_id, goal_id, version, step_id, executor_role,
        capability, started_at
    ON goal_plan_step_executions
    BEGIN SELECT RAISE(ABORT, 'execution identity is immutable'); END
    """,
    """
    CREATE TRIGGER goal_plan_step_executions_completed_immutable
    BEFORE UPDATE ON goal_plan_step_executions
    WHEN OLD.status <> 'started'
    BEGIN SELECT RAISE(ABORT, 'completed execution evidence is immutable'); END
    """,
    """
    CREATE TRIGGER goal_plan_step_executions_retained
    BEFORE DELETE ON goal_plan_step_executions
    BEGIN SELECT RAISE(ABORT, 'execution evidence is retained'); END
    """,
)

_SCHEMA_V3_OBJECTS = _SCHEMA_V2_OBJECTS | frozenset(
    {
        ("table", "goal_plan_step_executions"),
        ("trigger", "goal_plan_step_executions_identity_immutable"),
        ("trigger", "goal_plan_step_executions_completed_immutable"),
        ("trigger", "goal_plan_step_executions_retained"),
    },
)

_MAX_REASON_LENGTH = 1000


class GoalPlanRepositoryError(RuntimeError):
    """Durable Goal/Plan state is unavailable or could not be written."""


class GoalPlanNotFoundError(GoalPlanRepositoryError):
    """The requested Goal, Plan version or PlanStep does not exist."""


class GoalPlanConflictError(GoalPlanRepositoryError):
    """A write lost a race: duplicate identity, taken version or stale revision."""


class GoalPlanCorruptedError(GoalPlanRepositoryError):
    """Persisted Goal/Plan state is invalid and was not reconstructed."""


@dataclass(frozen=True, slots=True)
class StoredGoal:
    """A persisted Goal snapshot and the storage revision it was read at."""

    goal: Goal
    revision: int


@dataclass(frozen=True, slots=True)
class StoredPlan:
    """A persisted Plan version and the storage revision of its step progress."""

    plan: Plan
    revision: int


class StepDelegationStatus(StrEnum):
    """Durable handoff outcome of one step; none of these means work ran.

    The first four mirror ``TaskDelegationStatus``. ``approved`` and
    ``approval_rejected`` record the later explicit decision on an
    ``awaiting_approval`` handoff.
    """

    QUEUED = "queued"
    AWAITING_APPROVAL = "awaiting_approval"
    DENIED = "denied"
    ROUTE_REJECTED = "route_rejected"
    APPROVED = "approved"
    APPROVAL_REJECTED = "approval_rejected"


_HANDOFF_STATUSES = frozenset(
    {
        StepDelegationStatus.QUEUED,
        StepDelegationStatus.AWAITING_APPROVAL,
        StepDelegationStatus.DENIED,
        StepDelegationStatus.ROUTE_REJECTED,
    },
)


@dataclass(frozen=True, slots=True)
class StepHandoff:
    """What the delegation callback reports back from the existing TaskDelegator."""

    action_id: UUID
    status: StepDelegationStatus
    permission_status: str | None = None
    routing_reason: str | None = None

    def __post_init__(self) -> None:
        _require_uuid_argument(self.action_id, "handoff action id")
        if self.status not in _HANDOFF_STATUSES:
            raise GoalPlanContractError("step handoff requires a delegation outcome")
        for value, name in (
            (self.permission_status, "permission status"),
            (self.routing_reason, "routing reason"),
        ):
            if value is not None and not isinstance(value, str):
                raise GoalPlanContractError(f"step handoff {name} must be text")


@dataclass(frozen=True, slots=True)
class StepDelegation:
    """Durable evidence that one plan step was handed to the TaskDelegator."""

    goal_id: UUID
    version: int
    step_id: UUID
    action_id: UUID
    requested_by: str
    account_principal: str | None
    account_identifier: str | None
    status: StepDelegationStatus
    permission_status: str | None
    routing_reason: str | None


class GoalPlanDelegationExistsError(GoalPlanConflictError):
    """The step already has a durable delegation; it is never handed off twice."""

    def __init__(self, delegation: StepDelegation) -> None:
        super().__init__(
            f"plan step {delegation.step_id} was already delegated as action "
            f"{delegation.action_id}",
        )
        self.delegation = delegation


StepHandoffCallback = Callable[[PlanStep], StepHandoff]


class StepExecutionStatus(StrEnum):
    """Executor-reported status of one attempt; ``started`` until finish is observed."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


_EXECUTABLE_DELEGATIONS = frozenset(
    {StepDelegationStatus.QUEUED, StepDelegationStatus.APPROVED},
)


@dataclass(frozen=True, slots=True)
class StepExecution:
    """Durable evidence of one worker execution attempt of a delegated step Action.

    ``succeeded`` is what the executor reported, not proof that the step
    objective was met; ``external_execution_reported`` is the executor's claim,
    and ``False`` is not proof that no side effect happened.
    """

    execution_id: UUID
    action_id: UUID
    goal_id: UUID
    version: int
    step_id: UUID
    executor_role: ExecutorRole
    capability: str
    started_at: datetime
    status: StepExecutionStatus = StepExecutionStatus.STARTED
    finished_at: datetime | None = None
    external_execution_reported: bool | None = None
    failure_category: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.execution_id, "execution id"),
            (self.action_id, "action id"),
            (self.goal_id, "goal id"),
            (self.step_id, "plan step id"),
        ):
            _require_uuid_argument(value, name)
        _require_version_argument(self.version)
        if not isinstance(self.executor_role, ExecutorRole):
            raise GoalPlanContractError("execution requires a known executor role")
        if not isinstance(self.capability, str) or not self.capability.strip():
            raise GoalPlanContractError("execution requires a capability")
        if not isinstance(self.status, StepExecutionStatus):
            raise GoalPlanContractError("execution requires a known status")
        _require_aware(self.started_at, "execution start")
        started = self.status is StepExecutionStatus.STARTED
        if started:
            if (
                self.finished_at is not None
                or self.external_execution_reported is not None
                or self.failure_category is not None
                or self.reason is not None
            ):
                raise GoalPlanContractError("a started execution has no completion facts")
            return
        if self.finished_at is None:
            raise GoalPlanContractError("a completed execution requires its finish time")
        _require_aware(self.finished_at, "execution finish")
        if not isinstance(self.external_execution_reported, bool):
            raise GoalPlanContractError("a completed execution requires its external report")
        if self.failure_category is not None and (
            self.status is not StepExecutionStatus.FAILED
            or not isinstance(self.failure_category, str)
            or not self.failure_category.strip()
        ):
            raise GoalPlanContractError("only a failed execution carries a failure category")
        if self.reason is not None and (
            not isinstance(self.reason, str) or len(self.reason) > _MAX_REASON_LENGTH
        ):
            raise GoalPlanContractError("execution reason must be bounded text")


class GoalPlanRepository(Protocol):
    """Persistence Role for Goals and versioned Plans.

    New Goals must be ``draft`` and new Plan versions must have every step
    ``pending``; statuses change only through the transition operations, which
    apply ``goal_lifecycle`` policy and require the caller's expected revision.
    There is no delete and no arbitrary status replacement. Steps start only while
    their Goal is ``active``; terminal Goals receive no new Plan versions.
    """

    def create_goal(self, goal: Goal) -> StoredGoal:
        ...

    def get_goal(self, goal_id: UUID) -> StoredGoal | None:
        ...

    def transition_goal(
        self,
        goal_id: UUID,
        status: GoalStatus,
        *,
        expected_revision: int,
    ) -> StoredGoal:
        ...

    def create_plan(self, plan: Plan) -> StoredPlan:
        ...

    def get_plan(self, goal_id: UUID, version: int) -> StoredPlan | None:
        ...

    def get_latest_plan(self, goal_id: UUID) -> StoredPlan | None:
        ...

    def transition_step(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
        status: PlanStepStatus,
        *,
        expected_revision: int,
    ) -> StoredPlan:
        ...

    def get_step_delegation(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
    ) -> StepDelegation | None:
        ...

    def get_step_delegation_for_action(self, action_id: UUID) -> StepDelegation | None:
        ...

    def list_step_delegations(self) -> tuple[StepDelegation, ...]:
        """Every durable step delegation, in creation order (read-only)."""
        ...

    def record_execution_start(
        self,
        *,
        execution_id: UUID,
        action_id: UUID,
        executor_role: ExecutorRole,
        capability: str,
        started_at: datetime,
    ) -> StepExecution:
        """Record an observed attempt start for a ``queued``/``approved`` delegation.

        The step association comes from the delegation; the route must equal the
        stored step's Role and capability. Replaying the same start is idempotent.
        """
        ...

    def record_execution_finish(
        self,
        *,
        execution_id: UUID,
        action_id: UUID,
        status: StepExecutionStatus,
        finished_at: datetime,
        external_execution_reported: bool,
        failure_category: str | None,
        reason: str | None,
    ) -> StepExecution:
        """Complete a started attempt once; an identical replay is idempotent."""
        ...

    def list_step_executions(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
    ) -> tuple[StepExecution, ...]:
        """Every recorded attempt of one step, in recording order (read-only)."""
        ...

    def delegate_step(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
        *,
        expected_revision: int,
        requested_by: str,
        account_principal: str | None,
        account_identifier: str | None,
        handoff: StepHandoffCallback,
    ) -> tuple[StoredPlan, StepDelegation]:
        """Validate, hand off once and record the outcome in one atomic unit.

        ``handoff`` runs only after every check passed and while the lock or write
        transaction is held, so concurrent requests can never hand a step off
        twice. If it raises, nothing is recorded. A ``queued`` outcome starts the
        step; every outcome increments the plan revision.
        """
        ...

    def resolve_step_approval(
        self,
        action_id: UUID,
        *,
        approved: bool,
    ) -> tuple[StoredPlan, StepDelegation]:
        """Record the explicit decision on an ``awaiting_approval`` delegation.

        Approval re-checks that the Goal is active, the version is latest and the
        step is still pending with succeeded dependencies, then starts the step.
        Rejection only records the decision.
        """
        ...


class InMemoryGoalPlanRepository:
    """Process-local implementation with the same rules as the SQLite repository."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._goals: dict[UUID, StoredGoal] = {}
        self._plans: dict[UUID, list[StoredPlan]] = {}
        self._delegations: dict[tuple[UUID, int, UUID], StepDelegation] = {}
        self._delegation_keys: dict[UUID, tuple[UUID, int, UUID]] = {}
        self._executions: dict[UUID, StepExecution] = {}

    def create_goal(self, goal: Goal) -> StoredGoal:
        goal = _require_new_goal(goal)
        with self._lock:
            if goal.id in self._goals:
                raise GoalPlanConflictError(f"goal {goal.id} already exists")
            stored = StoredGoal(goal=goal, revision=1)
            self._goals[goal.id] = stored
            return stored

    def get_goal(self, goal_id: UUID) -> StoredGoal | None:
        _require_uuid_argument(goal_id, "goal id")
        with self._lock:
            return self._goals.get(goal_id)

    def transition_goal(
        self,
        goal_id: UUID,
        status: GoalStatus,
        *,
        expected_revision: int,
    ) -> StoredGoal:
        _require_uuid_argument(goal_id, "goal id")
        expected = _require_expected_revision(expected_revision)
        with self._lock:
            stored = self._goals.get(goal_id)
            if stored is None:
                raise GoalPlanNotFoundError(f"goal {goal_id} does not exist")
            _require_current_revision(stored.revision, expected, "goal")
            goal = goal_lifecycle.transition_goal(stored.goal, status)
            if goal is stored.goal:
                return stored
            updated = StoredGoal(goal=goal, revision=stored.revision + 1)
            self._goals[goal_id] = updated
            return updated

    def create_plan(self, plan: Plan) -> StoredPlan:
        plan = _require_new_plan(plan)
        with self._lock:
            goal = self._goals.get(plan.goal_id)
            if goal is None:
                raise GoalPlanNotFoundError(f"goal {plan.goal_id} does not exist")
            _require_goal_accepts_plans(goal.goal)
            versions = self._plans.setdefault(plan.goal_id, [])
            _require_next_version(plan, len(versions) or None)
            if versions:
                _require_supersedable(versions[-1].plan)
            stored = StoredPlan(plan=plan, revision=1)
            versions.append(stored)
            return stored

    def get_plan(self, goal_id: UUID, version: int) -> StoredPlan | None:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        with self._lock:
            versions = self._plans.get(goal_id, [])
            return versions[version - 1] if version <= len(versions) else None

    def get_latest_plan(self, goal_id: UUID) -> StoredPlan | None:
        _require_uuid_argument(goal_id, "goal id")
        with self._lock:
            versions = self._plans.get(goal_id)
            return versions[-1] if versions else None

    def transition_step(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
        status: PlanStepStatus,
        *,
        expected_revision: int,
    ) -> StoredPlan:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        _require_uuid_argument(step_id, "plan step id")
        expected = _require_expected_revision(expected_revision)
        with self._lock:
            versions = self._plans.get(goal_id, [])
            if version > len(versions):
                raise GoalPlanNotFoundError(
                    f"plan version {version} of goal {goal_id} does not exist",
                )
            stored = versions[version - 1]
            _require_latest_version(version, len(versions))
            _require_current_revision(stored.revision, expected, "plan")
            _require_step(stored.plan, step_id)
            plan = goal_lifecycle.transition_plan_step(stored.plan, step_id, status)
            if plan is stored.plan:
                return stored
            if status is PlanStepStatus.IN_PROGRESS:
                _require_goal_allows_start(self._goals[goal_id].goal)
            updated = StoredPlan(plan=plan, revision=stored.revision + 1)
            versions[version - 1] = updated
            return updated

    def get_step_delegation(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
    ) -> StepDelegation | None:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        _require_uuid_argument(step_id, "plan step id")
        with self._lock:
            return self._delegations.get((goal_id, version, step_id))

    def get_step_delegation_for_action(self, action_id: UUID) -> StepDelegation | None:
        _require_uuid_argument(action_id, "action id")
        with self._lock:
            key = self._delegation_keys.get(action_id)
            return self._delegations[key] if key is not None else None

    def list_step_delegations(self) -> tuple[StepDelegation, ...]:
        with self._lock:
            return tuple(self._delegations.values())

    def record_execution_start(
        self,
        *,
        execution_id: UUID,
        action_id: UUID,
        executor_role: ExecutorRole,
        capability: str,
        started_at: datetime,
    ) -> StepExecution:
        _require_uuid_argument(action_id, "action id")
        with self._lock:
            key = self._delegation_keys.get(action_id)
            if key is None:
                raise GoalPlanNotFoundError(f"no plan step delegation for action {action_id}")
            delegation = self._delegations[key]
            stored = self._plans[delegation.goal_id][delegation.version - 1]
            candidate = _started_execution(
                delegation,
                stored.plan,
                execution_id=execution_id,
                executor_role=executor_role,
                capability=capability,
                started_at=started_at,
            )
            existing = self._executions.get(execution_id)
            if existing is not None:
                return _require_same_start(existing, candidate)
            self._executions[execution_id] = candidate
            return candidate

    def record_execution_finish(
        self,
        *,
        execution_id: UUID,
        action_id: UUID,
        status: StepExecutionStatus,
        finished_at: datetime,
        external_execution_reported: bool,
        failure_category: str | None,
        reason: str | None,
    ) -> StepExecution:
        _require_uuid_argument(execution_id, "execution id")
        with self._lock:
            existing = self._executions.get(execution_id)
            if existing is None:
                raise GoalPlanNotFoundError(f"execution {execution_id} was never started")
            completed = _completed_execution(
                existing,
                action_id=action_id,
                status=status,
                finished_at=finished_at,
                external_execution_reported=external_execution_reported,
                failure_category=failure_category,
                reason=reason,
            )
            if completed is not existing:
                self._executions[execution_id] = completed
            return completed

    def list_step_executions(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
    ) -> tuple[StepExecution, ...]:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        _require_uuid_argument(step_id, "plan step id")
        with self._lock:
            return tuple(
                execution
                for execution in self._executions.values()
                if (execution.goal_id, execution.version, execution.step_id)
                == (goal_id, version, step_id)
            )

    def delegate_step(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
        *,
        expected_revision: int,
        requested_by: str,
        account_principal: str | None,
        account_identifier: str | None,
        handoff: StepHandoffCallback,
    ) -> tuple[StoredPlan, StepDelegation]:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        _require_uuid_argument(step_id, "plan step id")
        expected = _require_expected_revision(expected_revision)
        _require_requester(requested_by)
        _require_account(account_principal, account_identifier)
        with self._lock:
            versions = self._plans.get(goal_id, [])
            if version > len(versions):
                raise GoalPlanNotFoundError(
                    f"plan version {version} of goal {goal_id} does not exist",
                )
            key = (goal_id, version, step_id)
            existing = self._delegations.get(key)
            if existing is not None:
                raise GoalPlanDelegationExistsError(existing)
            stored = versions[version - 1]
            _require_latest_version(version, len(versions))
            _require_current_revision(stored.revision, expected, "plan")
            started = _require_startable(stored.plan, self._goals[goal_id].goal, step_id)
            result = _require_handoff(handoff(stored.plan.step(step_id)))
            if result.action_id in self._delegation_keys:
                raise GoalPlanConflictError(
                    f"action {result.action_id} is already linked to a plan step",
                )
            delegation = _delegation_from_handoff(
                goal_id,
                version,
                step_id,
                requested_by,
                account_principal,
                account_identifier,
                result,
            )
            plan = started if result.status is StepDelegationStatus.QUEUED else stored.plan
            updated = StoredPlan(plan=plan, revision=stored.revision + 1)
            versions[version - 1] = updated
            self._delegations[key] = delegation
            self._delegation_keys[result.action_id] = key
            return updated, delegation

    def resolve_step_approval(
        self,
        action_id: UUID,
        *,
        approved: bool,
    ) -> tuple[StoredPlan, StepDelegation]:
        _require_uuid_argument(action_id, "action id")
        with self._lock:
            key = self._delegation_keys.get(action_id)
            if key is None:
                raise GoalPlanNotFoundError(f"no plan step delegation for action {action_id}")
            delegation = self._delegations[key]
            _require_awaiting_approval(delegation)
            goal_id, version, step_id = key
            versions = self._plans[goal_id]
            stored = versions[version - 1]
            if not approved:
                rejected = replace(delegation, status=StepDelegationStatus.APPROVAL_REJECTED)
                self._delegations[key] = rejected
                return stored, rejected
            _require_latest_version(version, len(versions))
            started = _require_startable(stored.plan, self._goals[goal_id].goal, step_id)
            updated = StoredPlan(plan=started, revision=stored.revision + 1)
            approved_delegation = replace(delegation, status=StepDelegationStatus.APPROVED)
            versions[version - 1] = updated
            self._delegations[key] = approved_delegation
            return updated, approved_delegation


class SqliteGoalPlanRepository:
    """SQLite implementation: one short explicit transaction per operation.

    Writes run under ``BEGIN IMMEDIATE`` so a read-check-write sequence holds the
    database write lock throughout; reads run in one snapshot transaction so a
    Plan and its steps are always reconstructed from the same committed state.
    Any failure rolls the whole transaction back.
    """

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise GoalPlanRepositoryError("goal/plan state database path must be absolute")
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise GoalPlanRepositoryError(
                "goal/plan state database is unavailable",
            ) from None
        with self._transaction(
            write=True,
            failure="goal/plan state schema could not be initialized",
        ) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS velox_schema (
                    name TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """,
            )
            row = connection.execute(
                "SELECT version FROM velox_schema WHERE name = ?",
                (_SCHEMA_NAME,),
            ).fetchone()
            version = row["version"] if row is not None else None
            if row is None:
                for statement in (
                    *_SCHEMA_V1_STATEMENTS,
                    *_SCHEMA_V2_STATEMENTS,
                    *_SCHEMA_V3_STATEMENTS,
                ):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO velox_schema(name, version) VALUES (?, ?)",
                    (_SCHEMA_NAME, _SCHEMA_VERSION),
                )
            elif _is_int(version) and version in (1, 2):
                _require_schema_objects(
                    connection,
                    _SCHEMA_V1_OBJECTS if version == 1 else _SCHEMA_V2_OBJECTS,
                )
                upgrades = _SCHEMA_V2_STATEMENTS if version == 1 else ()
                for statement in (*upgrades, *_SCHEMA_V3_STATEMENTS):
                    connection.execute(statement)
                connection.execute(
                    "UPDATE velox_schema SET version = ? WHERE name = ? AND version = ?",
                    (_SCHEMA_VERSION, _SCHEMA_NAME, version),
                )
            elif _is_int(version) and version == _SCHEMA_VERSION:
                _require_schema_objects(connection, _SCHEMA_V3_OBJECTS)
            else:
                raise GoalPlanRepositoryError("unsupported goal/plan state schema version")

    @property
    def path(self) -> Path:
        return self._path

    def create_goal(self, goal: Goal) -> StoredGoal:
        goal = _require_new_goal(goal)
        with self._transaction(write=True, failure="goal could not be persisted") as connection:
            if _goal_exists(connection, goal.id):
                raise GoalPlanConflictError(f"goal {goal.id} already exists")
            connection.execute(
                """
                INSERT INTO goals (
                    goal_id, objective, success_criteria, constraints, status, revision
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    str(goal.id),
                    goal.objective,
                    _encode_texts(goal.success_criteria),
                    _encode_texts(goal.constraints),
                    goal.status.value,
                ),
            )
        return StoredGoal(goal=goal, revision=1)

    def get_goal(self, goal_id: UUID) -> StoredGoal | None:
        _require_uuid_argument(goal_id, "goal id")
        with self._transaction(write=False, failure="goal could not be read") as connection:
            row = connection.execute(
                "SELECT * FROM goals WHERE goal_id = ?",
                (str(goal_id),),
            ).fetchone()
        return _stored_goal_from_row(row) if row is not None else None

    def transition_goal(
        self,
        goal_id: UUID,
        status: GoalStatus,
        *,
        expected_revision: int,
    ) -> StoredGoal:
        _require_uuid_argument(goal_id, "goal id")
        expected = _require_expected_revision(expected_revision)
        with self._transaction(
            write=True,
            failure="goal transition could not be persisted",
        ) as connection:
            row = connection.execute(
                "SELECT * FROM goals WHERE goal_id = ?",
                (str(goal_id),),
            ).fetchone()
            if row is None:
                raise GoalPlanNotFoundError(f"goal {goal_id} does not exist")
            stored = _stored_goal_from_row(row)
            _require_current_revision(stored.revision, expected, "goal")
            goal = goal_lifecycle.transition_goal(stored.goal, status)
            if goal is stored.goal:
                return stored
            cursor = connection.execute(
                """
                UPDATE goals SET status = ?, revision = revision + 1
                WHERE goal_id = ? AND revision = ?
                """,
                (goal.status.value, str(goal_id), stored.revision),
            )
            if cursor.rowcount != 1:
                raise GoalPlanConflictError(f"goal {goal_id} changed concurrently")
        return StoredGoal(goal=goal, revision=stored.revision + 1)

    def create_plan(self, plan: Plan) -> StoredPlan:
        plan = _require_new_plan(plan)
        with self._transaction(write=True, failure="plan could not be persisted") as connection:
            goal = _load_goal(connection, plan.goal_id)
            if goal is None:
                raise GoalPlanNotFoundError(f"goal {plan.goal_id} does not exist")
            _require_goal_accepts_plans(goal.goal)
            latest = _latest_version(connection, plan.goal_id)
            _require_next_version(plan, latest)
            if latest is not None:
                current = _load_plan(connection, plan.goal_id, latest)
                if current is None:
                    raise GoalPlanCorruptedError("persisted plan is invalid")
                _require_supersedable(current.plan)
            connection.execute(
                "INSERT INTO goal_plans (goal_id, version, revision) VALUES (?, ?, 1)",
                (str(plan.goal_id), plan.version),
            )
            connection.executemany(
                """
                INSERT INTO goal_plan_steps (
                    goal_id, version, position, step_id, objective, target,
                    executor_role, capability, depends_on, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(plan.goal_id),
                        plan.version,
                        position,
                        str(step.id),
                        step.objective,
                        step.target,
                        step.executor_role.value,
                        step.capability,
                        _encode_texts([str(dependency) for dependency in step.depends_on]),
                        step.status.value,
                    )
                    for position, step in enumerate(plan.steps)
                ],
            )
        return StoredPlan(plan=plan, revision=1)

    def get_plan(self, goal_id: UUID, version: int) -> StoredPlan | None:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        with self._transaction(write=False, failure="plan could not be read") as connection:
            return _load_plan(connection, goal_id, version)

    def get_latest_plan(self, goal_id: UUID) -> StoredPlan | None:
        _require_uuid_argument(goal_id, "goal id")
        with self._transaction(write=False, failure="plan could not be read") as connection:
            latest = _latest_version(connection, goal_id)
            return _load_plan(connection, goal_id, latest) if latest is not None else None

    def transition_step(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
        status: PlanStepStatus,
        *,
        expected_revision: int,
    ) -> StoredPlan:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        _require_uuid_argument(step_id, "plan step id")
        expected = _require_expected_revision(expected_revision)
        with self._transaction(
            write=True,
            failure="plan step transition could not be persisted",
        ) as connection:
            stored = _load_plan(connection, goal_id, version)
            if stored is None:
                raise GoalPlanNotFoundError(
                    f"plan version {version} of goal {goal_id} does not exist",
                )
            latest = _latest_version(connection, goal_id)
            _require_latest_version(version, latest if latest is not None else version)
            _require_current_revision(stored.revision, expected, "plan")
            _require_step(stored.plan, step_id)
            plan = goal_lifecycle.transition_plan_step(stored.plan, step_id, status)
            if plan is stored.plan:
                return stored
            if status is PlanStepStatus.IN_PROGRESS:
                _require_goal_allows_start(_require_goal(connection, goal_id))
            return _write_progress(connection, stored, plan, step_id)

    def get_step_delegation(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
    ) -> StepDelegation | None:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        _require_uuid_argument(step_id, "plan step id")
        with self._transaction(
            write=False,
            failure="step delegation could not be read",
        ) as connection:
            return _load_delegation(connection, goal_id, version, step_id)

    def get_step_delegation_for_action(self, action_id: UUID) -> StepDelegation | None:
        _require_uuid_argument(action_id, "action id")
        with self._transaction(
            write=False,
            failure="step delegation could not be read",
        ) as connection:
            row = connection.execute(
                "SELECT * FROM goal_plan_step_delegations WHERE action_id = ?",
                (str(action_id),),
            ).fetchone()
        return _delegation_from_row(row) if row is not None else None

    def list_step_delegations(self) -> tuple[StepDelegation, ...]:
        with self._transaction(
            write=False,
            failure="step delegations could not be read",
        ) as connection:
            rows = connection.execute(
                "SELECT * FROM goal_plan_step_delegations ORDER BY rowid",
            ).fetchall()
        return tuple(_delegation_from_row(row) for row in rows)

    def record_execution_start(
        self,
        *,
        execution_id: UUID,
        action_id: UUID,
        executor_role: ExecutorRole,
        capability: str,
        started_at: datetime,
    ) -> StepExecution:
        _require_uuid_argument(action_id, "action id")
        with self._transaction(
            write=True,
            failure="execution start could not be persisted",
        ) as connection:
            row = connection.execute(
                "SELECT * FROM goal_plan_step_delegations WHERE action_id = ?",
                (str(action_id),),
            ).fetchone()
            if row is None:
                raise GoalPlanNotFoundError(f"no plan step delegation for action {action_id}")
            delegation = _delegation_from_row(row)
            stored = _load_plan(connection, delegation.goal_id, delegation.version)
            if stored is None:
                raise GoalPlanCorruptedError("persisted step delegation is invalid")
            candidate = _started_execution(
                delegation,
                stored.plan,
                execution_id=execution_id,
                executor_role=executor_role,
                capability=capability,
                started_at=started_at,
            )
            existing = _load_execution(connection, execution_id)
            if existing is not None:
                return _require_same_start(existing, candidate)
            connection.execute(
                """
                INSERT INTO goal_plan_step_executions (
                    execution_id, action_id, goal_id, version, step_id,
                    executor_role, capability, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(candidate.execution_id),
                    str(candidate.action_id),
                    str(candidate.goal_id),
                    candidate.version,
                    str(candidate.step_id),
                    candidate.executor_role.value,
                    candidate.capability,
                    candidate.started_at.isoformat(),
                    candidate.status.value,
                ),
            )
        return candidate

    def record_execution_finish(
        self,
        *,
        execution_id: UUID,
        action_id: UUID,
        status: StepExecutionStatus,
        finished_at: datetime,
        external_execution_reported: bool,
        failure_category: str | None,
        reason: str | None,
    ) -> StepExecution:
        _require_uuid_argument(execution_id, "execution id")
        with self._transaction(
            write=True,
            failure="execution finish could not be persisted",
        ) as connection:
            existing = _load_execution(connection, execution_id)
            if existing is None:
                raise GoalPlanNotFoundError(f"execution {execution_id} was never started")
            completed = _completed_execution(
                existing,
                action_id=action_id,
                status=status,
                finished_at=finished_at,
                external_execution_reported=external_execution_reported,
                failure_category=failure_category,
                reason=reason,
            )
            if completed is existing:
                return existing
            assert completed.finished_at is not None
            cursor = connection.execute(
                """
                UPDATE goal_plan_step_executions
                SET status = ?, finished_at = ?, external_execution_reported = ?,
                    failure_category = ?, reason = ?
                WHERE execution_id = ? AND status = ?
                """,
                (
                    completed.status.value,
                    completed.finished_at.isoformat(),
                    int(bool(completed.external_execution_reported)),
                    completed.failure_category,
                    completed.reason,
                    str(execution_id),
                    StepExecutionStatus.STARTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise GoalPlanConflictError(f"execution {execution_id} changed concurrently")
        return completed

    def list_step_executions(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
    ) -> tuple[StepExecution, ...]:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        _require_uuid_argument(step_id, "plan step id")
        with self._transaction(
            write=False,
            failure="execution evidence could not be read",
        ) as connection:
            rows = connection.execute(
                """
                SELECT * FROM goal_plan_step_executions
                WHERE goal_id = ? AND version = ? AND step_id = ?
                ORDER BY rowid
                """,
                (str(goal_id), version, str(step_id)),
            ).fetchall()
        return tuple(_execution_from_row(row) for row in rows)

    def delegate_step(
        self,
        goal_id: UUID,
        version: int,
        step_id: UUID,
        *,
        expected_revision: int,
        requested_by: str,
        account_principal: str | None,
        account_identifier: str | None,
        handoff: StepHandoffCallback,
    ) -> tuple[StoredPlan, StepDelegation]:
        _require_uuid_argument(goal_id, "goal id")
        _require_version_argument(version)
        _require_uuid_argument(step_id, "plan step id")
        expected = _require_expected_revision(expected_revision)
        _require_requester(requested_by)
        _require_account(account_principal, account_identifier)
        with self._transaction(
            write=True,
            failure="plan step delegation could not be persisted",
        ) as connection:
            stored = _load_plan(connection, goal_id, version)
            if stored is None:
                raise GoalPlanNotFoundError(
                    f"plan version {version} of goal {goal_id} does not exist",
                )
            existing = _load_delegation(connection, goal_id, version, step_id)
            if existing is not None:
                raise GoalPlanDelegationExistsError(existing)
            latest = _latest_version(connection, goal_id)
            _require_latest_version(version, latest if latest is not None else version)
            _require_current_revision(stored.revision, expected, "plan")
            started = _require_startable(
                stored.plan,
                _require_goal(connection, goal_id),
                step_id,
            )
            result = _require_handoff(handoff(stored.plan.step(step_id)))
            if connection.execute(
                "SELECT 1 FROM goal_plan_step_delegations WHERE action_id = ?",
                (str(result.action_id),),
            ).fetchone() is not None:
                raise GoalPlanConflictError(
                    f"action {result.action_id} is already linked to a plan step",
                )
            delegation = _delegation_from_handoff(
                goal_id,
                version,
                step_id,
                requested_by,
                account_principal,
                account_identifier,
                result,
            )
            connection.execute(
                """
                INSERT INTO goal_plan_step_delegations (
                    goal_id, version, step_id, action_id, requested_by,
                    account_principal, account_identifier, status,
                    permission_status, routing_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(goal_id),
                    version,
                    str(step_id),
                    str(delegation.action_id),
                    delegation.requested_by,
                    delegation.account_principal,
                    delegation.account_identifier,
                    delegation.status.value,
                    delegation.permission_status,
                    delegation.routing_reason,
                ),
            )
            plan = started if result.status is StepDelegationStatus.QUEUED else stored.plan
            updated = _write_progress(connection, stored, plan, step_id)
        return updated, delegation

    def resolve_step_approval(
        self,
        action_id: UUID,
        *,
        approved: bool,
    ) -> tuple[StoredPlan, StepDelegation]:
        _require_uuid_argument(action_id, "action id")
        with self._transaction(
            write=True,
            failure="step approval could not be persisted",
        ) as connection:
            row = connection.execute(
                "SELECT * FROM goal_plan_step_delegations WHERE action_id = ?",
                (str(action_id),),
            ).fetchone()
            if row is None:
                raise GoalPlanNotFoundError(f"no plan step delegation for action {action_id}")
            delegation = _delegation_from_row(row)
            _require_awaiting_approval(delegation)
            stored = _load_plan(connection, delegation.goal_id, delegation.version)
            if stored is None:
                raise GoalPlanCorruptedError("persisted step delegation is invalid")
            status = (
                StepDelegationStatus.APPROVED
                if approved
                else StepDelegationStatus.APPROVAL_REJECTED
            )
            if approved:
                latest = _latest_version(connection, delegation.goal_id)
                _require_latest_version(
                    delegation.version,
                    latest if latest is not None else delegation.version,
                )
                started = _require_startable(
                    stored.plan,
                    _require_goal(connection, delegation.goal_id),
                    delegation.step_id,
                )
                stored = _write_progress(connection, stored, started, delegation.step_id)
            cursor = connection.execute(
                """
                UPDATE goal_plan_step_delegations SET status = ?
                WHERE action_id = ? AND status = ?
                """,
                (
                    status.value,
                    str(action_id),
                    StepDelegationStatus.AWAITING_APPROVAL.value,
                ),
            )
            if cursor.rowcount != 1:
                raise GoalPlanConflictError(
                    f"step delegation for action {action_id} changed concurrently",
                )
        return stored, replace(delegation, status=status)

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        except sqlite3.Error:
            raise GoalPlanRepositoryError("goal/plan state database is unavailable") from None
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            connection.close()
            raise GoalPlanRepositoryError("goal/plan state database is unavailable") from None
        return connection

    @contextmanager
    def _transaction(self, *, write: bool, failure: str) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        except sqlite3.Error:
            raise GoalPlanRepositoryError(failure) from None
        finally:
            connection.close()


def _require_new_goal(goal: object) -> Goal:
    if not isinstance(goal, Goal):
        raise GoalPlanContractError("goal must be a Goal snapshot")
    if goal.status is not GoalStatus.DRAFT:
        raise GoalPlanTransitionError("a new goal must start as draft")
    return goal


def _require_new_plan(plan: object) -> Plan:
    if not isinstance(plan, Plan):
        raise GoalPlanContractError("plan must be a Plan snapshot")
    if any(step.status is not PlanStepStatus.PENDING for step in plan.steps):
        raise GoalPlanTransitionError("a new plan version must start with every step pending")
    return plan


def _require_uuid_argument(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise GoalPlanContractError(f"{name} must be a UUID")


def _require_version_argument(value: object) -> None:
    if not _is_int(value) or value < 1:
        raise GoalPlanContractError("plan version must be a positive integer")


def _require_expected_revision(value: object) -> int:
    if not _is_int(value) or value < 1:
        raise GoalPlanContractError("expected revision must be a positive integer")
    return value


def _require_current_revision(current: int, expected: int, name: str) -> None:
    if current != expected:
        raise GoalPlanConflictError(
            f"stale {name} revision: expected {expected}, current {current}",
        )


def _require_next_version(plan: Plan, latest: int | None) -> None:
    latest_version = latest or 0
    if plan.version <= latest_version:
        raise GoalPlanConflictError(
            f"plan version {plan.version} of goal {plan.goal_id} already exists",
        )
    if plan.version != latest_version + 1:
        raise GoalPlanConflictError(
            f"plan version must be {latest_version + 1}, not {plan.version}",
        )


def _require_supersedable(latest: Plan) -> None:
    started = [str(step.id) for step in latest.steps if step.status is PlanStepStatus.IN_PROGRESS]
    if started:
        raise GoalPlanConflictError(
            f"plan version {latest.version} of goal {latest.goal_id} cannot be superseded "
            f"while steps are in progress: {', '.join(started)}",
        )


def _require_latest_version(version: int, latest: int) -> None:
    if version != latest:
        raise GoalPlanConflictError(
            f"plan version {version} is superseded by version {latest} and is read-only",
        )


def _require_step(plan: Plan, step_id: UUID) -> None:
    if all(step.id != step_id for step in plan.steps):
        raise GoalPlanNotFoundError(f"plan step {step_id} does not exist")


def _require_goal_accepts_plans(goal: Goal) -> None:
    if goal.status in goal_lifecycle.TERMINAL_GOAL_STATUSES:
        raise GoalPlanTransitionError(
            f"goal {goal.id} is {goal.status} and cannot receive new plan versions",
        )


def _require_goal_allows_start(goal: Goal) -> None:
    if goal.status is not GoalStatus.ACTIVE:
        raise GoalPlanTransitionError(
            f"plan steps start only while their goal is active; goal {goal.id} "
            f"is {goal.status}",
        )


def _require_startable(plan: Plan, goal: Goal, step_id: UUID) -> Plan:
    """Return ``plan`` with ``step_id`` started, or raise why it cannot start."""
    _require_step(plan, step_id)
    step = plan.step(step_id)
    if step.status is not PlanStepStatus.PENDING:
        raise GoalPlanTransitionError(f"plan step {step_id} is {step.status}, not pending")
    _require_goal_allows_start(goal)
    return goal_lifecycle.transition_plan_step(plan, step_id, PlanStepStatus.IN_PROGRESS)


def _require_requester(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise GoalPlanContractError("delegation requires an explicit canonical requester")


def _require_account(principal: object, identifier: object) -> None:
    if identifier is None:
        if principal is not None:
            raise GoalPlanContractError("account principal requires an account identifier")
        return
    for value, name in ((identifier, "account identifier"), (principal, "account principal")):
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise GoalPlanContractError(f"delegation {name} must be canonical text")


def _require_handoff(value: object) -> StepHandoff:
    if not isinstance(value, StepHandoff):
        raise GoalPlanContractError("delegation handoff must return a StepHandoff")
    return value


def _require_awaiting_approval(delegation: StepDelegation) -> None:
    if delegation.status is not StepDelegationStatus.AWAITING_APPROVAL:
        raise GoalPlanConflictError(
            f"step delegation for action {delegation.action_id} is {delegation.status}, "
            "not awaiting approval",
        )


def _delegation_from_handoff(
    goal_id: UUID,
    version: int,
    step_id: UUID,
    requested_by: str,
    account_principal: str | None,
    account_identifier: str | None,
    handoff: StepHandoff,
) -> StepDelegation:
    return StepDelegation(
        goal_id=goal_id,
        version=version,
        step_id=step_id,
        action_id=handoff.action_id,
        requested_by=requested_by,
        account_principal=account_principal,
        account_identifier=account_identifier,
        status=handoff.status,
        permission_status=handoff.permission_status,
        routing_reason=handoff.routing_reason,
    )


def _require_schema_objects(
    connection: sqlite3.Connection,
    required: frozenset[tuple[str, str]],
) -> None:
    present = {
        (str(row["type"]), str(row["name"]))
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')",
        ).fetchall()
    }
    if not required.issubset(present):
        raise GoalPlanCorruptedError("goal/plan state schema is incomplete")


def _goal_exists(connection: sqlite3.Connection, goal_id: UUID) -> bool:
    row = connection.execute(
        "SELECT 1 FROM goals WHERE goal_id = ?",
        (str(goal_id),),
    ).fetchone()
    return row is not None


def _load_goal(connection: sqlite3.Connection, goal_id: UUID) -> StoredGoal | None:
    row = connection.execute(
        "SELECT * FROM goals WHERE goal_id = ?",
        (str(goal_id),),
    ).fetchone()
    return _stored_goal_from_row(row) if row is not None else None


def _require_goal(connection: sqlite3.Connection, goal_id: UUID) -> Goal:
    stored = _load_goal(connection, goal_id)
    if stored is None:
        raise GoalPlanCorruptedError(f"plan references missing goal {goal_id}")
    return stored.goal


def _write_progress(
    connection: sqlite3.Connection,
    stored: StoredPlan,
    plan: Plan,
    step_id: UUID,
) -> StoredPlan:
    """Persist one step's new status (if any) and bump the plan revision."""
    goal_id, version = stored.plan.goal_id, stored.plan.version
    previous = stored.plan.step(step_id).status
    current = plan.step(step_id).status
    if current is not previous:
        step_cursor = connection.execute(
            """
            UPDATE goal_plan_steps SET status = ?
            WHERE goal_id = ? AND version = ? AND step_id = ? AND status = ?
            """,
            (current.value, str(goal_id), version, str(step_id), previous.value),
        )
        if step_cursor.rowcount != 1:
            raise GoalPlanConflictError(
                f"plan version {version} of goal {goal_id} changed concurrently",
            )
    plan_cursor = connection.execute(
        """
        UPDATE goal_plans SET revision = revision + 1
        WHERE goal_id = ? AND version = ? AND revision = ?
        """,
        (str(goal_id), version, stored.revision),
    )
    if plan_cursor.rowcount != 1:
        raise GoalPlanConflictError(
            f"plan version {version} of goal {goal_id} changed concurrently",
        )
    return StoredPlan(plan=plan, revision=stored.revision + 1)


def _started_execution(
    delegation: StepDelegation,
    plan: Plan,
    *,
    execution_id: UUID,
    executor_role: object,
    capability: object,
    started_at: datetime,
) -> StepExecution:
    """Build start evidence from the trusted delegation; reject foreign routes."""
    if delegation.status not in _EXECUTABLE_DELEGATIONS:
        raise GoalPlanConflictError(
            f"action {delegation.action_id} is {delegation.status} and was not released "
            "for execution",
        )
    try:
        step = plan.step(delegation.step_id)
    except KeyError:
        raise GoalPlanCorruptedError("persisted step delegation is invalid") from None
    if (executor_role, capability) != (step.executor_role, step.capability):
        raise GoalPlanConflictError(
            f"execution route of action {delegation.action_id} differs from its plan step",
        )
    return StepExecution(
        execution_id=execution_id,
        action_id=delegation.action_id,
        goal_id=delegation.goal_id,
        version=delegation.version,
        step_id=delegation.step_id,
        executor_role=step.executor_role,
        capability=step.capability,
        started_at=started_at,
    )


def _require_same_start(existing: StepExecution, candidate: StepExecution) -> StepExecution:
    identity = (
        "action_id",
        "goal_id",
        "version",
        "step_id",
        "executor_role",
        "capability",
        "started_at",
    )
    if any(getattr(existing, name) != getattr(candidate, name) for name in identity):
        raise GoalPlanConflictError(
            f"execution {existing.execution_id} is already recorded with a different identity",
        )
    return existing


def _completed_execution(
    existing: StepExecution,
    *,
    action_id: UUID,
    status: StepExecutionStatus,
    finished_at: datetime,
    external_execution_reported: bool,
    failure_category: str | None,
    reason: str | None,
) -> StepExecution:
    """Return the completed evidence, ``existing`` itself for an identical replay."""
    if existing.action_id != action_id:
        raise GoalPlanConflictError(
            f"execution {existing.execution_id} belongs to a different action",
        )
    if status is StepExecutionStatus.STARTED:
        raise GoalPlanContractError("an execution cannot be completed as started")
    completed = replace(
        existing,
        status=status,
        finished_at=finished_at,
        external_execution_reported=external_execution_reported,
        failure_category=failure_category,
        reason=reason[:_MAX_REASON_LENGTH] if isinstance(reason, str) else reason,
    )
    if existing.status is StepExecutionStatus.STARTED:
        return completed
    if completed == existing:
        return existing
    raise GoalPlanConflictError(
        f"execution {existing.execution_id} already completed with a different outcome",
    )


def _load_execution(connection: sqlite3.Connection, execution_id: UUID) -> StepExecution | None:
    row = connection.execute(
        "SELECT * FROM goal_plan_step_executions WHERE execution_id = ?",
        (str(execution_id),),
    ).fetchone()
    return _execution_from_row(row) if row is not None else None


def _execution_from_row(row: sqlite3.Row) -> StepExecution:
    try:
        reported = row["external_execution_reported"]
        if reported is not None and (not _is_int(reported) or reported not in (0, 1)):
            raise ValueError("external execution report must be 0 or 1")
        finished = _decode_optional_text(row["finished_at"])
        return StepExecution(
            execution_id=_decode_uuid(row["execution_id"]),
            action_id=_decode_uuid(row["action_id"]),
            goal_id=_decode_uuid(row["goal_id"]),
            version=_decode_positive_int(row["version"]),
            step_id=_decode_uuid(row["step_id"]),
            executor_role=ExecutorRole(_decode_text(row["executor_role"])),
            capability=_decode_text(row["capability"]),
            started_at=_decode_datetime(_decode_text(row["started_at"])),
            status=StepExecutionStatus(_decode_text(row["status"])),
            finished_at=_decode_datetime(finished) if finished is not None else None,
            external_execution_reported=bool(reported) if reported is not None else None,
            failure_category=_decode_optional_text(row["failure_category"]),
            reason=_decode_optional_text(row["reason"]),
        )
    except (KeyError, TypeError, ValueError):
        raise GoalPlanCorruptedError("persisted execution evidence is invalid") from None


def _load_delegation(
    connection: sqlite3.Connection,
    goal_id: UUID,
    version: int,
    step_id: UUID,
) -> StepDelegation | None:
    row = connection.execute(
        """
        SELECT * FROM goal_plan_step_delegations
        WHERE goal_id = ? AND version = ? AND step_id = ?
        """,
        (str(goal_id), version, str(step_id)),
    ).fetchone()
    return _delegation_from_row(row) if row is not None else None


def _delegation_from_row(row: sqlite3.Row) -> StepDelegation:
    try:
        principal = _decode_optional_text(row["account_principal"])
        identifier = _decode_optional_text(row["account_identifier"])
        requested_by = _decode_text(row["requested_by"])
        _require_requester(requested_by)
        _require_account(principal, identifier)
        return StepDelegation(
            goal_id=_decode_uuid(row["goal_id"]),
            version=_decode_positive_int(row["version"]),
            step_id=_decode_uuid(row["step_id"]),
            action_id=_decode_uuid(row["action_id"]),
            requested_by=requested_by,
            account_principal=principal,
            account_identifier=identifier,
            status=StepDelegationStatus(_decode_text(row["status"])),
            permission_status=_decode_optional_text(row["permission_status"]),
            routing_reason=_decode_optional_text(row["routing_reason"]),
        )
    except (KeyError, TypeError, ValueError):
        raise GoalPlanCorruptedError("persisted step delegation is invalid") from None


def _latest_version(connection: sqlite3.Connection, goal_id: UUID) -> int | None:
    row = connection.execute(
        "SELECT MAX(version) AS latest FROM goal_plans WHERE goal_id = ?",
        (str(goal_id),),
    ).fetchone()
    latest = row["latest"]
    if latest is None:
        return None
    if not _is_int(latest) or latest < 1:
        raise GoalPlanCorruptedError("persisted plan version is invalid")
    return latest


def _load_plan(
    connection: sqlite3.Connection,
    goal_id: UUID,
    version: int,
) -> StoredPlan | None:
    plan_row = connection.execute(
        "SELECT * FROM goal_plans WHERE goal_id = ? AND version = ?",
        (str(goal_id), version),
    ).fetchone()
    if plan_row is None:
        return None
    step_rows = connection.execute(
        """
        SELECT * FROM goal_plan_steps
        WHERE goal_id = ? AND version = ?
        ORDER BY position
        """,
        (str(goal_id), version),
    ).fetchall()
    return _stored_plan_from_rows(plan_row, step_rows)


def _stored_goal_from_row(row: sqlite3.Row) -> StoredGoal:
    try:
        goal = Goal(
            id=_decode_uuid(row["goal_id"]),
            objective=_decode_text(row["objective"]),
            success_criteria=_decode_texts(row["success_criteria"]),
            constraints=_decode_texts(row["constraints"]),
            status=GoalStatus(_decode_text(row["status"])),
        )
        revision = _decode_positive_int(row["revision"])
    except (KeyError, TypeError, ValueError):
        raise GoalPlanCorruptedError("persisted goal is invalid") from None
    return StoredGoal(goal=goal, revision=revision)


def _stored_plan_from_rows(
    plan_row: sqlite3.Row,
    step_rows: Sequence[sqlite3.Row],
) -> StoredPlan:
    try:
        positions = [_decode_int(row["position"]) for row in step_rows]
        if positions != list(range(len(step_rows))):
            raise ValueError("plan step positions are not contiguous")
        plan = Plan(
            goal_id=_decode_uuid(plan_row["goal_id"]),
            version=_decode_positive_int(plan_row["version"]),
            steps=tuple(_step_from_row(row) for row in step_rows),
        )
        revision = _decode_positive_int(plan_row["revision"])
    except (KeyError, TypeError, ValueError):
        raise GoalPlanCorruptedError("persisted plan is invalid") from None
    return StoredPlan(plan=plan, revision=revision)


def _step_from_row(row: sqlite3.Row) -> PlanStep:
    return PlanStep(
        id=_decode_uuid(row["step_id"]),
        objective=_decode_text(row["objective"]),
        target=_decode_text(row["target"]),
        executor_role=ExecutorRole(_decode_text(row["executor_role"])),
        capability=_decode_text(row["capability"]),
        depends_on=tuple(_decode_uuid(value) for value in _decode_texts(row["depends_on"])),
        status=PlanStepStatus(_decode_text(row["status"])),
    )


def _encode_texts(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _decode_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("persisted value must be text")
    return value


def _decode_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("persisted timestamp is not canonical")
    return _require_aware(parsed, "persisted timestamp")


def _require_aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise GoalPlanContractError(f"{name} must be a timezone-aware datetime")
    return value


def _decode_optional_text(value: object) -> str | None:
    return None if value is None else _decode_text(value)


def _decode_texts(value: object) -> tuple[str, ...]:
    decoded = json.loads(_decode_text(value))
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise TypeError("persisted value must be a JSON array of text")
    return tuple(decoded)


def _decode_uuid(value: object) -> UUID:
    text = _decode_text(value)
    parsed = UUID(text)
    if str(parsed) != text:
        raise ValueError("persisted UUID is not canonical")
    return parsed


def _decode_int(value: object) -> int:
    if not _is_int(value):
        raise TypeError("persisted value must be an integer")
    return value


def _decode_positive_int(value: object) -> int:
    decoded = _decode_int(value)
    if decoded < 1:
        raise ValueError("persisted value must be positive")
    return decoded


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)
