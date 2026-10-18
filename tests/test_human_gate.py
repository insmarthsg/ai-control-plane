"""Human Gate: insertable decision capability with durable, authoritative decisions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from core.capability import CapabilityCandidate
from core.controller import AdvanceStatus, TaskController
from core.errors import classify_provider_error
from core.execution import ExecutionResult, ExecutionStatus
from core.human import (
    AlreadyPending,
    AlreadyResolved,
    InvalidAction,
    RequestNotFound,
    HumanAction,
    HumanGate,
    HumanGateConflict,
    ModificationRequired,
    StaleRequest,
    WrongTask,
    DECISION_ARTIFACT_KIND,
)
from core.provider_health import HealthStatus, ProviderHealthTracker
from core.router import ThinRouter
from core.task import (
    Artifact,
    CapabilityActivity,
    ExecutionCursor,
    InsertionFrame,
    LifecycleStatus,
)
from core.task_store import RevisionConflict
from stores import JSONTaskStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def running_task(store: JSONTaskStateStore, objective: str = "publish change"):
    task = store.create_task(objective=objective)
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    task = store.set_active_capability(
        task.task_id, CapabilityActivity("cap-a", "execution"), expected_revision=task.revision
    )
    task = store.save_checkpoint(
        task.task_id,
        ExecutionCursor(capability_id="cap-a", checkpoint="before-gate", continuation_ref="ref-a"),
        expected_revision=task.revision,
    )
    return task


def make_request(gate: HumanGate, task_id: str, **overrides):
    kwargs = dict(
        requesting_capability="cap-a",
        reason="publish is an external side effect",
        question="Allow publish?",
        allowed_actions=(HumanAction.APPROVE, HumanAction.REJECT, HumanAction.MODIFY),
        context={"target": "release"},
    )
    kwargs.update(overrides)
    return gate.request(task_id, **kwargs)


def test_human_contract_is_vendor_and_workflow_neutral() -> None:
    source = (REPO_ROOT / "core" / "human.py").read_text(encoding="utf-8").lower()
    for token in ("dsh", "openai", "deepseek", "glm", "zai", "pid", "json-rpc",
                  "planner", "worker", "reviewer", "verifier"):
        assert token not in source, f"core/human.py leaks {token!r}"


def test_request_creates_opaque_id_and_blocks_task(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)

    request = make_request(gate, task.task_id)

    uuid.UUID(request.request_id)
    assert request.request_id != task.task_id
    reloaded = store.get_task(task.task_id)
    assert reloaded.lifecycle_status is LifecycleStatus.BLOCKED
    assert reloaded.insertion_depth == 1
    assert gate.pending(task.task_id) == request


def test_request_is_durable_across_store_reload(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    request = make_request(HumanGate(store), task.task_id)

    reloaded_store = JSONTaskStateStore(tmp_path / "tasks")
    pending = HumanGate(reloaded_store).pending(task.task_id)
    assert pending is not None and pending.request_id == request.request_id
    assert pending.question == "Allow publish?"
    assert pending.allowed_actions == (HumanAction.APPROVE, HumanAction.REJECT, HumanAction.MODIFY)


def test_approve_is_immutable_and_restores_cursor_and_capability(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)
    request = make_request(gate, task.task_id)

    decision = gate.resolve(
        task.task_id, request.request_id, action=HumanAction.APPROVE,
        expected_revision=store.get_task(task.task_id).revision,
    )
    assert decision.action is HumanAction.APPROVE
    reloaded = store.get_task(task.task_id)
    assert reloaded.lifecycle_status is LifecycleStatus.RUNNING
    assert reloaded.cursor.checkpoint == "before-gate"
    assert reloaded.cursor.continuation_ref == "ref-a"
    assert reloaded.active_capability == CapabilityActivity("cap-a", "execution")
    assert reloaded.insertion_depth == 0

    with pytest.raises(AlreadyResolved):
        gate.resolve(task.task_id, request.request_id, action=HumanAction.MODIFY,
                     instruction="changed my mind")
    history = gate.history(task.task_id)
    assert len(history) == 1 and history[0].action is HumanAction.APPROVE


def test_approve_does_not_execute_anything(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)
    request = make_request(gate, task.task_id)
    before = store.get_task(task.task_id).revision

    gate.resolve(task.task_id, request.request_id, action="APPROVE",
                 expected_revision=before)
    # Only one human-gate revision; no controller/capability invocation happened.
    assert store.get_task(task.task_id).revision == before + 1


def test_reject_is_durable_not_task_failure_not_provider_failure(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    tracker = ProviderHealthTracker()
    tracker.record_success("p-a")
    before = tracker.get("p-a").to_dict()
    task = running_task(store)
    request = make_request(gate, task.task_id)

    gate.resolve(task.task_id, request.request_id, action=HumanAction.REJECT,
                 expected_revision=store.get_task(task.task_id).revision)

    reloaded = store.get_task(task.task_id)
    assert reloaded.lifecycle_status is LifecycleStatus.RUNNING
    assert reloaded.lifecycle_status is not LifecycleStatus.FAILED
    history = gate.history(task.task_id)
    assert history[0].action is HumanAction.REJECT
    assert tracker.get("p-a").to_dict() == before


def test_modify_requires_instruction_and_persists_constraint(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)
    request = make_request(gate, task.task_id)

    with pytest.raises(ModificationRequired):
        gate.resolve(task.task_id, request.request_id, action=HumanAction.MODIFY,
                     expected_revision=store.get_task(task.task_id).revision)

    decision = gate.resolve(
        task.task_id, request.request_id, action=HumanAction.MODIFY,
        instruction="Do not push",
        expected_revision=store.get_task(task.task_id).revision,
    )
    assert decision.instruction == "Do not push"
    reloaded = store.get_task(task.task_id)
    decisions = [a for a in reloaded.artifacts if a.kind == DECISION_ARTIFACT_KIND]
    assert decisions[-1].payload["instruction"] == "Do not push"
    assert decisions[-1].payload["authoritative"] is True


def test_duplicate_stale_wrong_task_and_revision_safety(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)
    with pytest.raises(HumanGateConflict):
        gate.request(
            task.task_id, requesting_capability="cap-a", reason="race",
            question="Race?", expected_revision=task.revision - 1,
        )
    request = make_request(gate, task.task_id)
    revision = store.get_task(task.task_id).revision

    with pytest.raises(HumanGateConflict):
        gate.resolve(task.task_id, request.request_id, action="APPROVE",
                     expected_revision=revision - 1)
    assert gate.pending(task.task_id) is not None

    other = running_task(store, objective="other task")
    with pytest.raises(WrongTask):
        gate.resolve(other.task_id, request.request_id, action="APPROVE",
                     expected_revision=store.get_task(other.task_id).revision)

    # A gate that was closed outside the resolver is stale, not approvable.
    store.complete_insertion(task.task_id, expected_revision=revision)
    with pytest.raises(StaleRequest):
        gate.resolve(task.task_id, request.request_id, action="APPROVE",
                     expected_revision=store.get_task(task.task_id).revision)


def test_only_one_active_gate_per_task(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)
    make_request(gate, task.task_id)
    with pytest.raises(AlreadyPending):
        make_request(gate, task.task_id)


def test_nested_insertion_lifo_restores_review_not_execution(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)

    review = CapabilityActivity("review", "judgment")
    task = store.push_insertion(
        task.task_id,
        InsertionFrame(capability=review, reason="risk review"),
        expected_revision=task.revision,
    )
    task = store.save_checkpoint(
        task.task_id,
        ExecutionCursor(capability_id="review", checkpoint="review-half"),
        expected_revision=task.revision,
    )
    request = make_request(gate, task.task_id)
    gate.resolve(task.task_id, request.request_id, action="APPROVE",
                 expected_revision=store.get_task(task.task_id).revision)

    after_gate = store.get_task(task.task_id)
    assert after_gate.insertion_depth == 1
    assert after_gate.active_capability == review
    assert after_gate.cursor.checkpoint == "review-half"

    final = store.complete_insertion(
        task.task_id, expected_revision=after_gate.revision
    )
    assert final.insertion_depth == 0
    assert final.active_capability == CapabilityActivity("cap-a", "execution")
    assert final.cursor.checkpoint == "before-gate"


def test_history_preserves_multiple_decisions(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)

    first = make_request(gate, task.task_id, question="First?")
    gate.resolve(task.task_id, first.request_id, action="APPROVE",
                 expected_revision=store.get_task(task.task_id).revision)
    second = make_request(gate, task.task_id, question="Second?")
    gate.resolve(task.task_id, second.request_id, action="MODIFY",
                 instruction="limited scope",
                 expected_revision=store.get_task(task.task_id).revision)

    history = gate.history(task.task_id)
    assert [d.action for d in history] == [HumanAction.APPROVE, HumanAction.MODIFY]
    assert first.request_id != second.request_id
    assert gate.pending(task.task_id) is None


def test_pending_request_survives_process_restart(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    worker = f"""
import os
from core.human import HumanGate
from stores import JSONTaskStateStore
store = JSONTaskStateStore(os.environ["HUMAN_STORE"])
task = store.get_task(os.environ["HUMAN_TASK"])
gate = HumanGate(store)
if os.environ["HUMAN_MODE"] == "request":
    request = gate.request(task.task_id, requesting_capability="cap-a",
                           reason="external side effect", question="Allow publish?")
    print(request.request_id)
