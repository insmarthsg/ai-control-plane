# DSH Execution Runtime Seam Audit (ACP-GOAL-04B)

Status: **read-only audit**, baseline `1cdd3de`. No adapter was implemented.
Related contract: [CONTROLLER.md](CONTROLLER.md).

## 1. DSH versions and locations

There are two DSH installations in this environment, and they are different
builds:

| Role | Version | Location |
|---|---|---|
| CLI on `PATH` (`dsh`) | **0.1.5-rc.3** | `/Users/insmart/.nvm/versions/node/v25.8.0/lib/node_modules/@deepseek-ai/dsh` |
| Running desktop app runtime | **0.2.1-rc.2** | `DeepSeek Harness.app/Contents/Resources/app.asar` (`@deepseek-ai/dsh-desktop-runtime`) |

`dsh --version` → `0.1.5-rc.3`. The desktop app cannot be driven by the CLI:
`dsh --profile desktop --dump-config` answers *"profile 'desktop' is managed
exclusively by the Electron application"*. A bridge spawned through `dsh`
therefore targets **0.1.5-rc.3**, not the 0.2.1-rc.2 app. Both ship the same
session format line and the same SDK/ACP packages at their own versions.

## 2. Discovered interfaces

### A. CLI (one-shot / profiles)

Installed CLI help:

```
dsh --help
  web | plugin | --profile <name> [args]

dsh --profile headless "<task>"
  Answer one task, stream reasoning to stderr, print the final assistant
  message, and exit.

dsh --profile sdk
  Serve DeepSeek Harness SDK clients over stdio JSON-RPC.

dsh --profile acp
  Serve automation clients over Agent Client Protocol stdio.
```

Only **headless** is a task CLI. `sdk` and `acp` are long-lived stdio servers.
Headless exposes no JSON output, no execution id, no status, no cancel, no
resume. It is not a control surface.

### B. Programmatic SDK runtime (`@deepseek-ai/dsh-sdk-jsonrpc-server`)

Newline-delimited JSON-RPC 2.0 over stdio, mounted by `dsh --profile sdk` (or
`sdk-minimal`). Contract in `dsh-sdk-protocol`. Methods:

| Direction | Method |
|---|---|
| client→server | `initialize` |
| client→server | `session/prompt` |
| client→server | `session/resume` |
| client→server | `session/wait` |
| client→server | `session/working-directory/get|set` |
| client→server | `shutdown` |
| server→client | `session.event` (raw `SessionEvent`) |
| server→client | `session.status` (`running`/`idle`) |
| server→client | `subagent.started` / `subagent.finished` |

The protocol has **no cancel, no session close, and no per-prompt result**.
The documented abandonment path is closing the runtime process. An official
Python SDK exists upstream but is **not installed** here (`dsh_sdk` import
fails); the TypeScript SDK is not shipped in the app.

Verified with a live handshake against the installed CLI under an isolated
`DSH_HOME` (no model call):

```
→ {"jsonrpc":"2.0","id":1,"method":"initialize",
   "params":{"cwd":"...","provider":"deepseek-official","model":"deepseek-flash"}}
← {"result":{"serverInfo":{"name":"deepseek-harness-sdk-runtime",
                           "version":"0.0.1"}}}
→ {"method":"shutdown"}
← {"result":{}}
```

### C. Programmatic ACP runtime (`@deepseek-ai/dsh-acp`)

Standard **Agent Client Protocol v1** over stdio, mounted by
`dsh --profile acp`. Standard surface:

| Method | Meaning |
|---|---|
| `initialize` | protocol v1, `session/list`, `session/resume`, `session/close` |
| `session/new` | create a persistent agent with cwd + MCP declarations |
| `session/prompt` | one prompt, settles with `stopReason` after activity + updates |
| `session/update` | committed assistant/thought text, tool lifecycle, usage, config |
| `session/cancel` / `$/cancel_request` | turn-level cancel |
| `session/close` | quiescent cancel, drain, descendant disposal, persistence flush |
| `session/resume` | reopen an inactive persisted session |
| `session/list` | list resumable root sessions |

