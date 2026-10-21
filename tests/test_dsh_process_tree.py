"""Real forked process-group tests. The SDK peer is synthetic, not a sandbox."""

import ast
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from adapters.dsh_execution import DSHExecutionAdapter, _SdkRuntime
from core.execution import ExecutionFailure, ExecutionFailureKind, ExecutionResult
from tests.test_dsh_execution_adapter import FAKE_PEER, make_adapter, make_request


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for test-owned process tree")


def start_tree(tmp_path, *, ignore_term=False, objective="SLOW_WAIT", adapter=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tree_dir = tmp_path / "tree"
    adapter = adapter or make_adapter(tmp_path, timeout_seconds=20, env_extra={
        "FAKE_STORE_DIR": str(tmp_path / "store"),
        "FAKE_TREE_DIR": str(tree_dir),
        "FAKE_IGNORE_TERM": "1" if ignore_term else "0",
    })
    results = []
    thread = threading.Thread(target=lambda: results.append(
        adapter.execute(make_request(tmp_path, objective=objective))), daemon=True)
    thread.start()
    wait_until(lambda: all((tree_dir / f"{role}.json").exists()
                           for role in ("leader", "child", "grandchild")))
    tree = {role: json.loads((tree_dir / f"{role}.json").read_text())
            for role in ("leader", "child", "grandchild")}
    entry = wait_until(lambda: next((r for r in adapter.active_executions()
                                    if r["message_id"]), None))
    assert tree["leader"]["pid"] == entry["runtime_pid"] == entry["pgid"]
    assert all(info["pgid"] == entry["pgid"] for info in tree.values())
    assert tree["child"]["ppid"] == tree["leader"]["pid"]
    assert tree["grandchild"]["ppid"] == tree["child"]["pid"]
    return adapter, entry, tree, thread, results


def assert_gone(entry, tree):
    with pytest.raises(ProcessLookupError):
        os.killpg(entry["pgid"], 0)
    for info in tree.values():
        with pytest.raises(ProcessLookupError):
            os.kill(info["pid"], 0)


@pytest.mark.parametrize("ignore_term", [False, True], ids=["term", "kill-escalation"])
def test_terminate_clears_real_leader_child_and_grandchild(tmp_path, ignore_term):
    adapter, entry, tree, thread, results = start_tree(tmp_path, ignore_term=ignore_term)
    try:
        assert adapter.terminate(entry["execution_id"]) is True
        thread.join(10)
        assert not thread.is_alive()
        assert isinstance(results[0], ExecutionFailure)
        assert results[0].category == "TERMINATED_BY_REQUEST"
        record = adapter.get_execution(entry["execution_id"])
        assert record["process_group_cleared"] is True
        assert record["process_tree_verified"] is False
        if ignore_term:
            assert record["termination"] == "SIGKILL"
        assert_gone(entry, tree)
        print(f"REAL_TREE ignore_term={ignore_term} pgid={entry['pgid']} "
              f"pids={[info['pid'] for info in tree.values()]} "
              f"termination={record['termination']} group_probe=ESRCH all_pids_absent=True")
    finally:
        adapter.terminate(entry["execution_id"])
        thread.join(10)


def test_cancel_a_does_not_kill_b(tmp_path):
    a = start_tree(tmp_path / "a", ignore_term=True)
    b = start_tree(tmp_path / "b", ignore_term=True)
    try:
        adapter_a, entry_a, tree_a, thread_a, results_a = a
        adapter_b, entry_b, tree_b, thread_b, _ = b
        assert entry_a["pgid"] != entry_b["pgid"]
        assert adapter_a.terminate(entry_a["execution_id"]) is True
        thread_a.join(10)
        assert results_a[0].category == "TERMINATED_BY_REQUEST"
        assert_gone(entry_a, tree_a)
        assert adapter_b.is_alive(entry_b["execution_id"]) is True
        for info in tree_b.values():
            os.kill(info["pid"], 0)
        print(f"REAL_TREE cancel_A={entry_a['pgid']} B={entry_b['pgid']} all_B_pids_alive=True")
    finally:
        for adapter, entry, _, thread, _ in (a, b):
            adapter.terminate(entry["execution_id"])
            thread.join(10)
    assert_gone(b[1], b[2])


def test_dead_leader_never_signals_saved_group(tmp_path, monkeypatch):
    adapter, entry, tree, thread, results = start_tree(tmp_path, ignore_term=True)
    runtime = adapter._active[entry["execution_id"]][1]
    # Kill/reap only our leader: the real descendants deliberately survive.
    os.kill(runtime.pid, signal.SIGKILL)
    runtime._proc.wait(timeout=3)
    signals = []
    real_killpg = os.killpg

    def observed_killpg(pgid, sig):
        signals.append((pgid, sig))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", observed_killpg)
    try:
        assert adapter.terminate(entry["execution_id"]) is False
        record = adapter.get_execution(entry["execution_id"])
        assert record["termination"] == "ORPHANED"
        assert record["process_group_cleared"] is False
        assert all(sig == 0 for _, sig in signals)
        for role in ("child", "grandchild"):
            os.kill(tree[role]["pid"], 0)
        thread.join(5)
        assert not thread.is_alive()
        assert isinstance(results[0], ExecutionFailure)
        assert results[0].category == "PROCESS_GROUP_ORPHANED"
    finally:
        # Test teardown knows the live descendants, independent of the adapter's
        # stale leader handle. Clean only these test-owned processes.
        for role in ("grandchild", "child"):
            try:
                os.kill(tree[role]["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        thread.join(10)
    assert not thread.is_alive()
    assert isinstance(results[0], ExecutionFailure)
    assert results[0].category == "PROCESS_GROUP_ORPHANED"


def test_finally_cleans_real_descendants_after_completion(tmp_path):
    adapter, entry, tree, thread, results = start_tree(tmp_path, objective="MARK_COMPLETED")
    thread.join(10)
    assert not thread.is_alive()
    assert isinstance(results[0], ExecutionResult)
    assert results[0].metadata["process_group_cleared"] is True
    assert_gone(entry, tree)


def test_unconfirmed_cleanup_cannot_report_success(tmp_path, monkeypatch):
    # Real cleanup still runs; only its reported confirmation is fault-injected.
    original = _SdkRuntime.terminate

    def unconfirmed(self, **kwargs):
        assert original(self, **kwargs)
        self.termination = "KILL_UNCONFIRMED"
        return False

    monkeypatch.setattr(_SdkRuntime, "terminate", unconfirmed)
    result = make_adapter(tmp_path).execute(make_request(tmp_path, objective="MARK_COMPLETED"))
    assert isinstance(result, ExecutionFailure)
    assert result.category == "PROCESS_GROUP_ORPHANED"
    assert result.metadata["termination"] == "KILL_UNCONFIRMED"


def test_production_uses_owned_groups_and_no_name_matching():
    source = (Path(__file__).resolve().parents[1] / "adapters/dsh_execution.py").read_text()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert any(isinstance(call.func, ast.Attribute) and call.func.attr == "killpg"
               and isinstance(call.func.value, ast.Name) and call.func.value.id == "os"
               for call in calls)
    assert "start_new_session=True" in source
    for forbidden in ("pkill", "killall", "pgrep", "ps lstart", "psutil", "process_iter"):
        assert forbidden not in source


@pytest.mark.parametrize("options", [{}, {"require_process_tree": True}], ids=["default", "explicit"])
def test_full_tree_requirement_fails_before_spawn(tmp_path, monkeypatch, options):
    import subprocess

    def must_not_spawn(*args, **kwargs):
        pytest.fail("full-tree execution spawned a process")
    monkeypatch.setattr(subprocess, "Popen", must_not_spawn)
    escape_path = tmp_path / "escaped.json"
    adapter = DSHExecutionAdapter(
        runtime_cmd=[sys.executable, str(FAKE_PEER)], permission_mode="read-only",
        env_extra={"FAKE_ESCAPE_PATH": str(escape_path)}, **options)
    outcome = adapter.execute(make_request(tmp_path, objective="MARK_COMPLETED"))
    assert isinstance(outcome, ExecutionFailure)
    assert outcome.kind is ExecutionFailureKind.EXECUTION
    assert outcome.category == "PROCESS_TREE_UNSUPPORTED"
    assert outcome.metadata["runtime_pid"] is None
    assert outcome.metadata["pgid"] is None
    assert outcome.metadata["process_tree_verified"] is False
    assert adapter.process_tree_supported is False
    assert adapter.active_executions() == []
    assert not escape_path.exists()
    print("REAL_ESCAPE full_tree_required=True outcome=PROCESS_TREE_UNSUPPORTED spawned=False")


@pytest.mark.parametrize("keep_stdout", [False, True], ids=["closed-stdio", "retained-stdout"])
def test_real_setsid_escape_never_claims_full_tree_cleanup(tmp_path, keep_stdout):
    escape_path = tmp_path / "escaped.json"
    adapter = make_adapter(tmp_path, require_process_tree=False, env_extra={
        "FAKE_STORE_DIR": str(tmp_path / "store"),
        "FAKE_ESCAPE_PATH": str(escape_path),
        "FAKE_ESCAPE_STDOUT": "1" if keep_stdout else "0",
    })
    try:
        outcome = adapter.execute(make_request(tmp_path, objective="MARK_COMPLETED"))
        escaped = json.loads(escape_path.read_text())
        assert escaped["pid"] == escaped["pgid"] != outcome.metadata["pgid"]
        os.kill(escaped["pid"], 0)  # The escaped child really survives cleanup.
        with pytest.raises(ProcessLookupError):
            os.killpg(outcome.metadata["pgid"], 0)
        assert outcome.metadata["process_group_cleared"] is True
        assert outcome.metadata["process_tree_verified"] is False
        assert outcome.metadata["process_tree_status"] == "UNVERIFIED"
        assert outcome.metadata["sandbox_modes"] == ["read-only"]
        assert outcome.metadata["sandbox_consistent"] is True
        if keep_stdout:
            assert isinstance(outcome, ExecutionFailure)
            assert outcome.category == "SANDBOX_EVENTS_UNDRAINED"
            assert outcome.metadata["stdout_reader_joined"] is False
            assert outcome.metadata["stdout_drained"] is False
        else:
            assert isinstance(outcome, ExecutionResult)  # Explicit group-only contract.
            assert outcome.metadata["stdout_reader_joined"] is True
            assert outcome.metadata["stdout_drained"] is True
        print(f"REAL_ESCAPE pgid={outcome.metadata['pgid']} escaped_pid={escaped['pid']} "
              f"escaped_alive=True group_cleared=True tree_verified=False "
              f"keep_stdout={keep_stdout} outcome={getattr(outcome, 'category', 'GROUP_ONLY_RESULT')}")
    finally:
        # Independent test ownership: clean the deliberately escaped child by
        # its recorded identity, never ask the adapter to signal a stale pgid.
        if escape_path.exists():
            pid = json.loads(escape_path.read_text())["pid"]
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            def gone():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                return False
            wait_until(gone)
