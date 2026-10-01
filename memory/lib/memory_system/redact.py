from __future__ import annotations

import re
from typing import Any

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"cursor_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]*?-----END [A-Z ]+-----"),
]

# A closed <private>…</private> span, or an unclosed <private> through the end of the text.
PRIVATE_SPAN = re.compile(r"<private>.*?(?:</private>|\Z)", re.IGNORECASE | re.DOTALL)
PRIVATE_PLACEHOLDER = "[private]"

PATH_DENY_FRAGMENTS = (
    ".env",
    "/secrets/",
    "credentials",
    "id_rsa",
    ".pem",
    ".key",
)


def should_redact_path(path: str | None) -> bool:
    if not path:
        return False
    lower = path.lower()
    return any(fragment in lower for fragment in PATH_DENY_FRAGMENTS)


def redact_text(text: str) -> str:
    if not text:
        return text
    # Private spans go first so a secret pattern cannot leave part of one behind.
    out = PRIVATE_SPAN.sub(PRIVATE_PLACEHOLDER, text)
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(0).split("=")[0] + "=[REDACTED]" if "=" in m.group(0) else "[REDACTED]", out)
    return out


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in ("output", "text", "prompt", "command"):
            out[key] = redact_text(str(value))[:8000]
        elif key == "file_path" and should_redact_path(str(value)):
            out[key] = "[REDACTED_PATH]"
        elif isinstance(value, dict):
            out[key] = sanitize_payload(value)
        elif isinstance(value, list):
            out[key] = [sanitize_payload(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out
