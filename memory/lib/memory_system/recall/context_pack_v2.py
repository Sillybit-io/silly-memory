"""Context-pack v2 renderer — topic-aware, score-weighted memory injection.

Read this first if you are new to the codebase:
  - The composite ranking each entry receives is
    ``SCORE_WEIGHT * sidecar_score + RECENCY_WEIGHT * recency_factor``.
    Default weights live at module scope and are also reported by
    ``cli.cli_inspect``'s ``memwhy`` command for transparency.
  - The recency factor decays with a 30-day half-life
    (``RECENCY_HALF_LIFE_DAYS``); entries without a ``last_accessed_iso``
    fall back to ``NEUTRAL_RECENCY=0.5`` so brand-new entries don't get
    penalised.
  - Within each topic bucket, ``DEFAULT_PER_TOPIC_LIMIT=5`` caps how many
    entries land in the pack. The grand total still respects the
    ``DEFAULT_TOKEN_BUDGET`` (~16k tokens) so the pack stays small.

Public interface (imported elsewhere): ``DEFAULT_TOKEN_BUDGET``,
    ``DEFAULT_PER_TOPIC_LIMIT``, ``SCORE_WEIGHT``, ``RECENCY_WEIGHT``,
    ``RECENCY_HALF_LIFE_DAYS``, ``NEUTRAL_RECENCY``, ``GENERAL_TOPIC``,
    ``TAG_TOKEN_RE``, ``DATE_PREFIX_RE``, ``TOPIC_MODEL_CACHE``,
    ``PACK_TITLE``, ``_Entry``, ``_collect_entries``, ``_topic_for_entry``,
    ``build_context_pack_v2``.
Depends on: learning.topic, lifecycle.scoring.
Used by: cli.cli_inspect (re-uses the constants for ``memwhy``).
"""
from __future__ import annotations

import datetime as _datetime
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from memory_system.learning.topic import TopicModel
from memory_system.lifecycle.scoring import DEFAULT_SCORE, load_scores

DEFAULT_TOKEN_BUDGET: int = 16000
DEFAULT_PER_TOPIC_LIMIT: int = 5

SCORE_WEIGHT: float = 0.7
RECENCY_WEIGHT: float = 0.3
RECENCY_HALF_LIFE_DAYS: float = 30.0
NEUTRAL_RECENCY: float = 0.5

GENERAL_TOPIC: str = "general"

TAG_TOKEN_RE = re.compile(r"#([a-z][\w-]*)", re.IGNORECASE)
DATE_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")
TOPIC_MODEL_CACHE = ".topic_model.json"

PACK_TITLE = "# Memory Context Pack v2"


@dataclass(frozen=True)
class _Entry:
    entry_id: str
    path: str
    snippet: str
    tags: tuple[str, ...]
    score: float
    composite: float


def build_context_pack_v2(
    store: Path,
    *,
    token_budget_chars: int = DEFAULT_TOKEN_BUDGET,
    per_topic_limit: int = DEFAULT_PER_TOPIC_LIMIT,
) -> str:
    """Build a topic-aware, score-weighted memory pack for ``store``.

    Falls back to a single-bucket, score-only pack when no topic model cache
    is present. Output is deterministic, deduplicated, and capped at
    ``token_budget_chars`` characters.
    """
    store = Path(store)
    entries = _collect_entries(store)
    if not entries:
        return ""

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

    sections: list[tuple[str, list[_Entry]]] = []
    for topic_name in topic_order:
        bucket = sorted(grouped[topic_name], key=lambda e: (-e.composite, e.entry_id))
        bucket = _dedupe(bucket)[:effective_limit]
        if bucket:
            sections.append((topic_name, bucket))

    return _render_with_budget(sections, max(0, int(token_budget_chars)), has_topics)


