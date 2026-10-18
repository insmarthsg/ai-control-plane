"""Vendor-neutral runtime control for one execution invocation.

Runtime Control is deliberately separate from Task Control and Task State:

    start(request) -> execution_id          (before completion)
    status(execution_id)                     (live view)
    cancel(execution_id) -> structured result (confirmed stop)

It knows only the Execution Contract and an optional terminate/liveness seam.
No provider, harness, process handle, wire format, or session format appears here.
Runtime state describes one invocation; it never mutates Task lifecycle.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from .execution import (
    ExecutionFailure,
    ExecutionFailureKind,
    ExecutionRequest,
    ExecutionResult,
)
from .redaction import redact

__all__ = [
    "CancelResult",
    "CancelStatus",
    "RuntimeEvent",
    "RuntimeManager",
    "RuntimeRecord",
    "RuntimeStatus",
    "UnknownCapability",
]


class RuntimeStatus(str, Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    LOST = "LOST"

    @property
    def terminal(self) -> bool:
        return self in {
            RuntimeStatus.STOPPED,
            RuntimeStatus.SUCCEEDED,
            RuntimeStatus.FAILED,
            RuntimeStatus.LOST,
        }


class CancelStatus(str, Enum):
    CANCELLED = "CANCELLED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NOT_FOUND = "NOT_FOUND"
    REQUESTED = "REQUESTED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RuntimeEvent:
    type: str
    at: str
    detail: str = ""


@dataclass(frozen=True)
class RuntimeRecord:
    execution_id: str
    task_id: str
    invocation_id: str
    capability_id: str
    status: RuntimeStatus
    started_at: str
    updated_at: str
    ended_at: str | None = None
    reason: str = ""
    events: tuple[RuntimeEvent, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.status.terminal

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "task_id": redact(self.task_id),
            "invocation_id": redact(self.invocation_id),
            "capability_id": redact(self.capability_id),
            "status": self.status.value,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "reason": redact(self.reason),
            "events": [
                {"type": event.type, "at": event.at, "detail": redact(event.detail)}
                for event in self.events
            ],
        }


@dataclass(frozen=True)
class CancelResult:
    execution_id: str
    status: CancelStatus
    confirmed: bool
    runtime_status: RuntimeStatus | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status.value,
            "confirmed": self.confirmed,
            "runtime_status": self.runtime_status.value if self.runtime_status else None,
            "reason": redact(self.reason),
        }


class UnknownCapability(KeyError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_TERMINATION_CATEGORIES = {
    "TERMINATED_BY_REQUEST",
    "CANCELLED",
    "CANCELLED_BY_REQUEST",
}


@dataclass
class _Entry:
    execution_id: str
    task_id: str
    invocation_id: str
    capability_id: str
    capability: RuntimeAbility
    status: RuntimeStatus = RuntimeStatus.STARTING
    started_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)
    ended_at: str | None = None
    reason: str = ""
    events: list[RuntimeEvent] = field(default_factory=list)
    thread: threading.Thread | None = None
    cancel_requested: bool = False
    running_since: float = 0.0

    def record(self, events: list[RuntimeEvent] | None = None) -> RuntimeRecord:
        return RuntimeRecord(
            execution_id=self.execution_id,
            task_id=self.task_id,
            invocation_id=self.invocation_id,
            capability_id=self.capability_id,
            status=self.status,
            started_at=self.started_at,
            updated_at=self.updated_at,
            ended_at=self.ended_at,
            reason=self.reason,
            events=tuple(events if events is not None else self.events),
        )


class RuntimeAbility(Protocol):
    def execute(self, request: ExecutionRequest): ...
    def terminate(self, execution_id: str) -> bool: ...
    def is_alive(self, execution_id: str) -> bool: ...


class RuntimeManager:
    """Live, in-memory control plane for invocation runtimes.

    Not durable truth: Task State remains the durable record. Completed
    invocations are kept briefly as terminal records for status queries.
    """

    def __init__(
        self,
        capabilities: Mapping[str, RuntimeAbility],
        *,
        cancel_timeout: float = 10.0,
        recent_limit: int = 100,
        lost_grace_seconds: float = 1.0,
    ) -> None:
        self._capabilities = dict(capabilities)
        self._cancel_timeout = float(cancel_timeout)
        self._recent_limit = int(recent_limit)
        self._lost_grace_seconds = float(lost_grace_seconds)
        self._lock = threading.RLock()
        self._active: dict[str, _Entry] = {}
        self._recent: OrderedDict[str, _Entry] = OrderedDict()

    # ---------------------------------------------------------------- start

    def start(self, *, capability_id: str, request: ExecutionRequest) -> RuntimeRecord:
        capability = self._capabilities.get(capability_id)
        if capability is None:
            raise UnknownCapability(capability_id)

        execution_id = str(uuid.uuid4())
        metadata = dict(request.metadata)
        metadata["execution_id"] = execution_id
        bound_request = _replace_metadata(request, metadata)

        entry = _Entry(
            execution_id=execution_id,
            task_id=request.task_id,
            invocation_id=request.invocation_id,
            capability_id=capability_id,
            capability=capability,
        )
        entry.events.append(RuntimeEvent("RUNTIME_STARTED", entry.started_at, "accepted"))
        thread = threading.Thread(
            target=self._run,
            args=(entry, bound_request),
            name=f"runtime-{execution_id[:8]}",
            daemon=True,
        )
        entry.thread = thread
        with self._lock:
            self._active[execution_id] = entry
        thread.start()
        with self._lock:
            return entry.record()

    def _run(self, entry: _Entry, request: ExecutionRequest) -> None:
        with self._lock:
            if entry.status is RuntimeStatus.STARTING:
                entry.status = RuntimeStatus.RUNNING
                entry.updated_at = _utcnow()
                entry.running_since = time.monotonic()

        outcome: Any = None
        error: BaseException | None = None
        try:
            outcome = entry.capability.execute(request)
        except BaseException as exc:  # noqa: BLE001 - runtime boundary
            error = exc

        with self._lock:
            if entry.status is RuntimeStatus.LOST:
                return
            now = _utcnow()
            entry.updated_at = now
            entry.ended_at = now
            if error is not None:
                entry.status = RuntimeStatus.FAILED
                entry.reason = f"runtime_error:{type(error).__name__}"
                entry.events.append(RuntimeEvent("RUNTIME_FAILED", now, entry.reason))
            elif isinstance(outcome, ExecutionResult):
                entry.status = RuntimeStatus.SUCCEEDED
                entry.reason = f"result:{outcome.status.value}"
                entry.events.append(RuntimeEvent("RUNTIME_SUCCEEDED", now, entry.reason))
            elif isinstance(outcome, ExecutionFailure):
                if outcome.category in _TERMINATION_CATEGORIES:
                    entry.status = RuntimeStatus.STOPPED
                    entry.reason = "cancelled"
                    entry.events.append(RuntimeEvent("RUNTIME_CANCELLED", now, "cancelled"))
                else:
                    entry.status = RuntimeStatus.FAILED
                    entry.reason = f"failure:{outcome.category}"
                    entry.events.append(RuntimeEvent("RUNTIME_FAILED", now, entry.reason))
            else:
                entry.status = RuntimeStatus.FAILED
                entry.reason = "invalid_execution_outcome"
                entry.events.append(RuntimeEvent("RUNTIME_FAILED", now, entry.reason))
            self._retire(entry)

    def _retire(self, entry: _Entry) -> None:
        self._active.pop(entry.execution_id, None)
        self._recent[entry.execution_id] = entry
        while len(self._recent) > self._recent_limit:
            self._recent.popitem(last=False)

    # --------------------------------------------------------------- status

    def status(self, execution_id: str) -> RuntimeRecord | None:
        """Return a snapshot, or ``None`` for an unknown/expired id (NOT_FOUND)."""

        with self._lock:
            entry = self._active.get(execution_id) or self._recent.get(execution_id)
            if entry is None:
                return None
            should_probe = (
                entry.status is RuntimeStatus.RUNNING
                and entry.thread is not None
                and entry.thread.is_alive()
                and not entry.cancel_requested
                and time.monotonic() - entry.running_since >= self._lost_grace_seconds
            )
            capability = entry.capability
        if should_probe:
            try:
                alive = bool(capability.is_alive(execution_id))
            except Exception:
                alive = True
            if not alive:
                with self._lock:
                    if entry.status is RuntimeStatus.RUNNING:
                        now = _utcnow()
                        entry.status = RuntimeStatus.LOST
                        entry.ended_at = now
                        entry.updated_at = now
                        entry.reason = "runtime_no_longer_alive"
                        entry.events.append(RuntimeEvent("RUNTIME_LOST", now, entry.reason))
                        self._retire(entry)
        with self._lock:
            return entry.record()

    # --------------------------------------------------------------- cancel

    def cancel(self, execution_id: str) -> CancelResult:
        with self._lock:
            entry = self._active.get(execution_id) or self._recent.get(execution_id)
            if entry is None:
                return CancelResult(execution_id, CancelStatus.NOT_FOUND, False)
            if entry.status.terminal:
                return CancelResult(
                    execution_id, CancelStatus.ALREADY_TERMINAL, True, entry.status,
                    entry.reason,
                )
            already_stopping = entry.status is RuntimeStatus.STOPPING
            if not already_stopping:
                entry.status = RuntimeStatus.STOPPING
                entry.cancel_requested = True
                entry.updated_at = _utcnow()
                entry.events.append(
                    RuntimeEvent("RUNTIME_CANCEL_REQUESTED", entry.updated_at, "requested")
                )
            capability = entry.capability
            thread = entry.thread

        accepted = False
        try:
            accepted = bool(capability.terminate(execution_id))
        except Exception:
            accepted = False

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._cancel_timeout)

        with self._lock:
            entry = self._active.get(execution_id) or self._recent.get(execution_id)
            if entry is None:  # defensive; retirement keeps recent
                return CancelResult(execution_id, CancelStatus.NOT_FOUND, False)
            if entry.status is RuntimeStatus.STOPPED:
                return CancelResult(
                    execution_id, CancelStatus.CANCELLED, True, entry.status, entry.reason
                )
            if entry.status.terminal:
                return CancelResult(
                    execution_id, CancelStatus.ALREADY_TERMINAL, True, entry.status,
                    entry.reason,
                )
            if not accepted:
                return CancelResult(
                    execution_id, CancelStatus.FAILED, False, entry.status,
                    "runtime_terminate_rejected",
                )
            return CancelResult(
                execution_id, CancelStatus.REQUESTED, False, entry.status,
                "termination_not_confirmed",
            )

    # --------------------------------------------------------------- events

    def events(self, execution_id: str) -> tuple[RuntimeEvent, ...]:
        with self._lock:
            entry = self._active.get(execution_id) or self._recent.get(execution_id)
            return tuple(entry.events) if entry else ()

    def active_executions(self) -> list[RuntimeRecord]:
        with self._lock:
            return [entry.record() for entry in self._active.values()]

    def records_for_task(self, task_id: str) -> list[RuntimeRecord]:
        """Read-only query used by observability; includes recent terminals."""

        with self._lock:
            entries = [
                entry for entry in self._active.values() if entry.task_id == task_id
            ] + [
                entry for entry in self._recent.values() if entry.task_id == task_id
            ]
        seen: set[str] = set()
        records: list[RuntimeRecord] = []
        for entry in sorted(entries, key=lambda item: (item.started_at, item.execution_id)):
            if entry.execution_id in seen:
                continue
            seen.add(entry.execution_id)
            records.append(entry.record())
        return records


def _replace_metadata(request: ExecutionRequest, metadata: Mapping[str, Any]) -> ExecutionRequest:
    from dataclasses import replace

    return replace(request, metadata=metadata)
