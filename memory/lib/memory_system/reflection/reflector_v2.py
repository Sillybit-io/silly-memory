"""Reflector v2 — archive then condense ``observations.md`` (supersedes v1).

Read this first if you are new to the codebase:
  - The contract is five steps: (1) read ``observations.md``; (2) archive
    the full content to ``observations-archive/YYYY-MM.md`` via
    ``storage.observations_archive.archive_observations`` BEFORE touching
    the live file (never lose evidence); (3) group bullets by topic via
    ``events.observer._classify_line``; (4) within each topic keep the
    most recent ``top_n`` lines (default 20); (5) atomic-write the
    condensed result under the workspace lock.
  - Step 2 is the load-bearing invariant. If archiving fails the live
    file is NOT rewritten — callers see an exception, not silent loss.
  - The classifier is the same one the observer uses for live event
    classification; a future task plans to swap it for the dedicated
    learning classifier.

Public interface (imported elsewhere): ``run_reflector_v2``,
    ``DEFAULT_TOP_N``.
Depends on: system.config, storage.observations_archive, events.observer,
    paths, safety.
Used by: tests only; ``reflector.py`` is still the pipeline's entry point.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import date
from pathlib import Path

from ..events.observer import _classify_line
from ..paths import ensure_layout, lock_path, workspace_store
from ..safety import atomic_write, file_lock
from ..storage.observations_archive import archive_observations
from ..system.config import load_config

DEFAULT_TOP_N = 20


def _estimate_obs_tokens(store: Path) -> int:
    obs = store / "observations.md"
    if not obs.exists():
        return 0
    return len(obs.read_text(encoding="utf-8")) // 4


def _topic_of(line: str) -> str:
    cat, _pri = _classify_line(line)
    return cat


def _bullet_lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines() if ln.strip().startswith("- ")]


def _group_by_topic(text: str) -> "OrderedDict[str, list[str]]":
    groups: OrderedDict[str, list[str]] = OrderedDict()
    for line in _bullet_lines(text):
        topic = _topic_of(line)
        groups.setdefault(topic, []).append(line)
    return groups


def _condense(text: str, top_n: int) -> tuple[str, int]:
    """Return (condensed_markdown, dropped_count).

    Sections are emitted in first-seen topic order. Within a topic we keep the
    LAST `top_n` lines (newest entries tend to be appended at the end), which
    matches the legacy truncation semantics.
    """
    groups = _group_by_topic(text)
    today = date.today().isoformat()
    out: list[str] = ["# Observations", "", f"## Condensed {today}", ""]
    dropped = 0
    for topic, lines in groups.items():
        if len(lines) > top_n:
            dropped += len(lines) - top_n
            kept = lines[-top_n:]
        else:
            kept = lines
        out.append(f"### {topic}")
        out.extend(kept)
        out.append("")
    if dropped:
        out.append(
            f"- 🟡 [{today}] #meta: Reflector v2 condensed {dropped} older "
            f"observation lines (top {top_n} per topic kept; full history in "
            f"observations-archive/)."
        )
        out.append("")
    return "\n".join(out).rstrip() + "\n", dropped


def run_reflector_v2(
    workspace_root: Path,
    force: bool = False,
    top_n: int = DEFAULT_TOP_N,
) -> bool:
    """Archive `observations.md`, then write a condensed version.

    Returns True if a reflection pass ran, False if skipped (below threshold,
    no observations, or empty content).
    """
    cfg = load_config()
    store = workspace_store(workspace_root)
    ensure_layout(store)

    raw_threshold = cfg.get("reflector_token_threshold", 20000)
    threshold = int(raw_threshold) if isinstance(raw_threshold, (float, int, str)) else 20000
    if not force and _estimate_obs_tokens(store) < threshold:
        return False

    obs_path = store / "observations.md"
    if not obs_path.exists():
        return False
    text = obs_path.read_text(encoding="utf-8")
    if not _bullet_lines(text):
        return False

    # CRITICAL: archive FIRST. archive_observations takes its own workspace
    # lock; we must not be holding `lock_path(store)` when calling it
    # (same-process flock on the same path will conflict — see tests/README.md).
    _ = archive_observations(workspace_root)

    condensed, _dropped = _condense(text, top_n=top_n)
    with file_lock(lock_path(store)):
        atomic_write(obs_path, condensed)
        atomic_write(
            store / ".reflect_state.json",
            json.dumps(
                {"ts": date.today().isoformat(), "version": 2, "top_n": top_n},
                indent=2,
            ),
        )
    return True
