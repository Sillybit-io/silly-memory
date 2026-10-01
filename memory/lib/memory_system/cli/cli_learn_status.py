# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false

"""Read-only telemetry for the self-teaching loop (``memory memlearn-status``).

Read this first if you are new to the codebase:
  - Reports five numbers: corrections detected, memories reinforced,
    memories demoted, contradictions flagged, and the timestamp of the
    last clarification asked. Each surfaces a different part of the
    self-teaching pipeline (corrections/reinforcements live in
    ``learning/``; contradictions in ``learning.contradiction``; active
    questioning in ``learning.active_questioning``).
  - The "last 24h" slice for reinforced/demoted is a PROXY: counters are
    not timestamped per increment, so an entry is counted as
    "touched in 24h" only when its ``last_accessed_iso`` falls inside the
    window AND the relevant counter is non-zero. Totals are exact.
  - Strictly read-only. Adding any write here is a bug — telemetry must
    never feed back into the data it observes.

Public interface (imported elsewhere): ``collect_learn_status``,
    ``render_learn_status``.
Depends on: learning.active_questioning (for the state filename),
    learning.contradiction (lazy-imported inside ``_count_contradictions``),
    paths.
Used by: ``bin/memory learn-status``.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from memory_system.learning.active_questioning import STATE_FILENAME as _ACTIVE_Q_STATE
from memory_system.paths import workspace_store

_CORRECTIONS_FILENAME = "corrections.jsonl"


def _parse_iso(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _count_corrections(store: Path, cutoff: datetime) -> tuple[int, int]:
    """Return ``(total, last_24h)`` from ``corrections.jsonl``."""
    path = store / _CORRECTIONS_FILENAME
    if not path.exists():
        return (0, 0)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return (0, 0)
    total = 0
    last_24h = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            rec: Any = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        total += 1
        ts = _parse_iso(str(rec.get("ts") or ""))
        if ts is not None and ts >= cutoff:
            last_24h += 1
    return (total, last_24h)


def _sidecar_counts(bank_dir: Path, cutoff: datetime) -> dict[str, int]:
    """Aggregate reinforced/demoted entry counts across every sidecar.

    Uses ``last_accessed_iso >= cutoff`` as a 24h proxy: the counters
    themselves are not timestamped per increment, so an entry is counted as
    "touched in last 24h" only when its last-access timestamp falls inside
    the window AND the relevant counter is non-zero.
    """
    out: dict[str, int] = {
        "reinforced_total": 0,
        "reinforced_24h": 0,
        "demoted_total": 0,
        "demoted_24h": 0,
    }
    if not bank_dir.exists():
        return out
    for sidecar in sorted(bank_dir.glob("*.score.json")):
        try:
            data: Any = _json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            try:
                reinf = int(entry.get("reinforcements_count") or 0)
                corr = int(entry.get("corrections_count") or 0)
            except (TypeError, ValueError):
                continue
            last_raw = entry.get("last_accessed_iso") or entry.get("created_iso")
            last_ts = _parse_iso(str(last_raw)) if isinstance(last_raw, str) else None
            if reinf > 0:
                out["reinforced_total"] += 1
                if last_ts is not None and last_ts >= cutoff:
                    out["reinforced_24h"] += 1
            if corr > 0:
                out["demoted_total"] += 1
                if last_ts is not None and last_ts >= cutoff:
                    out["demoted_24h"] += 1
    return out


def _count_contradictions(bank_dir: Path) -> int:
    """Invoke the contradiction detector live against current bank files.

    Local import so a missing or broken ``learning.contradiction`` module
    degrades to 0 instead of crashing the CLI.
    """
    if not bank_dir.exists():
        return 0
    bank_files = sorted(bank_dir.glob("*.md"))
    if not bank_files:
        return 0
    try:
        from memory_system.learning.contradiction import detect_contradictions

        return len(detect_contradictions(bank_files))
    except Exception:
        return 0


def _last_question(store: Path) -> dict[str, str | None]:
    """Read ``.active_q_state.json`` and return ``{session_id, ts}``.

    Active questioning only persists the most recent question (session id +
    ts); a true "total asked" counter would require a new log file, which is
    out of scope here. Missing or malformed state degrades to nulls.
    """
    path = store / _ACTIVE_Q_STATE
    if not path.exists():
        return {"session_id": None, "ts": None}
    try:
        data: Any = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return {"session_id": None, "ts": None}
    if not isinstance(data, dict):
        return {"session_id": None, "ts": None}
    sid = data.get("last_question_session_id")
    ts = data.get("last_question_ts")
    return {
        "session_id": str(sid) if sid else None,
        "ts": str(ts) if ts else None,
    }


def collect_learn_status(
    workspace: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return structured self-teaching telemetry for ``workspace``."""
    store = workspace_store(workspace)
    bank_dir = store / "memory-bank"
    actual_now = now or datetime.now(timezone.utc)
    cutoff = actual_now - timedelta(hours=24)
    corr_total, corr_24h = _count_corrections(store, cutoff)
    counts = _sidecar_counts(bank_dir, cutoff)
    return {
        "workspace": str(workspace),
        "now": actual_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "corrections": {"total": corr_total, "last_24h": corr_24h},
        "reinforced": {
            "total": counts["reinforced_total"],
            "last_24h": counts["reinforced_24h"],
        },
        "demoted": {
            "total": counts["demoted_total"],
            "last_24h": counts["demoted_24h"],
        },
        "contradictions_active": _count_contradictions(bank_dir),
        "active_questioning": _last_question(store),
    }


def render_learn_status(
    *,
    workspace: Path,
    json_output: bool = False,
    output_fn: Callable[[str], None] = print,
    now: datetime | None = None,
) -> int:
    """Pretty-print the self-teaching loop status. Returns 0 on success."""
    data = collect_learn_status(workspace, now=now)
    if json_output:
        output_fn(_json.dumps(data, indent=2, sort_keys=True))
        return 0

    lines: list[str] = []
    lines.append(f"Learning Status — workspace: {Path(workspace).name}")
    lines.append("=" * 48)
    corr = data["corrections"]
    lines.append(
        f"Corrections detected:           total={corr['total']}  last 24h={corr['last_24h']}"
    )
    reinf = data["reinforced"]
    lines.append(
        f"Memories reinforced:            total={reinf['total']}  last 24h={reinf['last_24h']}"
    )
    dem = data["demoted"]
    lines.append(
        f"Memories demoted:               total={dem['total']}  last 24h={dem['last_24h']}"
    )
    lines.append(f"Contradictions flagged (active): {data['contradictions_active']}")
    aq = data["active_questioning"]
    if aq["session_id"]:
        lines.append(
            f"Last clarification asked:       session={aq['session_id']}  ts={aq['ts']}"
        )
    else:
        lines.append("Last clarification asked:       not yet recorded")
    output_fn("\n".join(lines))
    return 0


__all__ = ["collect_learn_status", "render_learn_status"]
