# Runtime Control (ACP-GOAL-05)

Status: **implemented**, baseline `6deef60`. Real start/status/cancel verified
against `dsh --profile sdk` (CLI 0.1.5-rc.3).

## 1. Runtime vs Task lifecycle

Two independent lifecycles:

| Task (durable) | Runtime (live invocation) |
|---|---|
| `CREATED`, `RUNNING`, `PAUSED`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED` | `STARTING`, `RUNNING`, `STOPPING`, `STOPPED`, `SUCCEEDED`, `FAILED`, `LOST` |

Runtime cancel **never** moves the Task to `CANCELLED`, `PAUSED`, or `FAILED`.
The caller (Controller/upper runtime) decides the Task outcome from the
returned ExecutionFailure. Runtime state is in-memory live control state, not
durable truth; Task State and Task Events remain durable.

## 2. Runtime Contract (`core/runtime.py`)

```
RuntimeManager(capabilities={capability_id: RuntimeExecution})
  start(capability_id, ExecutionRequest) -> RuntimeRecord   # returns before completion
  status(execution_id) -> RuntimeRecord | None              # None = NOT_FOUND
  cancel(execution_id) -> CancelResult
  events(execution_id) -> tuple[RuntimeEvent, ...]
  records_for_task(task_id) -> RuntimeRecord[]  # read-only observability query
```

`RuntimeExecution` = the existing `execute(request)` seam plus
`terminate(execution_id) -> bool` and `is_alive(execution_id) -> bool`.
The core contract contains no DSH/provider/process/wire vocabulary.

## 3. Execution identity

`execution_id` is an opaque UUID minted by the runtime manager per invocation.
It encodes no provider, model, session, message, or process id. The manager
injects it into `ExecutionRequest.metadata["execution_id"]`; an adapter may
use that id for its own runtime handle. Callers never need session/message ids
or process handles.

`invocation_id` stays the durable correlation id already used by the Execution
Contract.

## 4. START

`start()` spawns one worker thread that invokes `capability.execute(request)`.
It returns immediately with the `execution_id` and `STARTING`/`RUNNING` state,
before completion. There is no scheduler, queue, job framework, or run loop.

## 5. STATUS

`status(execution_id)` returns a snapshot of the live invocation:

- `STARTING` — worker accepted, capability not yet running.
- `RUNNING` — execute() in flight.
- `STOPPING` — cancel requested, termination not confirmed.
- `STOPPED` / `SUCCEEDED` / `FAILED` / `LOST` — terminal.

Unknown or expired ids return `None` (`NOT_FOUND`); no exception is raised.
Completed invocations keep a small terminal record (100 max) so status remains
queryable after retirement.

## 6. CANCEL

`cancel()` sets `STOPPING`, records `RUNTIME_CANCEL_REQUESTED`, calls the
capability's `terminate(execution_id)`, and waits (bounded) for the worker to
settle. It never merely flips registry state.

`CancelResult`:

- `CANCELLED` — termination confirmed, runtime `STOPPED`.
- `ALREADY_TERMINAL` — invocation already `SUCCEEDED`/`FAILED`/`STOPPED`/`LOST`.
- `NOT_FOUND` — unknown id.
- `REQUESTED` — terminate accepted, worker did not settle within the timeout.
- `FAILED` — terminate rejected and worker did not settle.

`confirmed` distinguishes “request sent” from “confirmed stopped”.

## 7. Cancel isolation

Each invocation has its own `execution_id`, worker, and capability runtime.
Cancelling A terminates only A's runtime; B keeps `RUNNING` and can reach
`SUCCEEDED`. This is tested with two concurrent runtimes.

## 8. Terminal records

Retired invocations keep only: `execution_id`, `task_id`, `invocation_id`,
`capability_id`, status, timestamps, reason, and minimal runtime events.
No prompt, credential, session transcript, or process handle is retained.

## 9. LOST runtime

`status()` probes `capability.is_alive(execution_id)` for a `RUNNING` worker
(after a short grace period to avoid completion races). A dead runtime becomes
`LOST`, records `RUNTIME_LOST`, and later worker returns cannot resurrect it.
Recovery/reconciliation is future work.

## 10. Event model

In-memory per invocation: `RUNTIME_STARTED`, `RUNTIME_CANCEL_REQUESTED`,
`RUNTIME_CANCELLED`, `RUNTIME_SUCCEEDED`, `RUNTIME_FAILED`, `RUNTIME_LOST`.
Events carry a timestamp and short reason only — no prompt, PID, or secret.
They are not written to Task State; durable TaskEvent stays Task-owned.

## 11. Provider health boundary

User cancel is an execution event, never a provider error. The adapter returns
`ExecutionFailure(kind=EXECUTION, category=TERMINATED_BY_REQUEST)`; Runtime
Control records `STOPPED`. Provider Health is not read or written by runtime
control; tests assert Health is byte-identical before/after cancel.

## 12. DSH boundary

DSH-specific behavior lives only in `adapters/dsh_execution.py`: the
`dsh --profile sdk` process, JSON-RPC, session/message ids, and `terminate`.
`RuntimeManager` sees only `execution_id`, `execute`, `terminate`, `is_alive`.
Process-per-execution remains an adapter detail.

## 13. Pause

**NOT_SUPPORTED.** DSH 0.1.5 has no native pause, and runtime control does not
rename cancel to pause.

## 14. Resume

**BLOCKED_BY_DSH_VERSION.** DSH 0.1.5-rc.3 has no `session/resume`, and
re-prompting a persisted session id fails with `already exists`. The runtime
contract leaves room for a future `resume(execution_id)` seam; no fake resume
exists.

## 15. Real verification (2026-10-18)

| Check | Result |
|---|---|
| REAL_RUNTIME_START | VERIFIED — real runtime started, `execution_id` returned before completion |
| REAL_RUNTIME_STATUS | VERIFIED — `RUNNING`, real `message_id` |
| REAL_RUNTIME_CANCEL | VERIFIED — `CANCELLED`, confirmed, `STOPPED`, runtime PID dead |
| REAL_PROVIDER_CANCEL | UNVERIFIED — session log evidence unavailable after the kill; cannot prove an in-flight provider turn was streaming at cancel time |

The smoke cancelled a 900-word generation shortly after prompt acceptance. No
rate-limit/quota tests were performed.

## 16. Future recovery / reconciliation

Open items: durable runtime registry (not implemented by design), crash
recovery for live runtimes, provider-level cancellation evidence, and
cross-process resume once a DSH runtime exposes `session/resume`.
