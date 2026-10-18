"""Vendor-neutral Execution Contract.

An Execution Capability receives a self-contained request and returns either a
result or a failure. The contract knows nothing about providers, harnesses,
sessions, or wire details; adapters translate those into opaque
continuation references and normalized failures.

The controller treats execution as a bounded, single invocation. There is no
retry loop here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from .errors import ProviderError
from .redaction import redact, redact_data
from .task import Artifact

__all__ = [
    "ExecutionCapability",
    "ExecutionFailure",
    "ExecutionFailureKind",
    "ExecutionMode",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionStatus",
]


class ExecutionMode(str, Enum):
    FRESH = "FRESH"
    CONTINUE = "CONTINUE"
    HANDOFF = "HANDOFF"


class ExecutionStatus(str, Enum):
    PROGRESS = "PROGRESS"
    COMPLETED = "COMPLETED"


class ExecutionFailureKind(str, Enum):
    """Provider failures may update health; execution failures must not."""

    PROVIDER = "PROVIDER"
    EXECUTION = "EXECUTION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ExecutionRequest:
    """Minimal, handoff-safe input for one execution invocation."""

    invocation_id: str
    task_id: str
    objective: str
    durable_context: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    execution_mode: ExecutionMode = ExecutionMode.FRESH
    continuation_ref: str | None = None
    checkpoint: str | None = None
    workspace_path: str | None = None
    constraints: Mapping[str, Any] = field(default_factory=dict)
    handoff_reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "objective", redact(self.objective))
        object.__setattr__(self, "continuation_ref", redact(self.continuation_ref) if self.continuation_ref else None)
        object.__setattr__(self, "checkpoint", redact(self.checkpoint) if self.checkpoint else None)
        object.__setattr__(self, "handoff_reason", redact(self.handoff_reason) if self.handoff_reason else None)
        object.__setattr__(self, "durable_context", redact_data(dict(self.durable_context)))
        object.__setattr__(self, "constraints", redact_data(dict(self.constraints)))
        object.__setattr__(self, "metadata", redact_data(dict(self.metadata)))


@dataclass(frozen=True)
class ExecutionResult:
    """Successful bounded invocation. Completion is explicit, never inferred."""

    invocation_id: str = ""
    status: ExecutionStatus = ExecutionStatus.PROGRESS
    artifacts: tuple[Artifact, ...] = ()
    output: str | None = None
    checkpoint: str | None = None
    continuation_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.status, str) and not isinstance(self.status, ExecutionStatus):
            object.__setattr__(self, "status", ExecutionStatus(self.status.strip().upper()))
        object.__setattr__(self, "output", redact(self.output) if self.output else None)
        object.__setattr__(self, "checkpoint", redact(self.checkpoint) if self.checkpoint else None)
        object.__setattr__(self, "continuation_ref", redact(self.continuation_ref) if self.continuation_ref else None)
        object.__setattr__(self, "metadata", redact_data(dict(self.metadata)))

    @property
    def is_completed(self) -> bool:
        return self.status is ExecutionStatus.COMPLETED


@dataclass(frozen=True)
class ExecutionFailure:
    """Normalized failure.

    ``PROVIDER`` failures must carry a ``ProviderError`` and are the only kind
    that may update provider health. Environment/control failures
    (``EXECUTION``/``UNKNOWN``) must never be coerced into provider health.
    """

    invocation_id: str = ""
    kind: ExecutionFailureKind = ExecutionFailureKind.EXECUTION
    category: str = "EXECUTION_FAILURE"
    message: str = ""
    retryable: bool = False
    provider_error: ProviderError | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    partial_artifacts: tuple[Artifact, ...] = ()
    checkpoint: str | None = None
    continuation_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.kind, str) and not isinstance(self.kind, ExecutionFailureKind):
            object.__setattr__(self, "kind", ExecutionFailureKind(self.kind.strip().upper()))
        object.__setattr__(self, "category", redact(self.category))
        object.__setattr__(self, "message", redact(self.message))
        object.__setattr__(self, "evidence", redact_data(dict(self.evidence)))
        object.__setattr__(self, "checkpoint", redact(self.checkpoint) if self.checkpoint else None)
        object.__setattr__(self, "continuation_ref", redact(self.continuation_ref) if self.continuation_ref else None)
        object.__setattr__(self, "metadata", redact_data(dict(self.metadata)))

    @property
    def updates_provider_health(self) -> bool:
        return self.kind is ExecutionFailureKind.PROVIDER and self.provider_error is not None


@runtime_checkable
class ExecutionCapability(Protocol):
    """Replaceable execution seam. Adapters implement this, core consumes it."""

    def execute(self, request: ExecutionRequest) -> ExecutionResult | ExecutionFailure: ...
