# Task Controller Contract (GOAL-04)

Status: **frozen at ACP-GOAL-04**, baseline `ae3ad0e`.
Principles: [ARCHITECTURE.md](ARCHITECTURE.md). Facts: [TASK_STATE.md](TASK_STATE.md).
Selection: [ROUTER.md](ROUTER.md).

## 1. Responsibility

The Task Controller is a thin composition boundary:

```
load Task State
  -> read Provider Health
  -> Thin Router selects one capability
  -> invoke ONE Execution Capability
  -> normalize result/failure
  -> persist through Task Store
  -> return AdvanceOutcome
```

It is not an agent, planner, workflow engine, scheduler, DAG, retry framework,
or failover loop. **Controller ≠ Workflow Engine. Router ≠ Executor.
Execution Adapter ≠ Controller.**

## 2. Execution Contract

`ExecutionRequest` is vendor-neutral: `invocation_id`, `task_id`, `objective`,
`durable_context`, `artifacts`, `execution_mode` (`FRESH | CONTINUE |
HANDOFF`), opaque `continuation_ref`, `checkpoint`, constraints, handoff
reason, metadata. It contains no session transcript.

`ExecutionResult`: `invocation_id`, `status` (`PROGRESS | COMPLETED`),
artifacts, optional output, `checkpoint`, opaque `continuation_ref`, metadata.
Completion is explicit; the controller never infers it.

`ExecutionFailure`: `invocation_id`, `kind` (`PROVIDER | EXECUTION | UNKNOWN`),
`category`, message, `retryable`, optional normalized `ProviderError`,
evidence, `partial_artifacts`, `checkpoint`, opaque `continuation_ref`,
metadata.

`ExecutionCapability.execute(request) -> ExecutionResult | ExecutionFailure`
is the replaceable seam. It knows nothing about DSH in core.

## 3. Bounded advance

One `advance()` performs exactly one step:

1. load Task
2. route
3. record routing decision
4. invoke one capability
5. persist result/failure
6. return

It does not loop until completion, retry, sleep, schedule, or launch another
advance. The caller decides whether to call again. Pause/cancel/human-review
insertion points can therefore sit between advances without changing the
controller.

## 4. Continuation

If the Task has an opaque `continuation_ref`, the controller asks the Router
for `CONTINUE` first. If no candidate can directly consume it, the controller
makes **one** bounded second routing attempt with `HANDOFF`; it never loops.
The chosen mode is passed to the adapter and reported in the outcome.

## 5. HANDOFF

Handoff carries only durable Task facts: objective, durable context, existing
artifacts, checkpoint, and a reason. It does not carry chat history, event
history, or the previous session. The adapter may start a brand-new session.

## 6. Provider failure

Only a normalized `ExecutionFailure(kind=PROVIDER, provider_error=...)` updates
Provider Health, and only through the injected health port
(`record_error`). The controller never parses HTTP status, headers, or
transport errors. On the next advance, the Router sees the updated health and
may select another candidate.

Provider failure keeps the Task `RUNNING` with saved partial progress so the
caller can advance again (deterministic simulated failover in tests; no
internal loop).

## 7. Execution failure

`EXECUTION`/`UNKNOWN` failures (workspace denied, tool missing, git conflict,
permission, adapter contract violation) are persisted as failure events but
**never** change Provider Health. The Task becomes `BLOCKED`; the provider
remains whatever it was. **Provider Failure ≠ Execution Failure** is enforced
by branch and tested.

## 8. NoRoute

If the Router returns `NoRoute`, the controller executes nothing, records the
routing decision (including per-candidate reasons), moves the Task to
`BLOCKED`, and returns `AdvanceStatus.NO_ROUTE`. It never invents a choice.
A user `REQUIRE` on an unavailable capability yields `NoRoute`, never a
silent fallback.

## 9. Revision conflict

Every controller write carries `expected_revision`. If another actor changes
the Task during execution, the result write raises `RevisionConflict`; the
controller returns `AdvanceStatus.CONFLICT` without overwriting. The execution
side effect may already have happened; the caller must reconcile.

## 10. Persistence failure

If execution succeeds but the Task Store write fails, the controller returns
`AdvanceStatus.PERSISTENCE_FAILED` with the execution result and
`invocation_id`, and the Task is **not** marked complete. The last confirmed
state remains readable. This is an uncertain outcome, not a success.

## 11. Events

The controller records `ROUTING_DECIDED`, `EXECUTION_STARTED`,
`EXECUTION_RESULT` or `EXECUTION_FAILED`, `CAPABILITY_UNRESOLVED`,
`TASK_BLOCKED`, and `TASK_COMPLETED`. Event payloads contain ids, modes,
statuses, categories, checkpoints, and artifact ids — never prompts,
credentials, or full outputs.

## 12. Insertion compatibility

If a Task already has an insertion frame, `advance()` returns `BLOCKED`
without touching the frame or writing state. Inserted capabilities remain a
future composition concern; the controller schema is unchanged.

## 13. DSH boundary and audit

DSH remains an Execution Adapter concern. A headless seam exists
(`dsh --profile headless "<task>"` prints one final assistant message and
exits), but the current audit found no stable structured contract:

- no JSON result schema or explicit `PROGRESS/COMPLETED` signal
- no headless continuation/resume flag; a new session per invocation
- no normalized failure taxonomy; failures surface as stderr/exit codes
- boot writes profile/session state under `DSH_HOME` and would consume a real
  provider call

Therefore **DSH_EXECUTION_ADAPTER = BLOCKED** in this goal. No shell mock, no
fake provider, and no unsupported adapter was added. The missing seam is:
structured result JSON, failure normalization, and an explicit resume handle.

## 14. Non-goals

No UI, Jev, human gate, pause/cancel/resume implementation, scheduler, queue,
DAG, workflow DSL, planner/worker/reviewer pipeline, infinite retry, or
distributed execution.

## 15. Verification

`tests/test_controller.py` + `tests/test_execution_contract.py`: one-advance
completion, two-advance CONTINUE, provider failure → health update → HANDOFF,
execution failure isolation, NoRoute executes nothing, REQUIRE respected,
revision conflict, persistence failure, insertion preservation, event trace,
secret redaction, invocation identity, DSH-absent import, and no vendor/loop
vocabulary.
