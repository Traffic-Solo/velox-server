"""Provider-neutral Software Engineering worker support.

Holds what any coding-worker provider (Claude Code today, Codex or others later)
shares: the canonical capability, a narrow process-runner boundary, the single
trusted workspace and VELOX-owned per-Action worktree isolation. Providers add
only their own CLI contract and result parsing on top of this module.
"""

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Protocol
from uuid import UUID

SOFTWARE_ENGINEERING_IMPLEMENT_CAPABILITY = "code.implement"
"""Canonical capability: implement one bounded change in an isolated worktree."""

_GIT_TIMEOUT_SECONDS = 60.0
_TERMINATE_GRACE_SECONDS = 5.0


class ExecutableNotFoundError(Exception):
    """The requested executable could not be started."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome of one bounded child process; ``returncode`` is None on timeout."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float


class ProcessRunner(Protocol):
    """Run an argv vector without a shell, bounded by a timeout."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        ...


class SubprocessRunner:
    """Real runner: argv only, own process group, whole group ended on timeout."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        started = perf_counter()
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=dict(env) if env is not None else None,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise ExecutableNotFoundError("executable not found") from None
        try:
            stdout, stderr = process.communicate(input=stdin, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
            return ProcessResult(None, stdout, stderr, True, perf_counter() - started)
        return ProcessResult(
            process.returncode, stdout, stderr, False, perf_counter() - started,
        )


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


class WorkspaceUnavailableError(Exception):
    """The trusted workspace or its isolated worktree cannot be used safely."""


@dataclass(frozen=True, slots=True)
class IsolatedWorktree:
    """A VELOX-created worktree and branch derived only from the Action id."""

    path: Path
    branch: str


class TrustedGitWorkspace:
    """The one configured repository a coding worker may branch from.

    The path comes from trusted configuration, never from a task request. Each
    Action gets its own worktree outside the canonical checkout, on a new branch
    named from the Action UUID, created from the canonical ``HEAD``.
    """

    def __init__(
        self,
        root: Path,
        runner: ProcessRunner,
        *,
        worktrees_root: Path | None = None,
        git_executable: str = "git",
    ) -> None:
        self._root = root
        self._runner = runner
        self._worktrees_root = worktrees_root
        self._git = git_executable

    def run_git(self, *args: str, cwd: Path) -> ProcessResult:
        """Run git through the process boundary; argv only, bounded."""
        try:
            return self._runner.run(
                [self._git, *args], cwd=cwd, timeout_seconds=_GIT_TIMEOUT_SECONDS,
            )
        except ExecutableNotFoundError:
            raise WorkspaceUnavailableError("git is not available") from None

    def validate(self) -> Path:
        """Return the canonical repository root or fail closed."""
        root = self._root
        if not root.is_absolute() or not root.is_dir():
            raise WorkspaceUnavailableError("trusted workspace does not exist")
        result = self.run_git("rev-parse", "--show-toplevel", cwd=root)
        if result.returncode != 0 or result.timed_out:
            raise WorkspaceUnavailableError("trusted workspace is not a git repository")
        if Path(result.stdout.strip()).resolve() != root.resolve():
            raise WorkspaceUnavailableError("trusted workspace is not the repository root")
        return root

    def worktrees_root(self) -> Path:
        root = self._root
        return self._worktrees_root or root.parent / f".{root.name}-velox-worktrees"

    def expected_worktree(self, action_id: UUID) -> IsolatedWorktree:
        """The only identity a per-Action worktree may have: workspace + Action UUID."""
        name = f"se-{UUID(str(action_id))}"
        return IsolatedWorktree(path=self.worktrees_root() / name, branch=f"velox/{name}")

    def create_worktree(self, action_id: UUID) -> IsolatedWorktree:
        """Create the per-Action worktree; the name never comes from task text."""
        root = self.validate()
        worktree = self.expected_worktree(action_id)
        path, branch = worktree.path, worktree.branch
        if path.exists():
            raise WorkspaceUnavailableError("isolated worktree already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self.run_git("worktree", "add", "-b", branch, str(path), "HEAD", cwd=root)
        if result.returncode != 0 or result.timed_out:
            raise WorkspaceUnavailableError("isolated worktree could not be created")
        return worktree

    def status(self, path: Path) -> str:
        """Porcelain status of a checkout, used to verify worker side effects."""
        result = self.run_git("status", "--porcelain=v1", "--untracked-files=all", cwd=path)
        if result.returncode != 0 or result.timed_out:
            raise WorkspaceUnavailableError("workspace status is unavailable")
        return result.stdout

    @staticmethod
    def changed_files(status: str) -> tuple[str, ...]:
        """File paths from porcelain v1 output (rename targets for renames)."""
        files: list[str] = []
        for line in status.splitlines():
            if len(line) > 3:
                entry = line[3:]
                files.append(entry.split(" -> ", 1)[-1])
        return tuple(files)
