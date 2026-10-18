"""Provider error taxonomy, classification, and credential redaction.

This module is harness- and vendor-neutral. It accepts plain evidence (HTTP
status, headers, message text, and an optional adapter-supplied category hint)
and must never learn a harness's log format, error-code vocabulary, or
credential store.

Evidence rules (locked by GOAL-01):

* A 429 without explicit quota evidence is ``RATE_LIMIT``, never
  ``QUOTA_EXHAUSTED``.
* ``retry_after_seconds`` / ``reset_at`` are populated only from reliable,
  explicit evidence (an HTTP ``Retry-After`` header, an adapter-supplied
  retry-after value, or an explicit caller value). No provider's folklore
  reset window is encoded here.
* Unknown failures are allowed to stay ``UNKNOWN``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "ErrorCategory",
    "ProviderError",
    "classify_provider_error",
    "parse_retry_after",
    "redact",
]


class ErrorCategory(str, Enum):
    """The only eight provider error categories the control plane knows."""

    RATE_LIMIT = "RATE_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_FAILED = "AUTH_FAILED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    NETWORK_ERROR = "NETWORK_ERROR"
    MODEL_ERROR = "MODEL_ERROR"
    UNKNOWN = "UNKNOWN"


# Explicit terminal-quota wording used by major providers. Only
# quota/balance/credit/budget wording counts; a bare 429 never does.
_QUOTA_EVIDENCE = re.compile(
    r"insufficient[\s_-]+(?:quota|balance|credits?)"
    r"|(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)"
    r"|exceed(?:ed|s)?[\s_-]+(?:(?:your|the)[\s_-]+)?(?:current[\s_-]+)?quota"
    r"|(?:balance|credits?)[\s_-]+(?:exhausted|depleted)"
    r"|out[\s_-]+of[\s_-]+(?:credits?|budget)",
    re.IGNORECASE,
)

_NETWORK_EVIDENCE = re.compile(
    r"\btime(?:d)?[\s_-]*out\b|\btimeout\b|\bnetwork\b|\bconnection\b|\bsocket\b"
    r"|fetch failed|\bECONN[A-Z]+\b|\bENOTFOUND\b|\bEAI_AGAIN\b|\bTLS\b"
    r"|other side closed|premature close",
    re.IGNORECASE,
)

_MODEL_UNAVAILABLE_EVIDENCE = re.compile(
    r"\bmodel\b.{0,40}\b(?:not[\s_-]+found|not[\s_-]+available|unavailable"
    r"|does[\s_-]+not[\s_-]+exist|decommissioned|deprecated)\b"
    r"|no[\s_-]+such[\s_-]+model",
    re.IGNORECASE,
)

_MODEL_EVIDENCE = re.compile(
    r"context[\s_-]+(?:length|window)|maximum[\s_-]+context|too[\s_-]+(?:long|large)"
    r"|invalid[\s_-]+request|\b400\b|\b413\b|\b422\b",
    re.IGNORECASE,
)

_PROVIDER_EVIDENCE = re.compile(
    r"service[\s_-]+unavailable|overloaded|server[\s_-]+error|\b50[0-4]\b",
    re.IGNORECASE,
)

# Order matters: the bearer/api-key patterns consume the secret before the
# generic "Authorization: ..." pattern can mangle the label.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(api[_-]?key|authorization)\b\s*[:=]\s*(?:Bearer\s+)?[\"']?[^\s\"',;}]+"
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(access_token|refresh_token|id_token)\b\s*[:=]\s*[\"']?[^\s\"',;}]+"),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(accountId|account_id)\b\s*[:=]\s*[\"']?[^\s\"',;}]+"),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{8,}"),
        "[REDACTED_API_KEY]",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
        "[REDACTED_JWT]",
    ),
)


def redact(text: Any) -> str:
    """Return *text* with credentials and account identifiers removed.

    This is the single redaction choke point. Every provider-facing string
    (error message, health reason, serialized dict) passes through it.
    """

    rendered = text if isinstance(text, str) else str(text)
    for pattern, replacement in _REDACTIONS:
        rendered = pattern.sub(replacement, rendered)
    return rendered


def _as_http_status(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _as_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return redact(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_retry_after(
    headers: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> tuple[float | None, str | None]:
    """Extract ``(retry_after_seconds, reset_at)`` from HTTP headers.

    Supports ``Retry-After`` (delta seconds or HTTP-date) and
    ``Retry-After-Ms``. Anything else, including absent headers, yields
    ``(None, None)``. No default/backoff value is invented.
    """

    if not headers:
        return None, None

    normalized = {str(key).strip().lower(): value for key, value in headers.items()}
    current = now or _utcnow()

    raw_ms = normalized.get("retry-after-ms")
    if raw_ms not in (None, ""):
        try:
            milliseconds = float(str(raw_ms).strip())
        except (TypeError, ValueError):
            milliseconds = -1.0
        if milliseconds >= 0:
            return milliseconds / 1000.0, None

    raw = normalized.get("retry-after")
    if raw in (None, ""):
        return None, None

    candidate = str(raw).strip()

    # Delta-seconds form.
    try:
        seconds = float(candidate)
    except ValueError:
        seconds = -1.0
    if seconds >= 0:
        return seconds, None

    # HTTP-date form: the absolute reset instant is reliable evidence.
    try:
        moment = parsedate_to_datetime(candidate)
    except (TypeError, ValueError, IndexError):
        return None, None
    if moment is None:
        return None, None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = (moment.astimezone(timezone.utc) - current).total_seconds()
    if delta < 0:
        delta = 0.0
    return delta, _as_iso(moment)


def _has_quota_evidence(text: str) -> bool:
    return bool(_QUOTA_EVIDENCE.search(text))


def _classify(
    hint: ErrorCategory | None,
    message: str,
    http_status: int | None,
) -> ErrorCategory:
    text = message

    # A terminal quota signal is reliable independent of HTTP status.
    if hint is ErrorCategory.QUOTA_EXHAUSTED:
        return ErrorCategory.QUOTA_EXHAUSTED

    # 429 is ambiguous by nature: quota wording is required to escalate it.
    if http_status == 429:
        return ErrorCategory.QUOTA_EXHAUSTED if _has_quota_evidence(text) else ErrorCategory.RATE_LIMIT

    # Adapters may translate their harness's canonical codes into a category
    # hint. Core never learns those codes.
    if hint is not None:
        if hint is ErrorCategory.RATE_LIMIT and _has_quota_evidence(text):
            return ErrorCategory.QUOTA_EXHAUSTED
        return hint

    if http_status in (401, 403):
        return ErrorCategory.QUOTA_EXHAUSTED if _has_quota_evidence(text) else ErrorCategory.AUTH_FAILED

    if http_status is not None and 500 <= http_status <= 599:
        return ErrorCategory.PROVIDER_UNAVAILABLE

    if _MODEL_UNAVAILABLE_EVIDENCE.search(text):
        return ErrorCategory.MODEL_UNAVAILABLE

    if http_status == 404:
        return ErrorCategory.MODEL_UNAVAILABLE if _MODEL_UNAVAILABLE_EVIDENCE.search(text) else ErrorCategory.UNKNOWN

    if _NETWORK_EVIDENCE.search(text):
        return ErrorCategory.NETWORK_ERROR

    if _MODEL_EVIDENCE.search(text):
        return ErrorCategory.MODEL_ERROR

    if _PROVIDER_EVIDENCE.search(text):
        return ErrorCategory.PROVIDER_UNAVAILABLE

    return ErrorCategory.UNKNOWN


@dataclass(frozen=True)
class ProviderError:
    """A classified, credential-safe provider failure.

    ``message`` is redacted at construction, so ``repr()``, ``str()`` and
    ``to_dict()`` cannot leak secrets even if a caller logs the object
    directly. ``raw_code`` keeps the harness/provider code for diagnostics.
    """

    provider: str
    category: ErrorCategory
    message: str = field(default="", repr=False)
    raw_code: str | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None
    reset_at: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", redact(self.provider))
        object.__setattr__(self, "message", redact(self.message))
        object.__setattr__(self, "raw_code", redact(self.raw_code) if self.raw_code else None)
        object.__setattr__(self, "reset_at", _as_iso(self.reset_at))
        object.__setattr__(
            self,
            "evidence",
            {redact(key): redact(value) for key, value in dict(self.evidence).items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": redact(self.provider),
            "category": self.category.value,
            "message": redact(self.message),
            "raw_code": redact(self.raw_code) if self.raw_code else None,
            "http_status": self.http_status,
            "retry_after_seconds": self.retry_after_seconds,
            "reset_at": _as_iso(self.reset_at),
            "evidence": dict(self.evidence),
        }

    def __str__(self) -> str:
        detail = f" ({self.raw_code})" if self.raw_code else ""
        return f"{self.provider}: {self.category.value}{detail}: {self.message}"


def classify_provider_error(
    provider: str,
    *,
    message: str = "",
    code: str | None = None,
    category_hint: ErrorCategory | None = None,
    http_status: int | None = None,
    headers: Mapping[str, Any] | None = None,
    retry_after_seconds: float | None = None,
    reset_at: Any = None,
    now: datetime | None = None,
) -> ProviderError:
    """Classify one provider failure into :class:`ProviderError`.

    ``code`` is opaque diagnostic metadata (stored as ``raw_code``); this
    module never interprets a harness-specific code string. Adapters that do
    know their harness's canonical codes may translate them into a neutral
    ``category_hint``. Without a hint, classification falls back to
    message/HTTP heuristics and may legitimately produce ``UNKNOWN``.
    """

    status = _as_http_status(http_status)
    safe_message = redact(message)
    code_normalized = code.strip().upper() if isinstance(code, str) and code.strip() else None
    hint = category_hint if isinstance(category_hint, ErrorCategory) else None

    header_retry_after, header_reset_at = parse_retry_after(headers, now=now)
    if retry_after_seconds is None:
        retry_after_seconds = header_retry_after
    else:
        try:
            retry_after_seconds = float(retry_after_seconds)
        except (TypeError, ValueError):
            retry_after_seconds = None
    if retry_after_seconds is not None and retry_after_seconds < 0:
        retry_after_seconds = None
    if reset_at is None:
        reset_at = header_reset_at

    category = _classify(hint, safe_message, status)

    evidence: dict[str, Any] = {}
    if hint is not None:
        evidence["category_hint"] = hint.value
    if status is not None:
        evidence["http_status"] = status

    return ProviderError(
        provider=provider,
        category=category,
        message=safe_message,
        raw_code=code_normalized,
        http_status=status,
        retry_after_seconds=retry_after_seconds,
        reset_at=_as_iso(reset_at),
        evidence=evidence,
    )
