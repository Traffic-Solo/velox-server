"""Work-product review and disposition against real temporary git repositories."""

import inspect
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from apps.server.src.core.config import get_settings
from apps.server.src.core.container import ApplicationContainer
from apps.server.src.integrations import software_engineering_pilot
from apps.server.src.integrations import software_engineering_runtime as runtime
from apps.server.src.integrations.software_engineering import (
    ProcessResult,
    SubprocessRunner,
    TrustedGitWorkspace,
)
from apps.server.src.integrations.software_engineering_work_product import (
    SoftwareEngineeringWorkProductService,
    WorkProductDisposition,
    WorkProductIdentityError,
)

GIT_USER = ("-c", "user.email=velox@example.test", "-c", "user.name=VELOX")
REPORT = {"summary": "Changed app.py.", "files_changed": ["app.py"],
          "validation": ["uv run pytest -q: passed"], "blockers": []}


def git(cwd: Path, *args: str) -> str:
    outcome = SubprocessRunner().run(["git", *GIT_USER, *args], cwd=cwd, timeout_seconds=30)
    assert outcome.returncode == 0, outcome.stderr
    return outcome.stdout


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("print('hello')\n")
    (path / ".gitignore").write_text(".env\n")
    git(path, "add", "app.py", ".gitignore")
    git(path, "commit", "-q", "-m", "init")
    return path


def branch_exists(repo: Path, branch: str) -> bool:
    outcome = SubprocessRunner().run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo, timeout_seconds=30,
    )
    return outcome.returncode == 0


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def workspace(repo: Path, tmp_path: Path) -> TrustedGitWorkspace:
    return TrustedGitWorkspace(repo, SubprocessRunner(), worktrees_root=tmp_path / "wt")


@pytest.fixture
def service(workspace: TrustedGitWorkspace) -> SoftwareEngineeringWorkProductService:
    return SoftwareEngineeringWorkProductService(workspace)


def worker_edit(workspace: TrustedGitWorkspace, action_id: UUID) -> Path:
    """Simulate a worker: modify a tracked file, add untracked and ignored files."""
    path = workspace.create_worktree(action_id).path
    (path / "app.py").write_text("print('hello, velox')\n")
    (path / "notes.txt").write_text("UNTRACKED-CONTENT-MUST-NOT-APPEAR\n")
    (path / ".env").write_text("VELOX_API_TOKEN=IGNORED-SECRET-MUST-NOT-APPEAR\n")
    return path


def rendered(value: object) -> str:
    return repr(value)


# --- Identity ---------------------------------------------------------------------

def test_identity_is_derived_only_from_workspace_and_action_uuid(
    workspace: TrustedGitWorkspace, tmp_path: Path,
) -> None:
    action_id = uuid4()
    expected = workspace.expected_worktree(action_id)
    assert expected.path == tmp_path / "wt" / f"se-{action_id}"
    assert expected.branch == f"velox/se-{action_id}"
    assert workspace.create_worktree(action_id) == expected
    for method in (SoftwareEngineeringWorkProductService.review,
                   SoftwareEngineeringWorkProductService.apply):
        parameters = list(inspect.signature(method).parameters)
        assert "path" not in " ".join(parameters) and "branch" not in parameters


def test_metadata_paths_cannot_direct_review_or_cleanup(
    workspace: TrustedGitWorkspace, service: SoftwareEngineeringWorkProductService,
    tmp_path: Path,
) -> None:
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "keep-me.txt").write_text("not a VELOX worktree\n")
    with pytest.raises(WorkProductIdentityError):
        service.apply(uuid4(), WorkProductDisposition.DISCARD)
    assert (decoy / "keep-me.txt").exists()


def test_derived_path_equal_to_the_canonical_checkout_is_rejected(tmp_path: Path) -> None:
    action_id = uuid4()
    repo = make_repo(tmp_path / f"se-{action_id}")
    workspace = TrustedGitWorkspace(repo, SubprocessRunner(), worktrees_root=tmp_path)
    assert workspace.expected_worktree(action_id).path == repo
    with pytest.raises(WorkProductIdentityError, match="canonical"):
        SoftwareEngineeringWorkProductService(workspace).review(action_id)


