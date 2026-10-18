# DSH Execution Adapter (ACP-GOAL-04C)

Status: **implemented and real-verified**, baseline `0284ec5` → this commit.
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
`invocation_id`, `session_id`, `message_id`, `runtime_pid`, `started_at`,
`status`, provider/model/mode — an in-memory registry only
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
protocol.

## 7. Error mapping

Evidence priority: runtime events (`llm/retry.failure`, `turn/end.error`) →
JSON-RPC error → bridge failure. DSH codes map through
`adapters.dsh.DSH_CODE_CATEGORIES`; a mappable code yields
`ExecutionFailure(kind=PROVIDER, provider_error=…, retryable=…)` so Provider
Health updates; everything else (`spawn failure`, protocol errors, timeout,
terminate, `TURN_ERROR`) stays `EXECUTION` and never touches health.
`providerRetryAfterMs` becomes `retry_after_seconds` when present.

## 8. Timeout

One overall deadline (`timeout_seconds`, default 180) covers
initialize→resume→prompt→turn-end. On expiry the runtime process is terminated
and `ExecutionFailure(category=EXECUTION_TIMEOUT, retryable=True)` is returned.
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

Every execution spawns its own runtime and `finally: runtime.terminate()`
kills it — success, failure, timeout, or external terminate. No orphan DSH
process survives; the session record does. `terminate(execution_id)` and the
`execution_id → runtime pid` mapping exist today as the real seam GOAL-05 will
formalize. The registry is in-memory only and cleans itself.

## 11. Security

No credentials are read or logged by the adapter; the DSH runtime resolves
credentials inside `$DSH_HOME`. Prompt/event text passes the existing
`core.redaction` boundary, and Task State persistence redacts again.

## 12. Real verification evidence

- `REAL_SMOKE` (FRESH, `deepseek-official/deepseek-chat`): output contained
  `ACP_DSH_SMOKE_OK` + marker; usage real; `turn_end=completed`.
- `REAL_CONTROLLER`: TaskController → Router → adapter → DSH → Task State;
  task reached `COMPLETED` at revision 4 with the model's answer persisted as
  an artifact.
- `REAL_CONTINUE`: BLOCKED by CLI version (see §9).

## 13. Known gaps

- No cross-process CONTINUE until the DSH runtime exposes `session/resume`.
- No turn-level cancel over SDK; `terminate()` is process-level.
- `response_id` not exposed by the deepseek-official route.
- Completion marker is a smoke convention; richer completion contracts belong
  to later goals.
- `adapters/dsh_execution.py` imports the code table from
  `adapters/dsh.py`; deleting the session-log adapter module would require
  moving that table.
