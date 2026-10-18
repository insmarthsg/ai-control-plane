"""Persistence guarantees: restart recovery, atomicity, isolation, conflict."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import stores.json_task_store as json_task_store
from core.task import Artifact, LifecycleStatus
from core.task_store import TaskStoreError
from stores import JSONTaskStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path: Path) -> JSONTaskStateStore:
    return JSONTaskStateStore(tmp_path / "tasks")


def _running(store: JSONTaskStateStore, objective: str = "durability"):
    task = store.create_task(objective=objective)
    return store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)


WORKER_A = textwrap.dedent(
    """
    import json, os
    from core.task import Artifact, CapabilityActivity, ExecutionCursor, LifecycleStatus
    from stores import JSONTaskStateStore

    store = JSONTaskStateStore(os.environ["TASK_STORE_ROOT"])
    task = store.create_task(
        objective="continuity test",
        durable_context={"constraint": "no database schema changes"},
        actor="fake-executor-a",
    )
    task = store.set_lifecycle(
        task.task_id, LifecycleStatus.RUNNING,
        expected_revision=task.revision, actor="fake-executor-a",
    )
    task = store.set_active_capability(
        task.task_id, CapabilityActivity("fake-executor-a", "execution"),
        expected_revision=task.revision, actor="fake-executor-a",
    )
    task = store.add_artifact(
        task.task_id,
        Artifact(kind="analysis", producer_capability="fake-executor-a", payload=[17, 29, 43]),
        expected_revision=task.revision, actor="fake-executor-a",
    )
    task = store.save_checkpoint(
        task.task_id,
        ExecutionCursor(
            capability_id="fake-executor-a",
            checkpoint="after-analysis",
            continuation_ref="opaque-ref-a",
        ),
        expected_revision=task.revision, actor="fake-executor-a",
    )
    print(json.dumps({"task_id": task.task_id, "revision": task.revision}))
    """
)

WORKER_B = textwrap.dedent(
    """
    import json, os
    from core.task import Artifact, CapabilityActivity, LifecycleStatus
    from stores import JSONTaskStateStore

    payload = json.loads(os.environ["TASK_POINTER"])
    task_id = payload["task_id"]

    store = JSONTaskStateStore(os.environ["TASK_STORE_ROOT"])
    task = store.get_task(task_id)
    assert task.objective == "continuity test"
    assert task.lifecycle_status is LifecycleStatus.RUNNING
    assert task.artifacts[0].payload == [17, 29, 43]
    assert task.cursor.checkpoint == "after-analysis"
    assert task.revision == payload["revision"]
    assert task.task_id == task_id

    task = store.set_active_capability(
        task.task_id, CapabilityActivity("fake-executor-b", "execution"),
        expected_revision=task.revision, actor="fake-executor-b",
    )
    total = sum(task.artifacts[0].payload)
    task = store.add_artifact(
        task.task_id,
        Artifact(kind="result", producer_capability="fake-executor-b", payload=total),
        expected_revision=task.revision, actor="fake-executor-b",
    )
    task = store.set_lifecycle(
        task.task_id, LifecycleStatus.COMPLETED,
        expected_revision=task.revision, actor="fake-executor-b",
    )
    print(json.dumps({
        "task_id": task.task_id,
        "status": task.lifecycle_status.value,
        "revision": task.revision,
        "total": total,
        "cursor": task.cursor.checkpoint,
        "active": task.active_capability.capability_id,
    }))
    """
)


def _run_worker(code: str, root: Path, pointer: dict | None = None) -> dict:
    env = dict(os.environ)
    env["TASK_STORE_ROOT"] = str(root)
    if pointer is not None:
        env["TASK_POINTER"] = json.dumps(pointer)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_process_restart_and_executor_handoff(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    created = _run_worker(WORKER_A, root)
    task_id = created["task_id"]

    # Process A is gone. Process B recovers only from the persisted document.
    finished = _run_worker(WORKER_B, root, created)

    assert finished["task_id"] == task_id
    assert finished["total"] == 89
    assert finished["status"] == "COMPLETED"
    assert finished["cursor"] == "after-analysis"
    assert finished["active"] == "fake-executor-b"

    store = JSONTaskStateStore(root)
    task = store.get_task(task_id)
    assert task.lifecycle_status is LifecycleStatus.COMPLETED
    assert [artifact.payload for artifact in task.artifacts] == [[17, 29, 43], 89]
    assert task.durable_context == {"constraint": "no database schema changes"}

    events = store.list_events(task_id)
    actors = [event.actor for event in events]
    assert "fake-executor-a" in actors and "fake-executor-b" in actors
    assert task_id not in [event.actor for event in events]


def test_failed_replace_preserves_last_confirmed_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    task = _running(store)
    confirmed_revision = task.revision

    def broken_replace(source, destination):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(json_task_store.os, "replace", broken_replace)
        with pytest.raises(TaskStoreError):
            store.add_artifact(
                task.task_id,
                Artifact(kind="analysis", producer_capability="fake"),
                expected_revision=task.revision,
            )

    reloaded = _store(tmp_path).get_task(task.task_id)
    assert reloaded.revision == confirmed_revision
    assert reloaded.artifacts == ()
    assert list((tmp_path / "tasks").glob(".*.tmp")) == []


def test_one_task_write_failure_does_not_affect_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    task_a = _running(store, objective="task a")
    task_b = _running(store, objective="task b")
    revision_a = task_a.revision

    real_write = json_task_store._atomic_write

    def selective(path, payload):
        if path.stem == task_a.task_id:
            raise OSError("simulated disk failure")
        return real_write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(json_task_store, "_atomic_write", selective)
        with pytest.raises(TaskStoreError):
            store.add_artifact(
                task_a.task_id,
                Artifact(kind="analysis", producer_capability="fake"),
                expected_revision=task_a.revision,
            )
        task_b = store.add_artifact(
            task_b.task_id,
            Artifact(kind="analysis", producer_capability="fake", payload="b"),
            expected_revision=task_b.revision,
        )

    assert task_b.artifacts[0].payload == "b"
    reloaded_a = _store(tmp_path).get_task(task_a.task_id)
    assert reloaded_a.revision == revision_a
    assert reloaded_a.artifacts == ()


def test_document_is_inspectable_and_has_no_pipeline_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _running(store)
    document = json.loads((tmp_path / "tasks" / f"{task.task_id}.json").read_text())

    assert document["schema_version"] == 1
    task_doc = document["task"]
    assert isinstance(task_doc["insertions"], list)
    for forbidden in ("next_stage", "workflow_steps", "pipeline", "stage_number"):
        assert forbidden not in task_doc
    assert document["events"][0]["type"] == "TASK_CREATED"


RACER = textwrap.dedent(
    """
    import json, os, time
    from core.task import Artifact
    from core.task_store import RevisionConflict
    from stores import JSONTaskStateStore

    store = JSONTaskStateStore(os.environ["TASK_STORE_ROOT"])
    task = store.get_task(os.environ["TASK_ID"])
    with open(os.environ["READY"], "w") as marker:
        marker.write("ready")
    while not os.path.exists(os.environ["GO"]):
        time.sleep(0.005)
    try:
        store.add_artifact(
            task.task_id,
            Artifact(kind=os.environ["KIND"], producer_capability="race"),
            expected_revision=task.revision,
            actor=os.environ["KIND"],
        )
        print("OK")
    except RevisionConflict:
        print("CONFLICT")
    """
)


def test_cross_process_stale_writer_cannot_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    store = JSONTaskStateStore(root)
    task = _running(store)

    env = dict(os.environ)
    env["TASK_STORE_ROOT"] = str(root)
    env["TASK_ID"] = task.task_id
    env["GO"] = str(tmp_path / "go")

    processes = []
    for kind in ("a", "b"):
        worker_env = dict(env)
        worker_env["KIND"] = kind
        worker_env["READY"] = str(tmp_path / f"ready-{kind}")
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", RACER],
                cwd=REPO_ROOT,
                env=worker_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    for kind in ("a", "b"):
        deadline = 0
        ready = tmp_path / f"ready-{kind}"
        while not ready.exists():
            deadline += 1
            assert deadline < 2000, "worker did not become ready"
            import time

            time.sleep(0.005)

    (tmp_path / "go").write_text("go")

    outcomes = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        outcomes.append(stdout.strip().splitlines()[-1])

    assert sorted(outcomes) == ["CONFLICT", "OK"]
    reloaded = JSONTaskStateStore(root).get_task(task.task_id)
    assert reloaded.revision == task.revision + 1
    assert len(reloaded.artifacts) == 1
