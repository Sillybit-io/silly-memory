from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from typing import ClassVar, cast

from ... import privacy

_CURSOR_AGENT_BLOCKED = "cursor-agent blocked: MEMORY_ALLOW_NETWORK=0. Set MEMORY_ALLOW_NETWORK=1 and re-run."


class CursorAgentBackend:
    name: ClassVar[str] = "cursor-agent"

    def classify(self, text: str, categories: list[str]) -> tuple[str, float]:
        privacy.assert_offline("cursor-agent subprocess")
        if not categories:
            return "unknown", 0.0
        prompt = "".join(
            [
                "Classify the memory text into exactly one category from this list: ",
                f"{', '.join(categories)}. Return JSON shaped as ",
                '{"category":"<category>","confidence":0.0}.\n\n',
                f"Text:\n{text}",
            ]
        )
        raw = self._send_prompt(prompt)
        parsed = _json_object(raw)
        if parsed is None:
            return "unknown", 0.0
        category = str(parsed.get("category") or "unknown")
        if category not in categories:
            category = "unknown"
        confidence = _float_between_zero_one(parsed.get("confidence"))
        return category, confidence

    def condense(self, observations: list[str]) -> str:
        privacy.assert_offline("cursor-agent subprocess")
        return self._send_prompt("\n".join(observations)).strip()

    def extract_facts(self, prompt: str, response: str) -> list[dict[str, object]]:
        privacy.assert_offline("cursor-agent subprocess")
        raw = self._send_prompt(
            "".join(
                [
                    "Extract durable memory facts from the prompt and response. ",
                    "Return only a JSON array of objects.\n\n",
                    f"Prompt:\n{prompt}\n\nResponse:\n{response}",
                ]
            )
        )
        try:
            data = cast(object, json.loads(raw))
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        facts: list[dict[str, object]] = []
        for item in cast(list[object], data):
            if isinstance(item, dict):
                facts.append({str(key): value for key, value in cast(dict[object, object], item).items()})
        return facts

    def is_available(self) -> bool:
        if not privacy.network_allowed():
            return False
        if shutil.which("cursor-agent") is None:
            return False
        # `about` answers locally; `status` blocks on a locked keychain or a logged-out CLI.
        try:
            result = subprocess.run(
                ["cursor-agent", "about", "--format", "json"], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
        about = _json_object(result.stdout)
        if about is None:
            return False
        return bool(about.get("userEmail")) or bool(os.environ.get("CURSOR_API_KEY"))

    def _send_prompt(self, text: str) -> str:
        if not privacy.network_allowed():
            raise RuntimeError(_CURSOR_AGENT_BLOCKED)
        # Prompt on stdin; `ask` mode keeps the agent read-only (plain --print grants write + shell tools).
        result = subprocess.run(
            ["cursor-agent", "--print", "--trust", "--mode", "ask", "--output-format", "text"],
            input=text,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or "cursor-agent --print failed"
            raise RuntimeError(message)
        return result.stdout


def _json_object(raw: str) -> dict[str, object] | None:
    try:
        data = cast(object, json.loads(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, Mapping):
        return None
    mapping = cast(Mapping[object, object], data)
    return {str(key): value for key, value in mapping.items()}


def _float_between_zero_one(value: object) -> float:
    if not isinstance(value, (int, float, str)):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


__all__ = ["CursorAgentBackend"]
