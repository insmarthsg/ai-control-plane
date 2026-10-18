"""Task State domain model.

Task State records facts: identity, lifecycle, durable context, artifacts,
the execution cursor, interruptions, and temporary insertion frames. It does
not decide what runs next, and it does not encode step ordering.

The model is storage-agnostic: it imports no filesystem or harness
module. Persistence lives in ``stores/``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "Artifact",
    "CapabilityActivity",
    "ExecutionCursor",
    "InsertionFrame",
    "Interruption",
    "LifecycleStatus",
    "TaskEvent",
    "TaskState",
    "utc_now",
]


def utc_now() -> str:
    """UTC timestamp used for all persisted task fields."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id() -> str:
    return str(uuid.uuid4())


class LifecycleStatus(str, Enum):
    """Generic task lifecycle. Not tied to a particular workflow."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset(
    {LifecycleStatus.COMPLETED, LifecycleStatus.FAILED, LifecycleStatus.CANCELLED}
)

_ALLOWED_TRANSITIONS: dict[LifecycleStatus, frozenset[LifecycleStatus]] = {
    LifecycleStatus.CREATED: frozenset({LifecycleStatus.RUNNING, LifecycleStatus.CANCELLED}),
    LifecycleStatus.RUNNING: frozenset(
        {
            LifecycleStatus.PAUSED,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELLED,
        }
    ),
    LifecycleStatus.PAUSED: frozenset({LifecycleStatus.RUNNING, LifecycleStatus.CANCELLED}),
    LifecycleStatus.BLOCKED: frozenset({LifecycleStatus.RUNNING, LifecycleStatus.CANCELLED}),
    LifecycleStatus.COMPLETED: frozenset(),
    LifecycleStatus.FAILED: frozenset(),
    LifecycleStatus.CANCELLED: frozenset(),
}


def can_transition(current: LifecycleStatus, target: LifecycleStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


@dataclass(frozen=True)
class CapabilityActivity:
    """Opaque identity of the capability currently handling the task."""

    capability_id: str
    capability_kind: str


@dataclass(frozen=True)
class ExecutionCursor:
    """Generic resume information for a capability.

    ``checkpoint`` and ``continuation_ref`` are opaque to the control plane.
    A harness session id may appear only as a continuation reference; the core
    never interprets it.
    """

    checkpoint: str
    capability_id: str | None = None
    continuation_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Artifact:
    """A durable, handable output. Small payloads inline; large ones by reference."""

    kind: str
    producer_capability: str
    payload: Any = None
    reference: str | None = None
    artifact_id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Interruption:
    """Neutral pause record; not a provider/execution error taxonomy."""

    reason: str
    resume_point: ExecutionCursor | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    interruption_id: str = field(default_factory=_new_id)
    interrupted_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class InsertionFrame:
    """One temporary capability insertion.

    Frames form a stack, so nested insertions need no schema change. The frame
    remembers the cursor and active capability to restore when it completes.
    """

    capability: CapabilityActivity
    reason: str
    input: Mapping[str, Any] = field(default_factory=dict)
    return_cursor: ExecutionCursor | None = None
    return_capability: CapabilityActivity | None = None
    insertion_id: str = field(default_factory=_new_id)
    pushed_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class TaskEvent:
    """Transition history entry: what changed, when, by whom, and why."""

    seq: int
    type: str
    actor: str
    revision: int
    data: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=_new_id)
    at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class TaskState:
    task_id: str
    objective: str
    lifecycle_status: LifecycleStatus = LifecycleStatus.CREATED
    task_input: Mapping[str, Any] = field(default_factory=dict)
    durable_context: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    active_capability: CapabilityActivity | None = None
    cursor: ExecutionCursor | None = None
    interruption: Interruption | None = None
    insertions: tuple[InsertionFrame, ...] = ()
    revision: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def is_terminal(self) -> bool:
        return self.lifecycle_status in TERMINAL_STATUSES

    @property
    def insertion_depth(self) -> int:
        return len(self.insertions)