else:
    decision = gate.resolve(task.task_id, os.environ["HUMAN_REQUEST"], action="APPROVE",
                            expected_revision=int(os.environ["HUMAN_REV"]))
    print(decision.action.value)
"""
    env = dict(os.environ)
    env.update(HUMAN_STORE=str(tmp_path / "tasks"), HUMAN_TASK=task.task_id,
               HUMAN_MODE="request")
    created = subprocess.run([sys.executable, "-c", worker], cwd=REPO_ROOT, env=env,
                             capture_output=True, text=True)
    assert created.returncode == 0, created.stderr
    request_id = created.stdout.strip().splitlines()[-1]

    fresh = JSONTaskStateStore(tmp_path / "tasks")
    assert HumanGate(fresh).pending(task.task_id).request_id == request_id
    assert fresh.get_task(task.task_id).lifecycle_status is LifecycleStatus.BLOCKED

    env.update(HUMAN_MODE="resolve", HUMAN_REQUEST=request_id,
               HUMAN_REV=str(fresh.get_task(task.task_id).revision))
    resolved = subprocess.run([sys.executable, "-c", worker], cwd=REPO_ROOT, env=env,
                              capture_output=True, text=True)
    assert resolved.returncode == 0, resolved.stderr
    assert resolved.stdout.strip().splitlines()[-1] == "APPROVE"
    assert fresh.get_task(task.task_id).lifecycle_status is LifecycleStatus.RUNNING


def test_secrets_are_redacted_in_requests_and_decisions(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)
    request = make_request(
        gate, task.task_id,
        reason="Authorization: Bearer abcdefghijklmnop",
        context={"api_key": "sk-test-human-secret", "nested": {"password": "hunter2"}},
    )
    gate.resolve(task.task_id, request.request_id, action="MODIFY",
                 instruction="api_key=sk-test-human-secret",
                 expected_revision=store.get_task(task.task_id).revision)

    raw = (tmp_path / "tasks" / f"{task.task_id}.json").read_text()
    for secret in ("sk-test-human-secret", "abcdefghijklmnop", "hunter2"):
        assert secret not in raw
    pending = HumanGate(store).history(task.task_id)[0]
    assert pending.instruction == "[REDACTED]" or "sk-test" not in (pending.instruction or "")


def test_provider_health_untouched_by_request_and_resolution(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    tracker = ProviderHealthTracker()
    tracker.record_error(classify_provider_error("p-a", message="429", http_status=429))
    before = {name: record.to_dict() for name, record in tracker.all().items()}
    gate = HumanGate(store)
    task = running_task(store)

    request = make_request(gate, task.task_id)
    gate.resolve(task.task_id, request.request_id, action="APPROVE",
                 expected_revision=store.get_task(task.task_id).revision)

    after = {name: record.to_dict() for name, record in tracker.all().items()}
    assert after == before
    assert tracker.get("p-a").status is HealthStatus.COOLDOWN


class FakeHumanAwareCapability:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return self.handler(request)


def _signal():
    return {
        "human_decision": {
            "requesting_capability": "cap-a",
            "reason": "publish is external",
            "question": "Allow publish?",
            "allowed_actions": ["APPROVE", "REJECT", "MODIFY"],
            "context": {"target": "release"},
        }
    }


def _controller(store, gate, capabilities):
    health = ProviderHealthTracker()
    health.record_success("p-a")
    health.record_success("p-b")
    return TaskController(
        store=store,
        router=ThinRouter(),
        health=health,
        capabilities=capabilities,
        human_gate=gate,
    )


def test_controller_approve_e2e(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return ExecutionResult(
                status=ExecutionStatus.PROGRESS,
                artifacts=(Artifact(kind="analysis", producer_capability="cap-a", payload=[17]),),
                checkpoint="after-analysis",
                continuation_ref="ref-a",
                metadata=_signal(),
            )
        assert request.execution_mode.value == "CONTINUE"
        assert any(a.kind == DECISION_ARTIFACT_KIND and a.payload["action"] == "APPROVE"
                   for a in request.artifacts)
        return ExecutionResult(status=ExecutionStatus.COMPLETED,
                               artifacts=(Artifact(kind="result", producer_capability="cap-a", payload=89),))

    capability = FakeHumanAwareCapability(handler)
    controller = _controller(store, gate, {"cap-a": capability})
    task = store.create_task(objective="approve path")

    first = controller.advance(task.task_id, candidates=[
        CapabilityCandidate("cap-a", "execution", provider="p-a", continuation_refs=frozenset({"ref-a"}))
    ])
    assert first.status is AdvanceStatus.HUMAN_REQUIRED
    assert store.get_task(task.task_id).lifecycle_status is LifecycleStatus.BLOCKED
    assert gate.pending(task.task_id) is not None

    gate.resolve(task.task_id, gate.pending(task.task_id).request_id, action="APPROVE",
                 expected_revision=store.get_task(task.task_id).revision)
    second = controller.advance(task.task_id, candidates=[
        CapabilityCandidate("cap-a", "execution", provider="p-a", continuation_refs=frozenset({"ref-a"}))
    ])
    assert second.status is AdvanceStatus.COMPLETED
    assert store.get_task(task.task_id).lifecycle_status is LifecycleStatus.COMPLETED


def test_controller_modify_survives_handoff(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)

    def handler_a(request):
        return ExecutionResult(
            status=ExecutionStatus.PROGRESS,
            artifacts=(Artifact(kind="analysis", producer_capability="cap-a", payload=[17, 29, 43]),),
            checkpoint="numbers-ready",
            continuation_ref="ref-a",
            metadata=_signal(),
        )

    def handler_b(request):
        assert request.execution_mode.value == "HANDOFF"
        decision = [a for a in request.artifacts if a.kind == DECISION_ARTIFACT_KIND]
        assert decision and decision[-1].payload["action"] == "MODIFY"
        assert decision[-1].payload["instruction"] == "Do not push"
        return ExecutionResult(status=ExecutionStatus.COMPLETED,
                               artifacts=(Artifact(kind="result", producer_capability="cap-b", payload=89),))

    controller = _controller(store, gate, {
        "cap-a": FakeHumanAwareCapability(handler_a),
        "cap-b": FakeHumanAwareCapability(handler_b),
    })
    task = store.create_task(objective="modify handoff")
    candidates = [
        CapabilityCandidate("cap-a", "execution", provider="p-a", preference_rank=10),
        CapabilityCandidate("cap-b", "execution", provider="p-b", preference_rank=20,
                            supports_task_state_handoff=True),
    ]

    first = controller.advance(task.task_id, candidates=candidates)
    assert first.status is AdvanceStatus.HUMAN_REQUIRED
    pending = gate.pending(task.task_id)
    gate.resolve(task.task_id, pending.request_id, action="MODIFY",
                 instruction="Do not push", expected_revision=store.get_task(task.task_id).revision)

    controller._health.record_error(classify_provider_error("p-a", message="429", http_status=429))
    second = controller.advance(task.task_id, candidates=candidates)
    assert second.status is AdvanceStatus.COMPLETED
    assert store.get_task(task.task_id).task_id == task.task_id


def test_controller_reject_e2e(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)

    def handler(request):
        if len(handler.calls) == 0:
            handler.calls.append(1)
            return ExecutionResult(status=ExecutionStatus.PROGRESS,
                                   artifacts=(Artifact(kind="plan", producer_capability="cap-a", payload="push"),),
                                   checkpoint="planned", continuation_ref="ref-a", metadata=_signal())
        decision = [a for a in request.artifacts if a.kind == DECISION_ARTIFACT_KIND]
        assert decision and decision[-1].payload["action"] == "REJECT"
        return ExecutionResult(status=ExecutionStatus.COMPLETED,
                               artifacts=(Artifact(kind="result", producer_capability="cap-a", payload="no push"),))
    handler.calls = []

    controller = _controller(store, gate, {"cap-a": FakeHumanAwareCapability(handler)})
    health_before = {n: r.to_dict() for n, r in controller._health.all().items()}
    task = store.create_task(objective="reject path")
    candidates = [CapabilityCandidate("cap-a", "execution", provider="p-a",
                                      continuation_refs=frozenset({"ref-a"}))]

    first = controller.advance(task.task_id, candidates=candidates)
    assert first.status is AdvanceStatus.HUMAN_REQUIRED
    gate.resolve(task.task_id, gate.pending(task.task_id).request_id, action="REJECT",
                 expected_revision=store.get_task(task.task_id).revision)
    second = controller.advance(task.task_id, candidates=candidates)

    assert second.status is AdvanceStatus.COMPLETED
    assert {n: r.to_dict() for n, r in controller._health.all().items()} == health_before
    assert store.get_task(task.task_id).lifecycle_status is LifecycleStatus.COMPLETED


def test_ordinary_task_without_human_gate_still_completes(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    capability = FakeHumanAwareCapability(
        lambda request: ExecutionResult(status=ExecutionStatus.COMPLETED,
                                        artifacts=(Artifact(kind="result", producer_capability="cap-a", payload=1),))
    )
    controller = _controller(store, None, {"cap-a": capability})
    task = store.create_task(objective="no gate")
    outcome = controller.advance(task.task_id, candidates=[
        CapabilityCandidate("cap-a", "execution", provider="p-a")
    ])
    assert outcome.status is AdvanceStatus.COMPLETED
    assert store.get_task(task.task_id).lifecycle_status is LifecycleStatus.COMPLETED


def test_unknown_request_and_disallowed_action_fail_closed(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    gate = HumanGate(store)
    task = running_task(store)

    with pytest.raises(RequestNotFound):
        gate.resolve(task.task_id, str(uuid.uuid4()), action="APPROVE",
                     expected_revision=store.get_task(task.task_id).revision)

    request = make_request(gate, task.task_id, allowed_actions=(HumanAction.APPROVE,))
    with pytest.raises(InvalidAction):
        gate.resolve(task.task_id, request.request_id, action="REJECT",
                     expected_revision=store.get_task(task.task_id).revision)
    assert gate.pending(task.task_id) is not None
