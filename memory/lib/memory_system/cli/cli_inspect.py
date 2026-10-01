"""Read-only inspection CLI — explains entries and context-pack ranking.

Read this first if you are new to the codebase:
  - Two commands live here: ``inspect_entry`` (``memory meminspect``) shows
    all metadata for ONE bank entry; ``explain_context_pack``
    (``memory memwhy``) shows the score breakdown for every entry currently
    in the context pack.
  - Strictly read-only — no bank file, sidecar, vector store, or topic
    cache is ever mutated. Adding write side effects here is a bug.
  - The composite score reported by ``memwhy`` is
    ``SCORE_WEIGHT * sidecar_score + RECENCY_WEIGHT * recency_factor``;
    the recency factor decays with a 30-day half-life (see
    ``recall.context_pack_v2`` for the exact constants).

Public interface (imported elsewhere): ``inspect_entry``,
    ``explain_context_pack``.
Depends on: recall.context_pack_v2, learning.topic, lifecycle.scoring,
    paths, recall.recall_hybrid, storage.vector_store.
Used by: ``bin/memory inspect`` and ``bin/memory why``.
"""

from __future__ import annotations

import datetime as _datetime
import json as _json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from memory_system.recall.context_pack_v2 import (
    DEFAULT_PER_TOPIC_LIMIT,
    GENERAL_TOPIC,
    NEUTRAL_RECENCY,
    RECENCY_HALF_LIFE_DAYS,
    RECENCY_WEIGHT,
    SCORE_WEIGHT,
    TOPIC_MODEL_CACHE,
    _Entry,
    _collect_entries,
    _topic_for_entry,
)
from memory_system.learning.topic import TAG_TOKEN_RE, TopicModel
from memory_system.lifecycle.scoring import DEFAULT_SCORE, load_scores
from memory_system.paths import workspace_store
from memory_system.recall.recall_hybrid import DEFAULT_WEIGHTS
from memory_system.storage.vector_store import INDEX_FILENAME


def _parse_entry_id(entry_id: str) -> tuple[str, int] | None:
    if ":" not in entry_id:
        return None
    filename, _, lineno_s = entry_id.rpartition(":")
    if not filename or not lineno_s:
        return None
    try:
        lineno = int(lineno_s)
    except ValueError:
        return None
    if lineno < 1:
        return None
    return filename, lineno


def _bank_line_at(bank_file: Path, lineno: int) -> str | None:
    if not bank_file.exists():
        return None
    try:
        lines = bank_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if lineno < 1 or lineno > len(lines):
        return None
    line = lines[lineno - 1]
    return line if line.strip().startswith("- ") else None


