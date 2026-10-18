# ai-control-plane

**AI Control Plane = Task Control Layer. DSH = Execution Harness.**

This repository is the thin layer that decides *what to do* when an execution
harness reports that a provider is unhealthy. Today it contains exactly one
goal's worth of code: provider error classification and provider health state.
It is intentionally not an orchestrator, router, scheduler, UI, or database.

**Contracts:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (ACP-ARCH-01),
[docs/TASK_STATE.md](docs/TASK_STATE.md) (GOAL-02),
[docs/ROUTER.md](docs/ROUTER.md) (GOAL-03),
[docs/CONTROLLER.md](docs/CONTROLLER.md) (GOAL-04), and the read-only
[docs/DSH_EXECUTION_SEAM.md](docs/DSH_EXECUTION_SEAM.md) audit (GOAL-04B).
They are normative; read them before adding capabilities.

## Status: GOAL-01 … GOAL-06 + GOAL-07 (observability/recovery)

Implemented:

- `ProviderError` — eight categories, credential-safe, evidence-based
  `retry_after_seconds` / `reset_at`.
- `ProviderHealthTracker` / `get_provider_health(provider)` — five states,
  isolated per provider.
- `adapters/dsh.py` — **FALLBACK_ONLY** reader for DSH session logs
  (`session.v4.jsonl.zstd`), marked `TEMPORARY_INTERNAL_INTERFACE`.
- `core/task.py` + `core/task_store.py` + `stores/json_task_store.py` —
  persistent Task State: opaque identity, durable context, artifacts, execution
  cursor, interruptions, insertion frames, revision-checked updates.
- `core/capability.py` + `core/router.py` — Thin Router: deterministic,
  explainable selection over user constraints, capability availability,
  provider health, and continuation compatibility. It never executes anything.
- `adapters/dsh_execution.py` — real DSH execution: drives `dsh --profile sdk`
  (bridge-owned execution id, process-per-execution, event completion,
  timeout/terminate seam). Real provider smoke verified on
  `deepseek-official/deepseek-chat`; cross-process CONTINUE needs DSH 0.2.x
  (`session/resume`).
- `core/execution.py` + `core/controller.py` — bounded Task Controller:
  one `advance()` loads state, routes, invokes exactly one execution
  capability, persists the result, and returns a structured outcome.
- `core/runtime.py` — Runtime Control: non-blocking `start`, `status`,
  confirmed `cancel`, LOST detection, event trace. Runtime cancel stops only
  the invocation; Task lifecycle and Provider Health are untouched.
- `core/human.py` — insertable Human Gate: durable APPROVE / REJECT / MODIFY,
  insertion-stack cursor restoration, no timeout, no automatic approval.
- `core/observation.py` + `core/recovery.py` — read-only Task/runtime view,
  ordered timeline, evidence-based recovery classification (`ACTIVE`,
  `WAITING_HUMAN`, `RECOVERABLE`, `HANDOFF_POSSIBLE`, `BLOCKED`, `ORPHANED`,
  `UNCERTAIN`, `TERMINAL`, `LOST`) with recommended actions only.
- `tests/test_architecture.py` — frozen-boundary invariants.

Architecture in one line: **capabilities, not workflows**; the Stable Core is
only task identity, the capability contract, the event/state contract, and the
composition boundary; every harness/vendor detail stays behind an adapter.

Task State is **not** workflow state: it records facts (including an execution
cursor and temporary insertion frames) and never decides what runs next. It is
persisted per task, survives process restart, and is independent of provider,
model, session, and executor.

The Router is **not** an orchestrator: it returns a decision or `NoRoute`, with
per-candidate reasons. It does not execute, retry, fail over, or write state.

The Task Controller is **not** a workflow engine: `advance()` performs one
bounded step and returns. Provider failures update Health and let the caller
advance again; execution/environment failures block the task without poisoning
provider health.

Not implemented (later goals, by design):

- automatic failover, thin router, provider priority
- task state store
- UI / web server / database / scheduler
- quota prediction or credential refresh

## Quick start

```python
from core.errors import classify_provider_error
from core.provider_health import ProviderHealthTracker

tracker = ProviderHealthTracker()

error = classify_provider_error(
    "openai-codex",
    message="429 Too Many Requests",
    http_status=429,
    headers={"Retry-After": "42"},
)
tracker.record_error(error)
tracker.get("openai-codex").to_dict()
# {'provider': 'openai-codex', 'status': 'COOLDOWN', 'reason': '429 Too Many Requests',
#  'since': '...Z', 'retry_after_seconds': 42.0, 'reset_at': None}

tracker.record_success("openai-codex")
tracker.get("openai-codex").status.value  # 'AVAILABLE'
```

Reading a real DSH session (fallback path):

```python
from adapters.dsh import read_session_summary, record_session_health
from core.provider_health import ProviderHealthTracker

summary = read_session_summary("/path/to/session.v4.jsonl.zstd")
tracker = ProviderHealthTracker()
record_session_health(tracker, "/path/to/session.v4.jsonl.zstd")
```

## Evidence rules

- A `429` is `RATE_LIMIT` unless the payload explicitly names exhausted
  quota/balance/credit/budget; then it is `QUOTA_EXHAUSTED`.
- `retry_after_seconds` and `reset_at` are populated **only** from explicit
  evidence (`Retry-After`, `Retry-After-Ms`, DSH `providerRetryAfterMs`).
  No 5-hour default is ever invented; missing evidence stays `null`.
- Never-observed providers are `None`, not `AVAILABLE`. Health is evidence,
  not optimism.
- `openai-codex` entering `COOLDOWN` must not change `zai-coding-cn`.

## DSH interface status

- **OFFICIAL DSH INTERFACE FOUND: PARTIAL.** DSH has canonical error codes
  (`HarnessError.code`, `QUOTA`, `CONTEXT_WINDOW_EXCEEDED`, …) and an
  `agent/request-error` hook with `failure.providerRetryAfterMs`, but nothing
  is exported to an external process.
- **SESSION LOG DEPENDENCY: FALLBACK_ONLY.** The adapter anchors on official
  events (`request/header`, `llm/retry`, `turn/end`) rather than free text.
  If DSH ships an external error/health API, delete the fallback parser.

## Credential policy

No secret ever reaches an error, a health record, a log line, or a test
fixture. `core.errors.redact()` strips Bearer tokens, `sk-…` keys, JWTs,
`api_key`/`token` assignments and `accountId` values at the construction
boundary.

## Tests

```bash
python3 -m pytest -q
```

GOAL-01 scenarios, ACP-ARCH-01 boundary invariants, GOAL-02 continuity tests
(cross-process recovery, executor handoff, insertion nesting, concurrency,
atomic-write failure, secret non-persistence), and GOAL-03 routing tests
(constraints, health gates, continuation/handoff, NoRoute, determinism,
judgment boundary), and GOAL-04 controller tests (bounded advance, CONTINUE,
HANDOFF, provider-failure handoff, execution-failure isolation, NoRoute,
revision conflict, persistence failure) are all covered in `tests/`.
