from __future__ import annotations


class NoopEmbeddingBackend:
    name: str = "noop"
    dim: int = 384

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]

    def is_available(self) -> bool:
        return True
