"""Thin Router: deterministic selection, hard gates, explainability."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core.capability import CapabilityAvailability, CapabilityCandidate
from core.errors import classify_provider_error
from core.provider_health import HealthStatus, ProviderHealthTracker
from core.router import (
    ContinuationMode,
    JudgmentSignal,
    NoRoute,
    RejectionReason,
    RoutingDecision,
    RoutingRequest,
    ThinRouter,
    UserConstraints,
)
from core.task import LifecycleStatus
from stores import JSONTaskStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER = ThinRouter()


def candidate(
    capability_id: str,
    *,
    provider: str | None = None,
    rank: int = 100,
    kind: str = "execution",
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE,
    refs: frozenset[str] = frozenset(),
    handoff: bool = False,
) -> CapabilityCandidate:
    return CapabilityCandidate(
        capability_id=capability_id,
        kind=kind,
        availability=availability,
        provider=provider,
        preference_rank=rank,
        continuation_refs=refs,
        supports_task_state_handoff=handoff,
    )


def request(
    candidates,
    health,
    *,
    constraints: UserConstraints | None = None,
    mode: ContinuationMode = ContinuationMode.NONE,
    ref: str | None = None,
    durable: bool = False,
    signals=(),
    task_id: str = "task-1",
    kind: str = "execution",
) -> RoutingRequest:
    return RoutingRequest(
        task_id=task_id,
        required_capability_kind=kind,
        candidates=tuple(candidates),
        constraints=constraints or UserConstraints(),
        continuation_mode=mode,
        continuation_ref=ref,
        task_has_durable_state=durable,
        provider_health=health,
        judgment_signals=tuple(signals),
    )


def rejection_reasons(result) -> dict[str, str]:
    return {item.capability_id: item.reason.value for item in result.rejected_candidates}


def test_healthy_preferred_candidate_wins() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-openai", provider="openai-codex", rank=10),
                candidate("execution-glm", provider="zai-coding-cn", rank=20),
            ],
            {"openai-codex": "AVAILABLE", "zai-coding-cn": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-openai"
    assert rejection_reasons(result) == {"execution-glm": "LOWER_PRIORITY"}
    assert result.policy_version == "goal-03.v1"


def test_cooldown_provider_falls_to_healthy_alternative() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-openai", provider="openai-codex", rank=10),
                candidate("execution-glm", provider="zai-coding-cn", rank=20),
            ],
            {"openai-codex": "COOLDOWN", "zai-coding-cn": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-glm"
    assert rejection_reasons(result)["execution-openai"] == "PROVIDER_COOLDOWN"


def test_user_require_unavailable_yields_no_route_not_substitution() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-openai", provider="openai-codex", rank=10),
                candidate("execution-glm", provider="zai-coding-cn", rank=20),
            ],
            {"openai-codex": "COOLDOWN", "zai-coding-cn": "AVAILABLE"},
            constraints=UserConstraints(required=frozenset({"execution-openai"})),
        )
    )
    assert isinstance(result, NoRoute)
    reasons = rejection_reasons(result)
    assert reasons["execution-openai"] == "PROVIDER_COOLDOWN"
    assert reasons["execution-glm"] == "NOT_REQUIRED"
    assert "REQUIRE:execution-openai" in result.unsatisfied_constraints


def test_user_deny_is_respected_even_when_preferred() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-openai", provider="openai-codex", rank=10),
                candidate("execution-glm", provider="zai-coding-cn", rank=20),
            ],
            {"openai-codex": "AVAILABLE", "zai-coding-cn": "AVAILABLE"},
            constraints=UserConstraints(denied=frozenset({"execution-openai"})),
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-glm"
    assert rejection_reasons(result)["execution-openai"] == "USER_DENIED"


def test_continue_requires_matching_continuation_ref() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-openai", provider="openai-codex", refs=frozenset({"cont-1"})),
                candidate("execution-glm", provider="zai-coding-cn"),
            ],
            {"openai-codex": "AVAILABLE", "zai-coding-cn": "AVAILABLE"},
            mode=ContinuationMode.CONTINUE,
            ref="cont-1",
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-openai"
    assert result.mode is ContinuationMode.CONTINUE
    assert rejection_reasons(result)["execution-glm"] == "CONTINUATION_INCOMPATIBLE"


def test_handoff_selects_capable_candidate_and_reports_mode() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-openai", provider="openai-codex", rank=10),
                candidate("execution-glm", provider="zai-coding-cn", rank=20, handoff=True),
            ],
            {"openai-codex": "COOLDOWN", "zai-coding-cn": "AVAILABLE"},
            mode=ContinuationMode.HANDOFF,
            durable=True,
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-glm"
    assert result.mode is ContinuationMode.HANDOFF
    assert rejection_reasons(result)["execution-openai"] == "PROVIDER_COOLDOWN"


def test_all_candidates_unavailable_is_no_route_with_all_reasons() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-a", provider="p-a"),
                candidate("execution-b", provider="p-b"),
                candidate("execution-c", provider="p-c"),
            ],
            {"p-a": "COOLDOWN", "p-b": "AUTH_REQUIRED", "p-c": "UNAVAILABLE"},
        )
    )
    assert isinstance(result, NoRoute)
    assert result.to_dict()["outcome"] == "NO_ROUTE"
    reasons = rejection_reasons(result)
    assert set(reasons.values()) == {"PROVIDER_COOLDOWN", "PROVIDER_AUTH_REQUIRED", "PROVIDER_UNAVAILABLE"}


def test_health_isolated_per_provider() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-openai", provider="openai-codex", rank=10),
                candidate("execution-glm", provider="zai-coding-cn", rank=20),
            ],
            {"openai-codex": "COOLDOWN", "zai-coding-cn": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-glm"
    assert rejection_reasons(result)["execution-openai"] == "PROVIDER_COOLDOWN"


def test_same_input_is_deterministic() -> None:
    req = request(
        [
            candidate("execution-b", provider="p-b", rank=10),
            candidate("execution-a", provider="p-a", rank=10),
        ],
        {"p-a": "AVAILABLE", "p-b": "AVAILABLE"},
        signals=[JudgmentSignal("execution-b", 0.4), JudgmentSignal("execution-a", 0.4)],
    )
    baseline = ROUTER.route(req).to_dict()
    for _ in range(25):
        assert ROUTER.route(req).to_dict() == baseline


def test_judgment_signal_cannot_override_cooldown() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-openai", provider="openai-codex", rank=10),
                candidate("execution-glm", provider="zai-coding-cn", rank=20),
            ],
            {"openai-codex": "COOLDOWN", "zai-coding-cn": "AVAILABLE"},
            signals=[
                JudgmentSignal("execution-openai", 0.99, confidence=0.99, source="fake-jev"),
                JudgmentSignal("execution-glm", 0.40, confidence=0.80, source="fake-jev"),
            ],
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-glm"
    assert result.judgment_considered is True
    assert rejection_reasons(result)["execution-openai"] == "PROVIDER_COOLDOWN"


def test_judgment_only_breaks_ties_after_deterministic_rank() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-a", provider="p-a", rank=10),
                candidate("execution-b", provider="p-b", rank=20),
            ],
            {"p-a": "AVAILABLE", "p-b": "AVAILABLE"},
            signals=[JudgmentSignal("execution-b", 0.99, source="fake-jev")],
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-a"  # rank beats judgment

    tied = ROUTER.route(
        request(
            [
                candidate("execution-a", provider="p-a", rank=10),
                candidate("execution-b", provider="p-b", rank=10),
            ],
            {"p-a": "AVAILABLE", "p-b": "AVAILABLE"},
            signals=[JudgmentSignal("execution-b", 0.99, source="fake-jev")],
        )
    )
    assert isinstance(tied, RoutingDecision)
    assert tied.selected_capability_id == "execution-b"  # tie-break only


def test_degraded_provider_ranks_below_available() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-a", provider="p-a", rank=1),
                candidate("execution-b", provider="p-b", rank=99),
            ],
            {"p-a": "DEGRADED", "p-b": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-b"
    assert rejection_reasons(result)["execution-a"] == "LOWER_PRIORITY"


def test_malformed_candidate_does_not_break_others() -> None:
    malformed = candidate("execution-bad", provider="p-bad")
    object.__setattr__(malformed, "availability", "BROKEN")
    result = ROUTER.route(
        request(
            [malformed, candidate("execution-good", provider="p-good", rank=50)],
            {"p-bad": "AVAILABLE", "p-good": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-good"
    assert rejection_reasons(result)["execution-bad"] == "MALFORMED_CANDIDATE"


def test_missing_health_rejects_only_that_candidate() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-a", provider="p-missing"),
                candidate("execution-b", provider="p-ok", rank=50),
            ],
            {"p-ok": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-b"
    assert rejection_reasons(result)["execution-a"] == "HEALTH_UNKNOWN"


def test_malformed_health_rejects_only_that_candidate() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-a", provider="p-bad"),
                candidate("execution-b", provider="p-ok", rank=50),
            ],
            {"p-bad": {"status": "NOT_A_STATUS"}, "p-ok": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-b"
    assert rejection_reasons(result)["execution-a"] == "HEALTH_UNKNOWN"


def test_new_fake_provider_needs_no_core_change() -> None:
    result = ROUTER.route(
        request(
            [candidate("execution-fake", provider="fake-provider-z")],
            {"fake-provider-z": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-fake"


def test_router_works_without_dsh_adapter() -> None:
    program = """
