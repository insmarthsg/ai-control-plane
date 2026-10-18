"""Execution contract: vendor neutrality, completion, failure semantics."""

from __future__ import annotations

from pathlib import Path

from core.errors import classify_provider_error
from core.execution import (
    ExecutionFailure,
    ExecutionFailureKind,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
)
from core.task import Artifact

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_execution_contract_is_vendor_neutral() -> None:
    source = (REPO_ROOT / "core" / "execution.py").read_text(encoding="utf-8").lower()
    for token in ("openai", "chatgpt", "glm", "zai", "deepseek", "dsh", "session.v4"):
        assert token not in source, f"execution.py leaks {token!r}"


def test_result_completion_and_artifacts() -> None:
    artifact = Artifact(kind="result", producer_capability="cap-a", payload=89)
    result = ExecutionResult(
        status=ExecutionStatus.COMPLETED,
        artifacts=(artifact,),
        checkpoint="done",
        continuation_ref="opaque-ref-1",
    )
    assert result.is_completed
    assert result.artifacts[0].payload == 89
    assert result.continuation_ref == "opaque-ref-1"


def test_only_provider_failures_update_health() -> None:
    provider_error = classify_provider_error("p-a", message="429", http_status=429)
    provider_failure = ExecutionFailure(
        kind=ExecutionFailureKind.PROVIDER,
        category="RATE_LIMIT",
        provider_error=provider_error,
    )
    execution_failure = ExecutionFailure(
        kind=ExecutionFailureKind.EXECUTION,
        category="WORKSPACE_PERMISSION_DENIED",
    )
    unknown_failure = ExecutionFailure(kind=ExecutionFailureKind.UNKNOWN)

    assert provider_failure.updates_provider_health
    assert not execution_failure.updates_provider_health
    assert not unknown_failure.updates_provider_health


def test_execution_failure_redacts_credentials() -> None:
    failure = ExecutionFailure(
        message="Authorization: Bearer abcdefghijklmnop",
        evidence={"api_key": "sk-test-abcdef123456", "nested": {"password": "hunter2"}},
    )
    assert "abcdefghijklmnop" not in failure.message
    assert failure.evidence["api_key"] == "[REDACTED]"
    assert failure.evidence["nested"]["password"] == "[REDACTED]"


def test_request_redacts_context_and_metadata() -> None:
    request = ExecutionRequest(
        invocation_id="inv-1",
        task_id="task-1",
        objective="do the thing",
        durable_context={"note": "Authorization: Bearer abcdefghijklmnop"},
        execution_mode=ExecutionMode.HANDOFF,
        continuation_ref="opaque/ref#1",
        metadata={"api_key": "sk-test-abcdef123456"},
    )
    assert "abcdefghijklmnop" not in request.durable_context["note"]
    assert request.metadata["api_key"] == "[REDACTED]"
    assert request.continuation_ref == "opaque/ref#1"
