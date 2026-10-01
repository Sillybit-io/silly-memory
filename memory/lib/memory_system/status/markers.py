"""Status marker files — first-run + post-upgrade hints written into memory_home.

Read this first if you are new to the codebase:
  - Two marker files live under ``memory_home()``: ``.first-run-seen``
    (presence means we've shown the welcome banner already) and
    ``.upgraded-from`` (carries the previous version so memstatus can show
    a one-time upgrade summary).
  - ``_consume_upgrade_marker`` deletes the file so the upgrade banner is
    shown exactly once. ``_write_first_run_marker`` is best-effort — a
    write failure does NOT crash memstatus.
  - Read failures fall back to ``None`` rather than raising, so a
    permission glitch on the marker file never blanks out the dashboard.

Public interface (imported elsewhere): ``FIRST_RUN_MARKER``,
    ``UPGRADE_MARKER``, ``_first_run_marker_path``,
    ``_upgrade_marker_path``, ``_is_first_run``, ``_read_upgrade_marker``,
    ``_consume_upgrade_marker``, ``_write_first_run_marker``.
Depends on: system.config.
Used by: status (package init re-export), status.banner.
"""
from __future__ import annotations

import os
from pathlib import Path

from memory_system.system.config import memory_home

FIRST_RUN_MARKER = ".first-run-seen"
UPGRADE_MARKER = ".upgraded-from"


def _first_run_marker_path() -> Path:
    return memory_home() / FIRST_RUN_MARKER


def _upgrade_marker_path() -> Path:
    return memory_home() / UPGRADE_MARKER


def _is_first_run() -> bool:
    return not _first_run_marker_path().exists()


def _read_upgrade_marker() -> str | None:
    p = _upgrade_marker_path()
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _consume_upgrade_marker() -> None:
    try:
        _upgrade_marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def _write_first_run_marker() -> bool:
    """Fail-open: any OSError swallowed so banner emission never blocks status
    output, and (intentionally) the banner will reappear next run.
    """
    target = _first_run_marker_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    tmp = target.parent / f"{FIRST_RUN_MARKER}.tmp.{os.getpid()}"
    try:
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                tmp.unlink()
            except OSError:
                return False
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # Why: even if write/fsync raises, the file descriptor MUST close — otherwise the marker file stays locked.
        try:
            _ = os.write(fd, b"1\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(target))
        try:
            os.chmod(str(target), 0o600)
        except OSError:
            pass
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
