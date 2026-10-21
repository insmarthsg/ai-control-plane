"""Bounded Task Controller: one fact-based step per call.

The controller is a composition boundary, not an orchestrator:

    load task -> route -> invoke ONE execution capability -> persist -> return

It never loops to completion, retries, schedules, or decides multi-step plans.
The caller decides whether to call ``advance`` again. This is what keeps future
pause/cancel/human-review insertion possible without rewriting the core.

Boundaries:
- Router decides; controller does not select outside the router.
- Execution adapters execute; controller does not parse provider errors.
- Provider health is updated only through the injected health port and only
  for normalized ``PROVIDER`` failures.
- Task State is written only through the store port.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from .capability import CapabilityCandidate
from .errors import ProviderError
from .execution import (
    AccessMode,
    ExecutionCapability,
    ExecutionFailure,
    ExecutionFailureKind,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
)
from .provider_health import HealthRecord
from .router import (
    ContinuationMode,
    NoRoute,
    RoutingDecision,
    RoutingRequest,
    RoutingResult,
    UserConstraints,
)
from .task import (
    CapabilityActivity,
    ExecutionCursor,
    LifecycleStatus,
    TERMINAL_STATUSES,
    TaskState,
)
from .task_store import (
    RevisionConflict,
    TaskNotFound,
    TaskStateStore,
    TaskStoreError,
)
from .workspace import (
    WorkspaceAction,
    WorkspaceBinding,
    WorkspaceGuard,
    WorkspaceReason,
    WorkspaceStatus,
    WorkspaceVerification,
    binding_for,
    record_verification,
)

__all__ = [
    "AdvanceOutcome",
    "AdvanceStatus",
    "HumanGatePort",
    "ProviderHealthPort",
    "TaskController",
]


class AdvanceStatus(str, Enum):
    PROGRESSED = "PROGRESSED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    NO_ROUTE = "NO_ROUTE"
    FAILED = "FAILED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    WORKSPACE_BLOCKED = "WORKSPACE_BLOCKED"
    CONFLICT = "CONFLICT"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


@dataclass(frozen=True)
class AdvanceOutcome:
    task_id: str
    status: AdvanceStatus
    task_status: LifecycleStatus
    task_revision: int
    invocation_id: str
    selected_capability: str | None = None
    mode: ExecutionMode | None = None
    routing_decision: RoutingResult | None = None
    execution: ExecutionResult | ExecutionFailure | None = None
    message: str = ""
    next_condition: str = "none"


class ProviderHealthPort(Protocol):
    """Read/update seam for provider health; no raw parsing in the controller."""

    def all(self) -> Mapping[str, HealthRecord]: ...

    def record_error(self, error: ProviderError) -> Any: ...

    def record_success(self, provider: str) -> Any: ...


class HumanGatePort(Protocol):
    """Insertable human-decision capability; the controller never executes it."""

    def request(
        self,
        task_id: str,
        *,
        requesting_capability: str,
        reason: str,
        question: str,
        allowed_actions: Sequence[str],
        context: Mapping[str, Any],
        expected_revision: int,
    ) -> Any: ...


class _RouterPort(Protocol):
    def route(self, request: RoutingRequest) -> RoutingResult: ...


class TaskController:
    """Coordinates one bounded execution step for a task."""

    def __init__(
        self,
        *,
        store: TaskStateStore,
        router: _RouterPort,
        health: ProviderHealthPort,
        capabilities: Mapping[str, ExecutionCapability],
        human_gate: HumanGatePort | None = None,
        workspace_guard: WorkspaceGuard | None = None,
        actor: str = "task-controller",
    ) -> None:
        self._store = store
        self._router = router
        self._health = health
        self._capabilities = dict(capabilities)
        self._human_gate = human_gate
        self._workspace_guard = workspace_guard
        self._actor = actor

    def advance(
        self,
        task_id: str,
        *,
        candidates: Sequence[CapabilityCandidate],
        constraints: UserConstraints | None = None,
        kind: str = "execution",
    ) -> AdvanceOutcome:
        invocation_id = str(uuid.uuid4())
        constraints = constraints or UserConstraints()
        task = self._store.get_task(task_id)

        if task.insertions:
            return self._outcome(
                task,
                AdvanceStatus.BLOCKED,
                invocation_id,
                message="task has an active insertion frame; resolve it before advancing",
                next_condition="blocked",
            )
        if task.lifecycle_status in TERMINAL_STATUSES:
            status = (
                AdvanceStatus.COMPLETED
                if task.lifecycle_status is LifecycleStatus.COMPLETED
                else AdvanceStatus.BLOCKED
            )
            return self._outcome(
                task,
                status,
                invocation_id,
                message=f"task is terminal: {task.lifecycle_status.value}",
                next_condition="terminal",
            )
        if task.lifecycle_status not in {LifecycleStatus.CREATED, LifecycleStatus.RUNNING}:
            return self._outcome(
                task,
                AdvanceStatus.BLOCKED,
                invocation_id,
                message=f"task is not runnable: {task.lifecycle_status.value}",
                next_condition="blocked",
            )

        if task.lifecycle_status is LifecycleStatus.CREATED:
            try:
                task = self._store.set_lifecycle(
                    task_id,
                    LifecycleStatus.RUNNING,
                    expected_revision=task.revision,
                    actor=self._actor,
                )
            except RevisionConflict:
                return self._conflict(task, invocation_id, "task changed while starting")
            except TaskStoreError as exc:
                return self._persistence_failure(task, invocation_id, exc)

        decision = self._select(task, candidates, constraints, kind)

        try:
            task = self._store.record_event(
                task_id,
                "ROUTING_DECIDED",
                expected_revision=task.revision,
                data=decision.to_dict(),
                actor=self._actor,
            )
        except RevisionConflict:
            return self._conflict(task, invocation_id, "task changed during routing", decision=decision)
        except TaskStoreError as exc:
            return self._persistence_failure(task, invocation_id, exc, decision=decision)

        if isinstance(decision, NoRoute):
            try:
                task = self._store.set_lifecycle(
                    task_id,
                    LifecycleStatus.BLOCKED,
                    expected_revision=task.revision,
                    actor=self._actor,
                )
            except RevisionConflict:
                return self._conflict(task, invocation_id, "task changed while blocking", decision=decision)
            except TaskStoreError as exc:
                return self._persistence_failure(task, invocation_id, exc, decision=decision)
            return self._outcome(
                task,
                AdvanceStatus.NO_ROUTE,
                invocation_id,
                routing_decision=decision,
                message="no eligible execution capability",
                next_condition="blocked",
            )

        selected = next(
            (candidate for candidate in candidates if candidate.capability_id == decision.selected_capability_id),
            None,
        )
        capability = self._capabilities.get(decision.selected_capability_id)
        if selected is None or capability is None:
            try:
                task = self._store.record_event(
                    task_id,
                    "CAPABILITY_UNRESOLVED",
                    expected_revision=task.revision,
                    data={"capability_id": decision.selected_capability_id},
                    actor=self._actor,
                )
                task = self._store.set_lifecycle(
                    task_id,
                    LifecycleStatus.BLOCKED,
                    expected_revision=task.revision,
                    actor=self._actor,
                )
            except RevisionConflict:
                return self._conflict(task, invocation_id, "task changed while resolving capability")
            except TaskStoreError as exc:
                return self._persistence_failure(task, invocation_id, exc)
            return self._outcome(
                task,
                AdvanceStatus.BLOCKED,
                invocation_id,
                routing_decision=decision,
                message="selected capability implementation is not available",
                next_condition="blocked",
            )

        binding = binding_for(task)
        workspace_path = None
        access_mode = None
        if binding is not None:
            access_mode = (AccessMode.READ_ONLY if binding.policy.read_only
                           else AccessMode.WORKSPACE_WRITE)
            task, blocked, workspace_path = self._gate_workspace(
                task, binding, decision, invocation_id
            )
            if blocked is not None:
                return blocked

        request = self._build_request(
            task, selected, decision, constraints, invocation_id, workspace_path, access_mode
        )

        try:
            task = self._store.record_event(
                task_id,
                "EXECUTION_STARTED",
                expected_revision=task.revision,
                data={
                    "invocation_id": invocation_id,
                    "capability_id": selected.capability_id,
                    "mode": decision.mode.value,
                    "access_mode": access_mode.value if access_mode else None,
                    "workspace_id": binding.workspace_id if binding else None,
                },
                actor=self._actor,
            )
        except RevisionConflict:
            return self._conflict(task, invocation_id, "task changed before execution", decision=decision)
        except TaskStoreError as exc:
            return self._persistence_failure(task, invocation_id, exc, decision=decision)

        try:
            outcome: Any = capability.execute(request)
        except Exception as exc:  # adapter contract violation, never re-raised as provider failure
            outcome = ExecutionFailure(
                invocation_id=invocation_id,
                kind=ExecutionFailureKind.UNKNOWN,
                category="ADAPTER_EXCEPTION",
                message=f"execution adapter raised {type(exc).__name__}",
            )

        if isinstance(outcome, ExecutionFailure):
            return self._handle_failure(
                task, decision, request, outcome, selected, invocation_id
            )
        if not isinstance(outcome, ExecutionResult):
            outcome = ExecutionFailure(
                invocation_id=invocation_id,
                kind=ExecutionFailureKind.UNKNOWN,
                category="INVALID_RESULT",
                message="execution capability returned an unsupported result type",
            )
            return self._handle_failure(
                task, decision, request, outcome, selected, invocation_id
            )

        return self._handle_result(task, decision, request, outcome, selected, invocation_id)

    # ------------------------------------------------------------- selection

    def _select(
        self,
        task: TaskState,
        candidates: Sequence[CapabilityCandidate],
        constraints: UserConstraints,
        kind: str,
    ) -> RoutingResult:
        health = self._health_snapshot()
        common = {
            "task_id": task.task_id,
            "required_capability_kind": kind,
            "candidates": tuple(candidates),
            "constraints": constraints,
            "task_has_durable_state": True,
            "provider_health": health,
        }

        if task.cursor is not None and task.cursor.continuation_ref:
            direct = self._router.route(
                RoutingRequest(
                    **common,
                    continuation_mode=ContinuationMode.CONTINUE,
                    continuation_ref=task.cursor.continuation_ref,
                )
            )
            if isinstance(direct, RoutingDecision):
                return direct
            # Bounded second attempt: state handoff, never a loop.
            return self._router.route(
                RoutingRequest(**common, continuation_mode=ContinuationMode.HANDOFF)
            )

        return self._router.route(RoutingRequest(**common, continuation_mode=ContinuationMode.NONE))

    def _health_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        for provider, record in self._health.all().items():
            snapshot[provider] = record.to_dict() if hasattr(record, "to_dict") else record
        return snapshot

    # -------------------------------------------------------------- request

    def _build_request(
        self,
        task: TaskState,
        candidate: CapabilityCandidate,
        decision: RoutingDecision,
        constraints: UserConstraints,
        invocation_id: str,
        workspace_path: str | None = None,
        access_mode: AccessMode | None = None,
    ) -> ExecutionRequest:
        if decision.mode is ContinuationMode.CONTINUE:
            mode = ExecutionMode.CONTINUE
            continuation_ref = task.cursor.continuation_ref if task.cursor else None
            checkpoint = task.cursor.checkpoint if task.cursor else None
            handoff_reason = None
        elif decision.mode is ContinuationMode.HANDOFF:
            mode = ExecutionMode.HANDOFF
            continuation_ref = None
            checkpoint = task.cursor.checkpoint if task.cursor else None
            handoff_reason = "previous capability unavailable under current facts"
        else:
            mode = ExecutionMode.FRESH
            continuation_ref = None
            checkpoint = None
            handoff_reason = None

        return ExecutionRequest(
            invocation_id=invocation_id,
            task_id=task.task_id,
            objective=task.objective,
            durable_context=dict(task.durable_context),
            artifacts=tuple(task.artifacts),
            execution_mode=mode,
            continuation_ref=continuation_ref,
            checkpoint=checkpoint,
            workspace_path=workspace_path,
            access_mode=access_mode,
            constraints={
                "required": sorted(constraints.required),
                "denied": sorted(constraints.denied),
                "allowed": sorted(constraints.allowed) if constraints.allowed is not None else None,
            },
            handoff_reason=handoff_reason,
            metadata={"capability_id": candidate.capability_id, "capability_kind": candidate.kind},
        )

    # ------------------------------------------------------------ workspace

    def _gate_workspace(
        self,
        task: TaskState,
        binding: WorkspaceBinding,
        decision: RoutingDecision,
        invocation_id: str,
    ) -> tuple[TaskState, AdvanceOutcome | None, str | None]:
        if self._workspace_guard is None:
            verification = WorkspaceVerification(
                workspace_id=binding.workspace_id,
                status=WorkspaceStatus.BLOCKED,
                reason=WorkspaceReason.UNKNOWN,
                expected=binding.to_payload(),
                actual={"guard": "not_configured"},
                mismatches=("workspace_guard",),
                recommended_action=WorkspaceAction.MANUAL_REVIEW,
            )
            try:
                task = record_verification(
                    self._store, task.task_id, verification,
                    expected_revision=task.revision, actor=self._actor,
                )
            except RevisionConflict:
                return task, self._conflict(task, invocation_id,
                                            "task changed during workspace verification",
                                            decision=decision), None
            except TaskStoreError as exc:
                return task, self._persistence_failure(task, invocation_id, exc,
                                                       decision=decision), None
            return self._block_workspace(task, verification, decision, invocation_id)

        try:
            first = self._workspace_guard.verify(binding)
        except Exception:
            first = WorkspaceVerification(
                workspace_id=binding.workspace_id,
                status=WorkspaceStatus.BLOCKED,
                reason=WorkspaceReason.UNKNOWN,
                expected=binding.to_payload(),
                actual={"error": "guard_failed"},
                mismatches=("workspace_guard",),
                recommended_action=WorkspaceAction.MANUAL_REVIEW,
            )

        try:
            task = record_verification(
                self._store,
                task.task_id,
                first,
                expected_revision=task.revision,
                actor=self._actor,
            )
        except RevisionConflict:
            return task, self._conflict(task, invocation_id, "task changed during workspace verification",
                                        decision=decision), None
        except TaskStoreError as exc:
            return task, self._persistence_failure(task, invocation_id, exc, decision=decision), None

        if not first.execution_allowed:
            return self._block_workspace(task, first, decision, invocation_id)

        try:
            second = self._workspace_guard.verify(binding)
        except Exception:
            second = WorkspaceVerification(
                workspace_id=binding.workspace_id,
                status=WorkspaceStatus.BLOCKED,
                reason=WorkspaceReason.UNKNOWN,
                expected=binding.to_payload(),
                actual={"error": "guard_failed"},
                mismatches=("workspace_guard",),
                recommended_action=WorkspaceAction.MANUAL_REVIEW,
            )

        if self._workspace_signature(first) != self._workspace_signature(second):
            changed = WorkspaceVerification(
                workspace_id=second.workspace_id,
                status=WorkspaceStatus.BLOCKED,
                reason=WorkspaceReason.WORKSPACE_CHANGED_AFTER_VERIFY,
                expected=second.expected,
                actual=second.actual,
                mismatches=("workspace_changed",),
                warnings=second.warnings,
                recommended_action=WorkspaceAction.MANUAL_REVIEW,
            )
            try:
                task = record_verification(
                    self._store, task.task_id, changed,
                    expected_revision=task.revision, actor=self._actor,
                    changed_after_verify=True,
                )
            except RevisionConflict:
                return task, self._conflict(task, invocation_id, "task changed before execution",
                                            decision=decision), None
            except TaskStoreError as exc:
                return task, self._persistence_failure(task, invocation_id, exc, decision=decision), None
            return self._block_workspace(task, changed, decision, invocation_id)

        if not second.execution_allowed:
            try:
                task = record_verification(
                    self._store, task.task_id, second,
                    expected_revision=task.revision, actor=self._actor,
                )
            except RevisionConflict:
                return task, self._conflict(task, invocation_id,
                                            "task changed during workspace verification",
                                            decision=decision), None
            except TaskStoreError as exc:
                return task, self._persistence_failure(task, invocation_id, exc,
                                                       decision=decision), None
            return self._block_workspace(task, second, decision, invocation_id)

        cwd = (
            second.actual.get("worktree_path")
            or binding.worktree_path
            or binding.root_path
        )
        return task, None, str(cwd)

    def _block_workspace(
        self,
        task: TaskState,
        verification: WorkspaceVerification,
        decision: RoutingDecision,
        invocation_id: str,
    ) -> tuple[TaskState, AdvanceOutcome | None, str | None]:
        try:
            task = self._store.set_lifecycle(
                task.task_id,
                LifecycleStatus.BLOCKED,
                expected_revision=task.revision,
                actor=self._actor,
            )
        except RevisionConflict:
            return task, self._conflict(task, invocation_id, "task changed while blocking on workspace",
                                        decision=decision), None
        except TaskStoreError as exc:
            return task, self._persistence_failure(task, invocation_id, exc, decision=decision), None
        return task, self._outcome(
            task,
            AdvanceStatus.WORKSPACE_BLOCKED,
            invocation_id,
            routing_decision=decision,
            message=f"workspace blocked: {verification.reason.value}",
            next_condition="blocked",
        ), None

    @staticmethod
    def _workspace_signature(verification: WorkspaceVerification) -> tuple:
        actual = verification.actual
        return tuple(
            actual.get(key)
            for key in ("worktree_path", "repository_root", "branch", "head", "dirty", "tracking_head")
        )

    # -------------------------------------------------------------- results

    def _handle_result(
        self,
        task: TaskState,
        decision: RoutingDecision,
        request: ExecutionRequest,
        result: ExecutionResult,
        candidate: CapabilityCandidate,
        invocation_id: str,
    ) -> AdvanceOutcome:
        if not result.invocation_id:
            result = replace(result, invocation_id=invocation_id)
        provider = candidate.provider
        if provider:
            self._health.record_success(provider)

        cursor = self._cursor_from_result(result, decision, task)
        capability = CapabilityActivity(candidate.capability_id, candidate.kind)
        event_data = {
            "invocation_id": invocation_id,
            "capability_id": candidate.capability_id,
            "mode": decision.mode.value,
            "status": result.status.value,
            "checkpoint": result.checkpoint,
            "artifact_ids": [artifact.artifact_id for artifact in result.artifacts],
        }
        try:
            task = self._store.record_execution_result(
                task.task_id,
                expected_revision=task.revision,
                artifacts=result.artifacts,
                cursor=cursor,
                active_capability=capability,
                completed=result.is_completed,
                event_type="EXECUTION_RESULT",
                event_data=event_data,
                actor=self._actor,
            )
        except RevisionConflict:
            return self._conflict(
                task,
                invocation_id,
                "task changed during execution; result not persisted",
                decision=decision,
                execution=result,
            )
        except TaskStoreError as exc:
            return self._persistence_failure(
                task,
                invocation_id,
                exc,
                decision=decision,
                execution=result,
            )

        human_signal = (
            result.metadata.get("human_decision")
            if isinstance(result.metadata, Mapping)
            else None
        )
        if (
            isinstance(human_signal, Mapping)
            and not result.is_completed
            and self._human_gate is not None
        ):
            raw_actions = human_signal.get("allowed_actions")
            actions = (
                tuple(str(item) for item in raw_actions)
                if isinstance(raw_actions, (list, tuple)) and raw_actions
                else ("APPROVE", "REJECT", "MODIFY")
            )
            try:
                self._human_gate.request(
                    task.task_id,
                    requesting_capability=str(
                        human_signal.get("requesting_capability")
                        or candidate.capability_id
                    ),
                    reason=str(human_signal.get("reason") or ""),
                    question=str(human_signal.get("question") or ""),
                    allowed_actions=actions,
                    context=dict(human_signal.get("context") or {}),
                    expected_revision=task.revision,
                )
                task = self._store.get_task(task.task_id)
            except Exception as exc:  # gate is optional and must fail closed
                return self._outcome(
                    task,
                    AdvanceStatus.BLOCKED,
                    invocation_id,
                    selected_capability=candidate.capability_id,
                    mode=request.execution_mode,
                    routing_decision=decision,
                    execution=result,
                    message=f"human gate request failed: {type(exc).__name__}",
                    next_condition="reconcile",
                )
            return self._outcome(
                task,
                AdvanceStatus.HUMAN_REQUIRED,
                invocation_id,
                selected_capability=candidate.capability_id,
                mode=request.execution_mode,
                routing_decision=decision,
                execution=result,
                message="human decision required",
                next_condition="human",
            )

        status = AdvanceStatus.COMPLETED if result.is_completed else AdvanceStatus.PROGRESSED
        return self._outcome(
            task,
            status,
            invocation_id,
            selected_capability=candidate.capability_id,
            mode=request.execution_mode,
            routing_decision=decision,
            execution=result,
            message="execution completed" if result.is_completed else "execution progressed",
            next_condition="terminal" if result.is_completed else "advance-again",
        )

    def _handle_failure(
        self,
        task: TaskState,
        decision: RoutingDecision,
        request: ExecutionRequest,
        failure: ExecutionFailure,
        candidate: CapabilityCandidate,
        invocation_id: str,
    ) -> AdvanceOutcome:
        if not failure.invocation_id:
            failure = replace(failure, invocation_id=invocation_id)

        provider_error = failure.provider_error if failure.updates_provider_health else None
        if provider_error is not None:
            self._health.record_error(provider_error)

        cursor = self._cursor_from_failure(failure, decision, task)
        capability = CapabilityActivity(candidate.capability_id, candidate.kind)
        event_data = {
            "invocation_id": invocation_id,
            "capability_id": candidate.capability_id,
            "mode": decision.mode.value,
            "failure_kind": failure.kind.value,
            "category": failure.category,
            "retryable": failure.retryable,
            "provider": provider_error.provider if provider_error is not None else None,
            "checkpoint": failure.checkpoint,
            "artifact_ids": [artifact.artifact_id for artifact in failure.partial_artifacts],
        }
        try:
            task = self._store.record_execution_result(
                task.task_id,
                expected_revision=task.revision,
                artifacts=failure.partial_artifacts,
                cursor=cursor,
                active_capability=capability,
                completed=False,
                event_type="EXECUTION_FAILED",
                event_data=event_data,
                actor=self._actor,
            )
        except RevisionConflict:
            return self._conflict(
                task,
                invocation_id,
                "task changed during execution; failure not persisted",
                decision=decision,
                execution=failure,
            )
        except TaskStoreError as exc:
            return self._persistence_failure(
                task,
                invocation_id,
                exc,
                decision=decision,
                execution=failure,
            )

        if failure.kind is ExecutionFailureKind.PROVIDER:
            return self._outcome(
                task,
                AdvanceStatus.FAILED,
                invocation_id,
                selected_capability=candidate.capability_id,
                mode=request.execution_mode,
                routing_decision=decision,
                execution=failure,
                message="provider failure recorded; caller may advance again",
                next_condition="advance-again",
            )

        try:
            task = self._store.set_lifecycle(
                task.task_id,
                LifecycleStatus.BLOCKED,
                expected_revision=task.revision,
                actor=self._actor,
            )
        except RevisionConflict:
            return self._conflict(
                task,
                invocation_id,
                "task changed while blocking after execution failure",
                decision=decision,
                execution=failure,
            )
        except TaskStoreError as exc:
            return self._persistence_failure(
                task,
                invocation_id,
                exc,
                decision=decision,
                execution=failure,
            )

        return self._outcome(
            task,
            AdvanceStatus.FAILED,
            invocation_id,
            selected_capability=candidate.capability_id,
            mode=request.execution_mode,
            routing_decision=decision,
            execution=failure,
            message="execution failure recorded; provider health unchanged",
            next_condition="blocked",
        )

    # --------------------------------------------------------------- cursor

    @staticmethod
    def _cursor_from_result(
        result: ExecutionResult,
        decision: RoutingDecision,
        task: TaskState,
    ) -> ExecutionCursor | None:
        if result.checkpoint is None and result.continuation_ref is None:
            return None
        checkpoint = result.checkpoint or (task.cursor.checkpoint if task.cursor else "")
        return ExecutionCursor(
            capability_id=decision.selected_capability_id,
            checkpoint=checkpoint,
            continuation_ref=result.continuation_ref,
        )

    @staticmethod
    def _cursor_from_failure(
        failure: ExecutionFailure,
        decision: RoutingDecision,
        task: TaskState,
    ) -> ExecutionCursor | None:
        if failure.checkpoint is None and failure.continuation_ref is None:
            return None
        checkpoint = failure.checkpoint or (task.cursor.checkpoint if task.cursor else "")
        return ExecutionCursor(
            capability_id=decision.selected_capability_id,
            checkpoint=checkpoint,
            continuation_ref=failure.continuation_ref,
        )

    # -------------------------------------------------------------- outcomes

    def _outcome(
        self,
        task: TaskState,
        status: AdvanceStatus,
        invocation_id: str,
        *,
        selected_capability: str | None = None,
        mode: ExecutionMode | None = None,
        routing_decision: RoutingResult | None = None,
        execution: ExecutionResult | ExecutionFailure | None = None,
        message: str = "",
        next_condition: str = "none",
    ) -> AdvanceOutcome:
        return AdvanceOutcome(
            task_id=task.task_id,
            status=status,
            task_status=task.lifecycle_status,
            task_revision=task.revision,
            invocation_id=invocation_id,
            selected_capability=selected_capability,
            mode=mode,
            routing_decision=routing_decision,
            execution=execution,
            message=message,
            next_condition=next_condition,
        )

    def _conflict(
        self,
        task: TaskState,
        invocation_id: str,
        message: str,
        *,
        decision: RoutingResult | None = None,
        execution: ExecutionResult | ExecutionFailure | None = None,
    ) -> AdvanceOutcome:
        return self._outcome(
            task,
            AdvanceStatus.CONFLICT,
            invocation_id,
            routing_decision=decision,
            execution=execution,
            message=message,
            next_condition="reconcile",
        )

    def _persistence_failure(
        self,
        task: TaskState,
        invocation_id: str,
        error: Exception,
        *,
        decision: RoutingResult | None = None,
        execution: ExecutionResult | ExecutionFailure | None = None,
    ) -> AdvanceOutcome:
        return self._outcome(
            task,
            AdvanceStatus.PERSISTENCE_FAILED,
            invocation_id,
            routing_decision=decision,
            execution=execution,
            message=f"task persistence failed: {type(error).__name__}",
            next_condition="reconcile",
        )
