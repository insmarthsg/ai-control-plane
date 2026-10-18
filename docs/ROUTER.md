# Thin Router Contract (GOAL-03)

Status: **frozen at ACP-GOAL-03**, baseline `be65994`.
This is the concrete routing contract. Architecture principles remain in
[ARCHITECTURE.md](ARCHITECTURE.md); task facts live in
[TASK_STATE.md](TASK_STATE.md).

## 1. Purpose

The Router answers exactly one question:

> Given existing facts and constraints, which candidate capability is currently
> allowed, and why?

It returns a `RoutingDecision` or a first-class `NoRoute`. It never executes
the selection.

**Router ≠ Orchestrator. Router ≠ Executor. Router ≠ Workflow Engine.**

Not in scope: executing capabilities, calling models, saving tasks, mutating
health, retry loops, failover loops, planning, review, Jev, human approval,
scheduling, UI, quota prediction, cost optimization.

## 2. Input: `RoutingRequest`

| Field | Meaning |
|---|---|
| `task_id` | Opaque task identity; read-only. |
| `required_capability_kind` | Opaque kind string (`execution`, `judgment`, …); no role-specific router. |
| `candidates` | Ordered tuple of `CapabilityCandidate`. |
| `constraints` | Explicit user `ALLOW` / `DENY` / `REQUIRE`. |
| `continuation_mode` | `NONE`, `CONTINUE`, or `HANDOFF`. |
| `continuation_ref` | Opaque reference from Task State; never interpreted. |
| `task_has_durable_state` | Whether handoff from Task State is possible. |
| `provider_health` | Read-only provider→status facts. |
| `judgment_signals` | Optional advisory signals (future Jev slot). |

There is no `workflow_stage`, `pipeline_step`, planner/worker/reviewer field.

## 3. Capability candidate

`CapabilityCandidate` (`core/capability.py`):

- `capability_id` — stable identifier; the Router's only selection key.
- `kind` — opaque role string.
- `availability` — `AVAILABLE | UNAVAILABLE`, separate from provider health.
- `provider` — opaque metadata; Router never branches on its value.
- `preference_rank` — deterministic priority; lower wins.
- `continuation_refs` — opaque refs this candidate can directly resume.
- `supports_task_state_handoff` — can start from durable Task State.
- `metadata` — opaque; never copied into a decision.

The contract is a frozen dataclass, not a plugin framework: no registry,
loader, DI container, invoke dispatch, or marketplace.

## 4. User constraints

`UserConstraints(allowed, denied, required)`.

- `DENY` wins over everything the user said; the capability is rejected with
  `USER_DENIED` even if it is healthy and preferred.
- `ALLOW`, when not `None`, is the complete set of permitted capabilities.
- `REQUIRE`, when non-empty, means only those capabilities may be selected.

**A user REQUIRE never authorizes silent substitution.** If the required
capability is unavailable, cooling down, auth-failing, or continuation-
incompatible, the result is `NoRoute` with the required capability listed as
an unsatisfied constraint — not a different capability.

## 5. Health gating

Health facts are read-only and per provider:

| Status | Routing effect |
|---|---|
| `AVAILABLE` | eligible, tier 0 |
| `DEGRADED` | eligible, tier 1 (below every AVAILABLE candidate) |
| `COOLDOWN` | rejected `PROVIDER_COOLDOWN` |
| `AUTH_REQUIRED` | rejected `PROVIDER_AUTH_REQUIRED` |
| `UNAVAILABLE` | rejected `PROVIDER_UNAVAILABLE` |
| missing / malformed | rejected `HEALTH_UNKNOWN` (fail closed for that candidate) |

Router never changes health, never retries, never predicts recovery from
`reset_at`, and never treats an unknown provider as healthy. A missing or
malformed fact rejects only that candidate; other candidates remain routable.

Capability availability and provider health are separate gates: a capability
can be `UNAVAILABLE` while its provider is `AVAILABLE`, and vice versa.

