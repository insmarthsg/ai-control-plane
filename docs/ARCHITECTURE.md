# AI Control Plane — Architecture Contract

Status: **ACP-ARCH-01, frozen**. Baseline commit `e9d1730`.
This document is normative. Code that contradicts it is a defect, not a precedent.
Concrete contracts: [TASK_STATE.md](TASK_STATE.md) (GOAL-02),
[ROUTER.md](ROUTER.md) (GOAL-03), [CONTROLLER.md](CONTROLLER.md) (GOAL-04),
[DSH_EXECUTION_ADAPTER.md](DSH_EXECUTION_ADAPTER.md) (GOAL-04C).

Scope of this document: boundaries and invariants only. It deliberately does
not implement Task State, a router, a registry, a bus, a plugin runtime, or Jev.

---

## 1. Product / System Goal

AI Control Plane is the **task control layer**. An execution harness (today:
DSH) is the **execution engine**. The control plane accepts a goal, composes
whatever capabilities the goal needs, and observes/records what happened.

It is not a fixed software-development pipeline. Planned uses include software
work, research, review, ordinary AI tasks, and domains not yet known.

Therefore the architecture must let capabilities appear, disappear, and be
replaced without redesigning the whole system.

---

## 2. Stable Core

The Stable Core is intentionally tiny. It contains only what cannot be removed
without losing the identity of a "task" at all.

| Stable Core element | Why it is unavoidable |
|---|---|
| **Task identity** | Opaque `task_id` plus lifecycle; no stage names, no workflow shape. |
| **Capability contract** | A capability must be describable (id, kind, availability, invoke/failure semantics) for composition to be possible. |
| **Event / state transition contract** | Insertion, pause, resume, and audit require a neutral record of what happened. Shape is frozen in TASK_STATE.md. |
| **Composition boundary** | A task must be able to reference capabilities and insertion points without the core knowing concrete implementations. |

Explicitly **not** Stable Core:

- Provider Health, error classification, retry policy
- DSH, Codex, OpenAI, GLM, DeepSeek, any provider SDK
- Jev or any judgment model
- Router, Planner, Reviewer, Verifier
- UI, web server, database, scheduler, queue
- any fixed workflow

Code evidence: `core/provider_health.py` imports only `core.errors`; it is
keyed by an opaque provider string and can be deleted without changing task
identity. Provider health is therefore a **capability**, not Stable Core.

No abstraction is added "for completeness". A new Stable Core element requires
an explicit architecture revision.

---

## 3. Capability Model

A capability is a replaceable unit of behaviour with a declared contract.

At contract level a capability declares:

- `id` — stable, namespaced (e.g. `execution.dsh`, `judgment.jev`)
- `kind` — role, not stage: `execution | judgment | state | health | router |
  human | observability`
- `availability` — the capability instance's own readiness, distinct from
  provider health
- `invoke(input, context) -> result | failure`
- failure semantics — local and typed; never silently converted into another
  capability's failure

Capabilities are addressed through the composition boundary, not by importing
each other. A capability must not reach into another capability's internals.

**No plugin runtime is frozen.** GOAL-03 earned exactly one minimal contract:
`CapabilityCandidate` in `core/capability.py`, because the Router needs a
concrete candidate to select. It is still a frozen dataclass — not a registry,
bus, loader, DI container, service locator, or workflow DSL, and there is no
invoke dispatch.

`kind` is a role label. It does not imply ordering. `router` is listed only
because routing may be a capability; routing is not part of the Stable Core.

---

## 4. Capability ≠ Workflow

Hard constraint.

The system provides capabilities. Workflows are runtime compositions of those
capabilities, formed per task. A workflow must never become the system
skeleton.

Forbidden as architecture:

```
Planner → Worker → Reviewer → Verifier
```

Allowed as one possible composition:

```
Task → Execution → Result
```

```
Task → Judgment → Planning → Execution
     → [insert Review] → Resume → Verification → Result
```

```
Task → Execution → [insert Human Gate] → Resume → Result
```

Simple tasks must remain simple. Adding a stage for one task must not force the
stage onto every other task.