ACP deliberately does **not** carry DSH-specific presentation data, raw
provider deltas, or retry events. `turnEndToStopReason` maps harness
`completed`→`end_turn`, `interrupted`→`cancelled`, `max-tokens`→`max_tokens`,
and **`blocked`/`error`→`end_turn`** (`dsh-acp/lib/index.js`, codec). ACP
therefore loses provider-error identity.

### D. Child / agent tools (internal, not an external API)

`interrupt_agent` and `send_message` are model-facing tools over
`ctx.subagents` (`dsh-tool-subagent-control`). `subagent` providers support
two modes:

- **local spawn/fork** — persistent child Session; accepts messages; can be
  resumed; child id is a durable session id.
- **external** (Codex, ACP, SDK) — one-shot run, no local Session, no
  follow-ups; catalog entry `mode: 'external'`.

There is no RPC/CLI to call `interrupt_agent` or `send_message` from outside an
agent turn; they are only reachable through the model tool registry.

### E. Session log

Persisted at `$DSH_HOME/sessions/**/session.v4.jsonl.zstd`; event vocabulary is
`KNOWN_SESSION_EVENT_TYPES` (includes `turn/start`, `tool/call`, `tool/result`,
`assistant/message`, `llm/retry`, `turn/end`, `subagent/*`). This is the only
surface that carries raw DSH error codes and provider retry evidence, and it is
already quarantined in `adapters/dsh.py` as `FALLBACK_ONLY`.

## 3. Execution matrix

### SDK JSON-RPC (`dsh --profile sdk`)

| Capability | Available | Interface | Stable? | Evidence |
|---|---|---|---|---|
| START | YES | `session/prompt` | PARTIAL | `sdk-server.js` prompt queues `agent.followup` |
| EXECUTION_ID | PARTIAL | `session/prompt` → `messageId` | PARTIAL | messageId identifies queued user message, explicitly not the turn/result |
| RUNNING_STATUS | PARTIAL | `session.status` running/idle | PARTIAL | whole-agent status only; tool/waiting phases only derivable from events |
| FINAL_RESULT | PARTIAL | `session.event` + `session/wait` | PARTIAL | no result DTO; assemble assistant messages + `turn/end` |
| STRUCTURED_ERROR | PARTIAL | `session.event` (`turn/end`, `llm/retry`) | PARTIAL | raw event codes available; `wait` rejection loses code |
| INTERRUPT | NO | — | — | protocol has no cancel/close; kill process is the documented path |
| CONTINUE | YES | `session/resume` | PARTIAL | persisted session id, same `DSH_HOME`, cross-process |
| SESSION_PERSISTENCE | YES | jsonl | YES | `session-persistence-jsonl` |
| EVENT_STREAM | YES | `session.event` | PARTIAL | every session event, unfiltered |
| USAGE | PARTIAL | `session.event` assistant usage | PARTIAL | provider-dependent token fields |
| RESPONSE_ID | PARTIAL | assistant message `source.replayState.response.responseId` | PARTIAL | only when provider supplies it |

### ACP v1 (`dsh --profile acp`)