## 6. Continuation and handoff

- `CONTINUE`: the task's opaque `continuation_ref` must be listed in the
  candidate's `continuation_refs`. Otherwise `CONTINUATION_INCOMPATIBLE`.
  The Router does not parse a session id.
- `HANDOFF`: the task must have durable state and the candidate must declare
  `supports_task_state_handoff`; otherwise `HANDOFF_UNSUPPORTED`.
- `NONE`: no continuation gate.

The decision reports the resolved `mode`, so a handoff is never disguised as a
direct continuation. Router only classifies; it does not perform the handoff.

## 7. Selection priority (frozen)

1. user constraints (`DENY` / `ALLOW` / `REQUIRE`)
2. capability availability
3. provider health
4. continuation compatibility
5. deterministic policy: `health_tier`, then `preference_rank`, then
   advisory judgment score (tie only), then `capability_id`
6. judgment signal (future Jev)

Judgment can never override 1–4 or the rank in 5. It is advisory only.

## 8. Output

`RoutingDecision`:

- `task_id`, `selected_capability_id`, `mode`
- `decision_reason`
- `considered_candidates`
- `rejected_candidates` (`capability_id`, `reason`, `detail`)
- `policy_version`, `judgment_considered`

`NoRoute`:

- `task_id`, `requested_kind`, `mode`
- `considered_candidates`
- `rejected_candidates`
- `unsatisfied_constraints`
- `policy_version`, `judgment_considered`

Rejection reasons: `MALFORMED_CANDIDATE`, `KIND_MISMATCH`, `USER_DENIED`,
`USER_NOT_ALLOWED`, `NOT_REQUIRED`, `CAPABILITY_UNAVAILABLE`,
`PROVIDER_COOLDOWN`, `PROVIDER_AUTH_REQUIRED`, `PROVIDER_UNAVAILABLE`,
`HEALTH_UNKNOWN`, `CONTINUATION_INCOMPATIBLE`, `HANDOFF_UNSUPPORTED`,
`LOWER_PRIORITY`.

Every non-selected candidate appears in `rejected_candidates` with a reason.
No silent choices.

## 9. Determinism

Same request → same decision, selection, ordering, and rejection reasons.
Ordering: input order for `considered`/first-pass rejections; eligible
lower-priority candidates sorted by the frozen key. No randomness, clocks,
network, or global state.

## 10. Judgment boundary

`JudgmentSignal(candidate_id, score, confidence, source)` is accepted but used
only as the final tie-break after health tier and `preference_rank`. A high
score cannot revive a `COOLDOWN`, `AUTH_REQUIRED`, `UNAVAILABLE`,
`HEALTH_UNKNOWN`, unavailable, denied, or continuation-incompatible candidate.
Jev, when added, is one implementation behind this signal — not a router
decision-maker.

## 11. Read-only boundaries

- **Task State**: Router receives facts; it has no store, no write method, and
  no import from `stores/`.
- **Health**: Router receives a snapshot; it never mutates the tracker.
- **Execution**: no `execute()`; no loop; no retry/failover orchestration.
- **DSH**: no adapter import, no session/log knowledge.

## 12. Security

Decisions never include credential material. `capability_id`, `task_id`, and
rejection details pass through the existing `core.redaction` boundary;
candidate metadata is never copied into a decision.

## 13. Non-goals

Automatic failover loops, retry, execution, task persistence, health
persistence, planning, review, Jev calls, human approval, UI, queues,
schedulers, cost/budget optimization, and quota forecasting.

## 14. Verification

Covered by `tests/test_router.py`: preferred healthy selection, cooldown
fallback, REQUIRE→`NoRoute`, DENY, CONTINUE, HANDOFF, all-unavailable
`NoRoute`, provider isolation, determinism, judgment doesn't override hard
facts, degraded ranks below available, malformed/missing health isolation,
new fake provider, DSH-absent operation, Task State read-only, health
read-only, secret safety, and tie-breaking.
