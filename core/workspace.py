"""Workspace binding and guard contract.

A Task that operates on project files must be bound to an explicit workspace.
Project name, repository, and branch alone are not identities: the same
repository can have several worktrees with different branches and dirty state.
Execution is allowed only after a read-only guard verifies the expected
binding; the expected facts are never overwritten by the observed ones.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from .redaction import redact_data
from .task import Artifact, TaskState
from .task_store import TaskStateStore

__all__ = [
    "WorkspaceAction",
    "WorkspaceBinding",
    "WorkspaceGuard",
    "WorkspacePolicy",
    "WorkspaceReason",
    "WorkspaceStatus",
    "WorkspaceVerification",
    "binding_for",
    "bind_workspace",
    "record_verification",
]

WORKSPACE_BINDING_KIND = "workspace-binding"


class WorkspaceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"


class WorkspaceReason(str, Enum):
    OK = "OK"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    NOT_GIT_WORKSPACE = "NOT_GIT_WORKSPACE"
    REPOSITORY_MISMATCH = "REPOSITORY_MISMATCH"
    WORKTREE_MISMATCH = "WORKTREE_MISMATCH"
    WORKTREE_NOT_REGISTERED = "WORKTREE_NOT_REGISTERED"
    BRANCH_MISMATCH = "BRANCH_MISMATCH"
    DETACHED_HEAD = "DETACHED_HEAD"
    WORKSPACE_DIRTY = "WORKSPACE_DIRTY"
    HEAD_MISMATCH = "HEAD_MISMATCH"
    BEHIND_TRACKING_REF = "BEHIND_TRACKING_REF"
    BRANCH_DIVERGED = "BRANCH_DIVERGED"
    TRACKING_REF_MISSING = "TRACKING_REF_MISSING"
    WORKSPACE_CHANGED_AFTER_VERIFY = "WORKSPACE_CHANGED_AFTER_VERIFY"
    UNKNOWN = "UNKNOWN"


class WorkspaceAction(str, Enum):
    NONE = "NONE"
    SAFE_FAST_FORWARD = "SAFE_FAST_FORWARD"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass(frozen=True)
class WorkspacePolicy:
    read_only: bool = False
    require_tracking_ref: bool = True


@dataclass(frozen=True)
class WorkspaceBinding:
    root_path: str
    workspace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    kind: str = "git"
    repository_root: str | None = None
    worktree_path: str | None = None
    branch: str | None = None
    baseline_commit: str | None = None
    tracking_ref: str | None = None
    policy: WorkspacePolicy = field(default_factory=WorkspacePolicy)

    def to_payload(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "root_path": self.root_path,
            "repository_root": self.repository_root,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "baseline_commit": self.baseline_commit,
            "tracking_ref": self.tracking_ref,
            "policy": {
                "read_only": self.policy.read_only,
                "require_tracking_ref": self.policy.require_tracking_ref,
            },
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "WorkspaceBinding":
        policy = payload.get("policy") if isinstance(payload.get("policy"), Mapping) else {}
        return cls(
            workspace_id=str(payload["workspace_id"]),
            kind=str(payload.get("kind") or "git"),
            root_path=str(payload["root_path"]),
            repository_root=payload.get("repository_root"),
            worktree_path=payload.get("worktree_path"),
            branch=payload.get("branch"),
            baseline_commit=payload.get("baseline_commit"),
            tracking_ref=payload.get("tracking_ref"),
            policy=WorkspacePolicy(
                read_only=bool(policy.get("read_only", False)),
                require_tracking_ref=bool(policy.get("require_tracking_ref", True)),
            ),
        )


@dataclass(frozen=True)
class WorkspaceVerification:
    workspace_id: str
    status: WorkspaceStatus
    reason: WorkspaceReason
    expected: Mapping[str, Any] = field(default_factory=dict)
    actual: Mapping[str, Any] = field(default_factory=dict)
    mismatches: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    recommended_action: WorkspaceAction = WorkspaceAction.NONE
    verified_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    @property
    def execution_allowed(self) -> bool:
        return self.status is WorkspaceStatus.VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "status": self.status.value,
            "reason": self.reason.value,
            "expected": redact_data(dict(self.expected)),
            "actual": redact_data(dict(self.actual)),
            "mismatches": list(self.mismatches),
            "warnings": list(self.warnings),
            "recommended_action": self.recommended_action.value,
            "execution_allowed": self.execution_allowed,
            "verified_at": self.verified_at,
        }


class WorkspaceGuard(Protocol):
    """Read-only verification seam implemented by infrastructure adapters."""

    def verify(self, binding: WorkspaceBinding) -> WorkspaceVerification: ...


def bind_workspace(
    store: TaskStateStore,
    task_id: str,
    binding: WorkspaceBinding,
    *,
    expected_revision: int | None = None,
    actor: str = "workspace-binder",
) -> TaskState:
    """Persist a binding as an immutable Task artifact plus a creation event."""

    task = store.get_task(task_id)
    revision = task.revision if expected_revision is None else expected_revision
    artifact = Artifact(
        kind=WORKSPACE_BINDING_KIND,
        producer_capability="workspace",
        payload=binding.to_payload(),
    )
    updated = store.add_artifact(
        task_id, artifact, expected_revision=revision, actor=actor
    )
    store.record_event(
        task_id,
        "WORKSPACE_BINDING_CREATED",
        expected_revision=updated.revision,
        data={
            "workspace_id": binding.workspace_id,
            "worktree": binding.worktree_path or binding.root_path,
            "branch": binding.branch,
            "baseline_commit": binding.baseline_commit,
        },
        actor=actor,
    )
    return store.get_task(task_id)


def binding_for(task: TaskState) -> WorkspaceBinding | None:
    latest: WorkspaceBinding | None = None
    for artifact in task.artifacts:
        if artifact.kind != WORKSPACE_BINDING_KIND:
            continue
        payload = artifact.payload if isinstance(artifact.payload, Mapping) else {}
        try:
            latest = WorkspaceBinding.from_payload(payload)
        except (KeyError, TypeError, ValueError):
            continue
    return latest


def record_verification(
    store: TaskStateStore,
    task_id: str,
    verification: WorkspaceVerification,
    *,
    expected_revision: int,
    actor: str = "workspace-guard",
    changed_after_verify: bool = False,
) -> TaskState:
    event = (
        "WORKSPACE_VERIFICATION_FAILED"
        if not verification.execution_allowed
        else "WORKSPACE_VERIFIED"
    )
    data = {
        "workspace_id": verification.workspace_id,
        "status": verification.status.value,
        "reason": verification.reason.value,
        "branch": verification.actual.get("branch"),
        "worktree": verification.actual.get("worktree_path"),
        "head": verification.actual.get("head"),
        "tracking_head": verification.actual.get("tracking_head"),
        "dirty": verification.actual.get("dirty"),
        "changed_after_verify": changed_after_verify,
    }
    return store.record_event(
        task_id, event, expected_revision=expected_revision, data=data, actor=actor
    )
