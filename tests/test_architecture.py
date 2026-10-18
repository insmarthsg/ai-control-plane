"""ACP-ARCH-01 invariants: Stable Core stays small and vendor-neutral.

These tests are intentionally small. They check the frozen boundaries from
docs/ARCHITECTURE.md, not an abstract plugin framework.
"""

from __future__ import annotations

import ast
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
        "import core.errors, core.provider_health, core.task, core.task_store\n"
        "import core.capability, core.router, core.execution, core.controller, core.runtime, core.human\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in {'adapters', 'stores'})\n"
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


def test_task_core_has_no_storage_or_harness_imports() -> None:
    forbidden_roots = {
        "json",
        "pathlib",
        "sqlite3",
        "os",
        "fcntl",
        "tempfile",
        "stores",
        "adapters",
        "subprocess",
    }
    for path in (
        sorted((REPO_ROOT / "core").glob("task*.py"))
        + sorted((REPO_ROOT / "core").glob("router*.py"))
        + sorted((REPO_ROOT / "core").glob("capability*.py"))
        + sorted((REPO_ROOT / "core").glob("controller*.py"))
        + sorted((REPO_ROOT / "core").glob("execution*.py"))
        + sorted((REPO_ROOT / "core").glob("runtime*.py"))
        + sorted((REPO_ROOT / "core").glob("human*.py"))
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                assert root not in forbidden_roots, f"{path.name} imports {name}"


def test_router_is_selection_only() -> None:
    source = (REPO_ROOT / "core" / "router.py").read_text(encoding="utf-8")
    for forbidden in ("def execute", "def retry", "while ", "task_store", "import stores"):
        assert forbidden not in source, f"router.py contains {forbidden!r}"


def test_task_core_has_no_harness_or_pipeline_vocabulary() -> None:
    forbidden_tokens = (
        "dsh",
        "session.v4",
        "zstd",
        "openai",
        "deepseek",
        "zai-coding",
        "glm-",
        "codex",
        "planner",
        "worker",
        "reviewer",
        "pipeline",
        "next_stage",
        "workflow_steps",
        "stage",
    )
    for path in (
        sorted((REPO_ROOT / "core").glob("task*.py"))
        + sorted((REPO_ROOT / "core").glob("router*.py"))
        + sorted((REPO_ROOT / "core").glob("capability*.py"))
        + sorted((REPO_ROOT / "core").glob("controller*.py"))
        + sorted((REPO_ROOT / "core").glob("execution*.py"))
        + sorted((REPO_ROOT / "core").glob("runtime*.py"))
        + sorted((REPO_ROOT / "core").glob("human*.py"))
    ):
        text = path.read_text(encoding="utf-8").lower()
        for token in forbidden_tokens:
            assert token not in text, f"{path.name} leaks {token!r}"


def test_human_gate_never_executes_or_cancels() -> None:
    source = (REPO_ROOT / "core" / "human.py").read_text(encoding="utf-8")
    for forbidden in ("def execute(", "def terminate(", "def cancel(", "RuntimeManager", "import adapters"):
        assert forbidden not in source, f"core/human.py contains {forbidden!r}"


def test_human_gate_does_not_import_runtime_or_adapters() -> None:
    tree = ast.parse((REPO_ROOT / "core" / "human.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") not in {"runtime", "adapters", "adapters.dsh"}, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {"runtime", "adapters"}
