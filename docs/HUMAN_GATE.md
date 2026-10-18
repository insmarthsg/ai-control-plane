# Human Gate (ACP-GOAL-06)

Status: **implemented**, baseline `99fd4cd`.
Contracts: [CONTROLLER.md](CONTROLLER.md) · [TASK_STATE.md](TASK_STATE.md)

## 1. Purpose

Any capability may ask for **one human decision** at a safe boundary. The gate
is an insertable control capability, not a workflow stage, not a pause, and not
a provider feature.

```
Main capability
  -> durable progress (artifacts/cursor) persisted
  -> HumanDecisionRequest (insertion frame + durable artifact)
  -> Task BLOCKED
  -> human APPROVE / REJECT / MODIFY
  -> gate popped, cursor/capability restored
  -> Task RUNNING
  -> caller performs the next bounded advance
```

Tasks that never request a decision never touch the gate.

## 2. Contract (`core/human.py`)

`HumanDecisionRequest`: `request_id` (UUID), `task_id`, `requesting_capability`
(opaque), `reason`, `question`, `allowed_actions`, `context`, `created_at`.
`HumanDecision`: `request_id`, `task_id`, `action`, `instruction` (MODIFY only),
`decided_by`, `decided_at`, `task_revision`.

`HumanGate`:
`request(...)`, `resolve(task_id, request_id, action, instruction?, expected_revision)`,
`pending(task_id)`, `get_request`, `history(task_id)`.

Durable representation: request and decision are Task artifacts
(`human-decision-request`, `human-decision`); the gate itself is a normal
`InsertionFrame` with capability id `human-gate`.

## 3. Decisions

- **APPROVE** — continue under current conditions. The gate is popped, the
  cursor/capability restored, Task RUNNING. `approve()` never invokes the
  capability.
- **REJECT** — the requested action is not allowed. The decision is durable,
  the gate is popped, Task returns to RUNNING. REJECT is **not** Task FAILED,
  **not** Task CANCELLED, and has no Provider Health effect. The caller reads
  the decision and chooses the next step.
- **MODIFY** — requires a non-empty instruction. It is stored as an
  authoritative decision artifact; it is never silently converted to APPROVE.
  MODIFY survives `HANDOFF` because the decision artifact travels with the
  Task's artifacts into `ExecutionRequest.artifacts`.

Human Decision is authoritative: Router, Provider Health, judgment, and model
suggestions may not override a persisted MODIFY constraint.

## 4. Task blocking

`request` atomically pushes the gate frame, appends the request artifact, and
sets the Task to `BLOCKED` (one revision). `resolve` atomically pops the
matching frame, restores `return_cursor`/`return_capability`, appends the
decision artifact, and sets `RUNNING` (one revision). Both are
revision-checked; stale `expected_revision` returns `CONFLICT`.

While a gate is pending, `TaskController.advance()` refuses to execute
(`BLOCKED`) because an insertion frame is active. The Router is never asked to
bypass a pending human decision.

## 5. Insertion stack

Human gates use the existing LIFO stack. `Execution -> Review -> Human` restores
**Review** when the human resolves, not Execution; popping Review later returns
to Execution. Only the top frame may be resolved.

## 6. Identity and duplicates

- Every request gets a new `request_id` UUID; `task_id` alone is never used.
- One active gate per task. A second `request` raises `ALREADY_PENDING`.
- Decisions are immutable. Resolving a request twice raises `ALREADY_RESOLVED`;
  history is append-only.
- A request that is no longer the active top frame (e.g. gate closed outside
  the resolver) raises `STALE_REQUEST`; it can never resolve a newer gate.
- A request id presented under a different task raises `WRONG_TASK`.
- Unknown ids raise `NOT_FOUND`; disallowed actions raise `INVALID_ACTION`.

## 7. Controller integration

A capability requests a gate by returning an `ExecutionResult` (PROGRESS) with
`metadata["human_decision"] = {requesting_capability, reason, question,
allowed_actions, context}`. The controller:

1. persists artifacts/cursor/active capability first (durable boundary),
2. invokes the injected `HumanGatePort` (optional; no import of `core.human`),
3. reloads the Task and returns `AdvanceStatus.HUMAN_REQUIRED` with Task
   `BLOCKED`.

Without an injected gate the signal is ignored, so ordinary execution is
unaffected. A capability must not call the gate mid-invocation: it must return
at the safe boundary, otherwise its post-execution persistence could conflict.

## 8. Crash / reload

Requests and decisions live in Task State, not memory. A second process can
load the Task, query `pending()`, and resolve it; tested with two subprocesses.

## 9. Boundaries

- **Runtime Control**: the gate never starts, cancels, or freezes a runtime.
  A capability stops at a safe boundary before asking; an in-flight DSH process
  is not paused for approval.
- **Provider Health**: request/resolve never read or write health.
- **Router**: pending human decisions are a hard block; routing cannot bypass.
- **Workflow**: no planner/worker/reviewer vocabulary or fixed pipeline. The
  requesting capability is opaque.
- **Policy**: no rules engine decides what needs approval; the capability asks.

## 10. Security

Request/decision artifacts pass the existing `core.redaction` boundary
(text and key-aware). Tests confirm credentials in `reason`/`context`/
`instruction` do not reach the persisted Task document.

## 11. Known gaps

- No approval timeout, notification, or UI (by design).
- One active gate per task; parallel approvals are not supported.
- `request` requires the Task to be RUNNING and currently rejects opening over
  another active human gate; it supports other non-human insertion frames.
- No human identity/authentication beyond an opaque `decided_by` label.
- A stale/closed gate can only be detected from Task state, not from a
  distributed lease.
- The controller signal shape is a documented metadata convention, not a new
  Stable Core type.
