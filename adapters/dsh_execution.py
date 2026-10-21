"""DSH SDK Execution Adapter — the real ExecutionCapability over DSH.

Transport: one `dsh --profile sdk` stdio JSON-RPC runtime process per
execution (process-per-execution), so `terminate(execution_id)` can stop
exactly one execution without touching other tasks. The process model is an
adapter implementation detail, never a Stable Core concept.

Identity: DSH exposes no execution/turn id. The bridge mints an opaque UUID
`execution_id` and records the mapping to `invocation_id`, `session_id`,
`message_id`, runtime process identity, and status. `messageId` is a DSH queue
acknowledgement and is never renamed into a turn id.

Continuation: FRESH/HANDOFF create a new DSH session; the adapter returns an
opaque `dsh-session:<session_id>` continuation ref. CONTINUE parses that ref
and `session/resume`s it, including from a brand-new runtime process.

The core never sees JSON-RPC, session ids as structured data, or process
handles — only the existing Execution Contract.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from adapters.dsh import DSH_CODE_CATEGORIES
from core.errors import ErrorCategory, ProviderError, classify_provider_error
from core.execution import (
    AccessMode,
    ExecutionFailure,
    ExecutionFailureKind,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
)
from core.redaction import redact, redact_data
from core.task import Artifact

__all__ = ["DSHExecutionAdapter"]

CONTINUATION_PREFIX = "dsh-session:"
COMPLETION_MARKER = "ACP_EXECUTION_STATUS=COMPLETED"
DEFAULT_RUNTIME_CMD = ("dsh", "--profile", "sdk")
STDOUT_JOIN_TIMEOUT = 1.0


class _BridgeTimeout(Exception):
    pass


class _RuntimeClosed(Exception):
    pass


class _JsonRpcError(Exception):
    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _InvalidContinuation(Exception):
    pass


class _SdkRuntime:
    """One `dsh --profile sdk` child process speaking line-delimited JSON-RPC."""

    def __init__(self, cmd: list[str], *, cwd: str, env: dict[str, str],
                 on_notification) -> None:
        self.user_terminated = False
        self.process_group_cleared = False
        self.termination: str | None = None
        self._process_lock = threading.RLock()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        self.pgid = self._proc.pid
        self._on_notification = on_notification
        self._pending: dict[Any, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._request_counter = 0
        self.stderr_tail: deque[str] = deque(maxlen=20)
        self.stdout_reader_joined = False
        self._stdout_eof = False
        self._stdout_failed = False
        self._stdout_reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stdout_reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    @property
    def pid(self) -> int:
        return self._proc.pid

    def is_alive(self) -> bool:
        with self._process_lock:
            return self._proc.poll() is None

    def request(self, method: str, params: Mapping[str, Any], timeout: float) -> Any:
        if not self.is_alive():
            raise _RuntimeClosed("runtime process has exited")
        with self._lock:
            self._request_counter += 1
            request_id = f"req_{self._request_counter}"
            done = threading.Event()
            box: dict[str, Any] = {}
            self._pending[request_id] = {"done": done, "box": box}
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        try:
            with self._write_lock:
                assert self._proc.stdin is not None
                self._proc.stdin.write(json.dumps(frame) + "\n")
                self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise _RuntimeClosed(f"runtime stdin write failed: {exc}") from exc

        if not done.wait(max(timeout, 0.05)):
            with self._lock:
                self._pending.pop(request_id, None)
            raise _BridgeTimeout(method)
        with self._lock:
            self._pending.pop(request_id, None)
        if "error" in box:
            err = box["error"]
            raise _JsonRpcError(err.get("code"), str(err.get("message", "runtime error")))
        if "exception" in box:
            raise box["exception"]
        return box.get("result")

    def _settle(self, request_id: Any, payload: dict[str, Any]) -> None:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return
            pending["box"].update(payload)
            pending["done"].set()

    def _read_stdout(self) -> None:
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    frame = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                request_id = frame.get("id")
                method = frame.get("method")
                if request_id is not None and method is None:
                    err = frame.get("error")
                    if isinstance(err, dict):
                        self._settle(request_id, {"error": err})
                    else:
                        self._settle(request_id, {"result": frame.get("result")})
                elif isinstance(method, str):
                    params = frame.get("params") or {}
                    try:
                        self._on_notification(method, params if isinstance(params, dict) else {})
                    except Exception:
                        self._stdout_failed = True
            self._stdout_eof = True
        except Exception:
            self._stdout_failed = True
        finally:
            self._fail_pending(_RuntimeClosed("runtime stdout closed"))

    def _drain_stderr(self) -> None:
        try:
            assert self._proc.stderr is not None
            for line in self._proc.stderr:
                self.stderr_tail.append(line.rstrip()[:400])
        except Exception:
            pass

    def _fail_pending(self, exc: Exception) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item["box"]["exception"] = exc
            item["done"].set()

    def _group_exists(self) -> bool:
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        return True

    def terminate(self, *, requested: bool = True) -> bool:
        try:
            return self._terminate_group(requested=requested)
        finally:
            # Termination can itself emit session.event frames. Only EOF plus
            # a joined reader makes the collected safety evidence final. An
            # escaped descendant retaining stdout can prevent EOF: fail closed.
            self._stdout_reader.join(timeout=STDOUT_JOIN_TIMEOUT)
            self.stdout_reader_joined = not self._stdout_reader.is_alive()

    @property
    def stdout_drained(self) -> bool:
        return self.stdout_reader_joined and self._stdout_eof and not self._stdout_failed

    def _terminate_group(self, *, requested: bool) -> bool:
        # Serialize with poll: never reap the leader between the two signals.
        # Its unreaped identity pins the group against reuse during escalation.
        with self._process_lock:
            if requested:
                self.user_terminated = True
            if self.process_group_cleared:
                return True
            try:
                if self._proc.poll() is not None:
                    # Already reaped/dead: a saved numeric group is not authority
                    # to signal. Report surviving members without killing them.
                    self.process_group_cleared = not self._group_exists()
                    self.termination = "ALREADY_EXITED" if self.process_group_cleared else "ORPHANED"
                    return self.process_group_cleared

                try:
                    os.killpg(self.pgid, signal.SIGTERM)
                except OSError:
                    pass
                # Bounded grace, deliberately without poll()/wait() (which reap).
                time.sleep(0.25)
                try:
                    os.killpg(self.pgid, signal.SIGKILL)
                    self.termination = "SIGKILL"
                except OSError:
                    # Some hosts deny signals to a group containing only a
                    # zombie leader. Reap after the final signal attempt, then
                    # require ESRCH from the group probe; never infer clearance
                    # from the signal error itself.
                    self.termination = "SIGTERM"
                self._proc.wait(timeout=2.0)
                deadline = time.monotonic() + 2.0
                while self._group_exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.process_group_cleared = not self._group_exists()
                if not self.process_group_cleared:
                    self.termination = "KILL_UNCONFIRMED"
                return self.process_group_cleared
            except (OSError, subprocess.TimeoutExpired):
                self.termination = "KILL_UNCONFIRMED"
                return False
            finally:
                self._fail_pending(_RuntimeClosed("runtime terminated"))


@dataclass
class _ExecutionRecord:
    execution_id: str
    invocation_id: str
    task_id: str
    provider: str
    model: str
    mode: str
    session_id: str | None = None
    message_id: str | None = None
    runtime_pid: int | None = None
    pgid: int | None = None
    sandbox_mode: str | None = None
    sandbox_modes: list[Any] = field(default_factory=list)
    sandbox_consistent: bool = False
    stdout_reader_joined: bool = False
    stdout_drained: bool = False
    permission_preset: str | None = None
    process_group_cleared: bool = False
    termination: str | None = None
    started_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    status: str = "RUNNING"


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def add(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            self.events.append(dict(event))

    def counts(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for event in self.events:
                kind = str(event.get("type", "?"))
                counts[kind] = counts.get(kind, 0) + 1
            return counts

    def last_turn_end(self) -> dict[str, Any] | None:
        with self._lock:
            for event in reversed(self.events):
                if event.get("type") == "turn/end":
                    return (event.get("data") or {}).get("reason") or {}
        return None

    def sandbox_evidence(self) -> tuple[list[Any], Any, set[str]]:
        with self._lock:
            events = list(self.events)
        modes = []
        preset = None
        error_codes = set()
        for event in events:
            data = event.get("data") or {}
            if event.get("type") == "sandbox/mode":
                modes.append(data.get("mode"))
            elif event.get("type") == "permission/preset":
                preset = data.get("preset")
            elif event.get("type") == "tool/result":
                error = (data.get("result") or {}).get("error") or data.get("error") or {}
                error_codes.add(str(error.get("code", "")))
            elif event.get("type") == "turn/end":
                error = (data.get("reason") or {}).get("error") or {}
                error_codes.add(str(error.get("code", "")))
        return modes, preset, error_codes

    def assistant_summary(self) -> tuple[str, dict[str, Any] | None, str | None]:
        with self._lock:
            candidates = [e for e in self.events if e.get("type") == "assistant/message"]
        if not candidates:
            return "", None, None
        last = candidates[-1]
        data = last.get("data") or {}
        message = data.get("message") or {}
        text = "".join(
            block.get("text", "")
            for block in message.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        source = message.get("source") or {}
        replay = source.get("replayState") or {}
        response = replay.get("response") or {}
        response_id = response.get("responseId") if isinstance(response, dict) else None
        return text, usage, response_id


class DSHExecutionAdapter:
    """`ExecutionCapability` backed by a real DSH SDK runtime process."""

    # POSIX process groups can be escaped with setsid()/double-fork. There is
    # no reliable full-tree owner in this executor on this host.
    process_tree_supported = False

    def __init__(
        self,
        *,
        provider: str = "deepseek-official",
        model: str = "deepseek-flash",
        runtime_cmd: Sequence[str] | None = None,
        dsh_home: str | os.PathLike[str] | None = None,
        cwd: str | None = None,
        timeout_seconds: float = 180.0,
        env_extra: Mapping[str, str] | None = None,
        permission_mode: AccessMode | str | None = None,
        require_process_tree: bool = True,
    ) -> None:
        self._provider = redact(provider)
        self._model = redact(model)
        self._runtime_cmd = list(runtime_cmd) if runtime_cmd else list(DEFAULT_RUNTIME_CMD)
        self._dsh_home = str(dsh_home) if dsh_home else None
        self._cwd = cwd or os.getcwd()
        self._timeout_seconds = float(timeout_seconds)
        self._env_extra = dict(env_extra or {})
        self._permission_mode = permission_mode
        self._require_process_tree = require_process_tree
        self._lock = threading.Lock()
        self._active: dict[str, tuple[_ExecutionRecord, _SdkRuntime | None]] = {}
        self._recent: OrderedDict[str, _ExecutionRecord] = OrderedDict()

    # ------------------------------------------------------------ public API

    def execute(self, request: ExecutionRequest) -> ExecutionResult | ExecutionFailure:
        injected = request.metadata.get("execution_id") if isinstance(request.metadata, Mapping) else None
        execution_id = (
            injected.strip()
            if isinstance(injected, str) and injected.strip()
            else str(uuid.uuid4())
        )
        invocation_id = request.invocation_id or str(uuid.uuid4())
        mode_name = request.execution_mode.value

        record = _ExecutionRecord(
            execution_id=execution_id,
            invocation_id=invocation_id,
            task_id=request.task_id,
            provider=self._provider,
            model=self._model,
            mode=mode_name,
        )
        runtime: _SdkRuntime | None = None
        events = _EventCollector()
        requested_mode: AccessMode | None = None

        def finish(status: str) -> None:
            record.status = status

        deadline = time.monotonic() + self._timeout_seconds

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _BridgeTimeout("execution timeout")
            return left

        def perform() -> ExecutionResult | ExecutionFailure:
            nonlocal runtime, requested_mode
            if self._require_process_tree:
                finish("FAILED")
                return self._failure(
                    invocation_id, ExecutionFailureKind.EXECUTION,
                    "PROCESS_TREE_UNSUPPORTED", "no reliable full-tree process owner is available",
                    continuation_ref=None, metadata=self._meta(record, events),
                )
            try:
                value = request.access_mode or self._permission_mode
                mode = AccessMode(value.strip().lower() if isinstance(value, str) else value)
                requested_mode = mode
            except (ValueError, TypeError):
                finish("FAILED")
                return self._failure(
                    invocation_id, ExecutionFailureKind.EXECUTION,
                    "ACCESS_MODE_REQUIRED", "an explicit supported access mode is required",
                    continuation_ref=None, metadata=self._meta(record, events),
                )
            try:
                if not request.workspace_path:
                    raise ValueError("missing workspace")
                path = Path(request.workspace_path).resolve(strict=True)
                if not path.is_dir():
                    raise ValueError("workspace is not a directory")
                workspace = str(path)
            except (OSError, ValueError, RuntimeError):
                finish("FAILED")
                return self._failure(
                    invocation_id, ExecutionFailureKind.EXECUTION,
                    "WORKSPACE_PATH_REQUIRED", "an existing workspace directory is required",
                    continuation_ref=None, metadata=self._meta(record, events),
                )
            try:
                runtime = self._spawn(events, workspace, mode)
            except OSError as exc:
                finish("FAILED")
                return self._failure(
                    invocation_id, ExecutionFailureKind.EXECUTION,
                    "RUNTIME_SPAWN_FAILED",
                    f"cannot spawn DSH runtime: {type(exc).__name__}",
                    continuation_ref=None,
                    metadata=self._meta(record, events),
                )
            record.runtime_pid = runtime.pid
            record.pgid = runtime.pgid
            with self._lock:
                self._active[execution_id] = (record, runtime)

            try:
                initialize_params: dict[str, Any] = {
                    "cwd": workspace,
                    "provider": self._provider,
                }
                if self._model:
                    initialize_params["model"] = self._model
                runtime.request("initialize", initialize_params, remaining())

                session_id, continuation_ref = self._resolve_session(request)
                record.session_id = session_id
                if request.execution_mode is ExecutionMode.CONTINUE:
                    runtime.request("session/resume", {"sessionId": session_id}, remaining())

                prompt = self._build_prompt(request)
                prompt_result = runtime.request("session/prompt", {
                    "sessionId": session_id,
                    "contentBlocks": [{"type": "text", "text": prompt}],
                }, remaining())
                record.message_id = str(prompt_result.get("messageId", ""))

                # Protocol 0.1.5 has no session/wait: completion is the turn/end
                # event (single prompt per runtime process). 0.2.x may re-add
                # wait; this path stays wire-compatible with both.
                self._await_turn_end(events, runtime, remaining())
            except _InvalidContinuation as exc:
                finish("FAILED")
                return self._failure(
                    invocation_id, ExecutionFailureKind.EXECUTION,
                    "INVALID_CONTINUATION", str(exc),
                    continuation_ref=request.continuation_ref,
                    metadata=self._meta(record, events),
                )
            except _BridgeTimeout:
                finish("TIMEOUT")
                return self._failure(
                    invocation_id, ExecutionFailureKind.EXECUTION,
                    "EXECUTION_TIMEOUT",
                    f"execution exceeded {self._timeout_seconds:.0f}s and was terminated",
                    continuation_ref=self._ref_of(record.session_id),
                    metadata=self._meta(record, events, timeout=True),
                    retryable=True,
                )
            except _JsonRpcError as exc:
                finish("FAILED")
                return self._classify_failure(
                    invocation_id, record, events,
                    continuation_ref=self._ref_of(record.session_id),
                    protocol_error=exc,
                )
            except _RuntimeClosed:
                status = "TERMINATED" if runtime.user_terminated else "FAILED"
                finish(status)
                category = "TERMINATED_BY_REQUEST" if runtime.user_terminated else "RUNTIME_CLOSED"
                return self._failure(
                    invocation_id, ExecutionFailureKind.EXECUTION,
                    category, "runtime process was terminated",
                    continuation_ref=self._ref_of(record.session_id),
                    metadata=self._meta(record, events),
                )

            turn_end = events.last_turn_end()
            if turn_end and turn_end.get("kind") == "error":
                finish("FAILED")
                return self._classify_failure(
                    invocation_id, record, events,
                    continuation_ref=self._ref_of(record.session_id),
                    protocol_error=None,
                )

            final_text, usage, response_id = events.assistant_summary()
            completed = bool(final_text) and COMPLETION_MARKER in final_text
            status = ExecutionStatus.COMPLETED if completed else ExecutionStatus.PROGRESS
            # Provisional completion only; cleanup and final safety checks below
            # decide whether this result may be returned or recorded as success.

            artifacts = ()
            if final_text:
                artifacts = (
                    Artifact(
                        kind="assistant-output",
                        producer_capability=f"dsh:{self._provider}",
                        payload={"text": final_text},
                    ),
                )
            return ExecutionResult(
                invocation_id=invocation_id,
                status=status,
                artifacts=artifacts,
                output=final_text or None,
                checkpoint=None,
                continuation_ref=self._ref_of(session_id),
                metadata={
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "message_id": record.message_id,
                    "runtime_pid": runtime.pid,
                    "provider": self._provider,
                    "model": self._model,
                    "mode": mode_name,
                    "usage": usage,
                    "response_id": response_id,
                    "turn_end_kind": turn_end.get("kind") if turn_end else None,
                    "fallback_used": False,
                },
            )

        outcome: ExecutionResult | ExecutionFailure | None = None
        try:
            outcome = perform()
        finally:
            if runtime is not None:
                record.process_group_cleared = runtime.terminate(requested=False)
                record.termination = runtime.termination
                record.stdout_reader_joined = runtime.stdout_reader_joined
                record.stdout_drained = runtime.stdout_drained
            modes, preset, codes = events.sandbox_evidence()
            record.sandbox_modes = modes
            record.sandbox_mode = modes[-1] if modes else None
            record.sandbox_consistent = bool(modes) and requested_mode is not None and all(
                observed == requested_mode.value for observed in modes)
            record.permission_preset = preset
            with self._lock:
                # Serialize final publication against cancellation. A cancel
                # accepted while active must never race a COMPLETED result.
                category = None
                if runtime is not None:
                    if not record.process_group_cleared:
                        category = "PROCESS_GROUP_ORPHANED"
                    elif not record.stdout_drained:
                        category = "SANDBOX_EVENTS_UNDRAINED"
                    elif "SANDBOX_UNAVAILABLE" in codes:
                        category = "SANDBOX_UNAVAILABLE"
                    elif modes and not record.sandbox_consistent:
                        category = "SANDBOX_MODE_MISMATCH"
                    elif runtime.user_terminated:
                        category = "TERMINATED_BY_REQUEST"
                    elif isinstance(outcome, ExecutionResult) and not modes:
                        category = "SANDBOX_MODE_UNVERIFIED"
                if category:
                    record.status = ("ORPHANED" if category == "PROCESS_GROUP_ORPHANED" else
                                     "TERMINATED" if category == "TERMINATED_BY_REQUEST" else "FAILED")
                    outcome = self._failure(
                        invocation_id, ExecutionFailureKind.EXECUTION, category,
                        "execution did not satisfy final cleanup and sandbox requirements",
                        continuation_ref=self._ref_of(record.session_id),
                        metadata=self._meta(record, events),
                    )
                elif isinstance(outcome, ExecutionResult):
                    record.status = ("COMPLETED" if outcome.status is ExecutionStatus.COMPLETED
                                     else "PROGRESSED")
                self._active.pop(execution_id, None)
                self._recent[execution_id] = record
                while len(self._recent) > 100:
                    self._recent.popitem(last=False)

        return replace(outcome, metadata={**outcome.metadata, **self._record_dict(record),
                                          "event_counts": events.counts()})

    def terminate(self, execution_id: str) -> bool:
        with self._lock:
            entry = self._active.get(execution_id)
            if entry is None or entry[1] is None:
                return False
            record, runtime = entry
            cleared = runtime.terminate()
            record.process_group_cleared = cleared
            record.termination = runtime.termination
            if not cleared:
                record.status = "ORPHANED"
            return cleared

    def is_alive(self, execution_id: str) -> bool:
        with self._lock:
            entry = self._active.get(execution_id)
        if entry is None:
            return False
        runtime = entry[1]
        return bool(runtime is not None and runtime.is_alive())

    def get_execution(self, execution_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            entry = self._active.get(execution_id)
            record = entry[0] if entry else self._recent.get(execution_id)
        return self._record_dict(record) if record else None

    def active_executions(self) -> list[Mapping[str, Any]]:
        with self._lock:
            records = [entry[0] for entry in self._active.values()]
        return [self._record_dict(record) for record in records]

    # -------------------------------------------------------------- internals

    def _record_dict(self, record: _ExecutionRecord) -> dict[str, Any]:
        return {
            "execution_id": record.execution_id,
            "invocation_id": record.invocation_id,
            "task_id": record.task_id,
            "pgid": record.pgid,
            "sandbox_mode": record.sandbox_mode,
            "sandbox_modes": list(record.sandbox_modes),
            "sandbox_consistent": record.sandbox_consistent,
            "stdout_reader_joined": record.stdout_reader_joined,
            "stdout_drained": record.stdout_drained,
            "permission_preset": record.permission_preset,
            "process_group_cleared": record.process_group_cleared,
            "process_tree_verified": False,
            "process_tree_status": "UNVERIFIED",
            "require_process_tree": self._require_process_tree,
            "termination": record.termination,
            "session_id": record.session_id,
            "message_id": record.message_id,
            "runtime_pid": record.runtime_pid,
            "started_at": record.started_at,
            "status": record.status,
            "provider": record.provider,
            "model": record.model,
            "mode": record.mode,
        }

    def _spawn(self, collector: _EventCollector, cwd: str, mode: AccessMode) -> _SdkRuntime:
        env = dict(os.environ)
        if self._dsh_home:
            env["DSH_HOME"] = self._dsh_home
        env.update(self._env_extra)
        env["DSH_PERMISSION_MODE"] = mode.value

        def on_notification(method: str, params: Mapping[str, Any]) -> None:
            if method == "session.event":
                event = params.get("event")
                if isinstance(event, Mapping):
                    collector.add(event)

        return _SdkRuntime(self._runtime_cmd, cwd=cwd, env=env,
                           on_notification=on_notification)

    def _resolve_session(self, request: ExecutionRequest) -> tuple[str, str]:
        if request.execution_mode is ExecutionMode.CONTINUE:
            ref = request.continuation_ref or ""
            if not ref.startswith(CONTINUATION_PREFIX) or len(ref) <= len(CONTINUATION_PREFIX):
                raise _InvalidContinuation("continuation_ref is not a DSH session reference")
            session_id = ref[len(CONTINUATION_PREFIX):]
            return session_id, ref
        session_id = str(uuid.uuid4())
        return session_id, CONTINUATION_PREFIX + session_id

    def _build_prompt(self, request: ExecutionRequest) -> str:
        lines = [f"[objective]\n{request.objective}"]
        if request.execution_mode is ExecutionMode.CONTINUE:
            lines.append("[mode]\nCONTINUE: keep working inside this existing DSH session.")
        elif request.execution_mode is ExecutionMode.HANDOFF:
            lines.append("[mode]\nHANDOFF: start fresh from the durable task facts below.")
            if request.handoff_reason:
                lines.append(f"[handoff_reason]\n{request.handoff_reason}")
        else:
            lines.append("[mode]\nFRESH: single bounded execution.")
        if request.durable_context:
            lines.append("[durable_context]\n" + json.dumps(
                redact_data(dict(request.durable_context)), sort_keys=True))
        if request.artifacts:
            payload = [
                {"kind": artifact.kind, "payload": artifact.payload,
                 "reference": artifact.reference}
                for artifact in request.artifacts
            ]
            lines.append("[artifacts]\n" + json.dumps(redact_data(payload), sort_keys=True))
        if request.checkpoint:
            lines.append(f"[checkpoint]\n{request.checkpoint}")
        if request.constraints:
            lines.append("[constraints]\n" + json.dumps(
                redact_data(dict(request.constraints)), sort_keys=True))
        lines.append(
            "[instruction]\nThis is one bounded execution. When the overall task is "
            "fully complete, end your final message with the exact line:\n"
            f"{COMPLETION_MARKER}"
        )
        return "\n\n".join(lines)

    @staticmethod
    def _await_turn_end(
        events: _EventCollector,
        runtime: _SdkRuntime,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + max(timeout, 0.0)
        while time.monotonic() < deadline:
            if events.last_turn_end() is not None:
                return
            if not runtime.is_alive():
                raise _RuntimeClosed("runtime process exited before turn end")
            time.sleep(0.05)
        raise _BridgeTimeout("turn/end")

    def _ref_of(self, session_id: str | None) -> str | None:
        return CONTINUATION_PREFIX + session_id if session_id else None

    def _meta(
        self,
        record: _ExecutionRecord,
        events: _EventCollector,
        *,
        timeout: bool = False,
    ) -> dict[str, Any]:
        return {
            **self._record_dict(record),
            "timeout": timeout,
            "fallback_used": False,
            "event_counts": events.counts(),
        }

    def _failure(
        self,
        invocation_id: str,
        kind: ExecutionFailureKind,
        category: str,
        message: str,
        *,
        continuation_ref: str | None,
        metadata: Mapping[str, Any],
        provider_error: ProviderError | None = None,
        retryable: bool = False,
    ) -> ExecutionFailure:
        return ExecutionFailure(
            invocation_id=invocation_id,
            kind=kind,
            category=category,
            message=message,
            retryable=retryable,
            provider_error=provider_error,
            continuation_ref=continuation_ref,
            metadata=dict(metadata),
        )

    def _classify_failure(
        self,
        invocation_id: str,
        record: _ExecutionRecord,
        events: _EventCollector,
        *,
        continuation_ref: str | None,
        protocol_error: _JsonRpcError | None,
    ) -> ExecutionFailure:
        metadata = self._meta(record, events)
        retry_seconds = None
        code = ""
        message = ""
        for event in events.events:
            if event.get("type") == "llm/retry":
                failure = (event.get("data") or {}).get("failure") or {}
                code = str(failure.get("code") or code)
                message = str(failure.get("message") or message)
                retry = failure.get("providerRetryAfterMs")
                if isinstance(retry, (int, float)) and not isinstance(retry, bool) and retry > 0:
                    retry_seconds = float(retry) / 1000.0
        turn_end = events.last_turn_end()
        if turn_end and turn_end.get("kind") == "error":
            error = turn_end.get("error") or {}
            code = str(error.get("code") or code)
            message = str(error.get("message") or message)

        category = DSH_CODE_CATEGORIES.get(code.upper()) if code else None
        if category is not None and category is not ErrorCategory.UNKNOWN:
            provider_error = classify_provider_error(
                self._provider,
                message=message,
                code=code,
                retry_after_seconds=retry_seconds,
            )
            return self._failure(
                invocation_id, ExecutionFailureKind.PROVIDER,
                code or "PROVIDER_FAILURE",
                message or "provider failure reported by DSH",
                continuation_ref=continuation_ref, metadata=metadata,
                provider_error=provider_error,
                retryable=category is ErrorCategory.RATE_LIMIT,
            )

        detail = message or (
            protocol_error.message if protocol_error is not None else ""
        )
        if turn_end and turn_end.get("kind") == "error":
            bridge_category = "TURN_ERROR"
        elif protocol_error is not None:
            bridge_category = "SDK_PROTOCOL_ERROR"
        else:
            bridge_category = "EXECUTION_FAILURE"
        return self._failure(
            invocation_id, ExecutionFailureKind.EXECUTION,
            bridge_category, detail or "execution failed inside DSH runtime",
            continuation_ref=continuation_ref, metadata=metadata,
        )
