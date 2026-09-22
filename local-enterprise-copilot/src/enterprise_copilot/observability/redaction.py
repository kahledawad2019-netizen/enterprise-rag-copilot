"""
Redaction for logs and traces.

Traces are the most useful debugging artefact in the system and the easiest
place to leak data. They carry prompts, query results and connection details,
and they are written to disk and shipped to a collector — both of which outlive
the request and are read by people who were never authorised to see the
underlying records.

Everything written to a log or a span passes through here first.

Three categories are handled:

* **Credentials** — connection strings, passwords, API keys, tokens. Removed
  entirely, never truncated, because a partial password is still a hint.
* **Personal data** — emails, phone numbers. Masked in a way that keeps them
  recognisable for debugging (`j***@example.com`) without being usable.
* **Bulk content** — prompts and result sets. Truncated, because a trace that
  contains the whole evidence package is a copy of the data, not a record of
  what happened to it.
"""

from __future__ import annotations

import re
from typing import Any

# Credential-shaped values. Matched on the value, not just the key, because a
# connection string can appear inside a free-text error message.
CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"PWD=[^;]+", re.I), "PWD=[REDACTED]"),
    (re.compile(r"PASSWORD=[^;]+", re.I), "PASSWORD=[REDACTED]"),
    (re.compile(r"UID=[^;]+", re.I), "UID=[REDACTED]"),
    (re.compile(r"DRIVER=\{[^}]+\}[^\"']*", re.I), "[REDACTED CONNECTION STRING]"),
    (re.compile(r"\b(sk|pk|api|key|token)[-_][A-Za-z0-9]{16,}\b", re.I), "[REDACTED KEY]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}", re.I), "Bearer [REDACTED]"),
)

EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")

# Keys whose value is always removed, whatever it contains.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "api_key",
        "apikey",
        "token",
        "connection_string",
        "odbc_connection_string",
        "credential",
        "credentials",
        "authorization",
        "auth",
        "private_key",
        "ssn",
        "tax_id",
        "password_hash",
    }
)

# Keys carrying personal data: masked rather than removed, so a trace still
# shows that a contact was involved without exposing who.
PII_KEYS = frozenset({"email", "phone", "full_name", "contact_email"})

MAX_TEXT_LENGTH = 2000


def redact_text(value: str, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    """Strip credentials, mask personal data, and cap length."""
    if not value:
        return value

    for pattern, replacement in CREDENTIAL_PATTERNS:
        value = pattern.sub(replacement, value)

    value = EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", value)
    value = PHONE.sub("[PHONE]", value)

    if len(value) > max_length:
        value = value[:max_length] + f" ... [truncated, {len(value)} chars total]"
    return value


def redact_value(key: str, value: Any, *, max_length: int = MAX_TEXT_LENGTH) -> Any:
    """Redact one key/value pair according to its key and its content."""
    lowered = key.lower()

    if lowered in SENSITIVE_KEYS or any(s in lowered for s in ("password", "secret", "token")):
        return "[REDACTED]"

    if lowered in PII_KEYS and isinstance(value, str):
        return redact_text(value, max_length=200)

    if isinstance(value, str):
        return redact_text(value, max_length=max_length)

    if isinstance(value, dict):
        return redact_mapping(value, max_length=max_length)

    if isinstance(value, list):
        # Cap list length as well: a 5,000-row result set in a trace is a copy
        # of the data rather than a record of the request.
        capped = value[:50]
        redacted = [redact_value(key, item, max_length=max_length) for item in capped]
        if len(value) > 50:
            redacted.append(f"... [{len(value) - 50} more items omitted]")
        return redacted

    return value


def redact_mapping(mapping: dict[str, Any], *, max_length: int = MAX_TEXT_LENGTH) -> dict[str, Any]:
    return {key: redact_value(key, value, max_length=max_length) for key, value in mapping.items()}


def safe_exception(exc: BaseException) -> str:
    """Render an exception without leaking a connection string.

    Driver errors routinely echo the whole connection string back, so the type
    and message are redacted rather than formatted verbatim.
    """
    return redact_text(f"{type(exc).__name__}: {exc}", max_length=500)


__all__ = [
    "MAX_TEXT_LENGTH",
    "PII_KEYS",
    "SENSITIVE_KEYS",
    "redact_mapping",
    "redact_text",
    "redact_value",
    "safe_exception",
]