def _collect_entries(store: Path) -> list[_Entry]:
    bank = store / "memory-bank"
    if not bank.exists():
        if store.name == "memory-bank" and store.is_dir():
            bank = store
        else:
            return []
    if not bank.is_dir():
        return []

    now = _datetime.datetime.now(_datetime.timezone.utc)
    result: list[_Entry] = []

    for md in sorted(bank.glob("*.md")):
        scores = cast(dict[str, dict[str, object]], load_scores(md))
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, raw in enumerate(text.splitlines(), start=1):
            parsed = _parse_bullet(raw)
            if parsed is None:
                continue
            tags, snippet = parsed
            entry_id = f"{md.name}:{lineno}"
            sidecar = scores.get(entry_id, {})
            score = _coerce_score(sidecar.get("score"))
            recency = _recency_factor(sidecar, now)
            composite = SCORE_WEIGHT * score + RECENCY_WEIGHT * recency
            result.append(
                _Entry(
                    entry_id=entry_id,
                    path=md.name,
                    snippet=snippet,
                    tags=tags,
                    score=score,
                    composite=composite,
                )
            )
    return result


def _parse_bullet(raw: str) -> tuple[tuple[str, ...], str] | None:
    line = raw.strip()
    if not line.startswith("- "):
        return None
    body = line[2:].lstrip()
    body = DATE_PREFIX_RE.sub("", body)
    if not body:
        return None
    tags: tuple[str, ...] = ()
    snippet = body
    if ":" in body:
        tag_part, _, rest = body.partition(":")
        tag_matches = [m.group(1).lower() for m in TAG_TOKEN_RE.finditer(tag_part)]
        if tag_matches:
            tags = tuple(tag_matches)
            snippet = rest.strip()
    if not snippet:
        return None
    return tags, snippet


def _coerce_score(raw: object) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    return float(DEFAULT_SCORE)


def _recency_factor(sidecar: dict[str, object], now: _datetime.datetime) -> float:
    iso_raw = sidecar.get("last_accessed_iso") or sidecar.get("created_iso")
    if not isinstance(iso_raw, str) or not iso_raw:
        return NEUTRAL_RECENCY
    iso = iso_raw
    try:
        dt = _datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_datetime.timezone.utc
        )
    except ValueError:
        return NEUTRAL_RECENCY
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return 1.0 / (1.0 + days / RECENCY_HALF_LIFE_DAYS)


def _topic_for_entry(entry: _Entry, model: TopicModel) -> str:
    for tag in entry.tags:
        mapped = model.tag_to_topic(tag)
        if mapped:
            return mapped
    return GENERAL_TOPIC


def _dedupe(entries: list[_Entry]) -> list[_Entry]:
    seen: set[str] = set()
    out: list[_Entry] = []
    for entry in entries:
        key = entry.snippet.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _render_with_budget(
    sections: list[tuple[str, list[_Entry]]],
    budget: int,
    emit_headers: bool,
) -> str:
    if not sections:
        return ""

    rendered: list[str] = [PACK_TITLE, ""]
    used = len(PACK_TITLE) + 1 + 1

    for topic_name, entries in sections:
        block: list[str] = []
        if emit_headers:
            block.append(f"## {topic_name}")
            block.append("")
        block_chars = sum(len(line) + 1 for line in block)
        if used + block_chars > budget:
            break

        pending_bullets: list[str] = []
        pending_chars = 0
        for entry in entries:
            bullet = f"- {entry.snippet}"
            extra = len(bullet) + 1
            if used + block_chars + pending_chars + extra > budget:
                break
            pending_bullets.append(bullet)
            pending_chars += extra

        if not pending_bullets:
            continue

        rendered.extend(block)
        rendered.extend(pending_bullets)
        rendered.append("")
        used += block_chars + pending_chars + 1

    if len(rendered) <= 2:
        return ""

    return "\n".join(rendered).rstrip("\n") + "\n"


__all__ = [
    "DEFAULT_PER_TOPIC_LIMIT",
    "DEFAULT_TOKEN_BUDGET",
    "build_context_pack_v2",
]
