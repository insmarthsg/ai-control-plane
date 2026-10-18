"""Temporary multi-worktree Git fixture (no real project data)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


@dataclass
class GitFixture:
    root: Path
    repo: Path
    remote: Path
    worktree_current: Path
    worktree_old: Path
    baseline: str


def build(tmp_path: Path) -> GitFixture:
    root = tmp_path / "fixture"
    root.mkdir(parents=True)
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")

    remote = root / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    _git(repo, "checkout", "-b", "feature/current")
    (repo / "current.txt").write_text("current\n")
    _git(repo, "add", "current.txt")
    _git(repo, "commit", "-m", "current work")
    _git(repo, "push", "-u", "origin", "feature/current")
    baseline = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-b", "feature/old", "main")
    (repo / "old.txt").write_text("old\n")
    _git(repo, "add", "old.txt")
    _git(repo, "commit", "-m", "old work")
    _git(repo, "push", "-u", "origin", "feature/old")
    _git(repo, "checkout", "main")

    worktree_current = root / "wt-current"
    worktree_old = root / "wt-old"
    _git(repo, "worktree", "add", str(worktree_current), "feature/current")
    _git(repo, "worktree", "add", str(worktree_old), "feature/old")
    # The wrong worktree is intentionally dirty; no test may clean it.
    (worktree_old / "dirty.txt").write_text("dirty local work\n")

    return GitFixture(root, repo, remote, worktree_current, worktree_old, baseline)


def advance_remote(fixture: GitFixture, branch: str, *, marker: str) -> str:
    """Push one new commit to *branch* through a separate clone."""

    clone = fixture.root / f"pusher-{marker}"
    subprocess.run(["git", "clone", "--branch", branch, str(fixture.remote), str(clone)],
                   capture_output=True, check=True)
    _git(clone, "config", "user.email", "fixture@example.invalid")
    _git(clone, "config", "user.name", "Fixture")
    (clone / f"{marker}.txt").write_text(f"{marker}\n")
    _git(clone, "add", f"{marker}.txt")
    _git(clone, "commit", "-m", marker)
    _git(clone, "push", "origin", branch)
    return _git(clone, "rev-parse", "HEAD")


def fetch(fixture: GitFixture) -> None:
    _git(fixture.worktree_current, "fetch", "origin")


def local_commit(fixture: GitFixture, marker: str) -> str:
    path = fixture.worktree_current / f"local-{marker}.txt"
    path.write_text(f"{marker}\n")
    _git(fixture.worktree_current, "add", path.name)
    _git(fixture.worktree_current, "commit", "-m", marker)
    return _git(fixture.worktree_current, "rev-parse", "HEAD")
