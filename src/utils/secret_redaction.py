"""Redact common credentials before exception details leave the service."""

import re


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|key|token|access[_-]?token)[\"']?\s*[=:]\s*[\"']?([^\s\"'&,}]+)"),
    re.compile(r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/]+=*)"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{10,}"),
)


def redact_secrets(value: str) -> str:
    """Return readable error text with likely API credentials removed."""
    if not value:
        return ""

    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda match: match.group(0).replace(match.group(match.lastindex or 0), "[REDACTED]", 1),
            redacted,
        )
    return redacted
