"""Provider health state machine.

Five states only: ``AVAILABLE``, ``DEGRADED``, ``COOLDOWN``, ``AUTH_REQUIRED``,
``UNAVAILABLE``. State is keyed by provider; one provider's outage must never
move another provider's state (hard invariant, covered by tests).

This module is harness-neutral: it consumes :class:`~core.errors.ProviderError`
and never reads DSH logs, files, or zstd.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .errors import ErrorCategory, ProviderError, redact

__all__ = [
    "HealthRecord",
    "HealthStatus",
    "ProviderHealthTracker",
    "get_provider_health",
    "record_provider_error",
    "record_provider_success",
    "reset_default_tracker",
]


class HealthStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    COOLDOWN = "COOLDOWN"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"


_CATEGORY_STATUS: dict[ErrorCategory, HealthStatus] = {
    ErrorCategory.RATE_LIMIT: HealthStatus.COOLDOWN,
    ErrorCategory.QUOTA_EXHAUSTED: HealthStatus.COOLDOWN,
    ErrorCategory.AUTH_FAILED: HealthStatus.AUTH_REQUIRED,
    ErrorCategory.NETWORK_ERROR: HealthStatus.DEGRADED,
    ErrorCategory.MODEL_UNAVAILABLE: HealthStatus.DEGRADED,
    ErrorCategory.MODEL_ERROR: HealthStatus.DEGRADED,
    ErrorCategory.UNKNOWN: HealthStatus.DEGRADED,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | str | None) -> str:
    if value is None:
        moment = _utcnow()
    elif isinstance(value, str):
        return value
    elif isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    else:  # pragma: no cover - defensive; annotated types cover real callers
        raise TypeError(f"unsupported timestamp: {value!r}")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _status_for(error: ProviderError) -> HealthStatus:
    if error.category is ErrorCategory.PROVIDER_UNAVAILABLE:
        # A 5xx (or DSH SERVER code) is stronger evidence than a generic
        # "provider unavailable" message, which stays DEGRADED.
        strong = (error.http_status is not None and error.http_status >= 500) or (
            (error.raw_code or "").upper() == "SERVER"
        )
        return HealthStatus.UNAVAILABLE if strong else HealthStatus.DEGRADED
    return _CATEGORY_STATUS.get(error.category, HealthStatus.DEGRADED)


@dataclass(frozen=True)
class HealthRecord:
    """One provider's current health. Serializes to the agreed six fields."""

    provider: str
    status: HealthStatus
    reason: str = ""
    since: str = field(default_factory=lambda: _iso(None))
    retry_after_seconds: float | None = None
    reset_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": redact(self.provider),
            "status": self.status.value,
            "reason": redact(self.reason),
            "since": self.since,
            "retry_after_seconds": self.retry_after_seconds,
            "reset_at": self.reset_at,
        }


class ProviderHealthTracker:
    """In-memory, per-provider health tracker.

    No persistence, no scheduler, no priority, no failover: those belong to
    later goals and are intentionally absent here.
    """

    def __init__(self, clock: Any = None) -> None:
        self._clock = clock or _utcnow
        self._records: dict[str, HealthRecord] = {}

    def _now(self) -> datetime:
        return self._clock()

    def record_success(self, provider: str, *, at: datetime | str | None = None, reason: str = "success") -> HealthRecord:
        record = HealthRecord(
            provider=provider,
            status=HealthStatus.AVAILABLE,
            reason=reason,
            since=_iso(at if at is not None else self._now()),
            retry_after_seconds=None,
            reset_at=None,
        )
        self._records[provider] = record
        return record

    def record_error(self, error: ProviderError, *, at: datetime | str | None = None) -> HealthRecord:
        status = _status_for(error)
        reason = (error.message or error.category.value).strip()
        if len(reason) > 240:
            reason = reason[:240] + "..."
        record = HealthRecord(
            provider=error.provider,
            status=status,
            reason=reason,
            since=_iso(at if at is not None else self._now()),
            retry_after_seconds=error.retry_after_seconds,
            reset_at=error.reset_at,
        )
        self._records[error.provider] = record
        return record

    def record_failure(
        self,
        provider: str,
        category: ErrorCategory,
        *,
        message: str = "",
        retry_after_seconds: float | None = None,
        reset_at: str | None = None,
        at: datetime | str | None = None,
    ) -> HealthRecord:
        error = ProviderError(
            provider=provider,
            category=category,
            message=message,
            retry_after_seconds=retry_after_seconds,
            reset_at=reset_at,
        )
        return self.record_error(error, at=at)

    def get(self, provider: str) -> HealthRecord | None:
        """Return the current record, or ``None`` for a never-observed provider.

        "Never observed" is *not* reported as AVAILABLE: health must be
        evidence-based, and absence of evidence is not availability.
        """

        return self._records.get(provider)

    def all(self) -> dict[str, HealthRecord]:
        return dict(self._records)

    def snapshot(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self._records.values()]


_default_tracker = ProviderHealthTracker()


def get_provider_health(provider: str) -> dict[str, Any] | None:
    """Read interface: current health for *provider* (``None`` if unobserved)."""

    record = _default_tracker.get(provider)
    return record.to_dict() if record is not None else None


def record_provider_error(error: ProviderError) -> dict[str, Any]:
    return _default_tracker.record_error(error).to_dict()


def record_provider_success(provider: str) -> dict[str, Any]:
    return _default_tracker.record_success(provider).to_dict()


def reset_default_tracker() -> None:
    """Test/build helper: drop all health evidence."""

    global _default_tracker
    _default_tracker = ProviderHealthTracker()
