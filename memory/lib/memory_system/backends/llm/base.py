from __future__ import annotations

from typing import Protocol


class LLMBackend(Protocol):
    def classify(self, text: str, categories: list[str]) -> tuple[str, float]:
        ...

    def condense(self, observations: list[str]) -> str:
        ...

    def extract_facts(self, prompt: str, response: str) -> list[dict[str, object]]:
        ...

    def is_available(self) -> bool:
        ...
