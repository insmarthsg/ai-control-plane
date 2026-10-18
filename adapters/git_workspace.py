"""Read-only Git workspace verification.

All git calls are read-only (`rev-parse`, `status`, `worktree list`,
`merge-base`, `remote get-url`) with optional locks disabled. The guard never
checks out, stashes, resets, cleans, pulls, merges, or creates worktrees.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from core.workspace import (
    WorkspaceAction,
    WorkspaceBinding,
    WorkspaceReason,
    WorkspaceStatus,
    WorkspaceVerification,
)

__all__ = ["GitWorkspaceGuard"]


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            return urlunsplit((parts.scheme, f"[REDACTED]@{host}", parts.path, parts.query, ""))
    except ValueError:
        pass
    return re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[REDACTED]@", url)


class GitWorkspaceGuard:
    def __init__(self, *, timeout: float = 15.0) -> None:
        self._timeout = float(timeout)

    def verify(self, binding: WorkspaceBinding) -> WorkspaceVerification:
        expected = {
            "workspace_id": binding.workspace_id,
            "repository_root": binding.repository_root,
            "worktree_path": binding.worktree_path or binding.root_path,
            "branch": binding.branch,
            "baseline_commit": binding.baseline_commit,
            "tracking_ref": binding.tracking_ref,
            "policy": {"read_only": binding.policy.read_only},
        }
        root = Path(binding.root_path).expanduser()
        try:
            resolved = root.resolve(strict=True)
        except (OSError, RuntimeError):
            return self._blocked(binding, WorkspaceReason.WORKSPACE_NOT_FOUND, expected, {
                "root_path": str(root),
            })

        if binding.kind != "git":
            return self._blocked(binding, WorkspaceReason.UNKNOWN, expected, {
                "root_path": str(resolved), "kind": binding.kind,
            })

        worktree_raw = self._git(resolved, "rev-parse", "--show-toplevel")
        if worktree_raw is None:
            return self._blocked(binding, WorkspaceReason.NOT_GIT_WORKSPACE, expected, {
                "root_path": str(resolved),
            })
        worktree = Path(worktree_raw).resolve()
        common_raw = self._git(worktree, "rev-parse", "--git-common-dir")
        common = (worktree / common_raw).resolve() if common_raw else None
        repository = (common.parent if common and common.name == ".git" else common)

        branch = self._git(worktree, "symbolic-ref", "--short", "-q", "HEAD")
        head = self._git(worktree, "rev-parse", "HEAD")
        status = self._git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
        dirty = bool(status)
        registered = self._worktrees(worktree)

        tracking_head = None
        tracking_present = None
        if binding.tracking_ref:
            tracking_head = self._git(
                worktree, "rev-parse", "--verify", "--quiet", f"{binding.tracking_ref}^{{commit}}"
            )
            tracking_present = tracking_head is not None

        remote_url = self._git(worktree, "remote", "get-url", "origin")
        if remote_url:
            remote_url = _redact_url(remote_url)

        baseline_relationship = "UNKNOWN"
        if binding.baseline_commit and head:
            if head == binding.baseline_commit:
                baseline_relationship = "EXACT"
            elif self._is_ancestor(worktree, binding.baseline_commit, head):
                baseline_relationship = "AHEAD"
            elif self._is_ancestor(worktree, head, binding.baseline_commit):
                baseline_relationship = "BEHIND"
            else:
                baseline_relationship = "DIVERGED"

        tracking_relationship = "UNKNOWN"
        if tracking_head and head:
            if head == tracking_head:
                tracking_relationship = "UP_TO_DATE"
            elif self._is_ancestor(worktree, head, tracking_head):
                tracking_relationship = "BEHIND"
            elif self._is_ancestor(worktree, tracking_head, head):
                tracking_relationship = "AHEAD"
            else:
                tracking_relationship = "DIVERGED"

        actual = {
            "root_path": str(resolved),
            "worktree_path": str(worktree),
            "repository_root": str(repository) if repository else None,
            "branch": branch,
            "head": head,
            "dirty": dirty,
            "dirty_entries": status.count("\x00") if status else 0,
            "worktree_registered": registered,
            "tracking_ref": binding.tracking_ref,
            "tracking_present": tracking_present,
            "tracking_head": tracking_head,
            "baseline_relationship": baseline_relationship,
            "tracking_relationship": tracking_relationship,
            "remote_url": remote_url,
        }

        if head is None or status is None:
            return self._blocked(binding, WorkspaceReason.UNKNOWN, expected, actual,
                                 mismatches=["git_facts_unavailable"])

        if binding.worktree_path and worktree != Path(binding.worktree_path).expanduser().resolve():
            return self._blocked(binding, WorkspaceReason.WORKTREE_MISMATCH, expected, actual,
                                 mismatches=["worktree_path"])
        if binding.repository_root and repository != Path(binding.repository_root).expanduser().resolve():
            return self._blocked(binding, WorkspaceReason.REPOSITORY_MISMATCH, expected, actual,
                                 mismatches=["repository_root"])
        if not registered:
            return self._blocked(binding, WorkspaceReason.WORKTREE_NOT_REGISTERED, expected, actual,
                                 mismatches=["worktree_registered"])
        if branch is None:
            return self._blocked(binding, WorkspaceReason.DETACHED_HEAD, expected, actual,
                                 mismatches=["branch"])
        if binding.branch and branch != binding.branch:
            return self._blocked(binding, WorkspaceReason.BRANCH_MISMATCH, expected, actual,
                                 mismatches=["branch"])
        if dirty:
            return self._blocked(binding, WorkspaceReason.WORKSPACE_DIRTY, expected, actual,
                                 mismatches=["dirty"])

        warnings: list[str] = []
        if baseline_relationship == "BEHIND":
            return self._blocked(binding, WorkspaceReason.HEAD_MISMATCH, expected, actual,
                                 mismatches=["head"])
        if baseline_relationship == "DIVERGED":
            return self._blocked(binding, WorkspaceReason.HEAD_MISMATCH, expected, actual,
                                 mismatches=["head"])
        if binding.baseline_commit and tracking_head and tracking_head != binding.baseline_commit:
            warnings.append("BASELINE_ADVANCED")
        if tracking_relationship == "AHEAD":
            warnings.append("LOCAL_AHEAD_OF_TRACKING")

        if binding.tracking_ref and tracking_head is None:
            if binding.policy.require_tracking_ref:
                return self._blocked(binding, WorkspaceReason.TRACKING_REF_MISSING, expected, actual,
                                     mismatches=["tracking_ref"], warnings=warnings)
            warnings.append("TRACKING_REF_MISSING")
        if tracking_relationship == "BEHIND":
            return self._blocked(
                binding, WorkspaceReason.BEHIND_TRACKING_REF, expected, actual,
                mismatches=["tracking_head"], warnings=warnings,
                action=WorkspaceAction.SAFE_FAST_FORWARD,
            )
        if tracking_relationship == "DIVERGED":
            return self._blocked(binding, WorkspaceReason.BRANCH_DIVERGED, expected, actual,
                                 mismatches=["tracking_head"], warnings=warnings)

        return WorkspaceVerification(
            workspace_id=binding.workspace_id,
            status=WorkspaceStatus.VERIFIED,
            reason=WorkspaceReason.OK,
            expected=expected,
            actual=actual,
            mismatches=(),
            warnings=tuple(warnings),
            recommended_action=WorkspaceAction.NONE,
            verified_at=_utcnow(),
        )

    # ---------------------------------------------------------------- helpers

    def _blocked(
        self,
        binding: WorkspaceBinding,
        reason: WorkspaceReason,
        expected: dict,
        actual: dict,
        *,
        mismatches: list[str] | None = None,
        warnings: list[str] | None = None,
        action: WorkspaceAction = WorkspaceAction.MANUAL_REVIEW,
    ) -> WorkspaceVerification:
        return WorkspaceVerification(
            workspace_id=binding.workspace_id,
            status=WorkspaceStatus.BLOCKED,
            reason=reason,
            expected=expected,
            actual=actual,
            mismatches=tuple(mismatches or []),
            warnings=tuple(warnings or []),
            recommended_action=action,
            verified_at=_utcnow(),
        )

    def _git(self, cwd: Path, *args: str) -> str | None:
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    def _is_ancestor(self, cwd: Path, ancestor: str, descendant: str) -> bool:
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), "merge-base", "--is-ancestor", ancestor, descendant],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def _worktrees(self, worktree: Path) -> bool:
        raw = self._git(worktree, "worktree", "list", "--porcelain")
        if not raw:
            return False
        target = str(worktree)
        for line in raw.splitlines():
            if line.startswith("worktree "):
                if str(Path(line[len("worktree "):]).resolve()) == target:
                    return True
        return False