def test_derived_path_inside_the_canonical_checkout_is_rejected(repo: Path) -> None:
    action_id = uuid4()
    workspace = TrustedGitWorkspace(repo, SubprocessRunner(), worktrees_root=repo / "wt")
    (repo / "wt" / f"se-{action_id}").mkdir(parents=True)
    with pytest.raises(WorkProductIdentityError, match="canonical"):
        SoftwareEngineeringWorkProductService(workspace).review(action_id)


def test_unregistered_directory_at_the_derived_path_is_rejected(
    workspace: TrustedGitWorkspace, service: SoftwareEngineeringWorkProductService,
) -> None:
    action_id = uuid4()
    path = workspace.expected_worktree(action_id).path
    path.mkdir(parents=True)
    (path / "file.txt").write_text("plain directory\n")
    with pytest.raises(WorkProductIdentityError, match="registered"):
        service.review(action_id)
    with pytest.raises(WorkProductIdentityError):
        service.apply(action_id, WorkProductDisposition.DISCARD)
    assert (path / "file.txt").exists()


def test_worktree_on_an_unexpected_branch_is_rejected_and_not_cleaned(
    workspace: TrustedGitWorkspace, service: SoftwareEngineeringWorkProductService,
    repo: Path,
) -> None:
    action_id = uuid4()
    path = workspace.expected_worktree(action_id).path
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", "feature/other", str(path), "HEAD")
    with pytest.raises(WorkProductIdentityError, match="registered"):
        service.apply(action_id, WorkProductDisposition.DISCARD)
    assert path.exists()
    assert branch_exists(repo, "feature/other")


def test_missing_worktree_fails_closed(service: SoftwareEngineeringWorkProductService) -> None:
    with pytest.raises(WorkProductIdentityError, match="does not exist"):
        service.review(uuid4())


# --- Review -----------------------------------------------------------------------

def test_review_reports_files_stat_diff_and_untracked_names_only(
    workspace: TrustedGitWorkspace, service: SoftwareEngineeringWorkProductService,
) -> None:
    action_id = uuid4()
    worker_edit(workspace, action_id)
    review = service.review(action_id)
    assert review.action_id == action_id
    assert review.worktree_path == workspace.expected_worktree(action_id).path
    assert review.branch == f"velox/se-{action_id}"
    assert review.changed_files == ("app.py", "notes.txt")
    assert review.untracked_files == ("notes.txt",)
    assert "app.py" in review.diff_stat and "1 file changed" in review.diff_stat
    assert "+print('hello, velox')" in review.diff
    assert review.diff_truncated is False
    assert review.dirty is True
    assert review.canonical_clean is True and review.canonical_unchanged is True
    output = rendered(review)
    assert "UNTRACKED-CONTENT-MUST-NOT-APPEAR" not in output
    assert "IGNORED-SECRET-MUST-NOT-APPEAR" not in output
    assert ".env" not in output


def test_review_diff_is_bounded_and_truncation_is_reported(
    workspace: TrustedGitWorkspace,
) -> None:
    action_id = uuid4()
    path = workspace.create_worktree(action_id).path
    (path / "app.py").write_text("".join(f"line {n}\n" for n in range(2000)))
    review = SoftwareEngineeringWorkProductService(workspace, diff_limit=500).review(action_id)
    assert len(review.diff) == 500
    assert review.diff_truncated is True


def test_clean_worktree_review_is_not_dirty(
    workspace: TrustedGitWorkspace, service: SoftwareEngineeringWorkProductService,
) -> None:
    action_id = uuid4()
    workspace.create_worktree(action_id)
    review = service.review(action_id)
    assert review.dirty is False
    assert review.changed_files == () and review.diff == ""


