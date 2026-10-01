"""Lifecycle backup — timestamped snapshots of a store before risky changes.

Read this first if you are new to the codebase:
  - ``snapshot_store(memory_home, tag)`` produces a sibling directory
    named ``.backup-YYYY-MM-DD-HHMMSS-<tag>`` next to the live store.
    Migrations and other destructive operations MUST call this first.
  - ``_IGNORED_PATTERNS`` excludes SQLite WAL/SHM journals, ``.lock``
    files, and previous backups so a snapshot never copies a half-written
    journal or recursively pulls in older backup snapshots.
  - Restore is intentionally a manual ``cp -R`` — automating it from code
    invites accidental data loss; backup names are designed to be
    obvious so a human can restore by hand.

Public interface (imported elsewhere): ``snapshot_store`` (and the
    rest of the high-level snapshot helpers exported by this module).
Depends on: safety (for ``atomic_write``).
Used by: lifecycle.export_bundle (the snapshot taken before an import).
"""
from __future__ import annotations

import datetime as _datetime
import json
import os
import shutil
import sys
from pathlib import Path

from memory_system.safety import atomic_write

_IGNORED_PATTERNS = (
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.lock",
    ".backup-*",
    ".backup-*.partial",
)


def _snapshot_name(tag: str) -> str:
    ts = _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    return f".backup-{ts}-{tag}"


def _ignore_factory():
    return shutil.ignore_patterns(*_IGNORED_PATTERNS)


def _count_files(src: Path) -> int:
    ignore = _ignore_factory()
    count = 0
    for root, dirs, files in os.walk(src):
        ignored = set(ignore(root, dirs + files))
        dirs[:] = [d for d in dirs if d not in ignored]
        count += sum(1 for name in files if name not in ignored)
    return count


def snapshot_store(memory_home: Path, tag: str) -> Path:
    if not memory_home.exists():
        raise FileNotFoundError(str(memory_home))
    if not memory_home.is_dir():
        raise NotADirectoryError(str(memory_home))

    snapshot = memory_home / _snapshot_name(tag)
    partial = Path(f"{snapshot}.partial")
    ignore = _ignore_factory()

    if partial.exists():
        shutil.rmtree(partial, ignore_errors=True)

    try:
        _ = shutil.copytree(memory_home, partial, dirs_exist_ok=False, ignore=ignore)
        manifest = {
            "tag": tag,
            "ts": _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%d-%H%M%S"),
            "files_copied": _count_files(memory_home),
            "source_path": str(memory_home),
            "python_version": sys.version,
        }
        atomic_write(partial / "manifest.json", json.dumps(manifest, indent=2), mode=0o644)
        os.rename(partial, snapshot)
        return snapshot
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def list_snapshots(memory_home: Path) -> list[Path]:
    if not memory_home.exists():
        raise FileNotFoundError(str(memory_home))
    if not memory_home.is_dir():
        raise NotADirectoryError(str(memory_home))

    snaps = [
        path
        for path in memory_home.iterdir()
        if path.is_dir() and path.name.startswith(".backup-") and not path.name.endswith(".partial")
    ]
    return sorted(snaps, key=lambda p: p.stat().st_mtime, reverse=True)


def restore_snapshot(memory_home: Path, snapshot_path: Path, *, confirm_token: str) -> None:
    if confirm_token != "YES-RESTORE":
        raise ValueError("restore confirmation token required")
    if not memory_home.exists():
        raise FileNotFoundError(str(memory_home))
    if not snapshot_path.exists():
        raise FileNotFoundError(str(snapshot_path))
    if not snapshot_path.is_dir():
        raise NotADirectoryError(str(snapshot_path))
    if not memory_home.is_dir():
        raise NotADirectoryError(str(memory_home))

    snapshot_resolved = snapshot_path.resolve()
    for child in list(memory_home.iterdir()):
        if child.resolve() == snapshot_resolved:
            continue
        if child.name.startswith(".backup-"):
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    _ = shutil.copytree(snapshot_path, memory_home, dirs_exist_ok=True, ignore=_ignore_factory())
