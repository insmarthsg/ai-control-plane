"""ACP-ARCH-01 invariants: Stable Core stays small and vendor-neutral.

These tests are intentionally small. They check the frozen boundaries from
docs/ARCHITECTURE.md, not an abstract plugin framework.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core.errors import ErrorCategory, classify_provider_error
from core.provider_health import HealthStatus, ProviderHealthTracker

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_FILES = sorted((REPO_ROOT / "core").glob("*.py"))

# Vocabulary that belongs to an execution adapter, not to the Stable Core.
FORBIDDEN_CORE_TOKENS = (
    "dsh",
    "transport",
    "pi_ai",
    "account_quota",
    "image_offload",
    "session.v4",
    "zstd",
    "openai",
    "deepseek",
    "zai-coding",
    "glm-",
    "codex",
)


def test_core_has_no_harness_or_vendor_vocabulary() -> None:
    for path in CORE_FILES:
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_CORE_TOKENS:
            assert token not in text, f"{path.name} leaks {token!r} into the Stable Core"


def test_core_has_no_provider_branching() -> None:
    text = (REPO_ROOT / "core" / "provider_health.py").read_text(encoding="utf-8").lower()
    assert "provider ==" not in text
    for provider_name in ("openai", "zai", "deepseek", "glm", "codex", "anthropic"):
        assert provider_name not in text


def test_core_imports_without_any_adapter() -> None:
    program = (
        "import sys\n"
        "import core.errors, core.provider_health\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] == 'adapters')\n"
        "assert not loaded, loaded\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_new_provider_is_opaque_and_needs_no_core_change() -> None:
    tracker = ProviderHealthTracker()
    error = classify_provider_error(
        "fake-harness-alpha",
        message="429 Too Many Requests",
        http_status=429,
        headers={"Retry-After": "7"},
    )
    tracker.record_error(error)

    record = tracker.get("fake-harness-alpha")
    assert record is not None
    assert record.status is HealthStatus.COOLDOWN
    assert record.retry_after_seconds == 7.0

    # No unrelated provider was invented or moved.
    assert tracker.get("openai-codex") is None

    tracker.record_success("fake-harness-alpha")
    assert tracker.get("fake-harness-alpha").status is HealthStatus.AVAILABLE


def test_adapter_owns_the_harness_code_table() -> None:
    from adapters.dsh import DSH_CODE_CATEGORIES

    assert DSH_CODE_CATEGORIES["TRANSPORT"] is ErrorCategory.NETWORK_ERROR
    assert DSH_CODE_CATEGORIES["QUOTA"] is ErrorCategory.QUOTA_EXHAUSTED
    assert DSH_CODE_CATEGORIES["SERVER"] is ErrorCategory.PROVIDER_UNAVAILABLE