| Capability | Available | Interface | Stable? | Evidence |
|---|---|---|---|---|
| START | YES | `session/new` + `session/prompt` | YES | ACP v1 |
| EXECUTION_ID | PARTIAL | `sessionId` only | PARTIAL | no turn/attempt id on the wire |
| RUNNING_STATUS | PARTIAL | pending prompt + `session/update` | PARTIAL | no explicit running/waiting enum |
| FINAL_RESULT | PARTIAL | `session/prompt` → `stopReason` + updates | YES | result is stopReason, text arrives as updates |
| STRUCTURED_ERROR | NO | — | — | `error`/`blocked` collapse to `end_turn` |
| INTERRUPT | YES | `session/cancel`, `session/close` | YES | `agent.cancel({kind:'user'})`, waits for idle |
| CONTINUE | YES | `session/resume` | YES | persisted inactive session, cross-process |
| SESSION_PERSISTENCE | YES | jsonl | YES | same store |
| EVENT_STREAM | YES | `session/update` | YES | semantic only; no raw provider events |
| USAGE | PARTIAL | `usage_update` | YES | context occupancy, not input/output token counts |
| RESPONSE_ID | NO | — | — | not in ACP surface |

### Other surfaces

| Surface | START | EXEC ID | STATUS | FINAL | ERROR | INTERRUPT | CONTINUE |
|---|---|---|---|---|---|---|---|
| headless CLI | YES (one-shot) | NO | NO | YES (text) | NO | NO | NO |
| child tools (internal) | YES | YES (child session) | PARTIAL | YES (closing message) | NO | YES | local only |
| session log | — | session id | NO | events | PARTIAL | — | — |

## 4. Interrupt path (code evidence)

ACP:

```
session/cancel
  → AgentRecord.cancel()                      (dsh-acp/lib/index.js)
  → this.agent.cancel({ kind: "user" })
  → ReactLoopAgent.cancel() aborts phase.abort (dsh-agent-loop/lib/index.js)
  → waits agent.whenIdle() + waitForChildren, resolves stopReason "cancelled"
```

`session/close` additionally drains updates, disposes descendants, flushes
persistence, and releases only that session's agent scope. The process stays
alive; the session remains resumable.

`interrupt_agent` (not externally callable):

```
ctx.subagents.interrupt(target, {kind:'ancestor', agent: caller})
  → local:  activation.handle.agent.cancel({kind}, {keepInbox:true})
  → external: activation.controller.abort({kind}) + dispose(activation)
```

Verdict: DSH cancellation is a **real turn abort**, not just a status flip. It
aborts the agent-loop signal consumed by model/tool work and waits for
settlement. It is not a process kill; `session/close` is the stronger scope
release. The SDK has no equivalent: only process termination stops it.

## 5. Status path

- SDK: `agent/status` transitions only `running`/`idle`; events carry
  `turn/start`, `tool/call`, `tool/result`, `turn/end`.
- ACP: prompt stays pending; `session/update` streams tool lifecycle and text.
- Neither exposes `STARTED / WAITING / TOOL_EXECUTION / COMPLETED / FAILED` as
  an explicit runtime state. **NOT_EXPOSED** as a first-class enum; a bridge
  must derive phases from events, and should label them derived.

## 6. Continuation path

- SDK/ACP `session/resume` reopens a **persisted, inactive** session by id.
  It requires the same `$DSH_HOME` and rejects an already-open session.
  Another process can list/resume after the previous process exits.
- Session history is restored; old notifications are not replayed.
- `CONTINUE` = resume the same session and send another prompt.
- `HANDOFF` = new session with a minimal Task State prompt; never pretend it is
  the same continuation.
- Continuable child vs external child: a local spawn/fork child has a durable
  Session, so its usage/responseId/events are observable and it can be resumed;
  an external child has no local Session (`mode: 'external'`), only a final
  message/catalog fact, so it cannot be inspected or continued.

## 7. Error path

- Best structured source: SDK `session.event` — raw `turn/end` error code and
  `llm/retry.failure` (including `providerRetryAfterMs` when populated).
- ACP loses provider errors (`error`/`blocked` → `end_turn`).
- `session/wait` rejects with a JSON-RPC error message; it does not carry a
  `HarnessError.code` or structured data.
- The session log remains the only complete fallback for provider codes and
  retry evidence; it is not a substitute for a live API.

## 8. Options compared

