"""Operator entrypoint for one live Software Engineering pilot run.

    uv run --env-file .env.live python -m \\
        apps.server.src.integrations.software_engineering_pilot \\
        --objective "..." --target "..."

The run goes through the normal application path only: ``TaskDelegator`` ->
``PermissionEngineRuntime`` (held for approval) -> explicit operator approval via
``approve_pending_action`` -> ``WorkerRuntime`` -> the registered provider. The
worker changes files only in a new isolated worktree; the canonical checkout is
not modified, and nothing is committed, pushed or merged.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from apps.server.src.core.actions import ExecutorRole
from apps.server.src.core.approval_decisions import approve_pending_action
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.core.delegation import (
    TaskDelegationRequest,
    TaskDelegationRequestError,
    TaskDelegationStatus,
)
from apps.server.src.integrations.software_engineering import (
    SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
)

_REPORTED_FIELDS = (
    "provider", "worktree_path", "worktree_branch", "duration_seconds", "session_id",
    "changed_files", "reported_files_changed", "summary", "validation", "blockers",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delegate one bounded engineering task through VELOX.",
    )
    parser.add_argument("--objective", required=True, help="Bounded engineering objective.")
    parser.add_argument("--target", required=True, help="Logical work target (no file access).")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    container_factory: Callable[[], ApplicationContainer] = ApplicationContainer,
    read_line: Callable[[], str] = input,
    out: TextIO = sys.stdout,
) -> int:
    args = _parser().parse_args(argv)
    container = container_factory()
    executor = container.software_engineering_executor
    if executor is None:
        print(
            "Software Engineering provider is disabled. Set "
            "VELOX_SOFTWARE_ENGINEERING_PROVIDER=claude_code and "
            "VELOX_SOFTWARE_ENGINEERING_WORKSPACE (for example in .env.live).",
            file=out,
        )
        return 2
    try:
        request = TaskDelegationRequest(
            objective=args.objective,
            target=args.target,
            executor_role=ExecutorRole.SOFTWARE_ENGINEERING,
            capability=SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY,
        )
    except TaskDelegationRequestError as error:
        print(f"Invalid task: {error}", file=out)
        return 2

    delegation = container.task_delegator.delegate(request)
    action_id = str(delegation.action_id)
    print(f"Delegation: {delegation.status.value} (action {action_id})", file=out)
    if delegation.status is not TaskDelegationStatus.AWAITING_APPROVAL:
        print(
            "Expected the task to be held for approval; nothing was executed.",
            f"Routing: {delegation.routing_reason}",
            sep="\n",
            file=out,
        )
        return 1

    print(
        "Approving runs the coding worker in a NEW isolated git worktree under "
        f"{executor.worktrees_root}. The canonical checkout is not modified; nothing "
        "is committed, pushed or merged.",
        f"Type the action id to approve ({action_id}), anything else to cancel:",
        sep="\n",
        file=out,
    )
    if read_line().strip() != action_id:
        print("Not approved; the action stays pending and nothing was executed.", file=out)
        return 1
    approve_pending_action(
        delegation.action_id,
        pending_approval_registry=container.pending_approval_registry,
        lifecycle_repository=container.action_lifecycle_repository,
        lifecycle_manager=container.action_lifecycle_manager,
        action_queue=container.action_queue,
    )
    print("Approved. Running the worker (this can take several minutes)...", file=out)
    container.worker_runtime_invocation.invoke(max_actions=1)

    observation = next(
        (
            item for item in container.worker_execution_observer.list()
            if item.action_id == delegation.action_id
        ),
        None,
    )
    if observation is None:
        print("The worker did not run.", file=out)
        return 1
    print(
        f"Provider: {observation.matched_provider}",
        f"Status: {observation.status}",
        f"Reason: {observation.reason}",
        sep="\n",
        file=out,
    )
    metadata = observation.metadata or {}
    for key in _REPORTED_FIELDS:
        if key in metadata:
            print(f"{key}: {metadata[key]}", file=out)
    return 0 if observation.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
