"""Adapter tests: structured DSH failure extraction and core isolation."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import adapters.dsh as dsh
from adapters.dsh import (
    extract_provider_failure,
    iter_events,
    read_session_summary,
    record_session_health,
)
from core.errors import ErrorCategory
from core.provider_health import ProviderHealthTracker

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_session(tmp_path: Path, events: list[dict]) -> Path:
    path = tmp_path / "session.v4.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return path


def _header(provider: str = "openai-codex", model: str = "gpt-5.6-terra") -> dict:
    return {
        "type": "request/header",
        "data": {"config": {"provider": provider, "model": model}},
    }


def test_turn_end_error_is_extracted_and_drives_cooldown(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path,
        [
            {"type": "session", "version": 4, "id": "s"},
            _header(),
            {
                "type": "turn/end",
                "data": {
                    "turn": 1,
                    "reason": {
                        "kind": "error",
                        "error": {"message": "429 rate limit exceeded", "code": "RATE_LIMIT"},
                    },
                },
            },
        ],
    )

    summary = read_session_summary(path)
    assert summary.provider == "openai-codex"
    assert summary.model == "gpt-5.6-terra"
    assert summary.failure is not None
    assert summary.failure.category is ErrorCategory.RATE_LIMIT

    tracker = ProviderHealthTracker()
    failure = record_session_health(tracker, path)
    assert failure is not None
    assert tracker.get("openai-codex").status.value == "COOLDOWN"


def test_llm_retry_failure_carries_structured_retry_after(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path,
        [
            {"type": "session", "version": 4, "id": "s"},
            _header("zai-coding-cn", "glm-5.3-flash"),
            {
                "type": "llm/retry",
                "data": {
                    "provider": "zai-coding-cn",
                    "failure": {
                        "code": "QUOTA",
                        "message": "quota exhausted",
                        "providerRetryAfterMs": 45000,
                    },
                },
            },
        ],
    )

    failure = extract_provider_failure(list(iter_events(path)))
    assert failure is not None
    assert failure.category is ErrorCategory.QUOTA_EXHAUSTED
    assert failure.retry_after_seconds == 45.0

    tracker = ProviderHealthTracker()
    record_session_health(tracker, path)
    record = tracker.get("zai-coding-cn")
    assert record is not None
    assert record.status.value == "COOLDOWN"
    assert record.retry_after_seconds == 45.0


def test_session_without_failure_marks_provider_available(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path,
        [
            {"type": "session", "version": 4, "id": "s"},
            _header(),
            {
                "type": "assistant/message",
                "data": {
                    "message": {
                        "source": {
                            "replayState": {
                                "response": {
                                    "responseId": "resp_123",
                                    "usage": {
                                        "inputTokens": 730,
                                        "outputTokens": 228,
                                        "totalTokens": 958,
                                        "cacheReadTokens": 47552,
                                    },
                                }
                            }
                        }
                    }
                },
            },
        ],
    )

    summary = read_session_summary(path)
    assert summary.failure is None
    assert summary.response_id == "resp_123"
    assert summary.usage["inputTokens"] == 730

    tracker = ProviderHealthTracker()
    assert record_session_health(tracker, path) is None
    assert tracker.get("openai-codex").status.value == "AVAILABLE"


def test_adapter_is_quarantined_and_core_stays_harness_neutral() -> None:
    assert dsh.TEMPORARY_INTERNAL_INTERFACE is True
    assert dsh.FALLBACK_ONLY is True

    forbidden_roots = {"adapters", "zstandard", "subprocess"}
    for module_path in sorted((REPO_ROOT / "core").glob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                assert root not in forbidden_roots, f"{module_path.name} imports {name}"
