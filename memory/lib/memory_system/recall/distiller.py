"""Distiller — promote durable bullets from observations.md into the memory bank.

Read this first if you are new to the codebase:
  - Reads ``observations.md`` and copies bullets that look like durable
    facts into the right bank file (``activeContext.md``, ``actionItems.md``,
    etc.). Categories in ``SKIP_CATEGORIES`` (file-edit, shell, meta,
    observation) are NEVER promoted; they stay in the observation log.
  - Deduplication uses a content hash from ``events.content_hash`` so the
    same bullet text isn't promoted twice. The seen-cache and staging
    buffer have hard caps (``SEEN_CAP=5000``, ``STAGING_CAP=500``) to
    keep memory use bounded over time.
  - Name normalization runs every promoted line through
    ``normalize.normalize_text`` so user-specific aliases (people, repos)
    converge on canonical forms before they hit the bank.

Public interface (imported elsewhere): ``distill_from_observations``,
    ``SKIP_CATEGORIES``, ``SEEN_CAP``, ``STAGING_CAP``.
Depends on: system.config, events, system.normalize, paths, safety.
Used by: events.worker (called inside ``process_queue``).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import cast

from memory_system.events import content_hash
from memory_system.paths import bank_path, ensure_layout, global_store, lock_path, workspace_store
from memory_system.safety import LockTimeout, atomic_write, file_lock
from memory_system.system.config import load_config
from memory_system.system.normalize import normalize_text

SKIP_CATEGORIES = {"file-edit", "shell", "meta", "observation"}
SEEN_CAP = 5000
STAGING_CAP = 500


def _insert_bullet(path: Path, bullet: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {path.stem}\n\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if bullet in text:
        return
    lines = text.splitlines()
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## ") and i > 0:
            insert_at = i
            break
        if line.strip() == "" and i > 2:
            insert_at = i
            break
    new_lines = lines[:insert_at] + [bullet, ""] + lines[insert_at:]
    atomic_write(path, "\n".join(new_lines).rstrip() + "\n")


def _parse_observation(line: str) -> dict | None:
    m = re.search(r"#(\w[\w-]*)(?:\s+#[\w-]+)*:\s*(.+)$", line)
    if not m:
        return None
    cat = m.group(1)
    body = m.group(2)
    conf = 0.9 if "owner:" in body.lower() or "due:" in body.lower() else 0.75
    return {"category": cat, "body": body, "confidence": conf}


def _existing_line_hashes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {content_hash(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def promote_staging(workspace_root: Path) -> int:
    cfg = cast(dict[str, object], load_config())
    store = workspace_store(workspace_root)
    staging = store / "staging" / "pending.jsonl"
    if not staging.exists():
        return 0
    threshold = float(cast(int | float | str, cfg.get("auto_promote_confidence", 0.85)))
    promoted = 0
    remaining: list[str] = []
    seen_remaining: set[str] = set()
    try:
        with file_lock(lock_path(store), timeout=5.0):
            bank_hashes: dict[Path, set[str]] = {}
            global_routes = cast(dict[str, str], cfg.get("global_bank_routes", {}))
            workspace_routes = cast(dict[str, str], cfg.get("workspace_bank_routes", {}))
            for line in staging.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if float(item.get("confidence", 0)) < threshold:
                    # Keep below-threshold items for later, but never let duplicates pile
                    # up (the prior bug: identical low-confidence items re-staged forever).
                    key = content_hash(line)
                    if key not in seen_remaining:
                        seen_remaining.add(key)
                        remaining.append(line)
                    continue
                cat = item.get("category", "observation")
                today = date.today().isoformat()
                body = normalize_text(item.get("body", ""))
                bullet = f"- [{today}] #{cat}: {body}"
                if cat in global_routes:
                    target_store = global_store()
                    fname = global_routes[cat]
                else:
                    target_store = store
                    fname = workspace_routes.get(cat, "activeContext.md")
                bank_file = bank_path(target_store, fname)
                hashes = bank_hashes.get(bank_file)
                if hashes is None:
                    hashes = _existing_line_hashes(bank_file)
                    bank_hashes[bank_file] = hashes
                key = content_hash(bullet)
                if key in hashes:
                    continue
                _insert_bullet(bank_file, bullet)
                hashes.add(key)
                promoted += 1
            # Hard cap as a safety net against unbounded growth.
            if len(remaining) > STAGING_CAP:
                remaining = remaining[-STAGING_CAP:]
            atomic_write(staging, "\n".join(remaining) + ("\n" if remaining else ""))
            return promoted
    except LockTimeout:
        sys.stderr.write("promote_staging: lock timeout; skipping promotion\n")
        return 0


def _load_seen(store: Path) -> tuple[list[str], set[str]]:
    state_path = store / ".distiller_state.json"
    if not state_path.exists():
        return [], set()
    try:
        order = json.loads(state_path.read_text(encoding="utf-8")).get("seen", [])
    except (json.JSONDecodeError, OSError):
        return [], set()
    return order, set(order)


def distill_from_observations(workspace_root: Path) -> int:
    cfg = load_config()
    store = workspace_store(workspace_root)
    ensure_layout(store)
    obs_path = store / "observations.md"
    if not obs_path.exists():
        return 0

    seen_order, seen = _load_seen(store)
    new_items: list[str] = []
    staged = 0
    today = date.today().isoformat()
    for raw in obs_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        # Idempotency: each observation line is processed at most once, ever.
        h = content_hash(line)
        if h in seen:
            continue
        seen.add(h)
        seen_order.append(h)
        parsed = _parse_observation(line)
        if not parsed or parsed["category"] in SKIP_CATEGORIES:
            continue
        item = {
            "category": parsed["category"],
            "body": parsed["body"] + f"; source: session-{today}",
            "confidence": parsed["confidence"],
        }
        new_items.append(json.dumps(item))
        staged += 1

    if new_items:
        staging = store / "staging" / "pending.jsonl"
        with file_lock(lock_path(store)):
            with staging.open("a", encoding="utf-8") as f:
                f.write("\n".join(new_items) + "\n")

    # Persist the seen-set, bounded so it can't grow without limit.
    if len(seen_order) > SEEN_CAP:
        seen_order = seen_order[-SEEN_CAP:]
    atomic_write(store / ".distiller_state.json", json.dumps({"seen": seen_order}))

    promote_staging(workspace_root)
    return staged