---

## 5. Plugin Boundary

Anything vendor- or harness-specific lives behind an adapter/plugin boundary:

- DSH session log format, zstd, DSH error-code vocabulary
- CLI invocation details of Codex / ZCode / remote agents
- OpenAI / GLM / DeepSeek wire formats and auth
- provider credentials storage

Direction of dependency is one-way:

```
adapter / plugin  ──▶  core contract
core              ──▶  nothing vendor-specific
```

Adapters translate foreign evidence into neutral core inputs. Core exports
types and contracts; it never imports adapters or recognises vendor vocabulary.
ACP-ARCH-01 fixed the one place where this had been violated: the harness error
code table now lives in `adapters/dsh.py`, and core accepts a neutral
`category_hint`.

A plugin is not a microservice, package manager, or marketplace. Today a
"plugin" is a module behind a boundary, wired explicitly.

---

## 6. Insertability

A running task must be able to have a capability inserted temporarily:

```
Execution ──▶ risk detected ──▶ INSERT Review ──▶ resume Execution
Execution ──▶ approval needed ──▶ INSERT Human Gate ──▶ resume Execution
Research  ──▶ conflicting evidence ──▶ INSERT Judgment ──▶ resume Research
```

Contract requirements (modeled by GOAL-02 in TASK_STATE.md):

Task State must be able to express

- the current main path
- insertion source (where the interruption came from)
- inserted capability and its input
- insertion result
- a resume point that is meaningful without replaying chat history

Invariants:

- inserting a capability must not rewrite already completed steps
- resuming must not require the inserted capability to still be available
- insertion is composition, not a pre-declared workflow branch

The concrete Task State contract is frozen in [TASK_STATE.md](TASK_STATE.md); this document stays principles-only.

---

## 7. Horizontal Extension

Adding another instance of the same kind must be local:

- OpenAI, GLM, DeepSeek, Claude, future provider → add provider/adapter instance
- Jev, future judgment model, LLM judgment, rule judgment → add judgment instance
- Reviewer A / Reviewer B → add review instance
- DSH / future harness A / B → add execution adapter

No Task Core change. No existing capability change. No provider-name branch in
core. Registration/wiring is the only composition point that may be touched.

---

## 8. Vertical Evolution

A task must be able to evolve from simple to complex without rewriting the
simple path:

```
V1  Task → Execution
V2  Task → Execution → Review
V3  Task → Judgment → Execution → Review
V4  Task → Planning → Execution → [Human Gate] → Resume → Verification
```

New layers are added by composition. `V1` remains a valid task shape forever.
Vertical evolution must not introduce mandatory stages, mandatory judgment, or
a global pipeline object.

---

## 9. Replaceability

Every non-core capability should be replaceable behind its contract:

| From | To | Touches |
|---|---|---|
| DSH | future harness | `adapters/dsh.py` + wiring |
| Jev | future judgment model | judgment adapter + wiring |
| health persistence A | persistence B | storage adapter |
| UI A | UI B | UI adapter |
| memory impl A | memory impl B | memory adapter |
| task store backend A | backend B | `stores/` implementation |

Replacing one capability must not require migrating Task State, Router,
Judgment, Human Control, or UI. If it does, the boundary is wrong.

---

## 10. Local Change Invariant

`LOCAL_CHANGE_INVARIANT`:

> Adding, replacing, deleting, or failing a non-Stable-Core capability may only
> affect itself, its explicitly declared interface seam, the necessary
> composition point, and its tests. It must not force unrelated capabilities to
> change.

Operational test for every new capability:

> "If this capability is removed entirely, do the other unrelated capabilities
> still hold?"

If **no**, the design is over-coupled and must be revisited. The only permitted
exception is an explicit Stable Core contract change, which requires an
architecture revision and a migration note.

Enforced by `tests/test_architecture.py`.

---

## 11. Failure Isolation

`Capability failure ≠ Control Plane failure.`

- A judgment capability being down must not make tasks unrunnable; the
  composition may skip optional judgment or use another judgment capability.
