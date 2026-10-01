"""Events subpackage — capture, queue, and process Cursor hook activity.

Read this first if you are new to the codebase:
  - Three files: ``events.py`` (write hook events to ``events.jsonl``),
    ``observer.py`` (turn raw events into observation bullets), and
    ``worker.py`` (run the heavy distill/reflect/index pass under a lock).
  - This ``__init__.py`` re-exports ``events.py``, so callers write
    ``from memory_system.events import …``.
  - All disk writes go through the safety helpers (``atomic_write`` +
    ``file_lock``); raw open() inside this subpackage is a code smell.

Public interface (imported elsewhere): everything exported by
    ``events.events`` (``content_hash``, ``estimate_unobserved_tokens``,
    ``rotate_events``, ``enqueue_job``, …).
Depends on: events.events (star re-export).
Used by: recall.distiller, bin/memory.
"""
from .events import *  # noqa: F401, F403
