"""Explicit facts — "remember that …" prompts and direct additions, stored at full confidence.

Read this first if you are new to the codebase:
  - ``extract_explicit_fact`` fires only when the imperative opens the prompt
    ("remember that …", "remember: …", "please remember that …"). Narrative
    uses such as "remember that time …" and a mid-sentence "remember that" are
    ignored. The fact is the rest of the prompt's first line.
  - ``store_explicit_fact`` sanitizes the text (``<private>`` spans and secret
    patterns), routes it to a bank file by category, and writes it at most
    once: a fact already in the target bank — typed again, captured by the
    observer after a direct addition, or replayed after a retry — only has its
    score metadata repaired.
  - New bullets go at the end of the bank file so existing ``filename:line``
    entry ids keep pointing at the same lines. The fact's score sidecar entry
    gets ``score: 1.0`` and the ``explicit`` tag.
  - Lock order is store lock, then sidecar lock. Both are released before the
    search index is rebuilt, because ``index.rebuild_index`` takes the store
    lock itself and waits for it without a timeout.

Public interface (imported elsewhere): ``extract_explicit_fact``,
    ``store_explicit_fact``, ``EXPLICIT_TAG``, ``EXPLICIT_SCORE``, ``SCOPES``.
Depends on: config, index, learning.classifier, lifecycle.scoring, paths,
    redact, safety, system.normalize.
Used by: events.observer.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import cast

from memory_system.system.config import load_config
from memory_system.index import rebuild_index
from memory_system.learning.classifier import classify
from memory_system.lifecycle.scoring import upsert_entry
from memory_system.paths import bank_path, ensure_layout, global_store, lock_path, workspace_store
from memory_system.redact import PRIVATE_PLACEHOLDER, redact_text
from memory_system.safety import atomic_write, file_lock
from memory_system.system.normalize import normalize_text

EXPLICIT_TAG = "explicit"
EXPLICIT_SCORE = 1.0
SCOPES = ("auto", "workspace", "global")
STORE_LOCK_TIMEOUT = 5.0
UNCLASSIFIED_FILE = "domainContext.md"
GLOBAL_FALLBACK_FILE = "learned-memories.md"
_UNTAGGED_CATEGORIES = frozenset({"unknown", "observation"})

_TRIGGER = re.compile(
    r"^\s*(?:please\s+)?remember(?:\s+that\b(?!\s+(?:one\s+)?time\b)|\s*:)(?P<fact>.*)",
    re.IGNORECASE | re.DOTALL,
)
_BULLET = re.compile(r"^- (?:\[[^\]]*\]\s*)?(?:#[\w-]+\s*)*:?\s*(?P<body>.*)$")
_SOURCE_SUFFIX = re.compile(r";\s*source:.*$", re.IGNORECASE)


_WRAPPING_QUOTES = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}


def _meaningful(text: str) -> bool:
    return bool(re.search(r"\w", text.replace(PRIVATE_PLACEHOLDER, "")))


def _unwrap(prompt: str) -> str:
    """A prompt sent whole inside one pair of quotes, without them.

    ``opencode run "remember that ..."`` records the message as ``"remember that ..."``,
    escaping the quotes inside it; only such a fully wrapped prompt is unwrapped.
    """
    text = prompt.strip()
    closer = _WRAPPING_QUOTES.get(text[:1])
    if closer is None or len(text) < 2 or not text.endswith(closer):
        return prompt
    return text[1:-1].replace("\\" + closer, closer)


def extract_explicit_fact(prompt: str) -> str | None:
    """The fact a prompt asks to remember, or None when it is not such a request."""
    match = _TRIGGER.match(redact_text(_unwrap(prompt or "")))
    if not match:
        return None
    lines = match.group("fact").strip().splitlines()
    fact = lines[0].strip() if lines else ""
    return fact if _meaningful(fact) else None


def _fact_key(body: str) -> str:
    body = _SOURCE_SUFFIX.sub("", body)
    body = normalize_text(body).lower()
    return re.sub(r"\s+", " ", body).strip(" .!;,")


def _find_fact(lines: list[str], key: str) -> int | None:
    for lineno, line in enumerate(lines, start=1):
        match = _BULLET.match(line.strip())
        if match and _fact_key(match.group("body")) == key:
            return lineno
    return None


def _route(workspace_root: Path, category: str, scope: str) -> tuple[Path, str]:
    cfg = load_config()
    global_routes = cast(dict[str, str], cfg.get("global_bank_routes", {}))
    workspace_routes = cast(dict[str, str], cfg.get("workspace_bank_routes", {}))
    if scope == "global":
        return global_store(), global_routes.get(category, GLOBAL_FALLBACK_FILE)
    if scope == "auto" and category in global_routes:
        return global_store(), global_routes[category]
    return workspace_store(workspace_root), workspace_routes.get(category, UNCLASSIFIED_FILE)


def store_explicit_fact(
    workspace_root: Path,
    text: str,
    scope: str = "auto",
    *,
    reindex: bool = True,
) -> Path | None:
    """Store ``text`` as an explicit fact; return its bank file, or None when empty.

    ``scope`` is ``auto`` (route by category), ``workspace``, or ``global``.
    Raises ``LockTimeout`` when the target store stays locked; the fact is then
    not written and the caller can retry. A failed index rebuild after a
    successful write is reported on stderr and does not undo the write.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    body = " ".join(redact_text(text or "").split())
    if not _meaningful(body):
        return None
    body = normalize_text(body)
    category, _confidence = classify(body)
    store, filename = _route(Path(workspace_root), category, scope)
    ensure_layout(store)
    bank_file = bank_path(store, filename)
    key = _fact_key(body)
    tags = f"#{EXPLICIT_TAG}" if category in _UNTAGGED_CATEGORIES else f"#{EXPLICIT_TAG} #{category}"

    with file_lock(lock_path(store), timeout=STORE_LOCK_TIMEOUT):
        if bank_file.exists():
            lines = bank_file.read_text(encoding="utf-8").splitlines()
            lineno = _find_fact(lines, key)
        else:
            lines = [f"# {bank_file.stem}", ""]
            lineno = None
        if lineno is None:
            while len(lines) > 2 and not lines[-1].strip():
                lines.pop()
            lines.append(f"- [{date.today().isoformat()}] {tags}: {body}")
            atomic_write(bank_file, "\n".join(lines) + "\n")
            lineno = len(lines)
        _ = upsert_entry(bank_file, f"{bank_file.name}:{lineno}", score=EXPLICIT_SCORE, tags=(EXPLICIT_TAG,))

    if reindex:
        try:
            _ = rebuild_index(store, store.name)
        except Exception as exc:
            print(f"memory: saved to {bank_file} but the search index was not updated: {exc}", file=sys.stderr)
    return bank_file
