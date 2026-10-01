"""Canonical name normalization for distilled facts.

Read this first if you are new to the codebase:
  - The normalization map lives in a LOCAL-ONLY file at
    ``<memory_home>/name-normalization.md`` — deliberately excluded from
    the shareable package because it carries environment-specific names.
    Never hardcode names here; this module ships in ``lib/``.
  - The map file is parsed at most once per process via ``lru_cache`` so
    the distiller doesn't re-read it on every fact. Cache invalidation
    means restarting the process; live edits are not picked up.
  - Mappings marked ``[skill-only]`` are skipped by the automatic
    distiller — common English words like "reason" or "constant" risk
    false replacements; the agent-driven skills apply them with human
    judgment instead.

Public interface (imported elsewhere): ``normalize_text`` (and the
    helper ``_rules`` used by tests).
Depends on: config (for ``memory_home``).
Used by: learning.classifier, learning.contradiction, recall.distiller.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .config import memory_home

_BACKTICK = re.compile(r"`([^`]+)`")


@lru_cache(maxsize=1)
def _rules() -> tuple[tuple, tuple]:
    """Return (auto_rules, all_rules); each rule is (compiled_regex, canonical)."""
    path = memory_home() / "name-normalization.md"
    if not path.exists():
        return ((), ())
    auto: list[tuple[re.Pattern, str]] = []
    allr: list[tuple[re.Pattern, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("- ") or "->" not in s:
            continue
        left, right = s.split("->", 1)
        variants = _BACKTICK.findall(left)
        canon = _BACKTICK.findall(right)
        if not variants or not canon:
            continue
        canonical = canon[0].strip()
        if not canonical:
            continue
        skill_only = "[skill-only]" in right
        for v in variants:
            v = v.strip()
            # Guard: a variant must have real content (>=2 chars, contains a word
            # char). This prevents a malformed map line (blank/1-char variant)
            # from producing a \b..\b pattern that matches everywhere.
            if len(v) < 2 or not re.search(r"\w", v):
                continue
            rule = (re.compile(r"\b" + re.escape(v) + r"\b", re.IGNORECASE), canonical)
            allr.append(rule)
            if not skill_only:
                auto.append(rule)
    return (tuple(auto), tuple(allr))


def normalize_text(text: str, include_skill_only: bool = False) -> str:
    """Rewrite transcription variants to their canonical names.

    The automatic distiller calls this with include_skill_only=False (auto-safe
    rules only). Returns text unchanged if no map file exists.
    """
    if not text:
        return text
    auto, allr = _rules()
    for rx, canonical in (allr if include_skill_only else auto):
        text = rx.sub(canonical, text)
    return text


def clear_cache() -> None:
    """Drop the cached map (tests / after editing the map file in-process)."""
    _rules.cache_clear()
