"""Minimal capability contract.

Earned by GOAL-03: the Router has a concrete consumer that must compare
candidates without knowing providers or harnesses.

Deliberately *not* a plugin framework: no registry, loader, DI container,
invoke dispatch, or service locator. It is a frozen dataclass plus one enum.
A capability declares what it is and what it can continue; callers decide how
to execute it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

__all__ = ["CapabilityAvailability", "CapabilityCandidate"]


class CapabilityAvailability(str, Enum):
    """The capability's own readiness, independent of provider health."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class CapabilityCandidate:
    """One selectable capability instance.

    ``provider`` is opaque routing metadata; Router never branches on its
    value. ``continuation_refs`` are opaque identifiers this candidate can
    directly resume. ``supports_task_state_handoff`` means the candidate can
    start from durable Task State instead of continuing an existing session.
    """

    capability_id: str
    kind: str
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE
    provider: str | None = None
    preference_rank: int = 100
    continuation_refs: frozenset[str] = frozenset()
    supports_task_state_handoff: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
