"""GOAL-01 acceptance tests: taxonomy, health transitions, redaction, fidelity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from core.errors import ErrorCategory, classify_provider_error, parse_retry_after, redact
from core.provider_health import (
    HealthStatus,
    ProviderHealthTracker,
    get_provider_health,
    record_provider_success,
    reset_default_tracker,
)


def _health(tracker: ProviderHealthTracker, provider: str) -> dict:
    record = tracker.get(provider)
    assert record is not None
    return record.to_dict()


def test_429_with_retry_after_maps_to_rate_limit_and_cooldown() -> None:
    tracker = ProviderHealthTracker()
    error = classify_provider_error(
        "openai-codex",
        message="429 Too Many Requests",
        http_status=429,
        headers={"Retry-After": "42"},
    )

    assert error.category is ErrorCategory.RATE_LIMIT
    assert error.retry_after_seconds == 42.0
    assert error.reset_at is None

    tracker.record_error(error)
    health = _health(tracker, "openai-codex")
    assert health["status"] == "COOLDOWN"
    assert health["retry_after_seconds"] == 42.0


def test_explicit_quota_evidence_maps_to_quota_exhausted_and_cooldown() -> None:
    tracker = ProviderHealthTracker()
    error = classify_provider_error(
        "openai-codex",
        message="You exceeded your current quota. Please check your plan and billing details.",
        http_status=429,
    )

    assert error.category is ErrorCategory.QUOTA_EXHAUSTED
    tracker.record_error(error)
    assert _health(tracker, "openai-codex")["status"] == "COOLDOWN"

    # A canonical quota code is sufficient evidence on its own.
    coded = classify_provider_error(
        "openai-codex", category_hint=ErrorCategory.QUOTA_EXHAUSTED, http_status=429
    )
    assert coded.category is ErrorCategory.QUOTA_EXHAUSTED


def test_401_maps_to_auth_failed_and_auth_required() -> None:
    tracker = ProviderHealthTracker()
    error = classify_provider_error(
        "zai-coding-cn",
        message="401 Unauthorized: invalid_api_key",
        http_status=401,
    )

    assert error.category is ErrorCategory.AUTH_FAILED
    tracker.record_error(error)
    assert _health(tracker, "zai-coding-cn")["status"] == "AUTH_REQUIRED"


def test_5xx_maps_to_provider_unavailable_and_unavailable() -> None:
    tracker = ProviderHealthTracker()
    error = classify_provider_error(
        "zai-coding-cn",
        message="503 Service Unavailable",
        http_status=503,
    )

    assert error.category is ErrorCategory.PROVIDER_UNAVAILABLE
    tracker.record_error(error)
    assert _health(tracker, "zai-coding-cn")["status"] == "UNAVAILABLE"


def test_timeout_maps_to_network_error_and_degraded() -> None:
    tracker = ProviderHealthTracker()
    error = classify_provider_error(
        "openai-codex",
        message="Request timed out after 30s",
    )

    assert error.category is ErrorCategory.NETWORK_ERROR
    tracker.record_error(error)
    assert _health(tracker, "openai-codex")["status"] == "DEGRADED"


def test_unknown_failure_stays_unknown_and_degraded() -> None:
    tracker = ProviderHealthTracker()
    error = classify_provider_error("glm", message="something odd happened")

    assert error.category is ErrorCategory.UNKNOWN
    tracker.record_error(error)
    assert _health(tracker, "glm")["status"] == "DEGRADED"


def test_success_marks_available_and_clears_retry_evidence() -> None:
    tracker = ProviderHealthTracker()
    tracker.record_error(
        classify_provider_error(
            "openai-codex",
            message="429 Too Many Requests",
            http_status=429,
            headers={"Retry-After": "60"},
        )
    )
    assert _health(tracker, "openai-codex")["status"] == "COOLDOWN"

    tracker.record_success("openai-codex")
    health = _health(tracker, "openai-codex")
    assert health["status"] == "AVAILABLE"
    assert health["retry_after_seconds"] is None
    assert health["reset_at"] is None


def test_provider_isolation_is_a_hard_invariant() -> None:
    tracker = ProviderHealthTracker()
    tracker.record_error(
        classify_provider_error(
            "openai-codex",
            message="insufficient_quota",
            http_status=429,
        )
    )
    tracker.record_success("zai-coding-cn")

    assert _health(tracker, "openai-codex")["status"] == "COOLDOWN"
    assert _health(tracker, "zai-coding-cn")["status"] == "AVAILABLE"

    tracker.record_success("openai-codex")
    assert _health(tracker, "zai-coding-cn")["status"] == "AVAILABLE"

    tracker.record_error(
        classify_provider_error("zai-coding-cn", message="401 unauthorized", http_status=401)
    )
    assert _health(tracker, "openai-codex")["status"] == "AVAILABLE"
    assert _health(tracker, "zai-coding-cn")["status"] == "AUTH_REQUIRED"


def test_credential_redaction_hides_all_secret_shapes() -> None:
    secrets = {
        "bearer": "Bearer abcdefghijklmnopqrstuvwxyz012345",
        "api_key": "sk-proj-abcdefgh12345678",
        "jwt": (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        ),
        "raw_key": "api_key=plainsecret123",
        "account": "accountId=deadbeef-0000-4000-8000-000000000000",
    }
    message = " | ".join(secrets.values())

    error = classify_provider_error("openai-codex", message=message, http_status=401)
    tracker = ProviderHealthTracker()
    tracker.record_error(error)

    rendered = " ".join(
        [
            str(error),
            repr(error),
            error.to_dict()["message"],
            _health(tracker, "openai-codex")["reason"],
        ]
    )

    assert "abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "sk-proj" not in rendered
    assert "eyJhbGciOiJIUzI1NiJ9" not in rendered
    assert "plainsecret123" not in rendered
    assert "deadbeef" not in rendered
    assert "[REDACTED" in rendered or "REDACTED" in rendered

    # Public helper is idempotent and label-preserving.
    assert "plainsecret123" not in redact(secrets["raw_key"])


def test_retry_after_fidelity_never_invents_a_reset() -> None:
    seconds, reset = parse_retry_after({"Retry-After": "90"})
    assert seconds == 90.0
    assert reset is None

    milliseconds, _ = parse_retry_after({"Retry-After-Ms": "1500"})
    assert milliseconds == 1.5

    assert parse_retry_after({"Retry-After": "not-a-date"}) == (None, None)
    assert parse_retry_after({"Retry-After": "-5"}) == (None, None)
    assert parse_retry_after({}) == (None, None)

    now = datetime.now(timezone.utc)
    absolute = format_datetime(now + timedelta(seconds=120), usegmt=True)
    delta, reset_at = parse_retry_after({"Retry-After": absolute}, now=now)
    assert delta == pytest.approx(120.0, abs=2.0)
    assert reset_at is not None and reset_at.endswith("Z")

    # The crucial negative: a bare 429 must not fabricate the 5-hour myth.
    error = classify_provider_error("openai-codex", message="429", http_status=429)
    assert error.retry_after_seconds is None
    assert error.reset_at is None
    assert error.category is ErrorCategory.RATE_LIMIT


def test_read_interface_shape_and_unknown_provider() -> None:
    reset_default_tracker()
    try:
        # Never-observed is absence of evidence, not AVAILABLE.
        assert get_provider_health("openai-codex") is None
        health = record_provider_success("openai-codex")
        assert set(health) == {
            "provider",
            "status",
            "reason",
            "since",
            "retry_after_seconds",
            "reset_at",
        }
        assert health["status"] == "AVAILABLE"
    finally:
        reset_default_tracker()