# --- Disposition --------------------------------------------------------------------

def test_keep_verifies_and_mutates_nothing(
    workspace: TrustedGitWorkspace, service: SoftwareEngineeringWorkProductService,
    repo: Path,
) -> None:
    action_id = uuid4()
    path = worker_edit(workspace, action_id)
    before = service.review(action_id)
    result = service.apply(action_id, WorkProductDisposition.KEEP)
    assert result.succeeded is True and result.remaining == ()
    assert result.worktree_present is True and result.branch_present is True
    assert (path / "app.py").read_text() == "print('hello, velox')\n"
    assert branch_exists(repo, f"velox/se-{action_id}")
    assert service.review(action_id) == before


def test_discard_removes_exactly_the_derived_worktree_and_local_branch(
    workspace: TrustedGitWorkspace, service: SoftwareEngineeringWorkProductService,
    repo: Path,
) -> None:
    target, other = uuid4(), uuid4()
    target_path = worker_edit(workspace, target)
    other_path = worker_edit(workspace, other)
    git(repo, "branch", "feature/unrelated")
    canonical_before = workspace.status(repo)

    result = service.apply(target, WorkProductDisposition.DISCARD)

    assert result.succeeded is True and result.remaining == ()
    assert result.canonical_unchanged is True
    assert not target_path.exists()
    assert not branch_exists(repo, f"velox/se-{target}")
    assert other_path.exists() and branch_exists(repo, f"velox/se-{other}")
    assert branch_exists(repo, "main") and branch_exists(repo, "feature/unrelated")
    assert workspace.status(repo) == canonical_before
    assert (repo / "app.py").read_text() == "print('hello')\n"
    assert str(target_path) not in git(repo, "worktree", "list", "--porcelain")


def test_discard_refuses_a_branch_with_commits(
    workspace: TrustedGitWorkspace, service: SoftwareEngineeringWorkProductService,
    repo: Path,
) -> None:
    action_id = uuid4()
    path = worker_edit(workspace, action_id)
    git(path, "commit", "-q", "-am", "unexpected commit")
    with pytest.raises(WorkProductIdentityError, match="commits"):
        service.apply(action_id, WorkProductDisposition.DISCARD)
    assert path.exists() and branch_exists(repo, f"velox/se-{action_id}")


class FailingGitRunner:
    """Real git, except the named subcommands, which report failure."""

    def __init__(self, failing: set[str]) -> None:
        self.real = SubprocessRunner()
        self.failing = failing

    def run(self, argv: Sequence[str], *, cwd: Path, timeout_seconds: float,
            stdin: str | None = None, env: Mapping[str, str] | None = None) -> ProcessResult:
        if argv[0] == "git" and len(argv) > 1 and argv[1] in self.failing and (
            argv[1] != "worktree" or argv[2] == "remove"
        ):
            return ProcessResult(1, "", "simulated failure", False, 0.0)
        return self.real.run(argv, cwd=cwd, timeout_seconds=timeout_seconds,
                             stdin=stdin, env=env)


@pytest.mark.parametrize(
    ("failing", "remaining"),
    [({"branch"}, ("branch",)), ({"worktree"}, ("worktree", "branch"))],
)
def test_partial_cleanup_is_reported_with_the_remaining_resources(
    repo: Path, tmp_path: Path, failing: set[str], remaining: tuple[str, ...],
) -> None:
    creator = TrustedGitWorkspace(repo, SubprocessRunner(), worktrees_root=tmp_path / "wt")
    action_id = uuid4()
    worker_edit(creator, action_id)
    workspace = TrustedGitWorkspace(
        repo, FailingGitRunner(failing), worktrees_root=tmp_path / "wt",
    )
    result = SoftwareEngineeringWorkProductService(workspace).apply(
        action_id, WorkProductDisposition.DISCARD,
    )
    assert result.succeeded is False
    assert result.remaining == remaining
    assert result.canonical_unchanged is True


# --- Pilot ----------------------------------------------------------------------------

