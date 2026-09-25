"""Operator entrypoint for one live Software Engineering pilot run.

    uv run --env-file .env.live python -m \\
        apps.server.src.integrations.software_engineering_pilot \\
        --objective "..." --target "..."

The run goes through the normal application path only: ``TaskDelegator`` ->
``PermissionEngineRuntime`` (held for approval) -> explicit operator approval via
``approve_pending_action`` -> ``WorkerRuntime`` -> the registered provider. The
worker changes files only in a new isolated worktree; the canonical checkout is
not modified, and nothing is committed, pushed or merged. Afterwards VELOX shows a
bounded review of the worktree and asks once whether to keep or discard it.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import TextIO
from uuid import UUID

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
from apps.server.src.integrations.software_engineering_work_product import (
    SoftwareEngineeringWorkProductService,
    WorkProductDisposition,
    WorkProductIdentityError,
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
    work_products = container.software_engineering_work_products
    if work_products is not None:
        _review_and_dispose(work_products, delegation.action_id, read_line, out)
    return 0 if observation.status == "succeeded" else 1


def _review_and_dispose(
    work_products: SoftwareEngineeringWorkProductService,
    action_id: UUID,
    read_line: Callable[[], str],
    out: TextIO,
) -> None:
    """Show the VELOX-owned review, then apply exactly one explicit disposition."""
    try:
        review = work_products.review(action_id)
    except WorkProductIdentityError as error:
        print(f"No reviewable worktree for this action ({error}).", file=out)
        return
    print(
        "",
        "=== VELOX work-product review ===",
        f"Worktree: {review.worktree_path}",
        f"Branch: {review.branch}",
        f"Changed files: {', '.join(review.changed_files) or '(none)'}",
        f"Untracked files (content not shown): {', '.join(review.untracked_files) or '(none)'}",
        f"Canonical checkout clean: {review.canonical_clean}",
        "--- diff --stat ---",
        review.diff_stat.rstrip() or "(no tracked changes)",
        "--- diff ---",
        review.diff.rstrip() or "(no tracked changes)",
        sep="\n",
        file=out,
    )
    if review.diff_truncated:
        print(f"[diff truncated to {len(review.diff)} characters]", file=out)
    print("Type keep to preserve the worktree, discard to remove it:", file=out)
    answer = read_line().strip()
    if answer == WorkProductDisposition.KEEP.value:
        disposition = WorkProductDisposition.KEEP
    elif answer == WorkProductDisposition.DISCARD.value:
        disposition = WorkProductDisposition.DISCARD
    else:
        print(
            "No disposition applied; the worktree and branch are preserved:",
            f"  {review.worktree_path}",
            f"  {review.branch}",
            sep="\n",
            file=out,
        )
        return
    try:
        result = work_products.apply(action_id, disposition)
    except WorkProductIdentityError as error:
        print(f"Disposition refused ({error}); nothing was changed.", file=out)
        return
    if disposition is WorkProductDisposition.KEEP:
        print(
            "Kept. Worktree and branch remain for later review:",
            f"  {result.worktree_path}",
            f"  {result.branch}",
            sep="\n",
            file=out,
        )
    elif result.succeeded:
        print(
            "Discarded. Removed the isolated worktree and its local branch; "
            "the canonical checkout is unchanged.",
            file=out,
        )
    else:
        remaining = ", ".join(result.remaining) or "none"
        print(
            f"Discard incomplete. Remaining: {remaining}. "
            f"Canonical checkout unchanged: {result.canonical_unchanged}.",
            f"  {result.worktree_path}",
            f"  {result.branch}",
            sep="\n",
            file=out,
        )


if __name__ == "__main__":
    raise SystemExit(main())
