"""Insertability: temporary frames, resume points, and nesting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.task import (
    Artifact,
    CapabilityActivity,
    ExecutionCursor,
    InsertionFrame,
    LifecycleStatus,
)
from core.task_store import InvalidTaskState
from stores import JSONTaskStateStore

EXECUTION = CapabilityActivity("fake-execution", "execution")
REVIEW = CapabilityActivity("fake-review", "judgment")
HUMAN = CapabilityActivity("fake-human", "human")


def _store(tmp_path: Path) -> JSONTaskStateStore:
    return JSONTaskStateStore(tmp_path / "tasks")


def _execution_task(store: JSONTaskStateStore):
    task = store.create_task(objective="insertion test")
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    task = store.set_active_capability(task.task_id, EXECUTION, expected_revision=task.revision)
    task = store.save_checkpoint(
        task.task_id,
        ExecutionCursor(capability_id=EXECUTION.capability_id, checkpoint="after-analysis"),
        expected_revision=task.revision,
    )
    return task


def test_push_and_complete_insertion_restores_execution_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _execution_task(store)

    task = store.push_insertion(
        task.task_id,
        InsertionFrame(capability=REVIEW, reason="risk detected", input={"target": "patch"}),
        expected_revision=task.revision,
        actor="fake-execution",
    )
    assert task.insertion_depth == 1
    assert task.active_capability == REVIEW
    assert task.cursor is None

    task = store.complete_insertion(
        task.task_id,
        expected_revision=task.revision,
        actor="fake-review",
        result=Artifact(kind="review-result", producer_capability="fake-review", payload={"verdict": "ok"}),
    )
    assert task.insertion_depth == 0
    assert task.active_capability == EXECUTION
    assert task.cursor.checkpoint == "after-analysis"
    assert task.artifacts[-1].kind == "review-result"

    reloaded = _store(tmp_path).get_task(task.task_id)
    assert reloaded.cursor.checkpoint == "after-analysis"
    assert reloaded.active_capability == EXECUTION
    events = [event.type for event in store.list_events(task.task_id)]
    assert "INSERTION_PUSHED" in events and "INSERTION_COMPLETED" in events


def test_nested_insertions_restore_each_frame_in_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _execution_task(store)

    task = store.push_insertion(
        task.task_id,
        InsertionFrame(capability=REVIEW, reason="risk detected"),
        expected_revision=task.revision,
    )
    task = store.save_checkpoint(
        task.task_id,
        ExecutionCursor(capability_id=REVIEW.capability_id, checkpoint="review-half"),
        expected_revision=task.revision,
    )
    task = store.push_insertion(
        task.task_id,
        InsertionFrame(capability=HUMAN, reason="approval required"),
        expected_revision=task.revision,
    )
    assert task.insertion_depth == 2
    assert task.active_capability == HUMAN

    task = store.complete_insertion(
        task.task_id,
        expected_revision=task.revision,
        result=Artifact(kind="approval", producer_capability="fake-human", payload="approved"),
    )
    assert task.insertion_depth == 1
    assert task.active_capability == REVIEW
    assert task.cursor.checkpoint == "review-half"

    task = store.complete_insertion(
        task.task_id,
        expected_revision=task.revision,
        result=Artifact(kind="review-result", producer_capability="fake-review", payload="ok"),
    )
    assert task.insertion_depth == 0
    assert task.active_capability == EXECUTION
    assert task.cursor.checkpoint == "after-analysis"

    task = store.set_lifecycle(
        task.task_id, LifecycleStatus.COMPLETED, expected_revision=task.revision
    )
    assert task.lifecycle_status is LifecycleStatus.COMPLETED

    document = json.loads((tmp_path / "tasks" / f"{task.task_id}.json").read_text())
    assert document["task"]["insertions"] == []
    assert "next_stage" not in document["task"]


def test_insertion_requires_running_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _execution_task(store)
    task = store.set_lifecycle(task.task_id, LifecycleStatus.PAUSED, expected_revision=task.revision)

    with pytest.raises(InvalidTaskState):
        store.push_insertion(
            task.task_id,
            InsertionFrame(capability=REVIEW, reason="not allowed while paused"),
            expected_revision=task.revision,
        )


def test_schema_supports_future_nesting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _execution_task(store)
    task = store.push_insertion(
        task.task_id,
        InsertionFrame(capability=REVIEW, reason="one"),
        expected_revision=task.revision,
    )
    task = store.push_insertion(
        task.task_id,
        InsertionFrame(capability=HUMAN, reason="two"),
        expected_revision=task.revision,
    )

    document = json.loads((tmp_path / "tasks" / f"{task.task_id}.json").read_text())
    frames = document["task"]["insertions"]
    assert isinstance(frames, list) and len(frames) == 2
    assert frames[0]["return_cursor"]["checkpoint"] == "after-analysis"
    assert frames[1]["return_capability"]["capability_id"] == REVIEW.capability_id
