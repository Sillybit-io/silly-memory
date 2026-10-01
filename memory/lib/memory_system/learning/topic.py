"""Topic clustering — group bank tags into named topics for context-pack ranking.

Read this first if you are new to the codebase:
  - Builds a small in-memory model that maps each ``#tag`` seen in the
    bank to a named topic. Two tags are clustered when their TF-IDF
    cosine similarity exceeds ``CLUSTER_THRESHOLD`` (0.6).
  - Reuses tokenizer/cosine helpers from ``learning.classifier`` so the
    math is consistent across category and topic clustering — never
    re-implement those helpers here.
  - The trained model is cached on disk at ``<store>/.topic_model.json``
    with a schema version (``CACHE_VERSION``) so stale caches from older
    code paths are rejected automatically.

Public interface (imported elsewhere): ``Topic``, ``TopicModel``,
    ``CACHE_FILENAME``, ``CACHE_VERSION``, ``CLUSTER_THRESHOLD``,
    ``TAG_TOKEN_RE``, ``DATE_PREFIX_RE``.
Depends on: safety, learning.classifier.
Used by: cli.cli_inspect, recall.context_pack_v2.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..safety import atomic_write
from .classifier import (
    _cosine,
    _normalize,
    _tfidf_vector_from_counts,
    _tokens,
)

CACHE_FILENAME = ".topic_model.json"
CACHE_VERSION = 1
CLUSTER_THRESHOLD = 0.6

TAG_TOKEN_RE = re.compile(r"#([a-z][\w-]*)", re.IGNORECASE)
DATE_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")


@dataclass(frozen=True)
class Topic:
    name: str
    weight: float
    member_tags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": self.weight,
            "member_tags": list(self.member_tags),
        }


class TopicModel:
    def __init__(self, cache_path: Path | None = None) -> None:
        self._cache_path: Path | None = cache_path
        self._topics: list[Topic] = []
        self._tag_to_topic_map: dict[str, str] = {}
        self._loaded: bool = False
        if cache_path is not None and cache_path.exists():
            self._read_cache()

    def fit(self, store: Path) -> None:
        self._cache_path = store / CACHE_FILENAME
        tag_docs = _gather_tag_docs(store)
        if not tag_docs:
            self._topics = []
            self._tag_to_topic_map = {}
            self._loaded = True
            self._write_cache()
            return

        idf = _compute_idf(tag_docs)
        vectors: dict[str, dict[str, float]] = {
            tag: _tfidf_vector_from_counts(counts, idf) for tag, counts in tag_docs.items()
        }
        clusters = _cluster_tags(vectors)
        topics: list[Topic] = []
        tag_to_topic_map: dict[str, str] = {}
        for members in clusters:
            doc_totals = {tag: sum(tag_docs[tag].values()) for tag in members}
            canonical = max(members, key=lambda tag: (doc_totals[tag], tag))
            weight = sum(_magnitude(vectors[tag]) for tag in members)
            member_tags = tuple(sorted(members))
            topics.append(Topic(name=canonical, weight=weight, member_tags=member_tags))
            for tag in members:
                tag_to_topic_map[tag] = canonical

        topics.sort(key=lambda t: (-t.weight, t.name))
        self._topics = topics
        self._tag_to_topic_map = tag_to_topic_map
        self._loaded = True
        self._write_cache()

    def topics(self, top_k: int = 10) -> list[Topic]:
        self._ensure_loaded()
        if top_k <= 0:
            return []
        return list(self._topics[:top_k])

    def tag_to_topic(self, tag: str) -> str | None:
        self._ensure_loaded()
        return self._tag_to_topic_map.get(tag)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._cache_path is not None and self._cache_path.exists():
            self._read_cache()

    def _write_cache(self) -> None:
        if self._cache_path is None:
            return
        payload = {
            "version": CACHE_VERSION,
            "topics": [topic.to_dict() for topic in self._topics],
            "tag_to_topic": dict(sorted(self._tag_to_topic_map.items())),
        }
        atomic_write(self._cache_path, json.dumps(payload, indent=2, sort_keys=True))

    def _read_cache(self) -> None:
        if self._cache_path is None or not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return

        raw_topics = data.get("topics", [])
        topics: list[Topic] = []
        if isinstance(raw_topics, list):
            for item in raw_topics:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                weight = item.get("weight")
                members = item.get("member_tags", [])
                if not isinstance(name, str) or not isinstance(weight, (int, float)):
                    continue
                if not isinstance(members, list):
                    continue
                member_tags = tuple(m for m in members if isinstance(m, str))
                topics.append(Topic(name=name, weight=float(weight), member_tags=member_tags))

        raw_map = data.get("tag_to_topic", {})
        tag_map: dict[str, str] = {}
        if isinstance(raw_map, dict):
            for key, value in raw_map.items():
                if isinstance(key, str) and isinstance(value, str):
                    tag_map[key] = value

        self._topics = topics
        self._tag_to_topic_map = tag_map
        self._loaded = True


def _gather_tag_docs(store: Path) -> dict[str, Counter[str]]:
    tag_docs: dict[str, Counter[str]] = defaultdict(Counter)
    if not store.exists():
        return dict(tag_docs)

    bank_dirs: list[Path] = []
    if store.name == "memory-bank" and store.is_dir():
        bank_dirs.append(store)
    bank_dirs.extend(p for p in sorted(store.rglob("memory-bank")) if p.is_dir())

    for bank_dir in bank_dirs:
        for md_path in sorted(bank_dir.glob("*.md")):
            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError:
                continue
            for raw_line in text.splitlines():
                parsed = _parse_bank_line(raw_line)
                if parsed is None:
                    continue
                tags, body = parsed
                body_tokens = _tokens(body)
                if not body_tokens:
                    continue
                counter = Counter(body_tokens)
                for tag in tags:
                    tag_docs[tag].update(counter)

    return dict(tag_docs)


def _parse_bank_line(raw_line: str) -> tuple[set[str], str] | None:
    line = raw_line.strip()
    if not line.startswith("- "):
        return None
    body_part = line[2:].lstrip()
    body_part = DATE_PREFIX_RE.sub("", body_part)
    if ":" not in body_part:
        return None
    tag_part, _, rest = body_part.partition(":")
    tags = {match.group(1).lower() for match in TAG_TOKEN_RE.finditer(tag_part)}
    if not tags:
        return None
    rest = rest.strip()
    if not rest:
        return None
    return tags, rest


def _compute_idf(tag_docs: dict[str, Counter[str]]) -> dict[str, float]:
    doc_count = len(tag_docs)
    document_frequency: Counter[str] = Counter()
    for counter in tag_docs.values():
        document_frequency.update(counter.keys())
    return {
        token: math.log((1 + doc_count) / (1 + freq)) + 1.0
        for token, freq in document_frequency.items()
    }


def _magnitude(vector: dict[str, float]) -> float:
    return math.sqrt(sum(value * value for value in vector.values()))


def _cluster_tags(vectors: dict[str, dict[str, float]]) -> list[list[str]]:
    tags = sorted(vectors.keys())
    parent: dict[str, str] = {tag: tag for tag in tags}

    def find(tag: str) -> str:
        while parent[tag] != tag:
            parent[tag] = parent[parent[tag]]
            tag = parent[tag]
        return tag

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    normed: dict[str, dict[str, float]] = {tag: _normalize(vec) for tag, vec in vectors.items()}
    for i, ta in enumerate(tags):
        for tb in tags[i + 1 :]:
            similarity = _cosine(normed[ta], normed[tb])
            if similarity >= CLUSTER_THRESHOLD:
                union(ta, tb)

    groups: dict[str, list[str]] = defaultdict(list)
    for tag in tags:
        groups[find(tag)].append(tag)
    return list(groups.values())


__all__ = ["Topic", "TopicModel"]
