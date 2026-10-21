"""Local Control Console: API, workspace truth, real core wiring, UI surface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from adapters.git_workspace import GitWorkspaceGuard
from console.app import CapabilitySpec, ControlConsole, HumanGateDemoCapability
from console.server import make_server
from core.execution import ExecutionFailure, ExecutionFailureKind, ExecutionResult, ExecutionStatus
from core.task import Artifact
from tests.helpers.git_workspace_fixture import build

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeWorkspaceCapability:
    def __init__(self, *, delay: float = 0.0, blocking: bool = False) -> None:
        self.delay = delay
        self.blocking = blocking
        self.stop = threading.Event()
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        if self.blocking:
            self.stop.wait(15)
            return ExecutionFailure(
                invocation_id=request.invocation_id,
                kind=ExecutionFailureKind.EXECUTION,
                category="TERMINATED_BY_REQUEST",
                message="cancelled",
            )
        if self.delay:
            time.sleep(self.delay)
        probe = subprocess.check_output(
            [sys.executable, "-c",
             "import json,os,subprocess;"
             "print(json.dumps({'cwd':os.getcwd(),"
             "'branch':subprocess.check_output(['git','rev-parse','--abbrev-ref','HEAD'],text=True).strip(),"
             "'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()}))"],
            cwd=request.workspace_path, text=True,
        )
        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            artifacts=(Artifact(kind="assistant-output", producer_capability="cap",
                                payload={"text": probe.strip()}),),
        )

    def terminate(self, execution_id: str) -> bool:
        self.stop.set()
        return True


def stack(tmp_path: Path, capability=None, *, with_demo: bool = True):
    fixture = build(tmp_path)
    guard = GitWorkspaceGuard()
    specs = [
        CapabilitySpec("fake", "Fake · test", "p", "m", capability or FakeWorkspaceCapability(),
                       worker_kind="NONE", sandbox="NONE"),
    ]
    if with_demo:
        specs.append(CapabilitySpec("demo-human", "Demo · Human Gate", "local", "demo",
                                    HumanGateDemoCapability(), worker_kind="NONE", sandbox="NONE"))
    app = ControlConsole(data_dir=tmp_path / "data", guard=guard, specs=specs)
    server = make_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return fixture, app, server


def call(server, method: str, path: str, body=None):
    port = server.server_address[1]
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def verify_workspace(server, fixture, mode="read_only"):
    status, payload = call(server, "POST", "/api/workspaces/verify",
                           {"path": str(fixture.worktree_current), "mode": mode})
    assert status == 200, payload
    return payload


def create_task(server, fixture, *, objective="inspect project", capability_id="fake",
                mode="read_only", binding=None):
    if binding is None:
        binding = verify_workspace(server, fixture, mode)["binding"]
    return call(server, "POST", "/api/tasks", {
        "objective": objective, "binding": binding, "mode": mode,
        "capability_id": capability_id,
    })


def wait_for(server, task_id, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        status, payload = call(server, "GET", f"/api/tasks/{task_id}")
        last = payload
        if status == 200 and predicate(payload):
            return payload
        time.sleep(0.15)
    raise AssertionError(f"condition not met; last={last}")


def test_server_starts_on_loopback_and_serves_page(tmp_path: Path) -> None:
    fixture, app, server = stack(tmp_path)
    try:
        assert server.server_address[0] == "127.0.0.1"
        status, payload = call(server, "GET", "/api/capabilities")
        assert status == 200
        assert {item["capability_id"] for item in payload["capabilities"]} == {"fake", "demo-human"}
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            html = response.read().decode()
        assert "AI Control Plane" in html and "验证工作区" in html
    finally:
        server.shutdown()


def test_workspace_verify_reports_truth_and_blocks_bad_paths(tmp_path: Path) -> None:
    fixture, app, server = stack(tmp_path)
    try:
        payload = verify_workspace(server, fixture)
        verification = payload["verification"]
        assert verification["status"] == "VERIFIED"
        assert verification["actual"]["branch"] == "feature/current"
        assert verification["actual"]["dirty"] is False
        assert payload["binding"]["worktree_path"] == str(fixture.worktree_current.resolve())

        dirty = fixture.worktree_current / "dirty-ui.txt"
        dirty.write_text("x")
        status, blocked = call(server, "POST", "/api/workspaces/verify",
                               {"path": str(fixture.worktree_current)})
        assert status == 200
        assert blocked["verification"]["status"] == "BLOCKED"
        assert blocked["verification"]["reason"] == "WORKSPACE_DIRTY"

        status, missing = call(server, "POST", "/api/workspaces/verify",
                               {"path": str(tmp_path / "missing")})
        assert status == 200
        assert missing["verification"]["reason"] == "WORKSPACE_NOT_FOUND"
    finally:
        server.shutdown()


def test_create_task_returns_immediately_and_uses_bound_workspace(tmp_path: Path) -> None:
    fixture, app, server = stack(tmp_path, FakeWorkspaceCapability(delay=0.6))
    try:
        previous = os.getcwd()
        os.chdir(fixture.worktree_old)  # console server process starts in the wrong worktree
        try:
            started = time.monotonic()
            status, created = create_task(server, fixture)
            elapsed = time.monotonic() - started
        finally:
            os.chdir(previous)
        assert status == 200, created
        assert elapsed < 1.2
        task_id = created["task_id"]
        uuid.UUID(task_id)

        state = wait_for(server, task_id, lambda s: s["task_status"] == "COMPLETED")
        assert state["mode"] == "read_only"
        assert state["observation"]["workspace"]["status"] == "VERIFIED"
        evidence = json.loads(state["result"])
        assert evidence["cwd"] == str(fixture.worktree_current.resolve())
        assert evidence["branch"] == "feature/current"
        assert evidence["head"] == fixture.baseline
    finally:
        server.shutdown()


def test_observation_timeline_and_result(tmp_path: Path) -> None:
    fixture, app, server = stack(tmp_path)
    try:
        status, created = create_task(server, fixture)
        task_id = created["task_id"]
        state = wait_for(server, task_id, lambda s: s["task_status"] == "COMPLETED")
        assert state["result"]
        assert state["observation"]["task_revision"] >= 1
        types = [entry["type"] for entry in state["timeline"]]
        for required in ("WORKSPACE_BINDING_CREATED", "TASK_CREATED", "WORKSPACE_VERIFIED",
                         "EXECUTION_STARTED", "EXECUTION_RESULT", "TASK_COMPLETED"):
            assert required in types
        status, timeline = call(server, "GET", f"/api/tasks/{task_id}/timeline")
        assert status == 200 and len(timeline["timeline"]) == len(state["timeline"])
        assert state["reconciliation"]["status"] == "TERMINAL"
        assert state["provider"] == "p" and state["model"] == "m"
        assert state["workspace_binding"]["branch"] == "feature/current"
        assert "referenced_files" not in json.dumps(state)
    finally:
        server.shutdown()


def test_read_only_default_and_explicit_mutating(tmp_path: Path) -> None:
    fixture, app, server = stack(tmp_path)
    try:
        status, created = create_task(server, fixture)
        state = wait_for(server, created["task_id"], lambda s: s["task_status"] == "COMPLETED")
        assert state["mode"] == "read_only"
        assert state["workspace_binding"]["policy"]["read_only"] is True

        status, created = create_task(server, fixture, mode="mutating")
        assert status == 200
        state = wait_for(server, created["task_id"], lambda s: s["task_status"] == "COMPLETED")
        assert state["mode"] == "mutating"
        assert state["workspace_binding"]["policy"]["read_only"] is False
    finally:
        server.shutdown()


def test_cancel_calls_real_runtime_terminate(tmp_path: Path) -> None:
    capability = FakeWorkspaceCapability(blocking=True)
    fixture, app, server = stack(tmp_path, capability)
    try:
        status, created = create_task(server, fixture)
        task_id = created["task_id"]
        running = wait_for(server, task_id, lambda s: s["can_cancel"] is True)
        assert any(item["status"] == "RUNNING" for item in running["observation"]["runtimes"]), running["observation"]["runtimes"]

        status, payload = call(server, "POST", f"/api/tasks/{task_id}/cancel", {})
        assert status == 200 and payload["cancelled"] is True
        stopped = wait_for(server, task_id, lambda s: s["task_status"] == "BLOCKED")
        assert stopped["task_status"] != "COMPLETED"
        assert stopped["result"] is None
        assert any(item["status"] == "STOPPED" for item in stopped["observation"]["runtimes"])
    finally:
        capability.stop.set()
        server.shutdown()


def test_human_gate_approve_reject_modify_through_api(tmp_path: Path) -> None:
    fixture, app, server = stack(tmp_path)
    try:
        # APPROVE
        status, created = create_task(server, fixture, capability_id="demo-human")
        task_id = created["task_id"]
        pending = wait_for(server, task_id, lambda s: s["pending_human"] is not None)
        assert pending["pending_human"]["allowed_actions"] == ["APPROVE", "REJECT", "MODIFY"]
        status, response = call(server, "POST", f"/api/tasks/{task_id}/human-decision",
                                {"action": "APPROVE"})
        assert status == 200
        done = wait_for(server, task_id, lambda s: s["task_status"] == "COMPLETED")
        assert "已获批准" in done["result"]
        assert "HUMAN_DECISION_RESOLVED" in [e["type"] for e in done["timeline"]]

        # MODIFY is durable and drives the next execution
        status, created = create_task(server, fixture, capability_id="demo-human")
        task_id = created["task_id"]
        wait_for(server, task_id, lambda s: s["pending_human"] is not None)
        status, response = call(server, "POST", f"/api/tasks/{task_id}/human-decision",
                                {"action": "MODIFY", "instruction": "保持只读，不要修改项目"})
        assert status == 200
        done = wait_for(server, task_id, lambda s: s["task_status"] == "COMPLETED")
        assert "保持只读" in done["result"]
        decisions = [a for a in app._store.get_task(task_id).artifacts if a.kind == "human-decision"]
        assert decisions[-1].payload["instruction"] == "保持只读，不要修改项目"

        # REJECT is not Task FAILED
        status, created = create_task(server, fixture, capability_id="demo-human")
        task_id = created["task_id"]
        wait_for(server, task_id, lambda s: s["pending_human"] is not None)
        call(server, "POST", f"/api/tasks/{task_id}/human-decision", {"action": "REJECT"})
        done = wait_for(server, task_id, lambda s: s["task_status"] == "COMPLETED")
        assert "拒绝" in done["result"]
        assert done["task_status"] != "FAILED"
    finally:
        server.shutdown()


def test_reload_and_server_restart_preserve_durable_truth(tmp_path: Path) -> None:
    fixture, app, server = stack(tmp_path)
    try:
        status, created = create_task(server, fixture)
        task_id = created["task_id"]
        wait_for(server, task_id, lambda s: s["task_status"] == "COMPLETED")
    finally:
        server.shutdown()

    # New ControlConsole instance = server restart, same durable store.
    app2 = ControlConsole(data_dir=tmp_path / "data", guard=GitWorkspaceGuard(),
                          specs=[CapabilitySpec("fake", "Fake", "p", "m",
                                                FakeWorkspaceCapability(), worker_kind="NONE", sandbox="NONE")])
    server2 = make_server(app2, port=0)
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    try:
        status, state = call(server2, "GET", f"/api/tasks/{task_id}")
        assert status == 200
        assert state["task_status"] == "COMPLETED"
        assert state["result"]
        assert state["workspace_binding"]["branch"] == "feature/current"
        assert state["observation"]["workspace"]["status"] == "VERIFIED"
        assert state["observation"]["runtimes"] == []
    finally:
        server2.shutdown()


def test_workspace_drift_blocks_task_creation(tmp_path: Path) -> None:
    fixture, app, server = stack(tmp_path)
    try:
        binding = verify_workspace(server, fixture)["binding"]
        subprocess.run(["git", "-C", str(fixture.worktree_current), "checkout", "-b", "feature/drift"],
                       capture_output=True, check=True)
        status, payload = create_task(server, fixture, binding=binding)
        assert status == 409
        assert payload["error"] == "WORKSPACE_BLOCKED"
        assert payload["verification"]["reason"] in {"BRANCH_MISMATCH", "HEAD_MISMATCH"}
    finally:
        server.shutdown()


def test_remote_credentials_are_not_exposed(tmp_path: Path) -> None:
    fixture, app, server = stack(tmp_path)
    try:
        subprocess.run(["git", "-C", str(fixture.repo), "remote", "set-url", "origin",
                        "https://user:sekret@example.invalid/repo.git"], capture_output=True, check=True)
        payload = verify_workspace(server, fixture)
        rendered = json.dumps(payload)
        assert "sekret" not in rendered
        assert "[REDACTED]@" in rendered
    finally:
        server.shutdown()


@pytest.mark.parametrize("worker_kind,sandbox,process_tree", [
    ("CODEX_EXTERNAL", "UNSUPPORTED", False),
    ("UNSUPPORTED", "UNSUPPORTED", False),
    ("DSH_IN_PROCESS", "UNSUPPORTED", True),
    ("DSH_IN_PROCESS", "DSH", False),
])
def test_unsupported_child_type_creates_no_task_or_runtime(tmp_path, worker_kind, sandbox, process_tree):
    capability = FakeWorkspaceCapability()
    spec = CapabilitySpec("unsupported", "unsupported", "p", "m", capability,
                          worker_kind=worker_kind, sandbox=sandbox, process_tree=process_tree)
    app = ControlConsole(data_dir=tmp_path / "data", guard=GitWorkspaceGuard(), specs=[spec])
    with pytest.raises(ValueError, match="^UNSUPPORTED_CHILD_TYPE$"):
        app.create_task(objective="do work", binding_payload={}, mode="read_only",
                        capability_id="unsupported")
    assert app._store.list_tasks() == []
    assert app._gateway._entries == {}
    assert app._running == set()
    assert capability.calls == 0


@pytest.mark.parametrize("capability_id,mode", [
    ("dsh-readonly", "read_only"),
    ("dsh-mutating", "mutating"),
])
def test_default_dsh_specs_rejected_without_task_or_runtime(tmp_path, monkeypatch, capability_id, mode):
    from console.app import build_default_console

    fixture = build(tmp_path)
    app = build_default_console(data_dir=tmp_path / "data")
    spec = app._specs[capability_id]
    assert (spec.worker_kind, spec.sandbox, spec.process_tree) == ("DSH_IN_PROCESS", "DSH", False)
    assert "DSH_PERMISSION_MODE" not in spec.capability._env_extra
    assert spec.capability._permission_mode is None
    assert spec.capability._require_process_tree is True
    def must_not_execute(request):
        pytest.fail("unsupported Console DSH spec reached execution")
    monkeypatch.setattr(spec.capability, "execute", must_not_execute)
    binding, verification = app.verify_workspace(str(fixture.worktree_current), mode=mode)
    assert verification.execution_allowed
    with pytest.raises(ValueError, match="^UNSUPPORTED_CHILD_TYPE$"):
        app.create_task(objective="MARK_COMPLETED", binding_payload=binding.to_payload(),
                        mode=mode, capability_id=capability_id)
    assert app._store.list_tasks() == []
    assert app._gateway._entries == {}
    assert app._running == set()
    assert spec.capability.active_executions() == []
    demo = app._specs["demo-human"]
    assert (demo.worker_kind, demo.sandbox, demo.process_tree) == ("NONE", "NONE", False)


@pytest.mark.parametrize("termination", [None, False, "raise"])
def test_cancel_returns_false_when_termination_unavailable_or_unconfirmed(tmp_path, termination):
    from core.execution import ExecutionRequest
    from tests.test_dsh_process_tree import wait_until

    capability = FakeWorkspaceCapability(blocking=True)
    if termination is None:
        capability.terminate = None
    elif termination is False:
        capability.terminate = lambda execution_id: False
    else:
        def fail(execution_id):
            raise RuntimeError("termination failed")
        capability.terminate = fail
    spec = CapabilitySpec("fake", "Fake", "p", "m", capability, worker_kind="NONE", sandbox="NONE")
    app = ControlConsole(data_dir=tmp_path / "data", guard=GitWorkspaceGuard(), specs=[spec])
    assert app.cancel_task("no-runtime") is False
    request = ExecutionRequest(invocation_id="i", task_id="t", objective="wait")
    thread = threading.Thread(target=lambda: app._gateway.run(spec, request))
    thread.start()
    try:
        wait_until(lambda: capability.calls == 1)
        assert app.cancel_task("t") is False
    finally:
        capability.stop.set()
        thread.join(5)
