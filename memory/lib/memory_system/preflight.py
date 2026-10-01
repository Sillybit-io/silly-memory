"""Install-time health probes — checks that must pass before we touch a store.

Read this first if you are new to the codebase:
  - ``check_fts5_available()`` proves the bundled SQLite was compiled with
    the FTS5 full-text index; without it, ``memrecall`` cannot work.
  - ``check_sync_filesystem()`` warns when the user is about to put the
    store inside iCloud, Dropbox, OneDrive, etc. SQLite databases corrupt
    when a sync client edits them mid-write.
  - Both probes are read-only: they answer ``(ok, message)`` and never mutate
    the filesystem. The installer is responsible for deciding what to do
    with a False result.

Public interface (imported elsewhere): ``check_fts5_available``,
    ``check_sync_filesystem``.
Depends on: stdlib only (os, sqlite3, sys, pathlib).
Used by: install/upgrade entry points (called via ``python -m`` from shell
    scripts; no in-package importers as of T22).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def check_fts5_available() -> tuple[bool, str | None]:
    try:
        _probe_fts5()
    except sqlite3.OperationalError:
        return False, (
            f"FTS5 not available in sqlite3 build. Python {sys.version.split()[0]}. "
            "Try: brew reinstall python3 --build-from-source"
        )
    return True, None


def check_sync_filesystem(memory_home: Path) -> tuple[bool, str | None]:
    path_text = str(Path(memory_home).expanduser().resolve())
    path_text_lower = path_text.lower()
    sync_markers = (
        "/library/mobile documents/",
        "/documents/",
        "/desktop/",
        "dropbox",
        "google drive",
        "onedrive",
        "sync",
        "drive",
    )
    if any(marker in path_text_lower for marker in sync_markers):
        return False, (
            f"Warning: memory home is on a sync filesystem ({path_text}). "
            "SQLite may corrupt data there; use a local path like ~/.silly-memory."
        )
    return True, None


def _probe_fts5() -> None:
    if os.environ.get("MEMORY_FORCE_NO_FTS5"):
        raise sqlite3.OperationalError("forced missing FTS5")
    conn = sqlite3.connect(":memory:")
    # Why: the throwaway probe connection must close even if FTS5 is missing — leaving it open leaks a SQLite handle on every startup.
    try:
        _ = conn.execute("CREATE VIRTUAL TABLE _t USING fts5(x)")
    finally:
        conn.close()
