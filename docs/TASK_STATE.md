# Task State Contract (GOAL-02)

Status: **frozen at ACP-GOAL-02**, baseline `7f9d4b2`.
This is the concrete Task State schema. Architecture principles remain in
[ARCHITECTURE.md](ARCHITECTURE.md); this file is the single source of truth for
fields, transitions, persistence, and concurrency.

## 1. What this is

A task belongs to the control plane, not to a model, provider, session, or
executor. The store answers:

- what is true about this task now
- what has already been produced
- if execution stops, where does it resume

It does **not** decide what runs next. It is not a workflow engine, scheduler,
DAG, router, planner, or reviewer. **Task State ≠ Workflow State.**

## 2. Model

`TaskState`:

| Field | Meaning |
|---|---|
| `task_id` | Opaque UUID. Never encodes provider, model, session, project, or executor. |
| `objective` | The user's original goal. Immutable after creation in this goal. |
| `lifecycle_status` | Generic lifecycle (§3). |
| `created_at`, `updated_at` | UTC ISO-8601 timestamps. |
| `task_input` | Structured input for the task; not a conversation transcript. |
| `durable_context` | Curated facts learned during execution; replaces wholesale on update. |
| `artifacts` | Durable handable outputs (§4). |
| `active_capability` | Opaque `{capability_id, capability_kind}` currently handling the task. |
| `cursor` | Generic resume information for the active capability (§5). |
| `interruption` | Neutral pause record (§6). |
| `insertions` | Stack of temporary insertion frames (§7). |
| `revision` | Optimistic concurrency counter, starts at 0. |

`TaskEvent`: `seq`, `type`, `actor`, `revision`, `data`, `event_id`, `at`.
Events record why the snapshot changed; they are not an event-sourcing log and
cannot rebuild the task by replay.

No stage, step number, pipeline, DAG, owner, provider, or model field exists.

## 3. Lifecycle

```
CREATED ──▶ RUNNING ──▶ COMPLETED
                │  ▲
                │  └── PAUSED ◀── interrupt()
                │  └── BLOCKED ◀── set_lifecycle()
                ├──▶ FAILED
                └──▶ CANCELLED
```

| From | Allowed to |
|---|---|
| `CREATED` | `RUNNING`, `CANCELLED` |
| `RUNNING` | `PAUSED`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `PAUSED` | `RUNNING`, `CANCELLED` |
| `BLOCKED` | `RUNNING`, `CANCELLED` |
| terminal | nothing |

Terminal statuses are `COMPLETED`, `FAILED`, `CANCELLED`. Terminal tasks accept
no new artifacts, checkpoints, or insertions.

## 4. Artifacts

`Artifact`: `artifact_id`, `kind`, `producer_capability`, `payload` (inline
JSON), `reference` (opaque external pointer), `created_at`, `metadata`.

`kind` is free-form (`analysis`, `plan`, `patch`, `evidence`, `review-result`,
`answer`, `workspace-binding`, …). The core has no artifact-type registry. Small payloads are inline;
large objects are referenced, not embedded.

## 5. Execution cursor

`ExecutionCursor`: `capability_id`, `checkpoint`, `continuation_ref`,
`metadata`.

- `checkpoint` is an opaque string ("after-analysis"), never `stage_number`.
- `continuation_ref` may hold a harness session id, but the core never parses
  or requires it. DSH-specific values stay opaque.
- `save_checkpoint` stores it; insertion push/complete restores the exact
  previous cursor.

## 6. Interruption

`Interruption`: `interruption_id`, `reason`, `resume_point`, `evidence`,
`interrupted_at`.

`reason` is a neutral label (`provider_unavailable`, `human_required`,
`user_pause`, `environment_denied`, `capability_failure`,
`external_dependency`, …). It is **not** the provider error taxonomy.
`interrupt()` requires `RUNNING` and moves the task to `PAUSED`; `resume()`
requires `PAUSED`/`BLOCKED` and clears the interruption. The cursor is
preserved across pause/resume.

## 7. Insertion frames

`InsertionFrame`: `insertion_id`, `capability`, `reason`, `input`,
`return_cursor`, `return_capability`, `pushed_at`.

Frames are a stack:

```
push REVIEW   → active=REVIEW,  cursor=None, frame.return_cursor=execution cursor
push HUMAN    → active=HUMAN,   frame.return_cursor=review cursor
complete HUMAN → active=REVIEW, cursor=review cursor
complete REVIEW→ active=EXECUTION, cursor=execution cursor
```

