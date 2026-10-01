from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .paths import bank_path, global_store, lock_path, workspace_store
from .safety import file_lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  path TEXT NOT NULL,
  section TEXT,
  content TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  scope, path, section, content, content='documents', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, scope, path, section, content)
  VALUES (new.id, new.scope, new.path, new.section, new.content);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, scope, path, section, content)
  VALUES('delete', old.id, old.scope, old.path, old.section, old.content);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, scope, path, section, content)
  VALUES('delete', old.id, old.scope, old.path, old.section, old.content);
  INSERT INTO documents_fts(rowid, scope, path, section, content)
  VALUES (new.id, new.scope, new.path, new.section, new.content);
END;
"""


def db_path(store: Path) -> Path:
    return store / "memory.sqlite"


def connect(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(store))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def rebuild_index(store: Path, scope: str) -> int:
    count = 0
    bank = store / "memory-bank"
    with file_lock(lock_path(store)):
        conn = connect(store)
        conn.execute("DELETE FROM documents WHERE scope = ?", (scope,))
        if bank.exists():
            for md in bank.glob("*.md"):
                text = md.read_text(encoding="utf-8")
                for section, body in _split_sections(text):
                    conn.execute(
                        "INSERT INTO documents(scope, path, section, content, updated_at) VALUES (?,?,?,?,datetime('now'))",
                        (scope, md.name, section, body),
                    )
                    count += 1
        obs = store / "observations.md"
        if obs.exists():
            conn.execute(
                "INSERT INTO documents(scope, path, section, content, updated_at) VALUES (?,?,?,?,datetime('now'))",
                (scope, "observations.md", "all", obs.read_text(encoding="utf-8")),
            )
            count += 1
        conn.commit()
        conn.close()
    return count


def _split_sections(text: str) -> Iterable[tuple[str, str]]:
    current = "root"
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if buf:
                yield current, "\n".join(buf)
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        yield current, "\n".join(buf)


def _sanitize_fts(query: str) -> str:
    """Make an arbitrary user string safe for an FTS5 MATCH expression.

    FTS5 treats characters like '-', '+', '(', '"', ':' as operators/syntax, so a
    raw query such as "follow-up" or "t+2" raises an OperationalError. We tokenize
    on whitespace and wrap each token as a quoted string literal (doubling embedded
    quotes), joined by space (implicit AND). Empty input becomes a no-match.
    """
    tokens = query.split()
    if not tokens:
        return '""'
    return " ".join('"' + tok.replace('"', '""') + '"' for tok in tokens)


def recall(store: Path, query: str, limit: int = 10) -> list[dict[str, str]]:
    if not db_path(store).exists():
        rebuild_index(store, store.name)
    conn = connect(store)
    rows = conn.execute(
        """
        SELECT d.scope, d.path, d.section, snippet(documents_fts, 3, '[', ']', '…', 20) AS snippet
        FROM documents_fts
        JOIN documents d ON documents_fts.rowid = d.id
        WHERE documents_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (_sanitize_fts(query), limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recall_all(workspace_root: Path, query: str, limit: int = 10) -> list[dict[str, str]]:
    ws = workspace_store(workspace_root)
    gs = global_store()
    results: list[dict[str, str]] = []
    for store in (gs, ws):
        if db_path(store).exists() or (store / "memory-bank").exists():
            try:
                results.extend(recall(store, query, limit=limit))
            except sqlite3.OperationalError:
                rebuild_index(store, store.name)
                results.extend(recall(store, query, limit=limit))
    return results[:limit]
