"""Monthly observation archive — append-only history of ``observations.md``.

Read this first if you are new to the codebase:
  - ``archive_observations(workspace_root, text)`` appends to
    ``<store>/observations-archive/YYYY-MM.md`` using the workspace lock,
    one file per calendar month, so the full history never grows
    unbounded in a single file.
  - The archive folder ships a ``.gitignore`` that ignores everything
    except itself — committing observations would leak prompts and
    is forbidden.
  - ``reflector_v2`` calls this BEFORE rewriting the live
    ``observations.md`` so condensation can never silently lose data.

Public interface (imported elsewhere): ``archive_observations`` (and the
    helper paths exposed for tests).
Depends on: paths, safety.
Used by: reflection.reflector_v2.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from memory_system.paths import lock_path, workspace_store
from memory_system.safety import atomic_write, file_lock


def _coerce_workspace(workspace_root: str | Path) -> Path:
    return Path(workspace_root)


def _archive_dir(store: Path) -> Path:
    return store / "observations-archive"


def _ensure_archive_layout(store: Path) -> Path:
    archive_dir = _archive_dir(store)
    archive_dir.mkdir(parents=True, exist_ok=True)
    gitignore = archive_dir / ".gitignore"
    gitignore_text = "*\n!.gitignore\n"
    if not gitignore.exists() or gitignore.read_text(encoding="utf-8") != gitignore_text:
        atomic_write(gitignore, gitignore_text)
    return archive_dir


def _month_key() -> str:
    return _utcnow().strftime("%Y-%m")


def _archive_stamp() -> str:
    return _utcnow().replace(microsecond=0).isoformat() + "Z"


def _utcnow() -> datetime.datetime:
    # Naive UTC: callers format with isoformat() + "Z" and must not get "+00:00".
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _archive_path(store: Path, year_month: str) -> Path:
    return _ensure_archive_layout(store) / f"{year_month}.md"


def archive_observations(ws: str | Path) -> Path | None:
    store = workspace_store(_coerce_workspace(ws))
    obs_path = store / "observations.md"
    archive_path = _archive_path(store, _month_key())

    with file_lock(lock_path(store)):
        if not obs_path.exists():
            return None
        observations = obs_path.read_text(encoding="utf-8")
        if not observations.strip():
            return None
        existing = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
        chunk = f"\n## Archived {_archive_stamp()}\n{observations.rstrip()}\n"
        atomic_write(archive_path, existing + chunk)
    return archive_path


def read_archive(ws: str | Path, year_month: str | None = None) -> str:
    store = workspace_store(_coerce_workspace(ws))
    archive_dir = _archive_dir(store)
    if not archive_dir.exists():
        return ""

    if year_month:
        path = archive_dir / (year_month if year_month.endswith(".md") else f"{year_month}.md")
        return path.read_text(encoding="utf-8") if path.exists() else ""

    parts = [p.read_text(encoding="utf-8") for p in sorted(archive_dir.glob("*.md")) if p.name != ".gitignore"]
    return "".join(parts)
