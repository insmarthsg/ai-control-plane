"""Read-only truth projection for a Task.

Observability answers "what is true now?" from durable Task facts plus live
runtime/human/health queries. It never mutates Task, Health, Runtime, or Human
decisions. Durable Task State is authoritative; live registry data is
ephemeral evidence and is always shown separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from .redaction import redact, redact_data
from .task import CapabilityActivity, TaskEvent, TaskState
from .task_store import TaskStateStore

__all__ = [
    "ArtifactSummary",
    "HealthObservation",
    "HumanObservation",
    "ObservationIssue",
    "Observer",
    "ProviderHealthQuery",
    "RuntimeObservation",
    "RuntimeQuery",
    "TaskObservation",
    "TimelineEntry",
]

_SAFE_EVENT_KEYS = (
    "invocation_id",
    "capability_id",
    "request_id",
    "decision",
    "kind",
    "status",
    "artifact_id",
    "checkpoint",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ArtifactSummary:
    artifact_id: str
    kind: str
    producer_capability: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": redact(self.artifact_id),
            "kind": redact(self.kind),
            "producer_capability": redact(self.producer_capability),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RuntimeObservation:
    execution_id: str
    status: str
    started_at: str
    updated_at: str
    ended_at: str | None = None
    reason: str = ""

    @property
    def live(self) -> bool:
        return self.status in {"STARTING", "RUNNING", "STOPPING"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "reason": redact(self.reason),
        }


@dataclass(frozen=True)
class HumanObservation:
    request_id: str
    requesting_capability: str
    question: str
    allowed_actions: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "requesting_capability": redact(self.requesting_capability),
            "question": redact(self.question),
            "allowed_actions": list(self.allowed_actions),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class HealthObservation:
    provider: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {"provider": redact(self.provider), "status": self.status}


@dataclass(frozen=True)
class ObservationIssue:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": redact(self.detail)}


@dataclass(frozen=True)
class TimelineEntry:
    seq: int
    type: str
    at: str
    revision: int
    actor: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type,
            "at": self.at,
            "revision": self.revision,
            "actor": redact(self.actor),
            "details": redact_data(dict(self.details)),
        }


@dataclass(frozen=True)
class TaskObservation:
    task_id: str
    task_revision: int
    task_status: str
    created_at: str
    updated_at: str
    observed_at: str
    active_capability: CapabilityActivity | None = None
    cursor_checkpoint: str | None = None
    cursor_capability: str | None = None
    continuation_present: bool = False
    artifacts: tuple[ArtifactSummary, ...] = ()
    runtimes: tuple[RuntimeObservation, ...] = ()
    pending_human: HumanObservation | None = None
    provider_health: tuple[HealthObservation, ...] = ()
    issues: tuple[ObservationIssue, ...] = ()
    stale_snapshot: bool = False

    @property
    def has_live_runtime(self) -> bool:
        return any(runtime.live for runtime in self.runtimes)

    @property
    def waiting_for_human(self) -> bool:
        return self.pending_human is not None

    @property
    def latest_runtime(self) -> RuntimeObservation | None:
        return self.runtimes[-1] if self.runtimes else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "task_status": self.task_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active_capability": (
                {
                    "capability_id": redact(self.active_capability.capability_id),
                    "capability_kind": redact(self.active_capability.capability_kind),
                }
                if self.active_capability
                else None
            ),
            "cursor_checkpoint": redact(self.cursor_checkpoint) if self.cursor_checkpoint else None,
            "cursor_capability": redact(self.cursor_capability) if self.cursor_capability else None,
            "continuation_present": self.continuation_present,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "runtimes": [runtime.to_dict() for runtime in self.runtimes],
            "pending_human": self.pending_human.to_dict() if self.pending_human else None,
            "provider_health": [health.to_dict() for health in self.provider_health],
            "issues": [issue.to_dict() for issue in self.issues],
            "stale_snapshot": self.stale_snapshot,
        }


class RuntimeQuery(Protocol):
    def records_for_task(self, task_id: str) -> Sequence[Any]: ...


class ProviderHealthQuery(Protocol):
    def all(self) -> Mapping[str, Any]: ...


class Observer:
    """Read-only projector over durable Task facts and live control state."""

    def __init__(
        self,
        store: TaskStateStore,
        *,
        runtime: RuntimeQuery | None = None,
        human: Any | None = None,
        health: ProviderHealthQuery | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._human = human
        self._health = health

    def observe(self, task_id: str) -> TaskObservation:
        task = self._store.get_task(task_id)
        stale = False
        issues: list[ObservationIssue] = []
        runtimes: tuple[RuntimeObservation, ...] = ()
        pending: HumanObservation | None = None
        health: tuple[HealthObservation, ...] = ()

        for attempt in range(2):
            issues = []
            runtimes = self._runtime_view(task_id, issues)
            pending = self._human_view(task_id, issues)
            health = self._health_view(issues)
            latest = self._store.get_task(task_id)
            if latest.revision == task.revision:
                break
            task = latest
            if attempt == 1:
                stale = True
                issues.append(ObservationIssue("STALE_SNAPSHOT", "task revision changed while observing"))

        observation = TaskObservation(
            task_id=task.task_id,
            task_revision=task.revision,
            task_status=task.lifecycle_status.value,
            created_at=task.created_at,
            updated_at=task.updated_at,
            observed_at=_utcnow(),
            active_capability=task.active_capability,
            cursor_checkpoint=task.cursor.checkpoint if task.cursor else None,
            cursor_capability=task.cursor.capability_id if task.cursor else None,
            continuation_present=bool(task.cursor and task.cursor.continuation_ref),
            artifacts=tuple(
                ArtifactSummary(
                    artifact_id=artifact.artifact_id,
                    kind=artifact.kind,
                    producer_capability=artifact.producer_capability,
                    created_at=artifact.created_at,
                )
                for artifact in task.artifacts
            ),
            runtimes=runtimes,
            pending_human=pending,
            provider_health=health,
            issues=tuple(issues),
            stale_snapshot=stale,
        )
        return observation

    def timeline(self, task_id: str) -> tuple[TimelineEntry, ...]:
        events = self._store.list_events(task_id)
        return tuple(self._entry(event) for event in events)

    # --------------------------------------------------------------- sources

    def _runtime_view(
        self, task_id: str, issues: list[ObservationIssue]
    ) -> tuple[RuntimeObservation, ...]:
        if self._runtime is None:
            return ()
        try:
            records = list(self._runtime.records_for_task(task_id))
        except Exception:
            issues.append(ObservationIssue("RUNTIME_QUERY_FAILED", "runtime query failed"))
            return ()
        observations: list[RuntimeObservation] = []
        for record in records:
            status = getattr(record, "status", "UNKNOWN")
            observations.append(
                RuntimeObservation(
                    execution_id=str(getattr(record, "execution_id", "")),
                    status=status.value if hasattr(status, "value") else str(status),
                    started_at=str(getattr(record, "started_at", "")),
                    updated_at=str(getattr(record, "updated_at", "")),
                    ended_at=getattr(record, "ended_at", None),
                    reason=str(getattr(record, "reason", "")),
                )
            )
        return tuple(observations)

    def _human_view(
        self, task_id: str, issues: list[ObservationIssue]
    ) -> HumanObservation | None:
        if self._human is None:
            return None
        try:
            request = self._human.pending(task_id)
        except Exception:
            issues.append(ObservationIssue("HUMAN_QUERY_FAILED", "human gate query failed"))
            return None
        if request is None:
            return None
        return HumanObservation(
            request_id=request.request_id,
            requesting_capability=request.requesting_capability,
            question=request.question,
            allowed_actions=tuple(action.value for action in request.allowed_actions),
            created_at=request.created_at,
        )

    def _health_view(self, issues: list[ObservationIssue]) -> tuple[HealthObservation, ...]:
        if self._health is None:
            return ()
        try:
            records = self._health.all()
        except Exception:
            issues.append(ObservationIssue("HEALTH_QUERY_FAILED", "provider health query failed"))
            return ()
        observations: list[HealthObservation] = []
        for provider, record in records.items():
            status = getattr(record, "status", None)
            if status is None and isinstance(record, Mapping):
                status = record.get("status")
            observations.append(
                HealthObservation(
                    provider=str(provider),
                    status=status.value if hasattr(status, "value") else str(status or "UNKNOWN"),
                )
            )
        return tuple(sorted(observations, key=lambda item: item.provider))

    @staticmethod
    def _entry(event: TaskEvent) -> TimelineEntry:
        details: dict[str, Any] = {}
        data = event.data if isinstance(event.data, Mapping) else {}
        for key in _SAFE_EVENT_KEYS:
            value = data.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                details[key] = value if value is None else redact(str(value))[:120]
        return TimelineEntry(
            seq=event.seq,
            type=event.type,
            at=event.at,
            revision=event.revision,
            actor=event.actor,
            details=details,
        )