import sys
from core.capability import CapabilityCandidate
from core.router import RoutingRequest, ThinRouter

request = RoutingRequest(
    task_id="t",
    required_capability_kind="execution",
    candidates=(CapabilityCandidate("execution-a", "execution", provider="p"),),
    provider_health={"p": "AVAILABLE"},
)
decision = ThinRouter().route(request)
assert decision.selected_capability_id == "execution-a"
loaded = sorted(m for m in sys.modules if m.split(".")[0] in {"adapters", "stores"})
assert not loaded, loaded
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", program], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr


def test_router_does_not_mutate_task_state(tmp_path: Path) -> None:
    store = JSONTaskStateStore(tmp_path / "tasks")
    task = store.create_task(objective="router read-only")
    task = store.set_lifecycle(task.task_id, LifecycleStatus.RUNNING, expected_revision=task.revision)
    before = store.get_task(task.task_id)
    events_before = store.list_events(task.task_id)

    ROUTER.route(
        request(
            [candidate("execution-a", provider="p")],
            {"p": "AVAILABLE"},
            task_id=task.task_id,
        )
    )

    after = store.get_task(task.task_id)
    assert after == before
    assert store.list_events(task.task_id) == events_before


def test_router_does_not_mutate_health() -> None:
    tracker = ProviderHealthTracker()
    tracker.record_error(classify_provider_error("p-openai", message="429", http_status=429))
    tracker.record_success("p-glm")
    before = {provider: record.to_dict() for provider, record in tracker.all().items()}
    health = {provider: record.to_dict() for provider, record in tracker.all().items()}

    result = ROUTER.route(
        request(
            [
                candidate("execution-openai", provider="p-openai"),
                candidate("execution-glm", provider="p-glm", rank=50),
            ],
            health,
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-glm"
    after = {provider: record.to_dict() for provider, record in tracker.all().items()}
    assert after == before


def test_capability_available_but_user_denied_never_selected() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-a", provider="p", rank=1),
                candidate("execution-b", provider="q", rank=50),
            ],
            {"p": "AVAILABLE", "q": "AVAILABLE"},
            constraints=UserConstraints(denied=frozenset({"execution-a"}), allowed=frozenset({"execution-a", "execution-b"})),
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-b"


def test_no_route_when_allowed_set_is_empty() -> None:
    result = ROUTER.route(
        request(
            [candidate("execution-a", provider="p")],
            {"p": "AVAILABLE"},
            constraints=UserConstraints(allowed=frozenset()),
        )
    )
    assert isinstance(result, NoRoute)
    assert rejection_reasons(result)["execution-a"] == "USER_NOT_ALLOWED"
    assert "ALLOW: empty set" in result.unsatisfied_constraints


def test_secrets_in_metadata_never_reach_decision() -> None:
    candidate_with_secret = CapabilityCandidate(
        capability_id="execution-a",
        kind="execution",
        provider="p",
        metadata={
            "api_key": "sk-test-router-secret",
            "authorization": "Bearer abcdefghijklmnop",
        },
    )
    result = ROUTER.route(request([candidate_with_secret], {"p": "AVAILABLE"}))
    rendered = json.dumps(result.to_dict())
    assert "sk-test-router-secret" not in rendered
    assert "abcdefghijklmnop" not in rendered


def test_tie_break_is_capability_id_not_random() -> None:
    result = ROUTER.route(
        request(
            [
                candidate("execution-z", provider="p-z", rank=10),
                candidate("execution-a", provider="p-a", rank=10),
            ],
            {"p-z": "AVAILABLE", "p-a": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-a"


def test_capability_availability_is_separate_from_provider_health() -> None:
    result = ROUTER.route(
        request(
            [
                candidate(
                    "execution-broken",
                    provider="p-healthy",
                    rank=1,
                    availability=CapabilityAvailability.UNAVAILABLE,
                ),
                candidate("execution-good", provider="p-other", rank=50),
            ],
            {"p-healthy": "AVAILABLE", "p-other": "AVAILABLE"},
        )
    )
    assert isinstance(result, RoutingDecision)
    assert result.selected_capability_id == "execution-good"
    assert rejection_reasons(result)["execution-broken"] == "CAPABILITY_UNAVAILABLE"


def test_works_without_any_judgment_signal() -> None:
    result = ROUTER.route(
        request([candidate("execution-a", provider="p")], {"p": "AVAILABLE"})
    )
    assert isinstance(result, RoutingDecision)
    assert result.judgment_considered is False
    assert result.selected_capability_id == "execution-a"
