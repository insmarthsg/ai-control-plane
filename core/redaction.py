"""Credential redaction shared by provider errors and persisted task state.

One choke point, two shapes:

* :func:`redact` cleans free text (error messages, reasons, references).
* :func:`redact_data` cleans JSON-like structures before they are written to
  disk, including key-aware handling of ``api_key`` / ``Authorization`` /
  token-style fields whose value would not match a text pattern.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = ["redact", "redact_data"]

# Order matters: the bearer/api-key patterns consume the secret before the
# generic "Authorization: ..." pattern can mangle the label.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(api[_-]?key|authorization)\b\s*[:=]\s*(?:Bearer\s+)?[\"']?[^\s\"',;}]+"
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(access_token|refresh_token|id_token)\b\s*[:=]\s*[\"']?[^\s\"',;}]+"),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(accountId|account_id)\b\s*[:=]\s*[\"']?[^\s\"',;}]+"),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{8,}"),
        "[REDACTED_API_KEY]",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
        "[REDACTED_JWT]",
    ),
)

_SENSITIVE_KEYS = re.compile(
    r"(?i)^(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token"
    r"|id[_-]?token|token|secret|credentials?|client[_-]?secret|password|passwd"
    r"|accountId|account_id)$"
)

REDACTED = "[REDACTED]"


def redact(text: Any) -> str:
    """Return *text* with credentials and account identifiers removed."""

    rendered = text if isinstance(text, str) else str(text)
    for pattern, replacement in _REDACTIONS:
        rendered = pattern.sub(replacement, rendered)
    return rendered


def redact_data(value: Any) -> Any:
    """Recursively redact a JSON-like structure.

    Sensitive *field names* redact their whole value, even when the value has
    no recognisable token shape. Non-JSON scalars are stringified so the
    result stays serializable.
    """

    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            rendered_key = str(key)
            cleaned[rendered_key] = REDACTED if _SENSITIVE_KEYS.match(rendered_key) else redact_data(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(value)
