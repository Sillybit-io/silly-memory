from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from .. import privacy
from .embedding.noop_backend import NoopEmbeddingBackend
from .llm.noop_backend import NoopLLMBackend

_privacy_logged = False


def _normalize_name(value: object | None, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip().lower()
    return text or default


def _try_sentence_transformers() -> object | None:
    try:
        from .embedding.sentence_transformers_backend import SentenceTransformersBackend
    except ImportError:
        return None
    try:
        backend = SentenceTransformersBackend()
    except Exception:
        return None
    if not backend.is_available():
        return None
    return backend


def _try_fastembed() -> object | None:
    try:
        from .embedding.fastembed_backend import FastembedBackend
    except ImportError:
        return None
    try:
        backend = FastembedBackend()
    except Exception:
        return None
    if not backend.is_available():
        return None
    return backend


def _log_privacy_mode_once(backend: object) -> None:
    global _privacy_logged
    if _privacy_logged:
        return
    mode = "online" if privacy.network_allowed() else "offline"
    backend_name = str(getattr(backend, "name", backend.__class__.__name__))
    print(f"[privacy] mode={mode} backend={backend_name}", file=sys.stderr)
    _privacy_logged = True


def get_embedding_backend(config: Mapping[str, object]) -> object:
    requested = _normalize_name(
        os.getenv("MEMORY_EMBEDDING_BACKEND")
        or config.get("embedding_backend")
        or config.get("embedding_model")
    )

    if requested == "noop":
        backend = NoopEmbeddingBackend()
        _log_privacy_mode_once(backend)
        return backend

    if requested in ("", "auto", "sentence-transformers", "sentence_transformers", "st"):
        chain = (_try_sentence_transformers, _try_fastembed)
    elif requested in ("fastembed", "onnx"):
        chain = (_try_fastembed, _try_sentence_transformers)
    else:
        chain = (_try_sentence_transformers, _try_fastembed)

    for attempt in chain:
        backend = attempt()
        if backend is not None:
            _log_privacy_mode_once(backend)
            return backend
    backend = NoopEmbeddingBackend()
    _log_privacy_mode_once(backend)
    return backend


def get_llm_backend(config: Mapping[str, object]) -> NoopLLMBackend:
    _ = _normalize_name(
        os.getenv("MEMORY_LLM_BACKEND")
        or config.get("reflect_model")
        or config.get("observe_model")
        or config.get("llm_backend")
    )
    backend = NoopLLMBackend()
    _log_privacy_mode_once(backend)
    return backend
