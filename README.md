# ai-control-plane

**AI Control Plane = Task Control Layer. DSH = Execution Harness.**

This repository is the thin layer that decides *what to do* when an execution
harness reports that a provider is unhealthy. Today it contains exactly one
goal's worth of code: provider error classification and provider health state.
It is intentionally not an orchestrator, router, scheduler, UI, or database.

**Architecture contract: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**
(ACP-ARCH-01, frozen). It is normative; read it before adding capabilities.

## Status: GOAL-01 + ACP-ARCH-01 (contract)

Implemented:

- `ProviderError` — eight categories, credential-safe, evidence-based
  `retry_after_seconds` / `reset_at`.
- `ProviderHealthTracker` / `get_provider_health(provider)` — five states,
  isolated per provider.
- `adapters/dsh.py` — **FALLBACK_ONLY** reader for DSH session logs
  (`session.v4.jsonl.zstd`), marked `TEMPORARY_INTERNAL_INTERFACE`.
- `tests/test_architecture.py` — frozen-boundary invariants.

Architecture in one line: **capabilities, not workflows**; the Stable Core is
only task identity, the capability contract, the event/state contract, and the
composition boundary; every harness/vendor detail stays behind an adapter.

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

Ten required scenarios are covered in `tests/`, plus adapter fixture tests,
core-isolation checks, and the ACP-ARCH-01 boundary invariants.