| Option | Thin | Start | Status | Final | Error | Cancel | Continue | Risk |
|---|---|---|---|---|---|---|---|---|
| A headless subprocess | high | one-shot | no | text | no | no | no | text parsing, no control |
| B SDK runtime | **highest** | yes | coarse | assemble | raw events | **no** | yes | kill-only cancel |
| C ACP runtime | medium | yes | updates | stopReason | lossy | **yes** | yes | ACP client surface |
| D SDK + session log | medium | yes | coarse | assemble | good | no | yes | no cancel |
| E ACP + session log | medium | yes | updates | stopReason | enriched | yes | yes | two surfaces, coupling |
| F child/session API | low | internal | partial | message | no | yes | local | not externally callable |

## 9. Recommended bridge

Recommendation: **SDK JSON-RPC as the primary transport**, one DSH runtime
process per active execution or per task, with a bridge-owned `execution_id`
mapping to `(sessionId, messageId)`.

- `START` = `initialize` + `session/prompt`; persist mapping before returning.
- `STATUS` = `session.status` + event-derived phases; label phases derived.
- `FINAL` = collect `session.event` until `session/wait` resolves; assemble
  assistant text and `turn/end`.
- `CONTINUE` = `session/resume` + new prompt on the same session id.
- `HANDOFF` = new session with objective/context/artifacts/checkpoint only.
- `CANCEL` (interim) = terminate the dedicated runtime process; the session
  remains resumable. Turn-level cancel is **not** available over SDK.
- If turn-level cancel in a shared runtime becomes mandatory (GOAL-05), add an
  **ACP control path** and accept stopReason-only errors, enriched from the
  same session log (Option E).

Do not use headless as a control bridge. Do not call child tools as an API.
Do not treat `messageId` as an execution id; it is a queue receipt.

## 10. Stability risk

- DSH is pre-release; CLI (`0.1.5-rc.3`) and app runtime (`0.2.1-rc.2`) differ.
- SDK handshake has **no version negotiation** (`serverInfo.version 0.0.1`,
  client does not validate); methods can change.
- ACP is a stable standard, but DSH's mapping is lossy (errors → `end_turn`)
  and ACP is automation-only (no UI-specific data).
- SDK has no cancel/close; process kill is coarse and terminates every session
  in that runtime.
- Event payloads depend on `dsh-session` vocabulary and `dsh-llm` content
  blocks; `responseId`/usage are provider-dependent.
- The upstream Python SDK is not installed; a bridge must vendor or implement
  the ~7-method line-JSON client.

## 11. Missing capabilities

Post-audit correction (confirmed by GOAL-04C against the installed CLI):
0.1.5-rc.3 also **lacks `session/wait` and `session/resume`** — those methods
exist only in the 0.2.1-rc.2 app runtime documented above. Consequences:
completion must be derived from `turn/end` events, and cross-process CONTINUE
is impossible on 0.1.5 (`session "<id>" already exists` on duplicate prompt).

- No native execution/turn/attempt id at start (only session id + message id).
- No explicit WAITING / TOOL_EXECUTION status.
- No cancel/close over SDK.
- No raw provider error over ACP.
- No external API for `interrupt_agent` / `send_message` / `list_agents`.
- No pause API in any surface (cancel + resume is the available substitute).
- No protocol version negotiation.

## 12. Next action

1. Decide whether a **bridge-owned execution id** is acceptable for the
   Controller (`invocation_id` ↔ sessionId + messageId); if not, upstream must
   expose a turn/attempt id.
2. Decide the runtime model: process-per-execution (simple, kill-cancel) vs
   shared ACP runtime (turn-cancel, lossy errors).
3. Then implement the DSH adapter behind the existing Execution Contract.
   Recommended first slice: SDK bridge + process-per-execution + session
   resume; ACP upgrade when GOAL-05 needs turn-level cancel.

No prototype was implemented because `EXECUTION_ID = PARTIAL`; the prototype
gate in the task requires `EXECUTION_ID = YES`.