- An execution adapter being down must not corrupt Task State or the core; the
  task fails or pauses at the execution boundary.
- Health being unavailable must not invent health facts.
- Fallback policy belongs to composition/router (later goal), not to each
  capability. This document freezes only the isolation rule.

Silent substitution is forbidden: a capability failure must be recorded as a
failure, even when a fallback succeeds.

---

## 12. DSH Boundary

DSH is an **execution capability provider / adapter**, not the control plane.

Owned by `adapters/dsh.py`:

- locating and reading `session.v4.jsonl.zstd`
- zstd decoding
- DSH event schema (`request/header`, `llm/retry`, `turn/end`,
  `assistant/message`)
- DSH/pi-ai error-code vocabulary
- the `TEMPORARY_INTERNAL_INTERFACE` and `FALLBACK_ONLY` markers

Not known to core:

- log path, file format, compression
- event names or schemas
- harness error codes
- credentials layout

Replacing DSH must require changes only inside the adapter boundary and its
wiring — not Task State, Router, Judgment, UI, or Human Control.

Because DSH exposes no external error API, the adapter is a fallback parser and
is explicitly temporary. If DSH ships a first-class error/health interface, the
parser is deleted and the adapter binds to that interface.

---

## 13. Judgment / Jev Boundary

Jev is a **Judgment Capability**, one candidate implementation among:

- Jev
- LLM judgment
- rule judgment
- future model

Jev is not Router, not Task Core, not Workflow Engine, and not the sole decider.
A router may request a judgment signal, but routing must also respect:

- explicit user instruction
- provider health hard facts
- permissions
- availability
- task state
- deterministic policy
- the judgment signal

A judgment signal can never override:

- explicit user instructions
- health hard facts
- permission limits
- safety gates

Example: if `openai-codex` is `COOLDOWN`, then `openai-codex: 0.99` from Jev is
still ineligible. Health is an observation, not a vote.

No Jev integration exists in this repository.

---

## 14. Provider Failure vs Execution Failure

GOAL-01 models **provider failure only**, with a frozen eight-category enum:
`RATE_LIMIT`, `QUOTA_EXHAUSTED`, `AUTH_FAILED`, `PROVIDER_UNAVAILABLE`,
`MODEL_UNAVAILABLE`, `NETWORK_ERROR`, `MODEL_ERROR`, `UNKNOWN`.

Provider failure examples: HTTP 429, quota exhausted, invalid credential,
provider 5xx, transport timeout.

Execution / environment / control failure examples:

- sandbox permission denied
- workspace not writable
- git conflict
- tool unavailable
- human approval required
- task state conflict

These are **not** provider failures. They must not be coerced into
`MODEL_ERROR` or `PROVIDER_UNAVAILABLE` for convenience. They will get a
separate taxonomy in a later goal; ACP-ARCH-01 only records the boundary and
does not extend the GOAL-01 enum.

Note: "human approval required" is an expected control state, not an error
class; it is listed here to prevent it from being smuggled into provider
failure categories.

---

## 15. Examples

**Simple task**

```
Task(plan my week) → Execution(dsh: deepseek) → Result
```

No judgment, no review. Adding them later is composition, not a rewrite.

**Risk-triggered review**

```
Execution → risk detected → INSERT Review → resume → Execution → Result
```

**User approval**

```
Execution → approval needed → INSERT Human Gate → approved → resume → Result
```

**Research with conflict**

```
Research → conflicting evidence → INSERT Judgment → resume → Research → Result
```

**Vertical growth**

A V1 task shape stays valid while V4 tasks add planning, human gates, and
verification for tasks that need them.

---

## 16. Anti-patterns

1. Fixed `Planner → Worker → Reviewer → Verifier` skeleton in Task Core.
2. Provider names or `if provider == ...` branches in core.
3. Harness-specific error codes, log paths, zstd, or event names in core.
4. Jev deciding routing, or overriding health/permissions/user instructions.
5. Mapping sandbox/git/permission failures into provider error categories.
6. A capability importing another capability's internals.
7. A global registry/bus/DI/workflow DSL added before a real call site exists.
8. Making provider health, UI, or persistence part of the Stable Core.
9. Silent failover that hides the original failure.
10. Requiring every task to declare a full lifecycle before it can run.
11. "Temporary" cross-boundary shortcuts without a removal marker or owner.

