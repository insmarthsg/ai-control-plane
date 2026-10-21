# Control Console (ACP-GOAL-08B)

Status: **implemented**, baseline `6805375`.
Local-only, single-page console over the existing Control Plane. No new core
business logic.

## 1. Purpose

Let a human, in a browser:

1. bind and verify a real workspace with the 08A guard;
2. enter a natural-language task;
3. run one bounded execution through the real Controller → DSH adapter;
4. see durable Task truth, workspace truth, provider/model, result, timeline;
5. stop the active runtime through Runtime Control;
6. answer a pending Human Gate (approve / reject / modify);
7. refresh or restart the server and recover durable truth.

## 2. Architecture

```
browser (one HTML page, polling 1.5s)
  -> console/server.py (stdlib HTTP, 127.0.0.1)
  -> console/app.py (orchestration only)
       -> WorkspaceGuard / WorkspaceBinding
       -> JSONTaskStateStore + TaskController + ThinRouter
       -> HumanGate
       -> DSHExecutionAdapter (read-only / mutating)
       -> Observer + Reconciler
```

The page never keeps its own task/workspace state: every state it shows comes
from `Observation`, `Reconciliation`, `Timeline`, and the durable Task.

## 3. How to start

```bash
cd /Users/work_java/ai-control-plane
python3 -m console.server --port 8765
```

Open `http://127.0.0.1:8765`. Data lives in `.console-data/` (gitignored) by
default; override with `--data-dir` or `ACP_CONSOLE_DATA`.

Provider route defaults to `deepseek-official/deepseek-chat`; override with
`ACP_CONSOLE_PROVIDER` / `ACP_CONSOLE_MODEL`. The read-only capability uses
DSH permission mode `read-only`; the explicit mutating capability uses
`workspace-write`.

The server binds only `127.0.0.1`. There is no auth, no multi-user mode, no
remote deployment.

## 4. Workspace verification

The user enters an absolute path and clicks **验证工作区**. The backend:

1. asks the Git guard for mechanical facts (canonical path, repo identity,
   worktree, branch, HEAD, tracking ref/head, dirty state);
2. builds an authoritative `WorkspaceBinding` (branch/head/tracking are read,
   not guessed);
3. re-runs `WorkspaceGuard.verify` and returns the full verification.

The page displays VERIFIED/BLOCKED, path, repository, worktree, branch, HEAD,
tracking ref, tracking head, clean/dirty, mismatches, and recommendation.
**开始** stays disabled unless `execution_allowed` is true.

## 5. READ_ONLY / MUTATING

Default is **只读分析** (read-only). The user must explicitly choose
**允许修改项目**; the mode is stored in the binding policy and selects the
read-only or mutating DSH capability. No permission is inferred from natural
language. The Controller derives the request access mode from the persisted
binding, and the DSH adapter sets its permission environment from that mode.
Missing/unavailable/mismatched sandbox evidence blocks execution completion;
there is no fallback to an unsandboxed or writable mode.

## 6. Task start

Before creating a Task or runtime, the Console requires either `worker_kind=NONE`
(the local Human Gate demo) or all of `DSH_IN_PROCESS`, `sandbox=DSH`, and
`process_tree=True`. Other combinations, including `CODEX_EXTERNAL` and the
`UNSUPPORTED` defaults, raise `UNSUPPORTED_CHILD_TYPE`. Both default DSH specs
honestly declare `process_tree=False`, so automatic DSH execution is blocked
with that error and creates no Task/runtime. The adapter also defaults to
`require_process_tree=True` and fails before spawn with `PROCESS_TREE_UNSUPPORTED`.
There is no silent group-only downgrade. A reliable full-tree owner is not
implemented; only the `worker_kind=NONE` demo remains allowed by default.

`POST /api/tasks` creates a real Task, sets it RUNNING, persists the binding,
and starts **one** bounded `TaskController.advance` in a background thread. The
HTTP response returns the `task_id` immediately.

