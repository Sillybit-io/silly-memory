from __future__ import annotations

from .system import config

_TRUE_VALUES = {"1", "true", "yes", "on"}


def network_allowed() -> bool:
    value = config.memory_allow_network().strip().lower()
    return value in _TRUE_VALUES


def assert_offline(reason: str) -> None:
    if network_allowed():
        return
    raise RuntimeError(f"{reason} blocked: MEMORY_ALLOW_NETWORK=0. Set MEMORY_ALLOW_NETWORK=1 and re-run.")


__all__ = ["assert_offline", "network_allowed"]
