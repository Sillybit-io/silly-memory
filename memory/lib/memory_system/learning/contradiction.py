"""Rule-based contradiction detector — flags opposing-signal bank lines.

Read this first if you are new to the codebase:
  - Scans bank markdown files for lines that share a ``#tag`` but use
    opposing signal words (``prefer X`` vs ``avoid X``, ``use Y`` vs
    ``don't use Y``). Returns confidence-scored ``ContradictionPair``
    records — never mutates the bank.
  - Auto-resolution is intentionally forbidden: a downstream lifecycle
    step or a human reviewer decides what to do (the rule-based polarity
    here is too fragile to silently demote facts).
  - Reuses ``normalize.normalize_text`` so the same fact stated with
    different aliases (e.g. "Alice" vs "A. Smith") is treated as one
    topic, not two.

Public interface (imported elsewhere): ``ContradictionPair``,
    ``detect_contradictions``, ``MEMORY_LINE_RE``, ``POSITIVE_SIGNALS``,
    and the matching negative-signal set.
Depends on: system.normalize.
Used by: cli.cli_learn_status, status.main.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..system.normalize import normalize_text

MEMORY_LINE_RE = re.compile(
    r"#(?P<tag>[a-z][\w-]*)(?:\s+#[\w-]+)*:\s*(?P<body>.+)$",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]*", re.IGNORECASE)

# Positive signal words (with common inflections) — endorsement / adoption.
POSITIVE_SIGNALS = frozenset({
    "use", "uses", "using", "used",
    "prefer", "prefers", "preferred", "preferring",
    "like", "likes", "liked", "liking",
    "want", "wants", "wanted", "wanting",
    "choose", "chooses", "chose", "chosen", "choosing",
    "adopt", "adopts", "adopted", "adopting",
    "keep", "keeps", "kept", "keeping",
    "always",
})

# Negative signal words (with common inflections) — rejection / deprecation.
NEGATIVE_SIGNALS = frozenset({
    "avoid", "avoids", "avoided", "avoiding",
    "dislike", "dislikes", "disliked", "disliking",
    "skip", "skips", "skipped", "skipping",
    "deprecated", "deprecate", "deprecates", "deprecating",
    "reject", "rejects", "rejected", "rejecting",
    "never",
})

# Standalone negation tokens excluded from topic comparison so they cannot
# spuriously boost the Jaccard overlap.
NEGATION_TOKENS = frozenset({"don", "dont", "not", "no", "never", "t"})

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "i",
    "in", "is", "it", "of", "on", "or", "out", "the", "this", "that",
    "these", "those", "to", "we", "with",
})


@dataclass(frozen=True)
class ContradictionPair:
    """A pair of bank lines that share a tag but assert opposite polarity.

    ``polarity_score`` is a confidence in ``[0, 1]``: the product of the
    polarity strengths and the topic-token Jaccard overlap. A score of 1.0
    means both lines are unambiguously opposite and cover identical topics;
    near-zero means the signal is faint and should not drive any decision.
    """

    tag: str
    line_a: str
    line_b: str
    polarity_score: float
    file_path: str


def detect_contradictions(bank_files: list[Path]) -> list[ContradictionPair]:
    """Return contradiction candidates across the given bank files.

    Comparisons are intra-file only; cross-file contradictions are left to a
    later lifecycle step that can apply workspace-aware context. Returns an
    empty list when the input is empty or no opposing pairs are found.
    """
    pairs: list[ContradictionPair] = []
    for path in bank_files:
        pairs.extend(_scan_file(path))
    return pairs


def _scan_file(path: Path) -> list[ContradictionPair]:
    if not path.exists() or not path.is_file():
        return []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    by_tag: dict[str, list[tuple[str, float, frozenset[str]]]] = defaultdict(list)
    for raw in raw_lines:
        parsed = _parse_line(raw)
        if parsed is None:
            continue
        tag, body = parsed
        polarity = _polarity(body)
        if polarity == 0.0:
            continue
        topic = _topic_tokens(body)
        if not topic:
            continue
        by_tag[tag.lower()].append((raw, polarity, topic))
    pairs: list[ContradictionPair] = []
    file_path = str(path)
    for tag, entries in by_tag.items():
        for i in range(len(entries)):
            line_a, polarity_a, topic_a = entries[i]
            for j in range(i + 1, len(entries)):
                line_b, polarity_b, topic_b = entries[j]
                if polarity_a * polarity_b >= 0.0:
                    continue
                overlap = _jaccard(topic_a, topic_b)
                if overlap <= 0.0:
                    continue
                strength = (abs(polarity_a) + abs(polarity_b)) / 2.0
                score = round(strength * overlap, 4)
                if score <= 0.0:
                    continue
                pairs.append(ContradictionPair(
                    tag=tag,
                    line_a=line_a,
                    line_b=line_b,
                    polarity_score=score,
                    file_path=file_path,
                ))
    return pairs


def _parse_line(line: str) -> tuple[str, str] | None:
    match = MEMORY_LINE_RE.search(line)
    if not match:
        return None
    body = match.group("body").strip()
    if not body:
        return None
    return match.group("tag"), body


def _polarity(body: str) -> float:
    """Return polarity in ``[-1, 1]`` for a memory line body.

    ``+1`` is unambiguously positive, ``-1`` unambiguously negative, ``0`` is
    a neutral or balanced statement that the caller treats as "no signal".
    """
    text = normalize_text(body).lower()
    negation_count, text = _apply_negation_phrases(text)
    tokens: list[str] = TOKEN_RE.findall(text)
    positive_hits = sum(1 for token in tokens if token in POSITIVE_SIGNALS)
    negative_hits = sum(1 for token in tokens if token in NEGATIVE_SIGNALS)
    negative_hits += negation_count
    if positive_hits == 0 and negative_hits == 0:
        return 0.0
    total = positive_hits + negative_hits
    return (positive_hits - negative_hits) / total


def _apply_negation_phrases(text: str) -> tuple[int, str]:
    """Strip ``don't|do not|never|not <positive>`` so the positive word does
    not double-count, and return ``(neg_count, rewritten_text)``."""
    count = 0
    rewritten = text
    for pattern in _NEGATION_PATTERNS:
        rewritten, hits = pattern.subn(" ", rewritten)
        count += hits
    return count, rewritten


def _topic_tokens(body: str) -> frozenset[str]:
    text = normalize_text(body).lower()
    text = _apply_negation_phrases(text)[1]
    tokens: list[str] = TOKEN_RE.findall(text)
    return frozenset(
        token
        for token in tokens
        if token not in STOPWORDS
        and token not in POSITIVE_SIGNALS
        and token not in NEGATIVE_SIGNALS
        and token not in NEGATION_TOKENS
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if intersection == 0:
        return 0.0
    return intersection / len(left | right)


def _build_negation_patterns() -> tuple[re.Pattern[str], ...]:
    positives = "|".join(re.escape(word) for word in sorted(POSITIVE_SIGNALS))
    bases = (
        rf"\bdon'?t\s+(?:{positives})\b",
        rf"\bdo\s+not\s+(?:{positives})\b",
        rf"\bnever\s+(?:{positives})\b",
        rf"\bnot\s+(?:{positives})\b",
        rf"\bno\s+(?:{positives})\b",
    )
    return tuple(re.compile(base, re.IGNORECASE) for base in bases)


_NEGATION_PATTERNS: tuple[re.Pattern[str], ...] = _build_negation_patterns()


__all__ = ["ContradictionPair", "detect_contradictions"]