- `push_insertion` requires `RUNNING` and a non-terminal task.
- `complete_insertion` pops exactly one frame, optionally records a result
  artifact, and restores the saved cursor/capability.
- Nested insertions are **supported** by the same schema; no depth field or
  workflow branch exists.
- No automatic insertion policy is implemented. Callers push and complete
  frames.

## 8. Persistence

Backend: `JSONTaskStateStore`, one document per task at
`<root>/<task_id>.json`.

Why JSON files: inspectable, no dependency, atomic replace is crash-safe on a
single local filesystem, and cross-process recovery is directly testable.
SQLite was rejected for now: it adds a schema/migration layer and is less
transparent at this scale. A future `SQLiteTaskStateStore` can implement the
same `TaskStateStore` Protocol without changing `core/task.py`.

Document shape:

```json
{
  "schema_version": 1,
  "task": { "...": "TaskState fields" },
  "events": [ { "seq": 1, "type": "TASK_CREATED", "...": "..." } ]
}
```

Writes: temp file in the same directory → `fsync(file)` → `os.replace()` →
best-effort directory `fsync`. A failed write leaves the last confirmed
document intact and does not advance `revision`.

Concurrency: every update takes a per-task in-process lock plus a POSIX
`flock` on `<root>/.<task_id>.lock`, re-reads the current revision under the
lock, and rejects stale writers with `RevisionConflict`. No distributed lock
and no last-write-wins.

## 9. Store API

`core/task_store.py` defines the Protocol and errors. Operations:

- `create_task`, `get_task`, `list_tasks`, `list_events`
- `set_lifecycle`, `set_durable_context`, `set_active_capability`
- `add_artifact`, `save_checkpoint`
- `record_event`, `record_execution_result` (controller observability, atomic progress)
- `open_human_gate`, `resolve_human_gate` (atomic gate frame + artifact + BLOCKED/RUNNING)
- `interrupt`, `resume`
- `push_insertion`, `complete_insertion`

Callers never open the JSON file directly. `stores/json_task_store.py` is the
only module that knows the on-disk format. Observability reads Task/Events via
`get_task`/`list_events` and adds no schema of its own.

## 10. Events

Current types: `TASK_CREATED`, `TASK_STARTED`, `TASK_PAUSED`, `TASK_BLOCKED`,
`TASK_RESUMED`, `TASK_COMPLETED`, `TASK_FAILED`, `TASK_CANCELLED`,
`CONTEXT_UPDATED`, `CAPABILITY_ACTIVATED`, `ARTIFACT_ADDED`,
`CHECKPOINT_SAVED`, `TASK_INTERRUPTED`, `INSERTION_PUSHED`,
`INSERTION_COMPLETED`, `ROUTING_DECIDED`, `EXECUTION_STARTED`,
`EXECUTION_RESULT`, `EXECUTION_FAILED`, `CAPABILITY_UNRESOLVED`,
`HUMAN_DECISION_REQUESTED`, `HUMAN_DECISION_RESOLVED`, `WORKSPACE_BINDING_CREATED`,
`WORKSPACE_VERIFIED`, `WORKSPACE_VERIFICATION_FAILED`.

Most mutations append one event at the resulting revision; an execution result
that completes a task appends `EXECUTION_RESULT` and `TASK_COMPLETED` in the
same commit. The snapshot is authoritative; events explain change history.

## 11. Security

Before persistence, all task data passes `core.redaction.redact_data`:

- text patterns: Bearer tokens, `sk-…`, JWTs, `api_key=…`, `accountId=…`
- key-aware fields: `api_key`, `authorization`, `access_token`,
  `refresh_token`, `id_token`, `token`, `secret`, `password`, `accountId`

Redaction happens at write time; loaded state contains `[REDACTED]` and raw
secrets are never on disk.

## 12. Failure isolation

One task's write failure raises `TaskStoreError` and leaves that task at its
last confirmed revision. Other tasks are independent files with independent
locks and are unaffected. A corrupted document raises `CorruptedTaskState`
without touching sibling documents.

## 13. Non-goals

Not implemented here, by contract: Router, automatic failover, Jev, planning,
review, human approval UI, notifications, queue, scheduler, DAG, workflow
engine, object storage, event sourcing, provider selection, or stage model.

## 14. Verification

Automated tests cover: create/get, executor independence, lifecycle,
durable context, artifacts, cursor, interruption, process-restart recovery
(child process creates; a second child recovers and finishes 17+29+43=89),
executor A→B handoff, insertion push/complete, nested insertions, cross-process
stale-writer conflict, atomic-write failure, per-task isolation, secret
non-persistence, and core/storage boundary checks.
