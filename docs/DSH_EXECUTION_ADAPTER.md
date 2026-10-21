# DSH Execution Adapter (ACP-GOAL-04C)

Status: **implemented**, with GOAL-01 fail-closed enforcement. Earlier provider
smoke evidence is historical; real sandbox isolation remains **UNVERIFIED** here.
Contract: [CONTROLLER.md](CONTROLLER.md) · Seam audit: [DSH_EXECUTION_SEAM.md](DSH_EXECUTION_SEAM.md)

## 1. Runtime used

| Fact | Value |
|---|---|
| Runtime | `dsh --profile sdk` (stdio JSON-RPC), **CLI 0.1.5-rc.3** |
| `DSH_HOME` | `/Users/insmart/.dsh` (real credentials, real sessions) |
| Provider / model | `deepseek-official` / `deepseek-chat` |
| Process model | one runtime process per execution, terminated after every result/failure |

Not used: desktop Electron runtime, headless CLI, child tools, ACP (reserved
for turn-level cancel in a later goal).

## 2. Protocol

Wire: newline-delimited JSON-RPC 2.0 over the runtime's stdin/stdout.
Methods used by this adapter:

- `initialize {cwd, provider, model?}` — `model` omitted when empty so the
  route default applies (0.1.5 rejects `"undefined"` models).
- `session/prompt {sessionId, contentBlocks}` → `{messageId}`.
- `session/resume {sessionId}` — **not available in 0.1.5** (see §9).

**Protocol delta vs 0.2.1-rc.2 docs:** 0.1.5 has **no `session/wait`**.
Completion is therefore detected from the raw `turn/end` session event; the
runtime process is killed by the adapter afterwards either way. This path is
wire-compatible with 0.2.x, which re-adds `session/wait`/`session/resume`.

## 3. Execution identity

The bridge mints `execution_id` = opaque UUID per invocation and records:
`task_id`, `invocation_id`, `session_id`, `message_id`, `runtime_pid`, `pgid`,
`started_at`, `status`, provider/model/mode, `sandbox_modes` (all observed values),
`sandbox_mode` (last), `sandbox_consistent`, `permission_preset`,
`stdout_reader_joined`, `stdout_drained`, `process_group_cleared`,
`process_tree_verified=False`, `process_tree_status=UNVERIFIED`,
`require_process_tree`, `termination` — an in-memory registry only
(`active_executions()`, `get_execution()`, recent entries capped at 100).
`messageId` is stored as DSH's queue acknowledgement; it is never presented as
a turn/result id.

## 4. Modes

- **FRESH** — new UUID session id; prompt = objective (+context if present) +
  the bounded-execution instruction.
- **CONTINUE** — `continuation_ref` must be `dsh-session:<sessionId>`; the
  adapter strips the prefix and resumes/prompts that session.
- **HANDOFF** — brand-new session; prompt carries objective, durable context,
  artifacts, checkpoint, handoff reason, constraints. Never the old session id,
  never a transcript.

Access mode is separate from FRESH/CONTINUE/HANDOFF. The Controller derives
`ExecutionRequest.access_mode` from the workspace binding: `read_only=True`
means `READ_ONLY` (`read-only`); otherwise `WORKSPACE_WRITE` (`workspace-write`).
Direct group-only callers may supply the adapter's explicit `permission_mode` fallback.
Missing/invalid mode fails with `ACCESS_MODE_REQUIRED`; there is no writable
default. An explicit existing workspace directory is required, canonicalized
with `Path.resolve(strict=True)`; missing/invalid paths fail with
`WORKSPACE_PATH_REQUIRED`, without a constructor-cwd fallback.
`DSH_PERMISSION_MODE` is set after environment merging, so capability env
cannot override the request's binding-derived mode.

Prompt translation is a fixed, deterministic template (`_build_prompt`); no
planning/review logic lives in the adapter.

## 5. Result mapping

From real events (`assistant/message`, `turn/end`):

- `output` = concatenated text blocks of the last assistant message.
- `artifacts` = one `assistant-output` artifact carrying that text.
- `continuation_ref` = `dsh-session:<sessionId>` (opaque to core).
- `metadata.usage` = the event's token usage (real: `inputTokens 209`,
  `outputTokens 20`, `totalTokens 7525`, `cacheReadTokens 7296` on the smoke).
