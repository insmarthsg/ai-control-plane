"""Task Controller: bounded advance, continuity, failures, safety."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

import stores.json_task_store as json_task_store
from core.capability import CapabilityCandidate
from core.controller import AdvanceStatus, TaskController
from core.errors import classify_provider_error
from core.execution import (
    ExecutionFailure,
    ExecutionFailureKind,
    ExecutionMode,
    ExecutionResult,
    ExecutionStatus,
)
from core.provider_health import HealthStatus, ProviderHealthTracker
from core.router import NoRoute, ThinRouter, UserConstraints
from core.task import Artifact, CapabilityActivity, InsertionFrame, LifecycleStatus
from stores import JSONTaskStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeExecution:
    def __init__(self, handler: Callable) -> None:
        self.handler = handler
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return self.handler(request)


def candidate(
    capability_id: str,
    provider: str,
    *,
    rank: int = 10,
    handoff: bool = False,
    refs: frozenset[str] = frozenset(),
) -> CapabilityCandidate:
    return CapabilityCandidate(
        capability_id=capability_id,
        kind="execution",
        provider=provider,
        preference_rank=rank,
        supports_task_state_handoff=handoff,
        continuation_refs=refs,
    )


def setup(tmp_path: Path, handlers: dict[str, Callable], providers: dict[str, HealthStatus] | None = None):
    store = JSONTaskStateStore(tmp_path / "tasks")
    health = ProviderHealthTracker()
    for provider, status in (providers or {}).items():
        if status is HealthStatus.AVAILABLE:
            health.record_success(provider)
        elif status is HealthStatus.COOLDOWN:
            health.record_error(classify_provider_error(provider, message="429", http_status=429))
        elif status is HealthStatus.AUTH_REQUIRED:
            health.record_error(classify_provider_error(provider, message="401", http_status=401))
        elif status is HealthStatus.UNAVAILABLE:
            health.record_error(classify_provider_error(provider, message="503", http_status=503))
    capabilities = {name: FakeExecution(handler) for name, handler in handlers.items()}
    controller = TaskController(
        store=store,
        router=ThinRouter(),
        health=health,
        capabilities=capabilities,
    )
    return controller, store, health, capabilities


def test_one_advance_completes_task(tmp_path: Path) -> None:
    controller, store, health, caps = setup(
        tmp_path,
        {
            "cap-a": lambda request: ExecutionResult(
                status=ExecutionStatus.COMPLETED,
                artifacts=(Artifact(kind="result", producer_capability="cap-a", payload=89),),
                checkpoint="done",
                continuation_ref="cont-a",
            )
        },
        {"p-a": HealthStatus.AVAILABLE},
    )
    task = store.create_task(objective="calculate")

    outcome = controller.advance(task.task_id, candidates=[candidate("cap-a", "p-a")])

    assert outcome.status is AdvanceStatus.COMPLETED
    assert outcome.task_id == task.task_id
    assert outcome.invocation_id == caps["cap-a"].requests[0].invocation_id
    assert caps["cap-a"].requests[0].execution_mode is ExecutionMode.FRESH

    reloaded = store.get_task(task.task_id)
    assert reloaded.lifecycle_status is LifecycleStatus.COMPLETED
    assert reloaded.artifacts[0].payload == 89
    assert reloaded.cursor.checkpoint == "done"
    assert reloaded.active_capability.capability_id == "cap-a"
    assert health.get("p-a").status is HealthStatus.AVAILABLE

    events = [event.type for event in store.list_events(task.task_id)]
    assert events == [
        "TASK_CREATED",
        "TASK_STARTED",
        "ROUTING_DECIDED",
        "EXECUTION_STARTED",
        "EXECUTION_RESULT",
        "TASK_COMPLETED",
    ]


def test_two_advances_continue_with_opaque_ref(tmp_path: Path) -> None:
    calls: list = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return ExecutionResult(
                status=ExecutionStatus.PROGRESS,
                artifacts=(
                    Artifact(kind="analysis", producer_capability="cap-a", payload=[17, 29, 43]),
                ),
                checkpoint="numbers-ready",
                continuation_ref="opaque/cont#1",
            )
        assert request.execution_mode is ExecutionMode.CONTINUE
        assert request.continuation_ref == "opaque/cont#1"
        assert request.objective == "two steps"
        assert request.checkpoint == "numbers-ready"
        assert request.artifacts[0].payload == [17, 29, 43]
        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            artifacts=(Artifact(kind="result", producer_capability="cap-a", payload=89),),
            checkpoint="done",
            continuation_ref="opaque/cont#2",
        )

    controller, store, _, caps = setup(
        tmp_path, {"cap-a": handler}, {"p-a": HealthStatus.AVAILABLE}
    )
    task = store.create_task(objective="two steps")
    candidates = [candidate("cap-a", "p-a", refs=frozenset({"opaque/cont#1"}))]

    first = controller.advance(task.task_id, candidates=candidates)
    assert first.status is AdvanceStatus.PROGRESSED
    assert first.task_status is LifecycleStatus.RUNNING
    assert caps["cap-a"].requests[0].execution_mode is ExecutionMode.FRESH

    persisted = store.get_task(task.task_id)
    assert persisted.cursor.checkpoint == "numbers-ready"
    assert persisted.cursor.continuation_ref == "opaque/cont#1"
    assert persisted.artifacts[0].payload == [17, 29, 43]

    second = controller.advance(task.task_id, candidates=candidates)
    assert second.status is AdvanceStatus.COMPLETED
    assert second.task_id == task.task_id
    assert len(caps["cap-a"].requests) == 2
    assert store.get_task(task.task_id).lifecycle_status is LifecycleStatus.COMPLETED


def test_provider_failure_updates_health_then_next_advance_hands_off(tmp_path: Path) -> None:
    provider_error = classify_provider_error(
        "p-a", message="simulated 429 rate limit", http_status=429, headers={"Retry-After": "30"}
    )

    def fail_a(request):
        return ExecutionFailure(
            kind=ExecutionFailureKind.PROVIDER,
            category="RATE_LIMIT",
            message="simulated provider rate limit",
            retryable=True,
            provider_error=provider_error,
            partial_artifacts=(
                Artifact(kind="analysis", producer_capability="cap-a", payload=[17, 29, 43]),
            ),
            checkpoint="numbers-ready",
            continuation_ref="cont-a",
        )

    def finish_b(request):
        assert request.execution_mode is ExecutionMode.HANDOFF
        assert request.continuation_ref is None
        assert request.objective == "continuity test"
        assert request.checkpoint == "numbers-ready"
        assert request.artifacts[0].payload == [17, 29, 43]
        assert request.durable_context == {"constraint": "no schema changes"}
        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            artifacts=(Artifact(kind="result", producer_capability="cap-b", payload=89),),
            checkpoint="done",
        )

    controller, store, health, caps = setup(
        tmp_path,
        {"cap-a": fail_a, "cap-b": finish_b},
        {"p-a": HealthStatus.AVAILABLE, "p-b": HealthStatus.AVAILABLE},
    )
    task = store.create_task(
        objective="continuity test",
        durable_context={"constraint": "no schema changes"},
    )
    candidates = [
        candidate("cap-a", "p-a", rank=10),
        candidate("cap-b", "p-b", rank=20, handoff=True),
    ]

    first = controller.advance(task.task_id, candidates=candidates)
    assert first.status is AdvanceStatus.FAILED
    assert health.get("p-a").status is HealthStatus.COOLDOWN
    assert health.get("p-b").status is HealthStatus.AVAILABLE
    assert len(caps["cap-a"].requests) == 1

    partial = store.get_task(task.task_id)
    assert partial.lifecycle_status is LifecycleStatus.RUNNING
    assert partial.cursor.continuation_ref == "cont-a"
    assert partial.artifacts[0].payload == [17, 29, 43]
    assert "EXECUTION_FAILED" in [event.type for event in store.list_events(task.task_id)]

    second = controller.advance(task.task_id, candidates=candidates)
    assert second.status is AdvanceStatus.COMPLETED
    assert second.task_id == task.task_id
    assert len(caps["cap-a"].requests) == 1  # not retried inside the controller

    final = store.get_task(task.task_id)
    assert final.lifecycle_status is LifecycleStatus.COMPLETED
    assert [artifact.payload for artifact in final.artifacts] == [[17, 29, 43], 89]
    assert final.active_capability.capability_id == "cap-b"


def test_execution_failure_does_not_poison_provider_health(tmp_path: Path) -> None:
    def fail(request):
        return ExecutionFailure(
            kind=ExecutionFailureKind.EXECUTION,
            category="WORKSPACE_PERMISSION_DENIED",
            message="workspace is not writable",
        )

    controller, store, health, _ = setup(
        tmp_path, {"cap-a": fail}, {"p-a": HealthStatus.AVAILABLE}
    )
    task = store.create_task(objective="env failure")

    outcome = controller.advance(task.task_id, candidates=[candidate("cap-a", "p-a")])

    assert outcome.status is AdvanceStatus.FAILED
    assert outcome.task_status is LifecycleStatus.BLOCKED
    assert health.get("p-a").status is HealthStatus.AVAILABLE
    assert store.get_task(task.task_id).lifecycle_status is LifecycleStatus.BLOCKED
    events = [event.type for event in store.list_events(task.task_id)]
    assert "EXECUTION_FAILED" in events and "TASK_BLOCKED" in events


def test_no_route_executes_nothing_and_blocks(tmp_path: Path) -> None:
    def never(request):
        raise AssertionError("execute must not be called on NO_ROUTE")

    controller, store, health, _ = setup(
        tmp_path,
        {"cap-a": never, "cap-b": never},
        {"p-a": HealthStatus.COOLDOWN, "p-b": HealthStatus.AUTH_REQUIRED},
    )
    task = store.create_task(objective="nothing available")

    outcome = controller.advance(
        task.task_id,
        candidates=[candidate("cap-a", "p-a"), candidate("cap-b", "p-b", rank=20)],
    )

    assert outcome.status is AdvanceStatus.NO_ROUTE
    assert isinstance(outcome.routing_decision, NoRoute)
    assert outcome.task_status is LifecycleStatus.BLOCKED
    assert store.get_task(task.task_id).lifecycle_status is LifecycleStatus.BLOCKED
    reasons = {item.capability_id: item.reason.value for item in outcome.routing_decision.rejected_candidates}
    assert reasons == {"cap-a": "PROVIDER_COOLDOWN", "cap-b": "PROVIDER_AUTH_REQUIRED"}


def test_user_require_unavailable_does_not_fallback(tmp_path: Path) -> None:
    def never(request):
        raise AssertionError("fallback capability must not be executed")

    controller, store, _, _ = setup(
        tmp_path,
        {"cap-a": never, "cap-b": never},
        {"p-a": HealthStatus.COOLDOWN, "p-b": HealthStatus.AVAILABLE},
    )
    task = store.create_task(objective="required capability")
    constraints = UserConstraints(required=frozenset({"cap-a"}))

    outcome = controller.advance(
        task.task_id,
        candidates=[candidate("cap-a", "p-a"), candidate("cap-b", "p-b", rank=20)],
        constraints=constraints,
    )

    assert outcome.status is AdvanceStatus.NO_ROUTE
    assert outcome.task_status is LifecycleStatus.BLOCKED
    assert isinstance(outcome.routing_decision, NoRoute)
    assert "REQUIRE:cap-a" in outcome.routing_decision.unsatisfied_constraints


def test_revision_conflict_does_not_overwrite_external_change(tmp_path: Path) -> None:
    holder: dict = {}

    def handler(request):
        fresh = holder["store"].get_task(request.task_id)
        holder["store"].set_durable_context(
            request.task_id, {"external": "changed"}, expected_revision=fresh.revision
        )
        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            artifacts=(Artifact(kind="result", producer_capability="cap-a", payload=89),),
        )

    controller, store, _, _ = setup(
        tmp_path, {"cap-a": handler}, {"p-a": HealthStatus.AVAILABLE}
    )
    holder["store"] = store
    task = store.create_task(objective="conflict")

    outcome = controller.advance(task.task_id, candidates=[candidate("cap-a", "p-a")])

    assert outcome.status is AdvanceStatus.CONFLICT
    assert outcome.execution is not None
    reloaded = store.get_task(task.task_id)
    assert reloaded.lifecycle_status is LifecycleStatus.RUNNING
    assert reloaded.durable_context == {"external": "changed"}
    assert reloaded.artifacts == ()
    events = [event.type for event in store.list_events(task.task_id)]
    assert "EXECUTION_RESULT" not in events and "TASK_COMPLETED" not in events


def test_persistence_failure_does_not_falsely_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"fail": False}
    real_write = json_task_store._atomic_write

    def broken_write(path, payload):
        if state["fail"]:
            raise OSError("disk full")
        return real_write(path, payload)

    monkeypatch.setattr(json_task_store, "_atomic_write", broken_write)

    def handler(request):
        state["fail"] = True
        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            artifacts=(Artifact(kind="result", producer_capability="cap-a", payload=89),),
        )

    controller, store, _, _ = setup(
        tmp_path, {"cap-a": handler}, {"p-a": HealthStatus.AVAILABLE}
    )
    task = store.create_task(objective="persistence failure")

    outcome = controller.advance(task.task_id, candidates=[candidate("cap-a", "p-a")])

    assert outcome.status is AdvanceStatus.PERSISTENCE_FAILED
    assert outcome.execution is not None
    reloaded = store.get_task(task.task_id)
    assert reloaded.lifecycle_status is LifecycleStatus.RUNNING
    assert reloaded.artifacts == ()
    events = [event.type for event in store.list_events(task.task_id)]
    assert "EXECUTION_STARTED" in events
    assert "TASK_COMPLETED" not in events


def test_active_insertion_frame_is_preserved(tmp_path: Path) -> None:
    controller, store, _, caps = setup(
        tmp_path,
        {"cap-a": lambda request: ExecutionResult(status=ExecutionStatus.COMPLETED)},
        {"p-a": HealthStatus.AVAILABLE},
    )
    task = store.create_task(objective="insertion")
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    task = store.push_insertion(
        task.task_id,
        InsertionFrame(capability=CapabilityActivity("review", "judgment"), reason="risk"),
        expected_revision=task.revision,
    )
    before = store.get_task(task.task_id)

    outcome = controller.advance(task.task_id, candidates=[candidate("cap-a", "p-a")])

    after = store.get_task(task.task_id)
    assert outcome.status is AdvanceStatus.BLOCKED
    assert after.insertions == before.insertions
    assert after.revision == before.revision
    assert caps["cap-a"].requests == []


def test_secrets_in_execution_artifacts_are_not_persisted(tmp_path: Path) -> None:
    def handler(request):
        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            artifacts=(
                Artifact(
                    kind="debug",
                    producer_capability="cap-a",
                    payload={"api_key": "sk-test-controller-secret", "ok": 17},
                ),
            ),
        )

    controller, store, _, _ = setup(
        tmp_path, {"cap-a": handler}, {"p-a": HealthStatus.AVAILABLE}
    )
    task = store.create_task(objective="secret")

    controller.advance(task.task_id, candidates=[candidate("cap-a", "p-a")])

    raw = (tmp_path / "tasks" / f"{task.task_id}.json").read_text()
    assert "sk-test-controller-secret" not in raw
    reloaded = store.get_task(task.task_id)
    assert reloaded.artifacts[0].payload["api_key"] == "[REDACTED]"
    assert reloaded.artifacts[0].payload["ok"] == 17


def test_controller_and_execution_contract_have_no_vendor_or_loop_vocabulary() -> None:
    for name in ("controller.py", "execution.py"):
        source = (REPO_ROOT / "core" / name).read_text(encoding="utf-8").lower()
        for token in ("openai", "chatgpt", "glm", "zai", "deepseek", "dsh", "429", "httpx"):
            assert token not in source, f"{name} leaks {token!r}"

        tree = ast.parse((REPO_ROOT / "core" / name).read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree)), f"{name} contains a loop"


def test_controller_imports_without_dsh_adapter() -> None:
    program = """
import sys
import core.controller, core.execution
loaded = sorted(m for m in sys.modules if m.split(".")[0] in {"adapters", "stores"})
assert not loaded, loaded
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", program], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
