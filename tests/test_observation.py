"""Observability: read-only, truthful Task projection."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from core.errors import classify_provider_error
from core.execution import ExecutionRequest, ExecutionResult, ExecutionStatus
from core.human import HumanGate
from core.observation import Observer
from core.provider_health import HealthStatus, ProviderHealthTracker
from core.runtime import RuntimeManager, RuntimeStatus
from core.task import Artifact, CapabilityActivity, ExecutionCursor, LifecycleStatus
from stores import JSONTaskStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def running_task(store: JSONTaskStateStore, objective: str = "observe me"):
    task = store.create_task(objective=objective)
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    task = store.set_active_capability(
        task.task_id, CapabilityActivity("cap-a", "execution"), expected_revision=task.revision
    )
    task = store.save_checkpoint(
        task.task_id,
        ExecutionCursor(capability_id="cap-a", checkpoint="cp-1", continuation_ref="opaque-ref"),
        expected_revision=task.revision,
    )
    return task


class FailingRuntime:
    def records_for_task(self, task_id):
        raise RuntimeError("runtime query failed")


class FailingHealth:
    def all(self):
        raise RuntimeError("health query failed")


class BlockingCapability:
    def __init__(self):
        self.stop = threading.Event()

    def execute(self, request):
        self.stop.wait(5)
        return ExecutionResult(invocation_id=request.invocation_id, status=ExecutionStatus.COMPLETED)

    def terminate(self, execution_id):
        self.stop.set()
        return True

    def is_alive(self, execution_id):
        return not self.stop.is_set()


def test_observation_contract_is_vendor_neutral_and_read_only() -> None:
    for name in ("observation.py", "recovery.py"):
        source = (REPO_ROOT / "core" / name).read_text(encoding="utf-8").lower()
        for token in ("dsh", "openai", "deepseek", "glm", "zai", "pid", "json-rpc",
                      "session.v4", "def execute(", "def cancel(", "def route("):
            assert token not in source, f"{name} leaks {token!r}"


def test_durable_task_status_and_revision_are_visible(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    observation = Observer(store).observe(task.task_id)

    assert observation.task_id == task.task_id
    assert observation.task_status == "RUNNING"
    assert observation.task_revision == store.get_task(task.task_id).revision
    assert observation.cursor_checkpoint == "cp-1"
    assert observation.continuation_present is True
    assert observation.cursor_capability == "cap-a"


def test_runtime_status_is_separate_from_task_status(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    observation = Observer(store).observe(task.task_id)

    assert observation.task_status == "RUNNING"
    assert observation.runtimes == ()
    assert observation.has_live_runtime is False
    assert observation.latest_runtime is None


def test_active_runtime_projection(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = store.create_task(objective="active")
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    capability = BlockingCapability()
    manager = RuntimeManager({"cap": capability})
    record = manager.start(
        capability_id="cap",
        request=ExecutionRequest(invocation_id="inv-live", task_id=task.task_id, objective="wait"),
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if manager.status(record.execution_id).status is RuntimeStatus.RUNNING:
            break
        time.sleep(0.01)

    observation = Observer(store, runtime=manager).observe(task.task_id)
    assert observation.has_live_runtime is True
    assert observation.latest_runtime is not None
    assert observation.latest_runtime.execution_id == record.execution_id
    assert observation.latest_runtime.status == "RUNNING"

    manager.cancel(record.execution_id)


def test_pending_human_projection(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    gate = HumanGate(store)
    request = gate.request(
        task.task_id, requesting_capability="cap-a", reason="external effect",
        question="Allow publish?", context={"api_key": "sk-test-ignored"},
    )
    observation = Observer(store, human=gate).observe(task.task_id)

    assert observation.waiting_for_human is True
    assert observation.pending_human is not None
    assert observation.pending_human.request_id == request.request_id
    assert observation.pending_human.allowed_actions == ("APPROVE", "REJECT", "MODIFY")
    assert "context" not in observation.pending_human.to_dict()


def test_artifact_summary_does_not_expand_payloads(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    task = store.add_artifact(
        task.task_id,
        Artifact(kind="analysis", producer_capability="cap-a",
                 payload={"api_key": "sk-test-artifact-secret", "data": [1, 2]}),
        expected_revision=task.revision,
    )
    observation = Observer(store).observe(task.task_id)
    assert len(observation.artifacts) == 1
    summary = observation.artifacts[0]
    assert summary.kind == "analysis"
    assert summary.producer_capability == "cap-a"
    assert "payload" not in summary.to_dict()
    assert "sk-test-artifact-secret" not in json.dumps(observation.to_dict())


def test_timeline_is_ordered_and_filters_raw_data(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    store.record_event(
        task.task_id, "EXECUTION_STARTED", expected_revision=store.get_task(task.task_id).revision,
        data={"invocation_id": "inv-1", "capability_id": "cap-a", "prompt": "SECRET PROMPT"},
    )
    timeline = Observer(store).timeline(task.task_id)

    assert [entry.seq for entry in timeline] == sorted(entry.seq for entry in timeline)
    assert timeline[0].type == "TASK_CREATED"
    assert any(entry.type == "EXECUTION_STARTED" for entry in timeline)
    rendered = json.dumps([entry.to_dict() for entry in timeline])
    assert "SECRET PROMPT" not in rendered
    assert "inv-1" in rendered


def test_partial_failure_tolerance_for_health_and_runtime(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    observation = Observer(store, runtime=FailingRuntime(), health=FailingHealth()).observe(task.task_id)

    assert observation.task_status == "RUNNING"
    codes = {issue.code for issue in observation.issues}
    assert codes == {"RUNTIME_QUERY_FAILED", "HEALTH_QUERY_FAILED"}
    assert observation.runtimes == () and observation.provider_health == ()


def test_provider_health_summary_is_read_only(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    health = ProviderHealthTracker()
    health.record_success("p-b")
    health.record_error(classify_provider_error("p-a", message="429", http_status=429))
    before = {name: record.to_dict() for name, record in health.all().items()}
    observation = Observer(store, health=health).observe(task.task_id)
    assert [item.provider for item in observation.provider_health] == ["p-a", "p-b"]
    assert observation.provider_health[0].status == "COOLDOWN"
    assert {name: record.to_dict() for name, record in health.all().items()} == before


def test_observation_is_read_only(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    before = store.get_task(task.task_id)
    events = store.list_events(task.task_id)

    Observer(store).observe(task.task_id)
    Observer(store).timeline(task.task_id)

    assert store.get_task(task.task_id) == before
    assert store.list_events(task.task_id) == events
