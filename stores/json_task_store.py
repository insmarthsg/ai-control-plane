"""JSON-file Task State Store.

One document per task, written atomically (temp file + fsync + replace), with
per-task advisory file locking and revision-checked updates.

Why JSON files here:
- inspectable without tooling, trivially testable across processes
- no schema/migration machinery at this scale
- atomic replace gives crash safety on a single local filesystem
- SQLite would add a second query/serialization layer with no current need

The domain model and the ``TaskStateStore`` Protocol do not import this module.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

try:  # POSIX advisory locks; macOS/Linux in practice.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from core.redaction import redact, redact_data
from core.task import (
    Artifact,
    CapabilityActivity,
    ExecutionCursor,
    InsertionFrame,
    Interruption,
    LifecycleStatus,
    TaskEvent,
    TaskState,
    can_transition,
    utc_now,
)
from core.task_store import (
    CorruptedTaskState,
    InvalidTaskState,
    InvalidTransition,
    RevisionConflict,
    TaskNotFound,
    TaskStoreError,
)

SCHEMA_VERSION = 1
_TERMINAL = frozenset(
    {LifecycleStatus.COMPLETED, LifecycleStatus.FAILED, LifecycleStatus.CANCELLED}
)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

_EVENT_BY_STATUS = {
    LifecycleStatus.RUNNING: "TASK_STARTED",
    LifecycleStatus.PAUSED: "TASK_PAUSED",
    LifecycleStatus.BLOCKED: "TASK_BLOCKED",
    LifecycleStatus.COMPLETED: "TASK_COMPLETED",
    LifecycleStatus.FAILED: "TASK_FAILED",
    LifecycleStatus.CANCELLED: "TASK_CANCELLED",
}


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write *payload* so readers see either the old file or the new one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:  # pragma: no cover - platform without dir fsync
            pass
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        raise InvalidTaskState(f"unsafe task_id: {task_id!r}")


def _sanitize_capability(value: CapabilityActivity) -> CapabilityActivity:
    return CapabilityActivity(
        capability_id=redact(value.capability_id),
        capability_kind=redact(value.capability_kind),
    )


def _sanitize_cursor(value: ExecutionCursor | None) -> ExecutionCursor | None:
    if value is None:
        return None
    return ExecutionCursor(
        checkpoint=redact(value.checkpoint),
        capability_id=redact(value.capability_id) if value.capability_id else None,
        continuation_ref=redact(value.continuation_ref) if value.continuation_ref else None,
        metadata=redact_data(value.metadata),
    )


def _sanitize_artifact(value: Artifact) -> Artifact:
    return Artifact(
        kind=redact(value.kind),
        producer_capability=redact(value.producer_capability),
        payload=redact_data(value.payload),
        reference=redact(value.reference) if value.reference else None,
        artifact_id=value.artifact_id,
        created_at=value.created_at,
        metadata=redact_data(value.metadata),
    )


def _sanitize_interruption(value: Interruption) -> Interruption:
    return Interruption(
        reason=redact(value.reason),
        resume_point=_sanitize_cursor(value.resume_point),
        evidence=redact_data(value.evidence),
        interruption_id=value.interruption_id,
        interrupted_at=value.interrupted_at,
    )


def _sanitize_frame(value: InsertionFrame) -> InsertionFrame:
    return InsertionFrame(
        capability=_sanitize_capability(value.capability),
        reason=redact(value.reason),
        input=redact_data(value.input),
        return_cursor=_sanitize_cursor(value.return_cursor),
        return_capability=(
            _sanitize_capability(value.return_capability)
            if value.return_capability is not None
            else None
        ),
        insertion_id=value.insertion_id,
        pushed_at=value.pushed_at,
    )


class JSONTaskStateStore:
    """Persistent Task State Store backed by one JSON document per task."""

    def __init__(self, root: str | os.PathLike[str], *, clock: Callable[[], str] = utc_now) -> None:
        self._root = Path(root)
        self._clock = clock
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ paths

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, task_id: str) -> Path:
        _validate_task_id(task_id)
        return self._root / f"{task_id}.json"

    def _lock_path(self, task_id: str) -> Path:
        _validate_task_id(task_id)
        return self._root / f".{task_id}.lock"

    def _thread_lock(self, task_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(task_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[task_id] = lock
            return lock

    @contextmanager
    def _exclusive(self, task_id: str) -> Iterator[None]:
        with self._thread_lock(task_id):
            self._root.mkdir(parents=True, exist_ok=True)
            with self._lock_path(task_id).open("a+") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------ create

    def create_task(
        self,
        *,
        objective: str,
        task_input: Mapping[str, Any] | None = None,
        durable_context: Mapping[str, Any] | None = None,
        actor: str = "control-plane",
    ) -> TaskState:
        if not isinstance(objective, str) or not objective.strip():
            raise InvalidTaskState("objective must be a non-empty string")

        task_id = str(uuid.uuid4())
        now = self._clock()
        task = TaskState(
            task_id=task_id,
            objective=redact(objective.strip()),
            task_input=redact_data(dict(task_input or {})),
            durable_context=redact_data(dict(durable_context or {})),
            created_at=now,
            updated_at=now,
            revision=0,
        )
        event = TaskEvent(
            seq=1,
            type="TASK_CREATED",
            actor=redact(actor),
            revision=0,
            at=now,
            data={"lifecycle_status": LifecycleStatus.CREATED.value},
        )
        document = {
            "schema_version": SCHEMA_VERSION,
            "task": self._task_to_doc(task),
            "events": [self._event_to_doc(event)],
        }
        with self._exclusive(task_id):
            if self._path(task_id).exists():  # pragma: no cover - UUID collision
                raise TaskStoreError(f"task already exists: {task_id}")
            self._write(task_id, document)
        return task

    # -------------------------------------------------------------------- read

    def get_task(self, task_id: str) -> TaskState:
        document = self._load_document(task_id)
        return self._task_from_doc(document["task"])

    def list_tasks(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(
            path.stem
            for path in self._root.glob("*.json")
            if _TASK_ID_RE.match(path.stem)
        )

    def list_events(self, task_id: str) -> tuple[TaskEvent, ...]:
        document = self._load_document(task_id)
        return tuple(self._event_from_doc(item) for item in document["events"])

    # --------------------------------------------------------------- mutations

    def set_lifecycle(
        self,
        task_id: str,
        status: LifecycleStatus,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState:
        def apply(task: TaskState) -> tuple[TaskState, str, dict[str, Any]]:
            if not can_transition(task.lifecycle_status, status):
                raise InvalidTransition(task_id, task.lifecycle_status, status)
            event_type = _EVENT_BY_STATUS.get(status, "TASK_UPDATED")
            data = {"from": task.lifecycle_status.value, "to": status.value}
            updated = replace(task, lifecycle_status=status)
            if status in _TERMINAL:
                updated = replace(updated, interruption=None)
            return updated, event_type, data

        return self._mutate(task_id, expected_revision, apply, actor)

    def set_durable_context(
        self,
        task_id: str,
        durable_context: Mapping[str, Any],
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState:
        def apply(task: TaskState) -> tuple[TaskState, str, dict[str, Any]]:
            cleaned = redact_data(dict(durable_context))
            return replace(task, durable_context=cleaned), "CONTEXT_UPDATED", {
                "keys": sorted(cleaned.keys())
            }

        return self._mutate(task_id, expected_revision, apply, actor)

    def set_active_capability(
        self,
        task_id: str,
        capability: CapabilityActivity,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState:
        cleaned = _sanitize_capability(capability)
        if not cleaned.capability_id:
            raise InvalidTaskState("capability_id is required")

        def apply(task: TaskState) -> tuple[TaskState, str, dict[str, Any]]:
            if task.is_terminal:
                raise InvalidTaskState(f"cannot activate capability on terminal task {task_id}")
            updated = replace(task, active_capability=cleaned)
            return updated, "CAPABILITY_ACTIVATED", {
                "capability_id": cleaned.capability_id,
                "capability_kind": cleaned.capability_kind,
            }

        return self._mutate(task_id, expected_revision, apply, actor)

    def add_artifact(
        self,
        task_id: str,
        artifact: Artifact,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState:
        cleaned = _sanitize_artifact(artifact)
        if not cleaned.kind or not cleaned.producer_capability:
            raise InvalidTaskState("artifact.kind and artifact.producer_capability are required")

        def apply(task: TaskState) -> tuple[TaskState, str, dict[str, Any]]:
            if task.is_terminal:
                raise InvalidTaskState(f"cannot add artifact to terminal task {task_id}")
            updated = replace(task, artifacts=task.artifacts + (cleaned,))
            return updated, "ARTIFACT_ADDED", {
                "artifact_id": cleaned.artifact_id,
                "kind": cleaned.kind,
                "producer_capability": cleaned.producer_capability,
            }

        return self._mutate(task_id, expected_revision, apply, actor)

    def save_checkpoint(
        self,
        task_id: str,
        cursor: ExecutionCursor,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState:
        cleaned = _sanitize_cursor(cursor)
        assert cleaned is not None

        def apply(task: TaskState) -> tuple[TaskState, str, dict[str, Any]]:
            if task.is_terminal:
                raise InvalidTaskState(f"cannot checkpoint terminal task {task_id}")
            updated = replace(task, cursor=cleaned)
            return updated, "CHECKPOINT_SAVED", {
                "checkpoint": cleaned.checkpoint,
                "capability_id": cleaned.capability_id,
            }

        return self._mutate(task_id, expected_revision, apply, actor)

    def interrupt(
        self,
        task_id: str,
        interruption: Interruption,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState:
        cleaned = _sanitize_interruption(interruption)

        def apply(task: TaskState) -> tuple[TaskState, str, dict[str, Any]]:
            if task.lifecycle_status is not LifecycleStatus.RUNNING:
                raise InvalidTaskState(
                    f"interrupt requires RUNNING, current {task.lifecycle_status.value}"
                )
            updated = replace(
                task,
                lifecycle_status=LifecycleStatus.PAUSED,
                interruption=cleaned,
            )
            return updated, "TASK_INTERRUPTED", {
                "reason": cleaned.reason,
                "checkpoint": cleaned.resume_point.checkpoint if cleaned.resume_point else None,
            }

        return self._mutate(task_id, expected_revision, apply, actor)

    def resume(
        self,
        task_id: str,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState:
        def apply(task: TaskState) -> tuple[TaskState, str, dict[str, Any]]:
            if task.lifecycle_status not in {LifecycleStatus.PAUSED, LifecycleStatus.BLOCKED}:
                raise InvalidTaskState(
                    f"resume requires PAUSED or BLOCKED, current {task.lifecycle_status.value}"
                )
            previous = task.interruption.reason if task.interruption else None
            updated = replace(
                task,
                lifecycle_status=LifecycleStatus.RUNNING,
                interruption=None,
            )
            return updated, "TASK_RESUMED", {"interruption_reason": previous}

        return self._mutate(task_id, expected_revision, apply, actor)

    def push_insertion(
        self,
        task_id: str,
        frame: InsertionFrame,
        *,
        expected_revision: int,
        actor: str = "unknown",
    ) -> TaskState:
        cleaned = _sanitize_frame(frame)
        if not cleaned.capability.capability_id:
            raise InvalidTaskState("insertion capability_id is required")

        def apply(task: TaskState) -> tuple[TaskState, str, dict[str, Any]]:
            if task.is_terminal:
                raise InvalidTaskState(f"cannot insert into terminal task {task_id}")
            if task.lifecycle_status is not LifecycleStatus.RUNNING:
                raise InvalidTaskState(
                    f"insertion requires RUNNING, current {task.lifecycle_status.value}"
                )
            bounded = replace(
                cleaned,
                return_cursor=task.cursor,
                return_capability=task.active_capability,
            )
            updated = replace(
                task,
                insertions=task.insertions + (bounded,),
                active_capability=bounded.capability,
                cursor=None,
            )
            return updated, "INSERTION_PUSHED", {
                "insertion_id": bounded.insertion_id,
                "capability_id": bounded.capability.capability_id,
                "reason": bounded.reason,
                "depth": len(updated.insertions),
            }

        return self._mutate(task_id, expected_revision, apply, actor)

    def complete_insertion(
        self,
        task_id: str,
        *,
        expected_revision: int,
        actor: str = "unknown",
        result: Artifact | None = None,
    ) -> TaskState:
        cleaned_result = _sanitize_artifact(result) if result is not None else None
        if cleaned_result is not None and (
            not cleaned_result.kind or not cleaned_result.producer_capability
        ):
            raise InvalidTaskState("result artifact requires kind and producer_capability")

        def apply(task: TaskState) -> tuple[TaskState, str, dict[str, Any]]:
            if not task.insertions:
                raise InvalidTaskState(f"no active insertion on task {task_id}")
            frame = task.insertions[-1]
            remaining = task.insertions[:-1]
            artifacts = task.artifacts + ((cleaned_result,) if cleaned_result is not None else ())
            updated = replace(
                task,
                insertions=remaining,
                artifacts=artifacts,
                active_capability=frame.return_capability,
                cursor=frame.return_cursor,
            )
            return updated, "INSERTION_COMPLETED", {
                "insertion_id": frame.insertion_id,
                "capability_id": frame.capability.capability_id,
                "result_artifact_id": cleaned_result.artifact_id if cleaned_result else None,
                "depth": len(remaining),
            }

        return self._mutate(task_id, expected_revision, apply, actor)

    # --------------------------------------------------------------- internals

    def _mutate(
        self,
        task_id: str,
        expected_revision: int,
        apply: Callable[[TaskState], tuple[TaskState, str, dict[str, Any]]],
        actor: str,
    ) -> TaskState:
        _validate_task_id(task_id)
        with self._exclusive(task_id):
            document = self._load_document(task_id)
            current = self._task_from_doc(document["task"])
            if current.revision != expected_revision:
                raise RevisionConflict(task_id, expected_revision, current.revision)

            updated, event_type, event_data = apply(current)
            now = self._clock()
            updated = replace(updated, revision=current.revision + 1, updated_at=now)
            event = TaskEvent(
                seq=len(document["events"]) + 1,
                type=event_type,
                actor=redact(actor),
                revision=updated.revision,
                at=now,
                data=redact_data(event_data),
            )
            document["task"] = self._task_to_doc(updated)
            document["events"].append(self._event_to_doc(event))
            self._write(task_id, document)
            return updated

    def _write(self, task_id: str, document: Mapping[str, Any]) -> None:
        try:
            payload = json.dumps(
                document, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
        except TypeError as exc:
            raise InvalidTaskState(f"task {task_id} contains non-JSON data: {exc}") from exc
        try:
            _atomic_write(self._path(task_id), payload + b"\n")
        except OSError as exc:
            raise TaskStoreError(f"failed to persist task {task_id}: {exc}") from exc

    def _load_document(self, task_id: str) -> dict[str, Any]:
        path = self._path(task_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise TaskNotFound(task_id) from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorruptedTaskState(f"task {task_id} is not valid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise CorruptedTaskState(f"task {task_id} document is not an object")
        if document.get("schema_version") != SCHEMA_VERSION:
            raise CorruptedTaskState(
                f"task {task_id} has unsupported schema_version {document.get('schema_version')!r}"
            )
        if not isinstance(document.get("task"), dict) or not isinstance(document.get("events"), list):
            raise CorruptedTaskState(f"task {task_id} document is incomplete")
        return document

    # ---------------------------------------------------------- serialization

    @staticmethod
    def _task_to_doc(task: TaskState) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "objective": task.objective,
            "lifecycle_status": task.lifecycle_status.value,
            "task_input": dict(task.task_input),
            "durable_context": dict(task.durable_context),
            "artifacts": [JSONTaskStateStore._artifact_to_doc(item) for item in task.artifacts],
            "active_capability": (
                JSONTaskStateStore._capability_to_doc(task.active_capability)
                if task.active_capability
                else None
            ),
            "cursor": JSONTaskStateStore._cursor_to_doc(task.cursor),
            "interruption": (
                JSONTaskStateStore._interruption_to_doc(task.interruption)
                if task.interruption
                else None
            ),
            "insertions": [JSONTaskStateStore._frame_to_doc(item) for item in task.insertions],
            "revision": task.revision,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    @staticmethod
    def _task_from_doc(document: Mapping[str, Any]) -> TaskState:
        try:
            status = LifecycleStatus(document["lifecycle_status"])
            return TaskState(
                task_id=str(document["task_id"]),
                objective=str(document["objective"]),
                lifecycle_status=status,
                task_input=dict(document.get("task_input") or {}),
                durable_context=dict(document.get("durable_context") or {}),
                artifacts=tuple(
                    JSONTaskStateStore._artifact_from_doc(item)
                    for item in document.get("artifacts") or ()
                ),
                active_capability=(
                    JSONTaskStateStore._capability_from_doc(document["active_capability"])
                    if document.get("active_capability")
                    else None
                ),
                cursor=(
                    JSONTaskStateStore._cursor_from_doc(document["cursor"])
                    if document.get("cursor")
                    else None
                ),
                interruption=(
                    JSONTaskStateStore._interruption_from_doc(document["interruption"])
                    if document.get("interruption")
                    else None
                ),
                insertions=tuple(
                    JSONTaskStateStore._frame_from_doc(item)
                    for item in document.get("insertions") or ()
                ),
                revision=int(document["revision"]),
                created_at=str(document.get("created_at") or ""),
                updated_at=str(document.get("updated_at") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptedTaskState(f"invalid task document: {exc}") from exc

    @staticmethod
    def _capability_to_doc(value: CapabilityActivity) -> dict[str, Any]:
        return {"capability_id": value.capability_id, "capability_kind": value.capability_kind}

    @staticmethod
    def _capability_from_doc(document: Mapping[str, Any]) -> CapabilityActivity:
        return CapabilityActivity(
            capability_id=str(document["capability_id"]),
            capability_kind=str(document["capability_kind"]),
        )

    @staticmethod
    def _cursor_to_doc(value: ExecutionCursor | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            "capability_id": value.capability_id,
            "checkpoint": value.checkpoint,
            "continuation_ref": value.continuation_ref,
            "metadata": dict(value.metadata),
        }

    @staticmethod
    def _cursor_from_doc(document: Mapping[str, Any]) -> ExecutionCursor:
        return ExecutionCursor(
            checkpoint=str(document.get("checkpoint") or ""),
            capability_id=document.get("capability_id"),
            continuation_ref=document.get("continuation_ref"),
            metadata=dict(document.get("metadata") or {}),
        )

    @staticmethod
    def _artifact_to_doc(value: Artifact) -> dict[str, Any]:
        return {
            "artifact_id": value.artifact_id,
            "kind": value.kind,
            "producer_capability": value.producer_capability,
            "payload": value.payload,
            "reference": value.reference,
            "created_at": value.created_at,
            "metadata": dict(value.metadata),
        }

    @staticmethod
    def _artifact_from_doc(document: Mapping[str, Any]) -> Artifact:
        return Artifact(
            artifact_id=str(document["artifact_id"]),
            kind=str(document["kind"]),
            producer_capability=str(document["producer_capability"]),
            payload=document.get("payload"),
            reference=document.get("reference"),
            created_at=str(document.get("created_at") or ""),
            metadata=dict(document.get("metadata") or {}),
        )

    @staticmethod
    def _interruption_to_doc(value: Interruption) -> dict[str, Any]:
        return {
            "interruption_id": value.interruption_id,
            "reason": value.reason,
            "resume_point": JSONTaskStateStore._cursor_to_doc(value.resume_point),
            "evidence": dict(value.evidence),
            "interrupted_at": value.interrupted_at,
        }

    @staticmethod
    def _interruption_from_doc(document: Mapping[str, Any]) -> Interruption:
        return Interruption(
            interruption_id=str(document["interruption_id"]),
            reason=str(document["reason"]),
            resume_point=(
                JSONTaskStateStore._cursor_from_doc(document["resume_point"])
                if document.get("resume_point")
                else None
            ),
            evidence=dict(document.get("evidence") or {}),
            interrupted_at=str(document.get("interrupted_at") or ""),
        )

    @staticmethod
    def _frame_to_doc(value: InsertionFrame) -> dict[str, Any]:
        return {
            "insertion_id": value.insertion_id,
            "capability": JSONTaskStateStore._capability_to_doc(value.capability),
            "reason": value.reason,
            "input": dict(value.input),
            "return_cursor": JSONTaskStateStore._cursor_to_doc(value.return_cursor),
            "return_capability": (
                JSONTaskStateStore._capability_to_doc(value.return_capability)
                if value.return_capability
                else None
            ),
            "pushed_at": value.pushed_at,
        }

    @staticmethod
    def _frame_from_doc(document: Mapping[str, Any]) -> InsertionFrame:
        return InsertionFrame(
            insertion_id=str(document["insertion_id"]),
            capability=JSONTaskStateStore._capability_from_doc(document["capability"]),
            reason=str(document["reason"]),
            input=dict(document.get("input") or {}),
            return_cursor=(
                JSONTaskStateStore._cursor_from_doc(document["return_cursor"])
                if document.get("return_cursor")
                else None
            ),
            return_capability=(
                JSONTaskStateStore._capability_from_doc(document["return_capability"])
                if document.get("return_capability")
                else None
            ),
            pushed_at=str(document.get("pushed_at") or ""),
        )

    @staticmethod
    def _event_to_doc(value: TaskEvent) -> dict[str, Any]:
        return {
            "event_id": value.event_id,
            "seq": value.seq,
            "type": value.type,
            "actor": value.actor,
            "revision": value.revision,
            "at": value.at,
            "data": dict(value.data),
        }

    @staticmethod
    def _event_from_doc(document: Mapping[str, Any]) -> TaskEvent:
        return TaskEvent(
            event_id=str(document["event_id"]),
            seq=int(document["seq"]),
            type=str(document["type"]),
            actor=str(document["actor"]),
            revision=int(document["revision"]),
            at=str(document.get("at") or ""),
            data=dict(document.get("data") or {}),
        )
