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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
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
_SCHEMA_VERSION = 1

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


class GoalPlanRepository(Protocol):
    """Persistence Role for Goals and versioned Plans.

    New Goals must be ``draft`` and new Plan versions must have every step
    ``pending``; statuses change only through the transition operations, which
    apply ``goal_lifecycle`` policy and require the caller's expected revision.
    There is no delete and no arbitrary status replacement.
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


class InMemoryGoalPlanRepository:
    """Process-local implementation with the same rules as the SQLite repository."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._goals: dict[UUID, StoredGoal] = {}
        self._plans: dict[UUID, list[StoredPlan]] = {}

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
            if plan.goal_id not in self._goals:
                raise GoalPlanNotFoundError(f"goal {plan.goal_id} does not exist")
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
            updated = StoredPlan(plan=plan, revision=stored.revision + 1)
            versions[version - 1] = updated
            return updated


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
            if row is None:
                for statement in _SCHEMA_V1_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO velox_schema(name, version) VALUES (?, ?)",
                    (_SCHEMA_NAME, _SCHEMA_VERSION),
                )
            elif _is_int(row["version"]) and row["version"] == _SCHEMA_VERSION:
                _require_schema_objects(connection)
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
            if not _goal_exists(connection, plan.goal_id):
                raise GoalPlanNotFoundError(f"goal {plan.goal_id} does not exist")
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
            previous = stored.plan.step(step_id).status
            step_cursor = connection.execute(
                """
                UPDATE goal_plan_steps SET status = ?
                WHERE goal_id = ? AND version = ? AND step_id = ? AND status = ?
                """,
                (status.value, str(goal_id), version, str(step_id), previous.value),
            )
            plan_cursor = connection.execute(
                """
                UPDATE goal_plans SET revision = revision + 1
                WHERE goal_id = ? AND version = ? AND revision = ?
                """,
                (str(goal_id), version, stored.revision),
            )
            if step_cursor.rowcount != 1 or plan_cursor.rowcount != 1:
                raise GoalPlanConflictError(
                    f"plan version {version} of goal {goal_id} changed concurrently",
                )
        return StoredPlan(plan=plan, revision=stored.revision + 1)

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


def _require_schema_objects(connection: sqlite3.Connection) -> None:
    present = {
        (str(row["type"]), str(row["name"]))
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')",
        ).fetchall()
    }
    if not _SCHEMA_V1_OBJECTS.issubset(present):
        raise GoalPlanCorruptedError("goal/plan state schema is incomplete")


def _goal_exists(connection: sqlite3.Connection, goal_id: UUID) -> bool:
    row = connection.execute(
        "SELECT 1 FROM goals WHERE goal_id = ?",
        (str(goal_id),),
    ).fetchone()
    return row is not None


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
