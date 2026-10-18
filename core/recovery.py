"""Read-only reconciliation: classify recovery state from evidence.

`reconcile()` never routes, executes, resumes, cancels, or mutates Task State.
It reports what is true, why, and a recommended action for a future caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from .observation import Observer, TaskObservation, TimelineEntry
from .redaction import redact

__all__ = [
    "Evidence",
    "RecoveryProfile",
    "RecoveryStatus",
    "RecommendedAction",
    "Reconciler",
    "ReconciliationResult",
]


class RecoveryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    WAITING_HUMAN = "WAITING_HUMAN"
    RECOVERABLE = "RECOVERABLE"
    HANDOFF_POSSIBLE = "HANDOFF_POSSIBLE"
    BLOCKED = "BLOCKED"
    LOST = "LOST"
    TERMINAL = "TERMINAL"
    ORPHANED = "ORPHANED"
    UNCERTAIN = "UNCERTAIN"
    UNKNOWN = "UNKNOWN"
    WORKSPACE_RECONCILIATION_REQUIRED = "WORKSPACE_RECONCILIATION_REQUIRED"


class RecommendedAction(str, Enum):
    NONE = "NONE"
    WAIT_FOR_HUMAN = "WAIT_FOR_HUMAN"
    CONTINUE = "CONTINUE"
    HANDOFF = "HANDOFF"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    MARK_LOST = "MARK_LOST"


@dataclass(frozen=True)
class RecoveryProfile:
    """Vendor-neutral recovery capability facts for one capability id."""

    supports_continue: bool = False
    supports_handoff: bool = False


@dataclass(frozen=True)
class Evidence:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": redact(self.detail)}


@dataclass(frozen=True)
class ReconciliationResult:
    task_id: str
    task_revision: int
    status: RecoveryStatus
    reason: str
    evidence: tuple[Evidence, ...]
    recommended_action: RecommendedAction
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "status": self.status.value,
            "reason": redact(self.reason),
            "evidence": [item.to_dict() for item in self.evidence],
            "recommended_action": self.recommended_action.value,
            "observed_at": self.observed_at,
        }


_TERMINAL_TASKS = {"COMPLETED", "FAILED", "CANCELLED"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Reconciler:
    """Deterministic, read-only recovery classifier over one observation."""

    def __init__(self, observer: Observer) -> None:
        self._observer = observer

    def reconcile(
        self,
        task_id: str,
        *,
        profiles: Mapping[str, RecoveryProfile] | None = None,
        uncertain_side_effect: bool = False,
    ) -> ReconciliationResult:
        observed = self._observer.observe(task_id)
        timeline = self._observer.timeline(task_id)
        profiles = dict(profiles or {})

        evidence: list[Evidence] = [Evidence(f"TASK_{observed.task_status}")]
        for issue in observed.issues:
            evidence.append(Evidence(issue.code, issue.detail))
        if observed.stale_snapshot:
            evidence.append(Evidence("STALE_SNAPSHOT", "observation raced a task write"))

        def result(status: RecoveryStatus, reason: str, action: RecommendedAction) -> ReconciliationResult:
            return ReconciliationResult(
                task_id=observed.task_id,
                task_revision=observed.task_revision,
                status=status,
                reason=reason,
                evidence=tuple(evidence),
                recommended_action=action,
                observed_at=_utcnow(),
            )

        if observed.task_status in _TERMINAL_TASKS:
            evidence.append(Evidence("TASK_TERMINAL"))
            if any(runtime.live for runtime in observed.runtimes):
                evidence.append(Evidence("STALE_RUNTIME_RECORD", "task is terminal but runtime looks live"))
            return result(RecoveryStatus.TERMINAL, "task is already terminal", RecommendedAction.NONE)

        if observed.pending_human is not None:
            evidence.append(Evidence("PENDING_HUMAN_REQUEST"))
            return result(
                RecoveryStatus.WAITING_HUMAN,
                "a human decision is pending; execution cannot resume",
                RecommendedAction.WAIT_FOR_HUMAN,
            )

        if observed.workspace is not None:
            if not observed.workspace.execution_allowed:
                evidence.append(Evidence("WORKSPACE_VERIFICATION_FAILED", observed.workspace.reason))
                return result(
                    RecoveryStatus.WORKSPACE_RECONCILIATION_REQUIRED,
                    f"workspace is not verified: {observed.workspace.reason}",
                    RecommendedAction.MANUAL_REVIEW,
                )
            evidence.append(Evidence("WORKSPACE_VERIFIED"))

        if any(issue.code == "HUMAN_QUERY_FAILED" for issue in observed.issues):
            return result(
                RecoveryStatus.UNKNOWN,
                "human gate state is unavailable",
                RecommendedAction.MANUAL_REVIEW,
            )

        if any(issue.code == "RUNTIME_QUERY_FAILED" for issue in observed.issues):
            return result(
                RecoveryStatus.UNKNOWN,
                "runtime state is unavailable",
                RecommendedAction.MANUAL_REVIEW,
            )

        if observed.has_live_runtime:
            evidence.append(Evidence("ACTIVE_RUNTIME"))
            return result(RecoveryStatus.ACTIVE, "an invocation is currently running", RecommendedAction.NONE)

        if uncertain_side_effect:
            evidence.append(Evidence("PERSISTENCE_UNCERTAIN", "external side effect may be unconfirmed"))
            return result(
                RecoveryStatus.UNCERTAIN,
                "execution side effects cannot be confirmed from durable state",
                RecommendedAction.MANUAL_REVIEW,
            )

        lost_runtime = any(runtime.status == "LOST" for runtime in observed.runtimes)
        if lost_runtime:
            evidence.append(Evidence("RUNTIME_LOST"))

        incomplete = self._incomplete_invocations(timeline)
        if incomplete:
            evidence.append(Evidence("INCOMPLETE_INVOCATION", ",".join(sorted(incomplete))))
            return result(
                RecoveryStatus.ORPHANED,
                "an invocation started without a terminal event and no live runtime",
                RecommendedAction.MANUAL_REVIEW,
            )

        continuation = observed.continuation_present
        if continuation:
            evidence.append(Evidence("CONTINUATION_PRESENT"))
        if observed.cursor_checkpoint:
            evidence.append(Evidence("CHECKPOINT_PRESENT"))

        profile = self._profile(observed, profiles)
        if continuation and profile is not None and profile.supports_continue:
            evidence.append(Evidence("RESUME_SUPPORTED"))
            return result(
                RecoveryStatus.RECOVERABLE,
                "no live runtime, but the backend can resume the continuation",
                RecommendedAction.CONTINUE,
            )

        handoff = any(item.supports_handoff for item in profiles.values())
        durable_facts = bool(
            continuation or observed.cursor_checkpoint or observed.artifacts
        )
        if handoff and durable_facts:
            evidence.append(Evidence("HANDOFF_SUPPORTED"))
            if continuation:
                evidence.append(Evidence("RESUME_UNAVAILABLE"))
            return result(
                RecoveryStatus.HANDOFF_POSSIBLE,
                "direct continuation is unavailable, but durable facts allow a handoff",
                RecommendedAction.HANDOFF,
            )

        if continuation:
            evidence.append(Evidence("RESUME_UNAVAILABLE"))
            return result(
                RecoveryStatus.BLOCKED,
                "continuation exists but the backend cannot resume it",
                RecommendedAction.MANUAL_REVIEW,
            )

        if lost_runtime:
            return result(
                RecoveryStatus.LOST,
                "runtime is lost and no recovery facts are available",
                RecommendedAction.MARK_LOST,
            )

        return result(
            RecoveryStatus.UNKNOWN,
            "no active runtime and insufficient recovery evidence",
            RecommendedAction.MANUAL_REVIEW,
        )

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _profile(
        observed: TaskObservation, profiles: Mapping[str, RecoveryProfile]
    ) -> RecoveryProfile | None:
        if observed.cursor_capability:
            profile = profiles.get(observed.cursor_capability)
            if profile is not None:
                return profile
        if observed.active_capability:
            return profiles.get(observed.active_capability.capability_id)
        return None

    @staticmethod
    def _incomplete_invocations(timeline: tuple[TimelineEntry, ...]) -> set[str]:
        started: set[str] = set()
        finished: set[str] = set()
        for entry in timeline:
            invocation = entry.details.get("invocation_id")
            if not isinstance(invocation, str):
                continue
            if entry.type == "EXECUTION_STARTED":
                started.add(invocation)
            elif entry.type in {"EXECUTION_RESULT", "EXECUTION_FAILED"}:
                finished.add(invocation)
        return started - finished
