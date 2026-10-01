"""Doctor result cache — keeps the 9-check verdict around so memstatus stays fast.

Read this first if you are new to the codebase:
  - ``DOCTOR_CACHE_TTL_SEC = 300`` (5 minutes) is the budget. Inside that
    window, ``_read_doctor_cache`` returns the previous integrity verdict
    without re-running the doctor. Outside it, the caller recomputes.
  - Cache shape: ``{"ts": float, "integrity": "healthy|warn|error"}``.
    Any parse or read failure returns ``None`` so the caller recomputes —
    a corrupt cache must NEVER block the dashboard.
  - The cache file lives at ``<memory_home>/.doctor-cache.json`` — one
    cache per memory home, not per workspace, because the doctor's
    checks themselves are home-scoped.

Public interface (imported elsewhere): ``DOCTOR_CACHE``,
    ``DOCTOR_CACHE_TTL_SEC``, ``_doctor_cache_path``,
    ``_read_doctor_cache``, ``_write_doctor_cache``, ``_doctor_integrity``.
Depends on: system.config, cli.doctor (lazy).
Used by: status (package init re-export), status.banner.
"""
from __future__ import annotations

import contextlib
import io
import json
import time
from pathlib import Path

from memory_system.system.config import memory_home

DOCTOR_CACHE = ".doctor-cache.json"
DOCTOR_CACHE_TTL_SEC = 300  # 5 minutes — keeps memstatus latency <100ms.


def _doctor_cache_path() -> Path:
    return memory_home() / DOCTOR_CACHE


def _read_doctor_cache() -> str | None:
    """Return cached integrity level if cache is fresh (≤5min), else None.

    Cache shape: {"ts": float, "integrity": "healthy|warn|error"}. Any read
    or parse failure returns None so the caller recomputes — never blocks.
    """
    p = _doctor_cache_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        ts = float(data.get("ts", 0))
    except (TypeError, ValueError):
        return None
    if time.time() - ts > DOCTOR_CACHE_TTL_SEC:
        return None
    integrity = data.get("integrity")
    if isinstance(integrity, str) and integrity in ("healthy", "warn", "error"):
        return integrity
    return None


def _write_doctor_cache(integrity: str) -> None:
    """Fail-open: cache write failure is silently ignored — the next status
    call will simply recompute the integrity level.
    """
    p = _doctor_cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"ts": time.time(), "integrity": integrity}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _doctor_integrity() -> str:
    """Resolve memdoctor's top-level status with a 5-minute cache.

    Calls `doctor.run_doctor(json_output=True)` and captures stdout to parse
    the JSON payload's `status` field. Any failure degrades gracefully to
    "warn" so the banner still renders. Cached writes keep memstatus under
    its 100ms latency budget on subsequent invocations.
    """
    cached = _read_doctor_cache()
    if cached is not None:
        return cached
    integrity = "warn"
    try:
        from memory_system.cli import doctor
        buf = io.StringIO()
        # Why: capture whatever the doctor command would print so we can parse it as JSON without leaking text to the user's terminal.
        with contextlib.redirect_stdout(buf):
            _ = doctor.run_doctor(json_output=True)
        payload = json.loads(buf.getvalue() or "{}")
        status = payload.get("status")
        if isinstance(status, str) and status in ("healthy", "warn", "error"):
            integrity = status
    except Exception:
        integrity = "warn"
    _write_doctor_cache(integrity)
    return integrity