def _extract_tags(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("- "):
        body = body[2:].lstrip()
    if ":" not in body:
        return []
    tag_part, _, _ = body.partition(":")
    return [m.group(1).lower() for m in TAG_TOKEN_RE.finditer(tag_part)]


def _vector_info(store: Path, entry_id: str) -> tuple[bool, int | None]:
    """Return ``(present, dim)`` for ``entry_id`` in the vector index."""
    idx = store / INDEX_FILENAME
    if not idx.exists():
        return False, None
    try:
        payload: Any = _json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return False, None
    if not isinstance(payload, dict):
        return False, None
    ids = payload.get("ids", [])
    dim_raw = payload.get("dim")
    dim = int(dim_raw) if isinstance(dim_raw, int) else None
    if not isinstance(ids, list):
        return False, dim
    return (entry_id in ids), dim


def _topic_for_tags(tags: list[str], model: TopicModel) -> str | None:
    for tag in tags:
        mapped = model.tag_to_topic(tag)
        if mapped:
            return mapped
    return None


def _coerce_score(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    return float(DEFAULT_SCORE)


def _coerce_int(raw: Any) -> int:
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return int(raw)
    return 0


def inspect_entry(
    *,
    workspace: Path,
    entry_id: str,
    json_output: bool = False,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Pretty-print metadata for a single bank entry.

    Returns 0 on success, 1 when the entry id is unparseable or the entry
    does not exist in the bank.
    """
    parsed = _parse_entry_id(entry_id)
    if parsed is None:
        output_fn(f"missing entry: {entry_id} (unparseable id)")
        return 1
    filename, lineno = parsed

    store = workspace_store(workspace)
    bank_file = store / "memory-bank" / filename
    line = _bank_line_at(bank_file, lineno)
    if line is None:
        output_fn(f"missing entry: {entry_id}")
        return 1

    sidecar_entry: dict[str, Any] = load_scores(bank_file).get(entry_id, {}) or {}
    score = _coerce_score(sidecar_entry.get("score", DEFAULT_SCORE))
    access_count = _coerce_int(sidecar_entry.get("access_count"))
    corrections = _coerce_int(sidecar_entry.get("corrections_count"))
    reinforcements = _coerce_int(sidecar_entry.get("reinforcements_count"))
    last_accessed_raw = sidecar_entry.get("last_accessed_iso") or sidecar_entry.get(
        "created_iso"
    )
    last_accessed = last_accessed_raw if isinstance(last_accessed_raw, str) else None

    present, vector_dim = _vector_info(store, entry_id)

    topic_model = TopicModel(cache_path=store / TOPIC_MODEL_CACHE)
    tags = _extract_tags(line)
    topic = _topic_for_tags(tags, topic_model)

    snippet_body = line.strip()[2:].lstrip() if line.strip().startswith("- ") else line

    if json_output:
        payload: dict[str, Any] = {
            "entry_id": entry_id,
            "bank_file": str(bank_file),
            "line_no": lineno,
            "content": snippet_body,
            "tags": tags,
            "score": score,
            "access_count": access_count,
            "corrections_count": corrections,
            "reinforcements_count": reinforcements,
            "last_accessed_iso": last_accessed,
            "vector": {"present": present, "dim": vector_dim},
            "topic": topic,
        }
        output_fn(_json.dumps(payload, indent=2, sort_keys=True))
        return 0

    output_fn(f"Memory: {bank_file}:{lineno}")
    output_fn(f"  content: {snippet_body}")
    output_fn(f"  tags: {', '.join(tags) if tags else '(none)'}")
    output_fn(f"  score: {score:.3f}")
    output_fn(f"  access count: {access_count}")
    output_fn(f"  reinforcements: {reinforcements}")
    output_fn(f"  corrections: {corrections}")
    output_fn(f"  last accessed: {last_accessed or '(never)'}")
    if present:
        dim_s = str(vector_dim) if vector_dim is not None else "?"
        output_fn(f"  vector: present (dim={dim_s})")
    else:
        output_fn("  vector: absent")
    output_fn(f"  topic: {topic or '(unassigned)'}")
    return 0


@dataclass(frozen=True)
class _PackRow:
    entry_id: str
    snippet: str
    topic: str
    score: float
    recency: float
    composite: float
    vector_present: bool
    vector_dim: int | None


def _recency_factor(sidecar_entry: dict[str, Any], now: _datetime.datetime) -> float:
    iso_raw = sidecar_entry.get("last_accessed_iso") or sidecar_entry.get("created_iso")
    if not isinstance(iso_raw, str) or not iso_raw:
        return NEUTRAL_RECENCY
    try:
        dt = _datetime.datetime.strptime(iso_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_datetime.timezone.utc
        )
    except ValueError:
        return NEUTRAL_RECENCY
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return 1.0 / (1.0 + days / RECENCY_HALF_LIFE_DAYS)


def explain_context_pack(
    *,
    workspace: Path,
    json_output: bool = False,
    per_topic_limit: int = DEFAULT_PER_TOPIC_LIMIT,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Explain why each context-pack v2 entry was included.

    Returns 0 in every case; emits a friendly "no context pack yet" message
    when the bank is empty (or "entries: []" in JSON mode).
    """
    store = workspace_store(workspace)
    entries: list[_Entry] = _collect_entries(store)

    if not entries:
        if json_output:
            output_fn(
                _json.dumps(
                    {"entries": [], "note": "no context pack yet"},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            output_fn("no context pack yet")
        return 0

    topic_model = TopicModel(cache_path=store / TOPIC_MODEL_CACHE)
    topics = topic_model.topics(top_k=100)
    has_topics = bool(topics)

    grouped: dict[str, list[_Entry]] = {}
    for entry in entries:
        topic_name = _topic_for_entry(entry, topic_model) if has_topics else GENERAL_TOPIC
        grouped.setdefault(topic_name, []).append(entry)

    if has_topics:
        topic_order: list[str] = [t.name for t in topics if t.name in grouped]
        for name in sorted(grouped.keys()):
            if name not in topic_order:
                topic_order.append(name)
        effective_limit = max(1, int(per_topic_limit))
    else:
        topic_order = sorted(grouped.keys())
        effective_limit = max(int(per_topic_limit), len(entries))

    now = _datetime.datetime.now(_datetime.timezone.utc)
    sidecar_cache: dict[str, dict[str, dict[str, Any]]] = {}

    rows: list[_PackRow] = []
    for topic_name in topic_order:
        bucket = sorted(grouped[topic_name], key=lambda e: (-e.composite, e.entry_id))
        seen: set[str] = set()
        kept: list[_Entry] = []
        for e in bucket:
            key = e.snippet.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            kept.append(e)
            if len(kept) >= effective_limit:
                break

        for e in kept:
            if e.path not in sidecar_cache:
                sidecar_cache[e.path] = load_scores(store / "memory-bank" / e.path)
            sidecar_entry = sidecar_cache[e.path].get(e.entry_id, {}) or {}
            recency = _recency_factor(sidecar_entry, now)
            present, dim = _vector_info(store, e.entry_id)
            rows.append(
                _PackRow(
                    entry_id=e.entry_id,
                    snippet=e.snippet,
                    topic=topic_name,
                    score=e.score,
                    recency=recency,
                    composite=e.composite,
                    vector_present=present,
                    vector_dim=dim,
                )
            )

    if json_output:
        payload: dict[str, Any] = {
            "weights": {
                "score": SCORE_WEIGHT,
                "recency": RECENCY_WEIGHT,
            },
            "hybrid_recall_defaults": dict(DEFAULT_WEIGHTS),
            "entries": [
                {
                    "entry_id": r.entry_id,
                    "topic": r.topic,
                    "snippet": r.snippet,
                    "components": {
                        "sidecar_score": r.score,
                        "recency_factor": r.recency,
                        "composite": r.composite,
                    },
                    "vector": {"present": r.vector_present, "dim": r.vector_dim},
                }
                for r in rows
            ],
        }
        output_fn(_json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not rows:
        output_fn("no context pack yet")
        return 0

    output_fn(
        f"context pack v2: composite = {SCORE_WEIGHT:.1f}*sidecar_score "
        f"+ {RECENCY_WEIGHT:.1f}*recency"
    )
    current_topic: str | None = None
    for r in rows:
        if r.topic != current_topic:
            output_fn(f"[{r.topic}]")
            current_topic = r.topic
        if r.vector_present and r.vector_dim is not None:
            vec = f"vec=dim{r.vector_dim}"
        elif r.vector_present:
            vec = "vec=yes"
        else:
            vec = "vec=no"
        output_fn(
            f"  {r.entry_id}  score={r.score:.3f}  recency={r.recency:.3f}  "
            f"composite={r.composite:.3f}  {vec}"
        )
        output_fn(f"    {r.snippet}")
    return 0


__all__ = ["explain_context_pack", "inspect_entry"]
