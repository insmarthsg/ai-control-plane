"""Runtime Control: start/status/cancel over a vendor-neutral contract."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import pytest

from core.controller import AdvanceStatus, TaskController
from core.errors import classify_provider_error
from core.execution import (
    ExecutionFailure,
    ExecutionFailureKind,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
)
from core.provider_health import HealthStatus, ProviderHealthTracker
from core.router import ThinRouter
from core.runtime import (
    CancelStatus,
    RuntimeManager,
    RuntimeStatus,
    UnknownCapability,
)
from core.task import LifecycleStatus
from stores import JSONTaskStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeRuntimeCapability:
    def __init__(self, *, behavior: str = "complete", delay: float = 0.0) -> None:
        self.behavior = behavior
        self.delay = delay
        self.terminate_calls: list[str] = []
        self._stop = threading.Event()
        self._alive = True
        self.started = threading.Event()
        self.requests: list[ExecutionRequest] = []
        self.outcomes: list = []

    def execute(self, request: ExecutionRequest):
        self.requests.append(request)
        self.started.set()
        outcome = self._run_behaviour(request)
        self.outcomes.append(outcome)
        return outcome

    def _run_behaviour(self, request: ExecutionRequest):
        if self.behavior == "block":
            self._stop.wait(10)
            self._alive = False
            return ExecutionFailure(
                invocation_id=request.invocation_id,
                kind=ExecutionFailureKind.EXECUTION,
                category="TERMINATED_BY_REQUEST",
                message="cancelled",
            )
        if self.behavior == "lost":
            self._alive = False
            time.sleep(0.6)
            return ExecutionResult(
                invocation_id=request.invocation_id,
                status=ExecutionStatus.COMPLETED,
            )
        if self.behavior == "fail":
            return ExecutionFailure(
                invocation_id=request.invocation_id,
                kind=ExecutionFailureKind.EXECUTION,
                category="BOOM",
                message="local failure",
            )
        time.sleep(self.delay)
        return ExecutionResult(
            invocation_id=request.invocation_id,
            status=ExecutionStatus.COMPLETED,
        )

    def terminate(self, execution_id: str) -> bool:
        self.terminate_calls.append(execution_id)
        self._alive = False
        self._stop.set()
        return True

    def is_alive(self, execution_id: str) -> bool:
        return self._alive


def request(execution_id: str = "", objective: str = "demo") -> ExecutionRequest:
    return ExecutionRequest(
        invocation_id=str(uuid.uuid4()),
        task_id="task-1",
        objective=objective,
        metadata={"execution_id": execution_id} if execution_id else {},
    )


def wait_status(manager: RuntimeManager, execution_id: str, target, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = manager.status(execution_id)
        if snapshot is not None and snapshot.status is target:
            return snapshot
        time.sleep(0.01)
    return manager.status(execution_id)


def test_runtime_contract_is_vendor_neutral() -> None:
    source = (REPO_ROOT / "core" / "runtime.py").read_text(encoding="utf-8").lower()
    for token in ("dsh", "deepseek", "openai", "chatgpt", "glm", "zai", "pid",
                  "json-rpc", "session.v4", "dsh_home"):
        assert token not in source, f"core/runtime.py leaks {token!r}"


def test_start_returns_execution_id_before_completion() -> None:
    capability = FakeRuntimeCapability(behavior="block")
    manager = RuntimeManager({"fake": capability})
    record = manager.start(capability_id="fake", request=request())

    assert uuid.UUID(record.execution_id)
    assert record.status in {RuntimeStatus.STARTING, RuntimeStatus.RUNNING}
    assert not record.is_terminal

    manager.cancel(record.execution_id)


def test_status_progresses_starting_to_running() -> None:
    capability = FakeRuntimeCapability(behavior="block")
    manager = RuntimeManager({"fake": capability})
    record = manager.start(capability_id="fake", request=request())
    running = wait_status(manager, record.execution_id, RuntimeStatus.RUNNING)
    assert running is not None and running.status is RuntimeStatus.RUNNING
    manager.cancel(record.execution_id)


def test_natural_success_reaches_terminal_state() -> None:
    manager = RuntimeManager({"fake": FakeRuntimeCapability(delay=0.05)})
    record = manager.start(capability_id="fake", request=request())
    done = wait_status(manager, record.execution_id, RuntimeStatus.SUCCEEDED)
    assert done is not None and done.status is RuntimeStatus.SUCCEEDED
    assert done.ended_at is not None


def test_execution_failure_reaches_failed_state() -> None:
    manager = RuntimeManager({"fake": FakeRuntimeCapability(behavior="fail")})
    record = manager.start(capability_id="fake", request=request())
    done = wait_status(manager, record.execution_id, RuntimeStatus.FAILED)
    assert done is not None and done.status is RuntimeStatus.FAILED
    assert "BOOM" in done.reason


def test_cancel_really_calls_runtime_termination_and_confirms() -> None:
    capability = FakeRuntimeCapability(behavior="block")
    manager = RuntimeManager({"fake": capability})
    record = manager.start(capability_id="fake", request=request())
    wait_status(manager, record.execution_id, RuntimeStatus.RUNNING)

    result = manager.cancel(record.execution_id)

    assert result.status is CancelStatus.CANCELLED
    assert result.confirmed is True
    assert result.runtime_status is RuntimeStatus.STOPPED
    assert capability.terminate_calls == [record.execution_id]
    snapshot = manager.status(record.execution_id)
    assert snapshot is not None and snapshot.status is RuntimeStatus.STOPPED


def test_cancel_isolation_between_two_runtimes() -> None:
    a = FakeRuntimeCapability(behavior="block")
    b = FakeRuntimeCapability(delay=0.3)
    manager = RuntimeManager({"a": a, "b": b})
    record_a = manager.start(capability_id="a", request=request(objective="A"))
    record_b = manager.start(capability_id="b", request=request(objective="B"))
    wait_status(manager, record_a.execution_id, RuntimeStatus.RUNNING)
    wait_status(manager, record_b.execution_id, RuntimeStatus.RUNNING)

    cancelled = manager.cancel(record_a.execution_id)
    assert cancelled.status is CancelStatus.CANCELLED
    b_snapshot = wait_status(manager, record_b.execution_id, RuntimeStatus.SUCCEEDED)
    assert b_snapshot is not None and b_snapshot.status is RuntimeStatus.SUCCEEDED
    assert a.terminate_calls == [record_a.execution_id]
    assert b.terminate_calls == []


def test_duplicate_cancel_returns_already_terminal() -> None:
    capability = FakeRuntimeCapability(behavior="block")
    manager = RuntimeManager({"fake": capability})
    record = manager.start(capability_id="fake", request=request())
    wait_status(manager, record.execution_id, RuntimeStatus.RUNNING)
    assert manager.cancel(record.execution_id).status is CancelStatus.CANCELLED
    second = manager.cancel(record.execution_id)
    assert second.status is CancelStatus.ALREADY_TERMINAL
    assert second.runtime_status is RuntimeStatus.STOPPED


def test_cancel_unknown_execution_is_structured() -> None:
    manager = RuntimeManager({})
    result = manager.cancel("missing")
    assert result.status is CancelStatus.NOT_FOUND
    assert result.runtime_status is None
    assert manager.status("missing") is None


def test_cancel_already_succeeded_is_already_terminal() -> None:
    manager = RuntimeManager({"fake": FakeRuntimeCapability()})
    record = manager.start(capability_id="fake", request=request())
    wait_status(manager, record.execution_id, RuntimeStatus.SUCCEEDED)
    result = manager.cancel(record.execution_id)
    assert result.status is CancelStatus.ALREADY_TERMINAL
    assert result.runtime_status is RuntimeStatus.SUCCEEDED


def test_cancel_completion_race_never_raises() -> None:
    manager = RuntimeManager({"fake": FakeRuntimeCapability(delay=0.15)})
    record = manager.start(capability_id="fake", request=request())
    time.sleep(0.02)
    result = manager.cancel(record.execution_id)
    assert result.status in {
        CancelStatus.CANCELLED,
        CancelStatus.ALREADY_TERMINAL,
        CancelStatus.REQUESTED,
    }
    final = wait_status(manager, record.execution_id, RuntimeStatus.SUCCEEDED)
    if final is None or final.status not in {RuntimeStatus.SUCCEEDED, RuntimeStatus.STOPPED}:
        final = manager.status(record.execution_id)
    assert final is not None
    assert final.status in {RuntimeStatus.SUCCEEDED, RuntimeStatus.STOPPED}


def test_dead_runtime_is_detected_as_lost() -> None:
    manager = RuntimeManager({"fake": FakeRuntimeCapability(behavior="lost")}, lost_grace_seconds=0.05)
    record = manager.start(capability_id="fake", request=request())
    wait_status(manager, record.execution_id, RuntimeStatus.RUNNING)
    time.sleep(0.1)
    snapshot = manager.status(record.execution_id)
    assert snapshot is not None and snapshot.status is RuntimeStatus.LOST
    assert any(event.type == "RUNTIME_LOST" for event in snapshot.events)
    # A later natural return must not resurrect a LOST terminal record.
    time.sleep(0.8)
    assert manager.status(record.execution_id).status is RuntimeStatus.LOST


def test_recent_terminal_record_remains_queryable() -> None:
    manager = RuntimeManager({"fake": FakeRuntimeCapability()})
    record = manager.start(capability_id="fake", request=request())
    wait_status(manager, record.execution_id, RuntimeStatus.SUCCEEDED)
    snapshot = manager.status(record.execution_id)
    assert snapshot is not None
    assert snapshot.to_dict()["status"] == "SUCCEEDED"


def test_runtime_events_trace_start_and_cancel() -> None:
    capability = FakeRuntimeCapability(behavior="block")
    manager = RuntimeManager({"fake": capability})
    record = manager.start(capability_id="fake", request=request())
    wait_status(manager, record.execution_id, RuntimeStatus.RUNNING)
    manager.cancel(record.execution_id)
    types = [event.type for event in manager.events(record.execution_id)]
    assert types[0] == "RUNTIME_STARTED"
    assert "RUNTIME_CANCEL_REQUESTED" in types
    assert "RUNTIME_CANCELLED" in types


def test_unknown_capability_is_rejected() -> None:
    manager = RuntimeManager({})
    with pytest.raises(UnknownCapability):
        manager.start(capability_id="nope", request=request())


def test_runtime_records_do_not_expose_prompt_or_runtime_handles() -> None:
    manager = RuntimeManager({"fake": FakeRuntimeCapability()})
    record = manager.start(capability_id="fake", request=request(objective="secret sk-test-abc"))
    wait_status(manager, record.execution_id, RuntimeStatus.SUCCEEDED)
    payload = json.dumps(manager.status(record.execution_id).to_dict())
    for forbidden in ("prompt", "pid", "session_id", "sk-test-abc", "SECRET"):
        assert forbidden not in payload


def test_task_lifecycle_independent_and_health_unchanged_on_cancel(tmp_path: Path) -> None:
    capability = FakeRuntimeCapability(behavior="block")
    store = JSONTaskStateStore(tmp_path / "tasks")
    health = ProviderHealthTracker()
    health.record_success("p")
    before = health.get("p").to_dict()
    controller = TaskController(
        store=store,
        router=ThinRouter(),
        health=health,
        capabilities={"fake": capability},
    )
    task = store.create_task(objective="cancel me")

    outcome: dict = {}

    def advance() -> None:
        outcome["result"] = controller.advance(
            task.task_id,
            candidates=[__import__("core.capability", fromlist=["CapabilityCandidate"]).CapabilityCandidate(
                "fake", "execution", provider="p"
            )],
        )

    thread = threading.Thread(target=advance, daemon=True)
    thread.start()
    assert capability.started.wait(5)
    time.sleep(0.05)
    capability.terminate("external-cancel")
    thread.join(5)

    result = outcome["result"]
    assert result.status is AdvanceStatus.FAILED
    reloaded = store.get_task(task.task_id)
    assert reloaded.lifecycle_status is LifecycleStatus.BLOCKED
    assert reloaded.lifecycle_status is not LifecycleStatus.CANCELLED
    assert reloaded.lifecycle_status is not LifecycleStatus.COMPLETED
    assert health.get("p").to_dict() == before


def test_user_cancel_failure_is_not_provider_error() -> None:
    capability = FakeRuntimeCapability(behavior="block")
    manager = RuntimeManager({"fake": capability})
    record = manager.start(capability_id="fake", request=request())
    wait_status(manager, record.execution_id, RuntimeStatus.RUNNING)
    manager.cancel(record.execution_id)

    outcome = capability.outcomes[0]
    assert isinstance(outcome, ExecutionFailure)
    assert outcome.kind is ExecutionFailureKind.EXECUTION
    assert outcome.category == "TERMINATED_BY_REQUEST"
    assert outcome.provider_error is None
    snapshot = manager.status(record.execution_id)
    assert snapshot.status is RuntimeStatus.STOPPED
    assert snapshot.reason == "cancelled"


def test_provider_health_untouched_by_runtime_cancel() -> None:
    tracker = ProviderHealthTracker()
    tracker.record_error(classify_provider_error("p", message="429", http_status=429))
    before = {name: rec.to_dict() for name, rec in tracker.all().items()}
    capability = FakeRuntimeCapability(behavior="block")
    manager = RuntimeManager({"fake": capability})
    record = manager.start(capability_id="fake", request=request())
    wait_status(manager, record.execution_id, RuntimeStatus.RUNNING)
    manager.cancel(record.execution_id)
    after = {name: rec.to_dict() for name, rec in tracker.all().items()}
    assert after == before
    assert tracker.get("p").status is HealthStatus.COOLDOWN
