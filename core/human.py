"""Human Decision Gate — an insertable control capability.

A capability may ask for one human decision at a safe boundary. The gate is
represented with the existing Task State insertion stack: the previous cursor
and capability are saved in the frame, the Task becomes BLOCKED, and the
request plus its resolution are durable Task artifacts.

Human decisions are authoritative task constraints: APPROVE, REJECT, MODIFY.
MODIFY is never silently turned into APPROVE. There is no timeout, no
automatic approval, and no execution here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from .redaction import redact, redact_data
from .task import Artifact, CapabilityActivity, InsertionFrame, LifecycleStatus, TaskState
from .task_store import InvalidTaskState, RevisionConflict, TaskNotFound, TaskStateStore

__all__ = [
    "AlreadyPending",
    "AlreadyResolved",
    "HumanAction",
    "HumanDecision",
    "HumanDecisionRequest",
    "HumanGate",
    "HumanGateConflict",
    "HumanGateError",
    "InvalidAction",
    "ModificationRequired",
    "RequestNotFound",
    "StaleRequest",
    "WrongTask",
]

HUMAN_GATE_CAPABILITY_ID = "human-gate"
HUMAN_GATE_KIND = "human"
REQUEST_ARTIFACT_KIND = "human-decision-request"
DECISION_ARTIFACT_KIND = "human-decision"


class HumanAction(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    MODIFY = "MODIFY"


class HumanGateError(RuntimeError):
    code = "HUMAN_GATE_ERROR"


class RequestNotFound(HumanGateError):
    code = "NOT_FOUND"


class AlreadyResolved(HumanGateError):
    code = "ALREADY_RESOLVED"


class StaleRequest(HumanGateError):
    code = "STALE_REQUEST"


class WrongTask(HumanGateError):
    code = "WRONG_TASK"


class AlreadyPending(HumanGateError):
    code = "ALREADY_PENDING"


class InvalidAction(HumanGateError):
    code = "INVALID_ACTION"


class ModificationRequired(HumanGateError):
    code = "MODIFICATION_REQUIRED"


class HumanGateConflict(HumanGateError):
    code = "CONFLICT"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class HumanDecisionRequest:
    request_id: str
    task_id: str
    requesting_capability: str
    reason: str
    question: str
    allowed_actions: tuple[HumanAction, ...]
    context: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": redact(self.task_id),
            "requesting_capability": redact(self.requesting_capability),
            "reason": redact(self.reason),
            "question": redact(self.question),
            "allowed_actions": [action.value for action in self.allowed_actions],
            "context": redact_data(dict(self.context)),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class HumanDecision:
    request_id: str
    task_id: str
    action: HumanAction
    instruction: str | None = None
    decided_by: str = "human"
    decided_at: str = field(default_factory=_utcnow)
    task_revision: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": redact(self.task_id),
            "action": self.action.value,
            "instruction": redact(self.instruction) if self.instruction else None,
            "decided_by": redact(self.decided_by),
            "decided_at": self.decided_at,
            "task_revision": self.task_revision,
        }


class HumanGate:
    """Durable, insertable approval capability over an existing Task Store."""

    def __init__(self, store: TaskStateStore, *, actor: str = "human-gate") -> None:
        self._store = store
        self._actor = actor

    # -------------------------------------------------------------- request

    def request(
        self,
        task_id: str,
        *,
        requesting_capability: str,
        reason: str,
        question: str,
        allowed_actions: Sequence[HumanAction | str] = (HumanAction.APPROVE, HumanAction.REJECT, HumanAction.MODIFY),
        context: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> HumanDecisionRequest:
        task = self._store.get_task(task_id)
        if self.pending(task_id) is not None:
            raise AlreadyPending("task already has an active human decision request")
        if task.lifecycle_status is not LifecycleStatus.RUNNING:
            raise HumanGateError(
                f"human gate requires RUNNING, current {task.lifecycle_status.value}"
            )
        if expected_revision is not None and expected_revision != task.revision:
            raise HumanGateConflict(
                f"revision conflict: expected {expected_revision}, current {task.revision}"
            )

        actions = tuple(_coerce_action(item) for item in allowed_actions)
        if not actions:
            raise InvalidAction("allowed_actions must not be empty")

        request = HumanDecisionRequest(
            request_id=str(uuid.uuid4()),
            task_id=task_id,
            requesting_capability=redact(requesting_capability),
            reason=redact(reason),
            question=redact(question),
            allowed_actions=actions,
            context=redact_data(dict(context or {})),
        )
        artifact = Artifact(
            kind=REQUEST_ARTIFACT_KIND,
            producer_capability=HUMAN_GATE_CAPABILITY_ID,
            payload={
                "request_id": request.request_id,
                "task_id": request.task_id,
                "requesting_capability": request.requesting_capability,
                "reason": request.reason,
                "question": request.question,
                "allowed_actions": [action.value for action in actions],
                "context": dict(request.context),
                "created_at": request.created_at,
            },
        )
        frame = InsertionFrame(
            capability=CapabilityActivity(HUMAN_GATE_CAPABILITY_ID, HUMAN_GATE_KIND),
            reason=request.request_id,
            input={
                "request_id": request.request_id,
                "question": request.question,
                "allowed_actions": [action.value for action in actions],
            },
        )
        try:
            self._store.open_human_gate(
                task_id,
                expected_revision=task.revision,
                frame=frame,
                request_artifact=artifact,
                actor=self._actor,
            )
        except RevisionConflict as exc:
            raise HumanGateConflict(str(exc)) from exc
        return request

    # -------------------------------------------------------------- queries

    def pending(self, task_id: str) -> HumanDecisionRequest | None:
        task = self._store.get_task(task_id)
        frame = self._active_frame(task)
        if frame is None:
            return None
        request_id = str((frame.input or {}).get("request_id", ""))
        request = self._find_request(task, request_id)
        if request is None or self._find_decision(task, request_id) is not None:
            return None
        return request

    def get_request(self, task_id: str, request_id: str) -> HumanDecisionRequest | None:
        return self._find_request(self._store.get_task(task_id), request_id)

    def history(self, task_id: str) -> tuple[HumanDecision, ...]:
        task = self._store.get_task(task_id)
        decisions = [
            parsed for artifact in task.artifacts
            if artifact.kind == DECISION_ARTIFACT_KIND
            for parsed in [self._parse_decision(artifact)]
            if parsed is not None
        ]
        return tuple(decisions)

    # -------------------------------------------------------------- resolve

    def resolve(
        self,
        task_id: str,
        request_id: str,
        *,
        action: HumanAction | str,
        instruction: str | None = None,
        expected_revision: int | None = None,
        decided_by: str = "human",
    ) -> HumanDecision:
        task = self._store.get_task(task_id)
        request = self._find_request(task, request_id)
        if request is None:
            if self._request_in_other_task(task_id, request_id):
                raise WrongTask(f"request {request_id} belongs to another task")
            raise RequestNotFound(f"unknown human decision request: {request_id}")
        if request.task_id != task_id:
            raise WrongTask("request belongs to a different task")
        if self._find_decision(task, request_id) is not None:
            raise AlreadyResolved(f"human decision already resolved: {request_id}")

        frame = self._active_frame(task)
        if (
            frame is None
            or str((frame.input or {}).get("request_id", "")) != request_id
            or task.lifecycle_status is not LifecycleStatus.BLOCKED
        ):
            raise StaleRequest(f"human decision request is not the active gate: {request_id}")

        chosen = _coerce_action(action)
        if chosen not in request.allowed_actions:
            raise InvalidAction(f"{chosen.value} is not allowed for this request")
        modification = redact(instruction).strip() if instruction else ""
        if chosen is HumanAction.MODIFY and not modification:
            raise ModificationRequired("MODIFY requires an instruction")

        now = _utcnow()
        artifact = Artifact(
            kind=DECISION_ARTIFACT_KIND,
            producer_capability=HUMAN_GATE_CAPABILITY_ID,
            payload={
                "request_id": request_id,
                "task_id": task_id,
                "action": chosen.value,
                "instruction": modification or None,
                "decided_by": redact(decided_by),
                "decided_at": now,
                "authoritative": True,
            },
        )
        try:
            updated = self._store.resolve_human_gate(
                task_id,
                expected_revision=task.revision if expected_revision is None else expected_revision,
                request_id=request_id,
                decision_artifact=artifact,
                decision=chosen.value,
                actor=self._actor,
            )
        except RevisionConflict as exc:
            raise HumanGateConflict(str(exc)) from exc
        except HumanGateError:
            raise
        except InvalidTaskState as exc:
            raise StaleRequest(f"human gate is no longer active: {exc}") from exc
        except Exception as exc:  # storage failures are not silently approvable
            raise HumanGateError("human gate storage failure") from exc

        return HumanDecision(
            request_id=request_id,
            task_id=task_id,
            action=chosen,
            instruction=modification or None,
            decided_by=redact(decided_by),
            decided_at=now,
            task_revision=updated.revision,
        )

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _active_frame(task: TaskState) -> InsertionFrame | None:
        if not task.insertions:
            return None
        frame = task.insertions[-1]
        if frame.capability.capability_id != HUMAN_GATE_CAPABILITY_ID:
            return None
        return frame

    @staticmethod
    def _find_request(task: TaskState, request_id: str) -> HumanDecisionRequest | None:
        for artifact in task.artifacts:
            if artifact.kind != REQUEST_ARTIFACT_KIND:
                continue
            payload = artifact.payload if isinstance(artifact.payload, Mapping) else {}
            if payload.get("request_id") == request_id:
                return HumanGate._parse_request(payload)
        return None

    @staticmethod
    def _find_decision(task: TaskState, request_id: str) -> HumanDecision | None:
        for artifact in task.artifacts:
            if artifact.kind != DECISION_ARTIFACT_KIND:
                continue
            payload = artifact.payload if isinstance(artifact.payload, Mapping) else {}
            if payload.get("request_id") == request_id:
                return HumanGate._parse_decision(artifact)
        return None

    def _request_in_other_task(self, task_id: str, request_id: str) -> bool:
        for other in self._store.list_tasks():
            if other == task_id:
                continue
            try:
                task = self._store.get_task(other)
            except TaskNotFound:
                continue
            if self._find_request(task, request_id) is not None:
                return True
        return False

    @staticmethod
    def _parse_request(payload: Mapping[str, Any]) -> HumanDecisionRequest | None:
        try:
            actions = tuple(_coerce_action(item) for item in payload.get("allowed_actions") or ())
            return HumanDecisionRequest(
                request_id=str(payload["request_id"]),
                task_id=str(payload["task_id"]),
                requesting_capability=str(payload.get("requesting_capability") or ""),
                reason=str(payload.get("reason") or ""),
                question=str(payload.get("question") or ""),
                allowed_actions=actions,
                context=dict(payload.get("context") or {}),
                created_at=str(payload.get("created_at") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _parse_decision(artifact: Artifact) -> HumanDecision | None:
        payload = artifact.payload if isinstance(artifact.payload, Mapping) else {}
        try:
            return HumanDecision(
                request_id=str(payload["request_id"]),
                task_id=str(payload["task_id"]),
                action=_coerce_action(payload["action"]),
                instruction=payload.get("instruction"),
                decided_by=str(payload.get("decided_by") or "human"),
                decided_at=str(payload.get("decided_at") or ""),
                task_revision=None,
            )
        except (KeyError, TypeError, ValueError):
            return None


def _coerce_action(value: HumanAction | str) -> HumanAction:
    if isinstance(value, HumanAction):
        return value
    try:
        return HumanAction(str(value).strip().upper())
    except ValueError as exc:
        raise InvalidAction(f"unsupported human action: {value!r}") from exc
