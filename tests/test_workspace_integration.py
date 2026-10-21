"""Workspace binding end-to-end: controller cwd, fail-closed guard, durability."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

from adapters.git_workspace import GitWorkspaceGuard
from core.capability import CapabilityCandidate
from core.controller import AdvanceStatus, TaskController
from core.errors import classify_provider_error
from core.execution import ExecutionResult, ExecutionStatus
from core.human import HumanGate
from core.observation import Observer
from core.provider_health import ProviderHealthTracker
from core.recovery import RecoveryStatus, Reconciler
from core.router import ThinRouter
from core.task import Artifact, LifecycleStatus
from core.workspace import (
    WorkspaceBinding,
    WorkspacePolicy,
    WorkspaceReason,
    WorkspaceStatus,
    bind_workspace,
    binding_for,
)
from stores import JSONTaskStateStore
from tests.helpers.git_workspace_fixture import build

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = GitWorkspaceGuard()


def binding(fixture, **overrides) -> WorkspaceBinding:
    values = dict(
        workspace_id="ws-e2e",
        root_path=str(fixture.worktree_current),
        repository_root=str(fixture.repo),
        worktree_path=str(fixture.worktree_current),
        branch="feature/current",
        baseline_commit=fixture.baseline,
        tracking_ref="origin/feature/current",
        policy=WorkspacePolicy(require_tracking_ref=True),
    )
    values.update(overrides)
    return WorkspaceBinding(**values)


def running_bound_task(tmp_path: Path, fixture, **overrides):
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = store.create_task(objective="workspace-bound task")
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    bind_workspace(store, task.task_id, binding(fixture, **overrides), expected_revision=task.revision)
    return store, store.get_task(task.task_id)


class CwdCapability:
    def __init__(self) -> None:
        self.requests = []
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        self.requests.append(request)
        cwd = subprocess.check_output(
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            cwd=request.workspace_path,
            text=True,
        ).strip()
        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            artifacts=(Artifact(kind="cwd", producer_capability="cap-a", payload={"cwd": cwd}),),
        )


class ToggleGuard:
    def __init__(self, real) -> None:
        self.real = real
        self.calls = 0

    def verify(self, binding):
        verification = self.real.verify(binding)
        self.calls += 1
        if self.calls == 2:
            actual = dict(verification.actual)
            actual["head"] = "0" * 40
            return replace(verification, actual=actual)
        return verification


def controller(store, capability, guard=GUARD):
    health = ProviderHealthTracker()
    health.record_success("p")
    return TaskController(
        store=store,
        router=ThinRouter(),
        health=health,
        capabilities={"cap-a": capability},
        workspace_guard=guard,
    ), health


def test_binding_is_durable_and_evented(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    store, task = running_bound_task(tmp_path, fixture)

    loaded = JSONTaskStateStore(tmp_path / "tasks").get_task(task.task_id)
    bound = binding_for(loaded)
    assert bound is not None
    assert bound.workspace_id == "ws-e2e"
    assert bound.branch == "feature/current"
    assert bound.worktree_path == str(fixture.worktree_current)
    assert "WORKSPACE_BINDING_CREATED" in [event.type for event in store.list_events(task.task_id)]


def test_execution_uses_bound_worktree_cwd_not_caller_cwd(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    store, task = running_bound_task(tmp_path, fixture)
    capability = CwdCapability()
    ctl, _ = controller(store, capability)

    previous = os.getcwd()
    os.chdir(fixture.worktree_old)  # caller deliberately starts in the wrong worktree
    try:
        outcome = ctl.advance(
            task.task_id,
            candidates=[CapabilityCandidate("cap-a", "execution", provider="p")],
        )
    finally:
        os.chdir(previous)

    assert outcome.status is AdvanceStatus.COMPLETED
    expected = str(fixture.worktree_current.resolve())
    assert capability.requests[0].workspace_path == expected
    reloaded = store.get_task(task.task_id)
    assert reloaded.artifacts[-1].payload["cwd"] == expected
    events = [event.type for event in store.list_events(task.task_id)]
    assert "WORKSPACE_VERIFIED" in events and "EXECUTION_STARTED" in events


def test_wrong_worktree_fails_closed_and_never_executes(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    store, task = running_bound_task(tmp_path, fixture, **{
        "root_path": str(fixture.worktree_old),
        "worktree_path": str(fixture.worktree_current),
    })
    capability = CwdCapability()
    ctl, health = controller(store, capability)
    health_before = {name: rec.to_dict() for name, rec in health.all().items()}
    dirty_before = (fixture.worktree_old / "dirty.txt").read_bytes()

    outcome = ctl.advance(
        task.task_id, candidates=[CapabilityCandidate("cap-a", "execution", provider="p")]
    )

    assert outcome.status is AdvanceStatus.WORKSPACE_BLOCKED
    assert capability.calls == 0
    assert store.get_task(task.task_id).lifecycle_status is LifecycleStatus.BLOCKED
    events = store.list_events(task.task_id)
    types = [event.type for event in events]
    assert "WORKSPACE_VERIFICATION_FAILED" in types
    assert "EXECUTION_STARTED" not in types
    assert {name: rec.to_dict() for name, rec in health.all().items()} == health_before
    assert (fixture.worktree_old / "dirty.txt").read_bytes() == dirty_before
    bound = binding_for(store.get_task(task.task_id))
    assert bound is not None
    assert bound.worktree_path == str(fixture.worktree_current)


def test_binding_without_guard_fails_closed(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    store, task = running_bound_task(tmp_path, fixture)
    capability = CwdCapability()
    ctl, _ = controller(store, capability, guard=None)

    outcome = ctl.advance(
        task.task_id, candidates=[CapabilityCandidate("cap-a", "execution", provider="p")]
    )
    assert outcome.status is AdvanceStatus.WORKSPACE_BLOCKED
    assert capability.calls == 0
    types = [event.type for event in store.list_events(task.task_id)]
    assert "WORKSPACE_VERIFICATION_FAILED" in types


def test_toctou_recheck_blocks_changed_workspace(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    store, task = running_bound_task(tmp_path, fixture)
    capability = CwdCapability()
    ctl, _ = controller(store, capability, guard=ToggleGuard(GUARD))

    outcome = ctl.advance(
        task.task_id, candidates=[CapabilityCandidate("cap-a", "execution", provider="p")]
    )
    assert outcome.status is AdvanceStatus.WORKSPACE_BLOCKED
    assert capability.calls == 0
    failed = [event for event in store.list_events(task.task_id)
              if event.type == "WORKSPACE_VERIFICATION_FAILED"]
    assert failed and failed[-1].data.get("changed_after_verify") is True


def test_handoff_keeps_same_workspace_binding(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    store, task = running_bound_task(tmp_path, fixture)

    def handler_a(request):
        return ExecutionResult(
            status=ExecutionStatus.PROGRESS,
            artifacts=(Artifact(kind="analysis", producer_capability="cap-a", payload=[17]),),
            checkpoint="after-analysis",
            continuation_ref="ref-a",
            metadata={"workspace_path": "/tmp/evil-model-suggestion"},
        )

    def handler_b(request):
        assert request.workspace_path == str(fixture.worktree_current.resolve())
        decision = [a for a in request.artifacts if a.kind == "workspace-binding"]
        assert decision, "binding artifact must travel through handoff"
        return ExecutionResult(status=ExecutionStatus.COMPLETED,
                               artifacts=(Artifact(kind="result", producer_capability="cap-b", payload=89),))

    health = ProviderHealthTracker()
    health.record_success("p-a")
    health.record_success("p-b")
    ctl = TaskController(
        store=store, router=ThinRouter(), health=health,
        capabilities={"a": type("A", (), {"execute": staticmethod(handler_a)})(),
                      "b": type("B", (), {"execute": staticmethod(handler_b)})()},
        workspace_guard=GUARD,
    )
    candidates = [
        CapabilityCandidate("a", "execution", provider="p-a", preference_rank=10),
        CapabilityCandidate("b", "execution", provider="p-b", preference_rank=20,
                            supports_task_state_handoff=True),
    ]
    assert ctl.advance(task.task_id, candidates=candidates).status is AdvanceStatus.PROGRESSED
    health.record_error(classify_provider_error("p-a", message="429", http_status=429))
    outcome = ctl.advance(task.task_id, candidates=candidates)
    assert outcome.status is AdvanceStatus.COMPLETED
    assert binding_for(store.get_task(task.task_id)).workspace_id == "ws-e2e"


def test_human_gate_preserves_binding(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    store, task = running_bound_task(tmp_path, fixture)
    gate = HumanGate(store)
    request = gate.request(task.task_id, requesting_capability="cap-a",
                           reason="publish", question="Allow?")
    gate.resolve(task.task_id, request.request_id, action="APPROVE",
                 expected_revision=store.get_task(task.task_id).revision)
    assert binding_for(store.get_task(task.task_id)).workspace_id == "ws-e2e"


def test_observation_exposes_workspace_status(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    store, task = running_bound_task(tmp_path, fixture)
    observation = Observer(store, workspace_guard=GUARD).observe(task.task_id)

    assert observation.workspace is not None
    assert observation.workspace.status == "VERIFIED"
    assert observation.workspace.branch == "feature/current"
    assert Path(observation.workspace.worktree_path) == fixture.worktree_current.resolve()
    assert observation.workspace.execution_allowed is True
    assert "workspace" in observation.to_dict()


def test_reconciliation_flags_missing_workspace(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    missing = tmp_path / "gone"
    store, task = running_bound_task(
        tmp_path, fixture, root_path=str(missing), worktree_path=str(missing)
    )
    observer = Observer(store, workspace_guard=GUARD)
    result = Reconciler(observer).reconcile(task.task_id)

    assert result.status is RecoveryStatus.WORKSPACE_RECONCILIATION_REQUIRED
    assert any(evidence.code == "WORKSPACE_VERIFICATION_FAILED" for evidence in result.evidence)
    assert result.recommended_action.value == "MANUAL_REVIEW"


def test_restart_preserves_expected_workspace(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    script = f"""
import json, os
from core.task import LifecycleStatus
from core.workspace import WorkspaceBinding, WorkspacePolicy, bind_workspace
from stores import JSONTaskStateStore
store = JSONTaskStateStore(os.environ["WS_STORE"])
task = store.create_task(objective="restart workspace")
task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
binding = WorkspaceBinding(
    workspace_id="ws-restart",
    root_path=os.environ["WS_WORKTREE"],
    repository_root=os.environ["WS_REPO"],
    worktree_path=os.environ["WS_WORKTREE"],
    branch="feature/current",
    baseline_commit=os.environ["WS_BASELINE"],
    tracking_ref="origin/feature/current",
    policy=WorkspacePolicy(require_tracking_ref=True),
)
bind_workspace(store, task.task_id, binding, expected_revision=task.revision)
print(task.task_id)
"""
    env = dict(os.environ)
    env.update(WS_STORE=str(tmp_path / "tasks"), WS_WORKTREE=str(fixture.worktree_current),
               WS_REPO=str(fixture.repo), WS_BASELINE=fixture.baseline)
    completed = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    task_id = completed.stdout.strip().splitlines()[-1]

    store = JSONTaskStateStore(tmp_path / "tasks")
    bound = binding_for(store.get_task(task_id))
    assert bound is not None and bound.workspace_id == "ws-restart"
    assert GUARD.verify(bound).execution_allowed is True


def test_read_only_policy_is_durable(tmp_path: Path) -> None:
    fixture = build(tmp_path)
    store, task = running_bound_task(
        tmp_path, fixture, policy=WorkspacePolicy(read_only=True, require_tracking_ref=True)
    )
    loaded = binding_for(store.get_task(task.task_id))
    assert loaded is not None
    assert loaded.policy.read_only is True
    assert GUARD.verify(loaded).execution_allowed is True


def test_binding_access_mode_reaches_request_and_execution_event(tmp_path):
    from core.execution import AccessMode

    fixture = build(tmp_path)
    for read_only, expected in [(True, AccessMode.READ_ONLY), (False, AccessMode.WORKSPACE_WRITE)]:
        store, task = running_bound_task(
            tmp_path / expected.value, fixture,
            policy=WorkspacePolicy(read_only=read_only, require_tracking_ref=True),
        )
        capability = CwdCapability()
        ctl, _ = controller(store, capability)
        outcome = ctl.advance(task.task_id, candidates=[
            CapabilityCandidate("cap-a", "execution", provider="p")])
        assert outcome.status is AdvanceStatus.COMPLETED
        assert capability.requests[0].access_mode is expected
        event = next(e for e in store.list_events(task.task_id) if e.type == "EXECUTION_STARTED")
        assert event.data["access_mode"] == expected.value
        assert event.data["workspace_id"] == "ws-e2e"
