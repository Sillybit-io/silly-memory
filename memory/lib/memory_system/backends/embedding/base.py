from __future__ import annotations

from typing import ClassVar, Protocol


class EmbeddingBackend(Protocol):
    name: ClassVar[str]
    dim: ClassVar[int]

    def encode(self, texts: list[str]) -> list[list[float]]:
        ...

    def is_available(self) -> bool:
        ...