- `metadata.response_id` = `source.replayState.response.responseId` when the
  provider supplies it (real smoke: **absent** on this route → NOT_EXPOSED).
- `metadata.fallback_used = false` — the success path never scans
  `session.v4.jsonl.zstd`.

## 6. Completion semantics

`turn/end` alone does **not** mean the task is done. The smoke convention: the
executor ends its final message with the exact line
`ACP_EXECUTION_STATUS=COMPLETED`; the adapter then reports
`ExecutionStatus.COMPLETED`, otherwise `PROGRESS`. TaskController decides the
task lifecycle; the marker is an invocation-level convention, not a workflow
protocol. This result is provisional until cleanup and final sandbox validation
succeed. The marker is never safety evidence.

## 7. Error mapping

Evidence priority: runtime events (`llm/retry.failure`, `turn/end.error`) →
JSON-RPC error → bridge failure. DSH codes map through
`adapters.dsh.DSH_CODE_CATEGORIES`; a mappable code yields
`ExecutionFailure(kind=PROVIDER, provider_error=…, retryable=…)` so Provider
Health updates; everything else (`spawn failure`, protocol errors, timeout,
terminate, `TURN_ERROR`) stays `EXECUTION` and never touches health.
`providerRetryAfterMs` becomes `retry_after_seconds` when present.

GOAL-01 collects `permission/preset`, every `sandbox/mode`, and tool/turn error
codes. Cleanup terminates the group, then joins the stored stdout reader with
a one-second bound. Success requires a joined reader, normal EOF, no reader or
notification-delivery error, at least one mode, every mode exactly matching the
request, and no `SANDBOX_UNAVAILABLE`. Identical repeated modes are allowed;
conflicts before completion, during SIGTERM cleanup, or during cancel fail.
Permission preset is diagnostic, not a substitute for sandbox evidence.

Decision order:

1. Default `require_process_tree=True` returns `PROCESS_TREE_UNSUPPORTED`
   before spawning, even before access/workspace validation.
2. Explicit group-only mode validates access mode and workspace, spawns the
   runtime, and obtains a provisional result or protocol/provider/timeout error.
3. After cleanup and reader join, unconfirmed original-group cleanup wins with
   `PROCESS_GROUP_ORPHANED`; incomplete event draining wins next with
   `SANDBOX_EVENTS_UNDRAINED`.
4. All received `sandbox/mode` values plus `SANDBOX_UNAVAILABLE` in the
   supported tool/turn error shapes are rechecked: any unavailable error wins
   next, then any mode mismatch (`SANDBOX_MODE_MISMATCH`). Late non-sandbox
   errors, contradictory permission presets, and malformed JSON frames are not
   safety evidence and are not treated as one; only a fully drained reader can
   support success.
5. Accepted cancellation returns `TERMINATED_BY_REQUEST` if no higher-priority
   safety failure occurred. Final publication is serialized against cancel.
6. A provisional result without any mode becomes `SANDBOX_MODE_UNVERIFIED`.
   Existing failures (including early failures without mode evidence) stay
   failures; otherwise the provisional COMPLETED/PROGRESS result is returned.

Safety failures are `EXECUTION`, never provider-health failures. Final metadata
includes all modes and final event counts. `sandbox_consistent` describes mode
agreement only; a reported mode alone does not prove OS isolation worked.

## 8. Timeout

One overall deadline (`timeout_seconds`, default 180) covers
initialize→resume→prompt→turn-end. On expiry the runtime process is terminated
and `ExecutionFailure(category=EXECUTION_TIMEOUT, retryable=True)` is returned
unless a higher-priority final safety failure overrides it. Cleanup and the
bounded reader join run after this execution deadline.
Timeout is never a provider error.

## 9. Session persistence / CONTINUE boundary

Sessions persist in `$DSH_HOME/sessions` and the smoke session survived process
termination. However, **cross-process CONTINUE is blocked on CLI 0.1.5-rc.3**:

- `session/resume` does not exist (`unknown DeepSeek Harness SDK runtime
  method: session/resume`), and
- prompting the persisted session id on a new runtime fails with
  `session "<id>" already exists` (create-only persistence).