The controller verifies the workspace again before invoking the capability,
sets `ExecutionRequest.workspace_path` to the verified worktree, and the DSH
adapter spawns the runtime with that cwd. The console server's own cwd is
irrelevant.

## 7. Observation and polling

`GET /api/tasks/{id}` returns:

- durable task status and revision;
- `Observation` (task, runtime, pending human, workspace, health);
- `Reconciliation` status/action/evidence;
- ordered timeline;
- result text (latest `assistant-output`/`result` artifact);
- provider/model/capability presentation;
- `can_cancel` / `can_continue`.

The page polls every 1.5 seconds. Refresh restores `task` from the URL or
localStorage and re-reads everything from the backend.

## 8. Stop

**停止当前执行** calls `POST /api/tasks/{id}/cancel`, which uses the console's
execution gateway `terminate(execution_id)` — the same adapter termination seam
used by Runtime Control. It does not edit Task status or kill an unrelated PID.
The UI re-reads Observation; a runtime stop is not shown as Task CANCELLED.
Cancellation returns the adapter's actual termination result; no active runtime,
no terminate method, or unconfirmed cleanup returns false. DSH termination
checks the original owned process group; it refuses stale groups after leader
exit. Group clearance does not prove full descendant cleanup; direct group-only
adapter results always report `process_tree_verified=False`. There is no "pause" wording.

## 9. Human Gate

When `pending_human` exists, the page shows the question, requesting
capability, and allowed actions:

- **批准** → real `HumanDecision(APPROVE)`
- **拒绝** → real `HumanDecision(REJECT)` (not Task FAILED/CANCELLED)
- **按修改后的要求继续** → real `HumanDecision(MODIFY)` with durable
  instruction

Resolution is followed by exactly one bounded advance. Decisions are visible
after refresh and survive server restart.

The built-in `demo-human` capability exists only to validate this path; it goes
through real Task State, Human Gate, API, and UI.

## 10. Reconciliation

ORPHANED / UNCERTAIN / LOST / BLOCKED / WORKSPACE_RECONCILIATION_REQUIRED are
shown honestly with the recommended action. No automatic retry, resume,
handoff, failover, or workspace repair is performed.

## 11. Workspace evidence

The page shows the verified path, branch, HEAD, tracking head, clean/dirty, and
verification time. It does **not** claim which files a model actually read:
there is no file-monitoring evidence, so no "Files Referenced" is shown.

## 12. Result and timeline

The result is the real final `assistant-output` artifact, rendered as
pre-wrapped plain text. The timeline maps real Task events to human-readable
labels; raw JSON is not dumped.

## 13. First user validation

1. Open the local URL.
2. Enter a real Git workspace absolute path; click **验证工作区**; confirm
   VERIFIED, path, branch, HEAD, clean/dirty.
3. Keep **只读分析**; enter the project-judgment prompt; click **开始**.
4. Wait; confirm real workspace, provider/model, timeline, result.
5. Optionally start a long read-only analysis and click **停止当前执行**.
6. For Human Gate validation use the `demo-human` capability and try
   approve/reject/modify.

## 14. Known limitations

- Real DSH filesystem sandbox isolation is **UNVERIFIED** here; the approved
  host audit observed sandbox unavailability. Synthetic SDK tests establish
  failure handling only.
- Process ownership is in-memory and limited to the initial POSIX process group;
  crash orphans and descendants that leave that group are not contained.

- Browser automation is not installed; `BROWSER_E2E` is **UNVERIFIED** (manual
  validation required).
- Mutating mode is explicit but the DSH approval channel may still constrain
  tool use; read-only is the validated default.
- No pause; only stop is offered.
- No websocket/SSE; polling only.
- No auth/multi-user/remote deployment.
- Runtime registry is in-memory; after server restart the page shows durable
  Task truth and a reconciliation status rather than pretending a runtime
  exists.
- The demo capability is a test path, never presented as a real model.
