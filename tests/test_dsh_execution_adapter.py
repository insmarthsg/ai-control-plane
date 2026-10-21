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
        require_process_tree=False,  # Explicit group-only protocol test executor.
        permission_mode="read-only",
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


def make_request(workspace, mode=ExecutionMode.FRESH, *, objective="demo task", **kwargs):
    kwargs.setdefault("workspace_path", str(workspace))
    return ExecutionRequest(
        invocation_id=str(uuid.uuid4()),
        task_id="task-1",
        objective=objective,
        execution_mode=mode,
        **kwargs,
    )


def test_fresh_execution_completes_with_marker(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    result = adapter.execute(make_request(tmp_path, objective="demo MARK_COMPLETED"))

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
    result = adapter.execute(make_request(tmp_path, objective="plain work"))

    assert isinstance(result, ExecutionResult)
    assert result.status is ExecutionStatus.PROGRESS
    assert "ACP_EXECUTION_STATUS=COMPLETED" not in (result.output or "")


def test_continue_cross_process_resumes_session(tmp_path: Path) -> None:
    first = make_adapter(tmp_path)
    first_result = first.execute(make_request(tmp_path, objective="turn one"))
    assert isinstance(first_result, ExecutionResult)
    ref = first_result.continuation_ref
    assert ref is not None

    # A brand-new adapter = a brand-new runtime process; the session must
    # survive through the persisted store, not process memory.
    second = make_adapter(tmp_path)
    second_result = second.execute(
        make_request(tmp_path, ExecutionMode.CONTINUE, objective="turn two MARK_COMPLETED",
                     continuation_ref=ref)
    )
    assert isinstance(second_result, ExecutionResult)
    assert second_result.status is ExecutionStatus.COMPLETED
    assert "PREV_TURNS:1" in (second_result.output or "")
    assert "RESUMED:true" in (second_result.output or "")
    assert second_result.continuation_ref == ref


def test_handoff_creates_new_session_without_resume(tmp_path: Path) -> None:
    first = make_adapter(tmp_path)
    first_result = first.execute(make_request(tmp_path, objective="original executor"))
    assert isinstance(first_result, ExecutionResult)
    old_ref = first_result.continuation_ref

    second = make_adapter(tmp_path)
    second_result = second.execute(
        make_request(tmp_path, ExecutionMode.HANDOFF, objective="handed off MARK_COMPLETED",
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
    failure = adapter.execute(make_request(tmp_path, objective="TRIGGER_PROVIDER_FAILURE"))

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
        make_request(tmp_path, ExecutionMode.CONTINUE, objective="bad ref", continuation_ref="not-a-ref")
    )
    assert isinstance(failure, ExecutionFailure)
    assert failure.category == "INVALID_CONTINUATION"
    assert failure.kind is ExecutionFailureKind.EXECUTION


def test_timeout_terminates_runtime(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, timeout_seconds=2.0)
    failure = adapter.execute(make_request(tmp_path, objective="SLOW_WAIT forever"))

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
        outcome["failure"] = adapter.execute(make_request(tmp_path, objective="SLOW_WAIT terminate me"))

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
    failure = adapter.execute(make_request(tmp_path, objective="spawn fail"))
    assert isinstance(failure, ExecutionFailure)
    assert failure.category == "RUNTIME_SPAWN_FAILED"
    assert failure.kind is ExecutionFailureKind.EXECUTION


def test_registry_cleanup_after_finish(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    result = adapter.execute(make_request(tmp_path, objective="cleanup MARK_COMPLETED"))
    assert isinstance(result, ExecutionResult)
    assert adapter.active_executions() == []
    record = adapter.get_execution(result.metadata["execution_id"])
    assert record is not None and record["status"] == "COMPLETED"


def test_runtime_manager_cancel_reaches_dsh_runtime(tmp_path: Path) -> None:
    from core.runtime import CancelStatus, RuntimeManager, RuntimeStatus
    from core.execution import ExecutionRequest

    adapter = make_adapter(tmp_path, timeout_seconds=60.0)
    manager = RuntimeManager({"dsh": adapter}, cancel_timeout=15.0)
    record = manager.start(
        capability_id="dsh",
        request=ExecutionRequest(
            invocation_id="inv-cancel",
            task_id="task-cancel",
            objective="SLOW_WAIT until cancelled",
            workspace_path=str(tmp_path),
        ),
    )
    deadline = __import__("time").monotonic() + 10
    while __import__("time").monotonic() < deadline:
        snapshot = manager.status(record.execution_id)
        if (
            snapshot is not None
            and snapshot.status is RuntimeStatus.RUNNING
            and adapter.get_execution(record.execution_id) is not None
        ):
            break
        __import__("time").sleep(0.05)
    assert manager.status(record.execution_id).status is RuntimeStatus.RUNNING
    # The adapter must have used the manager's execution_id for its own handle.
    assert adapter.get_execution(record.execution_id) is not None

    result = manager.cancel(record.execution_id)
    assert result.status is CancelStatus.CANCELLED
    assert result.confirmed is True
    assert manager.status(record.execution_id).status is RuntimeStatus.STOPPED
    internal = adapter.get_execution(record.execution_id)
    assert internal is not None and internal["status"] == "TERMINATED"


def test_adapter_uses_request_workspace_path_as_spawn_cwd(tmp_path: Path) -> None:
    bound = tmp_path / "bound-workspace"
    bound.mkdir()
    adapter = make_adapter(tmp_path, cwd=str(tmp_path / "caller-cwd"))
    result = adapter.execute(
        make_request(tmp_path, objective="MARK_COMPLETED", workspace_path=str(bound))
    )
    assert isinstance(result, ExecutionResult)
    assert f"CWD:{bound}" in (result.output or "")


@pytest.mark.parametrize("trigger, category", [
    ("TOOL_SANDBOX_UNAVAILABLE", "SANDBOX_UNAVAILABLE"),
    ("TURN_SANDBOX_UNAVAILABLE", "SANDBOX_UNAVAILABLE"),
    ("MISSING_SANDBOX_MODE", "SANDBOX_MODE_UNVERIFIED"),
    ("MISMATCH_SANDBOX_MODE", "SANDBOX_MODE_MISMATCH"),
])
def test_sandbox_fail_closed_despite_completion_marker(tmp_path, trigger, category):
    adapter = make_adapter(tmp_path)
    outcome = adapter.execute(make_request(tmp_path, objective=f"{trigger} MARK_COMPLETED"))
    assert isinstance(outcome, ExecutionFailure)
    assert outcome.kind is ExecutionFailureKind.EXECUTION
    assert outcome.category == category
    assert outcome.provider_error is None
    assert outcome.metadata["process_group_cleared"] is True
    assert adapter.get_execution(outcome.metadata["execution_id"])["status"] == "FAILED"


@pytest.mark.parametrize("mode", [None, "", "unrestricted", 123])
def test_missing_or_invalid_access_mode_never_spawns(tmp_path, mode):
    adapter = make_adapter(tmp_path, permission_mode=mode,
                           runtime_cmd=["must-not-be-spawned"])
    failure = adapter.execute(make_request(tmp_path))
    assert isinstance(failure, ExecutionFailure)
    assert failure.category == "ACCESS_MODE_REQUIRED"
    assert failure.metadata["runtime_pid"] is None


@pytest.mark.parametrize("path_kind", ["missing", "nonexistent", "file"])
def test_workspace_is_required_and_must_be_directory(tmp_path, path_kind):
    path = None
    if path_kind == "nonexistent":
        path = str(tmp_path / "missing")
    elif path_kind == "file":
        file = tmp_path / "file.txt"
        file.write_text("not a directory")
        path = str(file)
    adapter = make_adapter(tmp_path, runtime_cmd=["must-not-be-spawned"])
    failure = adapter.execute(make_request(tmp_path, workspace_path=path))
    assert isinstance(failure, ExecutionFailure)
    assert failure.category == "WORKSPACE_PATH_REQUIRED"
    assert failure.metadata["runtime_pid"] is None


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_request_mode_overrides_constructor_and_environment(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("DSH_PERMISSION_MODE", "unrestricted")
    adapter = make_adapter(tmp_path, permission_mode="invalid-fallback", env_extra={
        "FAKE_STORE_DIR": str(tmp_path / "store"), "DSH_PERMISSION_MODE": "unrestricted"})
    result = adapter.execute(make_request(tmp_path, access_mode=mode, objective="MARK_COMPLETED"))
    assert isinstance(result, ExecutionResult)
    assert result.metadata["sandbox_mode"] == mode
    assert result.metadata["permission_preset"] == mode
    record = adapter.get_execution(result.metadata["execution_id"])
    assert record["task_id"] == "task-1"
    assert record["pgid"] == record["runtime_pid"]
    assert record["process_group_cleared"] is True


def test_workspace_symlink_is_canonicalized(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = tmp_path / "link"
    link.symlink_to(workspace, target_is_directory=True)
    result = make_adapter(tmp_path).execute(make_request(link, objective="MARK_COMPLETED"))
    assert isinstance(result, ExecutionResult)
    assert f"CWD:{workspace.resolve()}" in result.output


@pytest.mark.parametrize("trigger,modes,category", [
    ("REPEATED_SANDBOX_MODE", ["read-only", "read-only"], None),
    ("CONFLICTING_SANDBOX_MODE", ["read-only", "workspace-write"], "SANDBOX_MODE_MISMATCH"),
    ("LATE_SANDBOX_MODE", ["read-only", "workspace-write"], "SANDBOX_MODE_MISMATCH"),
    ("LATE_SANDBOX_UNAVAILABLE", ["read-only"], "SANDBOX_UNAVAILABLE"),
])
def test_all_sandbox_events_after_real_cleanup(tmp_path, trigger, modes, category):
    adapter = make_adapter(tmp_path)
    outcome = adapter.execute(make_request(tmp_path, objective=f"{trigger} MARK_COMPLETED"))
    if category:
        assert isinstance(outcome, ExecutionFailure)
        assert outcome.kind is ExecutionFailureKind.EXECUTION
        assert outcome.category == category
        assert outcome.provider_error is None
        assert adapter.get_execution(outcome.metadata["execution_id"])["status"] == "FAILED"
    else:
        assert isinstance(outcome, ExecutionResult)
        assert outcome.status is ExecutionStatus.COMPLETED
    assert outcome.metadata["sandbox_modes"] == modes
    assert outcome.metadata["sandbox_mode"] == modes[-1]
    assert outcome.metadata["sandbox_consistent"] is all(mode == "read-only" for mode in modes)
    assert outcome.metadata["event_counts"]["sandbox/mode"] == len(modes)
    assert outcome.metadata["stdout_reader_joined"] is True
    assert outcome.metadata["stdout_drained"] is True
    assert outcome.metadata["process_group_cleared"] is True
    assert outcome.metadata["process_tree_verified"] is False
    assert outcome.metadata["process_tree_status"] == "UNVERIFIED"
    print(f"REAL_EVENT trigger={trigger} outcome={category or outcome.status.value} "
          f"modes={modes} reader_joined=True group_cleared=True tree_verified=False")


def test_cleanup_joins_reader_with_pending_late_notification(tmp_path, monkeypatch):
    import time
    from adapters.dsh_execution import _EventCollector

    original = _EventCollector.add
    def slow_late_delivery(self, event):
        if event.get("type") == "sandbox/mode" and event["data"]["mode"] == "workspace-write":
            # Longer than TERM grace: the process exits before callback delivery.
            time.sleep(0.5)
        original(self, event)
    monkeypatch.setattr(_EventCollector, "add", slow_late_delivery)
    outcome = make_adapter(tmp_path).execute(make_request(
        tmp_path, objective="LATE_SANDBOX_MODE MARK_COMPLETED"))
    assert isinstance(outcome, ExecutionFailure)
    assert outcome.category == "SANDBOX_MODE_MISMATCH"
    assert outcome.metadata["sandbox_modes"] == ["read-only", "workspace-write"]
    assert outcome.metadata["stdout_drained"] is True


def test_late_mode_conflict_during_cancel_never_completes(tmp_path):
    from tests.test_dsh_process_tree import wait_until

    adapter = make_adapter(tmp_path)
    results = []
    thread = threading.Thread(target=lambda: results.append(adapter.execute(make_request(
        tmp_path, objective="SLOW_WAIT LATE_SANDBOX_MODE MARK_COMPLETED"))))
    thread.start()
    entry = wait_until(lambda: next((r for r in adapter.active_executions() if r["message_id"]), None))
    try:
        assert adapter.terminate(entry["execution_id"]) is True
        thread.join(5)
        assert not thread.is_alive()
        outcome = results[0]
        assert isinstance(outcome, ExecutionFailure)
        assert outcome.category == "SANDBOX_MODE_MISMATCH"
        assert outcome.metadata["sandbox_modes"] == ["read-only", "workspace-write"]
        assert outcome.metadata["sandbox_consistent"] is False
        assert outcome.metadata["stdout_drained"] is True
        assert adapter.get_execution(entry["execution_id"])["status"] == "FAILED"
    finally:
        adapter.terminate(entry["execution_id"])
        thread.join(5)


def test_cancel_after_provisional_completion_never_completes(tmp_path, monkeypatch):
    from adapters.dsh_execution import _SdkRuntime

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    original = _SdkRuntime.terminate
    def paused_cleanup(self, *, requested=True):
        if not requested:
            cleanup_started.set()
            assert allow_cleanup.wait(5)
        return original(self, requested=requested)
    monkeypatch.setattr(_SdkRuntime, "terminate", paused_cleanup)
    adapter = make_adapter(tmp_path)
    results = []
    thread = threading.Thread(target=lambda: results.append(adapter.execute(make_request(
        tmp_path, objective="MARK_COMPLETED"))))
    thread.start()
    try:
        assert cleanup_started.wait(5)
        entry = adapter.active_executions()[0]
        assert entry["status"] == "RUNNING"
        assert adapter.terminate(entry["execution_id"]) is True
    finally:
        allow_cleanup.set()
        thread.join(5)
    assert not thread.is_alive()
    assert isinstance(results[0], ExecutionFailure)
    assert results[0].category == "TERMINATED_BY_REQUEST"
    assert results[0].metadata["stdout_drained"] is True


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_direct_adapter_explicit_group_only_mode(tmp_path, mode):
    outcome = make_adapter(tmp_path, require_process_tree=False).execute(make_request(
        tmp_path, access_mode=mode, objective="MARK_COMPLETED"))
    assert isinstance(outcome, ExecutionResult)
    assert outcome.status is ExecutionStatus.COMPLETED
    assert outcome.metadata["sandbox_modes"] == [mode]
    assert outcome.metadata["sandbox_consistent"] is True
    assert outcome.metadata["process_group_cleared"] is True
    assert outcome.metadata["process_tree_verified"] is False
    assert outcome.metadata["process_tree_status"] == "UNVERIFIED"
