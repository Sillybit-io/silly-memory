from __future__ import annotations

import fcntl
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockTimeout(Exception):
    """Raised when a file lock cannot be acquired within the timeout."""


@contextmanager
def file_lock(path: Path, timeout: float | None = None) -> Iterator[None]:
    """Acquire an exclusive advisory lock.

    timeout=None blocks indefinitely (use only in background workers). A positive
    timeout polls non-blockingly and raises LockTimeout if it can't acquire in
    time — callers on the hook hot path use this to stay fail-open and never hang
    the shell if another process is holding the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+", encoding="utf-8")
    try:
        if timeout is None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(str(path))
                    time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
