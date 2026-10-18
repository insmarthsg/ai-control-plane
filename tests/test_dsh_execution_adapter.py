"""DSH execution adapter protocol tests against a fake SDK stdio peer.

These tests verify the bridge wire behaviour. They never claim a real provider
executed; see tests/test_dsh_execution_real.py for the marked real smoke.
"""

from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

import pytest

from core.execution import (
    ExecutionFailure,
    ExecutionFailureKind,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
)
REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PEER = REPO_ROOT / "tests" / "helpers" / "fake_dsh_runtime.py"


def make_adapter(tmp_path: Path, **overrides):
    from adapters.dsh_execution import DSHExecutionAdapter

    defaults = dict(
        provider="fake-provider",
        model="fake-model",
        runtime_cmd=[sys.executable, str(FAKE_PEER)],
        dsh_home=str(tmp_path / "dsh-home"),
        cwd=str(tmp_path),
        timeout_seconds=10.0,
        env_extra={"FAKE_STORE_DIR": str(tmp_path / "store")},
    )
    defaults.update(overrides)
    return DSHExecutionAdapter(**defaults)


def make_request(mode=ExecutionMode.FRESH, *, objective="demo task", **kwargs):
    return ExecutionRequest(
        invocation_id=str(uuid.uuid4()),
        task_id="task-1",
        objective=objective,
        execution_mode=mode,
        **kwargs,
    )


def test_fresh_execution_completes_with_marker(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    result = adapter.execute(make_request(objective="demo MARK_COMPLETED"))

    assert isinstance(result, ExecutionResult)
    assert result.status is ExecutionStatus.COMPLETED
    assert "ACP_EXECUTION_STATUS=COMPLETED" in (result.output or "")
    assert result.continuation_ref and result.continuation_ref.startswith("dsh-session:")
    session_id = result.continuation_ref[len("dsh-session:"):]
    uuid.UUID(session_id)  # opaque, bridge-owned session id
    assert result.metadata["message_id"]
    assert result.metadata["response_id"] == "resp_fake_0001"
    assert result.metadata["usage"]["totalTokens"] > 0
    assert result.metadata["fallback_used"] is False
    assert result.artifacts[0].payload["text"].startswith("demo output")

    # The execution id is a bridge-owned opaque UUID that encodes nothing.
    execution = adapter.get_execution(result.metadata["execution_id"])
    assert execution is not None
    assert execution["status"] in {"COMPLETED", "PROGRESSED"}
    uuid.UUID(result.metadata["execution_id"])
    assert "fake-provider" not in result.metadata["execution_id"]
    assert session_id not in result.metadata["execution_id"]
    assert adapter.active_executions() == []


def test_progress_without_marker_stays_progress(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    result = adapter.execute(make_request(objective="plain work"))

    assert isinstance(result, ExecutionResult)
    assert result.status is ExecutionStatus.PROGRESS
    assert "ACP_EXECUTION_STATUS=COMPLETED" not in (result.output or "")


def test_continue_cross_process_resumes_session(tmp_path: Path) -> None:
    first = make_adapter(tmp_path)
    first_result = first.execute(make_request(objective="turn one"))
    assert isinstance(first_result, ExecutionResult)
    ref = first_result.continuation_ref
    assert ref is not None

    # A brand-new adapter = a brand-new runtime process; the session must
    # survive through the persisted store, not process memory.
    second = make_adapter(tmp_path)
    second_result = second.execute(
        make_request(ExecutionMode.CONTINUE, objective="turn two MARK_COMPLETED",
                     continuation_ref=ref)
    )
    assert isinstance(second_result, ExecutionResult)
    assert second_result.status is ExecutionStatus.COMPLETED
    assert "PREV_TURNS:1" in (second_result.output or "")
    assert "RESUMED:true" in (second_result.output or "")
    assert second_result.continuation_ref == ref


def test_handoff_creates_new_session_without_resume(tmp_path: Path) -> None:
    first = make_adapter(tmp_path)
    first_result = first.execute(make_request(objective="original executor"))
    assert isinstance(first_result, ExecutionResult)
    old_ref = first_result.continuation_ref

    second = make_adapter(tmp_path)
    second_result = second.execute(
        make_request(ExecutionMode.HANDOFF, objective="handed off MARK_COMPLETED",
                     durable_context={"constraint": "no schema changes"},
                     checkpoint="after-analysis",
                     handoff_reason="previous capability unavailable",
                     continuation_ref=old_ref)  # must be ignored for HANDOFF
    )
    assert isinstance(second_result, ExecutionResult)
    assert second_result.status is ExecutionStatus.COMPLETED
    assert second_result.continuation_ref != old_ref
    assert "RESUMED:" not in (second_result.output or "")


def test_provider_failure_is_normalized_with_retry_after(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    failure = adapter.execute(make_request(objective="TRIGGER_PROVIDER_FAILURE"))

    assert isinstance(failure, ExecutionFailure)
    assert failure.kind is ExecutionFailureKind.PROVIDER
    assert failure.category == "RATE_LIMIT"
    assert failure.retryable is True
    assert failure.provider_error is not None
    assert failure.provider_error.retry_after_seconds == 30.0
    assert failure.metadata["fallback_used"] is False
    assert failure.continuation_ref and failure.continuation_ref.startswith("dsh-session:")


def test_invalid_continuation_ref_fails_without_runtime_abuse(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    failure = adapter.execute(
        make_request(ExecutionMode.CONTINUE, objective="bad ref", continuation_ref="not-a-ref")
    )
    assert isinstance(failure, ExecutionFailure)
    assert failure.category == "INVALID_CONTINUATION"
    assert failure.kind is ExecutionFailureKind.EXECUTION


def test_timeout_terminates_runtime(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, timeout_seconds=2.0)
    failure = adapter.execute(make_request(objective="SLOW_WAIT forever"))

    assert isinstance(failure, ExecutionFailure)
    assert failure.category == "EXECUTION_TIMEOUT"
    assert failure.kind is ExecutionFailureKind.EXECUTION
    pid = failure.metadata["runtime_pid"]
    with pytest.raises(ProcessLookupError):
        import os

        os.kill(pid, 0)


def test_terminate_stops_running_execution(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, timeout_seconds=60.0)
    outcome: dict = {}

    def run() -> None:
        outcome["failure"] = adapter.execute(make_request(objective="SLOW_WAIT terminate me"))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(100):
        if adapter.active_executions():
            break
        import time

        time.sleep(0.05)
    active = adapter.active_executions()
    assert active, "execution did not register"
    assert adapter.terminate(active[0]["execution_id"]) is True

    thread.join(timeout=15)
    failure = outcome.get("failure")
    assert isinstance(failure, ExecutionFailure)
    assert failure.category == "TERMINATED_BY_REQUEST"
    assert adapter.active_executions() == []


def test_spawn_failure_maps_to_execution_kind(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, runtime_cmd=["definitely-not-a-real-binary-xyz"])
    failure = adapter.execute(make_request(objective="spawn fail"))
    assert isinstance(failure, ExecutionFailure)
    assert failure.category == "RUNTIME_SPAWN_FAILED"
    assert failure.kind is ExecutionFailureKind.EXECUTION


def test_registry_cleanup_after_finish(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    result = adapter.execute(make_request(objective="cleanup MARK_COMPLETED"))
    assert isinstance(result, ExecutionResult)
    assert adapter.active_executions() == []
    record = adapter.get_execution(result.metadata["execution_id"])
    assert record is not None and record["status"] == "COMPLETED"
