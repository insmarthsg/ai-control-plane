"""Deterministic Thin Router.

Routing is a pure selection over facts and constraints:

    facts + constraints + policy -> selection | NoRoute

It never executes, never writes Task State, never changes health, never loops,
and never retries. User instructions, capability availability, provider
health, and continuation compatibility are hard gates; deterministic
preference ranks come next; a future advisory judgment signal may only break
ties after all hard gates and ranks.

Priority is frozen:
1. user constraints (REQUIRE / DENY / ALLOW)
2. capability availability
3. provider health
4. continuation compatibility
5. deterministic preference rank
6. advisory judgment signal (future Jev slot)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .capability import CapabilityAvailability, CapabilityCandidate
from .provider_health import HealthStatus
from .redaction import redact

__all__ = [
    "ContinuationMode",
    "JudgmentSignal",
    "NoRoute",
    "POLICY_VERSION",
    "Rejection",
    "RejectionReason",
    "RoutingDecision",
    "RoutingRequest",
    "RoutingResult",
    "ThinRouter",
    "UserConstraints",
]

POLICY_VERSION = "goal-03.v1"


class ContinuationMode(str, Enum):
    """How the caller wants continuity handled. Router only classifies it."""

    NONE = "NONE"
    CONTINUE = "CONTINUE"
    HANDOFF = "HANDOFF"


class RejectionReason(str, Enum):
    MALFORMED_CANDIDATE = "MALFORMED_CANDIDATE"
    KIND_MISMATCH = "KIND_MISMATCH"
    USER_DENIED = "USER_DENIED"
    USER_NOT_ALLOWED = "USER_NOT_ALLOWED"
    NOT_REQUIRED = "NOT_REQUIRED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    PROVIDER_COOLDOWN = "PROVIDER_COOLDOWN"
    PROVIDER_AUTH_REQUIRED = "PROVIDER_AUTH_REQUIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    HEALTH_UNKNOWN = "HEALTH_UNKNOWN"
    CONTINUATION_INCOMPATIBLE = "CONTINUATION_INCOMPATIBLE"
    HANDOFF_UNSUPPORTED = "HANDOFF_UNSUPPORTED"
    LOWER_PRIORITY = "LOWER_PRIORITY"


_HEALTH_REJECTIONS: dict[HealthStatus, RejectionReason] = {
    HealthStatus.COOLDOWN: RejectionReason.PROVIDER_COOLDOWN,
    HealthStatus.AUTH_REQUIRED: RejectionReason.PROVIDER_AUTH_REQUIRED,
    HealthStatus.UNAVAILABLE: RejectionReason.PROVIDER_UNAVAILABLE,
}


def _safe(value: Any) -> str:
    return redact(str(value))


def _health_status(value: Any) -> HealthStatus | None:
    if isinstance(value, HealthStatus):
        return value
    if isinstance(value, Mapping):
        return _health_status(value.get("status"))
    if isinstance(value, str):
        try:
            return HealthStatus(value.strip().upper())
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class UserConstraints:
    """Explicit user instructions. They always outrank health and policy."""

    allowed: frozenset[str] | None = None
    denied: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()


@dataclass(frozen=True)
class JudgmentSignal:
    """Advisory only. Never consulted before hard gates and rank."""

    candidate_id: str
    score: float
    confidence: float = 1.0
    source: str = "unknown"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingRequest:
    task_id: str
    required_capability_kind: str
    candidates: tuple[CapabilityCandidate, ...]
    constraints: UserConstraints = field(default_factory=UserConstraints)
    continuation_mode: ContinuationMode = ContinuationMode.NONE
    continuation_ref: str | None = None
    task_has_durable_state: bool = False
    provider_health: Mapping[str, Any] = field(default_factory=dict)
    judgment_signals: tuple[JudgmentSignal, ...] = ()


@dataclass(frozen=True)
class Rejection:
    capability_id: str
    reason: RejectionReason
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _safe(self.capability_id))
        object.__setattr__(self, "detail", _safe(self.detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "reason": self.reason.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RoutingDecision:
    task_id: str
    selected_capability_id: str
    mode: ContinuationMode
    decision_reason: str
    considered_candidates: tuple[str, ...]
    rejected_candidates: tuple[Rejection, ...]
    policy_version: str = POLICY_VERSION
    judgment_considered: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _safe(self.task_id))
        object.__setattr__(self, "selected_capability_id", _safe(self.selected_capability_id))
        object.__setattr__(self, "decision_reason", _safe(self.decision_reason))
        object.__setattr__(
            self, "considered_candidates", tuple(_safe(item) for item in self.considered_candidates)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": "SELECTED",
            "task_id": self.task_id,
            "selected_capability_id": self.selected_capability_id,
            "mode": self.mode.value,
            "decision_reason": self.decision_reason,
            "considered_candidates": list(self.considered_candidates),
            "rejected_candidates": [item.to_dict() for item in self.rejected_candidates],
            "policy_version": self.policy_version,
            "judgment_considered": self.judgment_considered,
        }


@dataclass(frozen=True)
class NoRoute:
    """First-class outcome: no candidate satisfies the hard constraints."""

    task_id: str
    requested_kind: str
    mode: ContinuationMode
    considered_candidates: tuple[str, ...]
    rejected_candidates: tuple[Rejection, ...]
    unsatisfied_constraints: tuple[str, ...] = ()
    policy_version: str = POLICY_VERSION
    judgment_considered: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _safe(self.task_id))
        object.__setattr__(self, "requested_kind", _safe(self.requested_kind))
        object.__setattr__(
            self, "considered_candidates", tuple(_safe(item) for item in self.considered_candidates)
        )
        object.__setattr__(
            self,
            "unsatisfied_constraints",
            tuple(_safe(item) for item in self.unsatisfied_constraints),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": "NO_ROUTE",
            "task_id": self.task_id,
            "requested_kind": self.requested_kind,
            "mode": self.mode.value,
            "considered_candidates": list(self.considered_candidates),
            "rejected_candidates": [item.to_dict() for item in self.rejected_candidates],
            "unsatisfied_constraints": list(self.unsatisfied_constraints),
            "policy_version": self.policy_version,
            "judgment_considered": self.judgment_considered,
        }


RoutingResult = RoutingDecision | NoRoute


class ThinRouter:
    """Stateless, deterministic capability selector."""

    policy_version = POLICY_VERSION

    def route(self, request: RoutingRequest) -> RoutingResult:
        mode = request.continuation_mode
        if not isinstance(mode, ContinuationMode):
            try:
                mode = ContinuationMode(str(mode).upper())
            except ValueError:
                mode = ContinuationMode.NONE

        health = request.provider_health if isinstance(request.provider_health, Mapping) else {}
        scores = self._judgment_scores(request)

        considered: list[str] = []
        rejected: list[Rejection] = []
        eligible: list[CapabilityCandidate] = []

        for candidate in request.candidates:
            considered.append(self._candidate_id(candidate))
            rejection = self._evaluate(candidate, request, mode, health)
            if rejection is not None:
                rejected.append(rejection)
                continue
            eligible.append(candidate)

        if not eligible:
            return NoRoute(
                task_id=request.task_id,
                requested_kind=request.required_capability_kind,
                mode=mode,
                considered_candidates=tuple(considered),
                rejected_candidates=tuple(rejected),
                unsatisfied_constraints=self._unsatisfied(request, mode),
                policy_version=self.policy_version,
                judgment_considered=bool(scores),
            )

        eligible.sort(
            key=lambda candidate: (
                self._health_tier(candidate, health),
                candidate.preference_rank,
                -scores.get(candidate.capability_id, 0.0),
                candidate.capability_id,
            )
        )
        selected = eligible[0]
        for candidate in eligible[1:]:
            rejected.append(
                Rejection(
                    capability_id=candidate.capability_id,
                    reason=RejectionReason.LOWER_PRIORITY,
                    detail=(
                        f"preference_rank={candidate.preference_rank}, "
                        f"health_tier={self._health_tier(candidate, health)}"
                    ),
                )
            )

        reason = (
            f"selected {selected.capability_id}: all hard gates passed; "
            f"lowest deterministic rank among eligible candidates"
        )
        return RoutingDecision(
            task_id=request.task_id,
            selected_capability_id=selected.capability_id,
            mode=mode,
            decision_reason=reason,
            considered_candidates=tuple(considered),
            rejected_candidates=tuple(rejected),
            policy_version=self.policy_version,
            judgment_considered=bool(scores),
        )

    # ------------------------------------------------------------ evaluation

    def _evaluate(
        self,
        candidate: Any,
        request: RoutingRequest,
        mode: ContinuationMode,
        health: Mapping[str, Any],
    ) -> Rejection | None:
        capability_id = self._candidate_id(candidate)
        if not isinstance(candidate, CapabilityCandidate):
            return Rejection(capability_id, RejectionReason.MALFORMED_CANDIDATE, "not a capability candidate")

        if not isinstance(candidate.capability_id, str) or not candidate.capability_id.strip():
            return Rejection(capability_id, RejectionReason.MALFORMED_CANDIDATE, "invalid capability_id")
        if not isinstance(candidate.kind, str) or not candidate.kind.strip():
            return Rejection(capability_id, RejectionReason.MALFORMED_CANDIDATE, "invalid kind")
        if not isinstance(candidate.preference_rank, int) or isinstance(candidate.preference_rank, bool):
            return Rejection(capability_id, RejectionReason.MALFORMED_CANDIDATE, "invalid preference_rank")
        if candidate.provider is not None and not isinstance(candidate.provider, str):
            return Rejection(capability_id, RejectionReason.MALFORMED_CANDIDATE, "invalid provider")

        availability = candidate.availability
        if isinstance(availability, str) and not isinstance(availability, CapabilityAvailability):
            try:
                availability = CapabilityAvailability(availability.strip().upper())
            except ValueError:
                availability = None
        if not isinstance(availability, CapabilityAvailability):
            return Rejection(capability_id, RejectionReason.MALFORMED_CANDIDATE, "invalid availability")

        if candidate.kind != request.required_capability_kind:
            return Rejection(
                capability_id,
                RejectionReason.KIND_MISMATCH,
                f"kind {candidate.kind!r} != {request.required_capability_kind!r}",
            )

        constraints = request.constraints if isinstance(request.constraints, UserConstraints) else UserConstraints()
        if candidate.capability_id in constraints.denied:
            return Rejection(capability_id, RejectionReason.USER_DENIED, "explicitly denied by user")
        if constraints.allowed is not None and candidate.capability_id not in constraints.allowed:
            return Rejection(capability_id, RejectionReason.USER_NOT_ALLOWED, "not in user allow set")
        if constraints.required and candidate.capability_id not in constraints.required:
            return Rejection(capability_id, RejectionReason.NOT_REQUIRED, "user required another capability")

        if availability is not CapabilityAvailability.AVAILABLE:
            return Rejection(
                capability_id, RejectionReason.CAPABILITY_UNAVAILABLE, "capability reports UNAVAILABLE"
            )

        if candidate.provider:
            if candidate.provider not in health:
                return Rejection(
                    capability_id,
                    RejectionReason.HEALTH_UNKNOWN,
                    f"no health fact for provider {candidate.provider!r}",
                )
            status = _health_status(health.get(candidate.provider))
            if status is None:
                return Rejection(
                    capability_id,
                    RejectionReason.HEALTH_UNKNOWN,
                    f"malformed health fact for provider {candidate.provider!r}",
                )
            rejection_reason = _HEALTH_REJECTIONS.get(status)
            if rejection_reason is not None:
                return Rejection(
                    capability_id,
                    rejection_reason,
                    f"provider {candidate.provider!r} is {status.value}",
                )

        if mode is ContinuationMode.CONTINUE:
            refs = candidate.continuation_refs
            if not isinstance(refs, (frozenset, set, tuple, list)):
                refs = ()
            if not request.continuation_ref or request.continuation_ref not in refs:
                return Rejection(
                    capability_id,
                    RejectionReason.CONTINUATION_INCOMPATIBLE,
                    "candidate cannot directly resume the task continuation",
                )

        if mode is ContinuationMode.HANDOFF:
            if not request.task_has_durable_state:
                return Rejection(
                    capability_id,
                    RejectionReason.HANDOFF_UNSUPPORTED,
                    "task has no durable state to hand off",
                )
            if not candidate.supports_task_state_handoff:
                return Rejection(
                    capability_id,
                    RejectionReason.HANDOFF_UNSUPPORTED,
                    "candidate does not support task-state handoff",
                )

        return None

    def _health_tier(self, candidate: CapabilityCandidate, health: Mapping[str, Any]) -> int:
        if not candidate.provider:
            return 0
        status = _health_status(health.get(candidate.provider))
        return 1 if status is HealthStatus.DEGRADED else 0

    def _judgment_scores(self, request: RoutingRequest) -> dict[str, float]:
        scores: dict[str, float] = {}
        signals = request.judgment_signals if isinstance(request.judgment_signals, (tuple, list)) else ()
        for signal in signals:
            if not isinstance(signal, JudgmentSignal):
                continue
            if not isinstance(signal.candidate_id, str):
                continue
            score = signal.score
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                continue
            numeric = float(score)
            if not math.isfinite(numeric):
                continue
            previous = scores.get(signal.candidate_id)
            if previous is None or numeric > previous:
                scores[signal.candidate_id] = numeric
        return scores

    def _unsatisfied(self, request: RoutingRequest, mode: ContinuationMode) -> tuple[str, ...]:
        constraints = request.constraints if isinstance(request.constraints, UserConstraints) else UserConstraints()
        unsatisfied: list[str] = []
        for capability_id in sorted(constraints.required):
            unsatisfied.append(f"REQUIRE:{_safe(capability_id)}")
        if constraints.allowed is not None and not constraints.allowed:
            unsatisfied.append("ALLOW: empty set")
        if mode is ContinuationMode.CONTINUE and not request.continuation_ref:
            unsatisfied.append("CONTINUE: continuation_ref required")
        if mode is ContinuationMode.CONTINUE and request.continuation_ref:
            unsatisfied.append(f"CONTINUE:{_safe(request.continuation_ref)}")
        if mode is ContinuationMode.HANDOFF and not request.task_has_durable_state:
            unsatisfied.append("HANDOFF: durable task state required")
        return tuple(unsatisfied)

    @staticmethod
    def _candidate_id(candidate: Any) -> str:
        raw = getattr(candidate, "capability_id", None)
        if not isinstance(raw, str) or not raw:
            return "<malformed>"
        return raw
