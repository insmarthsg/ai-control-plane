# Workspace Binding & Guard (ACP-GOAL-08A)

Status: **implemented**, baseline `a41efea`.
Read-only verification; no workspace mutation, no UI, no automatic git actions.

## 1. Why identity needs more than a name

A project name is not an execution environment. A repository path is not an
execution environment. A branch name is not an execution environment either:
one repository can have several registered worktrees with different branches,
different HEADs, and different dirty state. A task must therefore be bound to
an explicit **workspace** and verified before any capability runs.

Anonymized incident this closes:

```
repo R
├── worktree A: branch feature/current, clean   <- task was authorized here
└── worktree B: branch feature/old,   dirty    <- agent actually started here

old behavior: B's dirtiness blocked the task (wrong conclusion)
new behavior: the binding is A; B is never used and never modified
```

## 2. WorkspaceBinding

`core/workspace.py` (vendor-neutral):

| Field | Meaning |
|---|---|
| `workspace_id` | opaque UUID |
| `kind` | workspace kind (`git` today; seam for others) |
| `root_path` | path given for the workspace |
| `repository_root` | expected repository identity (Git worktree: main repo root) |
| `worktree_path` | expected worktree root |
| `branch` | expected branch |
| `baseline_commit` | commit the task was authorized against |
| `tracking_ref` | expected remote-tracking ref |
| `policy` | `read_only`, `require_tracking_ref` |

The binding is persisted as an immutable Task artifact
(`workspace-binding`) plus a `WORKSPACE_BINDING_CREATED` event. It survives
restart, Human Gate insertion, handoff, and provider switching. It is never
overwritten by observation.

## 3. Expected vs actual

The guard returns separate `expected` and `actual` maps. Observation never
writes `actual` into the binding. A wrong first execution therefore cannot
become the new "correct" baseline.

## 4. WorkspaceGuard

`WorkspaceGuard.verify(binding) -> WorkspaceVerification` is read-only.
`WorkspaceVerification` contains `status`, `reason`, `expected`, `actual`,
`mismatches`, `warnings`, `recommended_action`, `verified_at`,
`execution_allowed`.

Git implementation (`adapters/git_workspace.py`) uses only read-only commands:
`rev-parse`, `status --porcelain`, `worktree list`, `merge-base --is-ancestor`,
`remote get-url`. It never checks out, switches, stashes, resets, cleans,
pulls, merges, rebases, prunes, or creates worktrees.

## 5. Checks and failure reasons

| Check | Reason |
|---|---|
| path exists | `WORKSPACE_NOT_FOUND` (no fallback to cwd/home/repo root) |
| is a Git worktree | `NOT_GIT_WORKSPACE` |
| repository identity (Git common dir) | `REPOSITORY_MISMATCH` |
| worktree path | `WORKTREE_MISMATCH` |
| worktree still registered | `WORKTREE_NOT_REGISTERED` |
| branch present | `DETACHED_HEAD` |
| branch expected | `BRANCH_MISMATCH` |
| worktree clean | `WORKSPACE_DIRTY` |
| baseline reachable | `HEAD_MISMATCH` |
| tracking ref exists | `TRACKING_REF_MISSING` |
| local vs tracking | `BEHIND_TRACKING_REF`, `BRANCH_DIVERGED` |
| TOCTOU recheck | `WORKSPACE_CHANGED_AFTER_VERIFY` |

All failures set `execution_allowed = false`. No failure touches Git state.

## 6. Baseline and tracking semantics

`baseline_commit` is the authorized baseline, not a demand that HEAD never
move:

- `EXACT` — HEAD == baseline.
- `AHEAD` — baseline is an ancestor of HEAD (safe progress).
- `BEHIND` — HEAD is an ancestor of baseline (blocked as `HEAD_MISMATCH`).
- `DIVERGED` — no ancestry (blocked).
- `UNKNOWN` — no baseline supplied.

Tracking ref is compared independently:

- `UP_TO_DATE` — HEAD == tracking head.
- `BEHIND` — fast-forward only → blocked, recommended action
  `SAFE_FAST_FORWARD`; the guard never performs it.
