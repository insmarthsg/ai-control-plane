"""Storage-agnostic Task State Store contract.

The domain and callers depend on this Protocol, never on a concrete backend.
A JSON implementation lives in ``stores/json_task_store.py``; a future SQLite
implementation must satisfy the same interface without changing
:mod:`core.task` or its callers.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .task import (
    Artifact,
    CapabilityActivity,
    ExecutionCursor,
    Interruption,
    InsertionFrame,
    LifecycleStatus,
    TaskEvent,
    TaskState,
)

__all__ = [
    "CorruptedTaskState",
    "InvalidTaskState",
    "InvalidTransition",
    "RevisionConflict",
    "TaskNotFound",
    "TaskStateStore",
    "TaskStoreError",
]


class TaskStoreError(RuntimeError):
    """Base class for task-store failures."""


class TaskNotFound(TaskStoreError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"task not found: {task_id}")
        self.task_id = task_id


class RevisionConflict(TaskStoreError):
    """Optimistic concurrency failure: the caller saw a stale revision."""

    def __init__(self, task_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"revision conflict for {task_id}: expected {expected}, current {actual}"
        )
        self.task_id = task_id
        self.expected = expected
        self.actual = actual


class InvalidTransition(TaskStoreError):
    def __init__(self, task_id: str, current: LifecycleStatus, target: LifecycleStatus) -> None:
        super().__init__(f"invalid lifecycle transition for {task_id}: {current.value} -> {target.value}")
        self.task_id = task_id
        self.current = current
        self.target = target


class InvalidTaskState(TaskStoreError):
    """The task data violates a domain or serialization rule."""


class CorruptedTaskState(TaskStoreError):
    """The persisted document cannot be decoded."""


@runtime_checkable
class TaskStateStore(Protocol):
    """Minimal persistent task-continuity interface."""

    def create_task(
        self,
        *,
        objective: str,
        task_input: Mapping[str, Any] | None = None,
        durable_context: Mapping[str, Any] | None = None,
        actor: str = "control-plane",
    ) -> TaskState: ...

    def get_task(self, task_id: str) -> TaskState: ...

    def list_tasks(self) -> list[str]: ...

    def list_events(self, task_id: str) -> tuple[TaskEvent, ...]: ...

    def set_lifecycle(
        self,
        task_id: str,
        status: LifecycleStatus,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState: ...

    def set_durable_context(
        self,
        task_id: str,
        durable_context: Mapping[str, Any],
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState: ...

    def set_active_capability(
        self,
        task_id: str,
        capability: "CapabilityActivity",
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState: ...

    def add_artifact(
        self,
        task_id: str,
        artifact: Artifact,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState: ...

    def save_checkpoint(
        self,
        task_id: str,
        cursor: ExecutionCursor,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState: ...

    def interrupt(
        self,
        task_id: str,
        interruption: Interruption,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState: ...

    def resume(
        self,
        task_id: str,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState: ...

    def push_insertion(
        self,
        task_id: str,
        frame: InsertionFrame,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState: ...

    def complete_insertion(
        self,
        task_id: str,
        *,
        expected_revision: int,
        actor: str = "unknown",
        result: Artifact | None = None,
    ) -> TaskState: ...

    def record_event(
        self,
        task_id: str,
        event_type: str,
        *,
        expected_revision: int,
        data: Mapping[str, Any] | None = None,
        actor: str = "unknown",
    ) -> TaskState:
        """Append one transition event without changing task fields."""
        ...

    def record_execution_result(
        self,
        task_id: str,
        *,
        expected_revision: int,
        artifacts: Sequence[Artifact] = (),
        cursor: ExecutionCursor | None = None,
        active_capability: CapabilityActivity | None = None,
        completed: bool = False,
        event_type: str = "EXECUTION_RESULT",
        event_data: Mapping[str, Any] | None = None,
        actor: str = "unknown",
    ) -> TaskState:
        """Atomically persist artifacts, cursor, active capability, and completion."""
        ...
