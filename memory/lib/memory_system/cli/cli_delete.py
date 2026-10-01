"""User-approved delete CLI — single-entry removal across bank, sidecar, vectors.

Read this first if you are new to the codebase:
  - The CLI never deletes without explicit confirmation. ``--confirm`` is
    required on every operation; ``prune_review`` prompts per-entry unless
    you pass ``--confirm-all``.
  - An entry id has the form ``<filename>:<lineno>``; deletes touch THREE
    storage layers atomically — the bank markdown file, the per-bank
    ``.score.json`` sidecar, and the vector store row.
  - Prune ordering matters: when batching multiple deletes within one bank
    file, later line numbers are removed first so earlier deletes don't
    shift the line numbers of later targets.

Public interface (imported elsewhere): ``PruneCandidate``, ``delete_entry``,
    ``list_prune_candidates``, ``prune_review``, ``DEFAULT_PRUNE_FLOOR``,
    ``PREVIEW_CHARS``.
Depends on: lifecycle.scoring, paths, safety, storage.vector_store.
Used by: ``bin/memory delete`` and ``bin/memory prune-review``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from memory_system.lifecycle.scoring import (
    DEFAULT_SCORE,
    load_scores,
    save_scores,
)
from memory_system.paths import ensure_layout, workspace_store
from memory_system.safety import atomic_write, file_lock
from memory_system.storage.vector_store import VectorStore

DEFAULT_PRUNE_FLOOR: float = 0.1
PREVIEW_CHARS: int = 80


@dataclass(frozen=True)
class PruneCandidate:
    entry_id: str
    score: float
    bank_file: Path
    line_no: int
    preview: str


def _parse_entry_id(entry_id: str) -> tuple[str, int] | None:
    """Parse ``<filename>:<lineno>`` into (filename, lineno) or None."""
    if ":" not in entry_id:
        return None
    filename, _, lineno_s = entry_id.rpartition(":")
    if not filename or not lineno_s:
        return None
    try:
        lineno = int(lineno_s)
    except ValueError:
        return None
    if lineno < 1:
        return None
    return filename, lineno


def _bank_lock_path(bank_file: Path) -> Path:
    # Share the same advisory lock as scoring.update_score / bump_access so
    # concurrent score writes and entry deletes cannot interleave.
    return bank_file.parent / f"{bank_file.name}.score.json.lock"


def _bullet_line_at(bank_file: Path, lineno: int) -> str | None:
    if not bank_file.exists():
        return None
    try:
        lines = bank_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if lineno < 1 or lineno > len(lines):
        return None
    line = lines[lineno - 1]
    if not line.strip().startswith("- "):
        return None
    return line


def _remove_bank_line(bank_file: Path, lineno: int) -> None:
    text = bank_file.read_text(encoding="utf-8")
    had_trailing_nl = text.endswith("\n")
    lines = text.splitlines()
    if not (1 <= lineno <= len(lines)):
        return
    new_lines = lines[: lineno - 1] + lines[lineno:]
    new_text = "\n".join(new_lines)
    if had_trailing_nl and new_text:
        new_text += "\n"
    atomic_write(bank_file, new_text)


def delete_entry(
    *,
    workspace: Path,
    entry_id: str,
    confirm: bool,
    dry_run: bool = False,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Delete a memory entry by id.

    Returns:
        0 on success or dry-run preview.
        1 when the entry id is unparseable, the bank file is missing, or
          the line does not exist / is not a bullet.
        2 when ``confirm`` is False (no implicit delete).
    """
    if not confirm and not dry_run:
        output_fn("refused: --confirm required (no implicit delete)")
        return 2

    parsed = _parse_entry_id(entry_id)
    if parsed is None:
        output_fn(f"missing entry: {entry_id} (unparseable id)")
        return 1
    filename, lineno = parsed

    store = workspace_store(workspace)
    ensure_layout(store)
    bank_file = store / "memory-bank" / filename
    if _bullet_line_at(bank_file, lineno) is None:
        output_fn(f"missing entry: {entry_id}")
        return 1

    if dry_run:
        output_fn(f"dry-run: would delete {entry_id}")
        return 0

    with file_lock(_bank_lock_path(bank_file)):
        # Re-check under the lock so a concurrent writer cannot delete the
        # line out from under us between probe and rewrite.
        if _bullet_line_at(bank_file, lineno) is None:
            output_fn(f"missing entry: {entry_id}")
            return 1
        _remove_bank_line(bank_file, lineno)
        scores = load_scores(bank_file)
        if entry_id in scores:
            del scores[entry_id]
            save_scores(bank_file, scores)

    VectorStore(store).remove(entry_id)
    output_fn(f"deleted {entry_id}")
    return 0


