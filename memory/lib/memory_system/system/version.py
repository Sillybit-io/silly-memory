"""Version utilities — read, parse, and compare the shipped ``VERSION`` file.

Read this first if you are new to the codebase:
  - ``installed_version(home)`` reads ``<home>/VERSION`` and returns the
    trimmed string. Missing or unreadable files degrade to
    ``UNKNOWN_VERSION`` rather than raising.
  - ``parse_version("X.Y.Z+meta")`` returns
    ``(major, minor, patch, meta)``. Malformed strings degrade to
    ``(0, 0, 0, "unknown")`` so callers can compare any input safely.
  - ``compare_versions(a, b)`` returns -1/0/+1 like ``cmp``. Used by the
    upgrade flow to decide whether the on-disk version is older than the
    shipped code.

Public interface (imported elsewhere): ``UNKNOWN_VERSION``,
    ``installed_version``, ``parse_version``, ``compare_versions``,
    ``get_code_version``, ``CURRENT_VERSION``.
Depends on: stdlib only.
Used by: cli.doctor, status.banner.
"""
from __future__ import annotations

from pathlib import Path

UNKNOWN_VERSION = "0.0.0+unknown"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def installed_version(home: Path) -> str:
    try:
        text = (Path(home) / "VERSION").read_text(encoding="utf-8").strip()
        return text or UNKNOWN_VERSION
    except OSError:
        return UNKNOWN_VERSION


def parse_version(s: str) -> tuple[int, int, int, str]:
    try:
        main, meta = s.split("+", 1) if "+" in s else (s, "")
        parts = main.split(".")
        if len(parts) != 3:
            raise ValueError
        major, minor, patch = (int(part) for part in parts)
        return major, minor, patch, meta
    except Exception:
        return 0, 0, 0, "unknown"


def compare_versions(a: str, b: str) -> int:
    pa = parse_version(a)[:3]
    pb = parse_version(b)[:3]
    return (pa > pb) - (pa < pb)


def get_code_version() -> str:
    """Read the VERSION file shipped alongside this code.

    Tries the installed layout (<memory home>/VERSION, parents[3]) first,
    then the repo layout (<repo>/VERSION, parents[4]). Returns UNKNOWN_VERSION
    if neither is readable.
    """
    here = Path(__file__).resolve()
    for candidate in (here.parents[3] / "VERSION", here.parents[4] / "VERSION"):
        try:
            if candidate.exists():
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return UNKNOWN_VERSION


def _current_version() -> str:
    return get_code_version()


CURRENT_VERSION = _current_version()
