"""Bank-line classifier — assigns each memory line to a category like decision/action.

Read this first if you are new to the codebase:
  - Categories are fixed: decision, action-item, preference, stakeholder,
    milestone, active-focus, observation. Each line is scored against
    every category using cosine similarity on TF-IDF token vectors built
    from a cached training set.
  - The TF-IDF cache is stored under ``<memory_home>/_classifier/tfidf.pkl``
    and refreshed when bank content changes. A confidence below
    ``DEFAULT_MIN_CONFIDENCE`` (0.4) means "don't auto-route" — the
    caller usually keeps the line as a generic observation.
  - Implementation is pure stdlib (counter, math, pickle). The module also
    exports tokenizer helpers (``_tokens``, ``_normalize``, ``_cosine``,
    ``_tfidf_vector_from_counts``) which ``learning.topic`` reuses to
    avoid duplicating the math.

Public interface (imported elsewhere): ``CATEGORIES``, ``COSINE_WEIGHT``,
    ``DEFAULT_MIN_CONFIDENCE``, ``CACHE_RELATIVE_PATH``, ``TOKEN_RE``,
    ``MEMORY_LINE_RE``, ``STOPWORDS``, plus the cosine/tokenizer helpers
    re-used by ``topic``.
Depends on: system.config, system.normalize, safety.
Used by: learning.topic.
"""
from __future__ import annotations

import base64
import math
import pickle
import re
from collections.abc import Mapping
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..system.config import load_config, memory_home
from ..system.normalize import normalize_text
from ..safety import atomic_write

CATEGORIES = (
    "decision",
    "action-item",
    "preference",
    "stakeholder",
    "milestone",
    "active-focus",
    "observation",
)
COSINE_WEIGHT = 2.5
DEFAULT_MIN_CONFIDENCE = 0.4
CACHE_RELATIVE_PATH = Path("_classifier") / "tfidf.pkl"
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]*", re.IGNORECASE)
MEMORY_LINE_RE = re.compile(r"#(?P<label>[a-z][\w-]*):\s*(?P<body>.+)$", re.IGNORECASE)
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "out",
    "the",
    "to",
    "with",
}


# Why: frozen so the cached idf table cannot be mutated by callers — protects the in-memory recall index.
@dataclass(frozen=True)
class ClassifierState:
    idf: dict[str, float]
    centroids: dict[str, dict[str, float]]
    labels: tuple[str, ...]
    training_count: int


_classifier_instance: "TfidfRuleClassifier | None" = None


def classify(line: str, min_confidence: float | None = None) -> tuple[str, float]:
    threshold = _threshold(min_confidence)
    return _classifier().classify(line, threshold)


def reset_classifier_for_tests() -> None:
    global _classifier_instance
    _classifier_instance = None


def _threshold(min_confidence: float | None) -> float:
    if min_confidence is not None:
        return float(min_confidence)
    raw = load_config().get("classifier_min_confidence", DEFAULT_MIN_CONFIDENCE)
    if not isinstance(raw, (int, float, str)):
        return DEFAULT_MIN_CONFIDENCE
    return float(raw)


def _classifier() -> "TfidfRuleClassifier":
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = TfidfRuleClassifier()
    return _classifier_instance


