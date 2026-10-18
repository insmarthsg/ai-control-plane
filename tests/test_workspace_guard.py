"""Read-only Git workspace guard: worktree identity, baseline, tracking, no mutation."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from adapters.git_workspace import GitWorkspaceGuard
from core.workspace import (
    WorkspaceAction,
    WorkspaceBinding,
    WorkspacePolicy,
    WorkspaceReason,
    WorkspaceStatus,
)
from tests.helpers.git_workspace_fixture import (
    advance_remote,
    build,
    fetch,
    local_commit,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = GitWorkspaceGuard()


def binding(fixture, **overrides) -> WorkspaceBinding:
    values = dict(
        workspace_id="ws-test",
        root_path=str(fixture.worktree_current),
        repository_root=str(fixture.repo),
        worktree_path=str(fixture.worktree_current),
        branch="feature/current",
        baseline_commit=fixture.baseline,
        tracking_ref="origin/feature/current",
        policy=WorkspacePolicy(require_tracking_ref=True),
    )
    if "root_path" in overrides:
        overrides.setdefault("worktree_path", overrides["root_path"])
    values.update(overrides)
    return WorkspaceBinding(**values)


def status_bytes(path: Path) -> bytes:
    return subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain=v1", "-z"], capture_output=True
    ).stdout


def test_correct_workspace_is_verified(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    result = GUARD.verify(binding(fixture))

    assert result.status is WorkspaceStatus.VERIFIED
    assert result.reason is WorkspaceReason.OK
    assert result.execution_allowed is True
    assert result.actual["branch"] == "feature/current"
    assert Path(result.actual["worktree_path"]) == fixture.worktree_current.resolve()
    assert Path(result.actual["repository_root"]) == fixture.repo.resolve()
    assert result.actual["dirty"] is False
    assert result.actual["head"] == fixture.baseline
    assert result.warnings == ()


def test_wrong_worktree_is_blocked_and_untouched(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    dirty_before = status_bytes(fixture.worktree_old)
    file_before = (fixture.worktree_old / "dirty.txt").read_bytes()
    stash_before = subprocess.run(
        ["git", "-C", str(fixture.worktree_old), "stash", "list"], capture_output=True
    ).stdout

    result = GUARD.verify(binding(
        fixture, root_path=str(fixture.worktree_old), worktree_path=str(fixture.worktree_current)
    ))

    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.WORKTREE_MISMATCH
    assert result.execution_allowed is False
    assert result.actual["dirty"] is True
    assert status_bytes(fixture.worktree_old) == dirty_before
    assert (fixture.worktree_old / "dirty.txt").read_bytes() == file_before
    assert subprocess.run(
        ["git", "-C", str(fixture.worktree_old), "stash", "list"], capture_output=True
    ).stdout == stash_before


def test_wrong_branch_is_blocked(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    result = GUARD.verify(binding(fixture, branch="feature/old"))
    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.BRANCH_MISMATCH
    assert result.actual["branch"] == "feature/current"


def test_wrong_repository_is_blocked(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    other = tmp_path / "other-repo"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "-b", "main"], capture_output=True, check=True)
    result = GUARD.verify(binding(fixture, repository_root=str(other)))
    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.REPOSITORY_MISMATCH


def test_missing_workspace_never_falls_back(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    missing = tmp_path / "does-not-exist"
    result = GUARD.verify(binding(fixture, root_path=str(missing), worktree_path=str(missing)))
    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.WORKSPACE_NOT_FOUND
    assert "does-not-exist" in result.actual["root_path"]


def test_dirty_correct_workspace_is_blocked(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    (fixture.worktree_current / "local-change.txt").write_text("uncommitted\n")
    result = GUARD.verify(binding(fixture))
    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.WORKSPACE_DIRTY
    assert result.actual["dirty"] is True


def test_detached_head_is_blocked(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    detached = fixture.root / "wt-detached"
    subprocess.run(
        ["git", "-C", str(fixture.repo), "worktree", "add", "--detach", str(detached), fixture.baseline],
        capture_output=True, check=True,
    )
    result = GUARD.verify(binding(
        fixture, root_path=str(detached), worktree_path=str(detached)
    ))
    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.DETACHED_HEAD


def test_missing_tracking_ref_is_blocked(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    result = GUARD.verify(binding(fixture, tracking_ref="origin/does-not-exist"))
    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.TRACKING_REF_MISSING


def test_behind_tracking_ref_recommends_safe_fast_forward_only(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    local_head = subprocess.run(
        ["git", "-C", str(fixture.worktree_current), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    advance_remote(fixture, "feature/current", marker="behind")
    fetch(fixture)

    result = GUARD.verify(binding(fixture))
    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.BEHIND_TRACKING_REF
    assert result.recommended_action is WorkspaceAction.SAFE_FAST_FORWARD
    assert result.actual["tracking_relationship"] == "BEHIND"
    # Guard is read-only: local HEAD did not move.
    assert subprocess.run(
        ["git", "-C", str(fixture.worktree_current), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip() == local_head


def test_diverged_branch_is_blocked(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    local_commit(fixture, "local")
    advance_remote(fixture, "feature/current", marker="remote")
    fetch(fixture)
    result = GUARD.verify(binding(fixture))
    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.BRANCH_DIVERGED
    assert result.actual["tracking_relationship"] == "DIVERGED"


def test_baseline_advanced_is_truthful_not_rejected(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    tracking = advance_remote(fixture, "feature/current", marker="advanced")
    fetch(fixture)
    subprocess.run(
        ["git", "-C", str(fixture.worktree_current), "reset", "--hard", "origin/feature/current"],
        capture_output=True, check=True,
    )
    result = GUARD.verify(binding(fixture))
    assert result.status is WorkspaceStatus.VERIFIED
    assert result.actual["head"] == tracking
    assert "BASELINE_ADVANCED" in result.warnings


def test_guard_is_byte_for_byte_read_only(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    before_status = status_bytes(fixture.worktree_current)
    before_files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in fixture.worktree_current.iterdir() if path.is_file()
    }
    GUARD.verify(binding(fixture))
    GUARD.verify(binding(fixture, branch="feature/old"))
    assert status_bytes(fixture.worktree_current) == before_status
    after_files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in fixture.worktree_current.iterdir() if path.is_file()
    }
    assert after_files == before_files


def test_paths_are_canonicalized(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    link = tmp_path / "linked-worktree"
    link.symlink_to(fixture.worktree_current)
    dotdot = f"{fixture.worktree_current}/../wt-current"
    for root in (link, dotdot):
        result = GUARD.verify(binding(fixture, root_path=str(root), worktree_path=str(root)))
        assert result.status is WorkspaceStatus.VERIFIED
        assert Path(result.actual["worktree_path"]) == fixture.worktree_current.resolve()


def test_remote_credentials_are_redacted(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    subprocess.run(
        ["git", "-C", str(fixture.repo), "remote", "set-url", "origin",
         "https://user:sekret-token@example.invalid/repo.git"],
        capture_output=True, check=True,
    )
    result = GUARD.verify(binding(fixture))
    rendered = str(result.to_dict())
    assert "sekret-token" not in rendered
    assert "[REDACTED]@" in rendered


def test_non_git_directory_is_not_a_workspace(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    fixture_binding = WorkspaceBinding(
        workspace_id="ws-plain",
        root_path=str(plain),
        worktree_path=str(plain),
        branch="main",
    )
    result = GUARD.verify(fixture_binding)
    assert result.status is WorkspaceStatus.BLOCKED
    assert result.reason is WorkspaceReason.NOT_GIT_WORKSPACE
