#!/usr/bin/env python3
"""Stdlib-only, no-network secret scanner for the tracked tree.

`--tracked` enumerates `git ls-files` and flags provider secrets and
real-username absolute-path leaks in the files that will ship publicly. It is
the project's actual pre-publish secret gate (gitleaks is NOT installed here);
`scripts/prepublish-check.sh` imports `scan_tracked` from this module.

Exit 0 = clean. Non-zero = at least one offender (each printed as `path:line`).

Deliberate scoping (documented so future edits don't "fix" it into false
positives):

* NO bare 32/64-hex rule. The 64-hex SHA-256 integrity pins in
  ``memory/lib/memory_system/backends/embedding/weights_manifest.py`` are
  legitimate, not secrets, and must not be flagged.
* Binary files are skipped (extension denylist + NUL-byte sniff) so the shipped
  ~91 MB all-MiniLM-L6-v2 weights under ``memory/weights/**`` are never scanned.
* The private local runtime dirs (``.omo/``, ``.playwright-mcp/``,
  ``.remember/``, ``.cursor/``, ``.silly-memory/``) are skipped. They are untracked by the release
  scrub and separately denylisted by ``scripts/prepublish-check.sh``; they never
  enter the public archive, and they legitimately carry this repo's own
  ``/Users/<owner>`` planning paths. Content secret-scanning of the *shipping*
  tree is this module's job; keeping those private dirs out of the tracked tree
  is the prepublish gate's job.
* ``memory/tests/**`` is allowlisted: it holds canonical EXAMPLE fixtures
  (an ``sk-``-prefixed example key, a ``Bearer``-scheme example token, and
  ``/Users/test`` fixture paths) that must stay in the redaction corpus.

Every provider pattern below is written with a regex character class, so this
module's own source never matches its own rules (e.g. ``sk-[A-Za-z0-9]{20,}``
has a ``[`` right after ``sk-``, which is not a 20-char alnum run). The scanner
therefore stays exit-0 even once it is itself committed and tracked.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Provider-secret patterns (name, compiled regex). Real leaks only.
_PROVIDER_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("aws-akia", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("session-id", re.compile(r"ses_[0-9a-zA-Z]{20,}")),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{8,}=*")),
]

# Real leak usernames only. A bare ``/Users/`` (e.g. ``/Users/test`` fixtures)
# is intentionally NOT an offender. The current OS user is always included.
# Extra names (e.g. a previous account on this machine) are kept OUT of the
# tracked tree so the published scanner names nobody: one per line in the
# gitignored ``.secret-scan-usernames`` at the repo root, and/or
# comma-separated in ``$SECRET_SCAN_USERNAMES``.
_LOCAL_USERNAMES_FILE = ".secret-scan-usernames"

# Private local runtime dirs — skipped (see module docstring).
_SKIP_PREFIXES: tuple[str, ...] = (".omo/", ".playwright-mcp/", ".remember/", ".cursor/", ".silly-memory/")

# Canonical EXAMPLE fixtures live here — allowlisted from every rule.
_ALLOWLIST_PREFIXES: tuple[str, ...] = ("memory/tests/",)

# Never scan these — binary payloads (weights, models, archives, images, …).
_BINARY_EXTS: frozenset[str] = frozenset(
    {
        ".safetensors", ".bin", ".onnx", ".npy", ".npz", ".pt", ".pth",
        ".gz", ".tar", ".tgz", ".zip", ".xz", ".bz2",
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
        ".so", ".dylib", ".dll", ".o", ".a", ".woff", ".woff2",
    }
)

_NUL_SNIFF_BYTES = 8192


def _current_username() -> str:
    """Best-effort local OS username via ``id -un`` (never ``git config``)."""
    try:
        result = subprocess.run(
            ["id", "-un"], capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return result.stdout.strip()


def _extra_usernames(root: Path) -> list[str]:
    names = [n.strip() for n in os.environ.get("SECRET_SCAN_USERNAMES", "").split(",")]
    try:
        text = (root / _LOCAL_USERNAMES_FILE).read_text(encoding="utf-8")
    except OSError:
        text = ""
    names.extend(line.strip() for line in text.splitlines() if not line.lstrip().startswith("#"))
    return [n for n in names if n]


def _users_pattern(root: Path) -> "re.Pattern[str]":
    users = _extra_usernames(root)
    current = _current_username()
    if current and current not in users:
        users.append(current)
    if not users:
        # An empty alternation would match a bare ``/Users/`` (fixtures).
        return re.compile(r"(?!)")
    alternation = "|".join(re.escape(u) for u in users)
    # ``/Users/<real-username>`` followed by a word boundary — NOT a bare
    # ``/Users/``. The alternation keeps the literal ``/Users/<name>`` string
    # out of this file's own bytes.
    return re.compile(r"/Users/(?:" + alternation + r")\b")


def _repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line]


def _is_skipped(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in _SKIP_PREFIXES)


def _is_allowlisted(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in _ALLOWLIST_PREFIXES)


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_EXTS:
        return True
    try:
        with path.open("rb") as handle:
            chunk = handle.read(_NUL_SNIFF_BYTES)
    except OSError:
        return True  # unreadable → treat as binary and skip
    return b"\x00" in chunk


def scan_tracked(root: Path) -> list[str]:
    """Return a list of ``path:line: [rule] match`` offenders (empty == clean)."""
    rules: list[tuple[str, "re.Pattern[str]"]] = [
        *_PROVIDER_PATTERNS,
        ("users-path-leak", _users_pattern(root)),
    ]
    offenders: list[str] = []
    for rel in _tracked_files():
        if _is_skipped(rel) or _is_allowlisted(rel):
            continue
        path = root / rel
        if not path.is_file():
            continue
        if _is_binary(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, regex in rules:
                match = regex.search(line)
                if match is not None:
                    snippet = match.group(0)
                    if len(snippet) > 60:
                        snippet = snippet[:60] + "…"
                    offenders.append(f"{rel}:{lineno}: [{name}] {snippet}")
    return offenders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="secret_scan",
        description=(
            "Scan the tracked shipping tree for provider secrets and "
            "real-username path leaks (stdlib-only, no network)."
        ),
    )
    _ = parser.add_argument(
        "--tracked",
        action="store_true",
        help="scan `git ls-files` content (the only supported mode)",
    )
    args = parser.parse_args(argv)
    if not bool(getattr(args, "tracked", False)):
        parser.error("no mode selected; pass --tracked")

    offenders = scan_tracked(_repo_root())
    if offenders:
        print(
            f"secret_scan: FAIL — {len(offenders)} offender(s) in the tracked tree:",
            file=sys.stderr,
        )
        for offender in offenders:
            print(f"  {offender}", file=sys.stderr)
        return 1

    print(
        "secret_scan: clean — no provider secrets or real-username path leaks "
        "in the tracked shipping tree."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
