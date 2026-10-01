"""Private-profile sync — routes ``profile.md`` into recognized global bank files.

Read this first if you are new to the codebase:
  - The user's private profile lives at
    ``<memory_home>/_global/memory-bank/profile.md``. It is a human-editable
    SOURCE file seeded once by ``install.sh`` and preserved on reinstall. It is
    NOT one of ``scope.GLOBAL_FILES`` (``learned-memories.md``,
    ``audienceContext.md``, ``conventions.md``), so the context pack never reads
    it directly — a fact typed there would silently never surface (Oracle O-4).
  - ``sync_profile()`` is the bridge. It parses ``profile.md`` into ``##``
    sections, routes them by title into the RECOGNIZED global bank files —
    identity/role/company/stakeholders → ``audienceContext.md``, hard preferences
    → ``learned-memories.md`` — writing each inside a marker-delimited managed
    block (``<!-- profile:start -->`` … ``<!-- profile:end -->``), then rebuilds
    the ``_global`` FTS index so ``memory recall`` sees the new content.
  - The write is idempotent and non-destructive: only the managed block is ever
    replaced. Content a user or ``/add-memory`` wrote outside the markers is left
    byte-for-byte intact, and re-running yields identical files. Guidance in the
    template (HTML comments and ``>`` blockquotes) is stripped, so an unfilled
    section contributes nothing.
  - This module deliberately does NOT touch ``scope.py`` or the ``config.json``
    global-file filter (both are no-go): routing through the recognized files is
    what makes the profile surface without editing the injector.

Public interface (imported elsewhere): ``sync_profile`` (and the marker
    constants / ``SECTION_ROUTES`` used by tests).
Depends on: paths (global_store, bank_path, ensure_layout, GLOBAL_ID), index
    (rebuild_index). Stdlib-only otherwise (re) — no network imports, enforced by
    ``memory/tests/test_no_forbidden_imports.py``.
Used by: the ``memory profile-sync`` CLI handler (``bin/memory:cmd_profile_sync``),
    which lazy-imports this module inside the handler to keep cold start cheap.
"""
from __future__ import annotations

import re
from pathlib import Path

from memory_system.index import rebuild_index
from memory_system.paths import GLOBAL_ID, bank_path, ensure_layout, global_store

PROFILE_FILENAME = "profile.md"
AUDIENCE_FILE = "audienceContext.md"
LEARNED_FILE = "learned-memories.md"

START = "<!-- profile:start -->"
END = "<!-- profile:end -->"
MANAGED_NOTE = (
    "<!-- Managed by `memory profile-sync` from profile.md. "
    "Edit profile.md and re-run; do not edit inside these markers. -->"
)

# Section title (lowercased) → recognized global bank file it routes into.
SECTION_ROUTES: dict[str, str] = {
    "identity": AUDIENCE_FILE,
    "role": AUDIENCE_FILE,
    "company": AUDIENCE_FILE,
    "key stakeholders": AUDIENCE_FILE,
    "hard preferences": LEARNED_FILE,
}

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _clean_lines(lines: list[str]) -> list[str]:
    """Keep real content: drop ``>`` guidance blockquotes, trim edge blanks."""
    kept = [ln.rstrip() for ln in lines if not ln.lstrip().startswith(">")]
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def _parse_sections(raw: str) -> list[tuple[str, list[str]]]:
    """Split profile text into ``(title, content_lines)`` in document order.

    HTML comments (single- and multi-line) are stripped first, so template
    guidance never leaks into the bank. Only ``## `` headings start a section;
    text before the first one (the local-only banner) is ignored.
    """
    text = _COMMENT_RE.sub("", raw)
    sections: list[tuple[str, list[str]]] = []
    title: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if title is not None:
                sections.append((title, _clean_lines(buf)))
            title = line[3:].strip()
            buf = []
        elif title is not None:
            buf.append(line)
    if title is not None:
        sections.append((title, _clean_lines(buf)))
    return sections


def _grouped_blocks(sections: list[tuple[str, list[str]]]) -> tuple[list[str], list[str]]:
    """Return ``(audience_parts, learned_parts)`` — rendered ``### `` sub-blocks.

    A section is emitted only if it routes to a known file AND has real content,
    so unfilled template sections and unknown headings are skipped.
    """
    audience: list[str] = []
    learned: list[str] = []
    for title, lines in sections:
        route = SECTION_ROUTES.get(title.strip().lower())
        if route is None or not lines:
            continue
        block = f"### {title.strip()}\n\n" + "\n".join(lines) + "\n"
        (audience if route == AUDIENCE_FILE else learned).append(block)
    return audience, learned


def _managed_block(parts: list[str]) -> str:
    """Render the marker-delimited managed block (deterministic → idempotent)."""
    if parts:
        inner = "\n".join(p.rstrip() for p in parts)
        return f"{START}\n{MANAGED_NOTE}\n\n{inner}\n{END}"
    return f"{START}\n{MANAGED_NOTE}\n{END}"


def _apply_block(old: str, block: str) -> str:
    """Upsert ``block`` into ``old`` text, replacing only between the markers.

    - Existing markers → replace the region between them, leaving all other
      content (before ``START`` / after ``END``) byte-for-byte intact.
    - No markers but existing content → append the block after it.
    - Empty/new file → the block is the whole file.
    """
    if START in old and END in old:
        pre = old.split(START, 1)[0]
        post = old.split(END, 1)[1]
        return pre + block + post
    if old.strip():
        return old.rstrip() + "\n\n" + block + "\n"
    return block + "\n"


def _sync_target(store: Path, filename: str, parts: list[str]) -> bool:
    """Write the managed block for one bank file. Returns True if bytes changed.

    A file with no incoming content and no pre-existing managed block is left
    untouched (we do not pollute it with empty markers).
    """
    path = bank_path(store, filename)
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    has_block = START in old and END in old
    if not parts and not has_block:
        return False
    new = _apply_block(old, _managed_block(parts))
    if new == old:
        return False
    _ = path.write_text(new, encoding="utf-8")
    return True


def sync_profile() -> dict[str, object]:
    """Parse ``_global/memory-bank/profile.md`` and route it into the bank files.

    Identity/role/company/stakeholders land in ``audienceContext.md`` and hard
    preferences in ``learned-memories.md`` — both recognized global files — inside
    a managed block, then the ``_global`` index is rebuilt. Idempotent: re-running
    replaces only the managed block and never disturbs outside content.

    Returns a summary dict: ``status`` is ``"no-profile"`` when the source file is
    absent (nothing seeded/filled yet) or ``"synced"`` otherwise, plus the list of
    bank files whose bytes changed and the rebuilt ``_global`` document count.
    """
    store = global_store()
    ensure_layout(store)
    profile = bank_path(store, PROFILE_FILENAME)
    if not profile.is_file():
        return {"status": "no-profile", "path": str(profile), "written": []}

    sections = _parse_sections(profile.read_text(encoding="utf-8"))
    audience_parts, learned_parts = _grouped_blocks(sections)

    written: list[str] = []
    if _sync_target(store, AUDIENCE_FILE, audience_parts):
        written.append(AUDIENCE_FILE)
    if _sync_target(store, LEARNED_FILE, learned_parts):
        written.append(LEARNED_FILE)

    indexed = rebuild_index(store, GLOBAL_ID)
    return {
        "status": "synced",
        "path": str(profile),
        "written": written,
        "sections": [t for t, _ in sections],
        "indexed_docs": indexed,
    }
