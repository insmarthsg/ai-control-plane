# Observability & Reconciliation (ACP-GOAL-07)

Status: **implemented**, baseline `f16a64c`.
Read-only projection + recovery classification. No UI, no automatic recovery.

## 1. Truth sources

| Source | Kind | Authoritative? |
|---|---|---|
| Task State (`TaskState`) | durable | **yes** |
| Task Events (`list_events`) | durable append-only | yes, for history |
| Human request/decision artifacts | durable | yes |
| Artifacts / cursor / continuation | durable | yes |
| Runtime registry (`RuntimeManager`) | ephemeral, in-memory | no |
| Live liveness probe | ephemeral | only as evidence |
| Provider Health | live/ephemeral | no (read-only view) |

Two non-negotiables:

1. **`Task.status == RUNNING` does not mean an execution is running.** Task and
   runtime state are reported separately.
2. After a Control Plane restart, an empty runtime registry only means "no
   in-process controllable runtime". It is never proof that no external
   execution exists.

## 2. Task Observation

`Observer(store, runtime?, human?, health?).observe(task_id)` returns
`TaskObservation`:

- `task_id`, `task_revision`, `task_status`, `created_at`, `updated_at`,
  `observed_at`, `stale_snapshot`
- `active_capability`, `cursor_checkpoint`, `cursor_capability`,
  `continuation_present`
- `artifacts`: **summaries only** (id, kind, producer, created_at) — never
  payloads
- `runtimes`: separate runtime projections
- `pending_human`: waiting flag + request id/question/allowed actions (no
  context)
- `provider_health`: provider → status summary
- `issues`: `RUNTIME_QUERY_FAILED`, `HUMAN_QUERY_FAILED`,
  `HEALTH_QUERY_FAILED`, `STALE_SNAPSHOT`

Snapshot consistency: the observer reads Task, gathers sources, re-reads Task,
and retries once on a revision change; a second change marks
`stale_snapshot` instead of retrying forever. The view is not a distributed
transaction.

## 3. Runtime Observation

Per invocation: `execution_id`, `status`, timestamps, short reason. No PID,
session id, message id, or JSON-RPC handle. `has_live_runtime` is true only for
`STARTING/RUNNING/STOPPING`.

## 4. Human Gate Observation

`pending_human` exposes request id, requesting capability, question, allowed
actions, created time. Secret-bearing context is not projected.

## 5. Timeline

`timeline(task_id)` returns `TimelineEntry(seq, type, at, revision, actor,
details)` ordered by `seq`. `details` is a small whitelist
(invocation_id, capability_id, request id, decision, kind, status, artifact id,
checkpoint) — never prompts or raw event payloads.

## 6. Reconciliation contract

`Reconciler(observer).reconcile(task_id, profiles=..., uncertain_side_effect=...)`
returns `ReconciliationResult`:

- `status`, `reason`, `evidence[]`, `recommended_action`, `task_revision`,
  `observed_at`.

It is pure classification: no routing, execution, resume, cancel, health
update, or Task write. Repeating it on the same facts yields the same status,
evidence codes, and recommendation.

## 7. Evidence model

`Evidence(code, detail)`; codes include:

`TASK_*`, `PENDING_HUMAN_REQUEST`, `ACTIVE_RUNTIME`, `RUNTIME_LOST`,
`CONTINUATION_PRESENT`, `CHECKPOINT_PRESENT`, `RESUME_SUPPORTED`,
`RESUME_UNAVAILABLE`, `HANDOFF_SUPPORTED`, `INCOMPLETE_INVOCATION`,
`PERSISTENCE_UNCERTAIN`, `STALE_RUNTIME_RECORD`, `RUNTIME_QUERY_FAILED`,
`HUMAN_QUERY_FAILED`, `HEALTH_QUERY_FAILED`, `STALE_SNAPSHOT`,
`TASK_TERMINAL`.