def list_prune_candidates(
    *,
    workspace: Path,
    floor: float = DEFAULT_PRUNE_FLOOR,
) -> list[PruneCandidate]:
    """List sidecar entries whose score is at or below ``floor``.

    Skips orphan sidecar keys that no longer point at a bullet line so users
    are never asked to delete something that does not exist.
    """
    store = workspace_store(workspace)
    ensure_layout(store)
    bank_dir = store / "memory-bank"
    if not bank_dir.exists():
        return []

    out: list[PruneCandidate] = []
    for bank_file in sorted(bank_dir.glob("*.md")):
        scores = load_scores(bank_file)
        if not scores:
            continue
        try:
            lines = bank_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for entry_id, payload in scores.items():
            score = _coerce_score(payload)
            if score is None or score > floor:
                continue
            parsed = _parse_entry_id(entry_id)
            if parsed is None:
                continue
            filename, lineno = parsed
            if filename != bank_file.name:
                continue
            if not (1 <= lineno <= len(lines)):
                continue
            raw = lines[lineno - 1].strip()
            if not raw.startswith("- "):
                continue
            preview = raw[2:].strip()[:PREVIEW_CHARS]
            out.append(
                PruneCandidate(
                    entry_id=entry_id,
                    score=score,
                    bank_file=bank_file,
                    line_no=lineno,
                    preview=preview,
                )
            )

    out.sort(key=lambda c: (c.score, c.entry_id))
    return out


def _coerce_score(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("score", DEFAULT_SCORE)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def prune_review(
    *,
    workspace: Path,
    floor: float = DEFAULT_PRUNE_FLOOR,
    confirm_all: bool = False,
    dry_run: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Interactively review decay candidates and delete with confirmation.

    Interactive answers: ``y`` delete, ``n`` skip, ``all`` delete remaining,
    ``q`` abort. ``confirm_all=True`` bypasses the prompt and deletes every
    listed candidate (intended for ``--confirm-all`` and test harnesses).
    ``dry_run=True`` lists candidates but never deletes.
    """
    candidates = list_prune_candidates(workspace=workspace, floor=floor)
    if not candidates:
        output_fn(f"no decay candidates at score <= {floor}")
        return 0

    output_fn(f"{len(candidates)} decay candidate(s) at score <= {floor}:")
    for c in candidates:
        output_fn(
            f"  {c.entry_id}  score={c.score:.3f}  {c.bank_file.name}:{c.line_no}  {c.preview}"
        )

    if dry_run:
        output_fn("dry-run: no deletions performed")
        return 0

    to_delete: list[PruneCandidate] = []
    auto_remaining = confirm_all
    skipped = 0
    for c in candidates:
        if auto_remaining:
            to_delete.append(c)
            continue
        try:
            answer = input_fn(
                f"delete {c.entry_id} (score={c.score:.3f})? [y/n/all/q]: "
            ).strip().lower()
        except EOFError:
            output_fn("aborted: EOF on input")
            break
        if answer == "all":
            auto_remaining = True
            to_delete.append(c)
        elif answer == "q":
            output_fn("aborted by user")
            break
        elif answer == "y":
            to_delete.append(c)
        else:
            skipped += 1

    # Delete later lines in the same bank file FIRST so earlier deletes don't
    # shift the line number of the next target. Cross-file order is irrelevant
    # but stable for determinism.
    to_delete.sort(key=lambda c: (str(c.bank_file), -c.line_no))

    deleted = 0
    for c in to_delete:
        rc = delete_entry(
            workspace=workspace,
            entry_id=c.entry_id,
            confirm=True,
            output_fn=output_fn,
        )
        if rc == 0:
            deleted += 1
        else:
            skipped += 1

    output_fn(f"summary: deleted {deleted}, skipped {skipped}")
    return 0