Cross-process CONTINUE therefore requires a DSH runtime that ships
`session/resume` (0.2.x). The adapter already speaks that method; no core
change is needed. Same-process continuation was deliberately **not** used to
fake the cross-process requirement.

## 10. Process isolation / cleanup / cancellation boundary

The adapter declares `process_tree_supported=False`. Its constructor defaults
to `require_process_tree=True`, which fails closed with
`ExecutionFailure(EXECUTION, "PROCESS_TREE_UNSUPPORTED")` before spawn. There
is no reliable full-tree owner in this executor. The Console declares DSH
specs `process_tree=False` and rejects them as `UNSUPPORTED_CHILD_TYPE` before
creating any Task or runtime. The `worker_kind=NONE` demo remains available.

Only explicit `require_process_tree=False` enables group-only execution,
including protocol and opt-in provider smoke tests. Every such spawned
execution starts a new session/process group (`start_new_session=True`).
Explicit terminate and `finally` cleanup target only that owned group with
`os.killpg`: TERM, bounded grace, then KILL. The leader is not reaped between
signals; polling and termination share a lock to preserve that identity.
After the final signal attempt the adapter reaps the child and requires
`killpg(pgid, 0)` to raise `ProcessLookupError` before recording
`process_group_cleared=True`. Signal errors alone never prove cleanup. Failure
to confirm cleanup returns `PROCESS_GROUP_ORPHANED`, overriding success.
`process_group_cleared=True` describes the original group only. All results and
failures report `process_tree_verified=False`, `process_tree_status=UNVERIFIED`;
this executor never claims full descendant cleanup.

If the leader is already dead, the saved pgid is not signalled: it may have
been reused. A remaining group produces `ORPHANED`/`KILL_UNCONFIRMED` evidence
and terminate returns false. The registry is in-memory: an adapter/server
crash loses ownership, so crash orphans require external/manual reconciliation.
Descendants can escape with `setsid()`/double-fork. macOS has no unprivileged
cgroup/job object available here; `kqueue`/libproc scanning provides observation
or best-effort cleanup, not an atomic ownership guarantee. Containers/VMs would
be a different executor and are not implemented. A future executor with a
reliable owner may implement the full-tree requirement; this one cannot.

Tests use real forked leader/child/grandchild processes to verify TERM cleanup,
KILL escalation, completion cleanup, dead-leader refusal, and cancellation
isolation between A and B. Real `setsid()` tests demonstrate a cleared original
group with a surviving escaped child, while verification stays false. An
escaped child retaining stdout prevents reader finality and fails closed.
SIGTERM-time mode/error tests exercise real event delivery during cleanup.
The SDK messages are synthetic and do not establish filesystem isolation.

## 11. Security

No credentials are read or logged by the adapter; the DSH runtime resolves
credentials inside `$DSH_HOME`. Prompt/event text passes the existing
`core.redaction` boundary, and Task State persistence redacts again.

## 12. Historical real provider verification evidence

- `REAL_SMOKE` (FRESH, `deepseek-official/deepseek-chat`): output contained
  `ACP_DSH_SMOKE_OK` + marker; usage real; `turn_end=completed`.
- `REAL_CONTROLLER`: TaskController → Router → adapter → DSH → Task State;
  task reached `COMPLETED` at revision 4 with the model's answer persisted as
  an artifact.
- `REAL_CONTINUE`: BLOCKED by CLI version (see §9).

## 13. Known gaps

- Real DSH sandbox isolation remains **UNVERIFIED** on this host. The approved
  audit observed `sandbox_apply: Operation not permitted` and a real
  `SANDBOX_UNAVAILABLE` tool result despite a completed turn. GOAL-01 tests
  verify fail-closed handling with a fake SDK peer; that audit was not rerun.
- Process ownership is POSIX group scoped and in-memory; see the dead-leader,
  escaped-descendant and crash-orphan boundaries above.

- No cross-process CONTINUE until the DSH runtime exposes `session/resume`.
- No turn-level cancel over SDK; `terminate()` is process-level.
- `response_id` not exposed by the deepseek-official route.
- Completion marker is a smoke convention; richer completion contracts belong
  to later goals.
- `adapters/dsh_execution.py` imports the code table from
  `adapters/dsh.py`; deleting the session-log adapter module would require
  moving that table.
