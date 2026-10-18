"""DSH session-log adapter — TEMPORARY_INTERNAL_INTERFACE.

STATUS: FALLBACK_ONLY.

Why this exists
---------------
DSH (0.2.1-rc.2) has structured provider failures *inside* the harness
(``HarnessError.code`` plus the ``agent/request-error`` hook consumed by
``dsh-llm-retry``), but it exposes no external error/health API. A Python
control plane therefore has exactly two options: parse the on-disk session
log, or re-derive state from provider traffic. This adapter does the former,
and is quarantined here so that ``core/`` never learns about zstd, DSH home
paths, or the ``session.v4`` event schema.

Schema anchors used (checked against real sessions):
  * ``request/header``    — configured ``provider`` / ``model``
  * ``llm/retry``         — structured ``failure`` (``code``, ``message``,
                            optional ``providerRetryAfterMs``)
  * ``turn/end``          — ``reason.kind == "error"`` with ``error.code``
  * ``assistant/message`` — ``responseId`` / token usage (metadata only)

If DSH ever ships a first-class error/health export, delete this module's
parsers and bind ``core`` to that interface instead.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from core.errors import ProviderError, classify_provider_error
from core.provider_health import ProviderHealthTracker

__all__ = [
    "TEMPORARY_INTERNAL_INTERFACE",
    "FALLBACK_ONLY",
    "SessionLogUnavailable",
    "SessionSummary",
    "extract_provider_failure",
    "iter_events",
    "latest_session_file",
    "read_session_summary",
    "record_session_health",
    "sessions_root",
]

TEMPORARY_INTERNAL_INTERFACE = True
FALLBACK_ONLY = True


class SessionLogUnavailable(RuntimeError):
    """Raised when a DSH session log cannot be located or decoded."""


def sessions_root() -> Path:
    """Resolve the DSH sessions directory without hard-coding a user name."""

    override = os.environ.get("DSH_SESSIONS_DIR") or os.environ.get("ACP_DSH_SESSIONS_DIR")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("DSH_HOME")
    if home:
        return Path(home).expanduser() / "sessions"
    return Path.home() / ".dsh" / "sessions"


def latest_session_file(root: Path | None = None) -> Path:
    """Return the most recently modified ``session.v4.jsonl.zstd`` under *root*."""

    base = root or sessions_root()
    candidates = sorted(
        base.glob("**/session.v4.jsonl.zstd"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SessionLogUnavailable(f"no session.v4.jsonl.zstd under {base}")
    return candidates[0]


def _resolve(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        for name in ("session.v4.jsonl.zstd", "session.v4.jsonl"):
            nested = candidate / name
            if nested.is_file():
                return nested
        raise SessionLogUnavailable(f"no session log in directory {candidate}")
    if not candidate.is_file():
        raise SessionLogUnavailable(f"session log not found: {candidate}")
    return candidate


def _read_text(path: Path) -> str:
    """Decode a plain or zstd-compressed session log into text."""

    if path.suffix != ".zstd":
        return path.read_text(encoding="utf-8", errors="replace")

    try:
        import zstandard  # type: ignore[import-not-found]
    except ImportError:
        zstandard = None  # type: ignore[assignment]

    if zstandard is not None:
        try:
            with path.open("rb") as handle:
                payload = zstandard.ZstdDecompressor().stream_reader(handle).read()
        except Exception as exc:  # corrupt or truncated frame
            raise SessionLogUnavailable(f"cannot decompress {path}: {exc}") from exc
        return payload.decode("utf-8", errors="replace")

    # Dependency-free fallback: the zstd CLI ships with macOS tooling.
    try:
        completed = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SessionLogUnavailable(f"cannot decompress {path}: {exc}") from exc
    return completed.stdout.decode("utf-8", errors="replace")


def iter_events(path: str | os.PathLike[str]) -> Iterator[dict[str, Any]]:
    """Yield decoded session events, skipping malformed lines."""

    resolved = _resolve(path)
    for line in _read_text(resolved).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            yield event


def _find(node: Any, key: str, depth: int = 0) -> Any:
    """Depth-first lookup of the first *key* occurrence in nested JSON."""

    if depth > 8:
        return None
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find(value, key, depth + 1)
            if found is not None:
                return found
    return None


def _provider_from_events(events: list[dict[str, Any]]) -> str | None:
    explicit = None
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event["type"] == "llm/retry" and isinstance(data.get("provider"), str):
            explicit = data["provider"]
        elif event["type"] == "request/header":
            provider = _find(data, "provider")
            if isinstance(provider, str):
                explicit = provider
    return explicit


@dataclass(frozen=True)
class SessionSummary:
    provider: str | None
    model: str | None
    response_id: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    failure: ProviderError | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "response_id": self.response_id,
            "usage": dict(self.usage),
            "failure": self.failure.to_dict() if self.failure is not None else None,
        }


def extract_provider_failure(
    events: list[dict[str, Any]],
    *,
    provider: str | None = None,
) -> ProviderError | None:
    """Extract the latest structured provider failure, if any.

    Preference order: ``llm/retry.failure`` (canonical DSH code, may carry
    ``providerRetryAfterMs``) and then ``turn/end`` error reasons. No free
    text is invented when neither exists: the return value is ``None``.
    """

    selected_provider = provider or _provider_from_events(events)

    latest: tuple[str | None, str, float | None] | None = None
    for event in events:
        if event.get("type") != "llm/retry":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        failure = data.get("failure")
        if not isinstance(failure, dict):
            continue
        code = failure.get("code")
        message = failure.get("message")
        retry_ms = failure.get("providerRetryAfterMs")
        retry_seconds = None
        if isinstance(retry_ms, (int, float)) and not isinstance(retry_ms, bool) and retry_ms > 0:
            retry_seconds = float(retry_ms) / 1000.0
        latest = (
            str(code) if code is not None else None,
            str(message) if message is not None else "",
            retry_seconds,
        )
        if isinstance(data.get("provider"), str):
            selected_provider = data["provider"]

    if latest is None:
        for event in events:
            if event.get("type") != "turn/end":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            reason = data.get("reason")
            if not isinstance(reason, dict) or reason.get("kind") != "error":
                continue
            error = reason.get("error")
            if not isinstance(error, dict):
                error = {}
            code = error.get("code")
            message = error.get("message")
            latest = (
                str(code) if code is not None else None,
                str(message) if message is not None else "",
                None,
            )

    if latest is None:
        return None

    code, message, retry_seconds = latest
    return classify_provider_error(
        selected_provider or "unknown",
        message=message,
        code=code,
        retry_after_seconds=retry_seconds,
    )


def read_session_summary(path: str | os.PathLike[str]) -> SessionSummary:
    events = list(iter_events(path))
    provider = _provider_from_events(events)
    model = None
    response_id = None
    usage: Mapping[str, Any] = {}

    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event["type"] == "request/header":
            candidate = _find(data, "model")
            if isinstance(candidate, str):
                model = candidate
        elif event["type"] == "assistant/message":
            candidate = _find(data, "responseId")
            if isinstance(candidate, str):
                response_id = candidate
            if _find(data, "inputTokens") is not None:
                container = _find(data, "usage")
                if isinstance(container, dict):
                    usage = {
                        key: container[key]
                        for key in ("inputTokens", "outputTokens", "totalTokens", "cacheReadTokens", "cacheWriteTokens")
                        if key in container
                    }

    return SessionSummary(
        provider=provider,
        model=model,
        response_id=response_id,
        usage=usage,
        failure=extract_provider_failure(events, provider=provider),
    )


def record_session_health(
    tracker: ProviderHealthTracker,
    path: str | os.PathLike[str],
) -> ProviderError | None:
    """Update *tracker* from one session log and return the failure, if any."""

    summary = read_session_summary(path)
    if summary.failure is None:
        if summary.provider is not None:
            tracker.record_success(summary.provider)
        return None
    tracker.record_error(summary.failure)
    return summary.failure
