from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _has(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


HAS_SENTENCE_TRANSFORMERS = _has("sentence_transformers") and _has("torch")
HAS_FASTEMBED = _has("fastembed")


class TestSentenceTransformersBackendShape(unittest.TestCase):
    """Module-level + class-level shape — does not require torch to be installed."""

    def test_module_importable_without_torch(self) -> None:
        for name in list(sys.modules):
            if name == "torch" or name.startswith("torch."):
                del sys.modules[name]
            if name == "sentence_transformers" or name.startswith("sentence_transformers."):
                del sys.modules[name]
        mod_name = "memory.lib.memory_system.backends.embedding.sentence_transformers_backend"
        if mod_name in sys.modules:
            del sys.modules[mod_name]

        before = set(sys.modules)
        mod = importlib.import_module(mod_name)

        self.assertTrue(hasattr(mod, "SentenceTransformersBackend"))
        self.assertTrue(hasattr(mod.SentenceTransformersBackend, "download_model"))
        after = set(sys.modules)
        leaked = {"torch", "sentence_transformers"} & (after - before)
        self.assertFalse(
            leaked,
            f"importing sentence_transformers_backend leaked ML modules into sys.modules: {leaked}",
        )

    def test_lazy_import_torch_not_loaded_at_module_import(self) -> None:
        # Drop any pre-existing references so we measure a fresh import.
        for name in list(sys.modules):
            if name == "torch" or name.startswith("torch."):
                del sys.modules[name]
            if name == "sentence_transformers" or name.startswith("sentence_transformers."):
                del sys.modules[name]

        # Re-import the backend module fresh
        mod_name = "memory.lib.memory_system.backends.embedding.sentence_transformers_backend"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        importlib.import_module(mod_name)

        self.assertNotIn("torch", sys.modules)
        self.assertNotIn("sentence_transformers", sys.modules)

    def test_class_attributes(self) -> None:
        from memory.lib.memory_system.backends.embedding.sentence_transformers_backend import (
            SentenceTransformersBackend,
        )

        self.assertEqual(SentenceTransformersBackend.name, "sentence-transformers")
        self.assertEqual(SentenceTransformersBackend.dim, 384)
        self.assertEqual(
            SentenceTransformersBackend.model_name,
            "sentence-transformers/all-MiniLM-L6-v2",
        )

    def test_is_available_false_without_torch(self) -> None:
        from memory.lib.memory_system.backends.embedding.sentence_transformers_backend import (
            SentenceTransformersBackend,
        )

        # Sentinel-block the imports so the backend cannot load torch even if installed.
        with patch.dict(sys.modules, {"torch": None, "sentence_transformers": None}):
            backend = SentenceTransformersBackend()
            self.assertFalse(backend.is_available())

    def test_model_cache_path_honors_cursor_memory_home(self) -> None:
        from memory.lib.memory_system.backends.embedding.sentence_transformers_backend import (
            SentenceTransformersBackend,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SILLY_MEMORY_HOME": tmp}, clear=False):
                backend = SentenceTransformersBackend()
                expected = Path(tmp).resolve() / "_embeddings" / "sentence-transformers-all-MiniLM-L6-v2"
                self.assertEqual(backend.model_path, expected)


@unittest.skipUnless(
    HAS_SENTENCE_TRANSFORMERS,
    "sentence_transformers + torch not installed; skipping live encode test",
)
class TestSentenceTransformersBackendLive(unittest.TestCase):
    """Live encode tests — only run when the ML libs are present."""

    def test_encode_returns_384_dim_vector(self) -> None:
        from memory.lib.memory_system.backends.embedding.sentence_transformers_backend import (
            SentenceTransformersBackend,
        )

        backend = SentenceTransformersBackend()
        if not backend.is_available():
            backend.download_model()

        vectors = backend.encode(["hello world"])
        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(vectors[0]), 384)
        self.assertTrue(all(isinstance(v, float) for v in vectors[0]))

    def test_encode_round_trip_same_shape(self) -> None:
        from memory.lib.memory_system.backends.embedding.sentence_transformers_backend import (
            SentenceTransformersBackend,
        )

        backend = SentenceTransformersBackend()
        if not backend.is_available():
            backend.download_model()

        first = backend.encode(["hello", "world"])
        second = backend.encode(["hello", "world"])

        self.assertEqual(len(first), len(second))
        self.assertEqual(len(first[0]), len(second[0]))
        self.assertEqual(len(first[0]), 384)


class TestFastembedBackendShape(unittest.TestCase):
    """Module-level + class-level shape — does not require fastembed to be installed."""

    def test_module_importable_without_fastembed(self) -> None:
        for name in list(sys.modules):
            if name == "fastembed" or name.startswith("fastembed."):
                del sys.modules[name]
        mod_name = "memory.lib.memory_system.backends.embedding.fastembed_backend"
        if mod_name in sys.modules:
            del sys.modules[mod_name]

        before = set(sys.modules)
        mod = importlib.import_module(mod_name)

        self.assertTrue(hasattr(mod, "FastembedBackend"))
        self.assertTrue(hasattr(mod.FastembedBackend, "download_model"))
        after = set(sys.modules)
        leaked = {"fastembed"} & (after - before)
        self.assertFalse(
            leaked,
            f"importing fastembed_backend leaked fastembed into sys.modules: {leaked}",
        )

    def test_class_attributes(self) -> None:
        from memory.lib.memory_system.backends.embedding.fastembed_backend import (
            DEFAULT_MODEL_ID,
            FastembedBackend,
        )

        self.assertEqual(FastembedBackend.name, "fastembed")
        self.assertEqual(FastembedBackend.dim, 384)
        self.assertEqual(DEFAULT_MODEL_ID, "BAAI/bge-small-en-v1.5")

    def test_is_available_false_without_fastembed(self) -> None:
        from memory.lib.memory_system.backends.embedding.fastembed_backend import (
            FastembedBackend,
        )

        with patch.dict(sys.modules, {"fastembed": None}):
            backend = FastembedBackend()
            self.assertFalse(backend.is_available())

    def test_model_cache_path_honors_cursor_memory_home(self) -> None:
        from memory.lib.memory_system.backends.embedding.fastembed_backend import (
            FastembedBackend,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SILLY_MEMORY_HOME": tmp}, clear=False):
                backend = FastembedBackend()
                expected = (
                    Path(tmp).resolve()
                    / "_embeddings"
                    / "fastembed-bge-small-en-v1.5"
                )
                self.assertEqual(backend.model_dir, expected)

    def test_encode_raises_without_fastembed_installed(self) -> None:
        from memory.lib.memory_system.backends.embedding.fastembed_backend import (
            FastembedBackend,
        )

        with patch.dict(sys.modules, {"fastembed": None}):
            backend = FastembedBackend()
            with self.assertRaisesRegex(RuntimeError, "fastembed not installed"):
                _ = backend.encode(["hello"])


class TestEmbeddingFactoryFallbackChain(unittest.TestCase):
    """Factory falls back from sentence-transformers → fastembed → noop."""

    def test_returns_noop_when_both_backends_unavailable(self) -> None:
        from memory.lib.memory_system.backends.embedding.noop_backend import (
            NoopEmbeddingBackend,
        )
        from memory.lib.memory_system.backends.factory import get_embedding_backend

        with patch.dict(
            sys.modules,
            {"torch": None, "sentence_transformers": None, "fastembed": None},
        ):
            backend = get_embedding_backend({})

        self.assertIsInstance(backend, NoopEmbeddingBackend)

    def test_explicit_noop_request_returns_noop(self) -> None:
        from memory.lib.memory_system.backends.embedding.noop_backend import (
            NoopEmbeddingBackend,
        )
        from memory.lib.memory_system.backends.factory import get_embedding_backend

        with patch.dict(os.environ, {"MEMORY_EMBEDDING_BACKEND": "noop"}, clear=False):
            backend = get_embedding_backend({})

        self.assertIsInstance(backend, NoopEmbeddingBackend)

    def test_fastembed_request_falls_back_when_missing(self) -> None:
        from memory.lib.memory_system.backends.embedding.noop_backend import (
            NoopEmbeddingBackend,
        )
        from memory.lib.memory_system.backends.factory import get_embedding_backend

        with patch.dict(
            sys.modules,
            {"torch": None, "sentence_transformers": None, "fastembed": None},
        ):
            with patch.dict(
                os.environ, {"MEMORY_EMBEDDING_BACKEND": "fastembed"}, clear=False
            ):
                backend = get_embedding_backend({})

        self.assertIsInstance(backend, NoopEmbeddingBackend)


@unittest.skipUnless(
    HAS_FASTEMBED,
    "fastembed not installed; skipping live encode test",
)
class TestFastembedBackendLive(unittest.TestCase):
    """Live encode tests — only run when fastembed is present."""

    def test_encode_returns_384_dim_vector(self) -> None:
        from memory.lib.memory_system.backends.embedding.fastembed_backend import (
            FastembedBackend,
        )

        backend = FastembedBackend()
        if not backend.is_available():
            backend.download_model()

        vectors = backend.encode(["hello world"])
        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(vectors[0]), 384)
        self.assertTrue(all(isinstance(v, float) for v in vectors[0]))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