Recommendations are always justified by evidence. `CONTINUE` requires an
opaque continuation plus a profile declaring resume support; `HANDOFF` requires
durable facts plus a profile declaring handoff support.

## 8. Recovery states

`ACTIVE`, `WAITING_HUMAN`, `RECOVERABLE`, `HANDOFF_POSSIBLE`, `BLOCKED`,
`LOST`, `TERMINAL`, `ORPHANED`, `UNCERTAIN`, `UNKNOWN`.

Decision order (frozen):

1. terminal Task wins (stale live runtime is evidence, not a resume reason);
2. pending human gate → `WAITING_HUMAN` (hard block);
3. human/runtime query failure → `UNKNOWN`;
4. live runtime → `ACTIVE`;
5. uncertain side effect flag → `UNCERTAIN`;
6. invocation started without terminal event → `ORPHANED`;
7. resumable continuation → `RECOVERABLE` (`CONTINUE`);
8. durable facts + handoff profile → `HANDOFF_POSSIBLE` (`HANDOFF`);
9. continuation without resume support → `BLOCKED`;
10. lost runtime without recovery facts → `LOST`;
11. otherwise `UNKNOWN`.

## 9. Recommended actions

`NONE`, `WAIT_FOR_HUMAN`, `CONTINUE`, `HANDOFF`, `MANUAL_REVIEW`,
`MARK_LOST`. They are advice only; a future `apply_recovery_action` capability
will execute them.

## 10. Orphan and uncertainty

`ORPHANED`: `EXECUTION_STARTED` exists for an invocation with no later
`EXECUTION_RESULT`/`EXECUTION_FAILED` and no live runtime. Never auto-retried.
`UNCERTAIN`: a caller-declared possibility that an external side effect
occurred but was not confirmed durably (GOAL-04 persistence-failure boundary);
recommendation is `MANUAL_REVIEW`.

## 11. Resume capability boundary

The core never inspects continuation refs. Callers pass a
`Mapping[capability_id, RecoveryProfile(supports_continue, supports_handoff)]`.
For the current DSH runtime (0.1.5-rc.3) the profile is
`supports_continue=False` (no `session/resume`), so a running Task with a DSH
continuation is `BLOCKED` with `RESUME_UNAVAILABLE` unless a handoff-capable
profile is provided. No fake resume, no automatic new session.

## 12. Human priority

A pending human decision is checked before live runtime, continuation, and
handoff. Observation/reconciliation never resolve or bypass it.

## 13. Restart semantics

Process A may create a RUNNING Task with checkpoint/continuation and exit; the
runtime registry is lost. Process B observes: Task RUNNING, runtimes empty,
checkpoint present, continuation present, then reconciles to `RECOVERABLE` or
`BLOCKED` based on profiles. Verified through a child-process reload.

## 14. Read-only guarantee

`observe`, `timeline`, and `reconcile` only read. Tests assert Task revision
and events are unchanged. Failures in health/runtime/human queries degrade the
view with evidence rather than aborting durable facts.

## 14.1 Workspace observation (GOAL-08A)

When a Task has a `workspace-binding`, `TaskObservation.workspace` exposes
workspace id, status/reason, expected vs actual repository/worktree/branch/
HEAD/tracking/dirty, mismatches, warnings, and `execution_allowed`.
Reconciliation returns `WORKSPACE_RECONCILIATION_REQUIRED` with
`WORKSPACE_VERIFICATION_FAILED` evidence when the binding no longer verifies;
it never resumes or repairs the workspace.

## 15. Known gaps

- No HTTP/UI; DTOs only.
- No durable runtime registry or external-process probe: absence of a runtime
  record is not proof of no external execution (`UNKNOWN` where applicable).
- No automatic apply/recovery loop.
- No persistence-history/uncertainty detection beyond caller-provided
  `uncertain_side_effect`.
- Provider health is a summary; query failure is evidence only.
- Cross-process resume remains blocked by DSH 0.1.5 (separate compatibility
  item).
