"""Reconciliation: evidence-based, read-only recovery classification."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from core.human import HumanGate
from core.observation import Observer
from core.recovery import (
    Evidence,
    RecoveryProfile,
    RecoveryStatus,
    RecommendedAction,
    Reconciler,
)
from core.runtime import RuntimeRecord, RuntimeStatus
from core.task import (
    Artifact,
    CapabilityActivity,
    ExecutionCursor,
    LifecycleStatus,
)
from stores import JSONTaskStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def running_task(store: JSONTaskStateStore, *, checkpoint="cp-1", ref="opaque-ref"):
    task = store.create_task(objective="reconcile me")
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    task = store.set_active_capability(
        task.task_id, CapabilityActivity("cap-a", "execution"), expected_revision=task.revision
    )
    if checkpoint is not None or ref is not None:
        task = store.save_checkpoint(
            task.task_id,
            ExecutionCursor(capability_id="cap-a", checkpoint=checkpoint or "", continuation_ref=ref),
            expected_revision=task.revision,
        )
    return task


def runtime_record(task_id: str, status: RuntimeStatus, **overrides) -> RuntimeRecord:
    base = dict(
        execution_id=str(uuid.uuid4()),
        task_id=task_id,
        invocation_id="inv-runtime",
        capability_id="cap-a",
        status=status,
        started_at="2026-10-18T00:00:00.000Z",
        updated_at="2026-10-18T00:00:01.000Z",
        ended_at=None if status in {RuntimeStatus.STARTING, RuntimeStatus.RUNNING, RuntimeStatus.STOPPING} else "2026-10-18T00:00:02.000Z",
        reason="",
    )
    base.update(overrides)
    return RuntimeRecord(**base)


class StaticRuntime:
    def __init__(self, records):
        self._records = records

    def records_for_task(self, task_id):
        return [record for record in self._records if record.task_id == task_id]


def reconcile(store, *, records=(), profiles=None, uncertain=False):
    observer = Observer(store, runtime=StaticRuntime(list(records)))
    return Reconciler(observer).reconcile(
        store.list_tasks()[0], profiles=profiles or {}, uncertain_side_effect=uncertain
    )


def codes(result):
    return [evidence.code for evidence in result.evidence]


def test_reconciliation_contract_is_read_only_and_vendor_neutral() -> None:
    source = (REPO_ROOT / "core" / "recovery.py").read_text(encoding="utf-8").lower()
    for token in ("dsh", "openai", "deepseek", "glm", "zai", "pid", "json-rpc",
                  "session.v4", "def execute(", "def route(", "def cancel(", "def approve(",
                  "update_health", "while "):
        assert token not in source, f"core/recovery.py leaks {token!r}"


def test_active_runtime_is_active(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    result = reconcile(store, records=[runtime_record(task.task_id, RuntimeStatus.RUNNING)])
    assert result.status is RecoveryStatus.ACTIVE
    assert result.recommended_action is RecommendedAction.NONE
    assert "ACTIVE_RUNTIME" in codes(result)


def test_running_with_resumable_continuation_is_recoverable(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    result = reconcile(store, profiles={"cap-a": RecoveryProfile(supports_continue=True)})
    assert result.status is RecoveryStatus.RECOVERABLE
    assert result.recommended_action is RecommendedAction.CONTINUE
    assert {"CONTINUATION_PRESENT", "RESUME_SUPPORTED"} <= set(codes(result))


def test_running_without_resume_support_is_blocked(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    result = reconcile(store, profiles={"cap-a": RecoveryProfile(False, False)})
    assert result.status is RecoveryStatus.BLOCKED
    assert result.recommended_action is RecommendedAction.MANUAL_REVIEW
    assert "RESUME_UNAVAILABLE" in codes(result)


def test_handoff_possible_when_resume_unavailable(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    result = reconcile(store, profiles={
        "cap-a": RecoveryProfile(supports_continue=False, supports_handoff=True)
    })
    assert result.status is RecoveryStatus.HANDOFF_POSSIBLE
    assert result.recommended_action is RecommendedAction.HANDOFF
    assert {"HANDOFF_SUPPORTED", "RESUME_UNAVAILABLE"} <= set(codes(result))


def test_pending_human_overrides_recovery_paths(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    gate = HumanGate(store)
    gate.request(task.task_id, requesting_capability="cap-a", reason="publish", question="Allow?")

    observer = Observer(store, human=gate)
    result = Reconciler(observer).reconcile(
        task.task_id,
        profiles={"cap-a": RecoveryProfile(supports_continue=True, supports_handoff=True)},
    )
    assert result.status is RecoveryStatus.WAITING_HUMAN
    assert result.recommended_action is RecommendedAction.WAIT_FOR_HUMAN
    assert "PENDING_HUMAN_REQUEST" in codes(result)


def test_terminal_task_wins_over_stale_runtime(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    task = store.set_lifecycle(task.task_id, LifecycleStatus.COMPLETED, expected_revision=task.revision)
    result = reconcile(store, records=[runtime_record(task.task_id, RuntimeStatus.RUNNING)],
                       profiles={"cap-a": RecoveryProfile(supports_continue=True)})
    assert result.status is RecoveryStatus.TERMINAL
    assert result.recommended_action is RecommendedAction.NONE
    assert "STALE_RUNTIME_RECORD" in codes(result)


def test_lost_runtime_with_continuation_can_be_recoverable(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    result = reconcile(store, records=[runtime_record(task.task_id, RuntimeStatus.LOST)],
                       profiles={"cap-a": RecoveryProfile(supports_continue=True)})
    assert result.status is RecoveryStatus.RECOVERABLE
    assert "RUNTIME_LOST" in codes(result)


def test_lost_runtime_without_recovery_facts_is_lost(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = store.create_task(objective="lost")
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    result = reconcile(store, records=[runtime_record(task.task_id, RuntimeStatus.LOST)])
    assert result.status is RecoveryStatus.LOST
    assert result.recommended_action is RecommendedAction.MARK_LOST


def test_incomplete_invocation_is_orphaned(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = store.create_task(objective="orphan")
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    store.record_event(task.task_id, "EXECUTION_STARTED",
                       expected_revision=task.revision,
                       data={"invocation_id": "inv-orphan", "capability_id": "cap-a"})

    result = reconcile(store)
    assert result.status is RecoveryStatus.ORPHANED
    assert result.recommended_action is RecommendedAction.MANUAL_REVIEW
    assert "INCOMPLETE_INVOCATION" in codes(result)


def test_uncertain_side_effect_requires_manual_review(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    result = reconcile(store, uncertain=True,
                       profiles={"cap-a": RecoveryProfile(supports_continue=True)})
    assert result.status is RecoveryStatus.UNCERTAIN
    assert result.recommended_action is RecommendedAction.MANUAL_REVIEW
    assert "PERSISTENCE_UNCERTAIN" in codes(result)


def test_user_runtime_cancel_is_not_task_cancel(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    observer = Observer(store, runtime=StaticRuntime([runtime_record(task.task_id, RuntimeStatus.STOPPED)]))
    observation = observer.observe(task.task_id)
    result = Reconciler(observer).reconcile(task.task_id)

    assert observation.task_status == "RUNNING"
    assert observation.latest_runtime.status == "STOPPED"
    assert observation.task_status != "CANCELLED"
    assert result.status is not RecoveryStatus.TERMINAL


def test_reconciliation_is_read_only_and_deterministic(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    before = store.get_task(task.task_id)
    events = store.list_events(task.task_id)
    observer = Observer(store, runtime=StaticRuntime([]))
    reconciler = Reconciler(observer)

    results = [reconciler.reconcile(task.task_id,
                                    profiles={"cap-a": RecoveryProfile(True, False)})
               for _ in range(3)]
    assert store.get_task(task.task_id) == before
    assert store.list_events(task.task_id) == events
    signatures = {(r.status, r.reason, r.recommended_action, tuple(codes(r))) for r in results}
    assert len(signatures) == 1


def test_restart_reload_observation_and_reconciliation(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = running_task(store)
    script = f"""
import json, os
from core.observation import Observer
from core.recovery import Reconciler, RecoveryProfile
from stores import JSONTaskStateStore
store = JSONTaskStateStore(os.environ["RECON_STORE"])
observer = Observer(store)
observation = observer.observe(os.environ["RECON_TASK"])
result = Reconciler(observer).reconcile(
    os.environ["RECON_TASK"],
    profiles={{"cap-a": RecoveryProfile(False, False)}},
)
print(json.dumps({{
    "task_status": observation.task_status,
    "runtime_count": len(observation.runtimes),
    "checkpoint": observation.cursor_checkpoint,
    "continuation": observation.continuation_present,
    "status": result.status.value,
    "reason_codes": [e.code for e in result.evidence],
}}))
"""
    env = dict(os.environ)
    env.update(RECON_STORE=str(tmp_path / "tasks"), RECON_TASK=task.task_id)
    completed = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["task_status"] == "RUNNING"
    assert payload["runtime_count"] == 0
    assert payload["checkpoint"] == "cp-1"
    assert payload["continuation"] is True
    assert payload["status"] == "BLOCKED"
    assert "RESUME_UNAVAILABLE" in payload["reason_codes"]