class TfidfRuleClassifier:
    def __init__(self, cache_path: Path | None = None) -> None:
        self.cache_path: Path = cache_path or memory_home() / CACHE_RELATIVE_PATH
        self._state: ClassifierState | None = None

    def classify(self, line: str, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> tuple[str, float]:
        state = self._load_or_fit()
        scores = self._scores(state, line)
        if not scores:
            return "unknown", 0.0
        label, confidence = max(scores.items(), key=lambda item: (item[1], item[0]))
        confidence = max(0.0, min(1.0, confidence))
        if confidence < min_confidence:
            return "unknown", confidence
        return label, confidence

    def _load_or_fit(self) -> ClassifierState:
        if self._state is not None:
            return self._state
        cached = self._read_cache()
        if cached is not None:
            self._state = cached
            return cached
        examples = _load_labeled_memories(memory_home())
        examples.extend(_seed_examples())
        self._state = _fit(examples)
        self._write_cache(self._state)
        return self._state

    def _scores(self, state: ClassifierState, line: str) -> dict[str, float]:
        vector = _tfidf_vector(_tokens(line), state.idf)
        scores: dict[str, float] = {}
        for label in state.labels:
            scores[label] = _cosine(vector, state.centroids.get(label, {})) * COSINE_WEIGHT
        for label, boost in _keyword_boosts(line).items():
            if label in scores:
                scores[label] += boost
        return scores

    def _read_cache(self) -> ClassifierState | None:
        if not self.cache_path.exists():
            return None
        try:
            payload = self.cache_path.read_text(encoding="utf-8")
            raw = base64.b64decode(payload.encode("ascii"))
            data = pickle.loads(raw)
        except (OSError, ValueError, pickle.PickleError, EOFError):
            return None
        if not isinstance(data, dict):
            return None
        idf = _string_float_dict(data.get("idf"))
        centroids = _centroid_dict(data.get("centroids"))
        labels = _labels_tuple(data.get("labels"))
        training_count = _int_value(data.get("training_count"))
        if not idf or not centroids or not labels:
            return None
        return ClassifierState(idf=idf, centroids=centroids, labels=labels, training_count=training_count)

    def _write_cache(self, state: ClassifierState) -> None:
        data = {
            "version": 1,
            "idf": state.idf,
            "centroids": state.centroids,
            "labels": state.labels,
            "training_count": state.training_count,
        }
        payload = base64.b64encode(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii")
        atomic_write(self.cache_path, payload)


def _load_labeled_memories(root: Path) -> list[tuple[str, str]]:
    examples: list[tuple[str, str]] = []
    if not root.exists():
        return examples
    for bank_dir in sorted(root.glob("**/memory-bank")):
        if not bank_dir.is_dir():
            continue
        for path in sorted(bank_dir.glob("*.md")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                parsed = _parse_labeled_line(line)
                if parsed is not None:
                    examples.append(parsed)
    return examples


def _parse_labeled_line(line: str) -> tuple[str, str] | None:
    match = MEMORY_LINE_RE.search(line)
    if not match:
        return None
    label = match.group("label").lower()
    if label not in CATEGORIES:
        return None
    body = match.group("body").strip()
    if not body:
        return None
    return label, body


def _fit(examples: list[tuple[str, str]]) -> ClassifierState:
    doc_tokens = [(_label, Counter(_tokens(text))) for _label, text in examples]
    doc_count = len(doc_tokens)
    document_frequency: Counter[str] = Counter()
    for _label, counts in doc_tokens:
        document_frequency.update(counts.keys())
    idf = {
        token: math.log((1 + doc_count) / (1 + frequency)) + 1.0
        for token, frequency in document_frequency.items()
    }

    grouped: dict[str, list[dict[str, float]]] = defaultdict(list)
    for label, counts in doc_tokens:
        grouped[label].append(_tfidf_vector_from_counts(counts, idf))

    centroids = {label: _centroid(vectors) for label, vectors in grouped.items() if vectors}
    labels = tuple(label for label in CATEGORIES if label in centroids)
    return ClassifierState(idf=idf, centroids=centroids, labels=labels, training_count=doc_count)


def _seed_examples() -> list[tuple[str, str]]:
    return [
        ("decision", "Decided to use PostgreSQL for records"),
        ("decision", "Team agreed the architecture direction"),
        ("action-item", "Follow up with owner by Friday"),
        ("action-item", "Action item has owner and due date"),
        ("preference", "Prefer explicit tests and never hide behavior"),
        ("preference", "Always keep modules small"),
        ("stakeholder", "Maya is the PM for platform"),
        ("stakeholder", "Jordan is the engineering lead"),
        ("stakeholder", "Nina is the director for enterprise programs"),
        ("milestone", "Completed migration milestone"),
        ("milestone", "Delivered phase one rollout"),
        ("active-focus", "Current focus is classifier hardening"),
        ("active-focus", "Active work is checkout stabilization"),
        ("observation", "Test suite runs with python unittest only"),
        ("observation", "Repository uses stdlib unittest"),
        ("observation", "Memory bank stores markdown bullets"),
    ]


def _tokens(text: str) -> list[str]:
    normalized = normalize_text(text).lower()
    raw_tokens: list[str] = TOKEN_RE.findall(normalized)
    return [_stem(token) for token in raw_tokens if token not in STOPWORDS]


def _stem(token: str) -> str:
    irregular = {
        "agreed": "agree",
        "decided": "decide",
        "completed": "complete",
        "delivered": "deliver",
    }
    if token in irregular:
        return irregular[token]
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def _tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    return _tfidf_vector_from_counts(Counter(token for token in tokens if token in idf), idf)


def _tfidf_vector_from_counts(counts: Counter[str], idf: dict[str, float]) -> dict[str, float]:
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {token: (count / total) * idf[token] for token, count in counts.items() if token in idf}


def _centroid(vectors: list[dict[str, float]]) -> dict[str, float]:
    if not vectors:
        return {}
    total: defaultdict[str, float] = defaultdict(float)
    for vector in vectors:
        for token, value in vector.items():
            total[token] += value
    centroid = {token: value / len(vectors) for token, value in total.items()}
    return _normalize(centroid)


def _normalize(vector: dict[str, float]) -> dict[str, float]:
    length = math.sqrt(sum(value * value for value in vector.values()))
    if length == 0:
        return {}
    return {token: value / length for token, value in vector.items()}


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    left_norm = _normalize(left)
    if not left_norm:
        return 0.0
    if len(left_norm) > len(right):
        left_norm, right = right, left_norm
    return sum(value * right.get(token, 0.0) for token, value in left_norm.items())


def _keyword_boosts(line: str) -> dict[str, float]:
    lower = line.lower()
    boosts: dict[str, float] = {}
    if any(word in lower for word in ("decided", "decide", "decision", "agreed", "mandate")):
        boosts["decision"] = boosts.get("decision", 0.0) + 0.1
    if any(word in lower for word in ("action", "owner:", "due:", "follow up", "follow-up")):
        boosts["action-item"] = boosts.get("action-item", 0.0) + 0.1
    if any(word in lower for word in ("prefer", "always", "never", "hard rule")):
        boosts["preference"] = boosts.get("preference", 0.0) + 0.1
    if re.search(r"\b(is|are)\s+(the|a)\s+", lower) and any(
        word in lower for word in ("pm", "lead", "director", "engineer", "tl", "el")
    ):
        boosts["stakeholder"] = boosts.get("stakeholder", 0.0) + 0.1
    if any(word in lower for word in ("milestone", "delivered", "completed")):
        boosts["milestone"] = boosts.get("milestone", 0.0) + 0.1
    if any(word in lower for word in ("focus", "active", "current")):
        boosts["active-focus"] = boosts.get("active-focus", 0.0) + 0.1
    return boosts


def _labels_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(label for label in value if isinstance(label, str) and label in CATEGORIES)


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    return 0


def _string_float_dict(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, (int, float)):
            out[key] = float(item)
    return out


def _centroid_dict(value: object) -> dict[str, dict[str, float]]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, dict[str, float]] = {}
    for label, vector in value.items():
        if isinstance(label, str) and label in CATEGORIES:
            parsed = _string_float_dict(vector)
            if parsed:
                out[label] = parsed
    return out


__all__ = ["CATEGORIES", "DEFAULT_MIN_CONFIDENCE", "TfidfRuleClassifier", "classify"]
