from __future__ import annotations


_DISABLED_MESSAGE = "LLM disabled; enable via LLMBackend implementation when company policy allows"


class NoopLLMBackend:
    def classify(self, _text: str, _categories: list[str]) -> tuple[str, float]:
        raise NotImplementedError(_DISABLED_MESSAGE)

    def condense(self, _observations: list[str]) -> str:
        raise NotImplementedError(_DISABLED_MESSAGE)

    def extract_facts(self, _prompt: str, _response: str) -> list[dict[str, object]]:
        raise NotImplementedError(_DISABLED_MESSAGE)

    def is_available(self) -> bool:
        return False