class HybridRunner:
    """Real git; a fake Claude CLI that edits the worktree. Never runs Claude."""

    def __init__(self) -> None:
        self.real = SubprocessRunner()
        self.claude_runs = 0

    def run(self, argv: Sequence[str], *, cwd: Path, timeout_seconds: float,
            stdin: str | None = None, env: Mapping[str, str] | None = None) -> ProcessResult:
        if argv[0] == "git":
            return self.real.run(argv, cwd=cwd, timeout_seconds=timeout_seconds,
                                 stdin=stdin, env=env)
        if list(argv[1:]) == ["--version"]:
            return ProcessResult(0, "2.1.153 (Claude Code)\n", "", False, 0.0)
        self.claude_runs += 1
        (cwd / "app.py").write_text("print('hello, velox')\n")
        (cwd / "notes.txt").write_text("new file\n")
        envelope = {"type": "result", "is_error": False, "session_id": "s-1",
                    "structured_output": REPORT}
        return ProcessResult(0, json.dumps(envelope), "", False, 1.0)


@pytest.fixture
def pilot(monkeypatch: pytest.MonkeyPatch, repo: Path) -> HybridRunner:
    hybrid = HybridRunner()
    monkeypatch.setenv("VELOX_SOFTWARE_ENGINEERING_PROVIDER", "claude_code")
    monkeypatch.setenv("VELOX_SOFTWARE_ENGINEERING_WORKSPACE", str(repo))
    monkeypatch.setattr(runtime, "SubprocessRunner", lambda: hybrid)
    get_settings.cache_clear()
    return hybrid


def run_pilot(disposition: str) -> tuple[int, str, ApplicationContainer, UUID]:
    container = ApplicationContainer()
    out = io.StringIO()
    answers: list[str] = []

    def read_line() -> str:
        if not answers:
            [pending] = container.pending_approval_registry.list_pending()
            answers.append(str(pending.id))
            return str(pending.id)
        return disposition

    code = software_engineering_pilot.main(
        ["--objective", "Greet VELOX", "--target", "velox-server"],
        container_factory=lambda: container, read_line=read_line, out=out,
    )
    return code, out.getvalue(), container, UUID(answers[0])


def test_pilot_prints_review_and_keep_preserves_the_worktree(
    pilot: HybridRunner, repo: Path,
) -> None:
    code, output, container, action_id = run_pilot("keep")
    assert code == 0 and pilot.claude_runs == 1
    assert "=== VELOX work-product review ===" in output
    assert "Changed files: app.py, notes.txt" in output
    assert "Untracked files (content not shown): notes.txt" in output
    assert "+print('hello, velox')" in output
    assert "Type keep to preserve the worktree, discard to remove it:" in output
    assert "Kept." in output
    work_products = container.software_engineering_work_products
    assert work_products is not None
    review = work_products.review(action_id)
    assert review.worktree_path.exists()
    assert str(review.worktree_path) in output and review.branch in output
    assert branch_exists(repo, review.branch)


def test_pilot_discard_removes_worktree_and_branch(pilot: HybridRunner, repo: Path) -> None:
    code, output, container, action_id = run_pilot("discard")
    assert code == 0
    assert "Discarded." in output
    assert container.software_engineering_executor is not None
    expected = TrustedGitWorkspace(repo, SubprocessRunner()).expected_worktree(action_id)
    assert not expected.path.exists()
    assert not branch_exists(repo, expected.branch)
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("answer", ["", "yes", "DISCARD", "rm -rf /", "/tmp/elsewhere"])
def test_pilot_invalid_disposition_changes_nothing(
    pilot: HybridRunner, repo: Path, answer: str,
) -> None:
    code, output, _, action_id = run_pilot(answer)
    assert code == 0
    assert "No disposition applied" in output
    expected = TrustedGitWorkspace(repo, SubprocessRunner()).expected_worktree(action_id)
    assert expected.path.exists() and branch_exists(repo, expected.branch)
