"""Task State model and store behaviour: identity, lifecycle, context, records."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest

from core.task import (
    Artifact,
    CapabilityActivity,
    ExecutionCursor,
    Interruption,
    LifecycleStatus,
)
from core.task_store import (
    InvalidTaskState,
    InvalidTransition,
    RevisionConflict,
    TaskNotFound,
    TaskStateStore,
)
from stores import JSONTaskStateStore


def _store(tmp_path: Path) -> JSONTaskStateStore:
    return JSONTaskStateStore(tmp_path / "tasks")


def _running(store: JSONTaskStateStore, objective: str = "continuity test"):
    task = store.create_task(objective=objective, actor="fake-executor-a")
    return store.set_lifecycle(
        task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision, actor="fake-executor-a"
    )


def test_store_implements_the_storage_agnostic_protocol(tmp_path: Path) -> None:
    assert isinstance(_store(tmp_path), TaskStateStore)


def test_create_and_get_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = store.create_task(
        objective="continuity test",
        task_input={"scope": "local"},
        durable_context={"constraint": "no schema changes"},
    )

    assert task.lifecycle_status is LifecycleStatus.CREATED
    assert task.revision == 0
    assert uuid.UUID(task.task_id)  # opaque, stable UUID

    loaded = JSONTaskStateStore(tmp_path / "tasks").get_task(task.task_id)
    assert loaded == task
    assert store.list_tasks() == [task.task_id]

    events = store.list_events(task.task_id)
    assert [(event.seq, event.type, event.revision) for event in events] == [(1, "TASK_CREATED", 0)]

    with pytest.raises(TaskNotFound):
        store.get_task(str(uuid.uuid4()))


def test_task_identity_is_independent_of_executor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _running(store)
    original_id = task.task_id

    task = store.set_active_capability(
        task.task_id,
        CapabilityActivity("fake-executor-b", "execution"),
        expected_revision=task.revision,
        actor="fake-executor-b",
    )
    task = store.set_durable_context(
        task.task_id, {"handoff": "executor-b"}, expected_revision=task.revision, actor="fake-executor-b"
    )

    assert task.task_id == original_id
    assert "fake-executor-a" not in task.task_id
    assert task.active_capability == CapabilityActivity("fake-executor-b", "execution")
    assert task.durable_context["handoff"] == "executor-b"

    # The persisted identity does not gain an owner field.
    document = json.loads((tmp_path / "tasks" / f"{original_id}.json").read_text())
    assert "owner" not in document["task"]
    actors = {event.actor for event in store.list_events(original_id)}
    assert actors == {"fake-executor-a", "fake-executor-b"}


def test_lifecycle_transitions_are_generic_and_validated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = store.create_task(objective="lifecycle")

    with pytest.raises(InvalidTransition):
        store.set_lifecycle(
            task.task_id, LifecycleStatus.COMPLETED, expected_revision=task.revision
        )

    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    task = store.set_lifecycle(task.task_id, LifecycleStatus.BLOCKED, expected_revision=task.revision)
    assert task.lifecycle_status is LifecycleStatus.BLOCKED

    task = store.resume(task.task_id, expected_revision=task.revision)
    assert task.lifecycle_status is LifecycleStatus.RUNNING

    task = store.set_lifecycle(task.task_id, LifecycleStatus.COMPLETED, expected_revision=task.revision)
    assert task.lifecycle_status is LifecycleStatus.COMPLETED
    with pytest.raises(InvalidTaskState):
        store.add_artifact(
            task.task_id,
            Artifact(kind="late", producer_capability="fake"),
            expected_revision=task.revision,
        )


def test_durable_context_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _running(store)
    context = {
        "findings": ["x is the cause", "files y and z are involved"],
        "constraints": {"database_schema": "read-only"},
    }
    task = store.set_durable_context(task.task_id, context, expected_revision=task.revision)

    reloaded = _store(tmp_path).get_task(task.task_id)
    assert reloaded.durable_context == context


def test_artifacts_round_trip_inline_and_reference(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _running(store)

    task = store.add_artifact(
        task.task_id,
        Artifact(kind="analysis", producer_capability="fake-executor-a", payload=[17, 29, 43]),
        expected_revision=task.revision,
    )
    task = store.add_artifact(
        task.task_id,
        Artifact(kind="plan", producer_capability="fake-executor-a", reference="artifact://plan/1"),
        expected_revision=task.revision,
    )

    reloaded = _store(tmp_path).get_task(task.task_id)
    assert [artifact.kind for artifact in reloaded.artifacts] == ["analysis", "plan"]
    assert reloaded.artifacts[0].payload == [17, 29, 43]
    assert reloaded.artifacts[1].reference == "artifact://plan/1"


def test_cursor_and_checkpoint_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _running(store)
    cursor = ExecutionCursor(
        capability_id="fake-execution",
        checkpoint="after-analysis",
        continuation_ref="opaque-ref-42",
        metadata={"attempt": 2},
    )
    task = store.save_checkpoint(task.task_id, cursor, expected_revision=task.revision)

    reloaded = _store(tmp_path).get_task(task.task_id)
    assert reloaded.cursor == cursor
    assert reloaded.cursor.checkpoint == "after-analysis"


def test_interruption_round_trips_and_resume_clears_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _running(store)
    cursor = ExecutionCursor(capability_id="fake-execution", checkpoint="before-pause")
    task = store.save_checkpoint(task.task_id, cursor, expected_revision=task.revision)

    task = store.interrupt(
        task.task_id,
        Interruption(reason="human_required", resume_point=cursor, evidence={"approval": "pending"}),
        expected_revision=task.revision,
        actor="fake-executor-a",
    )
    assert task.lifecycle_status is LifecycleStatus.PAUSED

    reloaded = _store(tmp_path).get_task(task.task_id)
    assert reloaded.interruption.reason == "human_required"
    assert reloaded.interruption.resume_point.checkpoint == "before-pause"
    assert reloaded.cursor.checkpoint == "before-pause"

    resumed = store.resume(task.task_id, expected_revision=task.revision)
    assert resumed.lifecycle_status is LifecycleStatus.RUNNING
    assert resumed.interruption is None
    assert resumed.cursor.checkpoint == "before-pause"


def test_optimistic_concurrency_rejects_stale_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _running(store)
    snapshot_revision = task.revision

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def writer(name: str) -> None:
        barrier.wait()
        try:
            store.add_artifact(
                task.task_id,
                Artifact(kind=name, producer_capability=name),
                expected_revision=snapshot_revision,
                actor=name,
            )
            outcomes.append("ok")
        except RevisionConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "ok"]
    reloaded = _store(tmp_path).get_task(task.task_id)
    assert reloaded.revision == snapshot_revision + 1
    assert len(reloaded.artifacts) == 1


def test_secrets_never_persist_and_are_redacted_on_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _running(store)
    task = store.set_durable_context(
        task.task_id,
        {
            "api_key": "sk-test-abcdef123456",
            "note": "Authorization: Bearer abcdefghijklmnop",
            "nested": {"password": "hunter2"},
        },
        expected_revision=task.revision,
    )
    task = store.add_artifact(
        task.task_id,
        Artifact(
            kind="debug",
            producer_capability="fake-executor-a",
            payload={"access_token": "tok_test_secret", "ok": 17},
        ),
        expected_revision=task.revision,
    )

    raw = (tmp_path / "tasks" / f"{task.task_id}.json").read_text()
    for secret in ("sk-test-abcdef123456", "abcdefghijklmnop", "hunter2", "tok_test_secret"):
        assert secret not in raw

    loaded = _store(tmp_path).get_task(task.task_id)
    assert loaded.durable_context["api_key"] == "[REDACTED]"
    assert loaded.durable_context["nested"]["password"] == "[REDACTED]"
    assert loaded.artifacts[-1].payload["access_token"] == "[REDACTED]"
    assert loaded.artifacts[-1].payload["ok"] == 17
