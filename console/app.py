"""Application layer for the local Control Console.

It only orchestrates existing core capabilities: Workspace Guard, Task Store,
Task Controller, Human Gate, Runtime cancel, Observer, Reconciler. No business
state is invented here; the page reads Observation.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from adapters.git_workspace import GitWorkspaceGuard
from core.capability import CapabilityCandidate
from core.controller import AdvanceOutcome, AdvanceStatus, TaskController
from core.execution import (
    ExecutionFailure,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
)
from core.human import HumanAction, HumanGate
from core.observation import Observer
from core.provider_health import HealthStatus, ProviderHealthTracker
from core.recovery import RecoveryProfile, RecoveryStatus, Reconciler
from core.router import ThinRouter
from core.runtime import RuntimeRecord, RuntimeStatus
from core.task import Artifact, LifecycleStatus, TaskState
from core.workspace import (
    WorkspaceBinding,
    WorkspaceGuard,
    WorkspacePolicy,
    WorkspaceVerification,
    bind_workspace,
    binding_for,
)
from stores import JSONTaskStateStore

__all__ = [
    "CapabilitySpec",
    "ControlConsole",
    "HumanGateDemoCapability",
    "WorkspaceBlocked",
    "build_default_console",
]


class WorkspaceBlocked(RuntimeError):
    def __init__(self, verification: WorkspaceVerification) -> None:
        super().__init__(f"workspace blocked: {verification.reason.value}")
        self.verification = verification


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    label: str
    provider: str
    model: str
    capability: Any
    modes: tuple[str, ...] = ("read_only", "mutating")


class HumanGateDemoCapability:
    """Test capability for validating the Human Gate through real Task State."""

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        decisions = [a for a in request.artifacts if a.kind == "human-decision"]
        if not decisions:
            return ExecutionResult(
                invocation_id=request.invocation_id,
                status=ExecutionStatus.PROGRESS,
                artifacts=(Artifact(kind="analysis", producer_capability="demo-human",
                                    payload={"demo": True, "cwd": request.workspace_path}),),
                checkpoint="demo-awaiting-human",
                continuation_ref=f"demo:{request.task_id}",
                metadata={
                    "human_decision": {
                        "requesting_capability": "demo-human",
                        "reason": "示例操作需要人工确认",
                        "question": "是否允许继续执行示例外部操作？",
                        "allowed_actions": ["APPROVE", "REJECT", "MODIFY"],
                        "context": {"operation": "demo-publish"},
                    }
                },
            )
        payload = decisions[-1].payload or {}
        action = str(payload.get("action", "APPROVE"))
        instruction = payload.get("instruction")
        if action == "MODIFY":
            text = f"已按修改后的要求继续：{instruction}"
        elif action == "REJECT":
            text = "请求被拒绝；示例任务以只读方式收尾。"
        else:
            text = "已获批准；示例任务完成。"
        return ExecutionResult(
            invocation_id=request.invocation_id,
            status=ExecutionStatus.COMPLETED,
            artifacts=(Artifact(kind="assistant-output", producer_capability="demo-human",
                                payload={"text": text}),),
            metadata={"test_capability": True},
        )


class _TrackedCapability:
    def __init__(self, spec: CapabilitySpec, gateway: "_ExecutionGateway") -> None:
        self._spec = spec
        self._gateway = gateway

    def execute(self, request: ExecutionRequest):
        return self._gateway.run(self._spec, request)


class _ExecutionGateway:
    def __init__(self, specs: Mapping[str, CapabilitySpec]) -> None:
        self._specs = dict(specs)
        self._lock = threading.RLock()
        self._entries: dict[str, tuple[RuntimeRecord, CapabilitySpec]] = {}
        self._by_task: dict[str, list[str]] = {}

    def tracked(self, spec: CapabilitySpec) -> _TrackedCapability:
        return _TrackedCapability(spec, self)

    def run(self, spec: CapabilitySpec, request: ExecutionRequest):
        execution_id = str(uuid.uuid4())
        record = RuntimeRecord(
            execution_id=execution_id,
            task_id=request.task_id,
            invocation_id=request.invocation_id,
            capability_id=spec.capability_id,
            status=RuntimeStatus.RUNNING,
            started_at=_now(),
            updated_at=_now(),
        )
        with self._lock:
            self._entries[execution_id] = (record, spec)
            self._by_task.setdefault(request.task_id, []).append(execution_id)

        bound = replace(
            request,
            metadata={**dict(request.metadata), "execution_id": execution_id},
        )
        outcome: Any = None
        try:
            outcome = spec.capability.execute(bound)
        except BaseException:
            self._finish(execution_id, RuntimeStatus.FAILED, "adapter_exception")
            raise
        if isinstance(outcome, ExecutionResult):
            self._finish(execution_id, RuntimeStatus.SUCCEEDED, f"result:{outcome.status.value}")
        elif isinstance(outcome, ExecutionFailure):
            if outcome.category == "TERMINATED_BY_REQUEST":
                self._finish(execution_id, RuntimeStatus.STOPPED, "cancelled")
            else:
                self._finish(execution_id, RuntimeStatus.FAILED, f"failure:{outcome.category}")
        else:
            self._finish(execution_id, RuntimeStatus.FAILED, "invalid_outcome")
        return outcome

    def _finish(self, execution_id: str, status: RuntimeStatus, reason: str) -> None:
        with self._lock:
            entry = self._entries.get(execution_id)
            if entry is None:
                return
            record, spec = entry
            self._entries[execution_id] = (
                replace(record, status=status, updated_at=_now(), ended_at=_now(), reason=reason),
                spec,
            )

    def records_for_task(self, task_id: str) -> list[RuntimeRecord]:
        with self._lock:
            ids = list(self._by_task.get(task_id, []))
            return [self._entries[item][0] for item in ids if item in self._entries]

    def has_active(self, task_id: str) -> bool:
        return any(
            record.status in {RuntimeStatus.STARTING, RuntimeStatus.RUNNING, RuntimeStatus.STOPPING}
            for record in self.records_for_task(task_id)
        )

    def cancel_task(self, task_id: str) -> bool:
        active = [
            (execution_id, *self._entries[execution_id])
            for execution_id in self._by_task.get(task_id, [])
            if execution_id in self._entries
            and self._entries[execution_id][0].status in {
                RuntimeStatus.STARTING, RuntimeStatus.RUNNING, RuntimeStatus.STOPPING
            }
        ]
        if not active:
            return False
        execution_id, record, spec = active[0]
        with self._lock:
            self._entries[execution_id] = (
                replace(record, status=RuntimeStatus.STOPPING, updated_at=_now(), reason="cancelling"),
                spec,
            )
        terminate = getattr(spec.capability, "terminate", None)
        if callable(terminate):
            try:
                terminate(execution_id)
            except Exception:
                pass
        return True


class ControlConsole:
    def __init__(
        self,
        *,
        data_dir: str | Path,
        guard: WorkspaceGuard,
        specs: Sequence[CapabilitySpec],
        health: ProviderHealthTracker | None = None,
    ) -> None:
        self._store = JSONTaskStateStore(Path(data_dir) / "tasks")
        self._guard = guard
        self._specs = {spec.capability_id: spec for spec in specs}
        self._health = health or ProviderHealthTracker()
        self._human = HumanGate(self._store)
        self._gateway = _ExecutionGateway(self._specs)
        self._controller = TaskController(
            store=self._store,
            router=ThinRouter(),
            health=self._health,
            capabilities={cid: self._gateway.tracked(spec) for cid, spec in self._specs.items()},
            human_gate=self._human,
            workspace_guard=self._guard,
        )
        self._observer = Observer(
            self._store,
            runtime=self._gateway,
            human=self._human,
            health=self._health,
            workspace_guard=self._guard,
        )
        self._lock = threading.RLock()
        self._running: set[str] = set()
        self._meta: dict[str, str] = {}
        for spec in self._specs.values():
            if self._health.get(spec.provider) is None:
                self._health.record_success(spec.provider)

    # -------------------------------------------------------------- workspace

    def verify_workspace(
        self,
        path: str,
        *,
        branch: str | None = None,
        tracking_ref: str | None = None,
        baseline_commit: str | None = None,
        mode: str = "read_only",
    ) -> tuple[WorkspaceBinding | None, WorkspaceVerification]:
        probe = WorkspaceBinding(
            root_path=str(Path(path).expanduser()),
            worktree_path=None,
            branch=None,
            baseline_commit=None,
            tracking_ref=None,
            policy=WorkspacePolicy(read_only=mode == "read_only", require_tracking_ref=False),
        )
        probe_result = self._guard.verify(probe)
        actual = dict(probe_result.actual)
        if not actual.get("worktree_path"):
            return None, probe_result
        selected_tracking = tracking_ref if tracking_ref is not None else actual.get("upstream_ref")
        binding = WorkspaceBinding(
            workspace_id=str(uuid.uuid4()),
            root_path=str(Path(path).expanduser()),
            repository_root=actual.get("repository_root"),
            worktree_path=actual.get("worktree_path"),
            branch=branch or actual.get("branch"),
            baseline_commit=baseline_commit or actual.get("head"),
            tracking_ref=selected_tracking,
            policy=WorkspacePolicy(
                read_only=mode == "read_only",
                require_tracking_ref=bool(selected_tracking),
            ),
        )
        return binding, self._guard.verify(binding)

    # --------------------------------------------------------------- tasks

    def capabilities(self) -> list[dict[str, Any]]:
        return [
            {
                "capability_id": spec.capability_id,
                "label": spec.label,
                "provider": spec.provider,
                "model": spec.model,
                "modes": list(spec.modes),
            }
            for spec in self._specs.values()
        ]

    def create_task(
        self,
        *,
        objective: str,
        binding_payload: Mapping[str, Any],
        mode: str,
        capability_id: str,
    ) -> str:
        if not objective.strip():
            raise ValueError("objective is required")
        if mode not in {"read_only", "mutating"}:
            raise ValueError("mode must be read_only or mutating")
        spec = self._specs.get(capability_id)
        if spec is None:
            raise ValueError("unknown capability")
        if mode not in spec.modes:
            raise ValueError("capability does not support this mode")

        binding = WorkspaceBinding.from_payload(binding_payload)
        binding = replace(
            binding,
            policy=WorkspacePolicy(
                read_only=mode == "read_only",
                require_tracking_ref=binding.policy.require_tracking_ref,
            ),
        )
        verification = self._guard.verify(binding)
        if not verification.execution_allowed:
            raise WorkspaceBlocked(verification)

        task = self._store.create_task(objective=objective)
        task = self._store.set_lifecycle(
            task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision
        )
        bind_workspace(
            self._store, task.task_id, binding,
            expected_revision=task.revision, actor="console",
        )
        with self._lock:
            self._meta[task.task_id] = capability_id
        self._start_advance(task.task_id)
        return task.task_id

    def _spec_for(self, task: TaskState) -> CapabilitySpec | None:
        capability_id = self._meta.get(task.task_id)
        if capability_id is None and task.active_capability:
            capability_id = task.active_capability.capability_id
        return self._specs.get(capability_id or "")

    def _start_advance(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._running:
                return
            self._running.add(task_id)
        thread = threading.Thread(target=self._advance, args=(task_id,), daemon=True)
        thread.start()

    def _advance(self, task_id: str) -> None:
        try:
            task = self._store.get_task(task_id)
            spec = self._spec_for(task)
            if spec is None:
                return
            candidates = [
                CapabilityCandidate(
                    spec.capability_id,
                    "execution",
                    provider=spec.provider,
                    preference_rank=10,
                    supports_task_state_handoff=True,
                )
            ]
            self._controller.advance(task_id, candidates=candidates)
        except Exception:
            pass
        finally:
            with self._lock:
                self._running.discard(task_id)

    def continue_task(self, task_id: str) -> None:
        self._store.get_task(task_id)
        self._start_advance(task_id)

    def cancel_task(self, task_id: str) -> bool:
        return self._gateway.cancel_task(task_id)

    def decide(self, task_id: str, action: str, instruction: str | None = None) -> dict[str, Any]:
        task = self._store.get_task(task_id)
        pending = self._human.pending(task_id)
        if pending is None:
            raise ValueError("no pending human decision")
        decision = self._human.resolve(
            task_id,
            pending.request_id,
            action=HumanAction(str(action).upper()),
            instruction=instruction,
            expected_revision=task.revision,
        )
        self._start_advance(task_id)
        return decision.to_dict()

    # --------------------------------------------------------------- state

    def state(self, task_id: str) -> dict[str, Any]:
        task = self._store.get_task(task_id)
        observation = self._observer.observe(task_id)
        profiles = {spec.capability_id: RecoveryProfile(False, True) for spec in self._specs.values()}
        reconciliation = Reconciler(self._observer).reconcile(task_id, profiles=profiles)
        timeline = [entry.to_dict() for entry in self._observer.timeline(task_id)]
        spec = self._spec_for(task)
        binding = binding_for(task)
        result = self._result_text(task)
        active_runtime = any(runtime.live for runtime in observation.runtimes)
        return {
            "task_id": task_id,
            "task_status": task.lifecycle_status.value,
            "status_label": self._status_label(task, observation, reconciliation),
            "observation": observation.to_dict(),
            "reconciliation": reconciliation.to_dict(),
            "timeline": timeline,
            "result": result,
            "provider": spec.provider if spec else None,
            "model": spec.model if spec else None,
            "capability_id": spec.capability_id if spec else None,
            "capability_label": spec.label if spec else None,
            "mode": "read_only" if binding and binding.policy.read_only else "mutating",
            "workspace_binding": binding.to_payload() if binding else None,
            "pending_human": observation.pending_human.to_dict() if observation.pending_human else None,
            "can_cancel": active_runtime,
            "can_continue": (
                task.lifecycle_status is LifecycleStatus.RUNNING
                and observation.pending_human is None
                and not active_runtime
            ),
        }

    def timeline(self, task_id: str) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self._observer.timeline(task_id)]

    @staticmethod
    def _result_text(task: TaskState) -> str | None:
        for artifact in reversed(task.artifacts):
            if artifact.kind not in {"assistant-output", "result"}:
                continue
            payload = artifact.payload
            if isinstance(payload, Mapping):
                text = payload.get("text") or payload.get("output")
                if isinstance(text, str) and text:
                    return text
            if isinstance(payload, str) and payload:
                return payload
        return None

    @staticmethod
    def _status_label(task: TaskState, observation, reconciliation) -> str:
        if observation.pending_human is not None:
            return "等待你的决定"
        if observation.workspace is not None and not observation.workspace.execution_allowed:
            return "工作区阻止执行"
        if task.lifecycle_status is LifecycleStatus.COMPLETED:
            return "已完成"
        if any(runtime.live for runtime in observation.runtimes):
            return "正在执行"
        if task.lifecycle_status is LifecycleStatus.FAILED:
            return "执行失败"
        if reconciliation.status in {
            RecoveryStatus.ORPHANED,
            RecoveryStatus.UNCERTAIN,
            RecoveryStatus.LOST,
            RecoveryStatus.WORKSPACE_RECONCILIATION_REQUIRED,
        }:
            return "无法确认执行结果"
        if task.lifecycle_status is LifecycleStatus.BLOCKED:
            return "需要处理"
        if task.lifecycle_status is LifecycleStatus.RUNNING:
            return "准备执行"
        return task.lifecycle_status.value


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_default_console(data_dir: str | Path | None = None) -> ControlConsole:
    import os

    from adapters.dsh_execution import DSHExecutionAdapter

    provider = os.environ.get("ACP_CONSOLE_PROVIDER", "deepseek-official")
    model = os.environ.get("ACP_CONSOLE_MODEL", "deepseek-chat")
    guard = GitWorkspaceGuard()
    data = Path(data_dir or os.environ.get("ACP_CONSOLE_DATA", ".console-data"))
    specs: list[CapabilitySpec] = [
        CapabilitySpec(
            "dsh-readonly",
            f"DSH · {provider}/{model} · 只读",
            provider,
            model,
            DSHExecutionAdapter(
                provider=provider, model=model,
                cwd=str(Path.cwd()),
                env_extra={"DSH_PERMISSION_MODE": "read-only"},
            ),
            modes=("read_only",),
        ),
        CapabilitySpec(
            "dsh-mutating",
            f"DSH · {provider}/{model} · 可修改",
            provider,
            model,
            DSHExecutionAdapter(
                provider=provider, model=model,
                cwd=str(Path.cwd()),
                env_extra={"DSH_PERMISSION_MODE": "workspace-write"},
            ),
            modes=("mutating",),
        ),
        CapabilitySpec(
            "demo-human",
            "示例 · Human Gate 验证",
            "local",
            "demo",
            HumanGateDemoCapability(),
            modes=("read_only", "mutating"),
        ),
    ]
    return ControlConsole(data_dir=data, guard=guard, specs=specs)
