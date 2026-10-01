from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch


class TestBackends(unittest.TestCase):
    def test_protocol_modules_import_without_ml_libraries(self) -> None:
        before = set(sys.modules)

        from memory.lib.memory_system.backends.embedding import base as embedding_base
        from memory.lib.memory_system.backends.llm import base as llm_base

        self.assertTrue(hasattr(embedding_base, "EmbeddingBackend"))
        self.assertTrue(hasattr(llm_base, "LLMBackend"))
        after = set(sys.modules)
        self.assertFalse({"torch", "sentence_transformers", "fastembed"} & (after - before))

    def test_noop_embedding_backend_encodes_zero_vectors(self) -> None:
        from memory.lib.memory_system.backends.embedding.noop_backend import NoopEmbeddingBackend

        backend = NoopEmbeddingBackend()

        self.assertEqual(backend.name, "noop")
        self.assertEqual(backend.dim, 384)
        self.assertTrue(backend.is_available())
        vectors = backend.encode(["hello"])
        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(vectors[0]), 384)
        self.assertTrue(all(value == 0.0 for value in vectors[0]))

    def test_noop_llm_backend_raises_policy_message(self) -> None:
        from memory.lib.memory_system.backends.llm.noop_backend import NoopLLMBackend

        backend = NoopLLMBackend()

        self.assertFalse(backend.is_available())
        with self.assertRaisesRegex(NotImplementedError, "company policy"):
            _ = backend.classify("x", ["a", "b"])

    def test_factory_defaults_to_noop_backends_from_config(self) -> None:
        from memory.lib.memory_system.backends.embedding.noop_backend import NoopEmbeddingBackend
        from memory.lib.memory_system.backends.factory import get_embedding_backend, get_llm_backend
        from memory.lib.memory_system.backends.llm.noop_backend import NoopLLMBackend

        config = {"observe_model": "composer-2.5-fast", "reflect_model": "composer-2.5-fast"}

        embedding = get_embedding_backend(config)
        llm = get_llm_backend(config)

        self.assertIsInstance(embedding, NoopEmbeddingBackend)
        self.assertIsInstance(llm, NoopLLMBackend)

    def test_factory_falls_back_to_noop_when_fastembed_missing(self) -> None:
        from memory.lib.memory_system.backends.embedding.noop_backend import NoopEmbeddingBackend
        from memory.lib.memory_system.backends.factory import get_embedding_backend

        with patch.dict(os.environ, {"MEMORY_EMBEDDING_BACKEND": "fastembed"}, clear=False):
            backend = get_embedding_backend({})

        self.assertIsInstance(backend, NoopEmbeddingBackend)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
