"""REAL provider smoke for the DSH execution adapter.

Runs only when ACP_REAL_DSH_SMOKE=1. Requires a working `dsh` CLI, real
credentials in DSH_HOME, and network access. These tests call a real model and
consume a small amount of quota; they are never part of the default suite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.capability import CapabilityCandidate
from core.controller import AdvanceStatus, TaskController
from core.execution import ExecutionMode, ExecutionRequest, ExecutionResult
from core.provider_health import ProviderHealthTracker
from core.router import ThinRouter
from core.task import LifecycleStatus
from stores import JSONTaskStateStore

pytestmark = [
    pytest.mark.real_dsh,
    pytest.mark.skipif(
        os.environ.get("ACP_REAL_DSH_SMOKE") != "1",
        reason="real provider smoke disabled; set ACP_REAL_DSH_SMOKE=1",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER = os.environ.get("ACP_DSH_PROVIDER", "deepseek-official")
MODEL = os.environ.get("ACP_DSH_MODEL", "deepseek-chat")
TIMEOUT = float(os.environ.get("ACP_DSH_TIMEOUT", "240"))


def real_adapter(tmp_path: Path):
    from adapters.dsh_execution import DSHExecutionAdapter

    return DSHExecutionAdapter(
        require_process_tree=False,  # Opt-in smoke exercises group cleanup only.
        provider=PROVIDER,
        model=MODEL,
        cwd=str(REPO_ROOT),
        timeout_seconds=TIMEOUT,
        permission_mode="read-only",
    )


def smoke_request(invocation: str, token: str, mode=ExecutionMode.FRESH, **kwargs):
    return ExecutionRequest(
        invocation_id=invocation,
        task_id="real-smoke",
        objective=(
            f"Reply with the exact token {token}. Do not use any tools. "
            "End with the completion marker line."
        ),
        execution_mode=mode,
        workspace_path=str(REPO_ROOT),
        **kwargs,
    )


def test_real_fresh_execution(tmp_path: Path) -> None:
    result = real_adapter(tmp_path).execute(smoke_request("real-smoke-1", "ACP_DSH_SMOKE_OK"))
    assert isinstance(result, ExecutionResult), result
    assert "ACP_DSH_SMOKE_OK" in (result.output or "")
    assert "ACP_EXECUTION_STATUS=COMPLETED" in (result.output or "")
    assert result.continuation_ref and result.continuation_ref.startswith("dsh-session:")
    print("REAL_SMOKE provider=", PROVIDER, "model=", MODEL)
    print("REAL_SMOKE session_id=", result.metadata["session_id"])
    print("REAL_SMOKE message_id=", result.metadata["message_id"])
    print("REAL_SMOKE usage=", result.metadata["usage"])
    print("REAL_SMOKE response_id=", result.metadata["response_id"])
    print("REAL_SMOKE turn_end=", result.metadata["turn_end_kind"])


def test_real_continue_cross_process(tmp_path: Path) -> None:
    first = real_adapter(tmp_path)
    fresh = first.execute(smoke_request("real-cont-1", "ACP_DSH_SMOKE_OK"))
    assert isinstance(fresh, ExecutionResult), fresh
    ref = fresh.continuation_ref
    assert ref is not None

    second = real_adapter(tmp_path)  # brand-new runtime process
    continued = second.execute(
        smoke_request(
            "real-cont-2", "ACP_DSH_CONTINUE_OK",
            mode=ExecutionMode.CONTINUE, continuation_ref=ref,
        )
    )
    if isinstance(continued, ExecutionResult):
        assert continued.continuation_ref == ref
        assert "ACP_DSH_CONTINUE_OK" in (continued.output or "")
        print("REAL_CONTINUE session=", ref, "VERIFIED")
        return
    message = continued.message
    if "session/resume" in message or "already exists" in message:
        pytest.skip(
            f"DSH CLI runtime lacks cross-process session resume: {message}"
        )
    pytest.fail(f"real continue failed: {continued.category}: {message}")


def test_real_controller_execution_closure(tmp_path: Path) -> None:
    from adapters.git_workspace import GitWorkspaceGuard
    from core.workspace import WorkspacePolicy, bind_workspace
    from tests.helpers.git_workspace_fixture import build
    from tests.test_workspace_integration import binding

    fixture = build(tmp_path)
    store = JSONTaskStateStore(tmp_path / "tasks")
    health = ProviderHealthTracker()
    health.record_success(PROVIDER)
    controller = TaskController(
        store=store,
        router=ThinRouter(),
        health=health,
        capabilities={"dsh-exec": real_adapter(tmp_path)},
        workspace_guard=GitWorkspaceGuard(),
    )
    task = store.create_task(
        objective=(
            "Reply with the exact token ACP_DSH_CONTROLLER_OK. Do not use any "
            "tools. End with the completion marker line."
        )
    )
    bind_workspace(store, task.task_id, binding(
        fixture, policy=WorkspacePolicy(read_only=True, require_tracking_ref=True)),
        expected_revision=task.revision)
    outcome = controller.advance(
        task.task_id,
        candidates=[CapabilityCandidate("dsh-exec", "execution", provider=PROVIDER)],
    )
    assert outcome.status is AdvanceStatus.COMPLETED, outcome
    reloaded = store.get_task(task.task_id)
    assert reloaded.lifecycle_status is LifecycleStatus.COMPLETED
    joined = json.dumps([a.payload for a in reloaded.artifacts], ensure_ascii=False)
    assert "ACP_DSH_CONTROLLER_OK" in joined
    print("REAL_CONTROLLER task_id=", task.task_id, "revision=", reloaded.revision)