---

## 17. Router Boundary (GOAL-03)

Routing is a pure selection: `facts + constraints + policy → decision | NoRoute`.
The Router reads Task facts and provider-health facts, selects at most one
candidate, and stops.

- It never executes a capability, calls a model, writes Task State, mutates
  health, retries, or loops.
- User REQUIRE/DENY/ALLOW outrank health and policy. A required-but-unavailable
  capability yields `NoRoute`; it never authorizes silent substitution.
- Capability availability and provider health are separate hard gates.
- Continuation is classified as `CONTINUE` or `HANDOFF`; neither is executed.
- `NoRoute` is a first-class result with per-candidate rejection reasons.
- A judgment signal (future Jev) is advisory and may only break ties after all
  hard gates and deterministic ranks.

Concrete contract: [ROUTER.md](ROUTER.md).

## 18. Controller Boundary (GOAL-04)

The Task Controller composes existing capabilities one bounded step at a time:

```
load Task State -> read Health -> Router -> ONE execution invocation
                -> persist result/failure -> AdvanceOutcome
```

- `advance()` never loops to completion and never retries; the caller decides
  whether to advance again.
- Execution is behind a vendor-neutral contract (`ExecutionRequest`,
  `ExecutionResult`, `ExecutionFailure`). Adapters normalize failures.
- Only normalized PROVIDER failures update health; execution/environment
  failures never poison provider health.
- Every write uses optimistic revision checks. A conflict or persistence
  failure returns an explicit uncertain outcome; the Task is not falsely
  completed.
- NoRoute executes nothing and blocks the Task with recorded reasons.
- An active insertion frame is left untouched; the controller returns
  `BLOCKED`.

Concrete contract: [CONTROLLER.md](CONTROLLER.md).

## Appendix A — Current code map

| Path | Role | Boundary |
|---|---|---|
| `core/errors.py` | provider error taxonomy, classification, redaction | harness-neutral; no vendor vocabulary |
| `core/provider_health.py` | per-provider health state machine | harness-neutral; opaque provider keys |
| `adapters/dsh.py` | DSH session-log → core signals | `TEMPORARY_INTERNAL_INTERFACE`, `FALLBACK_ONLY` |
| `adapters/dsh_execution.py` | real execution over `dsh --profile sdk` | implements ExecutionCapability; process-per-execution |
| `core/task.py` | Task State domain model | no storage/harness imports |
| `core/task_store.py` | store Protocol + errors | storage-agnostic |
| `core/redaction.py` | shared credential redaction | one choke point |
| `stores/json_task_store.py` | atomic JSON persistence | implements the Protocol |
| `core/capability.py` | candidate contract | dataclass only; no registry |
| `core/router.py` | deterministic selection | no execution/store/health writes |
| `core/execution.py` | vendor-neutral execution contract | adapters implement it |
| `core/controller.py` | bounded one-step composition | no loops; no raw provider parsing |
| `tests/test_architecture.py` | invariant enforcement | — |

Not present by design: Task State, Router, Jev, Human Control, UI, server, DB,
scheduler, queue, plugin runtime, workflow engine.

## Appendix B — Change protocol

For every new capability:

1. State its `kind`, contract, and failure semantics.
2. Answer the removal question in §10.
3. Name the seam it touches.
4. Add an invariant test if the boundary can be checked statically.
5. If the answer to §10 is "removal breaks unrelated capabilities", revise the
   design before writing code.

## Appendix C — ACP-ARCH-01 audit result

The GOAL-01 audit found one P1 coupling: `core/errors.py` owned the DSH error
code table and DSH-specific comments, so replacing the harness would have
required editing core. Fix: the table moved to `adapters/dsh.py`; core now
accepts a neutral `category_hint` and treats `code` as opaque diagnostics.
No P0 issues were found: core had no adapter imports, health had no provider
branches, and deleting the adapter left core tests intact.