- `DIVERGED` — blocked, `BRANCH_DIVERGED`.
- tracking head ahead of `baseline_commit` → warning `BASELINE_ADVANCED`,
  still verified when local state is otherwise correct.
- local ahead of tracking → warning `LOCAL_AHEAD_OF_TRACKING`.

## 7. Dirty workspaces

A dirty **correct** worktree blocks (`WORKSPACE_DIRTY`). A dirty **wrong**
worktree is not cleaned and not used: identity checks fail before any
mutating action, and tests assert byte-for-byte unchanged `git status`, files,
and stash list. There is no automatic stash/reset/clean in this goal.

`policy.read_only` is recorded and verified (identity still required); dirty
state blocks both read-only and mutating tasks in this version.

## 8. Execution enforcement

`bind_workspace()` stores the binding. Before any execution with a binding,
`TaskController`:

1. verifies the binding with the injected guard;
2. records `WORKSPACE_VERIFIED` or `WORKSPACE_VERIFICATION_FAILED`;
3. on failure: Task → `BLOCKED`, returns `AdvanceStatus.WORKSPACE_BLOCKED`,
   runs no capability, creates no runtime, leaves Provider Health untouched;
4. on success: re-verifies immediately before execution (TOCTOU), blocking on
   any change;
5. passes `workspace_path = actual worktree` and binding-derived `access_mode`
   into `ExecutionRequest` (`read-only` or `workspace-write`), recording
   `access_mode` and `workspace_id` in `EXECUTION_STARTED`.

Callers cannot override this with their process cwd or with model text. The
DSH adapter spawns its runtime with `request.workspace_path`, and reports the
cwd in the fake-peer test. It requires an explicit mode and existing canonical
workspace directory even for direct calls. The mode reaches `DSH_PERMISSION_MODE`
after capability environment merging. `SANDBOX_UNAVAILABLE`, absent sandbox
mode, any mismatched mode (including cleanup/cancel-time events), or incomplete
stdout draining fails closed: Task becomes blocked through the
existing Controller failure path, with no successful completion or provider
health penalty. Mode-reporting tests do not prove real filesystem isolation.

The Console rejects unsupported worker types before creating a Task/runtime.
`CODEX_EXTERNAL` is unsupported even when its native sandbox works, because
it does not provide the required full-tree ownership through this adapter.
Default DSH specs also declare `process_tree=False` and are rejected as
`UNSUPPORTED_CHILD_TYPE` before creating a Task/runtime. Direct DSH execution
defaults to `PROCESS_TREE_UNSUPPORTED` before spawn; only an explicit
`require_process_tree=False` caller opts into group-only cleanup, always with
`process_tree_verified=False`. There is no silent downgrade.

## 9. Restart, Human, Handoff, provider switch

- Binding is a durable artifact; a new process reloads and re-verifies it.
- Human Gate insertion/resolution preserves the binding artifact.
- Handoff and provider switching pass the same binding artifact; the new
  capability receives the same verified worktree. Model output cannot change
  `workspace_path`.
- Reconcile reports `WORKSPACE_RECONCILIATION_REQUIRED` when the durable
  binding no longer verifies (missing path, branch change, divergence, etc.).

## 10. Observability

`TaskObservation.workspace` exposes workspace id, status, reason, expected and
actual repository/worktree/branch/HEAD/tracking/dirty, mismatches, warnings,
and execution-allowed. Remote URLs are credential-redacted; file contents and
prompts are never included.

## 11. Security

- Remote URLs are stripped of userinfo before entering evidence.
- Paths are canonicalized (`resolve`, symlinks, `..`).
- No credentials, file contents, or prompts are stored by the workspace layer.
- Guard failure is an execution/workspace failure, never a provider failure.

## 12. Known limitations

- Guard is local and read-only; it does not contact the remote.
- TOCTOU is narrowed by an immediate recheck, not eliminated (no filesystem
  lock).
- Only Git workspaces are implemented; the binding seam is generic.
- No automatic fast-forward, worktree creation, cleanup, or repair.
- Direct adapter calls that bypass `TaskController` do not get the guard; the
  product execution path is controller-driven.
- Dirty workspaces always block in this goal, including read-only policies.
